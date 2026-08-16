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


Expand = Callable[[ExpandCtx, list[object]], FrameRef]


@dataclass(frozen=True)
class VariantImpl:
    """How ONE overload of a spec lowers, when the overloads differ in kind.

    Most functions have overloads that differ only in arity (``scale``,
    ``pad``): one ``expand`` reads ``len(args)`` and one output type and one
    ``named_target`` cover them all, so their :class:`FuncSpec` needs none of
    this. ``delay`` is the exception (plan 038) — its audio overload is one
    ``adelay`` node and its video overload is a two-filter macro returning
    ``video`` — and an ``expand`` cannot tell them apart, because both arities
    are 2 and the stream argument is a plain ``FrameRef`` in either case. So
    the MATCHED VARIANT INDEX, which lower knows and nothing else does, selects
    the implementation: see :attr:`FuncSpec.by_variant`.
    """

    expand: Expand
    returns: StreamType
    # Same meaning as FuncSpec.named_target, per overload: None = this overload
    # is a macro over several filters and rejects named arguments.
    named_target: str | None = None


@dataclass(frozen=True)
class FuncSpec:
    name: str
    variants: tuple[tuple[Param, ...], ...]  # overloads; arity+kinds checked by lower
    doc: str  # one line, drives --help/docs/LLM prompt
    expand: Expand
    # expand args: FrameRef for video/audio params, python int/float/str for
    # literals, in SQL argument order. Returns the FrameRef of the subgraph
    # output.
    returns: StreamType
    # The ffmpeg filter whose introspected options validate trailing named
    # arguments (RFC-003 "Tier-1 named extras"); None = named args rejected
    # (macros).
    named_target: str | None = None
    # Per-overload override of the three fields above, ONE ENTRY PER VARIANT in
    # `variants` order (plan 038). None -- every entry but `delay` -- means all
    # overloads share the spec-level expand/returns/named_target, which is what
    # `impl()` then hands back.
    by_variant: tuple[VariantImpl, ...] | None = None

    def impl(self, index: int) -> VariantImpl:
        """How overload `index` (its position in ``variants``) lowers."""
        if self.by_variant is None:
            return VariantImpl(self.expand, self.returns, self.named_target)
        return self.by_variant[index]


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
# expand implementations - video (plan 029)
# --------------------------------------------------------------------------


def _expand_rotate(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_stream(args[0])
    degrees = args[1]
    return ctx.node("rotate", {"a": f"{degrees}*PI/180"}, [f], ["video"])


def _expand_pad(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_stream(args[0])
    w, h = args[1], args[2]
    if len(args) == 3:
        pad_args: dict[str, object] = {"w": w, "h": h, "x": "(ow-iw)/2", "y": "(oh-ih)/2"}
    elif len(args) == 4:
        color = args[3]
        pad_args = {"w": w, "h": h, "x": "(ow-iw)/2", "y": "(oh-ih)/2", "color": color}
    elif len(args) == 5:
        x, y = args[3], args[4]
        pad_args = {"w": w, "h": h, "x": x, "y": y}
    else:
        x, y, color = args[3], args[4], args[5]
        pad_args = {"w": w, "h": h, "x": x, "y": y, "color": color}
    return ctx.node("pad", pad_args, [f], ["video"])


def _expand_hstack(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    a = _as_stream(args[0])
    b = _as_stream(args[1])
    return ctx.node("hstack", {"inputs": 2}, [a, b], ["video"])


def _expand_vstack(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    a = _as_stream(args[0])
    b = _as_stream(args[1])
    return ctx.node("vstack", {"inputs": 2}, [a, b], ["video"])


def _expand_fps(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_stream(args[0])
    rate = args[1]
    return ctx.node("fps", {"fps": rate}, [f], ["video"])


def _expand_sharpen(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_stream(args[0])
    amount = args[1]
    return ctx.node(
        "unsharp",
        {"luma_msize_x": 5, "luma_msize_y": 5, "luma_amount": amount},
        [f],
        ["video"],
    )


def _expand_deinterlace(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_stream(args[0])
    return ctx.node("yadif", {}, [f], ["video"])


def _expand_denoise(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    # Only luma_spatial is set; hqdn3d derives chroma_spatial/luma_tmp/chroma_tmp
    # from it when they are left at their defaults (ffmpeg docs: "hqdn3d").
    f = _as_stream(args[0])
    strength = args[1]
    return ctx.node("hqdn3d", {"luma_spatial": strength}, [f], ["video"])


def _expand_brightness(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_stream(args[0])
    v = args[1]
    return ctx.node("eq", {"brightness": v}, [f], ["video"])


def _expand_contrast(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_stream(args[0])
    v = args[1]
    return ctx.node("eq", {"contrast": v}, [f], ["video"])


def _expand_saturate(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_stream(args[0])
    v = args[1]
    return ctx.node("eq", {"saturation": v}, [f], ["video"])


def _expand_grayscale(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_stream(args[0])
    return ctx.node("hue", {"s": 0}, [f], ["video"])


def _expand_crossfade(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    a = _as_stream(args[0])
    b = _as_stream(args[1])
    dur, offset = args[2], args[3]
    # The 4-arg form sets no transition (ffmpeg's own default is "fade"), so a
    # trailing `transition => '...'` named extra stays mergeable (RFC-003).
    node_args: dict[str, object] = {"duration": dur, "offset": offset}
    if len(args) == 5:
        node_args = {"transition": args[4], **node_args}
    return ctx.node("xfade", node_args, [a, b], ["video"])


def _expand_subtitles(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_stream(args[0])
    path = args[1]
    return ctx.node("subtitles", {"filename": path}, [f], ["video"])


def _expand_reverse(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    f = _as_stream(args[0])
    return ctx.node("reverse", {}, [f], ["video"])


# --------------------------------------------------------------------------
# expand implementations - audio (plan 029)
# --------------------------------------------------------------------------


def _expand_normalize(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    a = _as_stream(args[0])
    if len(args) == 2:
        lufs = args[1]
        return ctx.node("loudnorm", {"I": lufs}, [a], ["audio"])
    return ctx.node("loudnorm", {}, [a], ["audio"])


def _expand_highpass(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    a = _as_stream(args[0])
    freq = args[1]
    return ctx.node("highpass", {"f": freq}, [a], ["audio"])


def _expand_lowpass(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    a = _as_stream(args[0])
    freq = args[1]
    return ctx.node("lowpass", {"f": freq}, [a], ["audio"])


def _expand_delay(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    a = _as_stream(args[0])
    seconds = args[1]
    assert isinstance(seconds, (int, float))
    ms = round(seconds * 1000)
    return ctx.node("adelay", {"delays": ms, "all": 1}, [a], ["audio"])


def _expand_delay_video(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    """`delay(f: video, seconds)` -- a transparent-padded canvas (plan 038).

    A MACRO over two filters, so it has no ``named_target``:

    * ``format=pix_fmts=yuva420p`` gives the stream an alpha plane, without
      which a transparent pad colour is simply black;
    * ``tpad=start_duration=<seconds>:stop=1:color=black@0`` puts `seconds` of
      fully transparent frames in FRONT of it, and exactly ONE fully
      transparent frame BEHIND it.

    That single trailing frame is the whole trick, and it is what makes the
    result composable with ``overlay`` (VERIFIED against ffmpeg 7.1,
    2026-08-15):

    * ``overlay`` keeps redisplaying its top input's LAST frame after that
      input ends (``repeatlast``, on by default). Without the pad, the last
      frame is the delayed clip's final picture, which would then freeze on
      screen for the whole rest of the base stream. With it, the frame that
      gets held forever is transparent, so the clip simply disappears.
    * ``tpad``'s "pad for ever" spelling, ``stop=-1``, would say the same thing
      more directly, but a never-ending top input makes ``overlay`` never
      terminate: measured, the compile runs until it is killed. One frame plus
      ``repeatlast`` is the same picture with an end.

    So the composite ends when the LONGER of (base, delay + clip) ends, and the
    delayed video is transparent before its start and after its clip.
    """
    f = _as_stream(args[0])
    seconds = args[1]
    alpha = ctx.node("format", {"pix_fmts": "yuva420p"}, [f], ["video"])
    return ctx.node(
        "tpad",
        {"start_duration": seconds, "stop": 1, "color": "black@0"},
        [alpha],
        ["video"],
    )


def _expand_acrossfade(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    a = _as_stream(args[0])
    b = _as_stream(args[1])
    dur = args[2]
    return ctx.node("acrossfade", {"d": dur}, [a, b], ["audio"])


def _expand_areverse(ctx: ExpandCtx, args: list[object]) -> FrameRef:
    a = _as_stream(args[0])
    return ctx.node("areverse", {}, [a], ["audio"])


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
        named_target="scale",
    ),
    "crop": FuncSpec(
        name="crop",
        variants=((_video("f"), _num("x"), _num("y"), _num("w"), _num("h")),),
        doc="Crop a frame to a w x h rectangle at (x, y).",
        expand=_expand_crop,
        returns="video",
        named_target="crop",
    ),
    "overlay": FuncSpec(
        name="overlay",
        variants=((_video("base"), _video("top"), _num("x"), _num("y")),),
        doc="Composite top over base at position (x, y).",
        expand=_expand_overlay,
        returns="video",
        named_target="overlay",
    ),
    "hflip": FuncSpec(
        name="hflip",
        variants=((_video("f"),),),
        doc="Flip a frame horizontally.",
        expand=_expand_hflip,
        returns="video",
        named_target="hflip",
    ),
    "vflip": FuncSpec(
        name="vflip",
        variants=((_video("f"),),),
        doc="Flip a frame vertically.",
        expand=_expand_vflip,
        returns="video",
        named_target="vflip",
    ),
    "blur": FuncSpec(
        name="blur",
        variants=((_video("f"), _num("sigma")),),
        doc="Apply a Gaussian blur with the given sigma.",
        expand=_expand_blur,
        returns="video",
        named_target="gblur",
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
        named_target=None,
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
        named_target="drawbox",
    ),
    "text": FuncSpec(
        name="text",
        variants=((_video("f"), _str("s"), _num("x"), _num("y"), _num("size")),),
        doc="Draw text s at (x, y) with the given font size.",
        expand=_expand_text,
        returns="video",
        named_target="drawtext",
    ),
    "speed": FuncSpec(
        name="speed",
        variants=((_video("f"), _num("factor")),),
        doc="Change a video stream's playback speed by factor (use atempo for audio).",
        expand=_expand_speed,
        returns="video",
        named_target="setpts",
    ),
    "fade_in": FuncSpec(
        name="fade_in",
        variants=((_video("f"), _num("dur")),),
        doc="Fade in from black over dur seconds starting at t=0.",
        expand=_expand_fade_in,
        returns="video",
        named_target="fade",
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
        named_target="fade",
    ),
    "volume": FuncSpec(
        name="volume",
        variants=((_audio("a"), _num("factor")),),
        doc="Scale audio volume by a linear factor.",
        expand=_expand_volume,
        returns="audio",
        named_target="volume",
    ),
    "amix": FuncSpec(
        name="amix",
        variants=((_audio("a"), _audio("b")),),
        doc="Mix two audio streams together (equal weight, ffmpeg amix defaults).",
        expand=_expand_amix,
        returns="audio",
        named_target="amix",
    ),
    "atempo": FuncSpec(
        name="atempo",
        variants=((_audio("a"), _num("factor")),),
        doc="Change audio playback tempo by factor (pitch-preserving, audio-only).",
        expand=_expand_atempo,
        returns="audio",
        named_target="atempo",
    ),
    "afade_in": FuncSpec(
        name="afade_in",
        variants=((_audio("a"), _num("dur")),),
        doc="Fade audio in from silence over dur seconds starting at t=0.",
        expand=_expand_afade_in,
        returns="audio",
        named_target="afade",
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
        named_target="afade",
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
        named_target="aecho",
    ),
    # ----------------------------------------------------------------------
    # plan 029: tier-1 promotions
    # ----------------------------------------------------------------------
    "rotate": FuncSpec(
        name="rotate",
        variants=((_video("f"), _num("degrees")),),
        doc="Rotate a frame degrees clockwise (ffmpeg evaluates the angle expression).",
        expand=_expand_rotate,
        returns="video",
        named_target="rotate",
    ),
    "pad": FuncSpec(
        name="pad",
        variants=(
            (_video("f"), _num("w"), _num("h")),
            (_video("f"), _num("w"), _num("h"), _str("color")),
            (_video("f"), _num("w"), _num("h"), _num("x"), _num("y")),
            (_video("f"), _num("w"), _num("h"), _num("x"), _num("y"), _str("color")),
        ),
        doc=(
            "Pad a frame to w x h; with just (w, h) the original is centered on a "
            "black background, (x, y) place it explicitly, and a trailing color "
            "string sets the background."
        ),
        expand=_expand_pad,
        returns="video",
        named_target="pad",
    ),
    "hstack": FuncSpec(
        name="hstack",
        variants=((_video("a"), _video("b")),),
        doc="Stack two frames side by side horizontally (inputs should share height).",
        expand=_expand_hstack,
        returns="video",
        named_target="hstack",
    ),
    "vstack": FuncSpec(
        name="vstack",
        variants=((_video("a"), _video("b")),),
        doc="Stack two frames vertically (inputs should share width).",
        expand=_expand_vstack,
        returns="video",
        named_target="vstack",
    ),
    "fps": FuncSpec(
        name="fps",
        variants=((_video("f"), _num("rate")),),
        doc="Force a constant output frame rate, duplicating or dropping frames as needed.",
        expand=_expand_fps,
        returns="video",
        named_target="fps",
    ),
    "sharpen": FuncSpec(
        name="sharpen",
        variants=((_video("f"), _num("amount")),),
        doc="Sharpen a frame by the given luma amount (ffmpeg unsharp, 5x5 matrix).",
        expand=_expand_sharpen,
        returns="video",
        named_target="unsharp",
    ),
    "deinterlace": FuncSpec(
        name="deinterlace",
        variants=((_video("f"),),),
        doc="Deinterlace a frame (ffmpeg yadif, default mode/parity).",
        expand=_expand_deinterlace,
        returns="video",
        named_target="yadif",
    ),
    "denoise": FuncSpec(
        name="denoise",
        variants=((_video("f"), _num("strength")),),
        doc="Denoise a frame by the given luma spatial strength (ffmpeg hqdn3d).",
        expand=_expand_denoise,
        returns="video",
        named_target="hqdn3d",
    ),
    "brightness": FuncSpec(
        name="brightness",
        variants=((_video("f"), _num("v")),),
        doc="Adjust brightness by v (-1..1, 0 = unchanged; ffmpeg eq).",
        expand=_expand_brightness,
        returns="video",
        named_target="eq",
    ),
    "contrast": FuncSpec(
        name="contrast",
        variants=((_video("f"), _num("v")),),
        doc="Adjust contrast by v (0..2, 1 = unchanged; ffmpeg eq).",
        expand=_expand_contrast,
        returns="video",
        named_target="eq",
    ),
    "saturate": FuncSpec(
        name="saturate",
        variants=((_video("f"), _num("v")),),
        doc="Adjust saturation by v (0..3, 1 = unchanged; ffmpeg eq).",
        expand=_expand_saturate,
        returns="video",
        named_target="eq",
    ),
    "grayscale": FuncSpec(
        name="grayscale",
        variants=((_video("f"),),),
        doc="Desaturate a frame to grayscale (ffmpeg hue, s=0).",
        expand=_expand_grayscale,
        returns="video",
        named_target="hue",
    ),
    "crossfade": FuncSpec(
        name="crossfade",
        variants=(
            (_video("a"), _video("b"), _num("dur"), _num("offset")),
            (_video("a"), _video("b"), _num("dur"), _num("offset"), _str("transition")),
        ),
        doc=(
            "Cross fade from a to b over dur seconds, starting offset seconds into a "
            "(default transition 'fade'); inputs must share resolution and fps."
        ),
        expand=_expand_crossfade,
        returns="video",
        named_target="xfade",
    ),
    "subtitles": FuncSpec(
        name="subtitles",
        variants=((_video("f"), _str("path")),),
        doc="Burn subtitles from path into a frame at run time (the file must exist then).",
        expand=_expand_subtitles,
        returns="video",
        named_target="subtitles",
    ),
    "reverse": FuncSpec(
        name="reverse",
        variants=((_video("f"),),),
        doc="Reverse a video stream (buffers the entire stream in memory).",
        expand=_expand_reverse,
        returns="video",
        named_target="reverse",
    ),
    "normalize": FuncSpec(
        name="normalize",
        variants=(
            (_audio("a"),),
            (_audio("a"), _num("lufs")),
        ),
        doc="Normalize loudness to EBU R128 (default -24 LUFS, or the given target).",
        expand=_expand_normalize,
        returns="audio",
        named_target="loudnorm",
    ),
    "highpass": FuncSpec(
        name="highpass",
        variants=((_audio("a"), _num("freq")),),
        doc="Attenuate frequencies below freq Hz (ffmpeg highpass).",
        expand=_expand_highpass,
        returns="audio",
        named_target="highpass",
    ),
    "lowpass": FuncSpec(
        name="lowpass",
        variants=((_audio("a"), _num("freq")),),
        doc="Attenuate frequencies above freq Hz (ffmpeg lowpass).",
        expand=_expand_lowpass,
        returns="audio",
        named_target="lowpass",
    ),
    "delay": FuncSpec(
        name="delay",
        variants=(
            (_audio("a"), _num("seconds")),
            (_video("f"), _num("seconds")),
        ),
        doc=(
            "Delay a stream by seconds: audio shifts by that many milliseconds "
            "(adelay), video becomes a transparent canvas that is empty before "
            "the clip starts and after it ends (format + tpad), so it composites "
            "straight into overlay."
        ),
        expand=_expand_delay,
        returns="audio",
        named_target="adelay",
        # The two overloads differ in more than arity: audio is one adelay node,
        # video is a two-filter macro that returns video (plan 038).
        by_variant=(
            VariantImpl(_expand_delay, "audio", "adelay"),
            VariantImpl(_expand_delay_video, "video", None),
        ),
    ),
    "acrossfade": FuncSpec(
        name="acrossfade",
        variants=((_audio("a"), _audio("b"), _num("dur")),),
        doc="Cross fade from a to b over dur seconds of audio.",
        expand=_expand_acrossfade,
        returns="audio",
        named_target="acrossfade",
    ),
    "areverse": FuncSpec(
        name="areverse",
        variants=((_audio("a"),),),
        doc="Reverse an audio stream (buffers the entire stream in memory).",
        expand=_expand_areverse,
        returns="audio",
        named_target="areverse",
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
