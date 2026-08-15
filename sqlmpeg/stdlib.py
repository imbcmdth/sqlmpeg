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

from sqlmpeg.ir import FrameRef

ParamKind = Literal["frame", "num", "str"]


@dataclass(frozen=True)
class Param:
    name: str
    kind: ParamKind


class ExpandCtx(Protocol):
    def node(
        self, filter: str, args: dict[str, object], inputs: list[FrameRef]
    ) -> FrameRef:
        """Create a Node with a fresh id, register it, return its id."""
        ...


@dataclass(frozen=True)
class FuncSpec:
    name: str
    variants: tuple[tuple[Param, ...], ...]  # overloads; arity+kinds checked by lower
    doc: str  # one line, drives --help/docs/LLM prompt
    expand: Callable[[ExpandCtx, list[object]], FrameRef]
    # expand args: FrameRef for frame params, python int/float/str for literals,
    # in SQL argument order. Returns the FrameRef of the subgraph output.


# --------------------------------------------------------------------------
# Param shorthand
# --------------------------------------------------------------------------


def _frame(name: str) -> Param:
    return Param(name, "frame")


def _num(name: str) -> Param:
    return Param(name, "num")


def _str(name: str) -> Param:
    return Param(name, "str")


def _as_frame(value: object) -> FrameRef:
    assert isinstance(value, str)
    return value


# --------------------------------------------------------------------------
# expand implementations
# --------------------------------------------------------------------------


def _expand_scale(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_frame(args[0])
    if len(args) == 2:
        factor = args[1]
        return ctx.node("scale", {"w": f"iw*{factor}", "h": "-2"}, [f])
    w, h = args[1], args[2]
    return ctx.node("scale", {"w": w, "h": h}, [f])


def _expand_crop(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_frame(args[0])
    x, y, w, h = args[1], args[2], args[3], args[4]
    return ctx.node("crop", {"w": w, "h": h, "x": x, "y": y}, [f])


def _expand_overlay(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    base = _as_frame(args[0])
    top = _as_frame(args[1])
    x, y = args[2], args[3]
    return ctx.node("overlay", {"x": x, "y": y}, [base, top])


def _expand_hflip(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_frame(args[0])
    return ctx.node("hflip", {}, [f])


def _expand_vflip(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_frame(args[0])
    return ctx.node("vflip", {}, [f])


def _expand_blur(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_frame(args[0])
    sigma = args[1]
    return ctx.node("gblur", {"sigma": sigma}, [f])


def _expand_blur_regions(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_frame(args[0])
    x, y, w, h, sigma = args[1], args[2], args[3], args[4], args[5]
    cropped = ctx.node("crop", {"w": w, "h": h, "x": x, "y": y}, [f])
    blurred = ctx.node("gblur", {"sigma": sigma}, [cropped])
    return ctx.node("overlay", {"x": x, "y": y}, [f, blurred])


def _expand_draw_box(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_frame(args[0])
    x, y, w, h, color = args[1], args[2], args[3], args[4], args[5]
    return ctx.node("drawbox", {"x": x, "y": y, "w": w, "h": h, "color": color}, [f])


def _expand_text(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_frame(args[0])
    s, x, y, size = args[1], args[2], args[3], args[4]
    return ctx.node("drawtext", {"text": s, "x": x, "y": y, "fontsize": size}, [f])


def _expand_speed(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_frame(args[0])
    factor = args[1]
    return ctx.node("setpts", {"expr": f"PTS/{factor}"}, [f])


def _expand_fade_in(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_frame(args[0])
    dur = args[1]
    return ctx.node("fade", {"type": "in", "st": 0, "d": dur}, [f])


def _expand_fade_out(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_frame(args[0])
    dur = args[1]
    if len(args) == 3:
        return ctx.node("fade", {"type": "out", "st": args[2], "d": dur}, [f])
    return ctx.node("fade", {"type": "out", "d": dur}, [f])


# --------------------------------------------------------------------------
# THE function table (guardrail #4: single source of truth)
# --------------------------------------------------------------------------

FUNCTIONS: dict[str, FuncSpec] = {
    "scale": FuncSpec(
        name="scale",
        variants=(
            (_frame("f"), _num("factor")),
            (_frame("f"), _num("w"), _num("h")),
        ),
        doc="Resize a frame by a scale factor, or to explicit width/height.",
        expand=_expand_scale,
    ),
    "crop": FuncSpec(
        name="crop",
        variants=((_frame("f"), _num("x"), _num("y"), _num("w"), _num("h")),),
        doc="Crop a frame to a w x h rectangle at (x, y).",
        expand=_expand_crop,
    ),
    "overlay": FuncSpec(
        name="overlay",
        variants=(
            (_frame("base"), _frame("top"), _num("x"), _num("y")),
        ),
        doc="Composite top over base at position (x, y).",
        expand=_expand_overlay,
    ),
    "hflip": FuncSpec(
        name="hflip",
        variants=((_frame("f"),),),
        doc="Flip a frame horizontally.",
        expand=_expand_hflip,
    ),
    "vflip": FuncSpec(
        name="vflip",
        variants=((_frame("f"),),),
        doc="Flip a frame vertically.",
        expand=_expand_vflip,
    ),
    "blur": FuncSpec(
        name="blur",
        variants=((_frame("f"), _num("sigma")),),
        doc="Apply a Gaussian blur with the given sigma.",
        expand=_expand_blur,
    ),
    "blur_regions": FuncSpec(
        name="blur_regions",
        variants=(
            (
                _frame("f"),
                _num("x"),
                _num("y"),
                _num("w"),
                _num("h"),
                _num("sigma"),
            ),
        ),
        doc="Blur a w x h rectangle at (x, y) and composite it back over the frame.",
        expand=_expand_blur_regions,
    ),
    "draw_box": FuncSpec(
        name="draw_box",
        variants=(
            (
                _frame("f"),
                _num("x"),
                _num("y"),
                _num("w"),
                _num("h"),
                _str("color"),
            ),
        ),
        doc="Draw an outlined box at (x, y) sized w x h in the given color.",
        expand=_expand_draw_box,
    ),
    "text": FuncSpec(
        name="text",
        variants=(
            (_frame("f"), _str("s"), _num("x"), _num("y"), _num("size")),
        ),
        doc="Draw text s at (x, y) with the given font size.",
        expand=_expand_text,
    ),
    "speed": FuncSpec(
        name="speed",
        variants=((_frame("f"), _num("factor")),),
        doc="Change playback speed by factor (video-only in v0).",
        expand=_expand_speed,
    ),
    "fade_in": FuncSpec(
        name="fade_in",
        variants=((_frame("f"), _num("dur")),),
        doc="Fade in from black over dur seconds starting at t=0.",
        expand=_expand_fade_in,
    ),
    "fade_out": FuncSpec(
        name="fade_out",
        variants=(
            (_frame("f"), _num("dur")),
            (_frame("f"), _num("dur"), _num("at")),
        ),
        doc=(
            "Fade out to black over dur seconds starting at `at` seconds "
            "(without `at` the fade starts at t=0 and every later frame is black; "
            "pass at = clip length - dur to fade at the end)."
        ),
        expand=_expand_fade_out,
    ),
}


def signatures(name: str) -> str:
    """Human-readable signature list for error messages, e.g.

    ``"overlay(frame, frame, num, num)"``. Multiple overloads are joined
    with " | ".
    """
    spec = FUNCTIONS[name]
    return " | ".join(
        f"{name}(" + ", ".join(p.kind for p in variant) + ")"
        for variant in spec.variants
    )
