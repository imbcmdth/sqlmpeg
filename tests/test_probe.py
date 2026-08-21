"""Tests for sqlmpeg.probe.

Monkeypatched tests (subprocess/binaries.ffprobe_path faked) are unmarked so
the default suite (`pytest`, which runs `-m "not exec"`) stays offline. Tests
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
                "codec_name": "h264",
                "width": 320,
                "height": 240,
                "avg_frame_rate": "15/1",
                "bit_rate": "210584",
                "duration": "2.000000",
                "tags": {"language": "eng"},
            },
            {
                "index": 1,
                "codec_type": "subtitle",
            },
            {
                "index": 2,
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "44100",
                "channels": 2,
                "channel_layout": "stereo",
                "bit_rate": "128000",
                "duration": "2.000000",
                "tags": {"language": "eng", "title": "Track 1"},
            },
        ],
        "format": {"duration": "2.000000"},
    }
)


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    clear_cache()
    yield
    clear_cache()


def _fake_ffprobe_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe_mod.binaries, "ffprobe_path", lambda: "/usr/bin/ffprobe")


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


def test_url_spec_reaches_ffprobe_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """A '://' spec is handed to ffprobe as-is: ffprobe is the authority on
    its own protocols, and a remote input probes over the network (that is
    what naming a URL asks for). No local existence check applies."""
    _fake_ffprobe_present(monkeypatch)
    calls = _fake_run(monkeypatch)
    result = probe("https://example.com/master.mpd")
    assert result is not None and len(result.streams) > 0
    assert calls[0][-1] == "https://example.com/master.mpd"


def test_url_result_is_memoized_by_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    """No mtime exists for a URL, so the cache key is the spec string alone:
    one network probe per process, however many aliases name it."""
    _fake_ffprobe_present(monkeypatch)
    calls = _fake_run(monkeypatch)
    first = probe("https://example.com/master.mpd")
    second = probe("https://example.com/master.mpd")
    assert first is second
    assert len(calls) == 1


def test_url_failure_degrades_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unsupported scheme or unreachable host fails into the same
    permissive None every other unreadable input gets."""
    _fake_ffprobe_present(monkeypatch)
    _fake_run(monkeypatch, returncode=1)
    assert probe("rtsp://example.com/stream") is None


def test_missing_file_returns_none(tmp_path: Path) -> None:
    missing = tmp_path / "nope.mp4"
    assert probe(str(missing)) is None


def test_directory_returns_none(tmp_path: Path) -> None:
    assert probe(str(tmp_path)) is None


def test_ffprobe_absent_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "x.mp4"
    f.write_bytes(b"not really a video")
    monkeypatch.setattr(probe_mod.binaries, "ffprobe_path", lambda: None)
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
    # subtitle streams are mapped, not ignored, so all three streams
    # in FAKE_JSON (video, subtitle, audio) show up.
    assert len(result.streams) == 3
    # ProbeResult carries a container-level duration
    # from -show_format.
    assert result.duration == 2.0

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
            codec="h264",
            channels=None,
            channel_layout=None,
            bitrate=210584,
            duration=2.0,
            color_transfer=None,
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
            codec="aac",
            channels=2,
            channel_layout="stereo",
            bitrate=128000,
            duration=2.0,
            color_transfer=None,
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
            codec=None,
            channels=None,
            channel_layout=None,
            bitrate=None,
            duration=None,
            color_transfer=None,
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


# --- chapters (monkeypatched, offline) ---------------------------------------


def test_show_chapters_flag_is_passed_to_ffprobe(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_ffprobe_present(monkeypatch)
    calls = _fake_run(monkeypatch)
    probe("https://example.com/master.mpd")
    assert "-show_chapters" in calls[0]


def test_chapters_are_mapped_one_based_from_ffprobe_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "x.mkv"
    f.write_bytes(b"data")
    _fake_ffprobe_present(monkeypatch)
    chapters_json = json.dumps(
        {
            "streams": [{"codec_type": "video"}],
            "chapters": [
                {
                    "id": 7,  # container-specific; never reused as `index`
                    "start_time": "0.000000",
                    "end_time": "1.000000",
                    "tags": {"title": "Intro"},
                },
                {
                    "id": 8,
                    "start_time": "1.000000",
                    "end_time": "2.000000",
                    "tags": {"title": "Credits"},
                },
            ],
        }
    )
    _fake_run(monkeypatch, stdout=chapters_json)

    result = probe(str(f))
    assert result is not None
    assert [c.index for c in result.chapters] == [1, 2]
    assert [c.title for c in result.chapters] == ["Intro", "Credits"]
    assert [c.start_t for c in result.chapters] == [0.0, 1.0]
    assert [c.end_t for c in result.chapters] == [1.0, 2.0]


def test_a_chapter_missing_its_title_is_null(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Permissive like everything else here: no `tags` at all is a NULL
    title, not a dropped chapter or a failed probe."""
    f = tmp_path / "x.mkv"
    f.write_bytes(b"data")
    _fake_ffprobe_present(monkeypatch)
    chapters_json = json.dumps(
        {
            "streams": [{"codec_type": "video"}],
            "chapters": [{"start_time": "0.000000", "end_time": "1.000000"}],
        }
    )
    _fake_run(monkeypatch, stdout=chapters_json)

    result = probe(str(f))
    assert result is not None
    assert len(result.chapters) == 1
    assert result.chapters[0].title is None


def test_a_malformed_chapter_is_dropped_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One bad chapter entry does not null the whole probe -- the streams
    (and the other chapters) are still good."""
    f = tmp_path / "x.mkv"
    f.write_bytes(b"data")
    _fake_ffprobe_present(monkeypatch)
    chapters_json = json.dumps(
        {
            "streams": [{"codec_type": "video"}],
            "chapters": ["not a dict", {"start_time": "0.000000", "end_time": "1.000000"}],
        }
    )
    _fake_run(monkeypatch, stdout=chapters_json)

    result = probe(str(f))
    assert result is not None
    assert len(result.chapters) == 1
    assert result.chapters[0].index == 2  # ffprobe's own position, malformed entry included


def test_no_chapters_key_is_an_empty_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "x.mp4"
    f.write_bytes(b"data")
    _fake_ffprobe_present(monkeypatch)
    _fake_run(monkeypatch)  # FAKE_JSON has no "chapters" key

    result = probe(str(f))
    assert result is not None
    assert result.chapters == []


# --- probe enrichment: opportunistic, never raises -----


def test_enrichment_fields_absent_default_to_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """None of the new fields present in ffprobe's JSON -> every one is None,
    and parsing the REST of the stream still succeeds (opportunistic)."""
    f = tmp_path / "x.mp4"
    f.write_bytes(b"data")
    _fake_ffprobe_present(monkeypatch)
    minimal_json = json.dumps({"streams": [{"codec_type": "video", "width": 320}]})
    _fake_run(monkeypatch, stdout=minimal_json)

    result = probe(str(f))
    assert result is not None
    assert result.duration is None  # no "format" key at all
    v = result.streams[0]
    assert v.width == 320  # unaffected sibling field still parses
    assert v.codec is None
    assert v.channels is None
    assert v.channel_layout is None
    assert v.bitrate is None
    assert v.duration is None
    assert v.color_transfer is None


def test_enrichment_fields_wrong_typed_default_to_none_not_a_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ffprobe occasionally reports 'N/A' for bit_rate/duration; a bad value
    nulls only that field -- it must not blank the whole probe result the way
    the outer `_parse_streams` try/except would."""
    f = tmp_path / "x.mp4"
    f.write_bytes(b"data")
    _fake_ffprobe_present(monkeypatch)
    bad_json = json.dumps(
        {
            "streams": [
                {
                    "codec_type": "audio",
                    "sample_rate": "44100",
                    "channels": "not-a-number",
                    "bit_rate": "N/A",
                    "duration": "N/A",
                }
            ],
            "format": {"duration": "N/A"},
        }
    )
    _fake_run(monkeypatch, stdout=bad_json)

    result = probe(str(f))
    assert result is not None
    assert result.duration is None
    a = result.streams[0]
    assert a.sample_rate == 44100  # existing int-coercion field: unaffected
    assert a.channels is None
    assert a.bitrate is None
    assert a.duration is None


def test_enrichment_fields_present_are_parsed_with_correct_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "x.mp4"
    f.write_bytes(b"data")
    _fake_ffprobe_present(monkeypatch)
    hdr_json = json.dumps(
        {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "bit_rate": "5000000",
                    "duration": "12.5",
                    "color_transfer": "smpte2084",
                }
            ],
            "format": {"duration": "12.5"},
        }
    )
    _fake_run(monkeypatch, stdout=hdr_json)

    result = probe(str(f))
    assert result is not None
    assert result.duration == 12.5
    v = result.streams[0]
    assert v.codec == "hevc"
    assert v.bitrate == 5000000
    assert isinstance(v.bitrate, int)
    assert v.duration == 12.5
    assert v.color_transfer == "smpte2084"
    assert v.channels is None  # video row: audio-only field stays None
    assert v.channel_layout is None


def test_container_duration_absent_when_format_key_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "x.mp4"
    f.write_bytes(b"data")
    _fake_ffprobe_present(monkeypatch)
    _fake_run(monkeypatch, stdout=json.dumps({"streams": []}))

    result = probe(str(f))
    assert result is not None
    assert result.duration is None


# --- container tags (monkeypatched, offline) --------------------------------


def _probe_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw_format: object
) -> ProbeResult:
    """One probe of a file whose ffprobe output carries `raw_format`."""
    f = tmp_path / "x.mp4"
    f.write_bytes(b"data")
    _fake_ffprobe_present(monkeypatch)
    _fake_run(monkeypatch, stdout=json.dumps({"streams": [], "format": raw_format}))
    result = probe(str(f))
    assert result is not None
    return result


def test_container_tags_are_captured_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The FULL tag dict, at both levels: streams carry theirs the same way."""
    result = _probe_format(
        tmp_path,
        monkeypatch,
        {
            "duration": "2.000000",
            "tags": {
                "title": "Angel One",
                "artist": "Docs Dept",
                "major_brand": "isom",
            },
        },
    )
    assert result.tags == {
        "title": "Angel One",
        "artist": "Docs Dept",
        "major_brand": "isom",
    }


def test_stream_tags_are_captured_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stream's tags are the whole dict too, keys lowercased."""
    f = tmp_path / "x.mp4"
    f.write_bytes(b"data")
    _fake_ffprobe_present(monkeypatch)
    _fake_run(
        monkeypatch,
        stdout=json.dumps(
            {
                "streams": [
                    {
                        "codec_type": "audio",
                        "tags": {
                            "language": "eng",
                            "title": "Commentary",
                            "HANDLER_NAME": "SoundHandler",
                        },
                    }
                ]
            }
        ),
    )
    result = probe(str(f))
    assert result is not None
    assert result.streams[0].metadata == {
        "language": "eng",
        "title": "Commentary",
        "handler_name": "SoundHandler",
    }


def test_container_tag_keys_are_lowercased(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _probe_format(
        tmp_path, monkeypatch, {"tags": {"TITLE": "Angel One", "Artist": "Docs Dept"}}
    )
    assert result.tags == {"title": "Angel One", "artist": "Docs Dept"}


def test_container_tag_values_are_strings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _probe_format(tmp_path, monkeypatch, {"tags": {"date": 2026}})
    assert result.tags == {"date": "2026"}


def test_container_tags_absent_are_an_empty_dict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _probe_format(tmp_path, monkeypatch, {"duration": "2.0"}).tags == {}


def test_container_tags_wrong_typed_are_an_empty_dict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Permissive like every other field: a malformed value nulls that column,
    it does not fail the whole probe."""
    assert _probe_format(tmp_path, monkeypatch, {"tags": "nope"}).tags == {}


def test_container_tags_empty_when_format_key_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "x.mp4"
    f.write_bytes(b"data")
    _fake_ffprobe_present(monkeypatch)
    _fake_run(monkeypatch, stdout=json.dumps({"streams": []}))

    result = probe(str(f))
    assert result is not None
    assert result.tags == {}


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
    assert "-show_format" in argv  # needed for ProbeResult.duration


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
    """avs.mkv: av.mp4 + subs.en.vtt muxed with -c:s srt."""
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
    # The whole tag dict, not a whitelist: the muxer stamps an encoder and a
    # duration alongside the language.
    assert subtitle[0].metadata["language"] == "eng"
    assert set(subtitle[0].metadata) >= {"language", "encoder"}
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
def test_probe_av_eng_fixture_language_and_duration(_fixtures: Path) -> None:
    """av-eng.mp4: one eng-tagged audio track, ~4s -- both the
    per-stream and the container-level duration."""
    result = probe(str(_fixtures / "av-eng.mp4"))
    assert result is not None
    assert result.duration == pytest.approx(4.0, abs=0.2)

    audio = result.by_type("audio")
    assert len(audio) == 1
    assert audio[0].metadata.get("language") == "eng"
    assert audio[0].duration == pytest.approx(4.0, abs=0.2)
    assert audio[0].codec is not None


@pytest.mark.exec
def test_probe_tagged_fixture_carries_its_container_tags(_fixtures: Path) -> None:
    """tagged.mp4: title/artist/date written by the generator, no comment.

    The mp4 muxer adds an `encoder` tag of its own whose value tracks the
    ffmpeg build, so only its presence is checked, never its value.
    """
    result = probe(str(_fixtures / "tagged.mp4"))
    assert result is not None
    assert result.tags["title"] == "Angel One"
    assert result.tags["artist"] == "Docs Dept"
    assert result.tags["date"] == "2026"
    assert "comment" not in result.tags
    assert "encoder" in result.tags


@pytest.mark.exec
def test_probe_stereo_fixture_channel_layout(_fixtures: Path) -> None:
    """stereo.mp4: a real 2-channel `join` mux, channel_layout=stereo."""
    result = probe(str(_fixtures / "stereo.mp4"))
    assert result is not None

    audio = result.by_type("audio")
    assert len(audio) == 1
    assert audio[0].channels == 2
    assert audio[0].channel_layout == "stereo"


@pytest.mark.exec
def test_probe_av2_fixture_bitrate_and_codec(_fixtures: Path) -> None:
    """av2.mp4: video + two audio tracks, all with a real codec name
    and a positive bitrate from ffprobe."""
    result = probe(str(_fixtures / "av2.mp4"))
    assert result is not None

    video = result.by_type("video")
    audio = result.by_type("audio")
    assert len(video) == 1
    assert len(audio) == 2

    for stream in (*video, *audio):
        assert stream.codec  # non-empty codec name
        assert stream.bitrate is not None
        assert stream.bitrate > 0


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


# --- remote specs against a real ffprobe (localhost, no external network) ---


@pytest.mark.exec
def test_probe_http_url_end_to_end(_fixtures: Path) -> None:
    """The remote branch with a REAL ffprobe: serve the fixtures over
    localhost HTTP and probe av2.mp4 through http://. Exercises exactly the
    code path a DASH manifest or any other remote input takes, with no
    external network involved."""
    import http.server
    import threading
    from functools import partial

    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(_fixtures))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        result = probe(f"http://127.0.0.1:{port}/av2.mp4")
        assert result is not None
        audio = result.by_type("audio")
        assert [s.metadata.get("language") for s in audio] == ["eng", "fra"]
        assert result.by_type("video")[0].width == 320
    finally:
        server.shutdown()
        thread.join(timeout=5)
