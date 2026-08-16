"""Sink option table for sqlmpeg (RFC-002, plan 025).

Guardrail #4: the option table is DATA, not code. ``SINK_OPTIONS`` is the
single source of truth that drives ``COPY ... TO 'path' WITH (...)``
validation (plan 026's lower), rendering into ffmpeg args (plan 027's emit),
docs, and the LLM system prompt. No option-specific logic should live
anywhere else -- every sink-visible option's behavior is expressed here as a
``SinkOptionSpec``.

v1 set is exactly RFC-002's table: ``video_codec``, ``audio_codec``, ``crf``,
``preset``, ``pix_fmt``, ``video_bitrate``, ``audio_bitrate``,
``sample_rate``, ``format``, ``faststart``. No ``extra_args`` escape hatch --
arbitrary flag passthrough would break "reject, never approximate"; the
table grows instead.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Literal

from sqlmpeg.errors import ErrorCode, SqlmpegError

OptionScope = Literal["video", "audio", "container"]
OptionType = Literal["str", "int", "bool"]


@dataclass(frozen=True)
class SinkOptionSpec:
    name: str
    scope: OptionScope
    type: OptionType
    doc: str  # one line; drives docs + prompt
    # rendering data for emit (no logic outside the table):
    flag: str  # e.g. "-c", "-crf", "-b", "-f", "-movflags"
    per_stream: bool  # True -> rendered as f"{flag}:{i}" per output
    value_template: str = "{v}"  # e.g. "+faststart" for the bool movflags case


SINK_OPTIONS: dict[str, SinkOptionSpec] = {
    "video_codec": SinkOptionSpec(
        name="video_codec",
        scope="video",
        type="str",
        doc="Video codec name, e.g. 'libx264'.",
        flag="-c",
        per_stream=True,
    ),
    "audio_codec": SinkOptionSpec(
        name="audio_codec",
        scope="audio",
        type="str",
        doc="Audio codec name, e.g. 'aac'.",
        flag="-c",
        per_stream=True,
    ),
    "crf": SinkOptionSpec(
        name="crf",
        scope="video",
        type="int",
        doc="Constant rate factor (encoder-dependent quality target).",
        flag="-crf",
        per_stream=True,
    ),
    "preset": SinkOptionSpec(
        name="preset",
        scope="video",
        type="str",
        doc="Encoder speed/quality preset, e.g. 'slow'.",
        flag="-preset",
        per_stream=True,
    ),
    "pix_fmt": SinkOptionSpec(
        name="pix_fmt",
        scope="video",
        type="str",
        doc="Pixel format, e.g. 'yuv420p'.",
        flag="-pix_fmt",
        per_stream=True,
    ),
    "video_bitrate": SinkOptionSpec(
        name="video_bitrate",
        scope="video",
        type="str",
        doc="Target video bitrate, e.g. '4M'.",
        flag="-b",
        per_stream=True,
    ),
    "audio_bitrate": SinkOptionSpec(
        name="audio_bitrate",
        scope="audio",
        type="str",
        doc="Target audio bitrate, e.g. '192k'.",
        flag="-b",
        per_stream=True,
    ),
    "sample_rate": SinkOptionSpec(
        name="sample_rate",
        scope="audio",
        type="int",
        doc="Output audio sample rate in Hz, e.g. 48000.",
        flag="-ar",
        per_stream=True,
    ),
    "format": SinkOptionSpec(
        name="format",
        scope="container",
        type="str",
        doc="Container format, e.g. 'mp4' (else inferred from the path extension).",
        flag="-f",
        per_stream=False,
    ),
    "faststart": SinkOptionSpec(
        name="faststart",
        scope="container",
        type="bool",
        doc="Move the MP4 moov atom to the front of the file for progressive playback.",
        flag="-movflags",
        per_stream=False,
        value_template="+faststart",
    ),
}


def _unknown_option_hint(name: str) -> str:
    matches = difflib.get_close_matches(name, sorted(SINK_OPTIONS), n=1, cutoff=0.6)
    if matches:
        return f"did you mean {matches[0]!r}?"
    return "known options: " + ", ".join(sorted(SINK_OPTIONS))


def validate_option(
    name: str,
    value: object,
    *,
    line: int | None = None,
    col: int | None = None,
) -> object:
    """Validate one COPY ... WITH (name value) pair against SINK_OPTIONS.

    Returns the normalized value on success. Raises ``SqlmpegError`` with
    ``UNKNOWN_SINK_OPTION`` if ``name`` isn't in the table, or
    ``SINK_OPTION_TYPE`` if ``value``'s type doesn't match the spec's
    declared type. Bools accept `true`/`false`; ints reject floats and
    strings (bool is a subclass of int in Python but never accepted where
    an int is declared, and never confused with the bool case since that is
    checked first).
    """
    spec = SINK_OPTIONS.get(name)
    if spec is None:
        raise SqlmpegError(
            ErrorCode.UNKNOWN_SINK_OPTION,
            f"unknown sink option {name!r}",
            line=line,
            col=col,
            hint=_unknown_option_hint(name),
        )

    if spec.type == "bool":
        if isinstance(value, bool):
            return value
        raise SqlmpegError(
            ErrorCode.SINK_OPTION_TYPE,
            f"option {name!r} expects a bool, got {value!r}",
            line=line,
            col=col,
            hint=f"{name} accepts true or false",
        )

    if spec.type == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SqlmpegError(
                ErrorCode.SINK_OPTION_TYPE,
                f"option {name!r} expects an int, got {value!r}",
                line=line,
                col=col,
                hint=f"{name} takes a bare integer literal, e.g. {name} 20",
            )
        return value

    # spec.type == "str"
    if isinstance(value, bool) or not isinstance(value, str):
        raise SqlmpegError(
            ErrorCode.SINK_OPTION_TYPE,
            f"option {name!r} expects a str, got {value!r}",
            line=line,
            col=col,
            hint=f"{name} takes a single-quoted string literal, e.g. {name} 'libx264'",
        )
    return value
