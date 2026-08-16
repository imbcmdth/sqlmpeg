from __future__ import annotations

import pytest
from sqlglot import exp

from sqlmpeg.errors import ErrorCode, SqlmpegError
from sqlmpeg.parser import (
    RawInputOption,
    Resolved,
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
# resolve — open-ended time windows (plan 039)
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
# resolve — multiple projections (RFC-001: SELECT list = output stream list)
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
# resolve — SELECT * / <alias>.* (RFC-004)
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


# ---------------------------------------------------------------------------
# resolve — subtitle / data pseudo-columns (RFC-004)
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
# input() named options (RFC-005 SS4, plan 041)
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
        "WITH c AS (SELECT a.frame FROM input('x') a) SELECT z.frame FROM c z",
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
# named arguments — SHAPE only (RFC-003, plan 031)
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
# COPY ... TO ... WITH (...)  — the sink wrapper (RFC-002, plan 026)
# ---------------------------------------------------------------------------


def test_bare_select_has_no_sink() -> None:
    assert _resolve(SINK_QUERY).sink is None


def test_copy_populates_the_sink() -> None:
    res = _resolve(f"COPY ({SINK_QUERY}) TO 'out.mkv' WITH (video_codec 'libx264', crf 20)")
    sink = res.sink
    assert sink is not None
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
    sink = _resolve(f"COPY ({SINK_QUERY}) TO 'out.mkv'").sink
    assert sink is not None
    assert sink.options == ()


def test_copy_with_empty_option_list_has_no_options() -> None:
    sink = _resolve(f"COPY ({SINK_QUERY}) TO 'out.mkv' WITH ()").sink
    assert sink is not None
    assert sink.options == ()


def test_copy_option_names_are_folded_lowercase() -> None:
    """sqlglot drops the quoting of an option name, so "CRF" folds like CRF."""
    for written in ("CRF 20", '"CRF" 20'):
        sink = _resolve(f"COPY ({SINK_QUERY}) TO 'out.mkv' WITH ({written})").sink
        assert sink is not None
        assert [option.name for option in sink.options] == ["crf"]


def test_copy_wraps_a_cte_query() -> None:
    res = _resolve(
        "COPY (WITH c AS (SELECT a.frame AS f FROM input('x.mp4') a) "
        "SELECT c.f FROM c) TO 'out.mkv'"
    )
    assert list(res.ctes) == ["c"]
    assert res.sink is not None and res.sink.path == "out.mkv"


def test_copy_wraps_a_union_all() -> None:
    res = _resolve(
        "COPY (SELECT a.frame FROM input('x') a UNION ALL "
        "SELECT b.frame FROM input('y') b) TO 'out.mkv'"
    )
    assert len(res.branches) == 2
    assert res.sink is not None


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
        # several statements
        f"COPY ({SINK_QUERY}) TO 'a.mkv'; COPY ({SINK_QUERY}) TO 'b.mkv'",
    ],
)
def test_bad_copy_is_rejected(sql: str) -> None:
    assert _reject(sql).code is ErrorCode.UNSUPPORTED_SQL


def test_copy_from_names_the_supported_form() -> None:
    err = _reject("COPY t FROM 'in.csv'")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert err.hint is not None and "COPY (<query>) TO" in err.hint


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
