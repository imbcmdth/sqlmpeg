from __future__ import annotations

import pytest
from sqlglot import exp

from sqlmpeg.errors import ErrorCode, SqlmpegError
from sqlmpeg.parser import Resolved, parse, resolve, subscript_index, union_branches

README_SQL = """WITH pip AS (
  SELECT scale(crop(b.frame, 1200, 50, 600, 200), 0.5) AS frame
  FROM input('game.mp4') b
)
SELECT overlay(a.frame, pip.frame, 20, 20)
FROM input('game.mp4') a, pip
"""


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


@pytest.mark.parametrize("sql", ["SELECT * FROM input('x') a", "SELECT a.* FROM input('x') a"])
def test_select_star(sql: str) -> None:
    err = _reject(sql)
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert err.hint is not None and "frame expression" in err.hint


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
        "SELECT a.subtitle[1] FROM input('x') a",
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
