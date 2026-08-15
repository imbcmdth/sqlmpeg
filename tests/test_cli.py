"""Tests for the CLI (plan 008).

Invokes ``main([...])`` directly (no subprocess) and asserts on captured
stdout/stderr via pytest's ``capsys``, plus the process exit code returned
by ``main``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sqlmpeg import cli
from sqlmpeg.ir import Graph

VALID_QUERY = "SELECT scale(a.frame, 0.5) FROM input('x.mp4') a"
BAD_QUERY = "SELECT nope(a.frame) FROM input('x.mp4') a"


def _write_sql(tmp_path: Path, text: str, name: str = "query.sql") -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# compile
# ---------------------------------------------------------------------------


def test_compile_happy_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    query = _write_sql(tmp_path, VALID_QUERY)
    code = cli.main(["compile", query])
    out = capsys.readouterr().out
    assert code == 0
    assert "-filter_complex" in out
    assert "ffmpeg" in out
    assert "out.mp4" in out


def test_compile_graph_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    query = _write_sql(tmp_path, VALID_QUERY)
    code = cli.main(["compile", "--graph-only", query])
    out = capsys.readouterr().out
    assert code == 0
    assert "-filter_complex" not in out
    assert "scale=" in out


def test_compile_custom_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    query = _write_sql(tmp_path, VALID_QUERY)
    code = cli.main(["compile", query, "-o", "result.mkv"])
    out = capsys.readouterr().out
    assert code == 0
    assert "result.mkv" in out


def test_compile_bad_query_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    query = _write_sql(tmp_path, BAD_QUERY)
    code = cli.main(["compile", query])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "UNKNOWN_FUNCTION" in captured.err


def test_compile_missing_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["compile", str(tmp_path / "nope.sql")])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "error:" in captured.err


def test_compile_stdin(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import io

    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(VALID_QUERY))
    code = cli.main(["compile", "-"])
    out = capsys.readouterr().out
    assert code == 0
    assert "-filter_complex" in out


# ---------------------------------------------------------------------------
# explain
# ---------------------------------------------------------------------------


def test_explain_round_trips_graph(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    query = _write_sql(tmp_path, VALID_QUERY)
    code = cli.main(["explain", query])
    out = capsys.readouterr().out
    assert code == 0

    import json

    data = json.loads(out)
    graph = Graph.from_dict(data)
    assert graph.input_paths == ["x.mp4"]
    assert graph.to_dict() == data


def test_explain_bad_query(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    query = _write_sql(tmp_path, BAD_QUERY)
    code = cli.main(["explain", query])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "error:" in captured.err


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_validate_success_is_silent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    query = _write_sql(tmp_path, VALID_QUERY)
    code = cli.main(["validate", query])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == ""
    assert captured.err == ""


def test_validate_bad_query_human(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    query = _write_sql(tmp_path, BAD_QUERY)
    code = cli.main(["validate", query])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err.startswith("error: ")


def test_validate_bad_query_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    query = _write_sql(tmp_path, BAD_QUERY)
    code = cli.main(["validate", "--json", query])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err == ""

    import json

    data = json.loads(captured.out)
    assert data["code"] == "UNKNOWN_FUNCTION"
    assert set(data.keys()) == {"line", "col", "code", "message", "hint"}


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def test_run_ffmpeg_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    query = _write_sql(tmp_path, VALID_QUERY)
    out_path = str(tmp_path / "out.mp4")
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    code = cli.main(["run", query, "-o", out_path])
    captured = capsys.readouterr()
    assert code == 1
    assert "ffmpeg" in captured.err
    assert "not found" in captured.err


def test_run_bad_query_never_checks_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    query = _write_sql(tmp_path, BAD_QUERY)
    out_path = str(tmp_path / "out.mp4")

    def _boom(name: str) -> str | None:
        raise AssertionError("should not check for ffmpeg before compiling")

    monkeypatch.setattr(cli.shutil, "which", _boom)
    code = cli.main(["run", query, "-o", out_path])
    captured = capsys.readouterr()
    assert code == 1
    assert "error:" in captured.err


def test_run_missing_output_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    query = _write_sql(tmp_path, VALID_QUERY)
    out_path = str(tmp_path / "does_not_exist" / "out.mp4")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    code = cli.main(["run", query, "-o", out_path])
    captured = capsys.readouterr()
    assert code == 1
    assert "does not exist" in captured.err


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------


def test_no_subcommand_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main([])
    assert code == 2


def test_unknown_subcommand_exits_2() -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["bogus"])
    assert exc_info.value.code == 2
