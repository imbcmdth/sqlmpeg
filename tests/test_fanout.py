"""Tests for set-driven output fan-out: ``COPY (...) TO (<expression>)``.

The rule under test: in a media COPY over a compile-time row table, a TO
EXPRESSION that reads that table's columns writes one FILE per surviving row,
each binding its own row; a constant TO keeps today's semantics byte for byte.

Those files ride ONE ffmpeg command as one sink each -- inputs decoded once,
output stream indices restarting per file -- except in the one shape ffmpeg
cannot express that way: a fan-out that trims and stream-copies everything it
maps, which stays a ``&&`` chain of one command per file with input-side seeks.

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
from sqlmpeg.ir import SinkUnit, StreamType
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


def _units(sql: str) -> list[SinkUnit]:
    """Every output FILE the compile writes, in order, across its commands."""
    return [unit for graph in compile_commands(sql) for unit in graph.sinks]


def _paths(sql: str) -> list[str | None]:
    return [unit.path for unit in _units(sql)]


_CHAPTER_SPLIT = (
    f"COPY (SELECT f.video[1], f.audio[1] FROM input('{SRC}') f, unnest(f.chapters) c "
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


def test_a_constant_to_over_a_row_table_gathers_into_one_file() -> None:
    """Both tracks land in ONE file when the TO names no row column -- and the
    aggregate is what says so."""
    sql = (
        f"COPY (SELECT array_agg(t.track) FROM input('{SRC}') f, unnest(f.audio) t) "
        "TO 'both.mka'"
    )
    graphs = compile_commands(sql)
    assert len(graphs) == 1
    assert [output.ref for output in graphs[0].outputs] == ["src:f:a:0", "src:f:a:1"]


def test_a_row_reading_to_writes_one_file_per_row() -> None:
    graphs = compile_commands(_PER_LANGUAGE)
    assert len(graphs) == 1
    assert [unit.path for unit in graphs[0].sinks] == ["eng.m4a", "fra.m4a"]


# ---------------------------------------------------------------------------
# per-row binding: streams, tags, seek bounds, paths
# ---------------------------------------------------------------------------


def test_each_file_maps_its_own_row_stream() -> None:
    assert [unit.outputs[0].ref for unit in _units(_PER_LANGUAGE)] == [
        "src:f:a:0",
        "src:f:a:1",
    ]


def test_each_file_carries_its_own_rows_provenance() -> None:
    assert [unit.outputs[0].metadata for unit in _units(_PER_LANGUAGE)] == [
        {"language": "eng"},
        {"language": "fra"},
    ]


def test_output_stream_indices_restart_in_every_file() -> None:
    """ffmpeg numbers output streams per file, so ``-c:0`` twice in one command."""
    out = _compile_line(_PER_LANGUAGE)
    assert " && " not in out
    assert out.count("-c:0 copy") == 2
    assert out.count("-metadata:s:0 language=") == 2


def test_a_row_bounded_where_becomes_a_per_row_seek() -> None:
    graphs = compile_commands(_CHAPTER_SPLIT)
    assert [graph.input_trims["f"] for graph in graphs] == [(0.0, 4.0), (4.0, 10.0)]


def test_the_path_expression_is_evaluated_per_row() -> None:
    assert _paths(_CHAPTER_SPLIT) == ["ch1.mkv", "ch2.mkv"]


# ---------------------------------------------------------------------------
# trim windows: output seeks in one command, or the input-seek chain
# ---------------------------------------------------------------------------


def test_a_copy_only_windowed_fan_out_keeps_the_chain() -> None:
    """ffmpeg writes corrupt files from an output seek plus ``-c copy``, so a
    fan-out that copies everything it maps stays one command per file."""
    graphs = compile_commands(_CHAPTER_SPLIT)
    assert len(graphs) == 2
    assert all(graph.sinks[0].window is None for graph in graphs)
    line = _compile_line(_CHAPTER_SPLIT)
    assert line.count(" && ") == 1
    assert f"-ss 0.0 -to 4.0 -i {SRC}" in line
    assert line.count("-c:0 copy") == 2


def test_a_re_encoding_windowed_fan_out_seeks_its_outputs() -> None:
    sql = _CHAPTER_SPLIT + " WITH (video_codec 'libx264', audio_codec 'aac')"
    graphs = compile_commands(sql)
    assert len(graphs) == 1
    assert [unit.window for unit in graphs[0].sinks] == [(0.0, 4.0), (4.0, 10.0)]
    assert graphs[0].input_trims == {}
    line = _compile_line(sql)
    assert " && " not in line
    assert f"-i {SRC} -ss 0.0 -to 4.0 -map" in line


def test_one_named_codec_drops_the_copy_on_the_untouched_stream() -> None:
    """The window re-encodes the whole file, so the audio nobody named a codec
    for takes the container's default encoder rather than ``-c copy``."""
    sql = _CHAPTER_SPLIT + " WITH (video_codec 'libx264')"
    line = _compile_line(sql)
    assert " && " not in line
    assert "copy" not in line
    assert line.count("-ss ") == 2


def test_a_filtered_stream_is_enough_to_seek_the_outputs() -> None:
    """No codec named at all: one filtered column re-encodes the file anyway."""
    sql = (
        f"COPY (SELECT hflip(f.video[1]), f.audio[1] FROM input('{SRC}') f, "
        "unnest(f.chapters) c WHERE f.t BETWEEN c.start_t AND c.end_t) "
        "TO ('ch' || c.index::text || '.mkv')"
    )
    graphs = compile_commands(sql)
    assert len(graphs) == 1
    assert [unit.window for unit in graphs[0].sinks] == [(0.0, 4.0), (4.0, 10.0)]
    assert "copy" not in _compile_line(sql)


def test_an_unwindowed_copy_fan_out_is_still_one_command() -> None:
    """No window, no reason to chain: stream copies ride the single invocation."""
    graphs = compile_commands(_PER_LANGUAGE)
    assert len(graphs) == 1
    assert all(unit.window is None for unit in graphs[0].sinks)
    assert "-c:0 copy" in _compile_line(_PER_LANGUAGE)


def test_two_windows_in_one_file_send_the_fan_out_back_to_the_chain() -> None:
    """An output takes one seek, so a row trimming two inputs differently can
    only be said on the inputs."""
    sql = (
        "COPY (SELECT f.video[1], g.audio[1] FROM input('a.mkv') f, "
        "input('b.mkv') g, unnest(f.chapters) c "
        "WHERE f.t BETWEEN c.start_t AND c.end_t AND g.t BETWEEN 0 AND 2) "
        "TO ('ch' || c.index::text || '.mkv') WITH (video_codec 'libx264')"
    )
    graphs = compile_commands(sql)
    assert len(graphs) == 2
    assert [graph.input_trims["f"] for graph in graphs] == [(0.0, 4.0), (4.0, 10.0)]
    assert all(graph.sinks[0].window is None for graph in graphs)


def test_a_filtered_stream_the_files_share_is_split_across_them() -> None:
    """The CTE's video lowers once for the whole graph, so the sinks share its
    pad and the split pass hands each one its own."""
    sql = (
        "COPY ("
        "  WITH pic AS (SELECT hflip(g.video[1]) AS frame FROM input('v.mkv') g)"
        f"  SELECT pic.frame, t.track FROM pic, input('{SRC}') f, unnest(f.audio) t"
        ") TO (t.language || '.mkv')"
    )
    graph = compile_commands(sql)[0]
    assert [node.filter for node in graph.nodes.values()] == ["hflip", "split"]
    split = list(graph.nodes.values())[1]
    assert split.args["n"] == 2
    assert [unit.outputs[0].ref for unit in graph.sinks] == [
        f"{split.id}:0",
        f"{split.id}:1",
    ]


def test_a_row_tag_column_tags_only_its_own_file() -> None:
    sql = (
        f"COPY (SELECT t.track, 'Audio (' || t.language || ')' AS title "
        f"FROM input('{SRC}') f, unnest(f.audio) t) TO (t.language || '.m4a')"
    )
    assert [unit.outputs[0].metadata["title"] for unit in _units(sql)] == [
        "Audio (eng)",
        "Audio (fra)",
    ]


def test_a_tagged_ctes_tags_reach_every_file() -> None:
    """The CTE lowers once for the whole graph, so the per-stream tags it sets
    ride into every file the fan-out writes.

    The CTE's rows cross-join with the audio rows, so the captions are
    gathered per group -- one file per audio track, every caption inside.
    """
    sql = (
        "COPY ("
        "  WITH capt AS ("
        "    SELECT s.track AS track, 'Subs' AS title"
        f"    FROM input('{SRC}') g, unnest(g.subtitle) s"
        "  )"
        f"  SELECT t.track, array_agg(capt.track) FROM input('{SRC}') f, "
        "  unnest(f.audio) t, capt GROUP BY t.track, t.language"
        ") TO (t.language || '.mkv')"
    )
    units = _units(sql)
    assert [unit.path for unit in units] == ["eng.mkv", "fra.mkv"]
    for unit in units:
        assert [output.metadata.get("title") for output in unit.outputs] == [
            None,
            "Subs",
            "Subs",
        ]


def test_with_options_apply_to_every_file() -> None:
    sql = (
        f"COPY (SELECT t.track FROM input('{SRC}') f, unnest(f.audio) t) "
        "TO (t.language || '.m4a') WITH (audio_codec 'aac', audio_bitrate '192k')"
    )
    assert [unit.options for unit in _units(sql)] == [
        {"audio_codec": "aac", "audio_bitrate": "192k"},
    ] * 2


def test_a_where_row_predicate_still_filters_before_the_fan_out() -> None:
    sql = (
        f"COPY (SELECT t.track FROM input('{SRC}') f, unnest(f.audio) t "
        "WHERE t.language = 'fra') TO (t.language || '.m4a')"
    )
    assert _paths(sql) == ["fra.m4a"]


def test_order_by_reorders_the_files() -> None:
    sql = (
        f"COPY (SELECT t.track FROM input('{SRC}') f, unnest(f.audio) t "
        "ORDER BY t.language DESC) TO (t.language || '.m4a')"
    )
    assert _paths(sql) == ["fra.m4a", "eng.m4a"]


def test_a_cross_product_of_two_row_tables_fans_out_over_every_pair() -> None:
    sql = (
        f"COPY (SELECT a.track FROM input('{SRC}') f, unnest(f.audio) a, "
        "unnest(f.subtitle) s) TO (a.language || '-' || s.language || '.mka')"
    )
    assert _paths(sql) == [
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
        f"COPY (SELECT f.video[1] FROM input('{SRC}') f, unnest(f.chapters) c "
        "WHERE f.t BETWEEN c.start_t AND c.end_t) TO ('one.mkv')"
    )
    err = _rejects(sql)
    assert "only under a fan-out TO" in err.message


def test_a_row_bounded_window_under_a_quoted_to_keeps_the_old_rejection() -> None:
    sql = (
        f"COPY (SELECT f.video[1] FROM input('{SRC}') f, unnest(f.chapters) c "
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


def test_compile_prints_one_command_for_the_whole_fan_out() -> None:
    line = _compile_line(_PER_LANGUAGE)
    assert " && " not in line
    assert line.count("ffmpeg ") == 1


def test_dash_o_against_a_fan_out_query_is_an_unrecognized_argument(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A fan-out TO writes several files from its own row-computed paths;
    -o is gone entirely now, so it fails as argparse's ordinary unrecognized
    -argument error rather than a bespoke one-path-vs-several-files message."""
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["compile", _PER_LANGUAGE, "-o", "one.m4a"])
    assert exc_info.value.code == 2


def test_explain_dumps_one_graph_with_a_sink_per_file(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["explain", _PER_LANGUAGE]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, dict)
    assert [sink["path"] for sink in payload["sinks"]] == ["eng.m4a", "fra.m4a"]


def test_explain_dumps_a_graph_list_when_the_fan_out_chains(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The copy-and-trim shape is still a sequence, so explain is still a list."""
    assert cli.main(["explain", _CHAPTER_SPLIT]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert [graph["sinks"][0]["path"] for graph in payload] == ["ch1.mkv", "ch2.mkv"]


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


def _first_frame(path: Path) -> bytes:
    """The piece's opening video frame as PNG bytes.

    What tells two cuts of one source apart: the fixture's picture is a
    testsrc2 pattern with a running counter drawn into it.
    """
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", str(path),
            "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-",
        ],
        capture_output=True,
        timeout=120,
        check=True,
    )
    return result.stdout


def _duration(path: Path) -> float:
    container = _probe_json(path)["format"]
    assert isinstance(container, dict)
    return float(str(container["duration"]))


def _chapter_split_sql(options: str = "") -> str:
    source = (FIXTURES_DIR / "av-chapters.mkv").as_posix()
    return (
        f"COPY (SELECT f.video[1], f.audio[1] FROM input('{source}') f, unnest(f.chapters) c "
        "WHERE f.t BETWEEN c.start_t AND c.end_t) TO ('ch' || c.index::text || '.mkv')"
        + options
    )


@pytest.mark.exec
def test_split_by_chapter_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    _fixtures: None,
) -> None:
    """Recipe 47's stream-copy form executed: two chapter files, one command
    each, every command carrying its own input seek.

    It stream-COPIES, so ffmpeg snaps ``-ss 1.0`` back to the keyframe before
    it: ch2.mkv holds the whole 2.023s clip, and only the printed windows show
    the per-row seek.
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


@pytest.mark.exec
def test_split_by_chapter_re_encoding_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    _fixtures: None,
) -> None:
    """Recipe 47's re-encoding form executed: ONE command, output seeks.

    The source decodes once and each output takes its own window, so the cuts
    land where the chapters say -- 1.023s each -- and the two pieces really do
    hold different parts of the picture.
    """
    monkeypatch.undo()
    monkeypatch.chdir(tmp_path)
    options = " WITH (video_codec 'libx264', audio_codec 'aac')"
    assert cli.main(["run", _chapter_split_sql(options), "-y"]) == 0
    printed = capsys.readouterr().out
    assert " && " not in printed
    assert "-ss 0.0 -to 1.0 -map" in printed
    assert "-ss 1.0 -to 2.0 -map" in printed
    for name in ("ch1.mkv", "ch2.mkv"):
        assert _duration(tmp_path / name) == pytest.approx(1.0, abs=0.25)
    assert _first_frame(tmp_path / "ch1.mkv") != _first_frame(tmp_path / "ch2.mkv")


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


# ---------------------------------------------------------------------------
# the GROUPED fan-out: one file per GROUP, not per row
# ---------------------------------------------------------------------------


def _two_eng(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three audio rows, two of them sharing a language: two groups."""
    result = ProbeResult(
        streams=[
            _stream("audio", 0, "eng", codec="aac"),
            _stream("audio", 1, "eng", codec="aac"),
            _stream("audio", 2, "fra", codec="aac"),
        ],
        duration=10.0,
    )
    monkeypatch.setattr(compiler, "probe_path", lambda path: result)


_GROUPED = (
    f"COPY (SELECT array_agg(t.track), t.language AS title FROM input('{SRC}') f, "
    "unnest(f.audio) t GROUP BY t.language) TO (t.language || '.mka')"
)


def test_a_group_writes_one_file_holding_all_its_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _two_eng(monkeypatch)
    graphs = compile_commands(_GROUPED)
    assert len(graphs) == 1
    units = graphs[0].sinks
    assert [unit.path for unit in units] == ["eng.mka", "fra.mka"]
    assert [len(unit.outputs) for unit in units] == [2, 1]


def test_the_group_key_tags_the_groups_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _two_eng(monkeypatch)
    assert [unit.tags for unit in _units(_GROUPED)] == [
        {"title": "eng"},
        {"title": "fra"},
    ]


def test_groups_come_out_in_first_appearance_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = ProbeResult(
        streams=[
            _stream("audio", 0, "fra", codec="aac"),
            _stream("audio", 1, "eng", codec="aac"),
            _stream("audio", 2, "fra", codec="aac"),
        ],
        duration=10.0,
    )
    monkeypatch.setattr(compiler, "probe_path", lambda path: result)
    assert _paths(_GROUPED) == ["fra.mka", "eng.mka"]


def test_a_multi_key_group_by_partitions_on_the_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = ProbeResult(
        streams=[
            _stream("audio", 0, "eng", codec="aac"),
            _stream("audio", 1, "eng", codec="ac3"),
            _stream("audio", 2, "eng", codec="aac"),
        ],
        duration=10.0,
    )
    monkeypatch.setattr(compiler, "probe_path", lambda path: result)
    sql = (
        f"COPY (SELECT array_agg(t.track) FROM input('{SRC}') f, unnest(f.audio) t "
        "GROUP BY t.language, t.codec) TO (t.language || '-' || t.codec || '.mka')"
    )
    units = _units(sql)
    assert [unit.path for unit in units] == ["eng-aac.mka", "eng-ac3.mka"]
    assert [len(unit.outputs) for unit in units] == [2, 1]


def test_two_groups_naming_one_file_are_rejected() -> None:
    sql = (
        f"COPY (SELECT array_agg(t.track) FROM input('{SRC}') f, unnest(f.audio) t "
        "GROUP BY t.language) TO (t.codec || '.mka')"
    )
    err = _rejects(sql)
    assert "'t.codec' is neither aggregated nor a GROUP BY key" in err.message


def test_distinct_groups_colliding_on_one_path_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key tells the groups apart; the name has to as well."""
    result = ProbeResult(
        streams=[
            _stream("audio", 0, "eng", codec="aac"),
            _stream("audio", 1, "fra", codec="aac"),
        ],
        duration=10.0,
    )
    monkeypatch.setattr(compiler, "probe_path", lambda path: result)
    sql = (
        f"COPY (SELECT array_agg(t.track) FROM input('{SRC}') f, unnest(f.audio) t "
        "GROUP BY t.language, t.codec) TO (t.codec || '.mka')"
    )
    err = _rejects(sql)
    assert "groups 1 and 2 both name 'aac.mka'" in err.message
    assert "tells the groups apart" in (err.hint or "")


def test_the_ungrouped_collision_now_points_at_group_by() -> None:
    sql = (
        f"COPY (SELECT t.track FROM input('{SRC}') f, unnest(f.audio) t) "
        "TO (t.codec || '.m4a')"
    )
    err = _rejects(sql)
    assert "rows 1 and 2 both name 'aac.m4a'" in err.message
    assert "GROUP BY the column they share" in (err.hint or "")


@pytest.mark.exec
def test_one_file_per_language_with_all_its_tracks_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _fixtures: None
) -> None:
    """Recipe 55 executed: the eng file carries BOTH eng tracks, titled by key."""
    monkeypatch.undo()
    monkeypatch.chdir(tmp_path)
    source = (FIXTURES_DIR / "av-2eng.mp4").as_posix()
    sql = (
        f"COPY (SELECT array_agg(t.track), t.language AS title FROM input('{source}') f, "
        "unnest(f.audio) t GROUP BY t.language) TO (t.language || '.mka')"
    )
    assert cli.main(["run", sql, "-y"]) == 0
    for name, count in (("eng.mka", 2), ("fra.mka", 1)):
        written = tmp_path / name
        assert written.exists()
        streams = _probe_json(written)["streams"]
        assert isinstance(streams, list)
        assert len(streams) == count
        assert all(stream["codec_type"] == "audio" for stream in streams)
        container = _probe_json(written)["format"]
        assert isinstance(container, dict)
        tags = container["tags"]
        assert isinstance(tags, dict)
        assert tags["title"] == name.removesuffix(".mka")
