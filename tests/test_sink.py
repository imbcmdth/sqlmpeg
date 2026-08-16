"""Tests for the sink option table (plan 025, RFC-002).

Guardrail #4: SINK_OPTIONS is DATA. This file checks the table's shape
against RFC-002's v1 list (name/scope/type per option) and exercises
``validate_option``'s happy/unknown/wrong-type paths.
"""

from __future__ import annotations

import pytest

from sqlmpeg.errors import ErrorCode, SqlmpegError
from sqlmpeg.sink import SINK_OPTIONS, SinkOptionSpec, validate_option

# name -> (scope, type) per RFC-002's v1 table.
_EXPECTED: dict[str, tuple[str, str]] = {
    "video_codec": ("video", "str"),
    "audio_codec": ("audio", "str"),
    "crf": ("video", "int"),
    "preset": ("video", "str"),
    "pix_fmt": ("video", "str"),
    "video_bitrate": ("video", "str"),
    "audio_bitrate": ("audio", "str"),
    "sample_rate": ("audio", "int"),
    "format": ("container", "str"),
    "faststart": ("container", "bool"),
}


def test_table_has_exactly_ten_entries() -> None:
    assert len(SINK_OPTIONS) == 10
    assert set(SINK_OPTIONS) == set(_EXPECTED)


@pytest.mark.parametrize("name", sorted(_EXPECTED))
def test_entry_scope_and_type(name: str) -> None:
    spec = SINK_OPTIONS[name]
    expected_scope, expected_type = _EXPECTED[name]
    assert spec.name == name
    assert spec.scope == expected_scope
    assert spec.type == expected_type


def test_all_entries_are_spec_instances() -> None:
    for spec in SINK_OPTIONS.values():
        assert isinstance(spec, SinkOptionSpec)
        assert spec.doc  # non-empty, drives docs/prompt
        assert spec.flag.startswith("-")


def test_faststart_renders_as_movflags_bool() -> None:
    spec = SINK_OPTIONS["faststart"]
    assert spec.flag == "-movflags"
    assert spec.value_template == "+faststart"
    assert spec.per_stream is False


def test_format_is_not_per_stream() -> None:
    spec = SINK_OPTIONS["format"]
    assert spec.flag == "-f"
    assert spec.per_stream is False


def test_per_stream_options_use_colon_index_flags() -> None:
    per_stream_names = {n for n, s in SINK_OPTIONS.items() if s.per_stream}
    assert per_stream_names == {
        "video_codec",
        "audio_codec",
        "crf",
        "preset",
        "pix_fmt",
        "video_bitrate",
        "audio_bitrate",
        "sample_rate",
    }


# ---------------------------------------------------------------------------
# validate_option
# ---------------------------------------------------------------------------


def test_validate_option_happy_str() -> None:
    assert validate_option("video_codec", "libx264") == "libx264"


def test_validate_option_happy_int() -> None:
    assert validate_option("crf", 20) == 20


def test_validate_option_happy_bool_true() -> None:
    assert validate_option("faststart", True) is True


def test_validate_option_happy_bool_false() -> None:
    assert validate_option("faststart", False) is False


def test_validate_option_unknown_raises() -> None:
    with pytest.raises(SqlmpegError) as excinfo:
        validate_option("bogus_option", "x")
    err = excinfo.value
    assert err.code == ErrorCode.UNKNOWN_SINK_OPTION
    assert "bogus_option" in err.message


def test_validate_option_unknown_did_you_mean_hint() -> None:
    with pytest.raises(SqlmpegError) as excinfo:
        validate_option("video_codc", "libx264")
    err = excinfo.value
    assert err.code == ErrorCode.UNKNOWN_SINK_OPTION
    assert err.hint is not None
    assert "video_codec" in err.hint


def test_validate_option_unknown_no_close_match_lists_known() -> None:
    with pytest.raises(SqlmpegError) as excinfo:
        validate_option("zzzzzzzzzz", "x")
    err = excinfo.value
    assert err.hint is not None
    assert "known options:" in err.hint
    for name in SINK_OPTIONS:
        assert name in err.hint


def test_validate_option_wrong_type_str_expected() -> None:
    with pytest.raises(SqlmpegError) as excinfo:
        validate_option("video_codec", 5)
    err = excinfo.value
    assert err.code == ErrorCode.SINK_OPTION_TYPE
    assert "video_codec" in err.message


def test_validate_option_wrong_type_int_expected() -> None:
    with pytest.raises(SqlmpegError) as excinfo:
        validate_option("crf", "20")
    assert excinfo.value.code == ErrorCode.SINK_OPTION_TYPE


def test_validate_option_int_rejects_float() -> None:
    with pytest.raises(SqlmpegError) as excinfo:
        validate_option("crf", 20.5)
    assert excinfo.value.code == ErrorCode.SINK_OPTION_TYPE


def test_validate_option_int_rejects_bool() -> None:
    # bool is a subclass of int in Python; the table must not accept it
    # where an int is declared.
    with pytest.raises(SqlmpegError) as excinfo:
        validate_option("crf", True)
    assert excinfo.value.code == ErrorCode.SINK_OPTION_TYPE


def test_validate_option_bool_rejects_str() -> None:
    with pytest.raises(SqlmpegError) as excinfo:
        validate_option("faststart", "true")
    assert excinfo.value.code == ErrorCode.SINK_OPTION_TYPE


def test_validate_option_bool_rejects_int() -> None:
    with pytest.raises(SqlmpegError) as excinfo:
        validate_option("faststart", 1)
    assert excinfo.value.code == ErrorCode.SINK_OPTION_TYPE


def test_validate_option_str_rejects_bool() -> None:
    with pytest.raises(SqlmpegError) as excinfo:
        validate_option("format", True)
    assert excinfo.value.code == ErrorCode.SINK_OPTION_TYPE


def test_validate_option_preserves_line_col() -> None:
    with pytest.raises(SqlmpegError) as excinfo:
        validate_option("bogus", "x", line=3, col=12)
    err = excinfo.value
    assert err.line == 3
    assert err.col == 12
