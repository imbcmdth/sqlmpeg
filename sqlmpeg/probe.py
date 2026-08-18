"""ffprobe wrapper for sqlmpeg.

`probe()` NEVER raises. Every failure mode -- a missing file, ffprobe absent
from PATH and its provisioner both, a nonzero ffprobe exit, a timeout, or
unparseable JSON -- returns `None`, and callers fall back to symbolic
lowering. This module depends only on `sqlmpeg.ir` (`StreamType`),
`sqlmpeg.binaries` (locating ffprobe) and the stdlib; it must never import
anything else from the package.

A local-path existence check runs BEFORE `binaries.ffprobe_path()` is even
consulted: a missing file is `None` with no subprocess AND no
provider lookup, which matters because the provider's first call may trigger
a ~95MB download -- paying that once per compile for an input that does not
even exist would be its own footgun.

Results are memoized per `(realpath, mtime_ns, size)` so a compile that
probes the same input multiple times only shells out once; `clear_cache()`
resets the memo for tests.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass

from sqlmpeg import binaries
from sqlmpeg.ir import StreamType

_TIMEOUT_SECONDS = 5.0
# Remote specs fetch a manifest and often an init segment per stream before
# ffprobe can report anything; 5s flakes on real networks, so they get more.
_REMOTE_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class StreamMeta:
    """Audio rows carry channels/channel_layout/sample_rate, video rows carry
    width/height/fps/color_transfer; codec/bitrate/duration
    are common to both. Every field is opportunistic -- absent or wrong-typed
    in ffprobe's JSON means None, never an exception (see
    `_int_opt`/`_float_opt`/`_str_opt`), and a defaulted field means exactly
    that: not supplied.
    """

    type: StreamType
    index: int  # per-type, 0-based (0:a:<index>)
    metadata: dict[str, str]  # language/title tags, when present
    width: int | None
    height: int | None
    fps: str | None  # e.g. "30000/1001", verbatim from ffprobe avg_frame_rate
    sample_rate: int | None
    codec: str | None = None  # ffprobe codec_name, verbatim
    channels: int | None = None  # audio only
    channel_layout: str | None = None  # audio only, e.g. "stereo"
    bitrate: int | None = None  # ffprobe bit_rate, as int
    duration: float | None = None  # per-stream duration in seconds
    color_transfer: str | None = None  # video only; the HDR discriminator


@dataclass(frozen=True)
class ProbeResult:
    streams: list[StreamMeta]  # file order
    duration: float | None = None  # container-level, from -show_format

    def by_type(self, t: StreamType) -> list[StreamMeta]:
        return [s for s in self.streams if s.type == t]


_CacheKey = tuple[str, int, int]  # (realpath, mtime_ns, size)
_cache: dict[_CacheKey, ProbeResult] = {}


def clear_cache() -> None:
    """Clear the probe() memoization cache. For tests."""
    _cache.clear()


def probe(path: str) -> ProbeResult | None:
    """Probe a media input with ffprobe.

    Returns None -- never raises -- when: the file does not exist, ffprobe
    is not on PATH or via its provisioner, ffprobe exits nonzero or times
    out, or its output is not the JSON shape we expect.

    A spec containing "://" is handed to ffprobe VERBATIM -- ffprobe is the
    authority on its own protocols, so a remote input probes over the network
    and an unsupported scheme fails into the same permissive None. Remote
    probes get a longer timeout and are memoized by the spec string alone:
    there is no mtime to key on, so the cache is per-process "what this URL
    said when we asked".
    """
    if "://" in path:
        cache_key: _CacheKey = (path, -1, -1)
        return _cached_ffprobe(path, cache_key, _REMOTE_TIMEOUT_SECONDS)

    try:
        if not os.path.isfile(path):
            return None
        real = os.path.realpath(path)
        st = os.stat(real)
    except OSError:
        return None

    return _cached_ffprobe(real, (real, st.st_mtime_ns, st.st_size), _TIMEOUT_SECONDS)


def _cached_ffprobe(
    spec: str, cache_key: _CacheKey, timeout: float
) -> ProbeResult | None:
    """One memoized ffprobe invocation over `spec` (a path or a URL)."""
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    ffprobe = binaries.ffprobe_path()
    if ffprobe is None:
        return None

    argv = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        spec,
    ]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
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
        subtitle_idx = 0
        data_idx = 0
        for raw in raw_streams:
            if not isinstance(raw, dict):
                return None
            codec_type = raw["codec_type"]

            codec = _str_opt(raw, "codec_name")
            bitrate = _int_opt(raw, "bit_rate")
            duration = _float_opt(raw, "duration")

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
                        codec=codec,
                        channels=None,
                        channel_layout=None,
                        bitrate=bitrate,
                        duration=duration,
                        color_transfer=_str_opt(raw, "color_transfer"),
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
                        codec=codec,
                        channels=_int_opt(raw, "channels"),
                        channel_layout=_str_opt(raw, "channel_layout"),
                        bitrate=bitrate,
                        duration=duration,
                        color_transfer=None,
                    )
                )
                audio_idx += 1
            elif codec_type == "subtitle":
                metadata = _tags(raw)
                streams.append(
                    StreamMeta(
                        type="subtitle",
                        index=subtitle_idx,
                        metadata=metadata,
                        width=None,
                        height=None,
                        fps=None,
                        sample_rate=None,
                        codec=codec,
                        channels=None,
                        channel_layout=None,
                        bitrate=bitrate,
                        duration=duration,
                        color_transfer=None,
                    )
                )
                subtitle_idx += 1
            elif codec_type == "data":
                metadata = _tags(raw)
                streams.append(
                    StreamMeta(
                        type="data",
                        index=data_idx,
                        metadata=metadata,
                        width=None,
                        height=None,
                        fps=None,
                        sample_rate=None,
                        codec=codec,
                        channels=None,
                        channel_layout=None,
                        bitrate=bitrate,
                        duration=duration,
                        color_transfer=None,
                    )
                )
                data_idx += 1
            # other codec_type values (attachment, ...) are ignored.

        container_duration = None
        raw_format = data.get("format")
        if isinstance(raw_format, dict):
            container_duration = _float_opt(raw_format, "duration")

        return ProbeResult(streams=streams, duration=container_duration)
    except (KeyError, TypeError, ValueError):
        return None


def _int_opt(raw: dict[str, object], key: str) -> int | None:
    """`raw[key]` as `int`, or None if absent, None-valued, or unparseable.

    Never raises: a per-field escape hatch so one malformed value nulls only
    that column instead of failing the whole probe (unlike the outer
    try/except in `_parse_streams`, which nulls the entire result).
    """
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_opt(raw: dict[str, object], key: str) -> float | None:
    """`raw[key]` as `float`, or None if absent, None-valued, or unparseable."""
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _str_opt(raw: dict[str, object], key: str) -> str | None:
    """`raw[key]` as `str`, or None if absent or None-valued."""
    if key not in raw or raw[key] is None:
        return None
    return str(raw[key])


def _tags(raw: dict[str, object]) -> dict[str, str]:
    tags = raw.get("tags", {})
    metadata: dict[str, str] = {}
    if isinstance(tags, dict):
        if "language" in tags:
            metadata["language"] = str(tags["language"])
        if "title" in tags:
            metadata["title"] = str(tags["title"])
    return metadata
