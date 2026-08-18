"""Input option table for sqlmpeg.

Guardrail #4: ``INPUT_OPTIONS`` is DATA, the single source of truth driving
``input('path', <name> => <value>, ...)`` validation, emit, docs and the LLM
prompt. Input-side mirror of ``sqlmpeg.sink.SINK_OPTIONS``, deliberately the
same shape.

No ``extra_args`` escape hatch: arbitrary flag passthrough would break
"reject, never approximate"; the table grows instead.

``InputOptionSpec`` has no ``scope``/``per_stream`` (unlike ``SinkOptionSpec``)
-- a demuxer-level flag applies once to the input's ``-i``, with no per-stream
axis. It adds one type over the sink table: ``"num"`` (int or float, never
bool) for ``framerate``/``itsoffset``, which are routinely fractional and, for
``itsoffset``, legally negative (ffmpeg shifts a stream earlier).
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Literal

from sqlmpeg.errors import ErrorCode, SqlmpegError

InputOptionType = Literal["str", "int", "bool", "num"]


@dataclass(frozen=True)
class InputOptionSpec:
    name: str
    type: InputOptionType
    doc: str  # one line; drives docs + prompt
    flag: str  # e.g. "-loop", "-framerate"


INPUT_OPTIONS: dict[str, InputOptionSpec] = {
    "loop": InputOptionSpec(
        name="loop",
        type="bool",
        doc="Loop a single-frame input (e.g. a still image) indefinitely.",
        flag="-loop",
    ),
    "stream_loop": InputOptionSpec(
        name="stream_loop",
        type="int",
        doc="Loop the whole input this many extra times (-1 loops forever).",
        flag="-stream_loop",
    ),
    "framerate": InputOptionSpec(
        name="framerate",
        type="num",
        doc="Force the input's frame rate, e.g. for a looped still image.",
        flag="-framerate",
    ),
    "itsoffset": InputOptionSpec(
        name="itsoffset",
        type="num",
        doc="Shift the input's timestamps by this many seconds (negative shifts earlier).",
        flag="-itsoffset",
    ),
    "hwaccel": InputOptionSpec(
        name="hwaccel",
        type="str",
        doc="Request a hardware decoder for this input, e.g. 'cuda'.",
        flag="-hwaccel",
    ),
}


# Flags the COMPILER sets on an input it minted itself; no user SQL can name
# them. Kept out of `INPUT_OPTIONS` so the user-facing surface (docs, prompt,
# validation) never learns of them: `validate_option` still rejects these names
# as unknown, only `option_spec` (which emit renders through) resolves them.
#
# `format` is what `sqlmpeg.empty_captions()` needs: a `data:` URI carries no
# extension, so the demuxer has to be named (`-f webvtt -i "data:..."`).
_INTERNAL_INPUT_OPTIONS: dict[str, InputOptionSpec] = {
    "format": InputOptionSpec(
        name="format",
        type="str",
        doc="Force the demuxer of a compiler-minted input (internal).",
        flag="-f",
    ),
}


def option_spec(name: str) -> InputOptionSpec | None:
    """The spec emit renders `name` with: user-facing table first, then internal."""
    return INPUT_OPTIONS.get(name) or _INTERNAL_INPUT_OPTIONS.get(name)


def _unknown_option_hint(name: str) -> str:
    matches = difflib.get_close_matches(name, sorted(INPUT_OPTIONS), n=1, cutoff=0.6)
    if matches:
        return f"did you mean {matches[0]!r}?"
    return "known options: " + ", ".join(sorted(INPUT_OPTIONS))


def validate_option(
    name: str,
    value: object,
    *,
    line: int | None = None,
    col: int | None = None,
) -> object:
    """Validate one ``input('path', name => value)`` pair against INPUT_OPTIONS.

    Returns the normalized value. Raises ``UNKNOWN_INPUT_OPTION`` for a name
    not in the table, ``INPUT_OPTION_TYPE`` for a value whose type doesn't
    match the spec. ``str``/``int``/``bool`` mirror
    ``sqlmpeg.sink.validate_option``; ``"num"`` accepts any int or float,
    never a bool, and negatives are legal (``itsoffset``).
    """
    spec = INPUT_OPTIONS.get(name)
    if spec is None:
        raise SqlmpegError(
            ErrorCode.UNKNOWN_INPUT_OPTION,
            f"unknown input option {name!r}",
            line=line,
            col=col,
            hint=_unknown_option_hint(name),
        )

    if spec.type == "bool":
        if isinstance(value, bool):
            return value
        raise SqlmpegError(
            ErrorCode.INPUT_OPTION_TYPE,
            f"option {name!r} expects a bool, got {value!r}",
            line=line,
            col=col,
            hint=f"{name} accepts true or false",
        )

    if spec.type == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SqlmpegError(
                ErrorCode.INPUT_OPTION_TYPE,
                f"option {name!r} expects an int, got {value!r}",
                line=line,
                col=col,
                hint=f"{name} takes a bare integer literal, e.g. {name} 2",
            )
        return value

    if spec.type == "num":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise SqlmpegError(
                ErrorCode.INPUT_OPTION_TYPE,
                f"option {name!r} expects a number, got {value!r}",
                line=line,
                col=col,
                hint=f"{name} takes a bare numeric literal, e.g. {name} 15",
            )
        return value

    # spec.type == "str"
    if isinstance(value, bool) or not isinstance(value, str):
        raise SqlmpegError(
            ErrorCode.INPUT_OPTION_TYPE,
            f"option {name!r} expects a str, got {value!r}",
            line=line,
            col=col,
            hint=f"{name} takes a single-quoted string literal, e.g. {name} 'cuda'",
        )
    return value
