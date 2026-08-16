"""Tests for the CLI (plan 008).

Invokes ``main([...])`` directly (no subprocess) and asserts on captured
stdout/stderr via pytest's ``capsys``, plus the process exit code returned
by ``main``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sqlmpeg import cli
from sqlmpeg.ir import Graph, Output, Sink

VALID_QUERY = "SELECT scale(a.frame, 0.5) FROM input('x.mp4') a"
BAD_QUERY = "SELECT nope(a.frame) FROM input('x.mp4') a"


def _write_sql(tmp_path: Path, text: str, name: str = "query.sql") -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _sinked_graph(path: str, options: dict[str, object] | None = None) -> Graph:
    """A hand-built, already-sinked Graph.

    ``compile_sql`` cannot yet produce a ``Graph`` with a sink -- COPY ... TO
    parsing/lowering is plan 026, being written concurrently with this file.
    Sink-path-resolution tests below monkeypatch ``cli.compile_sql`` to
    return this instead of compiling real SQL.
    """
    return Graph(
        input_paths=["x.mp4"],
        sources={"a": 0},
        nodes={},
        outputs=[Output(ref="src:a:v:0", type="video", name=None, metadata={})],
        sink=Sink(path=path, options=dict(options or {})),
    )


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


def test_compile_no_probe(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    query = _write_sql(tmp_path, VALID_QUERY)
    code = cli.main(["compile", "--no-probe", query])
    out = capsys.readouterr().out
    assert code == 0
    assert "-filter_complex" in out


def test_compile_uses_sink_path_when_no_dash_o(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    query = _write_sql(tmp_path, VALID_QUERY)
    monkeypatch.setattr(
        cli, "compile_sql", lambda text, probe=True, portable=False: _sinked_graph("sink.mkv")
    )
    code = cli.main(["compile", query])
    out = capsys.readouterr().out
    assert code == 0
    assert "sink.mkv" in out
    assert "out.mp4" not in out


def test_compile_dash_o_overrides_sink_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    query = _write_sql(tmp_path, VALID_QUERY)
    monkeypatch.setattr(
        cli, "compile_sql", lambda text, probe=True, portable=False: _sinked_graph("sink.mkv")
    )
    code = cli.main(["compile", query, "-o", "override.mp4"])
    out = capsys.readouterr().out
    assert code == 0
    assert "override.mp4" in out
    assert "sink.mkv" not in out


def test_compile_no_probe_skips_probing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--no-probe must never touch the filesystem via probe()."""
    query = _write_sql(tmp_path, VALID_QUERY)

    def _boom(path: str) -> None:
        raise AssertionError("probe() should not be called with --no-probe")

    monkeypatch.setattr("sqlmpeg.compiler.probe_path", _boom)
    code = cli.main(["compile", "--no-probe", query])
    assert code == 0
    capsys.readouterr()


# ---------------------------------------------------------------------------
# --portable (compile/explain/validate only, RFC-003 / plan 032)
# ---------------------------------------------------------------------------

# A tier-2-only call: no stdlib entry named `unsharp` exists, so this is
# UNKNOWN_FUNCTION under --portable regardless of whether ffmpeg happens to
# be installed on the machine running the test -- --portable never even
# constructs the registry.
DYNAMIC_QUERY = "SELECT unsharp(a.frame, luma_amount => 1.5) FROM input('x.mp4') a"


def test_compile_portable_rejects_a_dynamic_filter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    query = _write_sql(tmp_path, DYNAMIC_QUERY)
    code = cli.main(["compile", "--portable", query])
    captured = capsys.readouterr()
    assert code == 1
    assert "UNKNOWN_FUNCTION" in captured.err


def test_compile_portable_still_accepts_the_stdlib(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    query = _write_sql(tmp_path, VALID_QUERY)
    code = cli.main(["compile", "--portable", query])
    out = capsys.readouterr().out
    assert code == 0
    assert "-filter_complex" in out


def test_explain_portable_rejects_a_dynamic_filter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    query = _write_sql(tmp_path, DYNAMIC_QUERY)
    code = cli.main(["explain", "--portable", query])
    captured = capsys.readouterr()
    assert code == 1
    assert "UNKNOWN_FUNCTION" in captured.err


def test_validate_portable_rejects_a_dynamic_filter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    query = _write_sql(tmp_path, DYNAMIC_QUERY)
    code = cli.main(["validate", "--json", "--portable", query])
    captured = capsys.readouterr()
    assert code == 1

    import json

    data = json.loads(captured.out)
    assert data["code"] == "UNKNOWN_FUNCTION"


def test_run_has_no_portable_flag(tmp_path: Path) -> None:
    """--portable is compile/explain/validate only -- run always needs ffmpeg
    to execute against, so there is no offline escape hatch for it."""
    query = _write_sql(tmp_path, VALID_QUERY)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["run", query, "--portable"])
    assert exc_info.value.code == 2


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


def test_explain_no_probe(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    query = _write_sql(tmp_path, VALID_QUERY)
    code = cli.main(["explain", "--no-probe", query])
    out = capsys.readouterr().out
    assert code == 0

    import json

    data = json.loads(out)
    graph = Graph.from_dict(data)
    assert graph.input_paths == ["x.mp4"]


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


def test_validate_no_probe_success_is_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    query = _write_sql(tmp_path, VALID_QUERY)
    code = cli.main(["validate", "--no-probe", query])
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


def test_run_uses_sink_path_when_no_dash_o(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """compile_sql is monkeypatched -- see `_sinked_graph`'s docstring."""
    query = _write_sql(tmp_path, VALID_QUERY)
    monkeypatch.setattr(cli, "compile_sql", lambda text: _sinked_graph("sink.mkv"))
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    code = cli.main(["run", query])
    captured = capsys.readouterr()
    # Reaching the ffmpeg-not-found check (rather than the exit-2 usage error
    # below) proves -o was resolved from the sink path and execution proceeded.
    assert code == 1
    assert "ffmpeg" in captured.err
    assert "not found" in captured.err


def test_run_dash_o_overrides_sink_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    query = _write_sql(tmp_path, VALID_QUERY)
    out_path = str(tmp_path / "override.mp4")
    monkeypatch.setattr(cli, "compile_sql", lambda text: _sinked_graph("sink.mkv"))
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    code = cli.main(["run", query, "-o", out_path])
    captured = capsys.readouterr()
    assert code == 1
    assert "not found" in captured.err


def test_run_no_output_and_no_sink_is_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    query = _write_sql(tmp_path, VALID_QUERY)
    code = cli.main(["run", query])
    captured = capsys.readouterr()
    assert code == 2
    assert "no output path" in captured.err


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
