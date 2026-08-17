"""LLM synthesis for `/research` — the only layer with a real per-call cost.

Strictly opt-in; `/search` and `/extract` never reach this module.

Three things bound the bill:

- Sources are clipped to LLM_MAX_SOURCE_CHARS before the call. Extracted pages run
  40k+ chars, so ten unclipped would be a six-figure-token prompt — cost has to be
  bounded by config rather than by whatever the web returned.
- Structured outputs, so there is no retry-on-bad-JSON loop and therefore no double
  billing.
- Token accounting per call, so spend is visible rather than inferred.

Citation indices are range-checked against the real source count: a model citing
source 7 of 6 is hallucinating, and passing it through renders a link to nothing.
"""

from __future__ import annotations

import re

import orjson

from app.common.metrics import llm_tokens
from app.config import Settings
from app.logging_setup import get_logger
from app.models import ResultItem
from app.rerank.base import LLMProvider, LLMResult

log = get_logger(__name__)


class LLMUnavailableError(RuntimeError):
    """The layer is disabled or misconfigured — a 503, not a 500."""


_SYSTEM = (
    "You answer questions using only the numbered sources provided. "
    "Every factual claim must be traceable to a source.\n\n"
    "Mark each claim inline with the source number in square brackets, like "
    "[1] or [2], and list every number you used in the `citations` field. "
    "These must agree: a claim marked [3] in the answer means 3 belongs in "
    "`citations`. An answer drawn from the sources always cites at least one "
    "of them — leave `citations` empty only when you could not answer from the "
    "sources at all.\n\n"
    "If the sources do not answer the question, say so plainly rather than "
    "filling the gap from memory."
)

#: Inline citation markers, e.g. "[3]" — the recovery path when the model writes
#: them in prose but leaves the structured field empty.
_INLINE_CITATION = re.compile(r"\[(\d{1,3})\]")

# No numeric constraints: structured outputs reject `minimum`/`maximum`, and the
# range check belongs in code anyway — see _valid_citations.
_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {
            "type": "string",
            "description": "The grounded answer, in markdown.",
        },
        "citations": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "1-based numbers of the sources actually used.",
        },
    },
    "required": ["answer", "citations"],
    "additionalProperties": False,
}


def build_prompt(query: str, sources: list[ResultItem], max_chars: int) -> str:
    """Render numbered sources plus the question.

    1-based numbering, which is what `_valid_citations` range-checks against.
    """
    blocks: list[str] = []
    for index, item in enumerate(sources, start=1):
        body = (item.markdown or item.snippet or "").strip()
        if len(body) > max_chars:
            body = body[:max_chars].rstrip() + "\n\n[truncated]"
        blocks.append(
            f"<source index=\"{index}\" url=\"{item.url}\" title=\"{item.title}\">\n"
            f"{body}\n</source>"
        )

    return (
        "\n\n".join(blocks)
        + f"\n\nQuestion: {query}\n\n"
        "Answer using only the sources above, citing the numbers you used."
    )


def _valid_citations(raw: object, source_count: int) -> list[int]:
    """Keep only citations pointing at a source that exists.

    Models occasionally emit an index past the end of the list; out-of-range and
    duplicate indices are dropped.
    """
    if not isinstance(raw, list):
        return []
    seen: set[int] = set()
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if 1 <= value <= source_count:
            seen.add(value)
    return sorted(seen)


def citations_from_answer(answer: str, source_count: int) -> list[int]:
    """Recover citations from inline [n] markers in the prose.

    The model sometimes marks the answer correctly — "Redis is used for caching[3]" —
    while leaving the structured field empty. The grounding is right there, so falling
    back to it beats shipping an apparently uncited answer.
    """
    if not answer:
        return []
    found = {int(m) for m in _INLINE_CITATION.findall(answer)}
    return sorted(n for n in found if 1 <= n <= source_count)


class AnthropicLLMProvider(LLMProvider):
    """Claude via the official Anthropic SDK."""

    name = "anthropic"
    billable = True

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._client = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.anthropic_api_key)

    def _ensure_client(self):
        if self._client is None:
            # Imported lazily so deployments with the LLM layer off never need
            # the SDK installed.
            from anthropic import AsyncAnthropic

            kwargs = {
                "api_key": self.settings.anthropic_api_key,
                "timeout": self.settings.llm_timeout_s,
                # The SDK already retries 429/5xx with backoff; a second layer on top
                # would multiply spend during a sustained outage.
                "max_retries": 2,
            }
            if self.settings.llm_base_url:
                # An Anthropic-compatible gateway: same wire protocol, different host.
                kwargs["base_url"] = self.settings.llm_base_url
                log.info("llm.using_gateway", base_url=self.settings.llm_base_url)
            self._client = AsyncAnthropic(**kwargs)
        return self._client

    async def synthesize(
        self,
        query: str,
        sources: list[ResultItem],
        *,
        instruction: str | None = None,
    ) -> LLMResult:
        if not self.enabled:
            raise LLMUnavailableError("ANTHROPIC_API_KEY is not set")

        import anthropic

        client = self._ensure_client()
        prompt = build_prompt(query, sources, self.settings.llm_max_source_chars)
        system = _SYSTEM if not instruction else f"{_SYSTEM}\n\nAdditional instruction: {instruction}"

        try:
            response = await client.messages.create(
                model=self.settings.llm_model,
                max_tokens=self.settings.llm_max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                # Parseable by construction, so no retry-on-bad-JSON and no double bill.
                output_config={"format": {"type": "json_schema", "schema": _ANSWER_SCHEMA}},
            )
        except anthropic.AuthenticationError as exc:
            raise LLMUnavailableError(f"anthropic auth failed: {exc}") from exc
        except anthropic.NotFoundError as exc:
            # Almost always a bad model id in config.
            raise LLMUnavailableError(f"unknown model {self.settings.llm_model!r}: {exc}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMUnavailableError(f"anthropic rate limited: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMUnavailableError(f"anthropic HTTP {exc.status_code}: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailableError(f"anthropic unreachable: {exc}") from exc

        usage = response.usage
        llm_tokens.labels(self.settings.llm_model, "input").inc(usage.input_tokens)
        llm_tokens.labels(self.settings.llm_model, "output").inc(usage.output_tokens)

        # Check stop_reason BEFORE reading content: a refusal returns HTTP 200 with an
        # empty content list, so indexing it blind would raise.
        if response.stop_reason == "refusal":
            raise LLMUnavailableError("model declined to answer this request")

        text = next((b.text for b in response.content if b.type == "text"), "")
        if not text:
            raise LLMUnavailableError(f"empty response (stop_reason={response.stop_reason})")

        try:
            payload = orjson.loads(text)
        except orjson.JSONDecodeError as exc:
            # Should be unreachable with a json_schema format, but a truncated response
            # (stop_reason="max_tokens") can still cut valid JSON short.
            raise LLMUnavailableError(
                f"unparseable answer (stop_reason={response.stop_reason}): {exc}"
            ) from exc

        answer = str(payload.get("answer") or "").strip()
        indices = _valid_citations(payload.get("citations"), len(sources))
        dropped = _count_dropped(payload.get("citations"), indices)
        if dropped:
            log.warning("llm.hallucinated_citations", dropped=dropped, sources=len(sources))

        if not indices and sources:
            # Structured field came back empty; check the prose before returning an
            # apparently ungrounded answer.
            indices = citations_from_answer(answer, len(sources))
            if indices:
                log.info("llm.citations_recovered_from_text", count=len(indices))
            else:
                log.warning("llm.answer_without_citations", sources=len(sources))

        return LLMResult(
            answer=answer,
            citations=[sources[i - 1].url for i in indices],
            model=response.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )

    async def count_tokens(self, query: str, sources: list[ResultItem]) -> int:
        """Input token count for the prompt this provider would send.

        Prices a request before paying for it. Do not substitute a third-party
        tokenizer — counts are model-specific.
        """
        if not self.enabled:
            raise LLMUnavailableError("ANTHROPIC_API_KEY is not set")

        client = self._ensure_client()
        prompt = build_prompt(query, sources, self.settings.llm_max_source_chars)
        result = await client.messages.count_tokens(
            model=self.settings.llm_model,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        return result.input_tokens

    async def health(self) -> bool:
        return self.enabled


class OllamaLLMProvider(LLMProvider):
    """Local model via Ollama — $0 marginal cost, paid for in local RAM.

    Implemented but never exercised in this deployment (LLM_PROVIDER=anthropic). Safe
    to delete if you will never use it.
    """

    name = "ollama"
    billable = False

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.ollama_url)

    async def synthesize(
        self,
        query: str,
        sources: list[ResultItem],
        *,
        instruction: str | None = None,
    ) -> LLMResult:
        import httpx

        from app.http_client import get_client

        prompt = build_prompt(query, sources, self.settings.llm_max_source_chars)
        system = _SYSTEM if not instruction else f"{_SYSTEM}\n\nAdditional instruction: {instruction}"

        try:
            response = await get_client().post(
                f"{self.settings.ollama_url.rstrip('/')}/api/chat",
                timeout=self.settings.llm_timeout_s,
                json={
                    "model": self.settings.ollama_model,
                    "stream": False,
                    # Weaker than a real schema, so the parse below must tolerate
                    # failure — unlike the Anthropic path.
                    "format": "json",
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(f"ollama unreachable: {exc!r}") from exc

        if response.status_code >= 400:
            raise LLMUnavailableError(f"ollama HTTP {response.status_code}")

        try:
            body = orjson.loads(response.content)
            content = body.get("message", {}).get("content", "")
            payload = orjson.loads(content)
        except (orjson.JSONDecodeError, AttributeError) as exc:
            raise LLMUnavailableError(f"ollama returned unparseable JSON: {exc}") from exc

        answer = str(payload.get("answer") or "").strip()
        indices = _valid_citations(payload.get("citations"), len(sources))
        if not indices and sources:
            indices = citations_from_answer(answer, len(sources))
        return LLMResult(
            answer=answer,
            citations=[sources[i - 1].url for i in indices],
            model=self.settings.ollama_model,
        )

    async def health(self) -> bool:
        import httpx

        from app.http_client import get_client

        try:
            response = await get_client().get(
                f"{self.settings.ollama_url.rstrip('/')}/api/tags", timeout=3.0
            )
        except httpx.HTTPError:
            return False
        return response.status_code == 200


def _count_dropped(raw: object, kept: list[int]) -> int:
    if not isinstance(raw, list):
        return 0
    return max(0, len({v for v in raw if isinstance(v, int) and not isinstance(v, bool)}) - len(kept))


def build_llm_provider(settings: Settings) -> LLMProvider | None:
    """The configured provider, or None when the layer is off."""
    if not settings.enable_llm_layer:
        return None
    provider = (
        AnthropicLLMProvider(settings)
        if settings.llm_provider == "anthropic"
        else OllamaLLMProvider(settings)
    )
    if not provider.enabled:
        log.warning("llm.provider_unconfigured", provider=settings.llm_provider)
        return None
    return provider
