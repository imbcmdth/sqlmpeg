"""Tests for the lower pass and the compiler pipeline (plan 005).

These go through the real parser: lowering is only ever handed a ``Resolved``
that ``resolve`` accepted, so hand-built inputs would test a shape that cannot
occur. ``compile_sql`` is used wherever the split pass is irrelevant or wanted;
``lower`` is called directly when a test needs the pre-split graph.
"""

from __future__ import annotations

import re
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

REPO_ROOT = Path(__file__).resolve().parent.parent


def _readme_sql() -> str:
    """The first ```sql block in README.md -- the PiP example, verbatim."""
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```sql\n(.*?)```", text, re.DOTALL)
    assert blocks, "README.md no longer contains a ```sql block"
    return blocks[0]


def _lower(sql: str) -> Graph:
    return lower(resolve(parse(sql)))


def _reject(sql: str) -> SqlmpegError:
    with pytest.raises(SqlmpegError) as excinfo:
        compile_sql(sql)
    err = excinfo.value
    assert err.line is not None, "every rejection must be line-anchored"
    assert err.col is not None
    return err


def _filters(g: Graph) -> list[str]:
    return [node.filter for node in g.nodes.values()]


# ---------------------------------------------------------------------------
# the README example
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
                "inputs": ["src:b"],
            },
            {
                "id": "n2",
                "filter": "scale",
                "args": {"w": "iw*0.5", "h": "-2"},
                "inputs": ["n1"],
            },
            {
                "id": "n3",
                "filter": "overlay",
                "args": {"x": 20, "y": 20},
                "inputs": ["src:a", "n2"],
            },
        ],
        "output": "n3",
    }


def test_readme_example_emits_a_filtergraph() -> None:
    e = emit(compile_sql(_readme_sql()))
    assert "crop=" in e.filter_complex
    assert "overlay=" in e.filter_complex
    assert e.inputs == ["game.mp4", "game.mp4"]
    assert e.output_label == "out"


def test_readme_example_scale_factor_is_not_a_decimal() -> None:
    """``Literal.to_py()`` yields Decimal for 0.5; the IR must carry float."""
    g = _lower("SELECT scale(a.frame, 0.5, 0.25) FROM input('x.mp4') a")
    args = g.nodes["n1"].args
    assert args == {"w": 0.5, "h": 0.25}
    assert all(type(v) is float for v in args.values())


# ---------------------------------------------------------------------------
# columns and the trivial query
# ---------------------------------------------------------------------------


def test_bare_column_select_is_a_source_ref() -> None:
    g = compile_sql("SELECT a.frame FROM input('x.mp4') a")
    assert g.nodes == {}
    assert g.output == "src:a"
    assert g.sources == {"a": 0}


def test_top_level_alias_is_ignored() -> None:
    assert compile_sql("SELECT a.frame AS frame FROM input('x.mp4') a").output == "src:a"


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


# ---------------------------------------------------------------------------
# WHERE -> trim + setpts
# ---------------------------------------------------------------------------


def test_where_between_prepends_trim_and_setpts() -> None:
    g = _lower("SELECT hflip(a.frame) FROM input('x.mp4') a WHERE a.t BETWEEN 1 AND 2.5")
    assert g.to_dict()["nodes"] == [
        {"id": "n1", "filter": "trim", "args": {"start": 1, "end": 2.5}, "inputs": ["src:a"]},
        {"id": "n2", "filter": "setpts", "args": {"expr": "PTS-STARTPTS"}, "inputs": ["n1"]},
        {"id": "n3", "filter": "hflip", "args": {}, "inputs": ["n2"]},
    ]
    assert g.output == "n3"


def test_every_consumer_of_a_trimmed_alias_sees_the_trim() -> None:
    g = _lower(
        "SELECT overlay(a.frame, a.frame, 5, 5) FROM input('x.mp4') a "
        "WHERE a.t BETWEEN 0 AND 3"
    )
    assert g.nodes["n3"].inputs == ["n2", "n2"]  # both arms, pre-split
    assert _filters(g) == ["trim", "setpts", "overlay"]


def test_where_trims_only_the_named_alias() -> None:
    g = _lower(
        "SELECT overlay(a.frame, b.frame, 0, 0) "
        "FROM input('x.mp4') a, input('y.mp4') b WHERE b.t BETWEEN 2 AND 4"
    )
    assert g.nodes["n1"].inputs == ["src:b"]
    assert g.nodes["n3"].inputs == ["src:a", "n2"]


def test_two_between_clauses_trim_both_aliases() -> None:
    g = _lower(
        "SELECT overlay(a.frame, b.frame, 0, 0) FROM input('x.mp4') a, input('y.mp4') b "
        "WHERE a.t BETWEEN 0 AND 1 AND b.t BETWEEN 2 AND 3"
    )
    assert _filters(g) == ["trim", "setpts", "trim", "setpts", "overlay"]
    assert g.nodes["n5"].inputs == ["n2", "n4"]


# ---------------------------------------------------------------------------
# CTEs, UNION ALL
# ---------------------------------------------------------------------------


def test_cte_output_ref_is_reused_by_the_consumer() -> None:
    g = _lower(
        "WITH c AS (SELECT hflip(a.frame) FROM input('x.mp4') a) "
        "SELECT vflip(c.frame) FROM c"
    )
    assert _filters(g) == ["hflip", "vflip"]
    assert g.nodes["n2"].inputs == ["n1"]


def test_union_all_lowers_to_one_concat() -> None:
    g = compile_sql(
        "SELECT a.frame FROM input('x.mp4') a "
        "UNION ALL SELECT hflip(b.frame) FROM input('y.mp4') b "
        "UNION ALL SELECT c.frame FROM input('z.mp4') c"
    )
    concat = g.nodes[g.output]
    assert concat.filter == "concat"
    assert concat.args == {"n": 3, "v": 1, "a": 0}
    assert concat.inputs == ["src:a", "n1", "src:c"]


def test_cte_union_all_gets_its_own_concat() -> None:
    g = _lower(
        "WITH u AS ("
        "  SELECT a.frame FROM input('x.mp4') a"
        "  UNION ALL SELECT b.frame FROM input('y.mp4') b"
        ") SELECT hflip(u.frame) FROM u"
    )
    assert _filters(g) == ["concat", "hflip"]
    assert g.nodes["n1"].inputs == ["src:a", "src:b"]
    assert g.nodes["n2"].inputs == ["n1"]


# ---------------------------------------------------------------------------
# function calls
# ---------------------------------------------------------------------------


def test_nested_calls_chain_bottom_up() -> None:
    g = _lower("SELECT blur(hflip(vflip(a.frame)), 4) FROM input('x.mp4') a")
    assert _filters(g) == ["vflip", "hflip", "gblur"]
    assert g.nodes["n1"].inputs == ["src:a"]
    assert g.nodes["n2"].inputs == ["n1"]
    assert g.nodes["n3"].inputs == ["n2"]
    assert g.output == "n3"


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
    assert "scale(frame, num)" in err.message
    assert "scale(frame, num, num)" in err.message
    assert "got scale(frame)" in err.message


def test_argument_kind_mismatch_is_typed() -> None:
    err = _reject("SELECT blur(a.frame, 'lots') FROM input('x.mp4') a")
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "got blur(frame, str)" in err.message


def test_frame_argument_where_a_number_is_expected() -> None:
    err = _reject("SELECT blur(a.frame, hflip(a.frame)) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "got blur(frame, frame)" in err.message


def test_non_literal_scalar_argument_is_rejected() -> None:
    err = _reject("SELECT blur(a.frame, 1 + 2) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "got blur(frame, expr)" in err.message


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
    assert node.inputs == ["src:a", "src:b"]


def test_overlay_arity_error_is_still_typed() -> None:
    err = _reject("SELECT overlay(a.frame, b.frame, 20) FROM input('x.mp4') a, input('y.mp4') b")
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "overlay(frame, frame, num, num)" in err.message


def test_a_colliding_builtin_is_an_unknown_function() -> None:
    err = _reject("SELECT trim(a.frame) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UNKNOWN_FUNCTION


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


def test_compile_sql_wraps_unexpected_exceptions_as_internal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(res: object) -> Graph:
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
