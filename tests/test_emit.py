"""Tests for the emit pass (plans 007 + 018).

Graphs are hand-built with ir.Node/ir.Graph/ir.Output -- emit must not depend
on the parser/lower/split modules.

The ``@pytest.mark.exec`` test at the bottom runs real ffmpeg against the
generated fixtures (``python scripts/gen_fixtures.py``); it is excluded from
the default run by ``addopts = -m "not exec"``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from sqlmpeg.emit import Emitted, OutputMap, _escape_value, build_ffmpeg_args, emit
from sqlmpeg.errors import ErrorCode, SqlmpegError
from sqlmpeg.ir import Graph, Node, Output, Sink, StreamType


def _node(
    node_id: str,
    filter_name: str,
    args: dict[str, object],
    inputs: list[str],
    outputs: list[StreamType] | None = None,
) -> Node:
    return Node(
        id=node_id,
        filter=filter_name,
        args=dict(args),
        inputs=list(inputs),
        outputs=list(outputs) if outputs is not None else ["video"],
    )


def _out(
    ref: str,
    type: StreamType = "video",
    *,
    name: str | None = None,
    metadata: dict[str, str] | None = None,
) -> Output:
    return Output(ref=ref, type=type, name=name, metadata=dict(metadata or {}))


def _graph(
    nodes: list[Node],
    outputs: list[Output],
    *,
    input_paths: list[str] | None = None,
    sources: dict[str, int] | None = None,
    sink: Sink | None = None,
    input_trims: dict[str, tuple[float, float]] | None = None,
) -> Graph:
    return Graph(
        input_paths=list(input_paths or ["a.mp4"]),
        sources=dict(sources or {"a": 0}),
        nodes={n.id: n for n in nodes},
        outputs=list(outputs),
        sink=sink,
        input_trims=dict(input_trims or {}),
    )


# ---------------------------------------------------------------------------
# chains, labels, semicolons
# ---------------------------------------------------------------------------


def test_single_node_graph() -> None:
    g = _graph(
        [_node("n1", "crop", {"w": 600, "h": 200, "x": 1200, "y": 50}, ["src:a:v:0"])],
        [_out("n1")],
    )
    e = emit(g)
    assert e.filter_complex == "[0:v:0]crop=w=600:h=200:x=1200:y=50[out0]"
    assert e.inputs == ["a.mp4"]
    assert e.maps == [OutputMap(target="[out0]", type="video", copy=False, metadata={})]
    assert isinstance(e, Emitted)


def test_readme_example_shape() -> None:
    """WITH pip AS (scale(crop(b.frame,...), 0.5)) SELECT overlay(a.frame, pip.frame, 20, 20)."""
    g = _graph(
        [
            _node("n1", "crop", {"w": 600, "h": 200, "x": 1200, "y": 50}, ["src:b:v:0"]),
            _node("n2", "scale", {"w": "iw*0.5", "h": "-2"}, ["n1"]),
            _node("n3", "overlay", {"x": 20, "y": 20}, ["src:a:v:0", "n2"]),
        ],
        [_out("n3")],
        input_paths=["game.mp4", "game.mp4"],
        sources={"a": 0, "b": 1},
    )
    e = emit(g)
    assert e.filter_complex == (
        "[1:v:0]crop=w=600:h=200:x=1200:y=50,scale=w=iw*0.5:h=-2[n2];"
        "[0:v:0][n2]overlay=x=20:y=20[out0]"
    )
    # one comma-chain (crop,scale) and one semicolon between the two chains
    assert e.filter_complex.count(";") == 1
    assert ",scale=" in e.filter_complex


def test_long_linear_run_merges_into_one_chain() -> None:
    g = _graph(
        [
            _node("n1", "trim", {"start": 1, "end": 5}, ["src:a:v:0"]),
            _node("n2", "setpts", {"expr": "PTS-STARTPTS"}, ["n1"]),
            _node("n3", "hflip", {}, ["n2"]),
            _node("n4", "gblur", {"sigma": 2.5}, ["n3"]),
        ],
        [_out("n4")],
    )
    e = emit(g)
    assert e.filter_complex == (
        "[0:v:0]trim=start=1:end=5,setpts=PTS-STARTPTS,hflip,gblur=sigma=2.5[out0]"
    )
    assert ";" not in e.filter_complex


def test_audio_source_ref_renders_typed_index() -> None:
    g = _graph(
        [_node("n1", "volume", {"volume": 0.5}, ["src:a:a:1"], ["audio"])],
        [_out("n1", "audio")],
    )
    e = emit(g)
    assert e.filter_complex == "[0:a:1]volume=volume=0.5[out0]"
    assert e.maps == [OutputMap(target="[out0]", type="audio", copy=False, metadata={})]


def test_diamond_post_split_labels_and_semicolons() -> None:
    """src:a:v:0 split in two, one branch blurred, both recombined by overlay."""
    g = _graph(
        [
            _node("src_a_split", "split", {"n": 2}, ["src:a:v:0"], ["video", "video"]),
            _node("n1", "gblur", {"sigma": 5}, ["src_a_split:1"]),
            _node("n2", "overlay", {"x": 0, "y": 0}, ["src_a_split:0", "n1"]),
        ],
        [_out("n2")],
    )
    e = emit(g)
    assert e.filter_complex == (
        "[0:v:0]split=2[src_a_split0][src_a_split1];"
        "[src_a_split1]gblur=sigma=5[n1];"
        "[src_a_split0][n1]overlay=x=0:y=0[out0]"
    )


def test_split_node_gets_one_label_per_declared_output_pad() -> None:
    """A split whose producer is not adjacent renders [n1]split=2[n1_split0][n1_split1]."""
    g = _graph(
        [
            _node("n1", "hflip", {}, ["src:a:v:0"]),
            _node("n2", "vflip", {}, ["src:b:v:0"]),
            _node("n1_split", "split", {"n": 2}, ["n1"], ["video", "video"]),
            _node("n3", "overlay", {"x": 0, "y": 0}, ["n1_split:0", "n2"]),
            _node("n4", "overlay", {"x": 10, "y": 10}, ["n1_split:1", "n3"]),
        ],
        [_out("n4")],
        input_paths=["a.mp4", "b.mp4"],
        sources={"a": 0, "b": 1},
    )
    e = emit(g)
    assert e.filter_complex == (
        "[0:v:0]hflip[n1];"
        "[1:v:0]vflip[n2];"
        "[n1]split=2[n1_split0][n1_split1];"
        "[n1_split0][n2]overlay=x=0:y=0[n3];"
        "[n1_split1][n3]overlay=x=10:y=10[out0]"
    )


def test_pad_count_comes_from_outputs_not_from_split_args() -> None:
    """``n`` is only an argument now; ``outputs`` decides how many pads render."""
    g = _graph(
        [
            _node("s", "split", {"n": 3}, ["src:a:v:0"], ["video", "video", "video"]),
            _node("n1", "hflip", {}, ["s:0"]),
            _node("n2", "vflip", {}, ["s:1"]),
            _node("n3", "gblur", {"sigma": 1}, ["s:2"]),
            _node("n4", "overlay", {"x": 0, "y": 0}, ["n1", "n2"]),
            _node("n5", "overlay", {"x": 0, "y": 0}, ["n4", "n3"]),
        ],
        [_out("n5")],
    )
    e = emit(g)
    assert e.filter_complex.startswith("[0:v:0]split=3[s0][s1][s2];")
    assert "[s0]hflip[n1];" in e.filter_complex
    assert "[s1]vflip[n2];" in e.filter_complex
    assert "[s2]gblur=sigma=1[n3];" in e.filter_complex
    assert e.filter_complex.endswith("[n4][n3]overlay=x=0:y=0[out0]")


def test_asplit_renders_value_only_args_and_audio_pads() -> None:
    g = _graph(
        [
            _node("s", "asplit", {"n": 2}, ["src:a:a:0"], ["audio", "audio"]),
            _node("n1", "volume", {"volume": 2}, ["s:0"], ["audio"]),
            _node("n2", "amix", {"inputs": 2}, ["s:1", "n1"], ["audio"]),
        ],
        [_out("n2", "audio")],
    )
    e = emit(g)
    assert e.filter_complex == (
        "[0:a:0]asplit=2[s0][s1];"
        "[s0]volume=volume=2[n1];"
        "[s1][n1]amix=inputs=2[out0]"
    )


def test_split_as_chain_tail_merges_with_its_producer() -> None:
    g = _graph(
        [
            _node("n1", "hflip", {}, ["src:a:v:0"]),
            _node("n1_split", "split", {"n": 2}, ["n1"], ["video", "video"]),
            _node("n2", "gblur", {"sigma": 3}, ["n1_split:0"]),
            _node("n3", "overlay", {"x": 0, "y": 0}, ["n1_split:1", "n2"]),
        ],
        [_out("n3")],
    )
    e = emit(g)
    assert e.filter_complex == (
        "[0:v:0]hflip,split=2[n1_split0][n1_split1];"
        "[n1_split0]gblur=sigma=3[n2];"
        "[n1_split1][n2]overlay=x=0:y=0[out0]"
    )


# ---------------------------------------------------------------------------
# multi-output maps, passthrough, metadata
# ---------------------------------------------------------------------------


def test_pure_passthrough_graph_has_empty_filter_complex() -> None:
    """SELECT a.video[1], a.audio[2] -- a remap, nothing re-encoded."""
    g = _graph([], [_out("src:a:v:0"), _out("src:a:a:1", "audio")])
    e = emit(g)
    assert e.filter_complex == ""
    assert e.maps == [
        OutputMap(target="0:v:0", type="video", copy=True, metadata={}),
        OutputMap(target="0:a:1", type="audio", copy=True, metadata={}),
    ]


def test_passthrough_from_second_input_uses_its_index() -> None:
    g = _graph(
        [],
        [_out("src:b:a:2", "audio")],
        input_paths=["a.mp4", "b.mp4"],
        sources={"a": 0, "b": 1},
    )
    assert emit(g).maps == [OutputMap(target="1:a:2", type="audio", copy=True, metadata={})]


def test_mixed_filtered_and_passthrough() -> None:
    """SELECT hflip(a.video[1]), a.audio[1] -- audio never enters the graph."""
    g = _graph(
        [_node("n1", "hflip", {}, ["src:a:v:0"])],
        [_out("n1"), _out("src:a:a:0", "audio", name="orig")],
    )
    e = emit(g)
    assert e.filter_complex == "[0:v:0]hflip[out0]"
    assert e.maps == [
        OutputMap(target="[out0]", type="video", copy=False, metadata={}),
        OutputMap(target="0:a:0", type="audio", copy=True, metadata={}),
    ]


def test_passthrough_first_still_labels_filtered_output_by_its_index() -> None:
    """Labels track the output index, not the count of filtered outputs."""
    g = _graph(
        [_node("n1", "volume", {"volume": 0.5}, ["src:a:a:0"], ["audio"])],
        [_out("src:a:v:0"), _out("n1", "audio")],
    )
    e = emit(g)
    assert e.filter_complex == "[0:a:0]volume=volume=0.5[out1]"
    assert e.maps == [
        OutputMap(target="0:v:0", type="video", copy=True, metadata={}),
        OutputMap(target="[out1]", type="audio", copy=False, metadata={}),
    ]


def test_two_filtered_outputs_are_labelled_in_graph_output_order() -> None:
    g = _graph(
        [
            _node("n1", "hflip", {}, ["src:a:v:0"]),
            _node("n2", "volume", {"volume": 2}, ["src:a:a:0"], ["audio"]),
        ],
        [_out("n2", "audio"), _out("n1")],  # deliberately not node order
    )
    e = emit(g)
    assert e.filter_complex == "[0:v:0]hflip[out1];[0:a:0]volume=volume=2[out0]"
    assert [m.target for m in e.maps] == ["[out0]", "[out1]"]
    assert [m.type for m in e.maps] == ["audio", "video"]


def test_concat_node_with_video_and_audio_pads() -> None:
    """UNION ALL of (video, audio) branches: concat n=2:v=1:a=1 -> two pads."""
    g = _graph(
        [
            _node(
                "n1",
                "concat",
                {"n": 2, "v": 1, "a": 1},
                ["src:a:v:0", "src:a:a:0", "src:b:v:0", "src:b:a:0"],
                ["video", "audio"],
            )
        ],
        [_out("n1"), _out("n1:1", "audio")],
        input_paths=["a.mp4", "b.mp4"],
        sources={"a": 0, "b": 1},
    )
    e = emit(g)
    assert e.filter_complex == (
        "[0:v:0][0:a:0][1:v:0][1:a:0]concat=n=2:v=1:a=1[out0][out1]"
    )
    assert e.maps == [
        OutputMap(target="[out0]", type="video", copy=False, metadata={}),
        OutputMap(target="[out1]", type="audio", copy=False, metadata={}),
    ]


def test_metadata_is_carried_onto_the_map() -> None:
    g = _graph(
        [_node("n1", "aecho", {"expr": "0.8:0.9:60:0.3"}, ["src:a:a:1"], ["audio"])],
        [_out("n1", "audio", name="fra", metadata={"language": "fra", "title": "VF"})],
    )
    e = emit(g)
    assert e.maps[0].metadata == {"language": "fra", "title": "VF"}
    assert e.maps[0].copy is False


def test_map_metadata_is_a_copy_not_the_output_dict() -> None:
    output = _out("src:a:a:0", "audio", metadata={"language": "eng"})
    g = _graph([], [output])
    e = emit(g)
    e.maps[0].metadata["language"] = "mangled"
    assert output.metadata == {"language": "eng"}


def test_output_label_wins_over_generated_label() -> None:
    """A node id of 'out0' does not steal the reserved output label."""
    g = _graph(
        [
            _node("out0", "hflip", {}, ["src:a:v:0"]),
            _node("n2", "vflip", {}, ["out0"]),
        ],
        [_out("n2")],
    )
    e = emit(g)
    # the 'out0' node is merged into the chain, so its label is elided entirely
    assert e.filter_complex == "[0:v:0]hflip,vflip[out0]"


def test_label_collision_is_broken() -> None:
    g = _graph(
        [
            _node("out0", "hflip", {}, ["src:a:v:0"]),
            _node("n2", "vflip", {}, ["src:b:v:0"]),
            _node("n3", "overlay", {"x": 0, "y": 0}, ["out0", "n2"]),
        ],
        [_out("n3")],
        input_paths=["a.mp4", "b.mp4"],
        sources={"a": 0, "b": 1},
    )
    e = emit(g)
    assert e.filter_complex == (
        "[0:v:0]hflip[out0_];[1:v:0]vflip[n2];[out0_][n2]overlay=x=0:y=0[out0]"
    )


def test_unused_output_index_is_still_reserved() -> None:
    """out0 belongs to the passthrough column even though it is never rendered."""
    g = _graph(
        [_node("out0", "hflip", {}, ["src:a:v:0"]), _node("n2", "vflip", {}, ["src:b:v:0"])],
        [_out("src:a:a:0", "audio"), _out("out0"), _out("n2")],
        input_paths=["a.mp4", "b.mp4"],
        sources={"a": 0, "b": 1},
    )
    e = emit(g)
    assert e.filter_complex == "[0:v:0]hflip[out1];[1:v:0]vflip[out2]"
    assert [m.target for m in e.maps] == ["0:a:0", "[out1]", "[out2]"]


def test_labels_are_sanitized() -> None:
    g = _graph(
        [
            _node("a.b-c", "hflip", {}, ["src:a:v:0"]),
            _node("n2", "vflip", {}, ["src:b:v:0"]),
            _node("n3", "overlay", {"x": 0, "y": 0}, ["a.b-c", "n2"]),
        ],
        [_out("n3")],
        input_paths=["a.mp4", "b.mp4"],
        sources={"a": 0, "b": 1},
    )
    assert "[a_b_c]" in emit(g).filter_complex


# ---------------------------------------------------------------------------
# argument rendering
# ---------------------------------------------------------------------------


def test_bare_filter_renders_without_equals() -> None:
    g = _graph([_node("n1", "hflip", {}, ["src:a:v:0"])], [_out("n1")])
    assert emit(g).filter_complex == "[0:v:0]hflip[out0]"


def test_expr_key_renders_value_only() -> None:
    g = _graph([_node("n1", "setpts", {"expr": "PTS/2"}, ["src:a:v:0"])], [_out("n1")])
    assert emit(g).filter_complex == "[0:v:0]setpts=PTS/2[out0]"


def test_concat_renders_named_args() -> None:
    g = _graph(
        [_node("n1", "concat", {"n": 2, "v": 1, "a": 0}, ["src:a:v:0", "src:b:v:0"])],
        [_out("n1")],
        input_paths=["a.mp4", "b.mp4"],
        sources={"a": 0, "b": 1},
    )
    assert emit(g).filter_complex == "[0:v:0][1:v:0]concat=n=2:v=1:a=0[out0]"


def test_args_render_in_insertion_order_not_sorted() -> None:
    g = _graph([_node("n1", "crop", {"w": 1, "h": 2, "x": 3, "y": 4}, ["src:a:v:0"])], [_out("n1")])
    assert emit(g).filter_complex == "[0:v:0]crop=w=1:h=2:x=3:y=4[out0]"
    g2 = _graph(
        [_node("n1", "crop", {"y": 4, "x": 3, "h": 2, "w": 1}, ["src:a:v:0"])], [_out("n1")]
    )
    assert emit(g2).filter_complex == "[0:v:0]crop=y=4:x=3:h=2:w=1[out0]"


def test_scalar_types_render() -> None:
    g = _graph(
        [_node("n1", "f", {"i": 3, "f": 0.5, "s": "auto", "b": True, "z": False}, ["src:a:v:0"])],
        [_out("n1")],
    )
    assert emit(g).filter_complex == "[0:v:0]f=i=3:f=0.5:s=auto:b=1:z=0[out0]"


def test_unrenderable_arg_type_is_internal() -> None:
    g = _graph([_node("n1", "f", {"k": [1, 2]}, ["src:a:v:0"])], [_out("n1")])
    with pytest.raises(SqlmpegError) as excinfo:
        emit(g)
    assert excinfo.value.code is ErrorCode.INTERNAL


def test_drawtext_value_goes_through_the_escaper() -> None:
    g = _graph(
        [_node("n1", "drawtext", {"text": "12:30, take 'one'", "x": 10}, ["src:a:v:0"])],
        [_out("n1")],
    )
    assert emit(g).filter_complex == (
        r"[0:v:0]drawtext=text=12\\:30\,\ take\ \\\'one\\\':x=10[out0]"
    )


# ---------------------------------------------------------------------------
# escaping (_escape_value unit tests)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "plain",
    ["hflip", "1200", "-2", "iw*0.5", "PTS-STARTPTS", "0x00FF00@0.5", "in_w/2", "a.b+c%d"],
)
def test_escape_plain_values_pass_through_unquoted(plain: str) -> None:
    assert _escape_value(plain) == plain


def test_escape_colon() -> None:
    # ':' separates filter options (level 1): '\:' there, so '\\:' in the graph.
    assert _escape_value("12:30") == r"12\\:30"


def test_escape_single_quote() -> None:
    # level 1 turns ' into \' ; level 2 escapes both characters again.
    assert _escape_value("it's") == r"it\\\'s"


def test_escape_comma() -> None:
    # ',' only matters at the filtergraph level.
    assert _escape_value("a,b") == r"a\,b"


def test_escape_semicolon_and_brackets() -> None:
    assert _escape_value("a;b[c]d") == r"a\;b\[c\]d"


def test_escape_backslash() -> None:
    assert _escape_value("C:\\clips") == r"C\\:\\\\clips"


def test_escape_space_and_hash() -> None:
    assert _escape_value("hello world #1") == r"hello\ world\ \#1"


def test_escape_equals() -> None:
    assert _escape_value("w=2") == r"w\\=2"


def test_escape_empty_string_is_quoted() -> None:
    assert _escape_value("") == "''"


def test_escape_ffmpeg_documentation_example() -> None:
    """The worked example from ffmpeg-filters "Notes on filtergraph escaping".

    The docs stop at ``\\\\\\'string\\\\\\'`` / ``\\\\:`` / ``\\,``; we also
    backslash-escape spaces, which is redundant-but-valid (ffmpeg unescapes
    ``\\ `` back to a plain space).
    """
    got = _escape_value("this is a 'string': may contain one, or more, special characters")
    assert got == (
        r"this\ is\ a\ \\\'string\\\'\\:\ may\ contain\ one\,\ or\ more\,\ "
        r"special\ characters"
    )
    assert got.replace("\\ ", " ") == (
        r"this is a \\\'string\\\'\\: may contain one\, or more\, special characters"
    )


# ---------------------------------------------------------------------------
# malformed graphs -> INTERNAL
# ---------------------------------------------------------------------------


def _assert_internal(g: Graph) -> SqlmpegError:
    with pytest.raises(SqlmpegError) as excinfo:
        emit(g)
    assert excinfo.value.code is ErrorCode.INTERNAL
    return excinfo.value


def test_cycle_is_internal_error() -> None:
    g = _graph(
        [_node("n1", "hflip", {}, ["n2"]), _node("n2", "vflip", {}, ["n1"])],
        [_out("n2")],
    )
    err = _assert_internal(g)
    assert "topological" in err.message


def test_forward_reference_is_internal_error() -> None:
    g = _graph(
        [_node("n1", "vflip", {}, ["n2"]), _node("n2", "hflip", {}, ["src:a:v:0"])],
        [_out("n1")],
    )
    _assert_internal(g)


def test_unknown_node_ref_is_internal_error() -> None:
    g = _graph([_node("n1", "hflip", {}, ["nope"])], [_out("n1")])
    err = _assert_internal(g)
    assert "unknown node" in err.message


def test_unknown_source_alias_is_internal_error() -> None:
    g = _graph([_node("n1", "hflip", {}, ["src:zz:v:0"])], [_out("n1")])
    err = _assert_internal(g)
    assert "unknown source alias" in err.message


def test_malformed_source_ref_is_internal_error() -> None:
    g = _graph([_node("n1", "hflip", {}, ["src:a:x:0"])], [_out("n1")])
    err = _assert_internal(g)
    assert "malformed source ref" in err.message


def test_untyped_v1_source_ref_is_internal_error() -> None:
    """v0/v1's untyped ``src:a`` is retired and must not silently render."""
    _assert_internal(_graph([_node("n1", "hflip", {}, ["src:a"])], [_out("n1")]))


def test_pad_index_past_output_count_is_internal_error() -> None:
    g = _graph(
        [
            _node("s", "split", {"n": 2}, ["src:a:v:0"], ["video", "video"]),
            _node("n1", "hflip", {}, ["s:0"]),
            _node("n2", "vflip", {}, ["s:5"]),
            _node("n3", "overlay", {"x": 0, "y": 0}, ["n1", "n2"]),
        ],
        [_out("n3")],
    )
    _assert_internal(g)


def test_node_without_output_pads_is_internal_error() -> None:
    _assert_internal(_graph([_node("n1", "hflip", {}, ["src:a:v:0"], [])], [_out("n1")]))


def test_unsplit_fanout_is_internal_error() -> None:
    """emit refuses to render a pad consumed twice -- ffmpeg pads are consume-once."""
    g = _graph(
        [
            _node("n1", "hflip", {}, ["src:a:v:0"]),
            _node("n2", "gblur", {"sigma": 1}, ["n1"]),
            _node("n3", "overlay", {"x": 0, "y": 0}, ["n1", "n2"]),
        ],
        [_out("n3")],
    )
    err = _assert_internal(g)
    assert "consume-once" in err.message


def test_pad_feeding_both_a_node_and_an_output_is_internal_error() -> None:
    """An Output counts as a consumer, so this needs a split too."""
    g = _graph(
        [
            _node("n1", "hflip", {}, ["src:a:v:0"]),
            _node("n2", "gblur", {"sigma": 1}, ["n1"]),
        ],
        [_out("n1"), _out("n2")],
    )
    err = _assert_internal(g)
    assert "consume-once" in err.message


def test_src_pad_used_by_a_node_and_an_output_is_internal_error() -> None:
    g = _graph(
        [_node("n1", "hflip", {}, ["src:a:v:0"])],
        [_out("n1"), _out("src:a:v:0")],
    )
    err = _assert_internal(g)
    assert "consume-once" in err.message


def test_two_outputs_naming_the_same_src_pad_is_internal_error() -> None:
    g = _graph([], [_out("src:a:a:0", "audio"), _out("src:a:a:0", "audio")])
    err = _assert_internal(g)
    assert "consume-once" in err.message


def test_two_outputs_naming_the_same_node_pad_is_internal_error() -> None:
    g = _graph([_node("n1", "hflip", {}, ["src:a:v:0"])], [_out("n1"), _out("n1:0")])
    err = _assert_internal(g)
    assert "consume-once" in err.message


def test_empty_output_ref_is_internal_error() -> None:
    _assert_internal(_graph([_node("n1", "hflip", {}, ["src:a:v:0"])], [_out("")]))


def test_no_outputs_is_internal_error() -> None:
    err = _assert_internal(_graph([_node("n1", "hflip", {}, ["src:a:v:0"])], []))
    assert "no outputs" in err.message


# ---------------------------------------------------------------------------
# build_ffmpeg_args
# ---------------------------------------------------------------------------


def test_build_ffmpeg_args_exact_list() -> None:
    g = _graph(
        [
            _node("n1", "crop", {"w": 600, "h": 200, "x": 1200, "y": 50}, ["src:b:v:0"]),
            _node("n2", "scale", {"w": "iw*0.5", "h": "-2"}, ["n1"]),
            _node("n3", "overlay", {"x": 20, "y": 20}, ["src:a:v:0", "n2"]),
        ],
        [_out("n3")],
        input_paths=["game.mp4", "game.mp4"],
        sources={"a": 0, "b": 1},
    )
    e = emit(g)
    assert build_ffmpeg_args(e, "out.mp4") == [
        "ffmpeg",
        "-i",
        "game.mp4",
        "-i",
        "game.mp4",
        "-filter_complex",
        "[1:v:0]crop=w=600:h=200:x=1200:y=50,scale=w=iw*0.5:h=-2[n2];"
        "[0:v:0][n2]overlay=x=20:y=20[out0]",
        "-map",
        "[out0]",
        "out.mp4",
    ]


def test_build_ffmpeg_args_single_input() -> None:
    e = Emitted(
        inputs=["a.mp4"],
        filter_complex="[0:v:0]hflip[out0]",
        maps=[OutputMap(target="[out0]", type="video", copy=False, metadata={})],
    )
    assert build_ffmpeg_args(e, "/tmp/o.mkv") == [
        "ffmpeg",
        "-i",
        "a.mp4",
        "-filter_complex",
        "[0:v:0]hflip[out0]",
        "-map",
        "[out0]",
        "/tmp/o.mkv",
    ]


def test_build_ffmpeg_args_omits_filter_complex_when_empty() -> None:
    """SELECT a.video[1], a.audio[2] -- a pure remap, no filtergraph at all."""
    g = _graph([], [_out("src:a:v:0"), _out("src:a:a:1", "audio")])
    assert build_ffmpeg_args(emit(g), "out.mp4") == [
        "ffmpeg",
        "-i",
        "a.mp4",
        "-map",
        "0:v:0",
        "-c:0",
        "copy",
        "-map",
        "0:a:1",
        "-c:1",
        "copy",
        "out.mp4",
    ]


def test_build_ffmpeg_args_mixed_with_metadata() -> None:
    g = _graph(
        [_node("n1", "hflip", {}, ["src:a:v:0"])],
        [
            _out("n1", metadata={"title": "flipped"}),
            _out("src:a:a:0", "audio", metadata={"language": "eng"}),
        ],
    )
    assert build_ffmpeg_args(emit(g), "out.mp4") == [
        "ffmpeg",
        "-i",
        "a.mp4",
        "-filter_complex",
        "[0:v:0]hflip[out0]",
        "-map",
        "[out0]",
        "-metadata:s:0",
        "title=flipped",
        "-map",
        "0:a:0",
        "-c:1",
        "copy",
        "-metadata:s:1",
        "language=eng",
        "out.mp4",
    ]


def test_build_ffmpeg_args_sorts_metadata_keys() -> None:
    e = Emitted(
        inputs=["a.mp4"],
        filter_complex="",
        maps=[
            OutputMap(
                target="0:a:0",
                type="audio",
                copy=True,
                metadata={"title": "Commentary", "language": "fra", "artist": "x"},
            )
        ],
    )
    args = build_ffmpeg_args(e, "out.mp4")
    assert args[args.index("-map") :] == [
        "-map",
        "0:a:0",
        "-c:0",
        "copy",
        "-metadata:s:0",
        "artist=x",
        "-metadata:s:0",
        "language=fra",
        "-metadata:s:0",
        "title=Commentary",
        "out.mp4",
    ]


def test_build_ffmpeg_args_metadata_values_are_passed_raw() -> None:
    """Metadata goes through argv, not the filtergraph parser -- no escaping."""
    e = Emitted(
        inputs=["a.mp4"],
        filter_complex="",
        maps=[
            OutputMap(
                target="0:a:0",
                type="audio",
                copy=True,
                metadata={"title": "12:30, take 'one'"},
            )
        ],
    )
    assert "title=12:30, take 'one'" in build_ffmpeg_args(e, "out.mp4")


# ---------------------------------------------------------------------------
# sink (RFC-002, plan 027) -- Graphs are hand-built with sink=Sink(...) here;
# compile_sql cannot yet produce a sinked Graph while plan 026 is in flight.
# ---------------------------------------------------------------------------


def test_emitted_sink_defaults_to_none() -> None:
    g = _graph([], [_out("src:a:v:0")])
    e = emit(g)
    assert e.sink is None


def test_emit_copies_graph_sink() -> None:
    """`emit` copies `g.sink`, not aliases it -- mutating one must not affect the other."""
    sink = Sink(path="out.mkv", options={"video_codec": "libx264"})
    g = _graph([], [_out("src:a:v:0")], sink=sink)
    e = emit(g)
    assert e.sink == sink
    assert e.sink is not sink
    assert e.sink is not None
    e.sink.options["video_codec"] = "mangled"
    assert sink.options["video_codec"] == "libx264"


def test_build_ffmpeg_args_without_out_path_or_sink_raises() -> None:
    g = _graph([], [_out("src:a:v:0")])
    e = emit(g)
    with pytest.raises(ValueError, match="no output path"):
        build_ffmpeg_args(e)


def test_build_ffmpeg_args_uses_sink_path_when_no_out_path() -> None:
    sink = Sink(path="sink.mkv", options={})
    g = _graph([], [_out("src:a:v:0")], sink=sink)
    e = emit(g)
    assert build_ffmpeg_args(e)[-1] == "sink.mkv"


def test_build_ffmpeg_args_out_path_overrides_sink_path() -> None:
    sink = Sink(path="sink.mkv", options={})
    g = _graph([], [_out("src:a:v:0")], sink=sink)
    e = emit(g)
    assert build_ffmpeg_args(e, "override.mp4")[-1] == "override.mp4"


def test_build_ffmpeg_args_no_sink_graph_unchanged_byte_for_byte() -> None:
    """Regression pin: a sinkless graph's args are byte-for-byte what plan 027 found."""
    g = _graph(
        [
            _node("n1", "crop", {"w": 600, "h": 200, "x": 1200, "y": 50}, ["src:b:v:0"]),
            _node("n2", "scale", {"w": "iw*0.5", "h": "-2"}, ["n1"]),
            _node("n3", "overlay", {"x": 20, "y": 20}, ["src:a:v:0", "n2"]),
        ],
        [_out("n3")],
        input_paths=["game.mp4", "game.mp4"],
        sources={"a": 0, "b": 1},
    )
    e = emit(g)
    assert e.sink is None
    assert build_ffmpeg_args(e, "out.mp4") == [
        "ffmpeg",
        "-i",
        "game.mp4",
        "-i",
        "game.mp4",
        "-filter_complex",
        "[1:v:0]crop=w=600:h=200:x=1200:y=50,scale=w=iw*0.5:h=-2[n2];"
        "[0:v:0][n2]overlay=x=20:y=20[out0]",
        "-map",
        "[out0]",
        "out.mp4",
    ]


def test_sink_video_codec_and_crf_render_per_video_output() -> None:
    sink = Sink(path="out.mp4", options={"video_codec": "libx264", "crf": 20})
    g = _graph(
        [
            _node("n1", "hflip", {}, ["src:a:v:0"]),
            _node("n2", "vflip", {}, ["src:b:v:0"]),
        ],
        [_out("n1"), _out("n2")],
        input_paths=["a.mp4", "b.mp4"],
        sources={"a": 0, "b": 1},
        sink=sink,
    )
    e = emit(g)
    args = build_ffmpeg_args(e)
    assert args[args.index("-map") :] == [
        "-map",
        "[out0]",
        "-map",
        "[out1]",
        "-c:0",
        "libx264",
        "-c:1",
        "libx264",
        "-crf:0",
        "20",
        "-crf:1",
        "20",
        "out.mp4",
    ]


def test_sink_audio_bitrate_renders_per_audio_output() -> None:
    sink = Sink(path="out.mp4", options={"audio_bitrate": "192k"})
    g = _graph(
        [
            _node("n1", "volume", {"volume": 1}, ["src:a:a:0"], ["audio"]),
            _node("n2", "volume", {"volume": 2}, ["src:a:a:1"], ["audio"]),
        ],
        [_out("n1", "audio"), _out("n2", "audio")],
        sink=sink,
    )
    e = emit(g)
    args = build_ffmpeg_args(e)
    assert args[args.index("-map") :] == [
        "-map",
        "[out0]",
        "-map",
        "[out1]",
        "-b:0",
        "192k",
        "-b:1",
        "192k",
        "out.mp4",
    ]


def test_sink_format_and_faststart_render_once_at_container_level() -> None:
    sink = Sink(path="out.mp4", options={"format": "mp4", "faststart": True})
    g = _graph([], [_out("src:a:v:0")], sink=sink)
    e = emit(g)
    args = build_ffmpeg_args(e)
    assert args[args.index("-map") :] == [
        "-map",
        "0:v:0",
        "-c:0",
        "copy",
        "-f",
        "mp4",
        "-movflags",
        "+faststart",
        "out.mp4",
    ]


def test_sink_faststart_false_omits_movflags_entirely() -> None:
    sink = Sink(path="out.mp4", options={"faststart": False})
    g = _graph([], [_out("src:a:v:0")], sink=sink)
    e = emit(g)
    args = build_ffmpeg_args(e)
    assert "-movflags" not in args
    assert "+faststart" not in args


def test_sink_options_render_in_insertion_order_not_table_order() -> None:
    """SINK_OPTIONS lists video_codec before crf; Sink.options insertion order wins."""
    sink = Sink(path="out.mp4", options={"crf": 20, "video_codec": "libx264"})
    g = _graph([_node("n1", "hflip", {}, ["src:a:v:0"])], [_out("n1")], sink=sink)
    e = emit(g)
    args = build_ffmpeg_args(e)
    assert args[args.index("-map") :] == [
        "-map",
        "[out0]",
        "-crf:0",
        "20",
        "-c:0",
        "libx264",
        "out.mp4",
    ]


def test_sink_audio_codec_suppresses_passthrough_copy_for_audio_only() -> None:
    """passthrough audio + audio_codec set -> no -c:<i> copy, codec rendered instead."""
    sink = Sink(path="out.mp4", options={"audio_codec": "aac"})
    g = _graph(
        [_node("n1", "hflip", {}, ["src:a:v:0"])],
        [_out("n1"), _out("src:a:a:0", "audio")],
        sink=sink,
    )
    e = emit(g)
    args = build_ffmpeg_args(e)
    assert args[args.index("-map") :] == [
        "-map",
        "[out0]",
        "-map",
        "0:a:0",
        "-c:1",
        "aac",
        "out.mp4",
    ]


def test_sink_video_codec_suppresses_only_video_passthrough_copy() -> None:
    """A video-scoped codec option leaves an untouched audio passthrough's copy alone."""
    sink = Sink(path="out.mp4", options={"video_codec": "libx264"})
    g = _graph([], [_out("src:a:v:0"), _out("src:a:a:0", "audio")], sink=sink)
    e = emit(g)
    args = build_ffmpeg_args(e)
    assert args[args.index("-map") :] == [
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-c:1",
        "copy",
        "-c:0",
        "libx264",
        "out.mp4",
    ]


# ---------------------------------------------------------------------------
# RFC-004: subtitle / data streams -- bare -map only, repeatable
# ---------------------------------------------------------------------------


def test_subtitle_and_data_src_refs_render_as_s_and_d_stream_specs() -> None:
    g = _graph(
        [],
        [_out("src:a:s:0", "subtitle"), _out("src:b:d:2", "data")],
        input_paths=["a.mkv", "b.mkv"],
        sources={"a": 0, "b": 1},
    )
    e = emit(g)
    assert e.filter_complex == ""
    assert e.maps == [
        OutputMap(target="0:s:0", type="subtitle", copy=True, metadata={}),
        OutputMap(target="1:d:2", type="data", copy=True, metadata={}),
    ]


def test_the_same_subtitle_ref_may_be_mapped_twice() -> None:
    """Consume-once is a filtergraph pad rule; a bare -map may repeat."""
    g = _graph(
        [],
        [_out("src:a:s:0", "subtitle"), _out("src:a:s:0", "subtitle")],
        input_paths=["a.mkv"],
    )
    args = build_ffmpeg_args(emit(g), "out.mkv")
    assert args[args.index("-map") :] == [
        "-map",
        "0:s:0",
        "-c:0",
        "copy",
        "-map",
        "0:s:0",
        "-c:1",
        "copy",
        "out.mkv",
    ]


def test_the_same_data_ref_may_be_mapped_twice() -> None:
    g = _graph(
        [], [_out("src:a:d:0", "data"), _out("src:a:d:0", "data")], input_paths=["a.mkv"]
    )
    assert [m.target for m in emit(g).maps] == ["0:d:0", "0:d:0"]


def test_a_duplicated_video_pad_is_still_a_consume_once_error() -> None:
    """The exemption is for subtitle/data ONLY -- video still needs the split pass."""
    g = _graph([], [_out("src:a:v:0"), _out("src:a:v:0")])
    with pytest.raises(SqlmpegError) as excinfo:
        emit(g)
    assert excinfo.value.code is ErrorCode.INTERNAL
    assert "consume-once" in excinfo.value.message


def test_subtitle_output_keeps_its_language_metadata() -> None:
    g = _graph(
        [],
        [_out("src:a:s:0", "subtitle", metadata={"language": "eng"})],
        input_paths=["a.mkv"],
    )
    args = build_ffmpeg_args(emit(g), "out.mkv")
    assert "-metadata:s:0" in args
    assert args[args.index("-metadata:s:0") + 1] == "language=eng"


def test_sink_subtitle_codec_renders_per_subtitle_output() -> None:
    sink = Sink(path="out.mp4", options={"subtitle_codec": "mov_text"})
    g = _graph(
        [],
        [_out("src:a:v:0"), _out("src:a:a:0", "audio"), _out("src:a:s:0", "subtitle")],
        input_paths=["a.mkv"],
        sink=sink,
    )
    args = build_ffmpeg_args(emit(g))
    assert args[args.index("-map") :] == [
        "-map",
        "0:v:0",
        "-c:0",
        "copy",
        "-map",
        "0:a:0",
        "-c:1",
        "copy",
        "-map",
        "0:s:0",
        # copy suppressed on the SUBTITLE output only: the option table's
        # scope drives it, so subtitle_codec generalizes for free (plan 033's
        # contract note; this is the test it had to skip).
        "-c:2",
        "mov_text",
        "out.mp4",
    ]


def test_sink_subtitle_codec_leaves_video_and_audio_copies_alone() -> None:
    sink = Sink(path="out.mkv", options={"subtitle_codec": "srt"})
    g = _graph(
        [],
        [_out("src:a:a:0", "audio"), _out("src:a:s:0", "subtitle")],
        input_paths=["a.mkv"],
        sink=sink,
    )
    args = build_ffmpeg_args(emit(g))
    assert "-c:0" in args and args[args.index("-c:0") + 1] == "copy"
    assert "-c:1" in args and args[args.index("-c:1") + 1] == "srt"


def test_sink_video_codec_does_not_suppress_a_subtitle_copy() -> None:
    sink = Sink(path="out.mkv", options={"video_codec": "libx264"})
    g = _graph(
        [],
        [_out("src:a:v:0"), _out("src:a:s:0", "subtitle")],
        input_paths=["a.mkv"],
        sink=sink,
    )
    args = build_ffmpeg_args(emit(g))
    assert args[args.index("-map") :] == [
        "-map",
        "0:v:0",
        "-map",
        "0:s:0",
        "-c:1",
        "copy",
        "-c:0",
        "libx264",
        "out.mkv",
    ]


# ---------------------------------------------------------------------------
# RFC-004 input seek (plan 035): -ss/-to in front of the owning -i
# ---------------------------------------------------------------------------


def test_emitted_input_trims_default_to_all_none() -> None:
    """Always parallel to `inputs`, so a consumer can index it directly."""
    g = _graph([], [_out("src:a:v:0")], input_paths=["a.mp4", "b.mp4"], sources={"a": 0, "b": 1})
    assert emit(g).input_trims == [None, None]


def test_alias_keyed_windows_resolve_to_input_positions() -> None:
    """`Graph.input_trims` is alias-keyed; `Emitted.input_trims` is -i-ordered."""
    g = _graph(
        [],
        [_out("src:b:v:0")],
        input_paths=["a.mp4", "b.mp4", "c.mp4"],
        sources={"a": 0, "b": 1, "c": 2},
        input_trims={"c": (7, 8), "a": (1, 2)},
    )
    assert emit(g).input_trims == [(1, 2), None, (7, 8)]


def test_build_ffmpeg_args_puts_ss_and_to_before_the_owning_input() -> None:
    g = _graph(
        [],
        [_out("src:a:v:0"), _out("src:b:a:0", "audio")],
        input_paths=["a.mp4", "b.mp4"],
        sources={"a": 0, "b": 1},
        input_trims={"b": (2, 4)},
    )
    assert build_ffmpeg_args(emit(g), "out.mp4") == [
        "ffmpeg",
        "-i",
        "a.mp4",
        "-ss",
        "2",
        "-to",
        "4",
        "-i",
        "b.mp4",
        "-map",
        "0:v:0",
        "-c:0",
        "copy",
        "-map",
        "1:a:0",
        "-c:1",
        "copy",
        "out.mp4",
    ]


def test_seek_times_render_by_the_scalar_rules() -> None:
    """Same rendering a filter argument gets: 12.5 -> "12.5", 5 -> "5"."""
    g = _graph([], [_out("src:a:v:0")], input_trims={"a": (12.5, 60)})
    args = build_ffmpeg_args(emit(g), "out.mp4")
    assert args[:6] == ["ffmpeg", "-ss", "12.5", "-to", "60", "-i"]


def test_a_seeked_input_still_stream_copies() -> None:
    """The new capability: a window no longer forces a filter node, so the
    output stays a passthrough (`-c:<i> copy`) with no filtergraph at all."""
    g = _graph([], [_out("src:a:v:0")], input_trims={"a": (5, 60)})
    e = emit(g)
    assert e.filter_complex == ""
    assert e.maps[0].copy is True
    assert "-filter_complex" not in build_ffmpeg_args(e, "out.mp4")


def test_a_hand_built_emitted_needs_no_input_trims() -> None:
    """The empty default means "no input is seeked", so an Emitted built
    without windows renders exactly as it did before plan 035."""
    e = Emitted(
        inputs=["a.mp4"],
        filter_complex="",
        maps=[OutputMap(target="0:v:0", type="video", copy=True, metadata={})],
    )
    assert build_ffmpeg_args(e, "out.mp4")[:3] == ["ffmpeg", "-i", "a.mp4"]


def test_input_trim_on_an_unknown_alias_is_internal_error() -> None:
    g = _graph([], [_out("src:a:v:0")], input_trims={"nope": (1, 2)})
    err = _assert_internal(g)
    assert "unknown source alias" in err.message


def test_two_aliases_disagreeing_on_one_inputs_window_is_internal_error() -> None:
    """Aliases each own an -i, so this cannot come from lowering -- but a
    hand-built graph that says otherwise must not silently drop a window."""
    g = _graph(
        [],
        [_out("src:a:v:0")],
        sources={"a": 0, "b": 0},
        input_trims={"a": (1, 2), "b": (3, 4)},
    )
    err = _assert_internal(g)
    assert "two different trim windows" in err.message


# ---------------------------------------------------------------------------
# real ffmpeg sanity check (exec-marked)
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
_AV = _FIXTURES_DIR / "av.mp4"
_SUBPROCESS_TIMEOUT = 60.0


def _require_ffmpeg_and_fixture() -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not found on PATH")
    if not _AV.exists():
        pytest.skip(f"fixture missing: {_AV} (run scripts/gen_fixtures.py first)")


def _run_ffmpeg(args: list[str]) -> None:
    args = list(args)
    args.insert(1, "-y")
    result = subprocess.run(args, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT)
    assert result.returncode == 0, f"{args}\n{result.stderr}"


def _probe_codec_types(path: Path) -> list[str]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    return [str(stream["codec_type"]) for stream in data["streams"]]


@pytest.mark.exec
def test_multi_map_command_runs_under_real_ffmpeg(tmp_path: Path) -> None:
    """hflip(a.video[1]) + volume(a.audio[1], 0.5) -> a real two-stream file."""
    _require_ffmpeg_and_fixture()
    g = _graph(
        [
            _node("n1", "hflip", {}, ["src:a:v:0"]),
            _node("n2", "volume", {"volume": 0.5}, ["src:a:a:0"], ["audio"]),
        ],
        [_out("n1"), _out("n2", "audio", metadata={"language": "eng"})],
        input_paths=[str(_AV)],
    )
    e = emit(g)
    assert e.filter_complex == "[0:v:0]hflip[out0];[0:a:0]volume=volume=0.5[out1]"

    out_path = tmp_path / "multimap.mp4"
    _run_ffmpeg(build_ffmpeg_args(e, str(out_path)))

    assert out_path.exists()
    assert _probe_codec_types(out_path) == ["video", "audio"]


@pytest.mark.exec
def test_passthrough_map_runs_under_real_ffmpeg(tmp_path: Path) -> None:
    """hflip(a.video[1]) + a.audio[1] -- the audio stream is copied, not filtered."""
    _require_ffmpeg_and_fixture()
    g = _graph(
        [_node("n1", "hflip", {}, ["src:a:v:0"])],
        [_out("n1"), _out("src:a:a:0", "audio", metadata={"language": "eng"})],
        input_paths=[str(_AV)],
    )
    e = emit(g)
    out_path = tmp_path / "passthrough.mp4"
    _run_ffmpeg(build_ffmpeg_args(e, str(out_path)))

    assert _probe_codec_types(out_path) == ["video", "audio"]
