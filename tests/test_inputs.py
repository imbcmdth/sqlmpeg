"""Tests for the input option table (plan 041, RFC-005 SS4).

Guardrail #4: INPUT_OPTIONS is DATA. This file checks the table's shape
against RFC-005's v1 list (name/type per option) and exercises
``validate_option``'s happy/unknown/wrong-type paths -- the input-side mirror
of ``tests/test_sink.py``.
"""

from __future__ import annotations

import pytest

from sqlmpeg.errors import ErrorCode, SqlmpegError
from sqlmpeg.inputs import INPUT_OPTIONS, InputOptionSpec, validate_option

# name -> type, per RFC-005 SS4's v1 table.
_EXPECTED: dict[str, str] = {
    "loop": "bool",
    "stream_loop": "int",
    "framerate": "num",
    "itsoffset": "num",
    "hwaccel": "str",
}


def test_table_has_exactly_five_entries() -> None:
    assert len(INPUT_OPTIONS) == 5
    assert set(INPUT_OPTIONS) == set(_EXPECTED)


@pytest.mark.parametrize("name", sorted(_EXPECTED))
def test_entry_type(name: str) -> None:
    spec = INPUT_OPTIONS[name]
    assert spec.name == name
    assert spec.type == _EXPECTED[name]


def test_all_entries_are_spec_instances() -> None:
    for spec in INPUT_OPTIONS.values():
        assert isinstance(spec, InputOptionSpec)
        assert spec.doc  # non-empty, drives docs/prompt
        assert spec.flag.startswith("-")


def test_loop_renders_as_loop_flag() -> None:
    spec = INPUT_OPTIONS["loop"]
    assert spec.flag == "-loop"
    assert spec.type == "bool"


def test_itsoffset_and_framerate_are_num() -> None:
    assert INPUT_OPTIONS["itsoffset"].type == "num"
    assert INPUT_OPTIONS["framerate"].type == "num"


# ---------------------------------------------------------------------------
# validate_option
# ---------------------------------------------------------------------------


def test_validate_option_happy_bool_true() -> None:
    assert validate_option("loop", True) is True


def test_validate_option_happy_bool_false() -> None:
    assert validate_option("loop", False) is False


def test_validate_option_happy_int() -> None:
    assert validate_option("stream_loop", -1) == -1


def test_validate_option_happy_num_int() -> None:
    assert validate_option("framerate", 15) == 15


def test_validate_option_happy_num_float() -> None:
    assert validate_option("framerate", 29.97) == 29.97


def test_validate_option_happy_num_negative() -> None:
    """itsoffset legitimately takes a negative offset."""
    assert validate_option("itsoffset", -1) == -1
    assert validate_option("itsoffset", -1.5) == -1.5


def test_validate_option_happy_str() -> None:
    assert validate_option("hwaccel", "cuda") == "cuda"


def test_validate_option_unknown_raises() -> None:
    with pytest.raises(SqlmpegError) as excinfo:
        validate_option("bogus_option", "x")
    err = excinfo.value
    assert err.code == ErrorCode.UNKNOWN_INPUT_OPTION
    assert "bogus_option" in err.message


def test_validate_option_unknown_did_you_mean_hint() -> None:
    with pytest.raises(SqlmpegError) as excinfo:
        validate_option("loob", True)
    err = excinfo.value
    assert err.code == ErrorCode.UNKNOWN_INPUT_OPTION
    assert err.hint is not None
    assert "loop" in err.hint


def test_validate_option_unknown_no_close_match_lists_known() -> None:
    with pytest.raises(SqlmpegError) as excinfo:
        validate_option("zzzzzzzzzz", "x")
    err = excinfo.value
    assert err.hint is not None
    assert "known options:" in err.hint
    for name in INPUT_OPTIONS:
        assert name in err.hint


def test_validate_option_bool_rejects_str() -> None:
    with pytest.raises(SqlmpegError) as excinfo:
        validate_option("loop", "true")
    assert excinfo.value.code == ErrorCode.INPUT_OPTION_TYPE


def test_validate_option_bool_rejects_int() -> None:
    with pytest.raises(SqlmpegError) as excinfo:
        validate_option("loop", 1)
    assert excinfo.value.code == ErrorCode.INPUT_OPTION_TYPE


def test_validate_option_int_rejects_float() -> None:
    with pytest.raises(SqlmpegError) as excinfo:
        validate_option("stream_loop", 1.5)
    assert excinfo.value.code == ErrorCode.INPUT_OPTION_TYPE


def test_validate_option_int_rejects_bool() -> None:
    # bool is a subclass of int in Python; the table must not accept it
    # where an int is declared.
    with pytest.raises(SqlmpegError) as excinfo:
        validate_option("stream_loop", True)
    assert excinfo.value.code == ErrorCode.INPUT_OPTION_TYPE


def test_validate_option_num_rejects_str() -> None:
    with pytest.raises(SqlmpegError) as excinfo:
        validate_option("framerate", "fast")
    err = excinfo.value
    assert err.code == ErrorCode.INPUT_OPTION_TYPE
    assert "expects a number" in err.message


def test_validate_option_num_rejects_bool() -> None:
    with pytest.raises(SqlmpegError) as excinfo:
        validate_option("itsoffset", True)
    assert excinfo.value.code == ErrorCode.INPUT_OPTION_TYPE


def test_validate_option_str_rejects_bool() -> None:
    with pytest.raises(SqlmpegError) as excinfo:
        validate_option("hwaccel", True)
    assert excinfo.value.code == ErrorCode.INPUT_OPTION_TYPE


def test_validate_option_str_rejects_int() -> None:
    with pytest.raises(SqlmpegError) as excinfo:
        validate_option("hwaccel", 5)
    assert excinfo.value.code == ErrorCode.INPUT_OPTION_TYPE


def test_validate_option_preserves_line_col() -> None:
    with pytest.raises(SqlmpegError) as excinfo:
        validate_option("bogus", "x", line=3, col=12)
    err = excinfo.value
    assert err.line == 3
    assert err.col == 12
