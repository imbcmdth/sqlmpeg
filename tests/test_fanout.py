"""Tests for set-driven output fan-out: ``COPY (...) TO (<expression>)``.

The rule under test: in a media COPY over a compile-time row table, a TO
EXPRESSION that reads that table's columns compiles to ONE ffmpeg command per
surviving row, each binding its own row; a constant TO keeps today's semantics
byte for byte.

HERMETIC by default: ``probe_path`` is stubbed with a synthetic
``ProbeResult`` (two language-tagged audio rows, two subtitle rows, two
chapters), so the row COUNTS and the per-row values are fixed here rather than
being properties of whatever file the machine has. The two ``exec`` tests at
the bottom are the exception -- they run the real commands against the real
fixtures and read the written files back.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from sqlmpeg import cli, compiler
from sqlmpeg.compiler import compile_commands, compile_sql
from sqlmpeg.errors import ErrorCode, SqlmpegError
from sqlmpeg.ir import StreamType
from sqlmpeg.probe import ChapterMeta, ProbeResult, StreamMeta

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"

SRC = "in.mkv"


def _stream(stream_type: StreamType, index: int, language: str, codec: str) -> StreamMeta:
    return StreamMeta(
        type=stream_type,
        index=index,
        metadata={"language": language},
        width=None,
        height=None,
        fps=None,
        sample_rate=None,
        codec=codec,
    )


@pytest.fixture(autouse=True)
def _synthetic_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    video = StreamMeta(
        type="video",
        index=0,
        metadata={},
        width=640,
        height=480,
        fps="30/1",
        sample_rate=None,
        codec="h264",
    )
    result = ProbeResult(
        streams=[
            video,
            _stream("audio", 0, "eng", codec="aac"),
            _stream("audio", 1, "fra", codec="aac"),
            _stream("subtitle", 0, "eng", codec="subrip"),
            _stream("subtitle", 1, "fra", codec="subrip"),
        ],
        duration=10.0,
        chapters=[
            ChapterMeta(index=1, start_t=0.0, end_t=4.0, title="Intro"),
            ChapterMeta(index=2, start_t=4.0, end_t=10.0, title="Credits"),
        ],
    )
    monkeypatch.setattr(compiler, "probe_path", lambda path: result)


def _rejects(sql: str) -> SqlmpegError:
    with pytest.raises(SqlmpegError) as excinfo:
        compile_commands(sql)
    return excinfo.value


_CHAPTER_SPLIT = (
    f"COPY (SELECT f.video[1], f.audio[1] FROM input('{SRC}') f, chapters(f) c "
    "WHERE f.t BETWEEN c.start_t AND c.end_t) TO ('ch' || c.index::text || '.mkv')"
)
_PER_LANGUAGE = (
    f"COPY (SELECT t.track FROM input('{SRC}') f, unnest(f.audio) t) "
    "TO (t.language || '.m4a')"
)


# ---------------------------------------------------------------------------
# dispatch: a constant TO is unchanged, a row-reading TO fans out
# ---------------------------------------------------------------------------


def test_a_quoted_to_still_compiles_to_one_command() -> None:
    sql = f"COPY (SELECT f.audio[1] FROM input('{SRC}') f) TO 'out.m4a'"
    graphs = compile_commands(sql)
    assert len(graphs) == 1
    assert graphs[0].to_dict() == compile_sql(sql).to_dict()


def test_a_constant_to_expression_is_just_a_path() -> None:
    """A parenthesized TO reading no row column is one command, one file."""
    sql = f"COPY (SELECT f.audio[1] FROM input('{SRC}') f) TO ('out' || '.m4a')"
    graphs = compile_commands(sql)
    assert len(graphs) == 1
    assert graphs[0].sinks[0].path == "out.m4a"


def test_a_constant_to_over_a_row_table_keeps_todays_splat() -> None:
    """Both tracks land in ONE file when the TO names no row column."""
    sql = (
        f"COPY (SELECT t.track FROM input('{SRC}') f, unnest(f.audio) t) TO 'both.mka'"
    )
    graphs = compile_commands(sql)
    assert len(graphs) == 1
    assert [output.ref for output in graphs[0].outputs] == ["src:f:a:0", "src:f:a:1"]


def test_a_row_reading_to_fans_out_one_command_per_row() -> None:
    graphs = compile_commands(_PER_LANGUAGE)
    assert [graph.sinks[0].path for graph in graphs] == ["eng.m4a", "fra.m4a"]


# ---------------------------------------------------------------------------
# per-row binding: streams, tags, seek bounds, paths
# ---------------------------------------------------------------------------


def test_each_command_maps_its_own_row_stream() -> None:
    graphs = compile_commands(_PER_LANGUAGE)
    assert [graph.outputs[0].ref for graph in graphs] == ["src:f:a:0", "src:f:a:1"]


def test_each_command_carries_its_own_rows_provenance() -> None:
    graphs = compile_commands(_PER_LANGUAGE)
    assert [graph.outputs[0].metadata for graph in graphs] == [
        {"language": "eng"},
        {"language": "fra"},
    ]


def test_output_stream_indices_restart_in_every_command() -> None:
    """Each command is its own file, so ``-c:0``/``-metadata:s:0`` twice."""
    out = _compile_line(_PER_LANGUAGE)
    assert out.count("-c:0 copy") == 2
    assert out.count("-metadata:s:0 language=") == 2


def test_a_row_bounded_where_becomes_a_per_row_seek() -> None:
    graphs = compile_commands(_CHAPTER_SPLIT)
    assert [graph.input_trims["f"] for graph in graphs] == [(0.0, 4.0), (4.0, 10.0)]


def test_the_path_expression_is_evaluated_per_row() -> None:
    graphs = compile_commands(_CHAPTER_SPLIT)
    assert [graph.sinks[0].path for graph in graphs] == ["ch1.mkv", "ch2.mkv"]


def test_a_row_tag_column_tags_only_its_own_command() -> None:
    sql = (
        f"COPY (SELECT t.track, 'Audio (' || t.language || ')' AS title "
        f"FROM input('{SRC}') f, unnest(f.audio) t) TO (t.language || '.m4a')"
    )
    graphs = compile_commands(sql)
    assert [graph.outputs[0].metadata["title"] for graph in graphs] == [
        "Audio (eng)",
        "Audio (fra)",
    ]


def test_with_options_apply_to_every_command() -> None:
    sql = (
        f"COPY (SELECT t.track FROM input('{SRC}') f, unnest(f.audio) t) "
        "TO (t.language || '.m4a') WITH (audio_codec 'aac', audio_bitrate '192k')"
    )
    graphs = compile_commands(sql)
    assert [graph.sinks[0].options for graph in graphs] == [
        {"audio_codec": "aac", "audio_bitrate": "192k"},
    ] * 2


def test_a_where_row_predicate_still_filters_before_the_fan_out() -> None:
    sql = (
        f"COPY (SELECT t.track FROM input('{SRC}') f, unnest(f.audio) t "
        "WHERE t.language = 'fra') TO (t.language || '.m4a')"
    )
    graphs = compile_commands(sql)
    assert [graph.sinks[0].path for graph in graphs] == ["fra.m4a"]


def test_order_by_reorders_the_commands() -> None:
    sql = (
        f"COPY (SELECT t.track FROM input('{SRC}') f, unnest(f.audio) t "
        "ORDER BY t.language DESC) TO (t.language || '.m4a')"
    )
    graphs = compile_commands(sql)
    assert [graph.sinks[0].path for graph in graphs] == ["fra.m4a", "eng.m4a"]


def test_a_cross_product_of_two_row_tables_fans_out_over_every_pair() -> None:
    sql = (
        f"COPY (SELECT a.track FROM input('{SRC}') f, unnest(f.audio) a, "
        "unnest(f.subtitle) s) TO (a.language || '-' || s.language || '.mka')"
    )
    graphs = compile_commands(sql)
    assert [graph.sinks[0].path for graph in graphs] == [
        "eng-eng.mka",
        "eng-fra.mka",
        "fra-eng.mka",
        "fra-fra.mka",
    ]


# ---------------------------------------------------------------------------
# rejections
# ---------------------------------------------------------------------------


def test_two_pass_and_a_fan_out_to_are_rejected() -> None:
    sql = _CHAPTER_SPLIT + " WITH (video_codec 'libx264', video_bitrate '2M', two_pass true)"
    err = _rejects(sql)
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "'two_pass' and a fan-out TO cannot both be set" in err.message


def test_a_fan_out_copy_may_not_share_a_script() -> None:
    sql = (
        f"COPY (SELECT f.video[1] FROM input('{SRC}') f) TO 'v.mkv'; "
        f"COPY (SELECT t.track FROM input('{SRC}') g, unnest(g.audio) t) "
        "TO (t.language || '.m4a')"
    )
    err = _rejects(sql)
    assert "cannot share a script" in err.message


def test_chapters_from_and_a_fan_out_to_are_rejected() -> None:
    sql = (
        f"COPY (SELECT t.track FROM input('{SRC}') f, unnest(f.audio) t) "
        "TO (t.language || '.m4a') WITH (chapters_from f)"
    )
    assert "'chapters' and a fan-out TO cannot both be set" in _rejects(sql).message


def test_metadata_from_and_a_fan_out_to_are_rejected() -> None:
    sql = (
        f"COPY (SELECT t.track FROM input('{SRC}') f, unnest(f.audio) t) "
        "TO (t.language || '.m4a') WITH (metadata_from f)"
    )
    assert "'metadata_from' and a fan-out TO cannot both be set" in _rejects(sql).message


def test_a_csv_copy_takes_no_to_expression() -> None:
    sql = (
        f"COPY (SELECT t.language FROM input('{SRC}') f, unnest(f.audio) t) "
        "TO (t.language || '.csv') WITH (format 'csv')"
    )
    err = _rejects(sql)
    assert "a csv COPY takes a quoted path or STDOUT" in err.message


def test_union_all_and_a_fan_out_to_are_rejected() -> None:
    sql = (
        f"COPY (SELECT t.track FROM input('{SRC}') f, unnest(f.audio) t "
        f"UNION ALL SELECT u.track FROM input('{SRC}') g, unnest(g.audio) u) "
        "TO (t.language || '.m4a')"
    )
    assert "one row set per branch" in _rejects(sql).message


def test_a_computed_path_segment_may_not_hold_a_separator() -> None:
    """A language tag of ``a/b`` would otherwise choose a directory."""
    sql = (
        f"COPY (SELECT t.track FROM input('{SRC}') f, unnest(f.audio) t) "
        "TO (t.language || '/' || t.codec || '.m4a')"
    )
    graphs = compile_commands(sql)  # a LITERAL separator is fine
    assert graphs[0].sinks[0].path == "eng/aac.m4a"
    sql = (
        f"COPY (SELECT t.track FROM input('{SRC}') f, unnest(f.audio) t) "
        "TO ('x' || t.language || '.m4a')"
    )
    assert compile_commands(sql)[0].sinks[0].path == "xeng.m4a"


def test_a_separator_inside_a_computed_segment_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _probe_with_language(monkeypatch, "../etc")
    sql = (
        f"COPY (SELECT t.track FROM input('{SRC}') f, unnest(f.audio) t) "
        "TO (t.language || '.m4a')"
    )
    err = _rejects(sql)
    assert "a computed path segment may not contain" in err.message
    assert "'/'" in err.message


def test_a_dot_dot_inside_a_computed_segment_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _probe_with_language(monkeypatch, "..")
    sql = (
        f"COPY (SELECT t.track FROM input('{SRC}') f, unnest(f.audio) t) "
        "TO (t.language || '.m4a')"
    )
    assert "'..'" in _rejects(sql).message


def test_two_rows_naming_one_file_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    sql = (
        f"COPY (SELECT t.track FROM input('{SRC}') f, unnest(f.audio) t) "
        "TO (t.codec || '.m4a')"
    )
    err = _rejects(sql)
    assert "rows 1 and 2 both name 'aac.m4a'" in err.message


def test_zero_surviving_rows_is_rejected() -> None:
    sql = (
        f"COPY (SELECT t.track FROM input('{SRC}') f, unnest(f.audio) t "
        "WHERE t.language = 'deu') TO (t.language || '.m4a')"
    )
    assert "no row survives the WHERE clause" in _rejects(sql).message


def test_a_null_path_names_the_field(monkeypatch: pytest.MonkeyPatch) -> None:
    _probe_with_language(monkeypatch, None)
    sql = (
        f"COPY (SELECT t.track FROM input('{SRC}') f, unnest(f.audio) t) "
        "TO (t.title || '.m4a')"
    )
    err = _rejects(sql)
    assert "the TO expression is NULL for this row" in err.message
    assert "'t.title' was never probed" in err.message


def test_a_numeric_to_expression_is_rejected() -> None:
    sql = (
        f"COPY (SELECT t.track FROM input('{SRC}') f, unnest(f.audio) t) "
        "TO (t.index + 1)"
    )
    assert "a TO expression must be text, got number" in _rejects(sql).message


def test_a_row_bounded_window_needs_a_fan_out_to() -> None:
    sql = (
        f"COPY (SELECT f.video[1] FROM input('{SRC}') f, chapters(f) c "
        "WHERE f.t BETWEEN c.start_t AND c.end_t) TO ('one.mkv')"
    )
    err = _rejects(sql)
    assert "only under a fan-out TO" in err.message


def test_a_row_bounded_window_under_a_quoted_to_keeps_the_old_rejection() -> None:
    sql = (
        f"COPY (SELECT f.video[1] FROM input('{SRC}') f, chapters(f) c "
        "WHERE f.t BETWEEN c.start_t AND c.end_t) TO 'one.mkv'"
    )
    assert "cannot mix track-row columns" in _rejects(sql).message


# ---------------------------------------------------------------------------
# the CLI seam
# ---------------------------------------------------------------------------


def _compile_line(sql: str, *extra: str) -> str:
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = cli.main(["compile", sql, *extra])
    assert code == 0
    return buffer.getvalue()


def test_compile_chains_the_commands_with_and() -> None:
    assert _compile_line(_PER_LANGUAGE).count(" && ") == 1


def test_dash_o_against_a_fan_out_query_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(["compile", _PER_LANGUAGE, "-o", "one.m4a"])
    captured = capsys.readouterr()
    assert code == 2
    assert "-o takes one path, but this script writes 2 files" in captured.err


def test_explain_dumps_one_graph_per_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["explain", _PER_LANGUAGE]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert [graph["sinks"][0]["path"] for graph in payload] == ["eng.m4a", "fra.m4a"]


def _probe_with_language(monkeypatch: pytest.MonkeyPatch, language: str | None) -> None:
    """One audio row whose language tag is `language` (absent when None)."""
    meta = StreamMeta(
        type="audio",
        index=0,
        metadata={} if language is None else {"language": language},
        width=None,
        height=None,
        fps=None,
        sample_rate=None,
        codec="aac",
    )
    monkeypatch.setattr(
        compiler, "probe_path", lambda path: ProbeResult(streams=[meta])
    )


# ---------------------------------------------------------------------------
# the recipes, executed
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _fixtures() -> None:
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "gen_fixtures.py")],
        check=True,
    )


def _probe_json(path: Path) -> dict[str, object]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    data: dict[str, object] = json.loads(result.stdout)
    return data


def _duration(path: Path) -> float:
    container = _probe_json(path)["format"]
    assert isinstance(container, dict)
    return float(str(container["duration"]))


def _chapter_split_sql(options: str = "") -> str:
    source = (FIXTURES_DIR / "av-chapters.mkv").as_posix()
    return (
        f"COPY (SELECT f.video[1], f.audio[1] FROM input('{source}') f, chapters(f) c "
        "WHERE f.t BETWEEN c.start_t AND c.end_t) TO ('ch' || c.index::text || '.mkv')"
        + options
    )


@pytest.mark.exec
def test_split_by_chapter_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    _fixtures: None,
) -> None:
    """Recipe 47 executed: two chapter files, each command carrying its own seek.

    Recipe 47 stream-COPIES, so ffmpeg snaps ``-ss 1.0`` back to the keyframe
    before it: ch2.mkv holds the whole 2.023s clip, and only the printed
    windows show the per-row seek. The re-encoding variant below is the same
    query with a codec, and cuts where the chapters say -- 1.023s each.
    """
    monkeypatch.undo()  # the synthetic probe: this one reads the real file
    monkeypatch.chdir(tmp_path)
    assert cli.main(["run", _chapter_split_sql(), "-y"]) == 0
    printed = capsys.readouterr().out
    assert "-ss 0.0 -to 1.0" in printed
    assert "-ss 1.0 -to 2.0" in printed
    for name in ("ch1.mkv", "ch2.mkv"):
        written = tmp_path / name
        assert written.exists()
        streams = _probe_json(written)["streams"]
        assert isinstance(streams, list)
        assert [stream["codec_type"] for stream in streams] == ["video", "audio"]
    assert _duration(tmp_path / "ch1.mkv") == pytest.approx(1.0, abs=0.25)

    options = " WITH (video_codec 'libx264', audio_codec 'aac')"
    assert cli.main(["run", _chapter_split_sql(options), "-y"]) == 0
    for name in ("ch1.mkv", "ch2.mkv"):
        assert _duration(tmp_path / name) == pytest.approx(1.0, abs=0.25)


@pytest.mark.exec
def test_extract_every_language_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _fixtures: None
) -> None:
    """Recipe 48 executed: one file per language, tags read back off them."""
    monkeypatch.undo()
    monkeypatch.chdir(tmp_path)
    source = (FIXTURES_DIR / "av2.mp4").as_posix()
    sql = (
        f"COPY (SELECT t.track FROM input('{source}') f, unnest(f.audio) t) "
        "TO (t.language || '.m4a')"
    )
    assert cli.main(["run", sql, "-y"]) == 0
    for name, language in (("eng.m4a", "eng"), ("fra.m4a", "fra")):
        written = tmp_path / name
        assert written.exists()
        streams = _probe_json(written)["streams"]
        assert isinstance(streams, list)
        assert len(streams) == 1
        assert streams[0]["tags"]["language"] == language
