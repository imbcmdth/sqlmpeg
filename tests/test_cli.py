"""Tests for the CLI (plan 008; plan 037 for the SQL-string-is-default convention).

Invokes ``main([...])`` directly (no subprocess) and asserts on captured
stdout/stderr via pytest's ``capsys``, plus the process exit code returned
by ``main``.

Convention (plan 037): ``compile``/``explain``/``validate``/``run`` take the
query as an inline SQL string by default; ``-f/--file`` reads it from a file
instead ('-' for stdin). Tests below use the inline positional wherever the
file itself isn't under test, and ``-f`` (with a real file, or '-' for
stdin) where file-reading behavior is the point.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sqlmpeg import cli
from sqlmpeg.ir import Graph, Output, SinkUnit

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
    return _multi_sink_graph((path, dict(options or {})))


def _multi_sink_graph(*sinks: tuple[str, dict[str, object]]) -> Graph:
    """One passthrough output per COPY, all reading the same input stream.

    RFC-006's cross-group passthrough shape, hand-built: two groups may map
    the same source stream without a split (see `sqlmpeg.split._exempt_refs`).
    """
    return Graph(
        input_paths=["x.mp4"],
        sources={"a": 0},
        nodes={},
        sinks=[
            SinkUnit(
                outputs=[Output(ref="src:a:v:0", type="video", name=None, metadata={})],
                path=path,
                options=dict(options),
            )
            for path, options in sinks
        ],
    )


# ---------------------------------------------------------------------------
# compile
# ---------------------------------------------------------------------------


def test_compile_happy_path(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["compile", VALID_QUERY])
    out = capsys.readouterr().out
    assert code == 0
    assert "-filter_complex" in out
    assert "ffmpeg" in out
    assert "out.mp4" in out


def test_compile_graph_only(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["compile", "--graph-only", VALID_QUERY])
    out = capsys.readouterr().out
    assert code == 0
    assert "-filter_complex" not in out
    assert "scale=" in out


def test_compile_custom_output(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["compile", VALID_QUERY, "-o", "result.mkv"])
    out = capsys.readouterr().out
    assert code == 0
    assert "result.mkv" in out


def test_compile_bad_query_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["compile", BAD_QUERY])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "UNKNOWN_FUNCTION" in captured.err


def test_compile_file_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["compile", "-f", str(tmp_path / "nope.sql")])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "error:" in captured.err


def test_compile_file_happy_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    query = _write_sql(tmp_path, VALID_QUERY)
    code = cli.main(["compile", "-f", query])
    out = capsys.readouterr().out
    assert code == 0
    assert "-filter_complex" in out


def test_compile_file_dash_reads_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io

    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(VALID_QUERY))
    code = cli.main(["compile", "-f", "-"])
    out = capsys.readouterr().out
    assert code == 0
    assert "-filter_complex" in out


def test_compile_no_probe(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["compile", "--no-probe", VALID_QUERY])
    out = capsys.readouterr().out
    assert code == 0
    assert "-filter_complex" in out


def test_compile_uses_sink_path_when_no_dash_o(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "compile_sql", lambda text, probe=True, portable=False: _sinked_graph("sink.mkv")
    )
    code = cli.main(["compile", VALID_QUERY])
    out = capsys.readouterr().out
    assert code == 0
    assert "sink.mkv" in out
    assert "out.mp4" not in out


def test_compile_dash_o_overrides_sink_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "compile_sql", lambda text, probe=True, portable=False: _sinked_graph("sink.mkv")
    )
    code = cli.main(["compile", VALID_QUERY, "-o", "override.mp4"])
    out = capsys.readouterr().out
    assert code == 0
    assert "override.mp4" in out
    assert "sink.mkv" not in out


def test_compile_prints_every_sink_path_of_a_script(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """RFC-006: one ffmpeg command, one output file per COPY."""
    monkeypatch.setattr(
        cli,
        "compile_sql",
        lambda text, probe=True, portable=False: _multi_sink_graph(
            ("720.mp4", {}), ("360.mp4", {})
        ),
    )
    code = cli.main(["compile", VALID_QUERY])
    out = capsys.readouterr().out
    assert code == 0
    assert out.count("ffmpeg") == 1
    assert "720.mp4" in out
    assert "360.mp4" in out


def test_compile_dash_o_against_a_multi_sink_script_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli,
        "compile_sql",
        lambda text, probe=True, portable=False: _multi_sink_graph(
            ("720.mp4", {}), ("360.mp4", {})
        ),
    )
    code = cli.main(["compile", VALID_QUERY, "-o", "override.mp4"])
    captured = capsys.readouterr()
    assert code == 2
    assert "-o takes one path" in captured.err
    assert "'720.mp4', '360.mp4'" in captured.err
    assert captured.out == ""


def test_compile_graph_only_still_works_for_a_multi_sink_script(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--graph-only prints the filtergraph, which has no paths in it at all."""
    monkeypatch.setattr(
        cli,
        "compile_sql",
        lambda text, probe=True, portable=False: _multi_sink_graph(
            ("720.mp4", {}), ("360.mp4", {})
        ),
    )
    code = cli.main(["compile", "--graph-only", VALID_QUERY, "-o", "override.mp4"])
    assert code == 0
    assert capsys.readouterr().out == "\n"  # pure passthrough graph


def test_explain_shows_the_sinks_list(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["explain", "--no-probe", VALID_QUERY])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert [unit["path"] for unit in payload["sinks"]] == [None]


def test_compile_no_probe_skips_probing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--no-probe must never touch the filesystem via probe()."""

    def _boom(path: str) -> None:
        raise AssertionError("probe() should not be called with --no-probe")

    monkeypatch.setattr("sqlmpeg.compiler.probe_path", _boom)
    code = cli.main(["compile", "--no-probe", VALID_QUERY])
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


def test_compile_portable_rejects_a_dynamic_filter(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["compile", "--portable", DYNAMIC_QUERY])
    captured = capsys.readouterr()
    assert code == 1
    assert "UNKNOWN_FUNCTION" in captured.err


def test_compile_portable_still_accepts_the_stdlib(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["compile", "--portable", VALID_QUERY])
    out = capsys.readouterr().out
    assert code == 0
    assert "-filter_complex" in out


def test_explain_portable_rejects_a_dynamic_filter(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["explain", "--portable", DYNAMIC_QUERY])
    captured = capsys.readouterr()
    assert code == 1
    assert "UNKNOWN_FUNCTION" in captured.err


def test_validate_portable_rejects_a_dynamic_filter(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["validate", "--json", "--portable", DYNAMIC_QUERY])
    captured = capsys.readouterr()
    assert code == 1

    import json

    data = json.loads(captured.out)
    assert data["code"] == "UNKNOWN_FUNCTION"


def test_run_has_no_portable_flag() -> None:
    """--portable is compile/explain/validate only -- run always needs ffmpeg
    to execute against, so there is no offline escape hatch for it."""
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["run", VALID_QUERY, "--portable"])
    assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# explain
# ---------------------------------------------------------------------------


def test_explain_round_trips_graph(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["explain", VALID_QUERY])
    out = capsys.readouterr().out
    assert code == 0

    import json

    data = json.loads(out)
    graph = Graph.from_dict(data)
    assert graph.input_paths == ["x.mp4"]
    assert graph.to_dict() == data


def test_explain_no_probe(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["explain", "--no-probe", VALID_QUERY])
    out = capsys.readouterr().out
    assert code == 0

    import json

    data = json.loads(out)
    graph = Graph.from_dict(data)
    assert graph.input_paths == ["x.mp4"]


def test_explain_bad_query(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["explain", BAD_QUERY])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "error:" in captured.err


def test_explain_file_happy_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    query = _write_sql(tmp_path, VALID_QUERY)
    code = cli.main(["explain", "-f", query])
    out = capsys.readouterr().out
    assert code == 0

    import json

    data = json.loads(out)
    assert data["inputs"] == ["x.mp4"]


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_validate_success_is_silent(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["validate", VALID_QUERY])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == ""
    assert captured.err == ""


def test_validate_no_probe_success_is_silent(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["validate", "--no-probe", VALID_QUERY])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == ""
    assert captured.err == ""


def test_validate_bad_query_human(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["validate", BAD_QUERY])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err.startswith("error: ")


def test_validate_bad_query_json_inline(capsys: pytest.CaptureFixture[str]) -> None:
    """validate --json on an inline SQL string (plan 037)."""
    code = cli.main(["validate", "--json", BAD_QUERY])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err == ""

    import json

    data = json.loads(captured.out)
    assert data["code"] == "UNKNOWN_FUNCTION"
    assert set(data.keys()) == {"line", "col", "code", "message", "hint"}


def test_validate_file_happy_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    query = _write_sql(tmp_path, VALID_QUERY)
    code = cli.main(["validate", "-f", query])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == ""
    assert captured.err == ""


def test_validate_file_dash_reads_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io

    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(BAD_QUERY))
    code = cli.main(["validate", "--json", "-f", "-"])
    captured = capsys.readouterr()
    assert code == 1

    import json

    data = json.loads(captured.out)
    assert data["code"] == "UNKNOWN_FUNCTION"


# ---------------------------------------------------------------------------
# usage: exactly one of the positional query / -f is required (plan 037)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["compile", "explain", "validate", "run"])
def test_both_query_and_file_is_a_usage_error(
    command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    query = _write_sql(tmp_path, VALID_QUERY)
    code = cli.main([command, VALID_QUERY, "-f", query])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "not both" in captured.err


@pytest.mark.parametrize("command", ["compile", "explain", "validate", "run"])
def test_neither_query_nor_file_is_a_usage_error(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main([command])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "error:" in captured.err


# ---------------------------------------------------------------------------
# did-you-mean-f hint (plan 037)
# ---------------------------------------------------------------------------


def test_positional_existing_sql_file_gets_the_file_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Passing an existing .sql file's path positionally (instead of -f) fails
    to parse as SQL and gets a second stderr line suggesting -f."""
    path = _write_sql(tmp_path, VALID_QUERY)
    code = cli.main(["compile", path])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.startswith("error: ")
    assert "PARSE_ERROR" in captured.err
    assert f"hint: '{path}' looks like a file; did you mean -f '{path}'?" in captured.err


def test_positional_dot_sql_suffix_gets_the_file_hint_even_if_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The hint also fires on a .sql-suffixed path that does not exist -- the
    suffix alone is muscle-memory enough to suggest -f."""
    path = str(tmp_path / "does_not_exist.sql")
    code = cli.main(["compile", path])
    captured = capsys.readouterr()
    assert code == 1
    assert f"hint: '{path}' looks like a file; did you mean -f '{path}'?" in captured.err


def test_hint_does_not_fire_for_ordinary_bad_sql(capsys: pytest.CaptureFixture[str]) -> None:
    """Ordinary invalid SQL that neither exists as a file nor ends in .sql
    gets no hint -- the guard is specific to the muscle-memory case."""
    code = cli.main(["compile", "not sql at all !!"])
    captured = capsys.readouterr()
    assert code == 1
    assert "PARSE_ERROR" in captured.err
    assert "hint:" not in captured.err


def test_hint_does_not_fire_when_using_dash_f(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """-f itself never triggers the hint, even against a file that fails to
    parse as SQL -- the hint only applies to the inline-positional case."""
    bad_file = _write_sql(tmp_path, "not sql at all !!", name="bad.sql")
    code = cli.main(["compile", "-f", bad_file])
    captured = capsys.readouterr()
    assert code == 1
    assert "PARSE_ERROR" in captured.err
    assert "hint:" not in captured.err


def test_hint_does_not_fire_for_non_parse_errors(capsys: pytest.CaptureFixture[str]) -> None:
    """The hint is scoped to PARSE_ERROR only -- a non-PARSE_ERROR rejection
    (this one's error message carries the library's own unrelated "(hint:
    did you mean ...)" text) never gets the CLI's did-you-mean-f line."""
    code = cli.main(["compile", BAD_QUERY])
    captured = capsys.readouterr()
    assert code == 1
    assert "UNKNOWN_FUNCTION" in captured.err
    assert "did you mean -f" not in captured.err
    assert captured.err.count("\n") == 1


def test_hint_on_validate_json_goes_to_stderr_stdout_stays_pure_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Machine contract (plan 037): --json's stdout is the library error
    verbatim, untouched by the hint; the hint (if any) only ever goes to
    stderr."""
    path = _write_sql(tmp_path, VALID_QUERY)
    code = cli.main(["validate", "--json", path])
    captured = capsys.readouterr()
    assert code == 1

    import json

    data = json.loads(captured.out)
    assert data["code"] == "PARSE_ERROR"
    assert set(data.keys()) == {"line", "col", "code", "message", "hint"}
    assert f"hint: '{path}' looks like a file; did you mean -f '{path}'?" in captured.err


# ---------------------------------------------------------------------------
# Windows note: a query containing single quotes must survive argparse
# unquoted -- the common case is input('x.mp4') itself (plan 037).
# ---------------------------------------------------------------------------


def test_inline_query_with_single_quotes_compiles(capsys: pytest.CaptureFixture[str]) -> None:
    query = "SELECT scale(a.frame, 0.5) FROM input('a clip''s name.mp4') a"
    code = cli.main(["compile", "--no-probe", query])
    out = capsys.readouterr().out
    assert code == 0
    assert "-filter_complex" in out


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def test_run_ffmpeg_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out_path = str(tmp_path / "out.mp4")
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    code = cli.main(["run", VALID_QUERY, "-o", out_path])
    captured = capsys.readouterr()
    assert code == 1
    assert "ffmpeg" in captured.err
    assert "not found" in captured.err


def test_run_bad_query_never_checks_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out_path = str(tmp_path / "out.mp4")

    def _boom(name: str) -> str | None:
        raise AssertionError("should not check for ffmpeg before compiling")

    monkeypatch.setattr(cli.shutil, "which", _boom)
    code = cli.main(["run", BAD_QUERY, "-o", out_path])
    captured = capsys.readouterr()
    assert code == 1
    assert "error:" in captured.err


def test_run_uses_sink_path_when_no_dash_o(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """compile_sql is monkeypatched -- see `_sinked_graph`'s docstring."""
    monkeypatch.setattr(cli, "compile_sql", lambda text: _sinked_graph("sink.mkv"))
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    code = cli.main(["run", VALID_QUERY])
    captured = capsys.readouterr()
    # Reaching the ffmpeg-not-found check (rather than the exit-2 usage error
    # below) proves -o was resolved from the sink path and execution proceeded.
    assert code == 1
    assert "ffmpeg" in captured.err
    assert "not found" in captured.err


def test_run_dash_o_overrides_sink_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out_path = str(tmp_path / "override.mp4")
    monkeypatch.setattr(cli, "compile_sql", lambda text: _sinked_graph("sink.mkv"))
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    code = cli.main(["run", VALID_QUERY, "-o", out_path])
    captured = capsys.readouterr()
    assert code == 1
    assert "not found" in captured.err


def test_run_no_output_and_no_sink_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["run", VALID_QUERY])
    captured = capsys.readouterr()
    assert code == 2
    assert "no output path" in captured.err


def test_run_dash_o_against_a_multi_sink_script_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "compile_sql", lambda text: _multi_sink_graph(("720.mp4", {}), ("360.mp4", {}))
    )
    code = cli.main(["run", VALID_QUERY, "-o", "override.mp4"])
    captured = capsys.readouterr()
    assert code == 2
    assert "-o takes one path" in captured.err


def test_run_of_a_multi_sink_script_reaches_the_ffmpeg_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every COPY's path is the query's own, so `run` needs no -o at all."""
    monkeypatch.setattr(
        cli, "compile_sql", lambda text: _multi_sink_graph(("720.mp4", {}), ("360.mp4", {}))
    )
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    code = cli.main(["run", VALID_QUERY])
    captured = capsys.readouterr()
    assert code == 1
    assert "not found" in captured.err


def test_run_checks_every_sinks_output_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = str(tmp_path / "does_not_exist" / "360.mp4")
    monkeypatch.setattr(
        cli, "compile_sql", lambda text: _multi_sink_graph(("720.mp4", {}), (missing, {}))
    )
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    code = cli.main(["run", VALID_QUERY])
    captured = capsys.readouterr()
    assert code == 1
    assert "does not exist" in captured.err


def test_run_missing_output_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out_path = str(tmp_path / "does_not_exist" / "out.mp4")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    code = cli.main(["run", VALID_QUERY, "-o", out_path])
    captured = capsys.readouterr()
    assert code == 1
    assert "does not exist" in captured.err


def test_run_file_happy_path_reaches_ffmpeg_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """-f still works end to end for run (the LLM repair-loop pipe uses -f -)."""
    query = _write_sql(tmp_path, VALID_QUERY)
    out_path = str(tmp_path / "out.mp4")
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    code = cli.main(["run", "-f", query, "-o", out_path])
    captured = capsys.readouterr()
    assert code == 1
    assert "ffmpeg" in captured.err
    assert "not found" in captured.err


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
