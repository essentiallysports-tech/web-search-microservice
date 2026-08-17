"""LLM layer interface.

Nothing on the `/search` or `/extract` paths may import a concrete implementation: the
LLM is the only layer with a meaningful per-call cost, so it has to be impossible to
invoke by accident rather than merely disabled.
"""

from __future__ import annotations

import abc

from pydantic import BaseModel

from app.config import Settings
from app.models import ResultItem


class LLMResult(BaseModel):
    answer: str
    citations: list[str] = []
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


class LLMProvider(abc.ABC):
    name: str
    billable: bool = True

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return True

    @abc.abstractmethod
    async def synthesize(
        self,
        query: str,
        sources: list[ResultItem],
        *,
        instruction: str | None = None,
    ) -> LLMResult:
        """Produce a grounded answer over `sources` with citations."""

    @abc.abstractmethod
    async def health(self) -> bool: ...
