"""Tests for the CLI.

Invokes ``main([...])`` directly (no subprocess) and asserts on captured
stdout/stderr via pytest's ``capsys``, plus the process exit code returned
by ``main``.

Convention: ``compile``/``explain``/``validate``/``run`` take the
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

VALID_QUERY = "SELECT scale(a.frame, 640, 480) FROM input('x.mp4') a"
BAD_QUERY = "SELECT nope(a.frame) FROM input('x.mp4') a"

# For tests that monkeypatch cli.compile_sql to return a hand-built, already
# -sinked Graph (see _sinked_graph/_multi_sink_graph below): the REAL text
# still goes through cli.classify() first, so it must
# itself look like a media query -- a real COPY, not FORMAT csv -- for
# `run`'s new table/csv branch to stay out of the way and reach the
# monkeypatched compile_sql at all.
SINKED_QUERY = f"COPY ({VALID_QUERY}) TO 'ignored.mkv'"


def _write_sql(tmp_path: Path, text: str, name: str = "query.sql") -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _sinked_graph(path: str, options: dict[str, object] | None = None) -> Graph:
    """A hand-built, already-sinked Graph.

    Sink-path-resolution tests below monkeypatch ``cli.compile_sql`` to
    return this instead of compiling real SQL.
    """
    return _multi_sink_graph((path, dict(options or {})))


def _multi_sink_graph(*sinks: tuple[str, dict[str, object]]) -> Graph:
    """One passthrough output per COPY, all reading the same input stream.

    The cross-group passthrough shape, hand-built: two groups may map
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


def test_compile_uses_sink_path_when_no_dash_o(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "compile_sql", lambda text: _sinked_graph("sink.mkv"))
    code = cli.main(["compile", VALID_QUERY])
    out = capsys.readouterr().out
    assert code == 0
    assert "sink.mkv" in out
    assert "out.mp4" not in out


def test_compile_dash_o_overrides_sink_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "compile_sql", lambda text: _sinked_graph("sink.mkv"))
    code = cli.main(["compile", VALID_QUERY, "-o", "override.mp4"])
    out = capsys.readouterr().out
    assert code == 0
    assert "override.mp4" in out
    assert "sink.mkv" not in out


def test_compile_prints_every_sink_path_of_a_script(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One ffmpeg command, one output file per COPY."""
    monkeypatch.setattr(
        cli,
        "compile_sql",
        lambda text: _multi_sink_graph(("720.mp4", {}), ("360.mp4", {})),
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
        lambda text: _multi_sink_graph(("720.mp4", {}), ("360.mp4", {})),
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
        lambda text: _multi_sink_graph(("720.mp4", {}), ("360.mp4", {})),
    )
    code = cli.main(["compile", "--graph-only", VALID_QUERY, "-o", "override.mp4"])
    assert code == 0
    assert capsys.readouterr().out == "\n"  # pure passthrough graph


# ---------------------------------------------------------------------------
# compile on a table/csv query
# ---------------------------------------------------------------------------


TABLE_QUERY = "SELECT t.language FROM input('tests/fixtures/av2.mp4') f, unnest(f.audio) t"


@pytest.fixture
def _two_track_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """A synthetic probe for the unnest-based CLI tests.

    The default tier must be deterministic on a bare machine (no ffprobe, no
    generated fixtures -- CI runs it before installing either), and a row
    query needs probed rows; so the rows are synthetic. The fixture PATH in
    the queries is illustrative, never read.
    """
    from sqlmpeg import compiler
    from sqlmpeg.probe import ProbeResult, StreamMeta

    def _track(index: int, language: str) -> StreamMeta:
        return StreamMeta(
            type="audio",
            index=index,
            metadata={"language": language},
            width=None,
            height=None,
            fps=None,
            sample_rate=44100,
            codec="aac",
        )

    result = ProbeResult(streams=[_track(0, "eng"), _track(1, "fra")])
    monkeypatch.setattr(compiler, "probe_path", lambda path: result)


def test_compile_on_a_table_query_is_a_typed_usage_message(
    capsys: pytest.CaptureFixture[str], _two_track_probe: None
) -> None:
    """Metadata columns have no ffmpeg command to show; `compile` says so and
    points at `run` instead of trying (and failing) to produce one."""
    code = cli.main(["compile", TABLE_QUERY])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "run" in captured.err


def test_compile_dash_o_still_works_on_a_bare_stream_select(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bare SELECT that COULD produce a command still can, with -o -- RFC-
    011's "`-o` stays as the implicit media COPY it always morally was"."""
    code = cli.main(["compile", VALID_QUERY, "-o", "out.mp4"])
    captured = capsys.readouterr()
    assert code == 0
    assert "ffmpeg" in captured.out


def test_validate_accepts_a_table_query(
    capsys: pytest.CaptureFixture[str], _two_track_probe: None
) -> None:
    code = cli.main(["validate", TABLE_QUERY])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == ""
    assert captured.err == ""


def test_explain_shows_the_sinks_list(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["explain", VALID_QUERY])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert [unit["path"] for unit in payload["sinks"]] == [None]


# ---------------------------------------------------------------------------
# --portable and --no-probe are GONE
# ---------------------------------------------------------------------------

# There is no portable subset any more: every function is a filter of the
# installed ffmpeg, so --portable had nothing left to select. --no-probe made
# a READABLE file compile as if unreadable, silently stripping provenance
# metadata -- a determinism switch that changed the result -- so it is gone
# too; opportunistic probing already degrades silently on
# missing/unreadable inputs, which is the whole of what it was ever for.


@pytest.mark.parametrize("flag", ["--portable", "--no-probe"])
@pytest.mark.parametrize("command", ["compile", "explain", "validate", "run"])
def test_removed_flags_are_gone_from_every_subcommand(command: str, flag: str) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main([command, VALID_QUERY, flag])
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


def test_validate_bad_query_human(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["validate", BAD_QUERY])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err.startswith("error: ")


def test_validate_bad_query_json_inline(capsys: pytest.CaptureFixture[str]) -> None:
    """validate --json on an inline SQL string."""
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
# usage: exactly one of the positional query / -f is required
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
# did-you-mean-f hint
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
    """Machine contract: --json's stdout is the library error
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
# unquoted -- the common case is input('x.mp4') itself.
# ---------------------------------------------------------------------------


def test_inline_query_with_single_quotes_compiles(capsys: pytest.CaptureFixture[str]) -> None:
    query = "SELECT scale(a.frame, 0.5) FROM input('a clip''s name.mp4') a"
    code = cli.main(["compile", query])
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
    monkeypatch.setattr(cli.binaries, "ffmpeg_path", lambda: None)

    code = cli.main(["run", VALID_QUERY, "-o", out_path])
    captured = capsys.readouterr()
    assert code == 1
    assert "ffmpeg" in captured.err
    assert "not found" in captured.err


def test_run_bad_query_never_checks_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out_path = str(tmp_path / "out.mp4")

    def _boom() -> str | None:
        raise AssertionError("should not check for ffmpeg before compiling")

    monkeypatch.setattr(cli.binaries, "ffmpeg_path", _boom)
    code = cli.main(["run", BAD_QUERY, "-o", out_path])
    captured = capsys.readouterr()
    assert code == 1
    assert "error:" in captured.err


def test_run_uses_sink_path_when_no_dash_o(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """compile_sql is monkeypatched -- see `_sinked_graph`'s docstring."""
    monkeypatch.setattr(cli, "compile_sql", lambda text: _sinked_graph("sink.mkv"))
    monkeypatch.setattr(cli.binaries, "ffmpeg_path", lambda: None)

    code = cli.main(["run", SINKED_QUERY])
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
    monkeypatch.setattr(cli.binaries, "ffmpeg_path", lambda: None)

    code = cli.main(["run", VALID_QUERY, "-o", out_path])
    captured = capsys.readouterr()
    assert code == 1
    assert "not found" in captured.err


def test_run_bare_select_with_no_dash_o_prints_a_table(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A sinkless SELECT with no `-o` is a TABLE query now
    -- it used to be the "no output path" usage error this test's name still
    remembers. `-o` (test_run_dash_o_overrides_sink_path et al.) still runs
    it as media, unchanged."""
    code = cli.main(["run", VALID_QUERY])
    captured = capsys.readouterr()
    assert code == 0
    assert "(1 row)" in captured.out
    assert "<video" in captured.out


def test_run_csv_copy_to_stdout(
    capsys: pytest.CaptureFixture[str], _two_track_probe: None
) -> None:
    sql = (
        "COPY (SELECT t.language, t.codec FROM "
        "input('tests/fixtures/av2.mp4') f, unnest(f.audio) t) "
        "TO STDOUT WITH (format 'csv', header true)"
    )
    code = cli.main(["run", sql])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == "language,codec\neng,aac\nfra,aac\n"


def test_run_csv_copy_to_a_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], _two_track_probe: None
) -> None:
    out_path = tmp_path / "tracks.csv"
    sql = (
        "COPY (SELECT t.language, t.codec FROM "
        f"input('tests/fixtures/av2.mp4') f, unnest(f.audio) t) "
        f"TO '{out_path.as_posix()}' WITH (format 'csv')"
    )
    code = cli.main(["run", sql])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == ""  # nothing printed -- it went to the file
    assert out_path.read_text(encoding="utf-8") == "eng,aac\nfra,aac\n"


def test_run_csv_copy_defaults_header_false(
    capsys: pytest.CaptureFixture[str], _two_track_probe: None
) -> None:
    sql = (
        "COPY (SELECT t.language FROM input('tests/fixtures/av2.mp4') f, "
        "unnest(f.audio) t) TO STDOUT WITH (format 'csv')"
    )
    code = cli.main(["run", sql])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == "eng\nfra\n"  # no header row


def test_run_dash_o_against_a_csv_copy_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sql = (
        "COPY (SELECT t.language FROM input('x') f, unnest(f.audio) t) "
        "TO STDOUT WITH (format 'csv')"
    )
    code = cli.main(["run", sql, "-o", "out.mp4"])
    captured = capsys.readouterr()
    assert code == 2
    assert "-o" in captured.err


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
    monkeypatch.setattr(cli.binaries, "ffmpeg_path", lambda: None)
    code = cli.main(["run", SINKED_QUERY])
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
    monkeypatch.setattr(cli.binaries, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    code = cli.main(["run", SINKED_QUERY])
    captured = capsys.readouterr()
    assert code == 1
    assert "does not exist" in captured.err


def test_run_missing_output_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out_path = str(tmp_path / "does_not_exist" / "out.mp4")
    monkeypatch.setattr(cli.binaries, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")

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
    monkeypatch.setattr(cli.binaries, "ffmpeg_path", lambda: None)

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


def test_unknown_subcommand_falls_through_to_run_and_fails_as_sql(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`run` is the default subcommand, unconditionally --
    no plausibility gating. A mistyped subcommand is not special-cased; it is
    just run's SQL text, and dies as an ordinary compile error (exit 1), a
    better diagnostic than a bare usage line."""
    code = cli.main(["bogus"])
    captured = capsys.readouterr()
    assert code == 1
    assert "error:" in captured.err


# ---------------------------------------------------------------------------
# default subcommand
# ---------------------------------------------------------------------------


def test_flag_first_argv_dispatches_to_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], _two_track_probe: None
) -> None:
    """``sqlmpeg -f query.sql`` (no ``run`` token, flags first) is exactly
    what the cookbook recipes show and what test_examples.py exercises end
    to end; this pins the same dispatch directly against a table query."""
    query = _write_sql(
        tmp_path,
        "SELECT t.index FROM input('tests/fixtures/av2.mp4') f, unnest(f.audio) t",
    )
    code = cli.main(["-f", query])
    captured = capsys.readouterr()
    assert code == 0
    assert "(2 rows)" in captured.out


def test_bare_inline_sql_with_no_subcommand_dispatches_to_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main([VALID_QUERY])
    captured = capsys.readouterr()
    assert code == 0
    assert "(1 row)" in captured.out


def test_explicit_run_and_default_dispatch_agree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    query = _write_sql(tmp_path, VALID_QUERY)
    code_explicit = cli.main(["run", "-f", query])
    explicit_out = capsys.readouterr().out
    code_default = cli.main(["-f", query])
    default_out = capsys.readouterr().out
    assert code_explicit == code_default == 0
    assert explicit_out == default_out


# ---------------------------------------------------------------------------
# -v/--set CLI variables
# ---------------------------------------------------------------------------

VAR_QUERY = "SELECT scale(a.frame, 640, 480) FROM input(:'path') a"


def test_compile_with_v_substitutes(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["compile", VAR_QUERY, "-v", "path=x.mp4"])
    out = capsys.readouterr().out
    assert code == 0
    assert "x.mp4" in out


def test_compile_with_set_alias(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["compile", VAR_QUERY, "--set", "path=x.mp4"])
    out = capsys.readouterr().out
    assert code == 0
    assert "x.mp4" in out


def test_duplicate_v_last_wins(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["compile", VAR_QUERY, "-v", "path=first.mp4", "-v", "path=second.mp4"])
    out = capsys.readouterr().out
    assert code == 0
    assert "second.mp4" in out
    assert "first.mp4" not in out


@pytest.mark.parametrize("bad_pair", ["noequals", "1bad=x", "=novame"])
def test_malformed_v_exits_2(bad_pair: str, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["compile", VALID_QUERY, "-v", bad_pair])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "error:" in captured.err
    assert "-v/--set" in captured.err


def test_missing_var_through_compile(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["compile", VAR_QUERY])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "UNSUPPORTED_SQL" in captured.err
    assert "path" in captured.err


def test_missing_var_through_validate_json(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["validate", "--json", VAR_QUERY])
    captured = capsys.readouterr()
    assert code == 1

    data = json.loads(captured.out)
    assert data["code"] == "UNSUPPORTED_SQL"
    assert "path" in data["message"]


def test_naked_dispatch_accepts_v(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`run`'s own parser must know -v -- naked dispatch (no explicit `run`
    token) reuses it, so this pins that it isn't lost along the way."""
    query = _write_sql(tmp_path, VAR_QUERY)
    code = cli.main(["-f", query, "-v", "path=x.mp4"])
    captured = capsys.readouterr()
    assert code == 0
    assert "(1 row)" in captured.out


def test_unused_v_is_silent(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["compile", VALID_QUERY, "-v", "unused=1"])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ""


def test_v_accepted_on_explain(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["explain", VAR_QUERY, "-v", "path=x.mp4"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["inputs"] == ["x.mp4"]


def test_v_accepted_on_run(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["run", VAR_QUERY, "-v", "path=x.mp4"])
    captured = capsys.readouterr()
    assert code == 0
    assert "(1 row)" in captured.out
