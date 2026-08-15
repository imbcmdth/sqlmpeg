"""Stdlib function table for sqlmpeg.

Guardrail #4: the function table is DATA, not code. ``FUNCTIONS`` is the
single source of truth that drives lowering, ``--help``, docs, and the LLM
system prompt. No stdlib-specific lowering logic should live anywhere else;
every SQL-visible function's behavior is expressed here as a ``FuncSpec``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from sqlmpeg.ir import FrameRef, StreamType

ParamKind = Literal["video", "audio", "num", "str"]


@dataclass(frozen=True)
class Param:
    name: str
    kind: ParamKind


class ExpandCtx(Protocol):
    def node(
        self,
        filter: str,
        args: dict[str, object],
        inputs: list[FrameRef],
        outputs: list[StreamType],
    ) -> FrameRef:
        """Create a Node with a fresh id, register it, return its id."""
        ...


@dataclass(frozen=True)
class FuncSpec:
    name: str
    variants: tuple[tuple[Param, ...], ...]  # overloads; arity+kinds checked by lower
    doc: str  # one line, drives --help/docs/LLM prompt
    expand: Callable[[ExpandCtx, list[object]], FrameRef]
    # expand args: FrameRef for video/audio params, python int/float/str for
    # literals, in SQL argument order. Returns the FrameRef of the subgraph
    # output.
    returns: StreamType


# --------------------------------------------------------------------------
# Param shorthand
# --------------------------------------------------------------------------


def _video(name: str) -> Param:
    return Param(name, "video")


def _audio(name: str) -> Param:
    return Param(name, "audio")


def _num(name: str) -> Param:
    return Param(name, "num")


def _str(name: str) -> Param:
    return Param(name, "str")


def _as_stream(value: object) -> FrameRef:
    assert isinstance(value, str)
    return value


# --------------------------------------------------------------------------
# expand implementations - video
# --------------------------------------------------------------------------


def _expand_scale(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_stream(args[0])
    if len(args) == 2:
        factor = args[1]
        return ctx.node("scale", {"w": f"iw*{factor}", "h": "-2"}, [f], ["video"])
    w, h = args[1], args[2]
    return ctx.node("scale", {"w": w, "h": h}, [f], ["video"])


def _expand_crop(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_stream(args[0])
    x, y, w, h = args[1], args[2], args[3], args[4]
    return ctx.node("crop", {"w": w, "h": h, "x": x, "y": y}, [f], ["video"])


def _expand_overlay(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    base = _as_stream(args[0])
    top = _as_stream(args[1])
    x, y = args[2], args[3]
    return ctx.node("overlay", {"x": x, "y": y}, [base, top], ["video"])


def _expand_hflip(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_stream(args[0])
    return ctx.node("hflip", {}, [f], ["video"])


def _expand_vflip(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_stream(args[0])
    return ctx.node("vflip", {}, [f], ["video"])


def _expand_blur(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_stream(args[0])
    sigma = args[1]
    return ctx.node("gblur", {"sigma": sigma}, [f], ["video"])


def _expand_blur_regions(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_stream(args[0])
    x, y, w, h, sigma = args[1], args[2], args[3], args[4], args[5]
    cropped = ctx.node("crop", {"w": w, "h": h, "x": x, "y": y}, [f], ["video"])
    blurred = ctx.node("gblur", {"sigma": sigma}, [cropped], ["video"])
    return ctx.node("overlay", {"x": x, "y": y}, [f, blurred], ["video"])


def _expand_draw_box(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_stream(args[0])
    x, y, w, h, color = args[1], args[2], args[3], args[4], args[5]
    return ctx.node("drawbox", {"x": x, "y": y, "w": w, "h": h, "color": color}, [f], ["video"])


def _expand_text(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_stream(args[0])
    s, x, y, size = args[1], args[2], args[3], args[4]
    return ctx.node("drawtext", {"text": s, "x": x, "y": y, "fontsize": size}, [f], ["video"])


def _expand_speed(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_stream(args[0])
    factor = args[1]
    return ctx.node("setpts", {"expr": f"PTS/{factor}"}, [f], ["video"])


def _expand_fade_in(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_stream(args[0])
    dur = args[1]
    return ctx.node("fade", {"type": "in", "st": 0, "d": dur}, [f], ["video"])


def _expand_fade_out(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_stream(args[0])
    dur = args[1]
    if len(args) == 3:
        return ctx.node("fade", {"type": "out", "st": args[2], "d": dur}, [f], ["video"])
    return ctx.node("fade", {"type": "out", "d": dur}, [f], ["video"])


# --------------------------------------------------------------------------
# expand implementations - audio
# --------------------------------------------------------------------------


def _expand_volume(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    a = _as_stream(args[0])
    factor = args[1]
    return ctx.node("volume", {"volume": factor}, [a], ["audio"])


def _expand_amix(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    a = _as_stream(args[0])
    b = _as_stream(args[1])
    return ctx.node("amix", {"inputs": 2}, [a, b], ["audio"])


def _expand_atempo(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    a = _as_stream(args[0])
    factor = args[1]
    return ctx.node("atempo", {"tempo": factor}, [a], ["audio"])


def _expand_afade_in(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    a = _as_stream(args[0])
    dur = args[1]
    return ctx.node("afade", {"t": "in", "st": 0, "d": dur}, [a], ["audio"])


def _expand_afade_out(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    a = _as_stream(args[0])
    dur = args[1]
    if len(args) == 3:
        return ctx.node("afade", {"t": "out", "st": args[2], "d": dur}, [a], ["audio"])
    return ctx.node("afade", {"t": "out", "d": dur}, [a], ["audio"])


def _expand_reverb(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    a = _as_stream(args[0])
    decay = args[1]
    return ctx.node(
        "aecho",
        {"in_gain": 0.8, "out_gain": 0.9, "delays": 60, "decays": decay},
        [a],
        ["audio"],
    )


# --------------------------------------------------------------------------
# THE function table (guardrail #4: single source of truth)
# --------------------------------------------------------------------------

FUNCTIONS: dict[str, FuncSpec] = {
    "scale": FuncSpec(
        name="scale",
        variants=(
            (_video("f"), _num("factor")),
            (_video("f"), _num("w"), _num("h")),
        ),
        doc="Resize a frame by a scale factor, or to explicit width/height.",
        expand=_expand_scale,
        returns="video",
    ),
    "crop": FuncSpec(
        name="crop",
        variants=((_video("f"), _num("x"), _num("y"), _num("w"), _num("h")),),
        doc="Crop a frame to a w x h rectangle at (x, y).",
        expand=_expand_crop,
        returns="video",
    ),
    "overlay": FuncSpec(
        name="overlay",
        variants=((_video("base"), _video("top"), _num("x"), _num("y")),),
        doc="Composite top over base at position (x, y).",
        expand=_expand_overlay,
        returns="video",
    ),
    "hflip": FuncSpec(
        name="hflip",
        variants=((_video("f"),),),
        doc="Flip a frame horizontally.",
        expand=_expand_hflip,
        returns="video",
    ),
    "vflip": FuncSpec(
        name="vflip",
        variants=((_video("f"),),),
        doc="Flip a frame vertically.",
        expand=_expand_vflip,
        returns="video",
    ),
    "blur": FuncSpec(
        name="blur",
        variants=((_video("f"), _num("sigma")),),
        doc="Apply a Gaussian blur with the given sigma.",
        expand=_expand_blur,
        returns="video",
    ),
    "blur_regions": FuncSpec(
        name="blur_regions",
        variants=(
            (
                _video("f"),
                _num("x"),
                _num("y"),
                _num("w"),
                _num("h"),
                _num("sigma"),
            ),
        ),
        doc="Blur a w x h rectangle at (x, y) and composite it back over the frame.",
        expand=_expand_blur_regions,
        returns="video",
    ),
    "draw_box": FuncSpec(
        name="draw_box",
        variants=(
            (
                _video("f"),
                _num("x"),
                _num("y"),
                _num("w"),
                _num("h"),
                _str("color"),
            ),
        ),
        doc="Draw an outlined box at (x, y) sized w x h in the given color.",
        expand=_expand_draw_box,
        returns="video",
    ),
    "text": FuncSpec(
        name="text",
        variants=((_video("f"), _str("s"), _num("x"), _num("y"), _num("size")),),
        doc="Draw text s at (x, y) with the given font size.",
        expand=_expand_text,
        returns="video",
    ),
    "speed": FuncSpec(
        name="speed",
        variants=((_video("f"), _num("factor")),),
        doc="Change playback speed by factor (video-only in v0).",
        expand=_expand_speed,
        returns="video",
    ),
    "fade_in": FuncSpec(
        name="fade_in",
        variants=((_video("f"), _num("dur")),),
        doc="Fade in from black over dur seconds starting at t=0.",
        expand=_expand_fade_in,
        returns="video",
    ),
    "fade_out": FuncSpec(
        name="fade_out",
        variants=(
            (_video("f"), _num("dur")),
            (_video("f"), _num("dur"), _num("at")),
        ),
        doc=(
            "Fade out to black over dur seconds starting at `at` seconds "
            "(without `at` the fade starts at t=0 and every later frame is black; "
            "pass at = clip length - dur to fade at the end)."
        ),
        expand=_expand_fade_out,
        returns="video",
    ),
    "volume": FuncSpec(
        name="volume",
        variants=((_audio("a"), _num("factor")),),
        doc="Scale audio volume by a linear factor.",
        expand=_expand_volume,
        returns="audio",
    ),
    "amix": FuncSpec(
        name="amix",
        variants=((_audio("a"), _audio("b")),),
        doc="Mix two audio streams together (equal weight, ffmpeg amix defaults).",
        expand=_expand_amix,
        returns="audio",
    ),
    "atempo": FuncSpec(
        name="atempo",
        variants=((_audio("a"), _num("factor")),),
        doc="Change audio playback tempo by factor (pitch-preserving, audio-only).",
        expand=_expand_atempo,
        returns="audio",
    ),
    "afade_in": FuncSpec(
        name="afade_in",
        variants=((_audio("a"), _num("dur")),),
        doc="Fade audio in from silence over dur seconds starting at t=0.",
        expand=_expand_afade_in,
        returns="audio",
    ),
    "afade_out": FuncSpec(
        name="afade_out",
        variants=(
            (_audio("a"), _num("dur")),
            (_audio("a"), _num("dur"), _num("at")),
        ),
        doc=(
            "Fade audio out to silence over dur seconds starting at `at` seconds "
            "(without `at` the fade starts at t=0 and every later sample is silent; "
            "pass at = clip length - dur to fade at the end)."
        ),
        expand=_expand_afade_out,
        returns="audio",
    ),
    "reverb": FuncSpec(
        name="reverb",
        variants=((_audio("a"), _num("decay")),),
        doc=(
            "Approximate reverb via a single-tap echo (aecho); not a true "
            "convolution reverb, but a cheap, dependency-free stand-in."
        ),
        expand=_expand_reverb,
        returns="audio",
    ),
}


def signatures(name: str) -> str:
    """Human-readable signature list for error messages, e.g.

    ``"overlay(video, video, num, num)"``. Multiple overloads are joined
    with " | ".
    """
    spec = FUNCTIONS[name]
    return " | ".join(
        f"{name}(" + ", ".join(p.kind for p in variant) + ")" for variant in spec.variants
    )
