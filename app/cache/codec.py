"""Cache payload encoding.

Redis memory is the recurring cost here and page markdown is what fills it. zstd
level 3 gets 3-5x on prose for well under a millisecond.

A one-byte frame header means the decoder never guesses whether a value is
compressed. Values below ~1KB skip compression — the CPU isn't worth the bytes.
"""

from __future__ import annotations

from typing import Any

import orjson
import zstandard

_RAW = b"\x00"
_ZSTD = b"\x01"


class CorruptCacheValue(ValueError):
    """A stored value could not be decoded. Treated as a miss, never fatal."""


class Codec:
    def __init__(self, *, min_compress_bytes: int = 1024, level: int = 3) -> None:
        self._min_compress_bytes = min_compress_bytes
        # Reusable and thread-safe for one-shot calls, so built once not per operation.
        self._compressor = zstandard.ZstdCompressor(level=level)
        self._decompressor = zstandard.ZstdDecompressor()

    def encode(self, value: Any) -> bytes:
        raw = orjson.dumps(value)
        if len(raw) < self._min_compress_bytes:
            return _RAW + raw
        return _ZSTD + self._compressor.compress(raw)

    def decode(self, blob: bytes | None) -> Any:
        if not blob:
            return None

        frame, payload = blob[:1], blob[1:]
        if frame == _ZSTD:
            try:
                payload = self._decompressor.decompress(payload)
            except zstandard.ZstdError as exc:
                raise CorruptCacheValue(f"zstd frame failed to decompress: {exc}") from exc
        elif frame != _RAW:
            # Almost certainly a value from another encoding version; bumping
            # CACHE_VERSION is the intended way to avoid this.
            raise CorruptCacheValue(f"unknown cache frame header: {frame!r}")

        try:
            return orjson.loads(payload)
        except orjson.JSONDecodeError as exc:
            raise CorruptCacheValue(f"payload is not valid JSON: {exc}") from exc
