"""Tests for sqlmpeg.vars.substitute -- psql-style CLI variables (plan 069)."""

from __future__ import annotations

import pytest

from sqlmpeg.errors import ErrorCode, SqlmpegError
from sqlmpeg.vars import substitute


def test_quoted_string_form() -> None:
    assert substitute(":'name'", {"name": "film.mkv"}) == "'film.mkv'"


def test_quoted_identifier_form() -> None:
    assert substitute(':"name"', {"name": "col"}) == '"col"'


def test_bare_raw_form() -> None:
    assert substitute("crf :name", {"name": "20"}) == "crf 20"


def test_quote_doubling_in_string_form() -> None:
    assert substitute(":'name'", {"name": "O'Brien"}) == "'O''Brien'"


def test_quote_doubling_in_identifier_form() -> None:
    assert substitute(':"name"', {"name": 'a"b'}) == '"a""b"'


def test_double_colon_cast_untouched() -> None:
    assert substitute("x::int", {}) == "x::int"


def test_double_colon_cast_even_with_matching_variable_name() -> None:
    # `::int` must never be read as a reference to a variable named `int`.
    assert substitute("x::int", {"int": "nope"}) == "x::int"


def test_lone_colon_before_digit_untouched() -> None:
    # `:5` has no identifier at all -- not `1` variables `0:x` style timestamps.
    assert substitute("00:5", {}) == "00:5"


def test_lone_trailing_colon_untouched() -> None:
    assert substitute("select 1:", {}) == "select 1:"


def test_lone_colon_before_space_untouched() -> None:
    assert substitute("a: b", {}) == "a: b"


def test_var_inside_single_quoted_string_untouched() -> None:
    assert substitute("SELECT ':name'", {"name": "x"}) == "SELECT ':name'"


def test_var_inside_double_quoted_identifier_untouched() -> None:
    assert substitute('SELECT ":name"', {"name": "x"}) == 'SELECT ":name"'


def test_var_inside_line_comment_untouched() -> None:
    text = "SELECT 1 -- :name\nFROM t"
    assert substitute(text, {"name": "x"}) == text


def test_var_inside_block_comment_untouched() -> None:
    text = "SELECT /* :name */ 1"
    assert substitute(text, {"name": "x"}) == text


def test_adjacent_bare_references() -> None:
    assert substitute(":a:b", {"a": "1", "b": "2"}) == "12"


def test_adjacent_quoted_references() -> None:
    assert substitute(":'a':'b'", {"a": "x", "b": "y"}) == "'x''y'"


def test_empty_value_bare() -> None:
    assert substitute(":name", {"name": ""}) == ""


def test_empty_value_quoted() -> None:
    assert substitute(":'name'", {"name": ""}) == "''"


def test_missing_variable_line_col() -> None:
    with pytest.raises(SqlmpegError) as exc_info:
        substitute("SELECT 1\nFROM input(:'source')", {})
    err = exc_info.value
    assert err.code == ErrorCode.UNSUPPORTED_SQL
    assert err.line == 2
    assert err.col == 12
    assert "source" in err.message


def test_missing_variable_hint_lists_defined_names() -> None:
    with pytest.raises(SqlmpegError) as exc_info:
        substitute(":dest", {"source": "a", "other": "b"})
    hint = exc_info.value.hint
    assert hint is not None
    assert "source" in hint and "other" in hint


def test_missing_variable_hint_when_none_defined() -> None:
    with pytest.raises(SqlmpegError) as exc_info:
        substitute(":dest", {})
    assert exc_info.value.hint == "define it with -v name=value"


def test_no_variables_no_references_is_a_no_op() -> None:
    text = "SELECT f.video[1] FROM input('film.mkv') f"
    assert substitute(text, {}) == text
