"""ffprobe wrapper for sqlmpeg (RFC-001 "Probing policy").

`probe()` NEVER raises. Every failure mode -- a URL-scheme input, a missing
file, ffprobe absent from PATH, a nonzero ffprobe exit, a 5s timeout, or
unparseable JSON -- returns `None`, and callers fall back to symbolic
lowering (see plans/rfc-001-stream-aware.md, "Probing policy"). This module
depends only on `sqlmpeg.ir` (for `StreamType`) and the stdlib; it must never
import anything else from the package.

Results are memoized per `(realpath, mtime_ns, size)` so a compile that
probes the same input multiple times only shells out once; `clear_cache()`
resets the memo for tests.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass

from sqlmpeg.ir import StreamType

_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class StreamMeta:
    type: StreamType
    index: int  # per-type, 0-based (0:a:<index>)
    metadata: dict[str, str]  # language/title tags, when present
    width: int | None
    height: int | None
    fps: str | None  # e.g. "30000/1001", verbatim from ffprobe avg_frame_rate
    sample_rate: int | None


@dataclass(frozen=True)
class ProbeResult:
    streams: list[StreamMeta]  # file order

    def by_type(self, t: StreamType) -> list[StreamMeta]:
        return [s for s in self.streams if s.type == t]


_CacheKey = tuple[str, int, int]  # (realpath, mtime_ns, size)
_cache: dict[_CacheKey, ProbeResult] = {}


def clear_cache() -> None:
    """Clear the probe() memoization cache. For tests."""
    _cache.clear()


def probe(path: str) -> ProbeResult | None:
    """Probe a local media file with ffprobe.

    Returns None -- never raises -- when: `path` looks like a URL
    (contains "://"), the file does not exist, ffprobe is not on PATH,
    ffprobe exits nonzero or times out (5s), or its output is not the JSON
    shape we expect.
    """
    if "://" in path:
        return None

    try:
        if not os.path.isfile(path):
            return None
        real = os.path.realpath(path)
        st = os.stat(real)
    except OSError:
        return None

    cache_key: _CacheKey = (real, st.st_mtime_ns, st.st_size)
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None

    argv = [ffprobe, "-v", "error", "-print_format", "json", "-show_streams", real]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if result.returncode != 0:
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    parsed = _parse_streams(data)
    if parsed is None:
        return None

    _cache[cache_key] = parsed
    return parsed


def _parse_streams(data: object) -> ProbeResult | None:
    try:
        if not isinstance(data, dict):
            return None
        raw_streams = data["streams"]
        if not isinstance(raw_streams, list):
            return None

        streams: list[StreamMeta] = []
        video_idx = 0
        audio_idx = 0
        for raw in raw_streams:
            if not isinstance(raw, dict):
                return None
            codec_type = raw["codec_type"]

            if codec_type == "video":
                metadata = _tags(raw)
                streams.append(
                    StreamMeta(
                        type="video",
                        index=video_idx,
                        metadata=metadata,
                        width=int(raw["width"]) if "width" in raw else None,
                        height=int(raw["height"]) if "height" in raw else None,
                        fps=str(raw["avg_frame_rate"]) if "avg_frame_rate" in raw else None,
                        sample_rate=None,
                    )
                )
                video_idx += 1
            elif codec_type == "audio":
                metadata = _tags(raw)
                streams.append(
                    StreamMeta(
                        type="audio",
                        index=audio_idx,
                        metadata=metadata,
                        width=None,
                        height=None,
                        fps=None,
                        sample_rate=int(raw["sample_rate"]) if "sample_rate" in raw else None,
                    )
                )
                audio_idx += 1
            # other codec_type values (subtitle, data, ...) are ignored.

        return ProbeResult(streams=streams)
    except (KeyError, TypeError, ValueError):
        return None


def _tags(raw: dict[str, object]) -> dict[str, str]:
    tags = raw.get("tags", {})
    metadata: dict[str, str] = {}
    if isinstance(tags, dict):
        if "language" in tags:
            metadata["language"] = str(tags["language"])
        if "title" in tags:
            metadata["title"] = str(tags["title"])
    return metadata
