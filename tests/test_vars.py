"""Tests for sqlmpeg.vars -- psql-style CLI variables, unset meaning NULL."""

from __future__ import annotations

from sqlmpeg.errors import ErrorCode, SqlmpegError
from sqlmpeg.vars import referenced, substitute, unset_error, unset_variable


def test_quoted_string_form() -> None:
    assert substitute(":'name'", {"name": "film.mkv"}).text == "'film.mkv'"


def test_quoted_identifier_form() -> None:
    assert substitute(':"name"', {"name": "col"}).text == '"col"'


def test_bare_raw_form() -> None:
    assert substitute("crf :name", {"name": "20"}).text == "crf 20"


def test_quote_doubling_in_string_form() -> None:
    assert substitute(":'name'", {"name": "O'Brien"}).text == "'O''Brien'"


def test_quote_doubling_in_identifier_form() -> None:
    assert substitute(':"name"', {"name": 'a"b'}).text == '"a""b"'


def test_double_colon_cast_untouched() -> None:
    assert substitute("x::int", {}).text == "x::int"


def test_double_colon_cast_even_with_matching_variable_name() -> None:
    # `::int` must never be read as a reference to a variable named `int`.
    assert substitute("x::int", {"int": "nope"}).text == "x::int"


def test_lone_colon_before_digit_untouched() -> None:
    # `:5` has no identifier at all -- not `1` variables `0:x` style timestamps.
    assert substitute("00:5", {}).text == "00:5"


def test_lone_trailing_colon_untouched() -> None:
    assert substitute("select 1:", {}).text == "select 1:"


def test_lone_colon_before_space_untouched() -> None:
    assert substitute("a: b", {}).text == "a: b"


def test_var_inside_single_quoted_string_untouched() -> None:
    assert substitute("SELECT ':name'", {"name": "x"}).text == "SELECT ':name'"


def test_var_inside_double_quoted_identifier_untouched() -> None:
    assert substitute('SELECT ":name"', {"name": "x"}).text == 'SELECT ":name"'


def test_var_inside_line_comment_untouched() -> None:
    text = "SELECT 1 -- :name\nFROM t"
    assert substitute(text, {"name": "x"}).text == text


def test_var_inside_block_comment_untouched() -> None:
    text = "SELECT /* :name */ 1"
    assert substitute(text, {"name": "x"}).text == text


def test_adjacent_bare_references() -> None:
    assert substitute(":a:b", {"a": "1", "b": "2"}).text == "12"


def test_adjacent_quoted_references() -> None:
    assert substitute(":'a':'b'", {"a": "x", "b": "y"}).text == "'x''y'"


def test_empty_value_bare() -> None:
    assert substitute(":name", {"name": ""}).text == ""


def test_empty_value_quoted() -> None:
    assert substitute(":'name'", {"name": ""}).text == "''"


def test_set_reference_records_no_unset_entry() -> None:
    assert substitute(":'name'", {"name": "x"}).unset == {}


# -- unset means NULL ------------------------------------------------------


def test_unset_quoted_string_form_is_null() -> None:
    sub = substitute("input(:'source')", {})
    assert sub.text == "input(NULL)"
    assert sub.unset == {(1, 7): "source"}


def test_unset_quoted_identifier_form_is_null() -> None:
    assert substitute(':"name"', {}).text == "NULL"


def test_unset_bare_form_is_null() -> None:
    sub = substitute("crf :crf", {})
    assert sub.text == "crf NULL"
    assert sub.unset == {(1, 5): "crf"}


def test_unset_map_is_positioned_in_the_substituted_text() -> None:
    # An earlier substitution changes the offsets; the map tracks the OUTPUT.
    sub = substitute("SELECT :'a',\n:'b'", {"a": "a-very-long-value"})
    assert sub.text == "SELECT 'a-very-long-value',\nNULL"
    assert sub.unset == {(2, 1): "b"}


def test_two_unset_references_both_recorded() -> None:
    sub = substitute(":w :h", {})
    assert sub.text == "NULL NULL"
    assert sub.unset == {(1, 1): "w", (1, 6): "h"}


def test_no_variables_no_references_is_a_no_op() -> None:
    text = "SELECT f.video[1] FROM input('film.mkv') f"
    sub = substitute(text, {})
    assert sub.text == text
    assert sub.unset == {}


# -- referenced ------------------------------------------------------------


def test_referenced_reports_every_form() -> None:
    text = "SELECT :'a', :\"b\", :c FROM t"
    assert referenced(text) == {"a", "b", "c"}


def test_referenced_skips_strings_identifiers_and_comments() -> None:
    text = "SELECT ':a', \":b\" -- :c\n/* :d */ FROM t"
    assert referenced(text) == set()


def test_referenced_skips_casts() -> None:
    assert referenced("x::int") == set()


def test_referenced_of_plain_text_is_empty() -> None:
    assert referenced("SELECT 1") == set()


# -- the unset rejection, built and read back ------------------------------


def test_unset_error_shape() -> None:
    err = unset_error(
        ErrorCode.UNSUPPORTED_SQL, "source", what="input() needs a path", line=2, col=12
    )
    assert err.message == "':source' was not set"
    assert err.line == 2 and err.col == 12
    assert err.hint == "input() needs a path; set it with -v source=<value>"


def test_unset_variable_reads_the_name_back() -> None:
    err = unset_error(ErrorCode.UDF_ARG_TYPE, "clip", what="x", line=1, col=1)
    assert unset_variable(err) == "clip"


def test_unset_variable_is_none_for_other_errors() -> None:
    err = SqlmpegError(ErrorCode.UNSUPPORTED_SQL, "something else", line=1, col=1)
    assert unset_variable(err) is None
