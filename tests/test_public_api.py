"""Tests for the top-level ``sqlmpeg`` package: version and public API.

``sqlmpeg.__version__`` and ``sqlmpeg.__all__`` are the library's front page;
these tests check the front page matches the real package metadata and the
real source-module objects, not a hand-copied restatement.
"""

from __future__ import annotations

import importlib
import importlib.metadata

import sqlmpeg


def test_version_matches_installed_metadata() -> None:
    assert sqlmpeg.__version__ == importlib.metadata.version("sqlmpeg")


def test_all_is_sorted_and_complete() -> None:
    expected = {
        "compile_sql",
        "compile_commands",
        "compile_table_sql",
        "classify",
        "SqlmpegError",
        "ErrorCode",
        "emit",
        "build_ffmpeg_commands",
        "build_system_prompt",
    }
    assert set(sqlmpeg.__all__) == expected
    assert sqlmpeg.__all__ == sorted(sqlmpeg.__all__)


def test_exports_are_the_source_module_objects() -> None:
    compiler = importlib.import_module("sqlmpeg.compiler")
    errors = importlib.import_module("sqlmpeg.errors")
    emit_module = importlib.import_module("sqlmpeg.emit")
    prompt_module = importlib.import_module("sqlmpeg.prompt")

    assert sqlmpeg.compile_sql is compiler.compile_sql
    assert sqlmpeg.compile_commands is compiler.compile_commands
    assert sqlmpeg.compile_table_sql is compiler.compile_table_sql
    assert sqlmpeg.classify is compiler.classify
    assert sqlmpeg.SqlmpegError is errors.SqlmpegError
    assert sqlmpeg.ErrorCode is errors.ErrorCode
    assert sqlmpeg.emit is emit_module.emit
    assert sqlmpeg.build_ffmpeg_commands is emit_module.build_ffmpeg_commands
    assert sqlmpeg.build_system_prompt is prompt_module.build_system_prompt
