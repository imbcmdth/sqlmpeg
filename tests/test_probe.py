"""Tests for sqlmpeg.probe.

Monkeypatched tests (subprocess/shutil.which faked) are unmarked so the
default suite (`pytest`, which runs `-m "not exec"`) stays offline. Tests
that shell out to a real ffprobe against generated fixtures are marked
`@pytest.mark.exec`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from sqlmpeg import probe as probe_mod
from sqlmpeg.probe import ProbeResult, StreamMeta, clear_cache, probe

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"

FAKE_JSON = json.dumps(
    {
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "width": 320,
                "height": 240,
                "avg_frame_rate": "15/1",
                "tags": {"language": "eng"},
            },
            {
                "index": 1,
                "codec_type": "subtitle",
            },
            {
                "index": 2,
                "codec_type": "audio",
                "sample_rate": "44100",
                "tags": {"language": "eng", "title": "Track 1"},
            },
        ]
    }
)


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    clear_cache()
    yield
    clear_cache()


def _fake_ffprobe_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe_mod.shutil, "which", lambda name: "/usr/bin/ffprobe")


def _fake_run(
    monkeypatch: pytest.MonkeyPatch, stdout: str = FAKE_JSON, returncode: int = 0
) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(probe_mod.subprocess, "run", fake_run)
    return calls


# --- URL / missing file / no ffprobe ---------------------------------------


def test_url_scheme_returns_none() -> None:
    assert probe("https://example.com/video.mp4") is None
    assert probe("rtsp://example.com/stream") is None
    assert probe("file:///etc/passwd") is None


def test_missing_file_returns_none(tmp_path: Path) -> None:
    missing = tmp_path / "nope.mp4"
    assert probe(str(missing)) is None


def test_directory_returns_none(tmp_path: Path) -> None:
    assert probe(str(tmp_path)) is None


def test_ffprobe_absent_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "x.mp4"
    f.write_bytes(b"not really a video")
    monkeypatch.setattr(probe_mod.shutil, "which", lambda name: None)
    assert probe(str(f)) is None


# --- subprocess failure modes (monkeypatched, offline) ----------------------


def test_nonzero_exit_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "x.mp4"
    f.write_bytes(b"data")
    _fake_ffprobe_present(monkeypatch)
    _fake_run(monkeypatch, stdout="", returncode=1)
    assert probe(str(f)) is None


def test_timeout_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "x.mp4"
    f.write_bytes(b"data")
    _fake_ffprobe_present(monkeypatch)

    def raise_timeout(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=5)

    monkeypatch.setattr(probe_mod.subprocess, "run", raise_timeout)
    assert probe(str(f)) is None


def test_bad_json_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "x.mp4"
    f.write_bytes(b"data")
    _fake_ffprobe_present(monkeypatch)
    _fake_run(monkeypatch, stdout="not json{")
    assert probe(str(f)) is None


def test_missing_streams_key_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "x.mp4"
    f.write_bytes(b"data")
    _fake_ffprobe_present(monkeypatch)
    _fake_run(monkeypatch, stdout=json.dumps({}))
    assert probe(str(f)) is None


def test_stream_missing_codec_type_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "x.mp4"
    f.write_bytes(b"data")
    _fake_ffprobe_present(monkeypatch)
    _fake_run(monkeypatch, stdout=json.dumps({"streams": [{"index": 0}]}))
    assert probe(str(f)) is None


def test_streams_not_a_list_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "x.mp4"
    f.write_bytes(b"data")
    _fake_ffprobe_present(monkeypatch)
    _fake_run(monkeypatch, stdout=json.dumps({"streams": "nope"}))
    assert probe(str(f)) is None


# --- field mapping (monkeypatched, offline) ---------------------------------


def test_maps_fields_including_subtitle_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "x.mp4"
    f.write_bytes(b"data")
    _fake_ffprobe_present(monkeypatch)
    _fake_run(monkeypatch)

    result = probe(str(f))
    assert result is not None
    # RFC-004: subtitle streams are mapped, not ignored, so all three streams
    # in FAKE_JSON (video, subtitle, audio) show up.
    assert len(result.streams) == 3

    video = result.by_type("video")
    audio = result.by_type("audio")
    subtitle = result.by_type("subtitle")
    assert video == [
        StreamMeta(
            type="video",
            index=0,
            metadata={"language": "eng"},
            width=320,
            height=240,
            fps="15/1",
            sample_rate=None,
        )
    ]
    assert audio == [
        StreamMeta(
            type="audio",
            index=0,
            metadata={"language": "eng", "title": "Track 1"},
            width=None,
            height=None,
            fps=None,
            sample_rate=44100,
        )
    ]
    assert subtitle == [
        StreamMeta(
            type="subtitle",
            index=0,
            metadata={},
            width=None,
            height=None,
            fps=None,
            sample_rate=None,
        )
    ]


def test_maps_data_streams(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "x.mp4"
    f.write_bytes(b"data")
    _fake_ffprobe_present(monkeypatch)
    data_json = json.dumps(
        {
            "streams": [
                {"codec_type": "video"},
                {"codec_type": "data", "tags": {"language": "eng"}},
                {"codec_type": "data"},
            ]
        }
    )
    _fake_run(monkeypatch, stdout=data_json)

    result = probe(str(f))
    assert result is not None
    assert len(result.streams) == 3
    data = result.by_type("data")
    assert [s.index for s in data] == [0, 1]
    assert data[0].metadata == {"language": "eng"}
    assert data[0].width is None
    assert data[0].height is None
    assert data[0].fps is None
    assert data[0].sample_rate is None


def test_attachment_codec_type_is_still_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "x.mp4"
    f.write_bytes(b"data")
    _fake_ffprobe_present(monkeypatch)
    attachment_json = json.dumps(
        {
            "streams": [
                {"codec_type": "video"},
                {"codec_type": "attachment"},
            ]
        }
    )
    _fake_run(monkeypatch, stdout=attachment_json)

    result = probe(str(f))
    assert result is not None
    assert len(result.streams) == 1
    assert result.streams[0].type == "video"


def test_per_type_index_counted_in_file_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "x.mp4"
    f.write_bytes(b"data")
    _fake_ffprobe_present(monkeypatch)
    two_video_json = json.dumps(
        {
            "streams": [
                {"codec_type": "video"},
                {"codec_type": "audio"},
                {"codec_type": "video"},
            ]
        }
    )
    _fake_run(monkeypatch, stdout=two_video_json)

    result = probe(str(f))
    assert result is not None
    assert [s.type for s in result.streams] == ["video", "audio", "video"]
    assert [s.index for s in result.streams] == [0, 0, 1]


# --- caching (monkeypatched, offline) ---------------------------------------


def test_cache_hit_avoids_second_subprocess_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "x.mp4"
    f.write_bytes(b"data")
    _fake_ffprobe_present(monkeypatch)
    calls = _fake_run(monkeypatch)

    r1 = probe(str(f))
    r2 = probe(str(f))
    assert r1 is not None
    assert r1 is r2  # same cached object, not just equal
    assert len(calls) == 1


def test_cache_invalidates_on_content_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "x.mp4"
    f.write_bytes(b"data")
    _fake_ffprobe_present(monkeypatch)
    calls = _fake_run(monkeypatch)

    r1 = probe(str(f))
    f.write_bytes(b"different content, different size")
    r2 = probe(str(f))
    assert r1 is not None
    assert r2 is not None
    assert r1 is not r2
    assert len(calls) == 2


def test_clear_cache_forces_reprobe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "x.mp4"
    f.write_bytes(b"data")
    _fake_ffprobe_present(monkeypatch)
    calls = _fake_run(monkeypatch)

    probe(str(f))
    clear_cache()
    probe(str(f))
    assert len(calls) == 2


def test_argv_is_a_list_with_expected_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "x.mp4"
    f.write_bytes(b"data")
    _fake_ffprobe_present(monkeypatch)
    calls = _fake_run(monkeypatch)

    probe(str(f))
    assert len(calls) == 1
    argv = calls[0]
    assert isinstance(argv, list)
    assert "-v" in argv and "error" in argv
    assert "-print_format" in argv and "json" in argv
    assert "-show_streams" in argv


# --- real ffprobe against generated fixtures --------------------------------


@pytest.fixture(scope="module")
def _fixtures() -> Path:
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "gen_fixtures.py")],
        check=True,
    )
    return FIXTURES_DIR


@pytest.mark.exec
def test_probe_avs_fixture_has_subtitle_stream_with_language_tag(_fixtures: Path) -> None:
    """avs.mkv (RFC-004, plan 033): av.mp4 + subs.en.vtt muxed with -c:s srt."""
    result = probe(str(_fixtures / "avs.mkv"))
    assert result is not None
    assert len(result.streams) == 3

    video = result.by_type("video")
    audio = result.by_type("audio")
    subtitle = result.by_type("subtitle")
    assert len(video) == 1
    assert len(audio) == 1
    assert len(subtitle) == 1

    assert subtitle[0].index == 0
    assert subtitle[0].metadata == {"language": "eng"}
    assert subtitle[0].width is None
    assert subtitle[0].height is None
    assert subtitle[0].fps is None
    assert subtitle[0].sample_rate is None


@pytest.mark.exec
def test_probe_video_only_fixture(_fixtures: Path) -> None:
    result = probe(str(_fixtures / "testsrc.mp4"))
    assert result is not None
    assert len(result.streams) == 1
    v = result.streams[0]
    assert v.type == "video"
    assert v.index == 0
    assert v.width == 320
    assert v.height == 240
    assert v.fps is not None
    assert v.sample_rate is None
    assert result.by_type("audio") == []


@pytest.mark.exec
def test_probe_av_fixture_has_video_and_audio(_fixtures: Path) -> None:
    result = probe(str(_fixtures / "av.mp4"))
    assert result is not None

    video = result.by_type("video")
    audio = result.by_type("audio")
    assert len(video) == 1
    assert len(audio) == 1

    assert video[0].index == 0
    assert video[0].width == 320
    assert video[0].height == 240

    assert audio[0].index == 0
    assert audio[0].sample_rate is not None
    assert audio[0].sample_rate > 0
    assert audio[0].width is None
    assert audio[0].height is None
    assert audio[0].fps is None


@pytest.mark.exec
def test_probe_missing_file_returns_none_with_real_ffprobe(_fixtures: Path) -> None:
    assert probe(str(_fixtures / "does-not-exist.mp4")) is None


@pytest.mark.exec
def test_probe_caches_real_ffprobe_call(
    _fixtures: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_cache()
    calls: list[list[str]] = []
    orig_run = subprocess.run

    def counting_run(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return orig_run(argv, **kwargs)  # type: ignore[return-value]

    monkeypatch.setattr(probe_mod.subprocess, "run", counting_run)
    path = str(_fixtures / "testsrc.mp4")
    r1 = probe(path)
    r2 = probe(path)
    assert r1 is not None
    assert r1 is r2
    assert len(calls) == 1


def test_probe_result_dataclass_shape() -> None:
    # ProbeResult/StreamMeta are frozen dataclasses -- sanity check equality
    # and immutability, which the field-mapping tests above rely on.
    r = ProbeResult(streams=[])
    assert r.by_type("video") == []
    assert r.by_type("audio") == []
    with pytest.raises(Exception):
        r.streams = []  # type: ignore[misc]
