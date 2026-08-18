"""The ``sqlmpeg.<name>`` macro namespace (RFC-007 wave 3a, plan 052).

Three ergonomic macros that expand to a small ffmpeg filter subgraph no
single filter provides ("Three namespaces, one convention" in
plans/rfc-007-uniform-calls.md). Unlike a registry filter call, a macro's
signature is OURS: fixed positional parameters in the order documented
below, checked without ever consulting the installed ffmpeg's option
tables -- macros work OFFLINE, with no registry at all. Named arguments are
rejected outright (sqlmpeg/lower.py's ``_lower_macro_call``): there is no
option surface to name.

:data:`MACROS` is keyed by lowercased macro name; sqlmpeg/lower.py is the
only reader, resolving ``sqlmpeg.<name>(...)`` against this table and
nowhere else (not the registry). Each entry's ``expand`` builds the macro's
filter chain through the ``node`` callback it is handed -- one call per
filter, shaped exactly like :meth:`sqlmpeg.lower._NodeFactory.node`
(filter name, its args, its input refs, its output pad types -> the new
node's ref) -- so lower.py's own node-minting is the only place FrameRefs
are actually created. ``expand`` never validates: by the time it runs,
lower.py has already checked arity, argument kinds and the macro's own
stream-type rule.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from sqlmpeg.ir import StreamType

__all__ = [
    "INPUT_MACROS",
    "MACROS",
    "InputMacro",
    "Macro",
    "MacroParam",
    "NodeBuilder",
    "macro_names",
]

# One node-minting call: filter name, its args, its input refs, its output pad
# types -> the new node's ref (pad 0). Exactly `_NodeFactory.node`'s shape, so
# `self.ctx.node` is passed straight through with no adapter.
NodeBuilder = Callable[[str, dict[str, object], list[str], list[StreamType]], str]


@dataclass(frozen=True)
class MacroParam:
    """One position of a macro's OWN signature: a stream pad, or a number.

    `kind` is ``"stream"`` (its pad type is fixed per macro, `stream_type`)
    or ``"num"`` (a numeric literal, sign included).
    """

    name: str
    kind: str  # "stream" | "num"
    stream_type: StreamType | None = None


@dataclass(frozen=True)
class Macro:
    """One ``sqlmpeg.<name>`` macro: its signature, output type, expansion.

    `kind_hints` gives a stream-position argument of the WRONG kind a hint
    more specific than the generic stream-signature message, keyed by the
    kind that was actually passed. Only ``delay`` uses this: an audio
    argument is common enough (video is the macro's only supported type)
    that it earns a hint naming the bare filter that does what the caller
    almost certainly wants.
    """

    name: str
    params: tuple[MacroParam, ...]
    output: StreamType
    expand: Callable[[list[object], NodeBuilder], str]
    kind_hints: dict[str, str] = field(default_factory=dict)

    @property
    def signature(self) -> str:
        return f"sqlmpeg.{self.name}(" + ", ".join(p.name for p in self.params) + ")"

    @property
    def stream_positions(self) -> list[int]:
        return [index for index, param in enumerate(self.params) if param.kind == "stream"]


def _blur_regions(values: list[object], node: NodeBuilder) -> str:
    """crop -> gblur -> overlay, region named once; `f` feeds crop AND overlay."""
    f, x, y, w, h, sigma = values
    cropped = node("crop", {"out_w": w, "out_h": h, "x": x, "y": y}, [str(f)], ["video"])
    blurred = node("gblur", {"sigma": sigma}, [cropped], ["video"])
    return node("overlay", {"x": x, "y": y}, [str(f), blurred], ["video"])


def _speed(values: list[object], node: NodeBuilder) -> str:
    f, factor = values
    return node("setpts", {"expr": f"PTS/{factor}"}, [str(f)], ["video"])


def _delay(values: list[object], node: NodeBuilder) -> str:
    """The transparent-canvas macro: format to yuva420p, then pad the start."""
    f, seconds = values
    formatted = node("format", {"pix_fmts": "yuva420p"}, [str(f)], ["video"])
    return node(
        "tpad",
        {"start_duration": seconds, "stop": 1, "color": "black@0"},
        [formatted],
        ["video"],
    )


_ADELAY_HINT = (
    "sqlmpeg.delay() is the video (transparent-canvas) macro; delay an audio "
    "stream with the bare filter directly, in milliseconds, e.g. "
    "adelay(a.audio[1], delays => '2000')"
)

MACROS: dict[str, Macro] = {
    "blur_regions": Macro(
        name="blur_regions",
        params=(
            MacroParam("f", "stream", "video"),
            MacroParam("x", "num"),
            MacroParam("y", "num"),
            MacroParam("w", "num"),
            MacroParam("h", "num"),
            MacroParam("sigma", "num"),
        ),
        output="video",
        expand=_blur_regions,
    ),
    "speed": Macro(
        name="speed",
        params=(MacroParam("f", "stream", "video"), MacroParam("factor", "num")),
        output="video",
        expand=_speed,
    ),
    "delay": Macro(
        name="delay",
        params=(MacroParam("f", "stream", "video"), MacroParam("seconds", "num")),
        output="video",
        expand=_delay,
        kind_hints={"audio": _ADELAY_HINT},
    ),
}


# ---------------------------------------------------------------------------
# input-minting macros (RFC-009, plan 062)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InputMacro:
    """A macro that lowers to an extra ``-i``, not to a filter node.

    The one kind of stream a filtergraph cannot generate: ffmpeg has no
    "empty subtitle source" filter, because a filtergraph carries no subtitle
    pads at all (RFC-004's passthrough-only rule). So the stand-in for a
    missing caption track is minted as an INPUT instead -- `format` forces the
    demuxer, `path` is a self-contained ``data:`` URI, and the result is an
    ordinary passthrough subtitle stream that takes tags like any other.

    `format` flows to emit through an INTERNAL per-input option (see
    ``sqlmpeg.inputs.internal_option``); it is not part of the user-facing
    ``INPUT_OPTIONS`` table, because there is no ``input('x', format => ...)``
    surface -- this is the compiler minting its own input.
    """

    name: str
    output: StreamType
    path: str
    format: str


# "WEBVTT\n\n", base64'd: a valid WebVTT file with zero cues. Measured
# 2026-08-17 against ffmpeg 7.1 -- `-f webvtt -i "data:text/vtt;base64,
# V0VCVlRUCgo="` muxes a real, taggable subtitle stream carrying no cues, and
# ffmpeg's own `data:` protocol keeps the compiled command self-contained (no
# temp file is written, nothing is shipped alongside the command).
_EMPTY_VTT_URI = "data:text/vtt;base64,V0VCVlRUCgo="

INPUT_MACROS: dict[str, InputMacro] = {
    "empty_captions": InputMacro(
        name="empty_captions",
        output="subtitle",
        path=_EMPTY_VTT_URI,
        format="webvtt",
    ),
}


def macro_names() -> list[str]:
    """Every ``sqlmpeg.<name>`` there is, both kinds, sorted."""
    return sorted({*MACROS, *INPUT_MACROS})
