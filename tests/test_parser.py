from __future__ import annotations

import pytest
from sqlglot import exp

from sqlmpeg.errors import ErrorCode, SqlmpegError
from sqlmpeg.parser import (
    RawInputOption,
    RawSink,
    RawSource,
    Resolved,
    from_entries,
    parse,
    resolve,
    star_qualifier,
    subscript_index,
    union_branches,
)

README_SQL = """WITH pip AS (
  SELECT scale(crop(b.frame, 1200, 50, 600, 200), 0.5) AS frame
  FROM input('game.mp4') b
)
SELECT overlay(a.frame, pip.frame, 20, 20)
FROM input('game.mp4') a, pip
"""

# The simplest query that resolves cleanly, for wrapping in a COPY sink.
SINK_QUERY = "SELECT a.frame FROM input('x.mp4') a"


def _resolve(sql: str) -> Resolved:
    return resolve(parse(sql))


def _reject(sql: str) -> SqlmpegError:
    with pytest.raises(SqlmpegError) as excinfo:
        _resolve(sql)
    err = excinfo.value
    assert err.line is not None, "every rejection must be line-anchored"
    assert err.col is not None
    return err


def _projection(sql: str, index: int = 0) -> exp.Expr:
    """The nth top-level projection of a query that resolves cleanly."""
    select = _resolve(sql).branches[0]
    projection = select.expressions[index]
    assert isinstance(projection, exp.Expr)
    return projection


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------


def test_parse_returns_select() -> None:
    tree = parse("SELECT a.frame FROM input('x.mp4') a")
    assert isinstance(tree, exp.Select)


def test_parse_returns_union_for_union_all() -> None:
    tree = parse("SELECT a.frame FROM input('x') a UNION ALL SELECT b.frame FROM input('y') b")
    assert isinstance(tree, exp.Union)


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_parse_empty_is_parse_error(text: str) -> None:
    with pytest.raises(SqlmpegError) as excinfo:
        parse(text)
    assert excinfo.value.code is ErrorCode.PARSE_ERROR
    assert excinfo.value.line == 1


@pytest.mark.parametrize(
    "text",
    [
        "SELEC 1",
        "SELECT FROM",
        "SELECT 'unterminated",
        "(((",
        "@@@ not sql @@@",
        "SELECT a.frame FROM input('x') a WHERE",
    ],
)
def test_parse_garbage_is_parse_error(text: str) -> None:
    with pytest.raises(SqlmpegError) as excinfo:
        parse(text)
    assert excinfo.value.code is ErrorCode.PARSE_ERROR
    assert excinfo.value.line is not None
    assert excinfo.value.col is not None


def test_parse_error_is_line_anchored() -> None:
    with pytest.raises(SqlmpegError) as excinfo:
        parse("SELECT a.frame\nFROM input('x') a\nWHERE a.t BETWEEN AND 2")
    assert excinfo.value.code is ErrorCode.PARSE_ERROR
    assert excinfo.value.line == 3


# ---------------------------------------------------------------------------
# resolve — happy paths
# ---------------------------------------------------------------------------


def test_single_input() -> None:
    res = _resolve("SELECT a.frame FROM input('clip.mp4') a")
    assert isinstance(res.select, exp.Select)
    assert res.input_paths == ["clip.mp4"]
    assert res.sources == {"a": 0}
    assert res.ctes == {}
    assert res.branches == [res.select]


def test_readme_example_maps_one_file_to_two_inputs() -> None:
    res = _resolve(README_SQL)
    # dedup is per ALIAS, not per path: two aliases -> two -i entries
    assert res.input_paths == ["game.mp4", "game.mp4"]
    assert res.sources == {"b": 0, "a": 1}
    assert list(res.ctes) == ["pip"]
    assert isinstance(res.ctes["pip"], exp.Select)
    assert len(res.branches) == 1


def test_two_aliases_same_file_without_cte() -> None:
    res = _resolve("SELECT overlay(a.frame, b.frame, 0, 0) FROM input('g.mp4') a, input('g.mp4') b")
    assert res.input_paths == ["g.mp4", "g.mp4"]
    assert res.sources == {"a": 0, "b": 1}


def test_cte_names_are_not_inputs() -> None:
    res = _resolve(
        "WITH c AS (SELECT hflip(a.frame) AS frame FROM input('x.mp4') a) SELECT c.frame FROM c"
    )
    assert res.input_paths == ["x.mp4"]
    assert res.sources == {"a": 0}
    assert "c" not in res.sources
    assert list(res.ctes) == ["c"]


def test_ctes_in_definition_order() -> None:
    sql = (
        "WITH one AS (SELECT a.frame FROM input('a.mp4') a), "
        "two AS (SELECT hflip(one.frame) AS frame FROM one) "
        "SELECT two.frame FROM two"
    )
    res = _resolve(sql)
    assert list(res.ctes) == ["one", "two"]
    assert res.sources == {"a": 0}


def test_union_all_branches() -> None:
    sql = (
        "SELECT a.frame FROM input('x.mp4') a "
        "UNION ALL SELECT b.frame FROM input('y.mp4') b "
        "UNION ALL SELECT c.frame FROM input('z.mp4') c"
    )
    res = _resolve(sql)
    assert isinstance(res.select, exp.Union)
    assert res.input_paths == ["x.mp4", "y.mp4", "z.mp4"]
    assert res.sources == {"a": 0, "b": 1, "c": 2}
    assert len(res.branches) == 3
    assert [b.expressions[0].table for b in res.branches] == ["a", "b", "c"]


def test_union_all_inside_cte() -> None:
    sql = (
        "WITH c AS ("
        "  SELECT a.frame FROM input('x.mp4') a "
        "  UNION ALL SELECT b.frame FROM input('y.mp4') b"
        ") SELECT c.frame FROM c"
    )
    res = _resolve(sql)
    assert list(res.ctes) == ["c"]
    assert isinstance(res.ctes["c"], exp.Union)
    assert len(union_branches(res.ctes["c"])) == 2
    assert res.sources == {"a": 0, "b": 1}


def test_where_between_is_accepted() -> None:
    res = _resolve("SELECT a.frame FROM input('x.mp4') a WHERE a.t BETWEEN 1 AND 2.5")
    assert res.sources == {"a": 0}
    where = res.select.args.get("where")
    assert isinstance(where, exp.Where)


def test_where_conjunction_per_alias_is_accepted() -> None:
    sql = (
        "SELECT overlay(a.frame, b.frame, 0, 0) FROM input('x.mp4') a, input('y.mp4') b "
        "WHERE a.t BETWEEN 0 AND 1 AND b.t BETWEEN 2 AND 3"
    )
    res = _resolve(sql)
    assert res.sources == {"a": 0, "b": 1}


# ---------------------------------------------------------------------------
# resolve — open-ended time windows
# ---------------------------------------------------------------------------


def test_where_gte_open_lower_bound_is_accepted() -> None:
    res = _resolve("SELECT a.frame FROM input('x.mp4') a WHERE a.t >= 5")
    assert res.sources == {"a": 0}


def test_where_lte_open_upper_bound_is_accepted() -> None:
    res = _resolve("SELECT a.frame FROM input('x.mp4') a WHERE a.t <= 60")
    assert res.sources == {"a": 0}


def test_where_flipped_gte_operand_order_is_accepted() -> None:
    """``120 <= a.t`` is the exact mirror of ``a.t >= 120``, same lower bound.

    VERIFIED (sqlglot 30.17, read="postgres"): operand order is NOT
    normalized at parse time, so this exercises the mirrored branch of
    ``parser._time_bounds`` for real, not the same code path as the
    unflipped form.
    """
    res = _resolve("SELECT a.frame FROM input('x.mp4') a WHERE 120 <= a.t")
    assert res.sources == {"a": 0}


def test_where_flipped_lte_operand_order_is_accepted() -> None:
    """``60 >= a.t`` is the exact mirror of ``a.t <= 60``, same upper bound."""
    res = _resolve("SELECT a.frame FROM input('x.mp4') a WHERE 60 >= a.t")
    assert res.sources == {"a": 0}


def test_where_gte_and_lte_merge_into_one_window() -> None:
    """``t >= 1 AND t <= 2`` is accepted exactly like ``t BETWEEN 1 AND 2``."""
    res = _resolve("SELECT a.frame FROM input('x.mp4') a WHERE a.t >= 1 AND a.t <= 2")
    assert res.sources == {"a": 0}


def test_where_open_bound_mixes_with_between_on_another_alias() -> None:
    sql = (
        "SELECT overlay(a.frame, b.frame, 0, 0) FROM input('x.mp4') a, input('y.mp4') b "
        "WHERE a.t >= 1 AND b.t BETWEEN 0 AND 5"
    )
    res = _resolve(sql)
    assert res.sources == {"a": 0, "b": 1}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a.frame FROM input('x.mp4') a WHERE a.t > 5",
        "SELECT a.frame FROM input('x.mp4') a WHERE 5 < a.t",
        "SELECT a.frame FROM input('x.mp4') a WHERE a.t < 60",
        "SELECT a.frame FROM input('x.mp4') a WHERE 60 > a.t",
    ],
)
def test_strict_inequality_is_rejected_with_dedicated_hint(sql: str) -> None:
    err = _reject(sql)
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert err.hint is not None
    assert "use >= / <=" in err.hint


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a.frame FROM input('x.mp4') a WHERE a.t >= 1 AND a.t >= 2",
        "SELECT a.frame FROM input('x.mp4') a WHERE a.t <= 1 AND a.t <= 2",
        "SELECT a.frame FROM input('x.mp4') a WHERE a.t BETWEEN 1 AND 2 AND a.t >= 3",
        "SELECT a.frame FROM input('x.mp4') a WHERE a.t >= 1 AND a.t BETWEEN 2 AND 3",
    ],
)
def test_a_second_bound_of_the_same_kind_is_rejected(sql: str) -> None:
    """Mirrors the old one-BETWEEN rule: at most one lower and one upper bound
    per alias, whichever forms supplied them."""
    err = _reject(sql)
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert err.hint is not None


def test_non_literal_inequality_bound_is_rejected() -> None:
    sql = "SELECT a.frame FROM input('x.mp4') a, input('y.mp4') b WHERE a.t >= b.t"
    err = _reject(sql)
    assert err.code is ErrorCode.UNSUPPORTED_SQL


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a.frame FROM input('x.mp4') a WHERE a.t >= 5 AND a.t <= 2",
        "SELECT a.frame FROM input('x.mp4') a WHERE a.t >= 5 AND a.t <= 5",
        "SELECT a.frame FROM input('x.mp4') a WHERE a.t BETWEEN 5 AND 2",
    ],
)
def test_empty_time_window_is_rejected_at_compile_time(sql: str) -> None:
    err = _reject(sql)
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert err.hint is not None


def test_alias_case_is_folded_like_postgres() -> None:
    res = _resolve("SELECT A.frame FROM input('x.mp4') A WHERE a.t BETWEEN 1 AND 2")
    assert res.sources == {"a": 0}


def test_parenthesized_union_branches() -> None:
    res = _resolve(
        "(SELECT a.frame FROM input('x.mp4') a) UNION ALL (SELECT b.frame FROM input('y.mp4') b)"
    )
    assert len(res.branches) == 2
    assert res.sources == {"a": 0, "b": 1}


def test_union_branches_helper_on_plain_select() -> None:
    res = _resolve("SELECT a.frame FROM input('x.mp4') a")
    assert union_branches(res.select) == [res.select]


# ---------------------------------------------------------------------------
# resolve — NO_STREAMING_EQUIVALENT
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a.frame FROM input('x') a GROUP BY a.frame",
        "SELECT a.frame FROM input('x') a HAVING count(*) > 1",
        "SELECT a.frame FROM input('x') a ORDER BY a.t",
        "SELECT a.frame FROM input('x') a LIMIT 1",
        "SELECT a.frame FROM input('x') a OFFSET 1",
        "SELECT DISTINCT a.frame FROM input('x') a",
        "SELECT row_number() OVER (ORDER BY a.t) FROM input('x') a",
        "SELECT count(a.frame) FROM input('x') a",
        "SELECT max(a.t) FROM input('x') a",
        "SELECT a.frame FROM input('x') a WHERE a.t IN (SELECT b.t FROM input('y') b)",
        "SELECT a.frame FROM input('x') a WHERE EXISTS (SELECT 1)",
    ],
)
def test_no_streaming_equivalent(sql: str) -> None:
    assert _reject(sql).code is ErrorCode.NO_STREAMING_EQUIVALENT


def test_union_without_all_suggests_union_all() -> None:
    err = _reject("SELECT a.frame FROM input('x') a UNION SELECT b.frame FROM input('y') b")
    assert err.code is ErrorCode.NO_STREAMING_EQUIVALENT
    assert err.hint is not None and "UNION ALL" in err.hint


# ---------------------------------------------------------------------------
# resolve — multiple projections (SELECT list = output stream list)
# ---------------------------------------------------------------------------


def test_two_output_columns_are_legal() -> None:
    res = _resolve("SELECT a.video[1], a.audio[1] FROM input('x') a")
    assert len(res.branches[0].expressions) == 2
    assert res.sources == {"a": 0}


def test_two_output_columns_in_cte_are_legal() -> None:
    res = _resolve(
        "WITH c AS (SELECT a.video[1] AS v, a.audio[1] AS aud FROM input('x') a) "
        "SELECT c.v, c.aud FROM c"
    )
    assert list(res.ctes) == ["c"]
    assert len(res.branches[0].expressions) == 2


def test_many_output_columns_are_legal() -> None:
    res = _resolve(
        "SELECT a.video[1], a.audio[1], a.audio[2], hflip(a.frame) FROM input('x') a"
    )
    assert len(res.branches[0].expressions) == 4


def test_multiple_projections_in_every_union_branch() -> None:
    sql = (
        "SELECT a.video[1], a.audio[1] FROM input('x') a "
        "UNION ALL SELECT b.video[1], b.audio[1] FROM input('y') b"
    )
    res = _resolve(sql)
    assert len(res.branches) == 2
    assert all(len(branch.expressions) == 2 for branch in res.branches)


def test_select_with_no_output_column_is_rejected() -> None:
    # sqlglot parses "SELECT FROM t" into a Select with an empty projection
    # list, so the no-projection guard is the one arity check that survives v2.
    err = _reject("SELECT FROM input('x') a")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "no output column" in err.message


# ---------------------------------------------------------------------------
# resolve — SELECT * / <alias>.*
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM input('x') a",
        "SELECT a.* FROM input('x') a",
        "SELECT a.*, b.audio[1] FROM input('x') a, input('y') b",
        "SELECT a.video[1], b.* FROM input('x') a, input('y') b",
        "SELECT *, a.audio[1] FROM input('x') a",
        'SELECT "A".* FROM input(\'x\') "A"',
        "WITH c AS (SELECT a.frame AS f FROM input('x') a) SELECT c.* FROM c",
        "WITH c AS (SELECT * FROM input('x') a) SELECT * FROM c",
        "COPY (SELECT * FROM input('x.mp4') a) TO 'out.mkv'",
    ],
)
def test_star_in_projection_position_is_accepted(sql: str) -> None:
    resolve(parse(sql))


@pytest.mark.parametrize(
    ("sql", "qualifier"),
    [
        ("SELECT * FROM input('x') a", ""),
        ("SELECT a.* FROM input('x') a", "a"),
        # Postgres identifier folding applies to the qualifier like any other.
        ("SELECT A.* FROM input('x') a", "a"),
        ('SELECT "A".* FROM input(\'x\') "A"', "A"),
    ],
)
def test_star_qualifier_reads_both_sqlglot_shapes(sql: str, qualifier: str) -> None:
    """The two VERIFIED shapes: bare ``exp.Star`` vs ``Column(this=Star())``."""
    select = resolve(parse(sql)).branches[0]
    assert star_qualifier(select.expressions[0]) == qualifier


@pytest.mark.parametrize(
    "sql",
    [
        # not a projection: a star inside a function call
        "SELECT scale(a.*, 0.5) FROM input('x') a",
        "SELECT scale(*, 0.5) FROM input('x') a",
        # not a projection: subscripted, or aliased
        "SELECT a.*[1] FROM input('x') a",
        "SELECT * AS everything FROM input('x') a",
        "SELECT a.* AS everything FROM input('x') a",
    ],
)
def test_star_outside_projection_position_is_rejected(sql: str) -> None:
    err = _reject(sql)
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "whole SELECT column" in err.message
    assert err.hint is not None and "<alias>.*" in err.hint


def test_star_over_an_unknown_alias_is_rejected() -> None:
    err = _reject("SELECT z.* FROM input('x') a")
    assert err.code is ErrorCode.UNKNOWN_ALIAS
    assert "'z'" in err.message


def test_star_projection_is_column_anchored() -> None:
    err = _reject("SELECT\n  z.*\nFROM input('x') a")
    assert err.code is ErrorCode.UNKNOWN_ALIAS
    assert err.line == 2


def test_star_in_where_is_still_rejected() -> None:
    err = _reject("SELECT a.frame FROM input('x') a WHERE * BETWEEN 1 AND 2")
    assert err.code is ErrorCode.UNSUPPORTED_SQL


def test_count_star_is_still_an_aggregate_rejection() -> None:
    """``count(*)`` must not be mistaken for a star projection."""
    err = _reject("SELECT count(*) FROM input('x') a")
    assert err.code is ErrorCode.NO_STREAMING_EQUIVALENT


def test_count_star_over_track_rows_is_still_an_aggregate_rejection() -> None:
    """Table mode makes metadata columns legal SELECT
    outputs, but it does not make sqlmpeg a database -- ``COUNT(*)`` over a
    row table (a bare SELECT, unconditionally table-capable) is still rejected
    exactly like any other aggregate."""
    err = _reject("SELECT count(*) FROM input('f.mkv') f, unnest(f.audio) t")
    assert err.code is ErrorCode.NO_STREAMING_EQUIVALENT


# ---------------------------------------------------------------------------
# resolve — subtitle / data pseudo-columns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a.subtitle FROM input('x') a",
        "SELECT a.subtitle[1] FROM input('x') a",
        "SELECT a.data FROM input('x') a",
        "SELECT a.data[2] FROM input('x') a",
        "SELECT a.video[1], a.audio[1], a.subtitle[1], a.data[1] FROM input('x') a",
    ],
)
def test_subtitle_and_data_columns_are_accepted(sql: str) -> None:
    resolve(parse(sql))


# ---------------------------------------------------------------------------
# resolve — stream columns and subscripts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a.frame FROM input('x') a",
        "SELECT a.video FROM input('x') a",
        "SELECT a.audio FROM input('x') a",
        "SELECT a.video[1] FROM input('x') a",
        "SELECT a.video[3] FROM input('x') a",
        "SELECT a.audio[2] FROM input('x') a",
        "SELECT a.video [ 1 ] FROM input('x') a",
        "SELECT a.video\n  [1] FROM input('x') a",
        "SELECT a.video[10] FROM input('x') a",
        "SELECT scale(a.video[1], 0.5) FROM input('x') a",
        "SELECT scale(a.video, 0.5) FROM input('x') a",
        "SELECT A.VIDEO[1] FROM input('x') A",
        "SELECT a.video[1] AS v FROM input('x') a",
    ],
)
def test_stream_columns_are_accepted(sql: str) -> None:
    assert _resolve(sql).sources == {"a": 0}


def test_cte_columns_may_have_any_name_and_be_subscripted() -> None:
    # A CTE's columns are named by AS, so the parser must not apply the input
    # pseudo-column whitelist to them — lower validates the name.
    res = _resolve(
        "WITH c AS (SELECT a.audio AS tracks FROM input('x') a) "
        "SELECT c.tracks[2], c.tracks FROM c"
    )
    assert list(res.ctes) == ["c"]
    assert res.sources == {"a": 0}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a.bogus FROM input('x') a",
        "SELECT a.captions[1] FROM input('x') a",
        "SELECT hflip(a.frames) FROM input('x') a",
        'SELECT a."Video" FROM input(\'x\') a',
    ],
)
def test_unknown_input_column_is_rejected(sql: str) -> None:
    err = _reject(sql)
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert err.hint is not None and "video" in err.hint


def test_unknown_input_column_is_line_anchored() -> None:
    err = _reject("SELECT\n  a.bogus\nFROM input('x') a")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert err.line == 2


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a.video[0] FROM input('x') a",
        "SELECT a.video[-1] FROM input('x') a",
        "SELECT a.video[-10] FROM input('x') a",
        "SELECT a.video[1.5] FROM input('x') a",
        "SELECT a.video['x'] FROM input('x') a",
        "SELECT a.video[x] FROM input('x') a",
        "SELECT a.video[a.t] FROM input('x') a",
        "SELECT a.video[null] FROM input('x') a",
        "SELECT a.video[true] FROM input('x') a",
        "SELECT a.video[1:2] FROM input('x') a",
        "SELECT a.video[1, 2] FROM input('x') a",
        "SELECT a.video[cast(1 AS INT)] FROM input('x') a",
        "SELECT scale(a.video[0], 0.5) FROM input('x') a",
    ],
)
def test_bad_subscript_is_rejected(sql: str) -> None:
    err = _reject(sql)
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert err.hint is not None and "stream subscripts are 1-based" in err.hint


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a.video[1][2] FROM input('x') a",
        "SELECT a.video[1][1][1] FROM input('x') a",
    ],
)
def test_chained_subscript_is_rejected(sql: str) -> None:
    err = _reject(sql)
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "chained" in err.message


def test_subscripting_a_function_result_is_rejected() -> None:
    err = _reject("SELECT scale(a.video[1], 0.5)[1] FROM input('x') a")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "stream columns" in err.message


def test_bad_subscript_is_line_anchored() -> None:
    err = _reject("SELECT\n  a.video[0]\nFROM input('x') a")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert err.line == 2


def test_subscript_on_unknown_alias_is_unknown_alias() -> None:
    assert _reject("SELECT z.video[1] FROM input('x') a").code is ErrorCode.UNKNOWN_ALIAS


# ---------------------------------------------------------------------------
# subscript_index — sqlglot rebases subscripts at parse time (INDEX_OFFSET)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql_index", "ast_literal"),
    [(1, "0"), (2, "1"), (3, "2"), (10, "9")],
)
def test_sqlglot_rebases_the_subscript_at_parse_time(sql_index: int, ast_literal: str) -> None:
    # Guards the assumption subscript_index is built on: read="postgres" has
    # INDEX_OFFSET = 1, so the parser stores <written index> - 1 and the
    # generator adds it back. If sqlglot ever stops doing this, this test fails
    # loudly instead of every subscript silently compiling off by one.
    bracket = _projection(f"SELECT a.video[{sql_index}] FROM input('x') a")
    assert isinstance(bracket, exp.Bracket)
    literal = bracket.expressions[0]
    assert isinstance(literal, exp.Literal)
    assert literal.this == ast_literal
    assert bracket.sql(dialect="postgres") == f"a.video[{sql_index}]"


@pytest.mark.parametrize("sql_index", [1, 2, 7, 128])
def test_subscript_index_undoes_the_rebase(sql_index: int) -> None:
    bracket = _projection(f"SELECT a.audio[{sql_index}] FROM input('x') a")
    assert isinstance(bracket, exp.Bracket)
    assert subscript_index(bracket) == sql_index


def test_subscript_index_of_a_bare_column_is_not_applicable() -> None:
    # Bare a.video is a Column, never a Bracket: callers check the node type.
    assert isinstance(_projection("SELECT a.video FROM input('x') a"), exp.Column)


def test_subscript_index_rejects_non_literals() -> None:
    bracket = parse("SELECT a.video[x] FROM input('x') a").expressions[0]
    assert isinstance(bracket, exp.Bracket)
    assert subscript_index(bracket) is None


def test_constant_folded_subscript_is_accepted_as_its_value() -> None:
    # KNOWN sqlglot behaviour: it simplifies the rebased index, so a.audio[1+1]
    # is indistinguishable from a.audio[2] by the time the parser sees it.
    # Documented rather than worked around — the result is still a valid stream.
    bracket = _projection("SELECT a.audio[1 + 1] FROM input('x') a")
    assert isinstance(bracket, exp.Bracket)
    assert subscript_index(bracket) == 2


# ---------------------------------------------------------------------------
# resolve — UNKNOWN_ALIAS
# ---------------------------------------------------------------------------


def test_unknown_alias_in_select() -> None:
    assert _reject("SELECT z.frame FROM input('x') a").code is ErrorCode.UNKNOWN_ALIAS


def test_unknown_alias_in_where() -> None:
    sql = "SELECT a.frame FROM input('x') a WHERE z.t BETWEEN 1 AND 2"
    assert _reject(sql).code is ErrorCode.UNKNOWN_ALIAS


def test_unknown_table_in_from() -> None:
    assert _reject("SELECT c.frame FROM c").code is ErrorCode.UNKNOWN_ALIAS


def test_cte_cannot_forward_reference() -> None:
    sql = (
        "WITH c AS (SELECT d.frame FROM d), "
        "d AS (SELECT a.frame FROM input('x') a) SELECT c.frame FROM c"
    )
    assert _reject(sql).code is ErrorCode.UNKNOWN_ALIAS


def test_unknown_alias_is_line_anchored() -> None:
    sql = "SELECT\n  z.frame\nFROM input('x') a"
    err = _reject(sql)
    assert err.code is ErrorCode.UNKNOWN_ALIAS
    assert err.line == 2


# ---------------------------------------------------------------------------
# resolve — UNSUPPORTED_SQL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a.frame FROM input('x') a JOIN input('y') b ON a.t = b.t",
        "SELECT a.frame FROM input('x') a INNER JOIN input('y') b ON a.t = b.t",
        "SELECT a.frame FROM input('x') a LEFT JOIN input('y') b ON a.t = b.t",
        "SELECT a.frame FROM input('x') a CROSS JOIN input('y') b",
    ],
)
def test_explicit_join_syntax(sql: str) -> None:
    err = _reject(sql)
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert err.hint is not None and "comma" in err.hint


def test_input_without_alias() -> None:
    err = _reject("SELECT frame FROM input('x')")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert err.hint is not None and "alias" in err.hint


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a.frame FROM input() a",
        "SELECT a.frame FROM input('x', 'y') a",
        "SELECT a.frame FROM input(1) a",
    ],
)
def test_input_requires_one_string_literal(sql: str) -> None:
    assert _reject(sql).code is ErrorCode.UNSUPPORTED_SQL


def test_unknown_table_function() -> None:
    assert _reject("SELECT a.frame FROM stream('x') a").code is ErrorCode.UNSUPPORTED_SQL


# ---------------------------------------------------------------------------
# input() named options
# ---------------------------------------------------------------------------


def test_input_with_no_named_options_has_no_input_options_entry() -> None:
    res = _resolve("SELECT a.frame FROM input('x.mp4') a")
    assert res.input_options == {}


def test_input_named_option_is_collected() -> None:
    res = _resolve("SELECT a.frame FROM input('x.png', loop => true) a")
    assert list(res.input_options) == ["a"]
    options = res.input_options["a"]
    assert len(options) == 1
    option = options[0]
    assert isinstance(option, RawInputOption)
    assert option.name == "loop"
    assert isinstance(option.value, exp.Boolean)


def test_input_multiple_named_options_keep_written_order() -> None:
    res = _resolve(
        "SELECT a.frame FROM input('x.png', loop => true, framerate => 15) a"
    )
    assert [o.name for o in res.input_options["a"]] == ["loop", "framerate"]


def test_input_option_name_is_verbatim_not_folded() -> None:
    """Unlike a sink option, input options reuse Kwarg's case-sensitive name."""
    res = _resolve("SELECT a.frame FROM input('x.mp4', HwAccel => 'cuda') a")
    assert res.input_options["a"][0].name == "HwAccel"


def test_two_input_aliases_keep_separate_option_sets() -> None:
    res = _resolve(
        "SELECT a.frame, b.frame FROM input('x.png', loop => true) a, "
        "input('y.mp4', hwaccel => 'cuda') b"
    )
    assert [o.name for o in res.input_options["a"]] == ["loop"]
    assert [o.name for o in res.input_options["b"]] == ["hwaccel"]


def test_input_duplicate_named_option_is_rejected() -> None:
    err = _reject(
        "SELECT a.frame FROM input('x.png', loop => true, loop => false) a"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL


def test_input_positional_after_named_option_is_rejected() -> None:
    err = _reject("SELECT a.frame FROM input('x.png', loop => true, 5) a")
    assert err.code is ErrorCode.UNSUPPORTED_SQL


def test_input_second_positional_before_any_named_option_is_rejected() -> None:
    err = _reject("SELECT a.frame FROM input('x.png', 5, loop => true) a")
    assert err.code is ErrorCode.UNSUPPORTED_SQL


def test_input_named_option_with_no_value_is_rejected() -> None:
    # sqlglot itself rejects a valueless `=>` -- never reaches the resolver.
    err = _reject("SELECT a.frame FROM input('x.png', loop =>) a")
    assert err.code is ErrorCode.PARSE_ERROR


# ---------------------------------------------------------------------------
# FROM ffmpeg.<source>(...) alias
# ---------------------------------------------------------------------------
#
# Shape only: which sources exist and which options they take is the installed
# ffmpeg's business (tests/test_lower.py, against a fixture registry).
#
# The sqlglot 30.17 shape this pass keys on -- MEASURED, and different from
# the same namespace in CALL position, which is an `exp.Dot`:
# `FROM ffmpeg.testsrc(duration => 2) t` is
# `Table(this=Anonymous(testsrc, [Kwarg]), db=Identifier(ffmpeg),
#  alias=TableAlias(t))`. `test_a_source_parses_as_a_table_with_a_db_qualifier`
# pins it so a sqlglot upgrade that moves the qualifier cannot pass silently.


def test_a_source_parses_as_a_table_with_a_db_qualifier() -> None:
    table = _resolve("SELECT t.frame FROM ffmpeg.testsrc(duration => 2) t").branches[
        0
    ].args["from_"].this
    assert isinstance(table, exp.Table)
    assert isinstance(table.this, exp.Anonymous)
    assert str(table.this.this) == "testsrc"
    db = table.args.get("db")
    assert isinstance(db, exp.Identifier) and db.name == "ffmpeg"
    assert not isinstance(table.this, exp.Dot)


def test_source_is_collected_with_its_raw_options() -> None:
    res = _resolve(
        "SELECT t.frame FROM ffmpeg.testsrc(duration => 2, size => '320x240') t"
    )
    assert list(res.source_filters) == ["t"]
    raw = res.source_filters["t"]
    assert isinstance(raw, RawSource)
    assert raw.alias == "t"
    assert raw.name == "testsrc"
    assert [o.name for o in raw.options] == ["duration", "size"]
    assert isinstance(raw.options[0].value, exp.Literal)


def test_a_source_takes_no_input_index() -> None:
    """No `-i`: a source is a zero-input filter, so it is in neither table."""
    res = _resolve("SELECT t.frame FROM ffmpeg.testsrc(duration => 2) t")
    assert res.input_paths == []
    assert res.sources == {}


def test_a_source_with_empty_parens_is_accepted() -> None:
    res = _resolve("SELECT t.frame FROM ffmpeg.testsrc() t")
    assert res.source_filters["t"].options == ()


def test_a_source_without_parens_is_rejected_with_a_hint() -> None:
    err = _reject("SELECT t.frame FROM ffmpeg.testsrc t")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "'ffmpeg.testsrc' is not a table" in err.message
    assert err.hint is not None and "is a CALL" in err.hint


def test_a_source_requires_an_alias() -> None:
    err = _reject("SELECT t.frame FROM ffmpeg.testsrc(duration => 2)")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "ffmpeg.testsrc() requires an alias" in err.message


def test_a_source_takes_no_positional_arguments() -> None:
    for sql in (
        "SELECT t.frame FROM ffmpeg.testsrc(2) t",
        "SELECT t.frame FROM ffmpeg.testsrc(2, duration => 1) t",
        "SELECT t.frame FROM ffmpeg.testsrc(duration => 1, 2) t",
    ):
        err = _reject(sql)
        assert err.code is ErrorCode.UNSUPPORTED_SQL, sql
        assert "no stream inputs" in err.message, sql


def test_a_source_rejects_a_duplicate_option() -> None:
    err = _reject("SELECT t.frame FROM ffmpeg.testsrc(duration => 1, duration => 2) t")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "duplicate named argument 'duration'" in err.message


def test_a_source_option_name_is_verbatim_and_its_own_name_is_folded() -> None:
    """Function names are case-insensitive; ffmpeg AVOption names are not."""
    res = _resolve("SELECT t.frame FROM FFMPEG.TestSrc(Duration => 2) t")
    raw = res.source_filters["t"]
    assert raw.name == "testsrc"
    assert raw.options[0].name == "Duration"


def test_a_source_alias_may_use_as() -> None:
    res = _resolve("SELECT t.frame FROM ffmpeg.testsrc(duration => 2) AS t")
    assert list(res.source_filters) == ["t"]


def test_a_source_joins_an_input_with_a_comma() -> None:
    res = _resolve(
        "SELECT f.video[1], s.audio[1] FROM input('a.mp4') f, "
        "ffmpeg.anullsrc(duration => 3) s"
    )
    assert res.input_paths == ["a.mp4"]
    assert res.sources == {"f": 0}
    assert list(res.source_filters) == ["s"]


def test_a_source_is_legal_in_a_cte_body_and_a_union_branch() -> None:
    res = _resolve(
        "WITH bg AS (SELECT t.frame AS v FROM ffmpeg.testsrc(duration => 2) t) "
        "SELECT bg.v FROM bg"
    )
    assert list(res.source_filters) == ["t"]
    res = _resolve(
        "SELECT f.frame FROM input('a.mp4') f UNION ALL "
        "SELECT t.frame FROM ffmpeg.testsrc(duration => 2) t"
    )
    assert list(res.source_filters) == ["t"]


def test_a_source_alias_is_unique_across_the_whole_query() -> None:
    for sql in (
        "SELECT t.frame FROM ffmpeg.testsrc() t, ffmpeg.testsrc() t",
        "SELECT t.frame FROM input('x.mp4') t, ffmpeg.testsrc() t",
        "SELECT t.frame FROM ffmpeg.testsrc() t, input('x.mp4') t",
    ):
        err = _reject(sql)
        assert err.code is ErrorCode.UNSUPPORTED_SQL, sql
        assert "duplicate name 't'" in err.message, sql


def test_a_source_may_not_be_aliased_ffmpeg() -> None:
    """The namespace name stays reserved in FROM position too."""
    err = _reject("SELECT ffmpeg.frame FROM ffmpeg.testsrc(duration => 2) ffmpeg")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "'ffmpeg' is reserved for the filter namespace" in err.message


def test_a_three_part_name_is_not_the_namespace() -> None:
    err = _reject("SELECT t.frame FROM x.ffmpeg.testsrc(duration => 2) t")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "qualified table names are not supported" in err.message


def test_a_non_namespace_qualifier_is_still_rejected() -> None:
    err = _reject("SELECT a.frame FROM public.clips a")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "qualified table names are not supported" in err.message


def test_a_bare_source_name_in_from_is_not_a_table_function() -> None:
    """The namespace is mandatory (`random` etc. collide bare)."""
    err = _reject("SELECT t.frame FROM testsrc(duration => 2) t")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "unsupported table function testsrc()" in err.message


def test_a_source_column_is_not_whitelisted_by_the_parser() -> None:
    """Which columns a source exposes depends on its output pad TYPE, which
    only the registry knows -- so the resolver checks the alias and leaves the
    column name to lower (unlike an input's fixed pseudo-column set)."""
    res = _resolve("SELECT t.audio[1] FROM ffmpeg.testsrc(duration => 2) t")
    assert list(res.source_filters) == ["t"]


def test_a_source_rejection_is_line_anchored() -> None:
    err = _reject("SELECT t.frame\nFROM ffmpeg.testsrc(2) t")
    assert err.line == 2


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a.frame FROM input('x') a WHERE a.t = 1",
        "SELECT a.frame FROM input('x') a WHERE a.t > 1",
        "SELECT a.frame FROM input('x') a WHERE a.t BETWEEN 1 AND 2 OR a.t BETWEEN 3 AND 4",
        "SELECT a.frame FROM input('x') a WHERE NOT a.t BETWEEN 1 AND 2",
        "SELECT a.frame FROM input('x') a WHERE a.x BETWEEN 1 AND 2",
        "SELECT a.frame FROM input('x') a WHERE a.t BETWEEN 1 AND 'two'",
        "SELECT a.frame FROM input('x') a WHERE t BETWEEN 1 AND 2",
        "SELECT a.frame FROM input('x') a WHERE a.t BETWEEN 1 AND 2 AND a.t BETWEEN 3 AND 4",
    ],
)
def test_unsupported_where_forms(sql: str) -> None:
    err = _reject(sql)
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert err.hint is not None


def test_duplicate_cte_name() -> None:
    sql = (
        "WITH c AS (SELECT a.frame FROM input('x') a), "
        "c AS (SELECT b.frame FROM input('y') b) SELECT c.frame FROM c"
    )
    assert _reject(sql).code is ErrorCode.UNSUPPORTED_SQL


def test_duplicate_alias() -> None:
    assert (
        _reject("SELECT a.frame FROM input('x') a, input('y') a").code
        is ErrorCode.UNSUPPORTED_SQL
    )


def test_alias_colliding_with_cte_name() -> None:
    sql = "WITH c AS (SELECT a.frame FROM input('x') a) SELECT c.frame FROM input('y') c, c"
    assert _reject(sql).code is ErrorCode.UNSUPPORTED_SQL


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET x = 1",
        "DELETE FROM t",
        "CREATE TABLE t (x INT)",
        "SELECT 1; SELECT 2",
        "SELECT 1",
        "SELECT a.frame FROM (SELECT b.frame FROM input('x') b) a",
        "SELECT a.frame FROM input('x') a EXCEPT SELECT b.frame FROM input('y') b",
        "SELECT a.frame FROM input('x') a INTERSECT SELECT b.frame FROM input('y') b",
        "WITH RECURSIVE c AS (SELECT a.frame FROM input('x') a) SELECT c.frame FROM c",
        "WITH c AS (WITH d AS (SELECT a.frame FROM input('x') a) SELECT d.frame FROM d) "
        "SELECT c.frame FROM c",
        "SELECT a.frame FROM input('x') a(c)",
        "SELECT frame FROM input('x') a",
        "SELECT a.frame FROM sch.tbl a",
    ],
)
def test_outside_the_surface(sql: str) -> None:
    assert _reject(sql).code is ErrorCode.UNSUPPORTED_SQL


def test_cte_body_must_be_a_select() -> None:
    err = _reject("WITH c AS (INSERT INTO t VALUES (1)) SELECT c.frame FROM c")
    assert err.code is ErrorCode.UNSUPPORTED_SQL


# ---------------------------------------------------------------------------
# named arguments — SHAPE only
# ---------------------------------------------------------------------------
#
# Which options exist, and what they accept, is a property of the installed
# ffmpeg and belongs to lower + the registry (tests/test_lower.py). Resolve
# only knows that a named argument trails the positional ones and appears once.


def test_named_arguments_resolve() -> None:
    """The resolver accepts them structurally: `sigma` is not an alias, not a
    column, and not checked against anything here."""
    projection = _projection("SELECT gblur(a.frame, sigma => 5) FROM input('x.mp4') a")
    assert isinstance(projection, exp.Anonymous)
    kwarg = projection.expressions[1]
    assert isinstance(kwarg, exp.Kwarg)
    assert kwarg.this.name == "sigma"


def test_named_argument_names_keep_their_case() -> None:
    """Unquoted identifiers fold lowercase in Postgres, but an option name is
    an ffmpeg AVOption, not an identifier: gblur's sigmaV must survive."""
    from sqlmpeg.parser import kwarg_name

    projection = _projection("SELECT gblur(a.frame, sigmaV => 5) FROM input('x.mp4') a")
    assert isinstance(projection, exp.Anonymous)
    kwarg = projection.expressions[1]
    assert isinstance(kwarg, exp.Kwarg)
    assert kwarg_name(kwarg) == "sigmaV"


def test_named_arguments_may_be_nested_and_repeated_across_calls() -> None:
    _resolve(
        "SELECT gblur(unsharp(a.frame, lx => 7), sigma => 2), gblur(a.frame, sigma => 3) "
        "FROM input('x.mp4') a"
    )


def test_a_positional_argument_after_a_named_one_is_rejected() -> None:
    err = _reject("SELECT blur(a.frame, planes => 1, 5) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "must come before named arguments" in err.message


def test_a_duplicate_named_argument_is_rejected() -> None:
    err = _reject("SELECT blur(a.frame, 5, planes => 1, planes => 2) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "duplicate named argument 'planes'" in err.message


def test_a_duplicate_named_argument_is_anchored_on_the_second_one() -> None:
    err = _reject(
        "SELECT blur(a.frame, 5,\n  planes => 1,\n  planes => 2)\nFROM input('x.mp4') a"
    )
    assert err.line == 3


def test_named_arguments_are_checked_inside_a_cte_and_a_union() -> None:
    for sql in (
        "WITH c AS (SELECT blur(a.frame, planes => 1, 5) AS f FROM input('x.mp4') a) "
        "SELECT c.f FROM c",
        "SELECT a.frame FROM input('x.mp4') a UNION ALL "
        "SELECT blur(b.frame, planes => 1, 5) FROM input('y.mp4') b",
    ):
        assert _reject(sql).code is ErrorCode.UNSUPPORTED_SQL


def test_a_named_argument_does_not_smuggle_in_a_column() -> None:
    """The value goes through the same column whitelist everything else does."""
    err = _reject("SELECT gblur(a.frame, sigma => b.frame) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UNKNOWN_ALIAS


def test_overlay_cannot_take_named_arguments() -> None:
    """Postgres has a builtin OVERLAY(x PLACING y FROM n FOR m) and sqlglot
    parses the stdlib's overlay() with that grammar, so `=>` inside it does not
    even tokenize as a named argument -- a documented dead end, not a silent
    mis-parse."""
    err = _reject(
        "SELECT overlay(a.frame, b.frame, 0, 0, eof_action => 'pass') "
        "FROM input('x.mp4') a, input('y.mp4') b"
    )
    assert err.code is ErrorCode.PARSE_ERROR


# ---------------------------------------------------------------------------
# COPY ... TO ... WITH (...)  — the sink wrapper
# ---------------------------------------------------------------------------


def _sink(sql: str) -> RawSink:
    """The single sink of a one-COPY statement (`sinks` is a list)."""
    sinks = _resolve(sql).sinks
    assert len(sinks) == 1
    return sinks[0]


def test_bare_select_has_no_sink() -> None:
    assert _resolve(SINK_QUERY).sinks == []


def test_copy_populates_the_sink() -> None:
    sink = _sink(f"COPY ({SINK_QUERY}) TO 'out.mkv' WITH (video_codec 'libx264', crf 20)")
    assert sink.path == "out.mkv"
    assert [option.name for option in sink.options] == ["video_codec", "crf"]
    # Values stay raw sqlglot nodes here: lower owns the option table.
    assert [option.value.sql() for option in sink.options] == ["'libx264'", "20"]


def test_copy_leaves_the_inner_query_untouched() -> None:
    """The wrapped query resolves exactly like the same query written bare."""
    bare = _resolve(SINK_QUERY)
    wrapped = _resolve(f"COPY ({SINK_QUERY}) TO 'out.mkv'")
    assert wrapped.input_paths == bare.input_paths == ["x.mp4"]
    assert wrapped.sources == bare.sources == {"a": 0}
    assert wrapped.select.sql() == bare.select.sql()


def test_copy_without_with_has_no_options() -> None:
    assert _sink(f"COPY ({SINK_QUERY}) TO 'out.mkv'").options == ()


def test_copy_with_empty_option_list_has_no_options() -> None:
    assert _sink(f"COPY ({SINK_QUERY}) TO 'out.mkv' WITH ()").options == ()


def test_copy_option_names_are_folded_lowercase() -> None:
    """sqlglot drops the quoting of an option name, so "CRF" folds like CRF."""
    for written in ("CRF 20", '"CRF" 20'):
        sink = _sink(f"COPY ({SINK_QUERY}) TO 'out.mkv' WITH ({written})")
        assert [option.name for option in sink.options] == ["crf"]


def test_copy_wraps_a_cte_query() -> None:
    sql = (
        "COPY (WITH c AS (SELECT a.frame AS f FROM input('x.mp4') a) "
        "SELECT c.f FROM c) TO 'out.mkv'"
    )
    res = _resolve(sql)
    assert list(res.ctes) == ["c"]
    assert [sink.path for sink in res.sinks] == ["out.mkv"]


def test_copy_wraps_a_union_all() -> None:
    res = _resolve(
        "COPY (SELECT a.frame FROM input('x') a UNION ALL "
        "SELECT b.frame FROM input('y') b) TO 'out.mkv'"
    )
    assert len(res.branches) == 2
    assert len(res.sinks) == 1


def test_a_sink_carries_its_own_validated_query() -> None:
    """One COPY is one output group, so a sink owns a whole query."""
    res = _resolve(
        "COPY (SELECT a.frame FROM input('x') a UNION ALL "
        "SELECT b.frame FROM input('y') b) TO 'out.mkv'"
    )
    sink = res.sinks[0]
    assert sink.query is res.select
    assert list(sink.branches) == res.branches


def test_copy_keeps_no_streaming_equivalent_of_the_inner_query() -> None:
    """A COPY wrapper never widens the surface: the inner error still wins."""
    err = _reject(
        "COPY (SELECT a.frame FROM input('x.mp4') a GROUP BY a.frame) "
        "TO 'out.mkv' WITH (crf 20)"
    )
    assert err.code is ErrorCode.NO_STREAMING_EQUIVALENT


@pytest.mark.parametrize(
    "sql",
    [
        "COPY (SELECT frame FROM input('x.mp4') a) TO 'out.mkv'",
        "COPY (SELECT a.frame FROM input('x.mp4')) TO 'out.mkv'",
        "COPY (SELECT scale(a.*, 0.5) FROM input('x.mp4') a) TO 'out.mkv'",
        "COPY (SELECT a.frame FROM input('x.mp4') a LIMIT 1) TO 'out.mkv'",
    ],
)
def test_copy_does_not_relax_inner_validation(sql: str) -> None:
    assert _reject(sql).code in (
        ErrorCode.UNSUPPORTED_SQL,
        ErrorCode.NO_STREAMING_EQUIVALENT,
    )


@pytest.mark.parametrize(
    "sql",
    [
        # COPY FROM loads data; there is no ffmpeg equivalent.
        "COPY t FROM 'in.csv'",
        "COPY t FROM 'in.csv' WITH (format 'csv')",
        # not a parenthesized query
        "COPY t TO 'out.csv'",
        # more than one target
        f"COPY ({SINK_QUERY}) TO 'a.mkv', 'b.mkv'",
        # non-literal targets
        f"COPY ({SINK_QUERY}) TO STDOUT",
        f"COPY ({SINK_QUERY}) TO x",
        f"COPY ({SINK_QUERY}) TO PROGRAM 'cat'",
        # option with no value at all
        f"COPY ({SINK_QUERY}) TO 'o.mkv' WITH (faststart)",
        f"COPY ({SINK_QUERY}) TO 'o.mkv' WITH (faststart on)",
        # duplicate option, folded name included
        f"COPY ({SINK_QUERY}) TO 'o.mkv' WITH (crf 20, crf 21)",
        f"COPY ({SINK_QUERY}) TO 'o.mkv' WITH (crf 20, CRF 21)",
        # two COPYs re-declaring the same input alias: the flat namespace is
        # script-wide (the multi-sink rejection itself lives in lower).
        f"COPY ({SINK_QUERY}) TO 'a.mkv'; COPY ({SINK_QUERY}) TO 'b.mkv'",
    ],
)
def test_bad_copy_is_rejected(sql: str) -> None:
    assert _reject(sql).code is ErrorCode.UNSUPPORTED_SQL


def test_copy_from_names_the_supported_form() -> None:
    err = _reject("COPY t FROM 'in.csv'")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert err.hint is not None and "COPY (<query>) TO" in err.hint


# ---------------------------------------------------------------------------
# csv sink classification: FORMAT csv, TO STDOUT
# ---------------------------------------------------------------------------


def test_format_csv_marks_the_sink_csv() -> None:
    sink = _sink(f"COPY ({SINK_QUERY}) TO 'out.csv' WITH (format 'csv')")
    assert sink.is_csv is True
    assert sink.path == "out.csv"


def test_format_csv_bare_word_also_marks_the_sink_csv() -> None:
    """``format csv`` (unquoted, a bare Var under sqlglot) folds the same way
    a quoted string does -- both spellings are stock Postgres COPY syntax."""
    sink = _sink(f"COPY ({SINK_QUERY}) TO 'out.csv' WITH (format csv)")
    assert sink.is_csv is True


def test_format_csv_is_case_insensitive_unquoted() -> None:
    sink = _sink(f"COPY ({SINK_QUERY}) TO 'out.csv' WITH (format CSV)")
    assert sink.is_csv is True


def test_media_copy_with_a_non_csv_format_is_not_csv() -> None:
    """``format`` already means "container format" for a media COPY (an
    existing SINK_OPTIONS entry) -- only the literal value 'csv' flips it."""
    sink = _sink(f"COPY ({SINK_QUERY}) TO 'out.mp4' WITH (format 'mp4')")
    assert sink.is_csv is False


def test_copy_without_format_option_is_not_csv() -> None:
    sink = _sink(f"COPY ({SINK_QUERY}) TO 'out.mkv'")
    assert sink.is_csv is False


def test_to_stdout_is_legal_for_a_csv_sink() -> None:
    sink = _sink(f"COPY ({SINK_QUERY}) TO STDOUT WITH (format 'csv')")
    assert sink.is_csv is True
    assert sink.path is None


def test_to_stdout_lowercase_is_also_accepted() -> None:
    sink = _sink(f"COPY ({SINK_QUERY}) TO stdout WITH (format 'csv')")
    assert sink.path is None


def test_to_stdout_without_format_csv_is_still_rejected() -> None:
    """A media COPY may not target STDOUT -- the TO STDOUT carve-out is
    csv-only (see test_bad_copy_is_rejected's plain "TO STDOUT" case, which
    keeps failing the exact same way)."""
    err = _reject(f"COPY ({SINK_QUERY}) TO STDOUT WITH (video_codec 'libx264')")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "STDOUT" in (err.hint or "") or "file path" in err.message


def test_to_a_named_identifier_with_format_csv_is_still_rejected() -> None:
    """Only the literal word STDOUT is special; anything else unquoted is
    still not a path, csv or not."""
    err = _reject(f"COPY ({SINK_QUERY}) TO x WITH (format 'csv')")
    assert err.code is ErrorCode.UNSUPPORTED_SQL


def test_header_option_is_legal_on_a_csv_sink() -> None:
    sink = _sink(f"COPY ({SINK_QUERY}) TO STDOUT WITH (format 'csv', header true)")
    assert [option.name for option in sink.options] == ["format", "header"]


def test_duplicate_option_is_anchored_on_the_second_one() -> None:
    err = _reject(
        f"COPY ({SINK_QUERY})\nTO 'out.mkv' WITH (\n  crf 20,\n  crf 21\n)"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "duplicate sink option 'crf'" in err.message
    assert err.line == 4


@pytest.mark.parametrize(
    "sql",
    [
        # sqlglot refuses a CTE in front of COPY outright ...
        f"WITH c AS ({SINK_QUERY}) COPY (SELECT c.frame FROM c) TO 'o.mkv'",
        # ... and a nested COPY, and a parenthesized one.
        f"COPY (COPY ({SINK_QUERY}) TO 'a.mkv') TO 'b.mkv'",
        f"(COPY ({SINK_QUERY}) TO 'o.mkv')",
        # a negative or computed option value is not COPY syntax at all
        f"COPY ({SINK_QUERY}) TO 'o.mkv' WITH (crf -3)",
        f"COPY ({SINK_QUERY}) TO 'o.mkv' WITH (crf 20 + 1)",
    ],
)
def test_copy_shapes_sqlglot_itself_refuses(sql: str) -> None:
    assert _reject(sql).code is ErrorCode.PARSE_ERROR


# ---------------------------------------------------------------------------
# scripts + CREATE VIEW
# ---------------------------------------------------------------------------
#
# A script is `CREATE VIEW name AS <query>;`* followed by `COPY ...;`+. The
# parser accepts any number of COPYs; lowering is still single-sink, and the
# TEMPORARY rejection of a second one lives in `sqlmpeg.lower`.

VIEW_SCRIPT = """CREATE VIEW master AS
  SELECT scale(a.frame, 1280, -2) AS v FROM input('film.mkv') a;

COPY (SELECT master.v FROM master) TO 'out.mp4' WITH (crf 20);
"""


def test_a_script_parses_into_a_block() -> None:
    """VERIFIED (sqlglot 30.17): parse_one wraps a multi-statement string in a
    Block; a single statement, trailing semicolon and all, is returned bare."""
    assert isinstance(parse(VIEW_SCRIPT), exp.Block)
    assert isinstance(parse(SINK_QUERY + ";"), exp.Select)
    assert isinstance(parse(SINK_QUERY + ";  \n"), exp.Select)


def test_an_extra_semicolon_is_not_a_statement() -> None:
    """`a;;` parses into a Block whose second entry is a literal None."""
    res = _resolve(SINK_QUERY + ";;")
    assert res.sinks == []
    assert isinstance(res.select, exp.Select)


def test_view_script_resolves() -> None:
    res = _resolve(VIEW_SCRIPT)
    assert list(res.views) == ["master"]
    assert [sink.path for sink in res.sinks] == ["out.mp4"]
    assert res.input_paths == ["film.mkv"]
    assert res.sources == {"a": 0}


def test_a_view_is_bound_exactly_like_a_cte() -> None:
    """`ctes` is the flat, ordered binding table lower walks; a view is one."""
    res = _resolve(VIEW_SCRIPT)
    assert list(res.ctes) == ["master"]
    assert res.ctes["master"] is res.views["master"]


def test_a_view_may_reference_an_earlier_view() -> None:
    res = _resolve(
        "CREATE VIEW one AS SELECT a.frame AS v FROM input('x.mp4') a;\n"
        "CREATE VIEW two AS SELECT scale(one.v, 0.5) AS v FROM one;\n"
        "COPY (SELECT two.v FROM two) TO 'out.mp4';"
    )
    assert list(res.views) == ["one", "two"]
    assert list(res.ctes) == ["one", "two"]


def test_a_view_may_not_reference_a_later_view() -> None:
    err = _reject(
        "CREATE VIEW one AS SELECT two.v AS v FROM two;\n"
        "CREATE VIEW two AS SELECT a.frame AS v FROM input('x.mp4') a;\n"
        "COPY (SELECT one.v FROM one) TO 'out.mp4';"
    )
    assert err.code is ErrorCode.UNKNOWN_ALIAS


def test_a_view_body_may_have_its_own_with() -> None:
    """A view BODY is a whole statement, so the nested-WITH rejection (which
    is CTE-body-only) does not apply to it. Its CTEs are hoisted into the flat
    binding table AHEAD of the view, which is the order lower needs."""
    res = _resolve(
        "CREATE VIEW v AS WITH c AS (SELECT a.frame AS f FROM input('x.mp4') a) "
        "SELECT c.f AS v FROM c;\n"
        "COPY (SELECT v.v FROM v) TO 'out.mp4';"
    )
    assert list(res.ctes) == ["c", "v"]
    assert list(res.views) == ["v"]


def test_a_cte_body_still_may_not_have_its_own_with() -> None:
    err = _reject(
        "CREATE VIEW v AS WITH c AS (WITH d AS (SELECT a.frame AS f "
        "FROM input('x.mp4') a) SELECT d.f AS f FROM d) SELECT c.f AS v FROM c;\n"
        "COPY (SELECT v.v FROM v) TO 'out.mp4';"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "nested WITH" in err.message


def test_a_view_body_may_be_a_union_all() -> None:
    res = _resolve(
        "CREATE VIEW v AS SELECT a.frame AS f FROM input('x') a UNION ALL "
        "SELECT b.frame AS f FROM input('y') b;\n"
        "COPY (SELECT v.f FROM v) TO 'out.mp4';"
    )
    assert isinstance(res.views["v"], exp.Union)
    assert len(union_branches(res.views["v"])) == 2


def test_a_copy_may_still_carry_its_own_with_in_a_script() -> None:
    res = _resolve(
        "CREATE VIEW v AS SELECT a.frame AS f FROM input('x.mp4') a;\n"
        "COPY (WITH c AS (SELECT v.f AS g FROM v) SELECT c.g FROM c) TO 'out.mp4';"
    )
    assert list(res.ctes) == ["v", "c"]


def test_several_copies_resolve_cleanly() -> None:
    """Resolve is already multi-sink; only lowering is not."""
    res = _resolve(
        "CREATE VIEW m AS SELECT a.frame AS v FROM input('film.mkv') a;\n"
        "COPY (SELECT scale(m.v, 1280, -2) FROM m) TO '720.mp4';\n"
        "COPY (SELECT scale(m.v, 640, -2) FROM m) TO '360.mp4' WITH (crf 30);"
    )
    assert [sink.path for sink in res.sinks] == ["720.mp4", "360.mp4"]
    assert [len(sink.branches) for sink in res.sinks] == [1, 1]
    assert [option.name for option in res.sinks[1].options] == ["crf"]
    # `select`/`branches` stay the FIRST sink's.
    assert res.select is res.sinks[0].query


def test_a_view_must_precede_every_copy() -> None:
    err = _reject(
        "COPY (SELECT a.frame FROM input('x.mp4') a) TO 'out.mp4';\n"
        "CREATE VIEW v AS SELECT b.frame AS f FROM input('y.mp4') b;"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "may not follow a COPY" in err.message
    assert err.line == 2


def test_a_bare_select_in_a_script_is_rejected() -> None:
    err = _reject(
        "CREATE VIEW v AS SELECT a.frame AS f FROM input('x.mp4') a;\n"
        "SELECT v.f FROM v;"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert err.hint is not None and "COPY (<query>) TO" in err.hint


def test_a_script_with_no_copy_is_rejected() -> None:
    err = _reject("CREATE VIEW v AS SELECT a.frame AS f FROM input('x.mp4') a;")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "must write its output with COPY" in err.message


def test_an_unused_view_is_rejected_at_its_create() -> None:
    err = _reject(
        "CREATE VIEW used AS SELECT a.frame AS f FROM input('x.mp4') a;\n"
        "CREATE VIEW spare AS SELECT b.frame AS f FROM input('y.mp4') b;\n"
        "COPY (SELECT used.f FROM used) TO 'out.mp4';"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "view 'spare' is never used" in err.message
    assert err.line == 2


def test_a_view_used_only_by_another_view_counts_as_used() -> None:
    _resolve(
        "CREATE VIEW one AS SELECT a.frame AS v FROM input('x.mp4') a;\n"
        "CREATE VIEW two AS SELECT scale(one.v, 0.5) AS v FROM one;\n"
        "COPY (SELECT two.v FROM two) TO 'out.mp4';"
    )


def test_view_names_share_the_flat_namespace() -> None:
    for sql in (
        # view vs view
        "CREATE VIEW v AS SELECT a.frame AS f FROM input('x.mp4') a;\n"
        "CREATE VIEW v AS SELECT b.frame AS f FROM input('y.mp4') b;\n"
        "COPY (SELECT v.f FROM v) TO 'out.mp4';",
        # view vs an input alias declared inside it
        "CREATE VIEW a AS SELECT a.frame AS f FROM input('x.mp4') a;\n"
        "COPY (SELECT a.f FROM a) TO 'out.mp4';",
        # view vs a CTE of its own body
        "CREATE VIEW c AS WITH c AS (SELECT a.frame AS f FROM input('x.mp4') a) "
        "SELECT c.f AS f FROM c;\n"
        "COPY (SELECT c.f FROM c) TO 'out.mp4';",
        # view vs a CTE of a later COPY
        "CREATE VIEW v AS SELECT a.frame AS f FROM input('x.mp4') a;\n"
        "COPY (WITH v AS (SELECT b.frame AS f FROM input('y.mp4') b) "
        "SELECT v.f FROM v) TO 'out.mp4';",
        # view vs a later input alias
        "CREATE VIEW b AS SELECT a.frame AS f FROM input('x.mp4') a;\n"
        "COPY (SELECT b.frame FROM b, input('y.mp4') b) TO 'out.mp4';",
    ):
        err = _reject(sql)
        assert err.code is ErrorCode.UNSUPPORTED_SQL, sql


def test_ffmpeg_is_reserved_as_a_view_name() -> None:
    err = _reject(
        "CREATE VIEW ffmpeg AS SELECT a.frame AS f FROM input('x.mp4') a;\n"
        "COPY (SELECT ffmpeg.f FROM ffmpeg) TO 'out.mp4';"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "reserved for the filter namespace" in err.message


def test_view_names_fold_the_postgres_way() -> None:
    res = _resolve(
        "CREATE VIEW Master AS SELECT a.frame AS f FROM input('x.mp4') a;\n"
        "COPY (SELECT MASTER.f FROM mAsTeR) TO 'out.mp4';"
    )
    assert list(res.views) == ["master"]


_VIEW_BODY = "SELECT a.frame AS f FROM input('x.mp4') a"
_VIEW_COPY = "COPY (SELECT v.f FROM v) TO 'out.mp4';"


@pytest.mark.parametrize(
    ("label", "create"),
    [
        ("or replace", f"CREATE OR REPLACE VIEW v AS {_VIEW_BODY}"),
        ("temp", f"CREATE TEMP VIEW v AS {_VIEW_BODY}"),
        ("temporary", f"CREATE TEMPORARY VIEW v AS {_VIEW_BODY}"),
        ("materialized", f"CREATE MATERIALIZED VIEW v AS {_VIEW_BODY}"),
        ("if not exists", f"CREATE VIEW IF NOT EXISTS v AS {_VIEW_BODY}"),
        ("column list", f"CREATE VIEW v (c1, c2) AS {_VIEW_BODY}"),
        ("qualified name", f"CREATE VIEW s.v AS {_VIEW_BODY}"),
        ("view options", f"CREATE VIEW v WITH (security_barrier=true) AS {_VIEW_BODY}"),
        # sqlglot cannot parse RECURSIVE VIEW at all and falls back to
        # exp.Command, which is not a statement sqlmpeg knows either.
        ("recursive", f"CREATE RECURSIVE VIEW v (c) AS {_VIEW_BODY}"),
        ("create table", f"CREATE TABLE v AS {_VIEW_BODY}"),
        ("drop view", "DROP VIEW v"),
        ("alter view", "ALTER VIEW v RENAME TO w"),
    ],
)
def test_rejected_create_variants(label: str, create: str) -> None:
    err = _reject(f"{create};\n{_VIEW_COPY}")
    assert err.code is ErrorCode.UNSUPPORTED_SQL, label
    assert err.line == 1, label


def test_a_bad_view_body_is_rejected_like_any_other_query() -> None:
    """A view never widens the surface."""
    err = _reject(
        "CREATE VIEW v AS SELECT a.frame AS f FROM input('x.mp4') a GROUP BY a.frame;\n"
        f"{_VIEW_COPY}"
    )
    assert err.code is ErrorCode.NO_STREAMING_EQUIVALENT


def test_a_view_may_be_aliased_in_from() -> None:
    """`FROM master m` binds a BRANCH-LOCAL name."""
    res = _resolve(
        f"CREATE VIEW v AS {_VIEW_BODY};\nCOPY (SELECT m.f FROM v m) TO 'out.mp4';"
    )
    assert list(res.views) == ["v"]
    assert [sink.path for sink in res.sinks] == ["out.mp4"]


def test_two_sinks_may_reuse_the_same_view_alias() -> None:
    """The alias is branch-local, so it is not in the flat namespace at all."""
    res = _resolve(
        f"CREATE VIEW v AS {_VIEW_BODY};\n"
        "COPY (SELECT m.f FROM v m) TO 'a.mp4';\n"
        "COPY (SELECT m.f FROM v m) TO 'b.mp4';"
    )
    assert [sink.path for sink in res.sinks] == ["a.mp4", "b.mp4"]


def test_a_view_alias_may_not_shadow_a_flat_namespace_name() -> None:
    err = _reject(
        f"CREATE VIEW v AS {_VIEW_BODY};\n"
        "COPY (SELECT a.f FROM v a, input('y.mp4') a) TO 'out.mp4';"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "duplicate name 'a'" in err.message


def test_a_view_alias_may_not_be_the_filter_namespace() -> None:
    err = _reject(
        f"CREATE VIEW v AS {_VIEW_BODY};\n"
        "COPY (SELECT ffmpeg.f FROM v ffmpeg) TO 'out.mp4';"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "reserved" in err.message


def test_a_cte_may_be_aliased_too() -> None:
    """The relaxation is on the binding, not on how it was defined."""
    res = _resolve(
        "WITH c AS (SELECT a.frame FROM input('x') a) SELECT z.frame FROM c z"
    )
    assert list(res.ctes) == ["c"]


def test_an_aliased_name_may_not_collide_with_another_from_entry() -> None:
    err = _reject(
        "WITH c AS (SELECT a.frame FROM input('x') a), "
        "d AS (SELECT b.frame FROM input('y') b) "
        "SELECT z.frame FROM c z, d z"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "duplicate name 'z'" in err.message


# ---------------------------------------------------------------------------
# track rows: FROM unnest(<input>.<type>) alias
# ---------------------------------------------------------------------------


def test_unnest_binds_a_track_row_table() -> None:
    res = _resolve(
        "SELECT t.track FROM input('f.mkv') f, unnest(f.audio) t "
        "WHERE t.language = 'eng'"
    )
    assert list(res.track_rows) == ["t"]
    rows = res.track_rows["t"]
    assert (rows.alias, rows.source, rows.column) == ("t", "f", "audio")
    # A row table takes no `-i` of its own: the tracks belong to the input.
    assert res.sources == {"f": 0}
    assert res.input_paths == ["f.mkv"]


@pytest.mark.parametrize("column", ["video", "audio", "subtitle", "data"])
def test_every_stream_array_unnests(column: str) -> None:
    res = _resolve(
        f"SELECT t.track FROM input('f.mkv') f, unnest(f.{column}) t"
    )
    assert res.track_rows["t"].column == column


def test_unnest_accepts_the_as_spelling_and_folds_the_alias() -> None:
    res = _resolve("SELECT T.track FROM input('f.mkv') f, unnest(f.AUDIO) AS T")
    assert list(res.track_rows) == ["t"]
    assert res.track_rows["t"].column == "audio"


def test_two_unnests_of_one_input_are_two_row_tables() -> None:
    res = _resolve(
        "SELECT a.track, b.track FROM input('f.mkv') f, "
        "unnest(f.audio) a, unnest(f.video) b"
    )
    assert [(name, rows.column) for name, rows in res.track_rows.items()] == [
        ("a", "audio"),
        ("b", "video"),
    ]


def test_unnest_binds_inside_a_cte_body_and_a_union_branch() -> None:
    res = _resolve(
        "WITH x AS (SELECT t.track AS a FROM input('f.mkv') f, unnest(f.audio) t) "
        "SELECT x.a FROM x"
    )
    assert res.track_rows["t"].source == "f"
    res = _resolve(
        "SELECT t.track FROM input('a.mkv') f, unnest(f.audio) t "
        "UNION ALL "
        "SELECT u.track FROM input('b.mkv') g, unnest(g.audio) u"
    )
    assert sorted(res.track_rows) == ["t", "u"]


def test_a_row_alias_shares_the_one_flat_namespace() -> None:
    err = _reject(
        "SELECT t.track FROM input('f.mkv') f, unnest(f.audio) t, unnest(f.video) t"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "duplicate name 't'" in err.message
    err = _reject("SELECT t.track FROM input('f.mkv') t, unnest(t.audio) t")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "duplicate name 't'" in err.message


def test_unnest_requires_an_alias() -> None:
    err = _reject("SELECT t.track FROM input('f.mkv') f, unnest(f.audio)")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "requires an alias" in err.message


def test_unnest_rejects_a_column_alias_list() -> None:
    err = _reject("SELECT t.track FROM input('f.mkv') f, unnest(f.audio) t(x)")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "column aliases" in err.message


def test_unnest_rejects_with_ordinality() -> None:
    err = _reject(
        "SELECT t.track FROM input('f.mkv') f, unnest(f.audio) WITH ORDINALITY t"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "ORDINALITY" in err.message
    assert err.hint is not None and "index" in err.hint


@pytest.mark.parametrize(
    "argument",
    ["f.audio[1]", "f.frame", "f.t", "'audio'", "f.*", "unnest(f.audio)"],
)
def test_unnest_takes_a_bare_stream_array_and_nothing_else(argument: str) -> None:
    err = _reject(f"SELECT t.track FROM input('f.mkv') f, unnest({argument}) t")
    assert err.code is ErrorCode.UNSUPPORTED_SQL


def test_unnest_rejects_more_than_one_argument() -> None:
    err = _reject(
        "SELECT t.track FROM input('f.mkv') f, unnest(f.audio, f.video) t"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "exactly one array column" in err.message


def test_unnest_only_sees_comma_sources_written_before_it() -> None:
    # Postgres scopes an implicit-LATERAL function call to the FROM items
    # written before it, so this genuinely does not see `f`.
    err = _reject("SELECT t.track FROM unnest(f.audio) t, input('f.mkv') f")
    assert err.code is ErrorCode.UNKNOWN_ALIAS
    assert "unknown alias 'f'" in err.message


def test_unnest_needs_an_input_not_a_cte_or_a_generated_source() -> None:
    err = _reject(
        "WITH c AS (SELECT a.audio AS tracks FROM input('f.mkv') a) "
        "SELECT t.track FROM c, unnest(c.tracks) t"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "only an input's array column can be unnested" in err.message
    err = _reject(
        "SELECT t.track FROM ffmpeg.anullsrc(duration => 2) s, unnest(s.audio) t"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "only an input's array column can be unnested" in err.message


# -- JOIN between track-row tables ----------------------


def _joined(join: str = "JOIN", on: str = "ON a.language = b.language") -> str:
    return (
        "SELECT a.track FROM input('a.mkv') f, input('b.mkv') g, "
        f"unnest(f.audio) a {join} unnest(g.audio) b {on}"
    )


@pytest.mark.parametrize(
    "join",
    ["JOIN", "INNER JOIN", "LEFT JOIN", "LEFT OUTER JOIN", "FULL JOIN", "FULL OUTER JOIN"],
)
def test_admitted_join_forms_between_unnest_tables(join: str) -> None:
    """FULL arrives with AND without `kind='OUTER'`; both are the same join."""
    assert sorted(_resolve(_joined(join)).track_rows) == ["a", "b"]


def test_from_entries_reports_each_items_join_kind() -> None:
    select = parse(_joined("FULL OUTER JOIN"))
    assert isinstance(select, exp.Select)
    entries = from_entries(select)
    assert [None if join is None else join.kind for _, join in entries] == [
        None,  # `FROM input('a.mkv') f` is not attached by anything
        "cross",  # a comma source
        "cross",  # `, unnest(f.audio) a`
        "full",
    ]


def test_a_join_may_match_on_several_keys_and_on_a_literal() -> None:
    sql = _joined(
        on="ON a.language = b.language AND a.channels = b.channels "
        "AND b.codec != 'ac3'"
    )
    assert sorted(_resolve(sql).track_rows) == ["a", "b"]


def test_a_join_on_columns_of_different_types_is_rejected() -> None:
    err = _reject(_joined(on="ON a.language = b.channels"))
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "so they can never match" in err.message


def test_a_join_cannot_match_on_the_track_column() -> None:
    err = _reject(_joined(on="ON a.track = b.track"))
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "is a stream, not a value to compare" in err.message


def test_a_join_with_no_on_is_the_comma_cross_join() -> None:
    """VERIFIED (sqlglot 30.17): `a JOIN b` with no ON parses to an exp.Join
    carrying nothing but `this` -- byte for byte what a comma source produces.
    The two are indistinguishable in the AST, so a bare JOIN between two row
    tables IS the bounded cross join, not a rejection we could even spell."""
    select = parse(_joined(on=""))
    assert isinstance(select, exp.Select)
    assert [None if join is None else join.kind for _, join in from_entries(select)][
        -1
    ] == "cross"
    assert sorted(_resolve(_joined(on="")).track_rows) == ["a", "b"]


def test_an_on_predicate_may_not_name_a_non_row_alias() -> None:
    err = _reject(_joined(on="ON a.language = f.t"))
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "unknown column 'f.t'" in err.message
    assert "not a track-row table" in (err.hint or "")


def test_unsupported_on_shapes_are_rejected() -> None:
    err = _reject(_joined(on="ON a.language LIKE b.language"))
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "unsupported ON predicate" in err.message


@pytest.mark.parametrize(
    ("join", "on", "message"),
    [
        ("RIGHT JOIN", "ON a.language = b.language", "RIGHT JOIN is not supported"),
        ("CROSS JOIN", "", "CROSS JOIN is not supported"),
        ("NATURAL JOIN", "", "this JOIN form is not supported"),
        ("JOIN", "USING (language)", "USING is not supported"),
    ],
)
def test_join_forms_outside_the_admitted_set(join: str, on: str, message: str) -> None:
    err = _reject(_joined(join, on))
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert message in err.message


def test_join_is_still_rejected_between_stream_level_sources() -> None:
    # input-level FROM stays a comma cross-join.
    err = _reject(
        "SELECT f.video[1] FROM input('a.mkv') f JOIN input('b.mkv') g ON f.t = g.t"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "between unnest(...) track-row tables only" in err.message


def test_join_is_rejected_when_its_left_side_is_not_a_row_table() -> None:
    err = _reject(
        "SELECT a.track FROM input('a.mkv') f, input('b.mkv') g "
        "JOIN unnest(g.audio) a ON a.language = 'eng'"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "between unnest(...) track-row tables only" in err.message


# -- row columns ------------------------------------------------------------


@pytest.mark.parametrize(
    "column",
    [
        "track", "index", "language", "title", "codec", "channels",
        "channel_layout", "sample_rate", "bitrate", "duration",
    ],
)
def test_audio_row_columns_resolve(column: str) -> None:
    sql = (
        "SELECT t.track FROM input('f.mkv') f, unnest(f.audio) t "
        f"WHERE t.{column} IS NOT NULL"
    )
    if column == "track":
        err = _reject(sql)
        assert err.code is ErrorCode.UNSUPPORTED_SQL
        assert "is a stream, not a value to compare" in err.message
    else:
        assert _resolve(sql).track_rows["t"].column == "audio"


@pytest.mark.parametrize(
    "column", ["width", "height", "fps", "color_transfer", "bitrate", "duration"]
)
def test_video_row_columns_resolve(column: str) -> None:
    _resolve(
        "SELECT t.track FROM input('f.mkv') f, unnest(f.video) t "
        f"WHERE t.{column} IS NOT NULL"
    )


def test_a_row_column_is_checked_against_its_stream_types_schema() -> None:
    # `channels` is an audio column; a video row has no such thing.
    err = _reject(
        "SELECT t.track FROM input('f.mkv') f, unnest(f.video) t WHERE t.channels = 2"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "unknown column 't.channels'" in err.message
    assert err.hint is not None and "video track rows expose" in err.hint
    # ... and a subtitle row carries only the five common ones.
    err = _reject(
        "SELECT s.track FROM input('f.mkv') f, unnest(f.subtitle) s WHERE s.width = 1"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "unknown column 's.width'" in err.message


def test_an_unknown_row_column_in_the_select_list_is_rejected() -> None:
    err = _reject("SELECT t.nope FROM input('f.mkv') f, unnest(f.audio) t")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "unknown column 't.nope'" in err.message


# -- WHERE over row columns -------------------------------------------------


@pytest.mark.parametrize(
    "predicate",
    [
        "t.language = 'eng'",
        "'eng' = t.language",
        "t.language != 'eng'",
        "t.channels > 2",
        "t.channels >= 2",
        "t.channels < 6",
        "t.channels <= 6",
        "t.bitrate BETWEEN 1000 AND 2000",
        "t.duration <= 2.5",
        "t.bitrate >= -1",
        "t.language IS NULL",
        "t.title IS NOT NULL",
        "NOT (t.language = 'eng')",
        "t.language = 'eng' AND t.channel_layout = 'stereo'",
        "(t.language = 'eng' OR t.language IS NULL) AND t.channels = 2",
    ],
)
def test_row_predicates_are_admitted(predicate: str) -> None:
    _resolve(
        f"SELECT t.track FROM input('f.mkv') f, unnest(f.audio) t WHERE {predicate}"
    )


@pytest.mark.parametrize(
    "predicate",
    [
        "t.language LIKE 'e%'",
        "t.language IN ('eng', 'fra')",
        "t.channels BETWEEN SYMMETRIC 2 AND 6",
        "t.channels = t.sample_rate",
        "t.language IS TRUE",
    ],
)
def test_unsupported_row_predicates_are_rejected(predicate: str) -> None:
    err = _reject(
        f"SELECT t.track FROM input('f.mkv') f, unnest(f.audio) t WHERE {predicate}"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL


def test_a_row_predicate_is_typed_against_the_static_column_type() -> None:
    err = _reject(
        "SELECT t.track FROM input('f.mkv') f, unnest(f.audio) t "
        "WHERE t.channels = 'stereo'"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "'t.channels' is number" in err.message
    err = _reject(
        "SELECT t.track FROM input('f.mkv') f, unnest(f.audio) t WHERE t.language = 5"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "'t.language' is text" in err.message


def test_a_row_predicate_may_not_compare_the_stream_column() -> None:
    err = _reject(
        "SELECT t.track FROM input('f.mkv') f, unnest(f.audio) t WHERE t.track = 1"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "is a stream, not a value to compare" in err.message


def test_a_time_window_and_a_row_predicate_coexist_as_separate_conjuncts() -> None:
    res = _resolve(
        "SELECT t.track FROM input('f.mkv') f, unnest(f.audio) t "
        "WHERE f.t BETWEEN 1 AND 2 AND t.language = 'eng'"
    )
    assert res.track_rows["t"].source == "f"


def test_a_conjunct_may_not_mix_a_row_column_with_another_alias() -> None:
    err = _reject(
        "SELECT t.track FROM input('f.mkv') f, unnest(f.audio) t "
        "WHERE t.language = 'eng' OR f.t >= 1"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "cannot mix track-row columns" in err.message


def test_a_conjunct_may_reference_only_one_row_table() -> None:
    err = _reject(
        "SELECT a.track FROM input('f.mkv') f, unnest(f.audio) a, unnest(f.video) b "
        "WHERE a.language = b.language"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "only one track-row table" in err.message


def test_a_time_window_over_an_input_still_rejects_a_non_t_column() -> None:
    # The time half of the WHERE clause is untouched by the row half.
    err = _reject(
        "SELECT t.track FROM input('f.mkv') f, unnest(f.audio) t WHERE f.video >= 1"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "only the time column 'f.t' can be filtered" in err.message


# -- ORDER BY: the one carve-out in the streaming rejection -----------------


def test_order_by_is_admitted_over_track_row_columns() -> None:
    res = _resolve(
        "SELECT t.track FROM input('f.mkv') f, unnest(f.audio) t "
        "ORDER BY t.language, t.channels DESC"
    )
    order = res.branches[0].args["order"]
    assert isinstance(order, exp.Order)
    # sqlglot fills the Postgres NULL defaults in for us: ASC -> NULLS LAST,
    # DESC -> NULLS FIRST. Honoring the flags verbatim IS Postgres semantics.
    assert [bool(o.args.get("desc")) for o in order.expressions] == [False, True]
    assert [bool(o.args.get("nulls_first")) for o in order.expressions] == [False, True]


def test_order_by_without_any_unnest_is_still_rejected() -> None:
    err = _reject("SELECT f.audio[1] FROM input('f.mkv') f ORDER BY f.t")
    assert err.code is ErrorCode.NO_STREAMING_EQUIVALENT
    assert err.hint == "remove the ORDER BY clause"


@pytest.mark.parametrize("key", ["f.t", "1", "t.track"])
def test_order_by_a_non_row_column_is_rejected_even_in_a_row_query(key: str) -> None:
    err = _reject(
        "SELECT t.track FROM input('f.mkv') f, unnest(f.audio) t "
        f"ORDER BY {key}"
    )
    assert err.code in (
        ErrorCode.NO_STREAMING_EQUIVALENT,
        ErrorCode.UNSUPPORTED_SQL,
    )


def test_the_other_streaming_rejections_are_untouched_by_the_carve_out() -> None:
    err = _reject("SELECT t.track FROM input('f.mkv') f, unnest(f.audio) t LIMIT 1")
    assert err.code is ErrorCode.NO_STREAMING_EQUIVALENT


# ---------------------------------------------------------------------------
# subscript metadata accessors: <alias>.<type>[k].<column>
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spelling",
    ["f.audio[1].language", "(f.audio[1]).language"],
)
def test_both_subscript_metadata_spellings_are_admitted(spelling: str) -> None:
    # The bracket-dot and the strictly-Postgres paren-dot forms are the same
    # shape, VERIFIED under sqlglot 30.17 (module docstring); either compiles
    # cleanly as a WHERE assertion.
    _resolve(f"SELECT f.audio[1] FROM input('f.mkv') f WHERE {spelling} = 'eng'")


def test_dot_track_is_sugar_for_the_bare_bracket_in_select() -> None:
    res = _resolve("SELECT f.audio[1].track FROM input('f.mkv') f")
    projection = res.branches[0].expressions[0]
    assert isinstance(projection, exp.Dot)


def test_dot_track_is_still_a_stream_not_a_where_value() -> None:
    # `.track` parses fine in WHERE position too -- it is simply rejected for
    # the same reason a row table's bare `t.track` is: a stream is not
    # something to compare.
    err = _reject(
        "SELECT f.audio[1] FROM input('f.mkv') f WHERE f.audio[1].track = 1"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "is a stream, not a value to compare" in err.message


@pytest.mark.parametrize(
    "column",
    ["language", "title", "codec", "channels", "channel_layout", "index"],
)
def test_a_non_track_accessor_is_rejected_as_a_select_output_in_a_media_query(
    column: str,
) -> None:
    # this rejection is MEDIA-only -- a bare SELECT (no
    # COPY) is always at least table-capable, and metadata columns are legal
    # there (see test_subscript_metadata_output_is_legal_in_table_mode
    # below). Wrapping in a real media COPY keeps this test on the rejection
    # it means to check.
    err = _reject(
        f"COPY (SELECT f.audio[1].{column} FROM input('f.mkv') f) TO 'out.mp4'"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "is track metadata, not a stream" in err.message


@pytest.mark.parametrize(
    "column",
    ["language", "title", "codec", "channels", "channel_layout", "index"],
)
def test_subscript_metadata_output_is_legal_in_table_mode(column: str) -> None:
    # A bare SELECT (no COPY at all) is table-capable unconditionally.
    _resolve(f"SELECT f.audio[1].{column} FROM input('f.mkv') f")
    # So is a csv COPY.
    _resolve(
        f"COPY (SELECT f.audio[1].{column} FROM input('f.mkv') f) "
        "TO STDOUT WITH (format csv)"
    )


def test_bare_array_metadata_access_is_rejected_in_select() -> None:
    err = _reject("SELECT f.audio.language FROM input('f.mkv') f")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "needs a subscript" in err.message
    assert err.hint is not None and "unnest" in err.hint


def test_bare_array_metadata_access_is_rejected_in_where() -> None:
    err = _reject(
        "SELECT f.audio[1] FROM input('f.mkv') f WHERE f.audio.language = 'eng'"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "needs a subscript" in err.message


@pytest.mark.parametrize(
    "predicate",
    [
        "f.audio[1].language = 'eng'",
        "'eng' = f.audio[1].language",
        "f.audio[1].language != 'eng'",
        "f.audio[1].channels > 2",
        "f.audio[1].channels >= 2",
        "f.audio[1].channels < 6",
        "f.audio[1].bitrate BETWEEN 1000 AND 2000",
        "f.audio[1].language IS NULL",
        "f.audio[1].title IS NOT NULL",
        "NOT (f.audio[1].language = 'eng')",
        "f.audio[1].language = 'eng' AND f.audio[2].language = 'fra'",
        "(f.audio[1].language = 'eng' OR f.audio[1].language IS NULL) "
        "AND f.audio[1].channels = 2",
    ],
)
def test_subscript_metadata_predicates_are_admitted(predicate: str) -> None:
    _resolve(f"SELECT f.audio[1] FROM input('f.mkv') f WHERE {predicate}")


def test_a_subscript_predicate_is_typed_against_the_static_column_type() -> None:
    err = _reject(
        "SELECT f.audio[1] FROM input('f.mkv') f WHERE f.audio[1].channels = 'stereo'"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "is number" in err.message
    err = _reject(
        "SELECT f.audio[1] FROM input('f.mkv') f WHERE f.audio[1].language = 5"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "is text" in err.message


def test_a_conjunct_may_not_mix_a_subscript_accessor_with_the_time_window() -> None:
    err = _reject(
        "SELECT f.audio[1] FROM input('f.mkv') f "
        "WHERE f.audio[1].language = 'eng' OR f.t >= 1"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "cannot mix subscript metadata accessors" in err.message


def test_a_time_window_and_a_subscript_predicate_coexist_as_separate_conjuncts() -> None:
    _resolve(
        "SELECT f.audio[1] FROM input('f.mkv') f "
        "WHERE f.t BETWEEN 1 AND 2 AND f.audio[1].language = 'eng'"
    )


@pytest.mark.parametrize(
    ("column", "predicate"),
    [
        ("video", "f.video[1].width = 640"),
        ("subtitle", "f.subtitle[1].language = 'eng'"),
        ("data", "f.data[1].index = 1"),
    ],
)
def test_video_subtitle_and_data_subscript_accessors_resolve(
    column: str, predicate: str
) -> None:
    _resolve(f"SELECT f.{column}[1] FROM input('f.mkv') f WHERE {predicate}")


def test_a_subscript_accessor_over_a_cte_is_rejected() -> None:
    err = _reject(
        "WITH c AS (SELECT a.frame FROM input('a.mkv') a) "
        "SELECT c.frame FROM c WHERE c.frame[1].language = 'eng'"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "is a CTE" in err.message


def test_a_subscript_accessor_over_a_generated_source_is_rejected() -> None:
    err = _reject(
        "SELECT s.video[1] FROM ffmpeg.testsrc(duration => 2) s "
        "WHERE s.video[1].language = 'eng'"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "is a generated source" in err.message


def test_a_subscript_accessor_over_a_row_alias_is_rejected() -> None:
    # In SELECT position, so the row-language WHERE grammar (which claims
    # anything mentioning a row alias first) is not what intercepts this.
    err = _reject(
        "SELECT t.track[1].language FROM input('f.mkv') f, unnest(f.audio) t"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "already a track-row table" in err.message


# ---------------------------------------------------------------------------
# guardrail #7: no panics on user input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT",
        "WITH",
        "WITH c AS () SELECT 1",
        "SELECT a.frame FROM input('x') a WHERE a.t BETWEEN AND 2",
        "SELECT scale(a.frame,) FROM input('x') a",
        "SELECT a.frame FROM input('x') a,",
        "select 'x' from input('x')",
        "-- just a comment",
        "/* block */",
        "SELECT a.frame FROM input('x') a UNION ALL",
        "\x00\x01",
        "SELECT a.video[ FROM input('x') a",
        "SELECT a.video[] FROM input('x') a",
        "SELECT a.video[[1]] FROM input('x') a",
        "SELECT a.video[1 FROM input('x') a",
        "SELECT a.video[" + "1," * 200 + "1] FROM input('x') a",
        "SELECT " + "a.video[1], " * 200 + "a.audio[1] FROM input('x') a",
        "SELECT " + "f(" * 60 + "a.frame" + ")" * 60 + " FROM input('x') a",
    ],
)
def test_never_raises_anything_but_sqlmpeg_error(sql: str) -> None:
    try:
        _resolve(sql)
    except SqlmpegError as err:
        assert err.code is not ErrorCode.INTERNAL
        assert err.line is not None


# ---------------------------------------------------------------------------
# unnest(f.chapters) in FROM, and VALUES CTEs
# ---------------------------------------------------------------------------


def test_chapters_binds_a_row_table_shaped_like_a_track_row() -> None:
    res = _resolve(
        "SELECT c.index, c.title, c.start_t, c.end_t "
        "FROM input('f.mkv') f, unnest(f.chapters) c"
    )
    assert list(res.track_rows) == ["c"]
    rows = res.track_rows["c"]
    assert (rows.alias, rows.source, rows.column) == ("c", "f", "chapters")


def test_chapters_accepts_the_as_spelling_and_folds_the_alias() -> None:
    res = _resolve("SELECT C.title FROM input('f.mkv') f, unnest(f.chapters) AS C")
    assert list(res.track_rows) == ["c"]


def test_chapters_requires_an_alias() -> None:
    err = _reject("SELECT 1 FROM input('f.mkv') f, unnest(f.chapters)")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "requires an alias" in err.message


def test_the_chapters_table_function_is_gone() -> None:
    """`chapters(f)` was removed: it hits the unknown-table-function path,
    with a hint naming the array column that replaced it."""
    err = _reject("SELECT 1 FROM input('f.mkv') f, chapters(f) c")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "unsupported table function chapters()" in err.message
    assert "unnest(f.chapters) c" in (err.hint or "")


def test_chapters_unnests_only_an_input_alias() -> None:
    err = _reject(f"WITH x AS ({SINK_QUERY}) SELECT 1 FROM x, unnest(x.chapters) c")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "only an input's array column can be unnested" in err.message

    err = _reject(
        "SELECT 1 FROM input('f.mkv') f, unnest(f.audio) t, unnest(t.chapters) c"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL

    err = _reject("SELECT 1 FROM input('f.mkv') f, unnest(nope.chapters) c")
    assert err.code is ErrorCode.UNKNOWN_ALIAS


def test_chapters_rows_combine_with_track_rows_like_any_other_array() -> None:
    """No carve-out left: two unnest tables of one input cross join."""
    res = _resolve(
        "SELECT t.language, c.title "
        "FROM input('f.mkv') f, unnest(f.audio) t, unnest(f.chapters) c"
    )
    assert [rows.column for rows in res.track_rows.values()] == ["audio", "chapters"]


def test_two_inputs_chapters_are_two_row_tables() -> None:
    res = _resolve(
        "SELECT c.title, d.title FROM input('f.mkv') f, input('g.mkv') g, "
        "unnest(f.chapters) c, unnest(g.chapters) d"
    )
    assert [rows.source for rows in res.track_rows.values()] == ["f", "g"]


def test_a_chapters_subscript_is_rejected_with_an_unnest_hint() -> None:
    err = _reject("SELECT f.chapters[1].title FROM input('f.mkv') f")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "'f.chapters' cannot be subscripted" in err.message
    assert "unnest(f.chapters) c" in (err.hint or "")


def test_a_bare_chapters_accessor_names_the_unnest() -> None:
    err = _reject("SELECT f.chapters.title FROM input('f.mkv') f")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "'f.chapters.title' needs a row" in err.message
    assert "unnest(f.chapters) c" in (err.hint or "")


def test_a_chapters_column_in_a_value_expression_is_rejected() -> None:
    err = _reject("SELECT f.chapters::text FROM input('f.mkv') f")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "'f.chapters' is an array of chapter records" in err.message
    assert "unnest(f.chapters) c" in (err.hint or "")


def test_chapters_column_list_is_rejected() -> None:
    err = _reject("SELECT 1 FROM input('f.mkv') f, unnest(f.chapters) c(x)")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "table column aliases are not supported" in err.message


def test_chapters_row_columns_are_the_fixed_schema() -> None:
    err = _reject("SELECT c.track FROM input('f.mkv') f, unnest(f.chapters) c")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "unknown column 'c.track'" in err.message


def test_values_cte_binds_a_row_table_not_a_normal_cte() -> None:
    res = _resolve(
        "COPY (WITH marks(start_t, end_t, title) AS (VALUES (0, 60, 'Intro')) "
        f"{SINK_QUERY}) TO 'x.mkv' WITH (chapters marks)"
    )
    assert list(res.values_ctes) == ["marks"]
    table = res.values_ctes["marks"]
    assert table.columns == ("start_t", "end_t", "title")
    assert len(table.rows) == 1
    assert "marks" not in res.ctes


def test_values_cte_column_order_is_whatever_was_written() -> None:
    res = _resolve(
        "COPY (WITH marks(title, start_t, end_t) AS (VALUES ('Intro', 0, 60)) "
        f"{SINK_QUERY}) TO 'x.mkv' WITH (chapters marks)"
    )
    assert res.values_ctes["marks"].columns == ("title", "start_t", "end_t")


def test_values_cte_cannot_be_selected_from_directly() -> None:
    err = _reject(
        "WITH marks(start_t, end_t, title) AS (VALUES (0, 60, 'Intro')) "
        "SELECT * FROM marks"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "VALUES CTE" in err.message


def test_ordinary_cte_column_renaming_still_stays_rejected() -> None:
    err = _reject("WITH x(a, b) AS (SELECT 1, 2) SELECT * FROM x")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "must be VALUES" in err.message


def test_values_cte_row_arity_must_match_its_column_list() -> None:
    err = _reject(
        "COPY (WITH marks(start_t, end_t, title) AS (VALUES (0, 60)) "
        f"{SINK_QUERY}) TO 'x.mkv' WITH (chapters marks)"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "VALUES row has 2 values" in err.message


def test_values_cte_cell_must_be_a_literal() -> None:
    err = _reject(
        "COPY (WITH marks(start_t, end_t, title) AS (VALUES (0, 1 + 1, 'Intro')) "
        f"{SINK_QUERY}) TO 'x.mkv' WITH (chapters marks)"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "must be a literal" in err.message


def test_values_cte_rejects_duplicate_column_names() -> None:
    err = _reject(
        "COPY (WITH marks(start_t, start_t, title) AS (VALUES (0, 60, 'Intro')) "
        f"{SINK_QUERY}) TO 'x.mkv' WITH (chapters marks)"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "duplicate column name" in err.message


# ---------------------------------------------------------------------------
# arithmetic, casts and <input>.duration in the compile-time value grammar
# ---------------------------------------------------------------------------

_ROWS = "FROM input('x.mp4') f, unnest(f.audio) t"


def test_arithmetic_over_row_columns_types_as_a_number() -> None:
    _resolve(f"SELECT t.track, t.channels * 2 AS chans {_ROWS}")


def test_arithmetic_needs_numbers_on_both_sides() -> None:
    err = _reject(f"SELECT t.track, t.language + 1 AS x {_ROWS}")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "'+' needs numbers, but one side is text" in err.message


def test_arithmetic_result_does_not_join_text() -> None:
    err = _reject(f"SELECT t.track, 'n=' || t.channels + 1 AS x {_ROWS}")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "'||' joins text" in err.message


def test_cast_to_text_bridges_a_number_into_concatenation() -> None:
    _resolve(f"SELECT t.track, 'n=' || t.channels::text AS title {_ROWS}")


def test_cast_function_spelling_is_the_same_cast() -> None:
    _resolve(f"SELECT t.track, 'n=' || CAST(t.channels AS text) AS title {_ROWS}")


def test_only_text_is_a_supported_cast_target() -> None:
    err = _reject(f"SELECT t.track, t.language::int + 1 AS x {_ROWS}")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "cast to int is not supported" in err.message
    assert "::text is the only cast" in (err.hint or "")


def test_a_comparison_still_needs_one_type_across_a_cast() -> None:
    err = _reject(f"SELECT t.track {_ROWS} WHERE t.channels::text = 2")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "a comparison needs one type" in err.message


def test_arithmetic_is_allowed_in_a_row_between_bound() -> None:
    _resolve(f"SELECT t.track {_ROWS} WHERE t.channels BETWEEN 1 AND 4 * 2")


def test_a_trim_bound_may_be_an_expression_over_duration() -> None:
    _resolve("SELECT f.video[1] FROM input('x.mp4') f WHERE f.t <= f.duration - 0.5")


def test_a_bare_duration_is_a_legal_trim_bound() -> None:
    _resolve("SELECT f.video[1] FROM input('x.mp4') f WHERE f.t <= f.duration")


def test_a_text_trim_bound_is_still_rejected() -> None:
    err = _reject("SELECT f.video[1] FROM input('x.mp4') f WHERE f.t <= 'ten'")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "time bounds must be numeric literals" in err.message


def test_a_trim_bound_cannot_read_a_track_row_column() -> None:
    err = _reject(f"SELECT t.track {_ROWS} WHERE f.t <= t.channels")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "cannot mix track-row columns" in err.message


def test_a_row_column_the_schema_never_had_is_still_unknown() -> None:
    err = _reject(f"SELECT t.track, t.nope * 2 AS x {_ROWS}")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "unknown column 't.nope'" in err.message


def test_only_duration_is_readable_off_an_input_alias() -> None:
    err = _reject("SELECT f.video[1] FROM input('x.mp4') f WHERE f.t <= f.width - 1")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "unknown column 'f.width'" in err.message


def test_a_container_tag_types_as_text_in_the_value_grammar() -> None:
    """`f.title` is text, so `||` takes it and arithmetic does not."""
    _resolve(
        "SELECT f.video[1], f.title || ' (restored)' AS title "
        "FROM input('x.mp4') f"
    )
    err = _reject(
        "SELECT f.video[1], f.title + 1 AS title FROM input('x.mp4') f"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL


def test_a_container_tag_is_readable_in_a_case_condition() -> None:
    _resolve(
        "SELECT f.video[1], CASE WHEN f.comment IS NULL THEN 'none' "
        "ELSE f.comment END AS comment FROM input('x.mp4') f"
    )


def test_an_input_column_outside_the_tag_list_is_still_unknown() -> None:
    err = _reject("SELECT f.video[1], f.mood AS mood FROM input('x.mp4') f")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "unknown column 'f.mood'" in err.message
    assert "container tags" in (err.hint or "")


def test_a_query_nested_too_deeply_is_a_plain_rejection() -> None:
    """Deep nesting is user input, not a bug: a typed, non-internal error
    with a plain message, at whichever layer the recursion limit bites."""
    import pytest

    from sqlmpeg.compiler import compile_sql
    from sqlmpeg.errors import ErrorCode, SqlmpegError

    depth = 3000
    query = "SELECT " + "(" * depth + "a.video[1]" + ")" * depth + " FROM input('x.mp4') a"
    with pytest.raises(SqlmpegError) as excinfo:
        compile_sql(query)
    assert excinfo.value.code is not ErrorCode.INTERNAL
    assert "nests too deeply" in excinfo.value.message
    assert "RecursionError" not in excinfo.value.message
