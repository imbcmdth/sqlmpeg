"""Sink option table for sqlmpeg.

Guardrail #4: the option table is DATA, not code. ``SINK_OPTIONS`` is the
single source of truth driving ``COPY ... TO 'path' WITH (...)`` validation,
emit, docs and the LLM prompt. No option-specific logic lives anywhere else --
every sink-visible option's behavior is a ``SinkOptionSpec`` field.

No ``extra_args`` escape hatch: arbitrary flag passthrough would break
"reject, never approximate"; the table grows instead.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Literal

from sqlmpeg.errors import ErrorCode, SqlmpegError

OptionScope = Literal["video", "audio", "subtitle", "container"]
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
    "subtitle_codec": SinkOptionSpec(
        name="subtitle_codec",
        scope="subtitle",
        type="str",
        doc="Subtitle codec name, e.g. 'mov_text', 'webvtt', 'srt'.",
        flag="-c",
        per_stream=True,
    ),
    "frames": SinkOptionSpec(
        name="frames",
        scope="video",
        type="int",
        doc="Stop the video output after N frames.",
        flag="-frames",
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


# CSV option table: a COPY ... WITH (FORMAT csv, ...) sink takes
# exactly these two. A media option in a csv COPY is rejected against THIS
# table, not SINK_OPTIONS; `header` in a media COPY is rejected against
# SINK_OPTIONS, which never held it.
CSV_OPTIONS: dict[str, SinkOptionSpec] = {
    "format": SinkOptionSpec(
        name="format",
        scope="container",
        type="str",
        doc="Must be 'csv' -- this is what makes a COPY a table sink.",
        flag="",
        per_stream=False,
    ),
    "header": SinkOptionSpec(
        name="header",
        scope="container",
        type="bool",
        doc="Emit a header row of column names (default false).",
        flag="",
        per_stream=False,
    ),
}


def _unknown_option_hint(name: str, table: dict[str, SinkOptionSpec] = SINK_OPTIONS) -> str:
    matches = difflib.get_close_matches(name, sorted(table), n=1, cutoff=0.6)
    if matches:
        return f"did you mean {matches[0]!r}?"
    return "known options: " + ", ".join(sorted(table))


def validate_option(
    name: str,
    value: object,
    *,
    line: int | None = None,
    col: int | None = None,
) -> object:
    """Validate one COPY ... WITH (name value) pair against SINK_OPTIONS.

    Returns the normalized value. Raises ``UNKNOWN_SINK_OPTION`` for a name
    not in the table, ``SINK_OPTION_TYPE`` for a value whose type doesn't
    match the spec. Bools accept `true`/`false`; ints reject floats, strings
    and bools (a Python bool is an int subclass, so the bool case is checked
    first and `isinstance(value, bool)` guards the int case).
    """
    return _validate_against(SINK_OPTIONS, name, value, line=line, col=col)


def validate_csv_option(
    name: str,
    value: object,
    *,
    line: int | None = None,
    col: int | None = None,
) -> object:
    """Validate one COPY ... WITH (name value) pair against CSV_OPTIONS.

    A separate table from ``SINK_OPTIONS``: a media option like
    ``video_codec`` is unknown here and gets its own typed rejection.
    """
    return _validate_against(CSV_OPTIONS, name, value, line=line, col=col)


def _validate_against(
    table: dict[str, SinkOptionSpec],
    name: str,
    value: object,
    *,
    line: int | None,
    col: int | None,
) -> object:
    spec = table.get(name)
    if spec is None:
        raise SqlmpegError(
            ErrorCode.UNKNOWN_SINK_OPTION,
            f"unknown sink option {name!r}",
            line=line,
            col=col,
            hint=_unknown_option_hint(name, table),
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
