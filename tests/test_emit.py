"""Tests for the emit pass (plan 007).

Graphs are hand-built with ir.Node/ir.Graph -- emit must not depend on the
parser/lower/split modules.
"""

from __future__ import annotations

import pytest

from sqlmpeg.emit import Emitted, _escape_value, build_ffmpeg_args, emit
from sqlmpeg.errors import ErrorCode, SqlmpegError
from sqlmpeg.ir import Graph, Node


def _graph(
    nodes: list[Node],
    output: str,
    *,
    input_paths: list[str] | None = None,
    sources: dict[str, int] | None = None,
) -> Graph:
    return Graph(
        input_paths=list(input_paths or ["a.mp4"]),
        sources=dict(sources or {"a": 0}),
        nodes={n.id: n for n in nodes},
        output=output,
    )


# ---------------------------------------------------------------------------
# chains, labels, semicolons
# ---------------------------------------------------------------------------


def test_single_node_graph() -> None:
    g = _graph([Node("n1", "crop", {"w": 600, "h": 200, "x": 1200, "y": 50}, ["src:a"])], "n1")
    e = emit(g)
    assert e.filter_complex == "[0:v]crop=w=600:h=200:x=1200:y=50[out]"
    assert e.output_label == "out"
    assert e.inputs == ["a.mp4"]
    assert isinstance(e, Emitted)


def test_readme_example_shape() -> None:
    """WITH pip AS (scale(crop(b.frame,...), 0.5)) SELECT overlay(a.frame, pip.frame, 20, 20)."""
    g = _graph(
        [
            Node("n1", "crop", {"w": 600, "h": 200, "x": 1200, "y": 50}, ["src:b"]),
            Node("n2", "scale", {"w": "iw*0.5", "h": "-2"}, ["n1"]),
            Node("n3", "overlay", {"x": 20, "y": 20}, ["src:a", "n2"]),
        ],
        "n3",
        input_paths=["game.mp4", "game.mp4"],
        sources={"a": 0, "b": 1},
    )
    e = emit(g)
    assert e.filter_complex == (
        "[1:v]crop=w=600:h=200:x=1200:y=50,scale=w=iw*0.5:h=-2[n2];"
        "[0:v][n2]overlay=x=20:y=20[out]"
    )
    # one comma-chain (crop,scale) and one semicolon between the two chains
    assert e.filter_complex.count(";") == 1
    assert ",scale=" in e.filter_complex


def test_long_linear_run_merges_into_one_chain() -> None:
    g = _graph(
        [
            Node("n1", "trim", {"start": 1, "end": 5}, ["src:a"]),
            Node("n2", "setpts", {"expr": "PTS-STARTPTS"}, ["n1"]),
            Node("n3", "hflip", {}, ["n2"]),
            Node("n4", "gblur", {"sigma": 2.5}, ["n3"]),
        ],
        "n4",
    )
    e = emit(g)
    assert e.filter_complex == (
        "[0:v]trim=start=1:end=5,setpts=PTS-STARTPTS,hflip,gblur=sigma=2.5[out]"
    )
    assert ";" not in e.filter_complex


def test_diamond_post_split_labels_and_semicolons() -> None:
    """src:a split in two, one branch blurred, both recombined by overlay."""
    g = _graph(
        [
            Node("src_a_split", "split", {"n": 2}, ["src:a"]),
            Node("n1", "gblur", {"sigma": 5}, ["src_a_split:1"]),
            Node("n2", "overlay", {"x": 0, "y": 0}, ["src_a_split:0", "n1"]),
        ],
        "n2",
    )
    e = emit(g)
    assert e.filter_complex == (
        "[0:v]split=2[src_a_split0][src_a_split1];"
        "[src_a_split1]gblur=sigma=5[n1];"
        "[src_a_split0][n1]overlay=x=0:y=0[out]"
    )


def test_split_node_gets_one_label_per_output_pad() -> None:
    """A split whose producer is not adjacent renders [n1]split=2[n1_split0][n1_split1]."""
    g = _graph(
        [
            Node("n1", "hflip", {}, ["src:a"]),
            Node("n2", "vflip", {}, ["src:b"]),
            Node("n1_split", "split", {"n": 2}, ["n1"]),
            Node("n3", "overlay", {"x": 0, "y": 0}, ["n1_split:0", "n2"]),
            Node("n4", "overlay", {"x": 10, "y": 10}, ["n1_split:1", "n3"]),
        ],
        "n4",
        input_paths=["a.mp4", "b.mp4"],
        sources={"a": 0, "b": 1},
    )
    e = emit(g)
    assert "[n1]split=2[n1_split0][n1_split1];" in e.filter_complex
    assert e.filter_complex == (
        "[0:v]hflip[n1];"
        "[1:v]vflip[n2];"
        "[n1]split=2[n1_split0][n1_split1];"
        "[n1_split0][n2]overlay=x=0:y=0[n3];"
        "[n1_split1][n3]overlay=x=10:y=10[out]"
    )


def test_split_output_pads_consumed_in_order() -> None:
    """A 3-way split: pad k is labelled <id><k> and consumed by the right node."""
    g = _graph(
        [
            Node("s", "split", {"n": 3}, ["src:a"]),
            Node("n1", "hflip", {}, ["s:0"]),
            Node("n2", "vflip", {}, ["s:1"]),
            Node("n3", "gblur", {"sigma": 1}, ["s:2"]),
            Node("n4", "overlay", {"x": 0, "y": 0}, ["n1", "n2"]),
            Node("n5", "overlay", {"x": 0, "y": 0}, ["n4", "n3"]),
        ],
        "n5",
    )
    e = emit(g)
    assert e.filter_complex.startswith("[0:v]split=3[s0][s1][s2];")
    assert "[s0]hflip[n1];" in e.filter_complex
    assert "[s1]vflip[n2];" in e.filter_complex
    assert "[s2]gblur=sigma=1[n3];" in e.filter_complex
    assert e.filter_complex.endswith("[n4][n3]overlay=x=0:y=0[out]")


def test_split_as_chain_tail_merges_with_its_producer() -> None:
    g = _graph(
        [
            Node("n1", "hflip", {}, ["src:a"]),
            Node("n1_split", "split", {"n": 2}, ["n1"]),
            Node("n2", "gblur", {"sigma": 3}, ["n1_split:0"]),
            Node("n3", "overlay", {"x": 0, "y": 0}, ["n1_split:1", "n2"]),
        ],
        "n3",
    )
    e = emit(g)
    assert e.filter_complex == (
        "[0:v]hflip,split=2[n1_split0][n1_split1];"
        "[n1_split0]gblur=sigma=3[n2];"
        "[n1_split1][n2]overlay=x=0:y=0[out]"
    )


def test_passthrough_graph_emits_null_filter() -> None:
    g = _graph([], "src:a")
    e = emit(g)
    assert e.filter_complex == "[0:v]null[out]"
    assert e.output_label == "out"


def test_output_label_wins_over_generated_label() -> None:
    """A node id of 'out' does not steal the reserved output label."""
    g = _graph(
        [
            Node("out", "hflip", {}, ["src:a"]),
            Node("n2", "vflip", {}, ["out"]),
        ],
        "n2",
    )
    e = emit(g)
    # the 'out' node is merged into the chain, so its label is elided entirely
    assert e.filter_complex == "[0:v]hflip,vflip[out]"


def test_label_collision_is_broken() -> None:
    g = _graph(
        [
            Node("out", "hflip", {}, ["src:a"]),
            Node("n2", "vflip", {}, ["src:b"]),
            Node("n3", "overlay", {"x": 0, "y": 0}, ["out", "n2"]),
        ],
        "n3",
        input_paths=["a.mp4", "b.mp4"],
        sources={"a": 0, "b": 1},
    )
    e = emit(g)
    assert e.filter_complex == (
        "[0:v]hflip[out_];[1:v]vflip[n2];[out_][n2]overlay=x=0:y=0[out]"
    )


# ---------------------------------------------------------------------------
# argument rendering
# ---------------------------------------------------------------------------


def test_bare_filter_renders_without_equals() -> None:
    g = _graph([Node("n1", "hflip", {}, ["src:a"])], "n1")
    assert emit(g).filter_complex == "[0:v]hflip[out]"


def test_expr_key_renders_value_only() -> None:
    g = _graph([Node("n1", "setpts", {"expr": "PTS/2"}, ["src:a"])], "n1")
    assert emit(g).filter_complex == "[0:v]setpts=PTS/2[out]"


def test_concat_renders_named_args() -> None:
    g = _graph(
        [Node("n1", "concat", {"n": 2, "v": 1, "a": 0}, ["src:a", "src:b"])],
        "n1",
        input_paths=["a.mp4", "b.mp4"],
        sources={"a": 0, "b": 1},
    )
    assert emit(g).filter_complex == "[0:v][1:v]concat=n=2:v=1:a=0[out]"


def test_args_render_in_insertion_order_not_sorted() -> None:
    g = _graph([Node("n1", "crop", {"w": 1, "h": 2, "x": 3, "y": 4}, ["src:a"])], "n1")
    assert emit(g).filter_complex == "[0:v]crop=w=1:h=2:x=3:y=4[out]"
    g2 = _graph([Node("n1", "crop", {"y": 4, "x": 3, "h": 2, "w": 1}, ["src:a"])], "n1")
    assert emit(g2).filter_complex == "[0:v]crop=y=4:x=3:h=2:w=1[out]"


def test_scalar_types_render() -> None:
    g = _graph(
        [Node("n1", "f", {"i": 3, "f": 0.5, "s": "auto", "b": True, "z": False}, ["src:a"])],
        "n1",
    )
    assert emit(g).filter_complex == "[0:v]f=i=3:f=0.5:s=auto:b=1:z=0[out]"


def test_unrenderable_arg_type_is_internal() -> None:
    g = _graph([Node("n1", "f", {"k": [1, 2]}, ["src:a"])], "n1")
    with pytest.raises(SqlmpegError) as excinfo:
        emit(g)
    assert excinfo.value.code is ErrorCode.INTERNAL


def test_drawtext_value_goes_through_the_escaper() -> None:
    g = _graph(
        [Node("n1", "drawtext", {"text": "12:30, take 'one'", "x": 10}, ["src:a"])],
        "n1",
    )
    assert emit(g).filter_complex == (
        r"[0:v]drawtext=text=12\\:30\,\ take\ \\\'one\\\':x=10[out]"
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
        [Node("n1", "hflip", {}, ["n2"]), Node("n2", "vflip", {}, ["n1"])],
        "n2",
    )
    err = _assert_internal(g)
    assert "topological" in err.message


def test_forward_reference_is_internal_error() -> None:
    g = _graph(
        [Node("n1", "vflip", {}, ["n2"]), Node("n2", "hflip", {}, ["src:a"])],
        "n1",
    )
    _assert_internal(g)


def test_unknown_node_ref_is_internal_error() -> None:
    g = _graph([Node("n1", "hflip", {}, ["nope"])], "n1")
    err = _assert_internal(g)
    assert "unknown node" in err.message


def test_unknown_source_alias_is_internal_error() -> None:
    g = _graph([Node("n1", "hflip", {}, ["src:zz"])], "n1")
    err = _assert_internal(g)
    assert "unknown source alias" in err.message


def test_pad_index_past_output_count_is_internal_error() -> None:
    g = _graph(
        [
            Node("s", "split", {"n": 2}, ["src:a"]),
            Node("n1", "hflip", {}, ["s:0"]),
            Node("n2", "vflip", {}, ["s:5"]),
            Node("n3", "overlay", {"x": 0, "y": 0}, ["n1", "n2"]),
        ],
        "n3",
    )
    _assert_internal(g)


def test_unsplit_fanout_is_internal_error() -> None:
    """emit refuses to render a pad consumed twice -- ffmpeg pads are consume-once."""
    g = _graph(
        [
            Node("n1", "hflip", {}, ["src:a"]),
            Node("n2", "gblur", {"sigma": 1}, ["n1"]),
            Node("n3", "overlay", {"x": 0, "y": 0}, ["n1", "n2"]),
        ],
        "n3",
    )
    err = _assert_internal(g)
    assert "consume-once" in err.message


def test_bad_split_arity_is_internal_error() -> None:
    g = _graph([Node("s", "split", {"n": 1}, ["src:a"]), Node("n1", "hflip", {}, ["s:0"])], "n1")
    _assert_internal(g)


def test_empty_output_ref_is_internal_error() -> None:
    _assert_internal(_graph([Node("n1", "hflip", {}, ["src:a"])], ""))


# ---------------------------------------------------------------------------
# build_ffmpeg_args
# ---------------------------------------------------------------------------


def test_build_ffmpeg_args_exact_list() -> None:
    g = _graph(
        [
            Node("n1", "crop", {"w": 600, "h": 200, "x": 1200, "y": 50}, ["src:b"]),
            Node("n2", "scale", {"w": "iw*0.5", "h": "-2"}, ["n1"]),
            Node("n3", "overlay", {"x": 20, "y": 20}, ["src:a", "n2"]),
        ],
        "n3",
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
        "[1:v]crop=w=600:h=200:x=1200:y=50,scale=w=iw*0.5:h=-2[n2];"
        "[0:v][n2]overlay=x=20:y=20[out]",
        "-map",
        "[out]",
        "-map",
        "0:a?",
        "-c:a",
        "copy",
        "out.mp4",
    ]


def test_build_ffmpeg_args_single_input() -> None:
    e = Emitted(inputs=["a.mp4"], filter_complex="[0:v]hflip[out]", output_label="out")
    assert build_ffmpeg_args(e, "/tmp/o.mkv") == [
        "ffmpeg",
        "-i",
        "a.mp4",
        "-filter_complex",
        "[0:v]hflip[out]",
        "-map",
        "[out]",
        "-map",
        "0:a?",
        "-c:a",
        "copy",
        "/tmp/o.mkv",
    ]
