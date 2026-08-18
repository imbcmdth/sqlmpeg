"""Tests for the ``sqlmpeg.<name>`` macro namespace (RFC-007 wave 3a, plan 052).

Self-contained (file firewall: tests/test_lower.py, tests/exec and the
goldens are owned by a concurrently-running respell agent) -- every helper
below is a trimmed-down copy of test_lower.py's own conventions, not an
import from it, so this file has no coupling to what that agent is doing.

Macros resolve against `sqlmpeg.macros.MACROS` alone (never the registry),
so most tests pass `registry=None` -- proving the offline claim by
construction rather than by mocking `shutil.which`. The one exception is the
ad-insert composition test, which also calls the ordinary `overlay` filter
and so needs a real filter set; it uses the captured reference snapshot
(tests/data/reference_registry.json), exactly as test_lower.py's own
offline-fallback tests do.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
from pathlib import Path

import pytest

from sqlmpeg.compiler import compile_sql
from sqlmpeg.emit import build_ffmpeg_args, emit
from sqlmpeg.errors import ErrorCode, SqlmpegError
from sqlmpeg.inputs import INPUT_OPTIONS, option_spec
from sqlmpeg.ir import Graph
from sqlmpeg.lower import lower
from sqlmpeg.macros import INPUT_MACROS, MACROS
from sqlmpeg.parser import parse, resolve
from sqlmpeg.probe import ProbeResult, StreamMeta
from sqlmpeg.registry import Registry, load_reference

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
SNAPSHOT_PATH = REPO_ROOT / "tests" / "data" / "reference_registry.json"


def _lower(
    sql: str,
    probes: dict[str, ProbeResult | None] | None = None,
    *,
    registry: Registry | None = None,
) -> Graph:
    return lower(resolve(parse(sql)), probes or {}, registry=registry)


def _reject(
    sql: str,
    probes: dict[str, ProbeResult | None] | None = None,
    *,
    registry: Registry | None = None,
) -> SqlmpegError:
    with pytest.raises(SqlmpegError) as excinfo:
        _lower(sql, probes, registry=registry)
    err = excinfo.value
    assert err.line is not None, "every rejection must be line-anchored"
    assert err.col is not None
    return err


def _reject_resolve(sql: str) -> SqlmpegError:
    with pytest.raises(SqlmpegError) as excinfo:
        resolve(parse(sql))
    err = excinfo.value
    assert err.line is not None
    assert err.col is not None
    return err


def _filters(g: Graph) -> list[str]:
    return [node.filter for node in g.nodes.values()]


def _probe_result(videos: int = 1, audios: int = 1) -> ProbeResult:
    """A synthetic ProbeResult -- no ffprobe, no fixture, no disk."""
    streams = [
        StreamMeta(
            type="video", index=i, metadata={}, width=320, height=240,
            fps="15/1", sample_rate=None,
        )
        for i in range(videos)
    ]
    streams += [
        StreamMeta(
            type="audio", index=i, metadata={}, width=None, height=None,
            fps=None, sample_rate=44100,
        )
        for i in range(audios)
    ]
    return ProbeResult(streams=streams)


# ---------------------------------------------------------------------------
# each expansion's node shape
# ---------------------------------------------------------------------------


def test_blur_regions_expands_to_crop_gblur_overlay() -> None:
    g = _lower(
        "SELECT sqlmpeg.blur_regions(a.frame, 10, 20, 100, 50, 5) FROM input('x.mp4') a"
    )
    assert _filters(g) == ["crop", "gblur", "overlay"]
    crop, gblur, overlay = (g.nodes[f"n{i}"] for i in (1, 2, 3))
    # crop's arg KEYS are the registry's long names (out_w/out_h), plan 051's
    # verified contract note for 053a's golden regen.
    assert crop.args == {"out_w": 100, "out_h": 50, "x": 10, "y": 20}
    assert crop.inputs == ["src:a:v:0"]
    assert crop.outputs == ["video"]
    assert gblur.args == {"sigma": 5}
    assert gblur.inputs == ["n1"]
    # `f` (src:a:v:0) is consumed TWICE: once by crop, once by overlay's base
    # pad -- the split pass inserts the `split` node, not lowering.
    assert overlay.args == {"x": 10, "y": 20}
    assert overlay.inputs == ["src:a:v:0", "n2"]
    assert overlay.outputs == ["video"]


def test_speed_expands_to_setpts() -> None:
    g = _lower("SELECT sqlmpeg.speed(a.frame, 2) FROM input('x.mp4') a")
    assert _filters(g) == ["setpts"]
    node = g.nodes["n1"]
    assert node.args == {"expr": "PTS/2"}
    assert node.inputs == ["src:a:v:0"]
    assert node.outputs == ["video"]


def test_delay_expands_to_format_tpad() -> None:
    g = _lower("SELECT sqlmpeg.delay(a.frame, 3) FROM input('x.mp4') a")
    assert _filters(g) == ["format", "tpad"]
    fmt, tpad = g.nodes["n1"], g.nodes["n2"]
    assert fmt.args == {"pix_fmts": "yuva420p"}
    assert fmt.inputs == ["src:a:v:0"]
    assert tpad.args == {"start_duration": 3, "stop": 1, "color": "black@0"}
    assert tpad.inputs == ["n1"]
    assert tpad.outputs == ["video"]


def test_delay_seconds_may_be_a_float() -> None:
    g = _lower("SELECT sqlmpeg.delay(a.frame, 1.5) FROM input('x.mp4') a")
    assert g.nodes["n2"].args["start_duration"] == 1.5


# ---------------------------------------------------------------------------
# broadcasting (plan 020): the machinery is type-driven, macros need no
# special casing
# ---------------------------------------------------------------------------


def test_delay_broadcasts_over_a_video_array() -> None:
    probes: dict[str, ProbeResult | None] = {"a": _probe_result(videos=2, audios=0)}
    g = _lower("SELECT sqlmpeg.delay(a.video, 2) FROM input('x.mp4') a", probes)
    assert _filters(g) == ["format", "tpad", "format", "tpad"]
    assert g.nodes["n1"].inputs == ["src:a:v:0"]
    assert g.nodes["n3"].inputs == ["src:a:v:1"]
    assert len(g.sinks[0].outputs) == 2


def test_speed_broadcasts_over_a_video_array() -> None:
    probes: dict[str, ProbeResult | None] = {"a": _probe_result(videos=2, audios=0)}
    g = _lower("SELECT sqlmpeg.speed(a.video, 2) FROM input('x.mp4') a", probes)
    assert _filters(g) == ["setpts", "setpts"]
    assert len(g.sinks[0].outputs) == 2


# ---------------------------------------------------------------------------
# ad-insert composition (needs a real filter set for the bare `overlay`)
# ---------------------------------------------------------------------------


def _snapshot_registry() -> Registry:
    return load_reference(SNAPSHOT_PATH)


def test_ad_insert_composition_compiles() -> None:
    g = _lower(
        "SELECT overlay(f.frame, sqlmpeg.delay(p.frame, 120), 20, 20) "
        "FROM input('f.mp4') f, input('p.mp4') p",
        registry=_snapshot_registry(),
    )
    assert _filters(g) == ["format", "tpad", "overlay"]
    overlay = g.nodes["n3"]
    assert overlay.inputs == ["src:f:v:0", "n2"]
    assert overlay.args == {"x": 20, "y": 20}


# ---------------------------------------------------------------------------
# signature checking (macros own their OWN positional signature)
# ---------------------------------------------------------------------------


def test_named_argument_is_rejected() -> None:
    err = _reject("SELECT sqlmpeg.speed(a.frame, factor => 2) FROM input('x.mp4') a")
    assert err.code == ErrorCode.UNSUPPORTED_SQL
    assert "positional" in err.message


def test_wrong_arity_is_udf_arg_type() -> None:
    err = _reject("SELECT sqlmpeg.speed(a.frame) FROM input('x.mp4') a")
    assert err.code == ErrorCode.UDF_ARG_TYPE
    assert "sqlmpeg.speed(f, factor)" in (err.hint or "")


def test_wrong_arity_too_many_is_udf_arg_type() -> None:
    err = _reject("SELECT sqlmpeg.speed(a.frame, 2, 3) FROM input('x.mp4') a")
    assert err.code == ErrorCode.UDF_ARG_TYPE


def test_literal_where_stream_expected_is_udf_arg_type() -> None:
    err = _reject("SELECT sqlmpeg.speed(5, 2) FROM input('x.mp4') a")
    assert err.code == ErrorCode.UDF_ARG_TYPE


def test_stream_where_literal_expected_is_udf_arg_type() -> None:
    err = _reject("SELECT sqlmpeg.speed(a.frame, a.frame) FROM input('x.mp4') a")
    assert err.code == ErrorCode.UDF_ARG_TYPE


def test_delay_on_an_audio_stream_hints_bare_adelay() -> None:
    err = _reject("SELECT sqlmpeg.delay(a.audio[1], 2) FROM input('x.mp4') a")
    assert err.code == ErrorCode.UDF_ARG_TYPE
    assert "adelay" in (err.hint or "")


def test_unknown_macro_did_you_mean() -> None:
    err = _reject("SELECT sqlmpeg.spede(a.frame, 2) FROM input('x.mp4') a")
    assert err.code == ErrorCode.UNKNOWN_FUNCTION
    assert "sqlmpeg.speed()" in (err.hint or "")


def test_unknown_macro_with_no_close_match_names_the_trio() -> None:
    err = _reject("SELECT sqlmpeg.zzz(a.frame, 2) FROM input('x.mp4') a")
    assert err.code == ErrorCode.UNKNOWN_FUNCTION
    hint = err.hint or ""
    assert "blur_regions" in hint and "speed" in hint and "delay" in hint


# ---------------------------------------------------------------------------
# reserved name + bare-column hint (parser.py, mirrors the ffmpeg namespace)
# ---------------------------------------------------------------------------


def test_sqlmpeg_alias_is_reserved() -> None:
    err = _reject_resolve("SELECT frame FROM input('x.mp4') sqlmpeg")
    assert err.code == ErrorCode.UNSUPPORTED_SQL
    assert "reserved" in err.message


def test_sqlmpeg_cte_name_is_reserved() -> None:
    err = _reject_resolve(
        "WITH sqlmpeg AS (SELECT a.frame FROM input('x.mp4') a) "
        "SELECT frame FROM sqlmpeg"
    )
    assert err.code == ErrorCode.UNSUPPORTED_SQL
    assert "reserved" in err.message


def test_bare_sqlmpeg_dot_column_hints_it_is_a_call() -> None:
    err = _reject_resolve("SELECT sqlmpeg.speed FROM input('x.mp4') a")
    assert err.code == ErrorCode.UNKNOWN_ALIAS
    assert "is a call, not a column" in (err.hint or "")


# ---------------------------------------------------------------------------
# offline: macros need no registry at all
# ---------------------------------------------------------------------------


def test_macros_compile_with_no_registry() -> None:
    g = _lower("SELECT sqlmpeg.speed(a.frame, 2) FROM input('x.mp4') a", registry=None)
    assert _filters(g) == ["setpts"]


def test_blur_regions_compiles_with_no_registry() -> None:
    g = _lower(
        "SELECT sqlmpeg.blur_regions(a.frame, 0, 0, 10, 10, 2) FROM input('x.mp4') a",
        registry=None,
    )
    assert _filters(g) == ["crop", "gblur", "overlay"]


# ---------------------------------------------------------------------------
# exec: the ad-insert composition actually runs
# ---------------------------------------------------------------------------


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix()


def _ffprobe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        capture_output=True, text=True, timeout=30,
    )
    return float(result.stdout.strip())


@pytest.mark.exec
def test_ad_insert_composition_execs(tmp_path: Path) -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not found on PATH")
    base = FIXTURES_DIR / "av2.mp4"
    ad = FIXTURES_DIR / "testsrc.mp4"
    if not base.exists() or not ad.exists():
        pytest.skip("fixtures missing (run scripts/gen_fixtures.py first)")
    out_path = tmp_path / "ad_insert.mp4"
    query = (
        "SELECT overlay(f.frame, sqlmpeg.delay(p.frame, 1), 20, 20) "
        f"FROM input('{_sql_path(base)}') f, input('{_sql_path(ad)}') p"
    )
    graph = compile_sql(query)
    emitted = emit(graph)
    args = build_ffmpeg_args(emitted, str(out_path))
    args.insert(1, "-y")
    result = subprocess.run(args, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert out_path.exists()
    assert _ffprobe_duration(out_path) > 0


# ---------------------------------------------------------------------------
# the input-minting macro: sqlmpeg.empty_captions() (RFC-009, plan 062)
# ---------------------------------------------------------------------------


def test_empty_captions_is_an_input_macro_not_a_filter_one() -> None:
    """It lowers to an ``-i``, not to a node: no ffmpeg filter generates a
    subtitle stream, because a filtergraph carries no subtitle pads at all."""
    assert "empty_captions" not in MACROS
    macro = INPUT_MACROS["empty_captions"]
    assert (macro.output, macro.format) == ("subtitle", "webvtt")
    # "WEBVTT\n\n" -- a valid WebVTT file with zero cues, and nothing else.
    assert base64.b64decode(macro.path.split(",", 1)[1]) == b"WEBVTT\n\n"


def test_empty_captions_mints_an_input_with_no_registry_at_all() -> None:
    g = _lower("SELECT sqlmpeg.empty_captions() FROM input('x.mp4') a")
    assert not g.nodes
    assert g.input_paths[1] == INPUT_MACROS["empty_captions"].path
    assert g.input_options["sqlmpeg.empty_captions#2"] == {"format": "webvtt"}
    assert [(o.ref, o.type) for o in g.outputs] == [
        ("src:sqlmpeg.empty_captions#2:s:0", "subtitle")
    ]


def test_empty_captions_takes_no_arguments() -> None:
    err = _reject("SELECT sqlmpeg.empty_captions(2) FROM input('x.mp4') a")
    assert err.code == ErrorCode.UNSUPPORTED_SQL
    assert "takes no arguments" in err.message


def test_the_macro_namespace_hint_names_the_input_macro_too() -> None:
    err = _reject("SELECT sqlmpeg.zzz(a.frame, 2) FROM input('x.mp4') a")
    assert "empty_captions" in (err.hint or "")


def test_a_minted_input_is_not_a_user_facing_input_option() -> None:
    """`format` renders like any other per-input flag but cannot be written:
    it is not in the table `input('path', name => value)` validates against."""
    assert "format" not in INPUT_OPTIONS
    assert option_spec("format") is not None
    err = _reject("SELECT a.frame FROM input('x.mp4', format => 'webvtt') a")
    assert err.code == ErrorCode.UNKNOWN_INPUT_OPTION
