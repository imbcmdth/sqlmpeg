from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from sqlmpeg.ir import FrameRef, StreamType
from sqlmpeg.stdlib import FUNCTIONS, ExpandCtx, signatures

EXPECTED_NAMES = {
    "scale",
    "crop",
    "overlay",
    "hflip",
    "vflip",
    "blur",
    "blur_regions",
    "draw_box",
    "text",
    "speed",
    "fade_in",
    "fade_out",
    "volume",
    "amix",
    "atempo",
    "afade_in",
    "afade_out",
    "reverb",
    # plan 029
    "rotate",
    "pad",
    "hstack",
    "vstack",
    "fps",
    "sharpen",
    "deinterlace",
    "denoise",
    "brightness",
    "contrast",
    "saturate",
    "grayscale",
    "crossfade",
    "subtitles",
    "reverse",
    "normalize",
    "highpass",
    "lowpass",
    "delay",
    "acrossfade",
    "areverse",
}


@dataclass
class FakeCtx:
    """Records every node() call so tests can assert on shape and args."""

    nodes: list[tuple[str, str, dict[str, object], list[FrameRef], list[StreamType]]] = field(
        default_factory=list
    )
    _counter: int = 0

    def node(
        self,
        filter: str,
        args: dict[str, object],
        inputs: list[FrameRef],
        outputs: list[StreamType],
    ) -> FrameRef:
        node_id = f"n{self._counter}"
        self._counter += 1
        self.nodes.append((node_id, filter, dict(args), list(inputs), list(outputs)))
        return node_id


def _check_ctx_protocol(ctx: FakeCtx) -> ExpandCtx:
    # Structural typing sanity check: FakeCtx must satisfy ExpandCtx.
    return ctx


def test_all_names_present() -> None:
    # Plan 003's table has video rows (the two `scale` arities share a row, as
    # do `hflip`/`vflip`), plan 015 adds the audio rows, and plan 029 promotes
    # 21 more (15 video, 6 audio) from the dynamic long tail; every entry is
    # independently callable.
    assert set(FUNCTIONS.keys()) == EXPECTED_NAMES
    assert len(FUNCTIONS) == len(EXPECTED_NAMES) == 39


def test_names_match_spec_name_field() -> None:
    for key, spec in FUNCTIONS.items():
        assert key == spec.name


def test_returns_video_or_audio() -> None:
    video_names = {
        "scale",
        "crop",
        "overlay",
        "hflip",
        "vflip",
        "blur",
        "blur_regions",
        "draw_box",
        "text",
        "speed",
        "fade_in",
        "fade_out",
        "rotate",
        "pad",
        "hstack",
        "vstack",
        "fps",
        "sharpen",
        "deinterlace",
        "denoise",
        "brightness",
        "contrast",
        "saturate",
        "grayscale",
        "crossfade",
        "subtitles",
        "reverse",
    }
    audio_names = {
        "volume",
        "amix",
        "atempo",
        "afade_in",
        "afade_out",
        "reverb",
        "normalize",
        "highpass",
        "lowpass",
        "delay",
        "acrossfade",
        "areverse",
    }
    for name in video_names:
        assert FUNCTIONS[name].returns == "video"
    for name in audio_names:
        assert FUNCTIONS[name].returns == "audio"


@pytest.mark.parametrize(
    ("name", "arities"),
    [
        ("scale", {2, 3}),
        ("crop", {5}),
        ("overlay", {4}),
        ("hflip", {1}),
        ("vflip", {1}),
        ("blur", {2}),
        ("blur_regions", {6}),
        ("draw_box", {6}),
        ("text", {5}),
        ("speed", {2}),
        ("fade_in", {2}),
        ("fade_out", {2, 3}),
        ("volume", {2}),
        ("amix", {2}),
        ("atempo", {2}),
        ("afade_in", {2}),
        ("afade_out", {2, 3}),
        ("reverb", {2}),
        ("rotate", {2}),
        ("pad", {3, 4, 5, 6}),
        ("hstack", {2}),
        ("vstack", {2}),
        ("fps", {2}),
        ("sharpen", {2}),
        ("deinterlace", {1}),
        ("denoise", {2}),
        ("brightness", {2}),
        ("contrast", {2}),
        ("saturate", {2}),
        ("grayscale", {1}),
        ("crossfade", {4, 5}),
        ("subtitles", {2}),
        ("reverse", {1}),
        ("normalize", {1, 2}),
        ("highpass", {2}),
        ("lowpass", {2}),
        ("delay", {2}),
        ("acrossfade", {3}),
        ("areverse", {1}),
    ],
)
def test_arities(name: str, arities: set[int]) -> None:
    spec = FUNCTIONS[name]
    assert {len(variant) for variant in spec.variants} == arities


def test_doc_is_one_line() -> None:
    for spec in FUNCTIONS.values():
        assert spec.doc
        assert "\n" not in spec.doc


def test_scale_factor_variant() -> None:
    ctx = FakeCtx()
    out = FUNCTIONS["scale"].expand(ctx, ["src:a", 2])
    assert out == "n0"
    assert len(ctx.nodes) == 1
    node_id, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "scale"
    assert args == {"w": "iw*2", "h": "-2"}
    assert inputs == ["src:a"]
    assert outputs == ["video"]


def test_scale_wh_variant() -> None:
    ctx = FakeCtx()
    out = FUNCTIONS["scale"].expand(ctx, ["src:a", 640, 480])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "scale"
    assert args == {"w": 640, "h": 480}
    assert inputs == ["src:a"]
    assert outputs == ["video"]
    assert out == ctx.nodes[0][0]


def test_crop_arg_order_remap() -> None:
    ctx = FakeCtx()
    out = FUNCTIONS["crop"].expand(ctx, ["src:a", 10, 20, 100, 200])
    assert len(ctx.nodes) == 1
    node_id, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "crop"
    # SQL order is (x, y, w, h); ffmpeg crop wants w, h, x, y.
    assert args == {"w": 100, "h": 200, "x": 10, "y": 20}
    assert inputs == ["src:a"]
    assert outputs == ["video"]
    assert out == node_id


def test_overlay_inputs_order() -> None:
    ctx = FakeCtx()
    out = FUNCTIONS["overlay"].expand(ctx, ["src:base", "src:top", 5, 6])
    assert len(ctx.nodes) == 1
    node_id, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "overlay"
    assert args == {"x": 5, "y": 6}
    assert inputs == ["src:base", "src:top"]
    assert outputs == ["video"]
    assert out == node_id


def test_hflip_vflip_bare() -> None:
    ctx = FakeCtx()
    out_h = FUNCTIONS["hflip"].expand(ctx, ["src:a"])
    out_v = FUNCTIONS["vflip"].expand(ctx, ["src:a"])
    assert len(ctx.nodes) == 2
    assert ctx.nodes[0][1:] == ("hflip", {}, ["src:a"], ["video"])
    assert ctx.nodes[1][1:] == ("vflip", {}, ["src:a"], ["video"])
    assert out_h == ctx.nodes[0][0]
    assert out_v == ctx.nodes[1][0]


def test_blur() -> None:
    ctx = FakeCtx()
    FUNCTIONS["blur"].expand(ctx, ["src:a", 3.5])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "gblur"
    assert args == {"sigma": 3.5}
    assert inputs == ["src:a"]
    assert outputs == ["video"]


def test_blur_regions_macro_three_nodes_base_referenced_twice() -> None:
    ctx = FakeCtx()
    out = FUNCTIONS["blur_regions"].expand(ctx, ["src:a", 1, 2, 3, 4, 5])
    assert len(ctx.nodes) == 3

    crop_id, crop_filt, crop_args, crop_inputs, crop_outputs = ctx.nodes[0]
    assert crop_filt == "crop"
    assert crop_args == {"w": 3, "h": 4, "x": 1, "y": 2}
    assert crop_inputs == ["src:a"]  # base referenced once here...
    assert crop_outputs == ["video"]

    blur_id, blur_filt, blur_args, blur_inputs, blur_outputs = ctx.nodes[1]
    assert blur_filt == "gblur"
    assert blur_args == {"sigma": 5}
    assert blur_inputs == [crop_id]
    assert blur_outputs == ["video"]

    overlay_id, overlay_filt, overlay_args, overlay_inputs, overlay_outputs = ctx.nodes[2]
    assert overlay_filt == "overlay"
    assert overlay_args == {"x": 1, "y": 2}
    assert overlay_inputs == ["src:a", blur_id]  # ...and again here: twice total
    assert overlay_outputs == ["video"]

    assert out == overlay_id
    base_refs = sum(1 for n in ctx.nodes for inp in n[3] if inp == "src:a")
    assert base_refs == 2


def test_draw_box() -> None:
    ctx = FakeCtx()
    FUNCTIONS["draw_box"].expand(ctx, ["src:a", 1, 2, 3, 4, "red"])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "drawbox"
    assert args == {"x": 1, "y": 2, "w": 3, "h": 4, "color": "red"}
    assert inputs == ["src:a"]
    assert outputs == ["video"]


def test_text() -> None:
    ctx = FakeCtx()
    FUNCTIONS["text"].expand(ctx, ["src:a", "hello", 10, 20, 24])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "drawtext"
    assert args == {"text": "hello", "x": 10, "y": 20, "fontsize": 24}
    assert inputs == ["src:a"]
    assert outputs == ["video"]


def test_speed() -> None:
    ctx = FakeCtx()
    FUNCTIONS["speed"].expand(ctx, ["src:a", 2])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "setpts"
    assert args == {"expr": "PTS/2"}
    assert inputs == ["src:a"]
    assert outputs == ["video"]


def test_fade_in() -> None:
    ctx = FakeCtx()
    FUNCTIONS["fade_in"].expand(ctx, ["src:a", 1.5])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "fade"
    assert args == {"type": "in", "st": 0, "d": 1.5}
    assert inputs == ["src:a"]
    assert outputs == ["video"]


def test_fade_out() -> None:
    ctx = FakeCtx()
    FUNCTIONS["fade_out"].expand(ctx, ["src:a", 2.0])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "fade"
    assert args == {"type": "out", "d": 2.0}
    assert inputs == ["src:a"]
    assert outputs == ["video"]


def test_fade_out_with_start() -> None:
    ctx = FakeCtx()
    FUNCTIONS["fade_out"].expand(ctx, ["src:a", 1.5, 8.5])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "fade"
    assert args == {"type": "out", "st": 8.5, "d": 1.5}
    assert inputs == ["src:a"]
    assert outputs == ["video"]


# --------------------------------------------------------------------------
# audio functions
# --------------------------------------------------------------------------


def test_volume() -> None:
    ctx = FakeCtx()
    FUNCTIONS["volume"].expand(ctx, ["src:a", 0.5])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "volume"
    assert args == {"volume": 0.5}
    assert inputs == ["src:a"]
    assert outputs == ["audio"]


def test_amix() -> None:
    ctx = FakeCtx()
    out = FUNCTIONS["amix"].expand(ctx, ["src:a", "src:b"])
    assert len(ctx.nodes) == 1
    node_id, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "amix"
    assert args == {"inputs": 2}
    assert inputs == ["src:a", "src:b"]
    assert outputs == ["audio"]
    assert out == node_id


def test_atempo() -> None:
    ctx = FakeCtx()
    FUNCTIONS["atempo"].expand(ctx, ["src:a", 1.5])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "atempo"
    assert args == {"tempo": 1.5}
    assert inputs == ["src:a"]
    assert outputs == ["audio"]


def test_afade_in() -> None:
    ctx = FakeCtx()
    FUNCTIONS["afade_in"].expand(ctx, ["src:a", 1.5])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "afade"
    assert args == {"t": "in", "st": 0, "d": 1.5}
    assert inputs == ["src:a"]
    assert outputs == ["audio"]


def test_afade_out() -> None:
    ctx = FakeCtx()
    FUNCTIONS["afade_out"].expand(ctx, ["src:a", 2.0])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "afade"
    assert args == {"t": "out", "d": 2.0}
    assert inputs == ["src:a"]
    assert outputs == ["audio"]


def test_afade_out_with_start() -> None:
    ctx = FakeCtx()
    FUNCTIONS["afade_out"].expand(ctx, ["src:a", 1.5, 8.5])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "afade"
    assert args == {"t": "out", "st": 8.5, "d": 1.5}
    assert inputs == ["src:a"]
    assert outputs == ["audio"]


def test_reverb() -> None:
    ctx = FakeCtx()
    FUNCTIONS["reverb"].expand(ctx, ["src:a", 0.4])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "aecho"
    assert args == {
        "in_gain": 0.8,
        "out_gain": 0.9,
        "delays": 60,
        "decays": 0.4,
    }
    assert inputs == ["src:a"]
    assert outputs == ["audio"]


# --------------------------------------------------------------------------
# plan 029: tier-1 promotions
# --------------------------------------------------------------------------


def test_rotate_degrees_expression() -> None:
    ctx = FakeCtx()
    FUNCTIONS["rotate"].expand(ctx, ["src:a", 90])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "rotate"
    assert args == {"a": "90*PI/180"}
    assert inputs == ["src:a"]
    assert outputs == ["video"]


def test_pad_centered_variant() -> None:
    ctx = FakeCtx()
    FUNCTIONS["pad"].expand(ctx, ["src:a", 640, 480])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "pad"
    assert args == {"w": 640, "h": 480, "x": "(ow-iw)/2", "y": "(oh-ih)/2"}
    assert inputs == ["src:a"]
    assert outputs == ["video"]


def test_pad_centered_with_color_variant() -> None:
    ctx = FakeCtx()
    FUNCTIONS["pad"].expand(ctx, ["src:a", 640, 480, "blue"])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "pad"
    assert args == {
        "w": 640,
        "h": 480,
        "x": "(ow-iw)/2",
        "y": "(oh-ih)/2",
        "color": "blue",
    }


def test_pad_explicit_xy_variant() -> None:
    ctx = FakeCtx()
    FUNCTIONS["pad"].expand(ctx, ["src:a", 640, 480, 10, 20])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "pad"
    assert args == {"w": 640, "h": 480, "x": 10, "y": 20}


def test_pad_explicit_xy_with_color_variant() -> None:
    ctx = FakeCtx()
    FUNCTIONS["pad"].expand(ctx, ["src:a", 640, 480, 10, 20, "red"])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "pad"
    assert args == {"w": 640, "h": 480, "x": 10, "y": 20, "color": "red"}


def test_hstack() -> None:
    ctx = FakeCtx()
    out = FUNCTIONS["hstack"].expand(ctx, ["src:a", "src:b"])
    assert len(ctx.nodes) == 1
    node_id, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "hstack"
    assert args == {"inputs": 2}
    assert inputs == ["src:a", "src:b"]
    assert outputs == ["video"]
    assert out == node_id


def test_vstack() -> None:
    ctx = FakeCtx()
    out = FUNCTIONS["vstack"].expand(ctx, ["src:a", "src:b"])
    assert len(ctx.nodes) == 1
    node_id, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "vstack"
    assert args == {"inputs": 2}
    assert inputs == ["src:a", "src:b"]
    assert outputs == ["video"]
    assert out == node_id


def test_fps() -> None:
    ctx = FakeCtx()
    FUNCTIONS["fps"].expand(ctx, ["src:a", 30])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "fps"
    assert args == {"fps": 30}
    assert inputs == ["src:a"]
    assert outputs == ["video"]


def test_sharpen() -> None:
    ctx = FakeCtx()
    FUNCTIONS["sharpen"].expand(ctx, ["src:a", 1.5])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "unsharp"
    assert args == {"luma_msize_x": 5, "luma_msize_y": 5, "luma_amount": 1.5}
    assert inputs == ["src:a"]
    assert outputs == ["video"]


def test_deinterlace() -> None:
    ctx = FakeCtx()
    FUNCTIONS["deinterlace"].expand(ctx, ["src:a"])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "yadif"
    assert args == {}
    assert inputs == ["src:a"]
    assert outputs == ["video"]


def test_denoise() -> None:
    ctx = FakeCtx()
    FUNCTIONS["denoise"].expand(ctx, ["src:a", 4])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "hqdn3d"
    assert args == {"luma_spatial": 4}
    assert inputs == ["src:a"]
    assert outputs == ["video"]


def test_brightness() -> None:
    ctx = FakeCtx()
    FUNCTIONS["brightness"].expand(ctx, ["src:a", 0.2])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "eq"
    assert args == {"brightness": 0.2}


def test_contrast() -> None:
    ctx = FakeCtx()
    FUNCTIONS["contrast"].expand(ctx, ["src:a", 1.2])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "eq"
    assert args == {"contrast": 1.2}


def test_saturate() -> None:
    ctx = FakeCtx()
    FUNCTIONS["saturate"].expand(ctx, ["src:a", 1.5])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "eq"
    assert args == {"saturation": 1.5}


def test_grayscale() -> None:
    ctx = FakeCtx()
    FUNCTIONS["grayscale"].expand(ctx, ["src:a"])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "hue"
    assert args == {"s": 0}
    assert inputs == ["src:a"]
    assert outputs == ["video"]


def test_crossfade_default_transition() -> None:
    ctx = FakeCtx()
    out = FUNCTIONS["crossfade"].expand(ctx, ["src:a", "src:b", 1.0, 5.0])
    assert len(ctx.nodes) == 1
    node_id, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "xfade"
    assert args == {"transition": "fade", "duration": 1.0, "offset": 5.0}
    assert inputs == ["src:a", "src:b"]
    assert outputs == ["video"]
    assert out == node_id


def test_crossfade_explicit_transition() -> None:
    ctx = FakeCtx()
    FUNCTIONS["crossfade"].expand(ctx, ["src:a", "src:b", 1.0, 5.0, "wipeleft"])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "xfade"
    assert args == {"transition": "wipeleft", "duration": 1.0, "offset": 5.0}


def test_subtitles() -> None:
    ctx = FakeCtx()
    FUNCTIONS["subtitles"].expand(ctx, ["src:a", "subs.srt"])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "subtitles"
    assert args == {"filename": "subs.srt"}
    assert inputs == ["src:a"]
    assert outputs == ["video"]


def test_reverse() -> None:
    ctx = FakeCtx()
    FUNCTIONS["reverse"].expand(ctx, ["src:a"])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "reverse"
    assert args == {}
    assert inputs == ["src:a"]
    assert outputs == ["video"]


def test_normalize_default() -> None:
    ctx = FakeCtx()
    FUNCTIONS["normalize"].expand(ctx, ["src:a"])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "loudnorm"
    assert args == {}
    assert inputs == ["src:a"]
    assert outputs == ["audio"]


def test_normalize_with_target() -> None:
    ctx = FakeCtx()
    FUNCTIONS["normalize"].expand(ctx, ["src:a", -16.0])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "loudnorm"
    assert args == {"I": -16.0}


def test_highpass() -> None:
    ctx = FakeCtx()
    FUNCTIONS["highpass"].expand(ctx, ["src:a", 200])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "highpass"
    assert args == {"f": 200}
    assert inputs == ["src:a"]
    assert outputs == ["audio"]


def test_lowpass() -> None:
    ctx = FakeCtx()
    FUNCTIONS["lowpass"].expand(ctx, ["src:a", 3000])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "lowpass"
    assert args == {"f": 3000}
    assert inputs == ["src:a"]
    assert outputs == ["audio"]


def test_delay_converts_seconds_to_milliseconds() -> None:
    ctx = FakeCtx()
    FUNCTIONS["delay"].expand(ctx, ["src:a", 0.25])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "adelay"
    assert args == {"delays": 250, "all": 1}
    assert inputs == ["src:a"]
    assert outputs == ["audio"]


def test_delay_rounds_to_nearest_millisecond() -> None:
    ctx = FakeCtx()
    FUNCTIONS["delay"].expand(ctx, ["src:a", 1.2346])
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "adelay"
    assert args == {"delays": 1235, "all": 1}
    assert isinstance(args["delays"], int)


def test_acrossfade() -> None:
    ctx = FakeCtx()
    out = FUNCTIONS["acrossfade"].expand(ctx, ["src:a", "src:b", 2.0])
    assert len(ctx.nodes) == 1
    node_id, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "acrossfade"
    assert args == {"d": 2.0}
    assert inputs == ["src:a", "src:b"]
    assert outputs == ["audio"]
    assert out == node_id


def test_areverse() -> None:
    ctx = FakeCtx()
    FUNCTIONS["areverse"].expand(ctx, ["src:a"])
    assert len(ctx.nodes) == 1
    _, filt, args, inputs, outputs = ctx.nodes[0]
    assert filt == "areverse"
    assert args == {}
    assert inputs == ["src:a"]
    assert outputs == ["audio"]


def test_signatures_single_variant() -> None:
    assert signatures("overlay") == "overlay(video, video, num, num)"


def test_signatures_multi_variant() -> None:
    sig = signatures("scale")
    assert sig == "scale(video, num) | scale(video, num, num)"


def test_signatures_audio_variant() -> None:
    assert signatures("amix") == "amix(audio, audio)"


def test_signatures_all_names_resolve() -> None:
    for name in EXPECTED_NAMES:
        sig = signatures(name)
        assert sig.startswith(f"{name}(")


# --------------------------------------------------------------------------
# RFC-003 "Tier-1 named extras": named_target
# --------------------------------------------------------------------------

# Sample expand() args for every entry that has named_target set (all of
# them except the blur_regions macro); each of these produces exactly one
# node, so the single node's filter must equal named_target.
_NAMED_TARGET_SAMPLE_ARGS: dict[str, list[object]] = {
    "scale": ["src:a", 2],
    "crop": ["src:a", 10, 20, 100, 200],
    "overlay": ["src:base", "src:top", 5, 6],
    "hflip": ["src:a"],
    "vflip": ["src:a"],
    "blur": ["src:a", 3.5],
    "draw_box": ["src:a", 1, 2, 3, 4, "red"],
    "text": ["src:a", "hello", 10, 20, 24],
    "speed": ["src:a", 2],
    "fade_in": ["src:a", 1.5],
    "fade_out": ["src:a", 2.0],
    "volume": ["src:a", 0.5],
    "amix": ["src:a", "src:b"],
    "atempo": ["src:a", 1.5],
    "afade_in": ["src:a", 1.5],
    "afade_out": ["src:a", 2.0],
    "reverb": ["src:a", 0.4],
    "rotate": ["src:a", 90],
    "pad": ["src:a", 640, 480],
    "hstack": ["src:a", "src:b"],
    "vstack": ["src:a", "src:b"],
    "fps": ["src:a", 30],
    "sharpen": ["src:a", 1.5],
    "deinterlace": ["src:a"],
    "denoise": ["src:a", 4],
    "brightness": ["src:a", 0.2],
    "contrast": ["src:a", 1.2],
    "saturate": ["src:a", 1.5],
    "grayscale": ["src:a"],
    "crossfade": ["src:a", "src:b", 1.0, 5.0],
    "subtitles": ["src:a", "subs.srt"],
    "reverse": ["src:a"],
    "normalize": ["src:a"],
    "highpass": ["src:a", 200],
    "lowpass": ["src:a", 3000],
    "delay": ["src:a", 0.25],
    "acrossfade": ["src:a", "src:b", 2.0],
    "areverse": ["src:a"],
}


def test_named_target_set_on_every_entry() -> None:
    # Every FUNCTIONS entry sets named_target explicitly (no accidental
    # reliance on the dataclass default).
    assert set(_NAMED_TARGET_SAMPLE_ARGS) | {"blur_regions"} == EXPECTED_NAMES


def test_named_target_matches_expand_output_filter() -> None:
    for name, args in _NAMED_TARGET_SAMPLE_ARGS.items():
        spec = FUNCTIONS[name]
        assert spec.named_target is not None, name
        ctx = FakeCtx()
        spec.expand(ctx, args)
        # At least one recorded node's filter equals named_target...
        assert any(node[1] == spec.named_target for node in ctx.nodes), name
        # ...and, stronger, these entries all produce exactly one node, so
        # that single node's filter must equal named_target.
        assert len(ctx.nodes) == 1, name
        assert ctx.nodes[0][1] == spec.named_target, name


def test_blur_regions_named_target_is_none() -> None:
    # blur_regions is a macro (three nodes: crop, gblur, overlay) with no
    # single underlying filter to validate named extras against.
    assert FUNCTIONS["blur_regions"].named_target is None
