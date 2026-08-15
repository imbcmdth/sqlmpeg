"""Tests for the lower pass and the compiler pipeline (plans 005, 019).

These go through the real parser: lowering is only ever handed a ``Resolved``
that ``resolve`` accepted, so hand-built inputs would test a shape that cannot
occur. ``compile_sql`` is used wherever the split pass is irrelevant or wanted;
``lower`` is called directly when a test needs the pre-split graph or a
synthetic :class:`~sqlmpeg.probe.ProbeResult`.

Paths in these queries deliberately do not exist, so ``compile_sql``'s
opportunistic probing degrades to symbolic lowering without shelling out
(RFC-001 "Probing policy"); probe-dependent behavior is exercised either with
a hand-built ``ProbeResult`` or, for the real thing, in an ``exec``-marked
test against ``tests/fixtures/av.mp4``.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from sqlmpeg import compiler
from sqlmpeg import lower as lower_module
from sqlmpeg.compiler import compile_sql
from sqlmpeg.emit import emit
from sqlmpeg.errors import ErrorCode, SqlmpegError
from sqlmpeg.ir import Graph
from sqlmpeg.lower import lower
from sqlmpeg.parser import parse, resolve
from sqlmpeg.probe import ProbeResult, StreamMeta

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"


def _readme_sql() -> str:
    """The first ```sql block in README.md -- the PiP example, verbatim."""
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```sql\n(.*?)```", text, re.DOTALL)
    assert blocks, "README.md no longer contains a ```sql block"
    return str(blocks[0])


def _lower(sql: str, probes: dict[str, ProbeResult | None] | None = None) -> Graph:
    return lower(resolve(parse(sql)), probes or {})


def _reject(sql: str) -> SqlmpegError:
    with pytest.raises(SqlmpegError) as excinfo:
        compile_sql(sql)
    err = excinfo.value
    assert err.line is not None, "every rejection must be line-anchored"
    assert err.col is not None
    return err


def _filters(g: Graph) -> list[str]:
    return [node.filter for node in g.nodes.values()]


def _outputs(g: Graph) -> list[tuple[str, str, str | None]]:
    return [(o.ref, o.type, o.name) for o in g.outputs]


def _probe_result(
    videos: int = 1,
    audios: int = 1,
    *,
    video_tags: dict[str, str] | None = None,
    audio_tags: dict[str, str] | None = None,
) -> ProbeResult:
    """A synthetic ProbeResult -- no ffprobe, no fixture, no disk."""
    streams = [
        StreamMeta(
            type="video",
            index=i,
            metadata=dict(video_tags or {}),
            width=320,
            height=240,
            fps="15/1",
            sample_rate=None,
        )
        for i in range(videos)
    ]
    streams += [
        StreamMeta(
            type="audio",
            index=i,
            metadata=dict(audio_tags or {}),
            width=None,
            height=None,
            fps=None,
            sample_rate=44100,
        )
        for i in range(audios)
    ]
    return ProbeResult(streams=streams)


# ---------------------------------------------------------------------------
# the README example (v0 compat: `frame` sugar still compiles)
# ---------------------------------------------------------------------------


def test_readme_example_lowers_to_expected_nodes() -> None:
    g = compile_sql(_readme_sql())
    assert g.to_dict() == {
        "inputs": ["game.mp4", "game.mp4"],
        # CTEs are traversed first, so the CTE's alias `b` takes input 0.
        "sources": {"b": 0, "a": 1},
        "nodes": [
            {
                "id": "n1",
                "filter": "crop",
                "args": {"w": 600, "h": 200, "x": 1200, "y": 50},
                "inputs": ["src:b:v:0"],
                "outputs": ["video"],
            },
            {
                "id": "n2",
                "filter": "scale",
                "args": {"w": "iw*0.5", "h": "-2"},
                "inputs": ["n1"],
                "outputs": ["video"],
            },
            {
                "id": "n3",
                "filter": "overlay",
                "args": {"x": 20, "y": 20},
                "inputs": ["src:a:v:0", "n2"],
                "outputs": ["video"],
            },
        ],
        "outputs": [{"ref": "n3", "type": "video", "name": None, "metadata": {}}],
    }


def test_readme_example_has_exactly_one_video_output() -> None:
    """`frame` sugar does NOT re-add v0's implicit audio stream."""
    g = compile_sql(_readme_sql())
    assert len(g.outputs) == 1
    assert g.outputs[0].type == "video"


def test_readme_example_emits_a_filtergraph() -> None:
    e = emit(compile_sql(_readme_sql()))
    assert "crop=" in e.filter_complex
    assert "overlay=" in e.filter_complex
    assert e.inputs == ["game.mp4", "game.mp4"]
    assert [m.target for m in e.maps] == ["[out0]"]
    assert [m.copy for m in e.maps] == [False]


def test_readme_example_scale_factor_is_not_a_decimal() -> None:
    """``Literal.to_py()`` yields Decimal for 0.5; the IR must carry float."""
    g = _lower("SELECT scale(a.frame, 0.5, 0.25) FROM input('x.mp4') a")
    args = g.nodes["n1"].args
    assert args == {"w": 0.5, "h": 0.25}
    assert all(type(v) is float for v in args.values())


# ---------------------------------------------------------------------------
# typed columns, subscripts, passthrough
# ---------------------------------------------------------------------------


def test_frame_sugar_is_the_first_video_stream() -> None:
    g = compile_sql("SELECT a.frame FROM input('x.mp4') a")
    assert g.nodes == {}
    assert _outputs(g) == [("src:a:v:0", "video", None)]
    assert g.sources == {"a": 0}


def test_frame_and_video_subscript_one_agree() -> None:
    assert _outputs(compile_sql("SELECT a.frame FROM input('x.mp4') a")) == _outputs(
        compile_sql("SELECT a.video[1] FROM input('x.mp4') a")
    )


def test_sql_subscripts_are_one_based() -> None:
    g = compile_sql("SELECT a.video[2], a.audio[3] FROM input('x.mp4') a")
    assert _outputs(g) == [
        ("src:a:v:1", "video", None),
        ("src:a:a:2", "audio", None),
    ]


def test_remap_only_query_is_pure_passthrough() -> None:
    """`SELECT a.video[1], a.audio[2]` -> two -map/-c copy streams, no filters."""
    g = compile_sql("SELECT a.video[1], a.audio[2] FROM input('foo.mp4') a")
    assert g.nodes == {}
    assert _outputs(g) == [
        ("src:a:v:0", "video", None),
        ("src:a:a:1", "audio", None),
    ]
    e = emit(g)
    assert e.filter_complex == ""
    assert [(m.target, m.type, m.copy) for m in e.maps] == [
        ("0:v:0", "video", True),
        ("0:a:1", "audio", True),
    ]


def test_filtered_video_with_passthrough_audio() -> None:
    g = compile_sql("SELECT scale(a.video[1], 0.5), a.audio[1] FROM input('foo.mp4') a")
    assert _filters(g) == ["scale"]
    assert _outputs(g) == [("n1", "video", None), ("src:a:a:0", "audio", None)]
    e = emit(g)
    assert [(m.target, m.copy) for m in e.maps] == [("[out0]", False), ("0:a:0", True)]


def test_select_alias_becomes_the_output_name() -> None:
    g = compile_sql(
        "SELECT a.video[1] AS picture, a.audio[1] AS Sound FROM input('x.mp4') a"
    )
    assert [o.name for o in g.outputs] == ["picture", "sound"]  # unquoted -> folded


def test_quoted_select_alias_keeps_its_case() -> None:
    g = compile_sql('SELECT a.audio[1] AS "Commentary" FROM input(\'x.mp4\') a')
    assert [o.name for o in g.outputs] == ["Commentary"]


def test_output_order_is_select_order() -> None:
    g = compile_sql("SELECT a.audio[1], a.video[1] FROM input('x.mp4') a")
    assert [o.type for o in g.outputs] == ["audio", "video"]


def test_time_column_outside_where_is_rejected() -> None:
    err = _reject("SELECT a.t FROM input('x.mp4') a")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "a.t" in err.message


def test_unknown_column_is_rejected() -> None:
    err = _reject("SELECT hflip(a.width) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "a.width" in err.message


def test_literal_projection_is_rejected() -> None:
    err = _reject("SELECT 1 FROM input('x.mp4') a")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "every SELECT column must be a stream expression" in err.message


def test_arithmetic_projection_is_rejected() -> None:
    err = _reject("SELECT 1 + 2 FROM input('x.mp4') a")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "every SELECT column must be a stream expression" in err.message


def test_frame_cannot_be_subscripted() -> None:
    err = _reject("SELECT a.frame[1] FROM input('x.mp4') a")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "a.frame" in err.message


# ---------------------------------------------------------------------------
# bare arrays: rejected until broadcasting (plan 020)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a.audio FROM input('x.mp4') a",
        "SELECT a.video FROM input('x.mp4') a",
        "SELECT volume(a.audio, 0.5) FROM input('x.mp4') a",
        "WITH c AS (SELECT a.audio FROM input('x.mp4') a) SELECT c.audio FROM c",
    ],
)
def test_bare_stream_array_says_broadcasting_is_coming(sql: str) -> None:
    err = _reject(sql)
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert err.hint is not None and "broadcasting" in err.hint


# ---------------------------------------------------------------------------
# WHERE -> typed trim
# ---------------------------------------------------------------------------


def test_where_between_prepends_trim_and_setpts_on_video() -> None:
    g = _lower("SELECT hflip(a.frame) FROM input('x.mp4') a WHERE a.t BETWEEN 1 AND 2.5")
    assert g.to_dict()["nodes"] == [
        {
            "id": "n1",
            "filter": "trim",
            "args": {"start": 1, "end": 2.5},
            "inputs": ["src:a:v:0"],
            "outputs": ["video"],
        },
        {
            "id": "n2",
            "filter": "setpts",
            "args": {"expr": "PTS-STARTPTS"},
            "inputs": ["n1"],
            "outputs": ["video"],
        },
        {"id": "n3", "filter": "hflip", "args": {}, "inputs": ["n2"], "outputs": ["video"]},
    ]
    assert _outputs(g) == [("n3", "video", None)]


def test_where_between_uses_atrim_and_asetpts_on_audio() -> None:
    g = _lower("SELECT a.audio[1] FROM input('x.mp4') a WHERE a.t BETWEEN 1 AND 2")
    assert g.to_dict()["nodes"] == [
        {
            "id": "n1",
            "filter": "atrim",
            "args": {"start": 1, "end": 2},
            "inputs": ["src:a:a:0"],
            "outputs": ["audio"],
        },
        {
            "id": "n2",
            "filter": "asetpts",
            "args": {"expr": "PTS-STARTPTS"},
            "inputs": ["n1"],
            "outputs": ["audio"],
        },
    ]
    assert _outputs(g) == [("n2", "audio", None)]


def test_one_predicate_trims_video_and_audio_in_sync() -> None:
    g = _lower(
        "SELECT a.video[1], a.audio[1] FROM input('x.mp4') a WHERE a.t BETWEEN 5 AND 10"
    )
    assert _filters(g) == ["trim", "setpts", "atrim", "asetpts"]
    assert g.nodes["n1"].inputs == ["src:a:v:0"]
    assert g.nodes["n3"].inputs == ["src:a:a:0"]
    assert _outputs(g) == [("n2", "video", None), ("n4", "audio", None)]


def test_trim_is_spliced_once_per_stream_and_shared() -> None:
    g = _lower(
        "SELECT overlay(a.frame, a.frame, 5, 5) FROM input('x.mp4') a "
        "WHERE a.t BETWEEN 0 AND 3"
    )
    assert _filters(g) == ["trim", "setpts", "overlay"]
    assert g.nodes["n3"].inputs == ["n2", "n2"]  # both arms, pre-split


def test_where_trims_only_the_named_alias() -> None:
    g = _lower(
        "SELECT overlay(a.frame, b.frame, 0, 0) "
        "FROM input('x.mp4') a, input('y.mp4') b WHERE b.t BETWEEN 2 AND 4"
    )
    assert g.nodes["n1"].inputs == ["src:b:v:0"]
    assert g.nodes["n3"].inputs == ["src:a:v:0", "n2"]


def test_two_between_clauses_trim_both_aliases() -> None:
    g = _lower(
        "SELECT overlay(a.frame, b.frame, 0, 0) FROM input('x.mp4') a, input('y.mp4') b "
        "WHERE a.t BETWEEN 0 AND 1 AND b.t BETWEEN 2 AND 3"
    )
    assert _filters(g) == ["trim", "setpts", "trim", "setpts", "overlay"]
    assert g.nodes["n5"].inputs == ["n2", "n4"]


def test_untouched_streams_of_a_trimmed_alias_cost_nothing() -> None:
    """Trims are lazy: an unconsumed audio stream never gets an atrim."""
    g = _lower("SELECT a.video[1] FROM input('x.mp4') a WHERE a.t BETWEEN 0 AND 1")
    assert _filters(g) == ["trim", "setpts"]


# ---------------------------------------------------------------------------
# CTEs
# ---------------------------------------------------------------------------


def test_unnamed_single_video_cte_column_is_reachable_as_frame() -> None:
    g = _lower(
        "WITH c AS (SELECT hflip(a.frame) FROM input('x.mp4') a) "
        "SELECT vflip(c.frame) FROM c"
    )
    assert _filters(g) == ["hflip", "vflip"]
    assert g.nodes["n2"].inputs == ["n1"]


def test_cte_columns_are_reachable_by_their_as_names() -> None:
    g = _lower(
        "WITH c AS ("
        "  SELECT hflip(a.video[1]) AS pic, volume(a.audio[1], 0.5) AS snd "
        "  FROM input('x.mp4') a"
        ") SELECT c.snd, vflip(c.pic) FROM c"
    )
    assert _filters(g) == ["hflip", "volume", "vflip"]
    assert _outputs(g) == [("n2", "audio", None), ("n3", "video", None)]


def test_unknown_cte_column_lists_the_known_names() -> None:
    err = _reject(
        "WITH c AS (SELECT hflip(a.video[1]) AS pic, a.audio[1] AS snd "
        "FROM input('x.mp4') a) SELECT c.nope FROM c"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "c.nope" in err.message
    assert err.hint is not None and "pic" in err.hint and "snd" in err.hint


def test_cte_column_cannot_be_subscripted_yet() -> None:
    err = _reject(
        "WITH c AS (SELECT a.audio[1] AS snd FROM input('x.mp4') a) "
        "SELECT c.snd[1] FROM c"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert err.hint is not None and "broadcasting" in err.hint


def test_where_trims_a_cte_column_by_its_type() -> None:
    g = _lower(
        "WITH c AS (SELECT a.audio[1] AS snd FROM input('x.mp4') a) "
        "SELECT c.snd FROM c WHERE c.t BETWEEN 1 AND 2"
    )
    assert _filters(g) == ["atrim", "asetpts"]
    assert g.nodes["n1"].inputs == ["src:a:a:0"]


def test_cte_union_all_gets_its_own_concat() -> None:
    g = _lower(
        "WITH u AS ("
        "  SELECT a.frame FROM input('x.mp4') a"
        "  UNION ALL SELECT b.frame FROM input('y.mp4') b"
        ") SELECT hflip(u.frame) FROM u"
    )
    assert _filters(g) == ["concat", "hflip"]
    assert g.nodes["n1"].inputs == ["src:a:v:0", "src:b:v:0"]
    assert g.nodes["n1"].outputs == ["video"]
    assert g.nodes["n2"].inputs == ["n1"]


# ---------------------------------------------------------------------------
# UNION ALL -> concat
# ---------------------------------------------------------------------------


def test_union_all_video_only_lowers_to_one_concat() -> None:
    g = compile_sql(
        "SELECT a.frame FROM input('x.mp4') a "
        "UNION ALL SELECT hflip(b.frame) FROM input('y.mp4') b "
        "UNION ALL SELECT c.frame FROM input('z.mp4') c"
    )
    concat = g.nodes["n2"]
    assert concat.filter == "concat"
    assert concat.args == {"n": 3, "v": 1, "a": 0}
    assert concat.inputs == ["src:a:v:0", "n1", "src:c:v:0"]
    assert concat.outputs == ["video"]
    assert _outputs(g) == [("n2", "video", None)]


def test_union_all_with_audio_interleaves_inputs_per_segment() -> None:
    g = compile_sql(
        "SELECT a.video[1], a.audio[1] FROM input('intro.mp4') a "
        "UNION ALL SELECT b.video[1], b.audio[1] FROM input('main.mp4') b"
    )
    concat = g.nodes["n1"]
    assert concat.args == {"n": 2, "v": 1, "a": 1}
    # per ffmpeg: [seg1 v][seg1 a][seg2 v][seg2 a]
    assert concat.inputs == ["src:a:v:0", "src:a:a:0", "src:b:v:0", "src:b:a:0"]
    assert concat.outputs == ["video", "audio"]
    assert _outputs(g) == [("n1:0", "video", None), ("n1:1", "audio", None)]


def test_concat_pads_map_back_to_the_branch_column_order() -> None:
    """Pads are videos-then-audios; the SELECT list keeps its own order."""
    g = compile_sql(
        "SELECT a.audio[1], a.video[1] FROM input('x.mp4') a "
        "UNION ALL SELECT b.audio[1], b.video[1] FROM input('y.mp4') b"
    )
    concat = g.nodes["n1"]
    assert concat.inputs == ["src:a:v:0", "src:a:a:0", "src:b:v:0", "src:b:a:0"]
    assert concat.outputs == ["video", "audio"]
    assert _outputs(g) == [("n1:1", "audio", None), ("n1:0", "video", None)]


def test_union_all_keeps_the_first_branch_column_names() -> None:
    g = compile_sql(
        "SELECT a.video[1] AS pic FROM input('x.mp4') a "
        "UNION ALL SELECT b.video[1] AS other FROM input('y.mp4') b"
    )
    assert [o.name for o in g.outputs] == ["pic"]


def test_union_all_type_mismatch_is_a_concat_mismatch() -> None:
    err = _reject(
        "SELECT a.video[1] FROM input('x.mp4') a\n"
        "UNION ALL SELECT b.audio[1] FROM input('y.mp4') b"
    )
    assert err.code is ErrorCode.CONCAT_MISMATCH
    assert err.line == 2, "the mismatching branch is the anchor"
    assert "branch 2" in err.message


def test_union_all_column_count_mismatch_is_a_concat_mismatch() -> None:
    err = _reject(
        "SELECT a.video[1], a.audio[1] FROM input('x.mp4') a\n"
        "UNION ALL SELECT b.video[1] FROM input('y.mp4') b"
    )
    assert err.code is ErrorCode.CONCAT_MISMATCH
    assert err.line == 2


def test_union_all_column_order_mismatch_is_a_concat_mismatch() -> None:
    err = _reject(
        "SELECT a.video[1], a.audio[1] FROM input('x.mp4') a\n"
        "UNION ALL SELECT b.audio[1], b.video[1] FROM input('y.mp4') b"
    )
    assert err.code is ErrorCode.CONCAT_MISMATCH


# ---------------------------------------------------------------------------
# function calls
# ---------------------------------------------------------------------------


def test_nested_calls_chain_bottom_up() -> None:
    g = _lower("SELECT blur(hflip(vflip(a.frame)), 4) FROM input('x.mp4') a")
    assert _filters(g) == ["vflip", "hflip", "gblur"]
    assert g.nodes["n1"].inputs == ["src:a:v:0"]
    assert g.nodes["n2"].inputs == ["n1"]
    assert g.nodes["n3"].inputs == ["n2"]
    assert _outputs(g) == [("n3", "video", None)]


def test_audio_calls_chain_bottom_up() -> None:
    g = _lower("SELECT volume(reverb(a.audio[1], 0.3), 0.5) FROM input('x.mp4') a")
    assert _filters(g) == ["aecho", "volume"]
    assert g.nodes["n1"].inputs == ["src:a:a:0"]
    assert g.nodes["n1"].outputs == ["audio"]
    assert _outputs(g) == [("n2", "audio", None)]


def test_function_lookup_is_case_insensitive() -> None:
    g = _lower("SELECT SCALE(a.frame, 0.5) FROM input('x.mp4') a")
    assert _filters(g) == ["scale"]


def test_macro_expands_to_several_nodes() -> None:
    g = _lower("SELECT blur_regions(a.frame, 10, 20, 30, 40, 8) FROM input('x.mp4') a")
    assert _filters(g) == ["crop", "gblur", "overlay"]
    assert list(g.nodes) == ["n1", "n2", "n3"]


def test_negative_numeric_literals_survive() -> None:
    g = _lower("SELECT scale(a.frame, -2, 720) FROM input('x.mp4') a")
    assert g.nodes["n1"].args == {"w": -2, "h": 720}


def test_string_literal_argument() -> None:
    g = _lower("SELECT draw_box(a.frame, 1, 2, 3, 4, 'red') FROM input('x.mp4') a")
    assert g.nodes["n1"].args["color"] == "red"


def test_unknown_function_suggests_a_close_match() -> None:
    err = _reject("SELECT scal(a.frame, 0.5) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UNKNOWN_FUNCTION
    assert "scal()" in err.message
    assert err.hint is not None and "scale()" in err.hint


def test_unknown_function_without_a_match_lists_the_stdlib() -> None:
    err = _reject("SELECT zzzz(a.frame) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UNKNOWN_FUNCTION
    assert err.hint is not None and "blur_regions" in err.hint


def test_unknown_nested_function_beats_the_outer_arity_check() -> None:
    err = _reject("SELECT blur(a.frame, nope(a.frame)) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UNKNOWN_FUNCTION
    assert "nope()" in err.message


def test_arity_error_lists_every_signature() -> None:
    err = _reject("SELECT scale(a.frame) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "scale(video, num)" in err.message
    assert "scale(video, num, num)" in err.message
    assert "got scale(video)" in err.message


def test_argument_kind_mismatch_is_typed() -> None:
    err = _reject("SELECT blur(a.frame, 'lots') FROM input('x.mp4') a")
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "got blur(video, str)" in err.message


def test_audio_stream_where_video_is_expected() -> None:
    err = _reject("SELECT hflip(a.audio[1]) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "hflip(video)" in err.message
    assert "got hflip(audio)" in err.message


def test_video_stream_where_audio_is_expected() -> None:
    err = _reject("SELECT amix(a.video[1], a.audio[1]) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "amix(audio, audio)" in err.message
    assert "got amix(video, audio)" in err.message


def test_video_result_where_audio_is_expected() -> None:
    """The kind of a nested call comes from its FuncSpec.returns."""
    err = _reject("SELECT volume(hflip(a.frame), 2) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "got volume(video, num)" in err.message


def test_stream_argument_where_a_number_is_expected() -> None:
    err = _reject("SELECT blur(a.frame, hflip(a.frame)) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "got blur(video, video)" in err.message


def test_non_literal_scalar_argument_is_rejected() -> None:
    err = _reject("SELECT blur(a.frame, 1 + 2) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "got blur(video, expr)" in err.message


def test_malformed_numeric_literal_is_a_typed_rejection() -> None:
    """sqlglot tokenizes `1e` as a number but ``to_py()`` raises on it."""
    err = _reject("SELECT blur(a.frame, 1e) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "1e" in err.message


def test_malformed_between_bound_is_a_typed_rejection() -> None:
    err = _reject("SELECT a.frame FROM input('x.mp4') a WHERE a.t BETWEEN 1e AND 2")
    assert err.code is ErrorCode.UNSUPPORTED_SQL


def test_overlay_keeps_its_four_positional_arguments() -> None:
    """Postgres has a builtin OVERLAY, so sqlglot hands lower named args."""
    g = _lower(
        "SELECT overlay(a.frame, b.frame, 20, 30) FROM input('x.mp4') a, input('y.mp4') b"
    )
    node = g.nodes["n1"]
    assert node.filter == "overlay"
    assert node.args == {"x": 20, "y": 30}
    assert node.inputs == ["src:a:v:0", "src:b:v:0"]


def test_overlay_arity_error_is_still_typed() -> None:
    err = _reject("SELECT overlay(a.frame, b.frame, 20) FROM input('x.mp4') a, input('y.mp4') b")
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "overlay(video, video, num, num)" in err.message


def test_a_colliding_builtin_is_an_unknown_function() -> None:
    err = _reject("SELECT trim(a.frame) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UNKNOWN_FUNCTION


# ---------------------------------------------------------------------------
# probing: bounds, provenance, symbolic fallback
# ---------------------------------------------------------------------------


def test_probed_subscript_out_of_range_is_stream_not_found() -> None:
    with pytest.raises(SqlmpegError) as excinfo:
        _lower(
            "SELECT a.audio[2] FROM input('x.mp4') a",
            {"a": _probe_result(videos=1, audios=1)},
        )
    err = excinfo.value
    assert err.code is ErrorCode.STREAM_NOT_FOUND
    assert err.line == 1 and err.col is not None
    assert "a.audio[2]" in err.message
    assert "x.mp4" in err.message


def test_probed_frame_sugar_is_bounds_checked_too() -> None:
    with pytest.raises(SqlmpegError) as excinfo:
        _lower("SELECT a.frame FROM input('x.mp4') a", {"a": _probe_result(videos=0)})
    assert excinfo.value.code is ErrorCode.STREAM_NOT_FOUND


def test_probed_subscript_in_range_lowers_normally() -> None:
    g = _lower(
        "SELECT a.audio[2] FROM input('x.mp4') a", {"a": _probe_result(audios=2)}
    )
    assert _outputs(g) == [("src:a:a:1", "audio", None)]


def test_unprobed_subscript_is_symbolic_and_out_of_range_is_fine() -> None:
    """No probe -> no bounds check; ffmpeg validates at runtime."""
    g = compile_sql("SELECT a.audio[9] FROM input('does-not-exist.mp4') a")
    assert _outputs(g) == [("src:a:a:8", "audio", None)]


def test_passthrough_output_copies_language_and_title() -> None:
    g = _lower(
        "SELECT a.audio[1] FROM input('x.mp4') a",
        {"a": _probe_result(audio_tags={"language": "fra", "title": "VF"})},
    )
    assert g.outputs[0].metadata == {"language": "fra", "title": "VF"}


def test_undefined_language_tag_is_not_copied() -> None:
    """mp4 muxers stamp language=und on untagged streams; it says nothing."""
    g = _lower(
        "SELECT a.audio[1] FROM input('x.mp4') a",
        {"a": _probe_result(audio_tags={"language": "und", "title": "Main"})},
    )
    assert g.outputs[0].metadata == {"title": "Main"}


def test_filtered_output_carries_no_metadata_in_this_plan() -> None:
    """1:1 provenance through a filter chain arrives with plan 020."""
    g = _lower(
        "SELECT volume(a.audio[1], 0.5) FROM input('x.mp4') a",
        {"a": _probe_result(audio_tags={"language": "fra"})},
    )
    assert g.outputs[0].metadata == {}


def test_unprobed_passthrough_has_no_metadata() -> None:
    g = compile_sql("SELECT a.audio[1] FROM input('x.mp4') a")
    assert g.outputs[0].metadata == {}


# ---------------------------------------------------------------------------
# compile_sql: probing policy
# ---------------------------------------------------------------------------


def test_compile_sql_probes_each_distinct_path_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def counting_probe(path: str) -> ProbeResult | None:
        calls.append(path)
        return None

    monkeypatch.setattr(compiler, "probe_path", counting_probe)
    compile_sql(
        "WITH pip AS (SELECT b.frame FROM input('game.mp4') b) "
        "SELECT overlay(a.frame, pip.frame, 0, 0) FROM input('game.mp4') a, pip"
    )
    assert calls == ["game.mp4"]  # two aliases, one file, one probe


def test_compile_sql_no_probe_skips_probing_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(path: str) -> ProbeResult | None:
        raise AssertionError("probe() must not be called with probe=False")

    monkeypatch.setattr(compiler, "probe_path", boom)
    g = compile_sql("SELECT a.audio[9] FROM input('x.mp4') a", probe=False)
    assert _outputs(g) == [("src:a:a:8", "audio", None)]


def test_compile_sql_uses_probe_results_for_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(compiler, "probe_path", lambda path: _probe_result(audios=1))
    err = _reject("SELECT a.audio[4] FROM input('x.mp4') a")
    assert err.code is ErrorCode.STREAM_NOT_FOUND


# ---------------------------------------------------------------------------
# node ids, the pipeline, the backstop
# ---------------------------------------------------------------------------


def test_node_ids_are_sequential_across_ctes_and_branches() -> None:
    g = _lower(
        "WITH c AS (SELECT hflip(a.frame) FROM input('x.mp4') a) "
        "SELECT vflip(c.frame) FROM c "
        "UNION ALL SELECT blur(b.frame, 2) FROM input('y.mp4') b"
    )
    assert list(g.nodes) == ["n1", "n2", "n3", "n4"]
    assert _filters(g) == ["hflip", "vflip", "gblur", "concat"]


def test_compile_sql_runs_the_split_pass() -> None:
    sql = "SELECT overlay(a.frame, a.frame, 5, 5) FROM input('x.mp4') a"
    assert "split" not in _filters(_lower(sql))
    assert "split" in _filters(compile_sql(sql))


def test_split_pass_picks_asplit_for_audio() -> None:
    g = compile_sql("SELECT amix(a.audio[1], a.audio[1]) FROM input('x.mp4') a")
    assert "asplit" in _filters(g)


def test_compile_sql_wraps_unexpected_exceptions_as_internal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(res: object, probes: object) -> Graph:
        raise ValueError("kaboom")

    monkeypatch.setattr(compiler, "lower", boom)
    with pytest.raises(SqlmpegError) as excinfo:
        compile_sql("SELECT a.frame FROM input('x.mp4') a")
    assert excinfo.value.code is ErrorCode.INTERNAL
    assert "kaboom" in excinfo.value.message


def test_lower_wraps_unexpected_exceptions_as_internal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise ValueError("kaboom")

    monkeypatch.setattr(lower_module._Lowerer, "run", boom)
    with pytest.raises(SqlmpegError) as excinfo:
        _lower("SELECT a.frame FROM input('x.mp4') a")
    assert excinfo.value.code is ErrorCode.INTERNAL


def test_pipeline_output_survives_a_round_trip_through_dicts() -> None:
    g = compile_sql(_readme_sql())
    assert Graph.from_dict(g.to_dict()).to_dict() == g.to_dict()


# ---------------------------------------------------------------------------
# real probing against a generated fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _av_fixture() -> str:
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "gen_fixtures.py")],
        check=True,
    )
    return (FIXTURES_DIR / "av.mp4").as_posix()


@pytest.mark.exec
def test_probed_fixture_rejects_an_out_of_range_subscript(_av_fixture: str) -> None:
    """av.mp4 has exactly one video and one audio stream."""
    err = _reject(f"SELECT a.video[2] FROM input('{_av_fixture}') a")
    assert err.code is ErrorCode.STREAM_NOT_FOUND
    assert "a.video[2]" in err.message


@pytest.mark.exec
def test_probed_fixture_accepts_its_real_streams(_av_fixture: str) -> None:
    g = compile_sql(f"SELECT a.video[1], a.audio[1] FROM input('{_av_fixture}') a")
    assert _outputs(g) == [
        ("src:a:v:0", "video", None),
        ("src:a:a:0", "audio", None),
    ]


@pytest.mark.exec
def test_no_probe_flag_skips_the_bounds_check_on_a_real_file(_av_fixture: str) -> None:
    g = compile_sql(f"SELECT a.audio[3] FROM input('{_av_fixture}') a", probe=False)
    assert _outputs(g) == [("src:a:a:2", "audio", None)]
