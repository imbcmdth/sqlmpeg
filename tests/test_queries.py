"""Tests for packages/ -- ready-to-run programs distilled from the cookbook.

Parametrized over every ``packages/*/*/queries/*.sql`` file: the header
comment's ``-- variables:`` list (read by ``sqlmpeg.vars.declared_variables``)
supplies dummy ``-v NAME=VALUE`` pairs, and the file is compiled through
``sqlmpeg.cli.main``. ``validate`` is the harness verb rather than
``compile`` because ``tracks-to-csv.sql`` is a metadata/CSV query, which the
``compile`` subcommand refuses by design (``compile`` wants an ffmpeg
command, ``validate`` accepts any compilable query, table or media) -- one
verb, uniform across all the query shapes here.

Beside the compiles, every package's manifest is read and cross-checked
against its ``queries/`` directory: every file there is named by the
manifest's ``bin``/``bins`` (no orphans), and ``read_manifest`` itself
already refuses a ``bin``/``bins`` entry naming a file that is not there (no
dangling entries).

HERMETIC: the registry comes from ``tests/conftest.py``'s snapshot shim
(autouse outside the ``exec`` tier); ``probe_path`` is stubbed here with a
rich synthetic ``ProbeResult`` (one video row, one audio row, one subtitle
row) carrying the language/codec/channel_layout/resolution metadata the
track-row queries filter on, so those queries compile without touching a
real file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sqlmpeg import cli, compiler
from sqlmpeg.probe import ChapterMeta, ProbeResult, StreamMeta
from sqlmpeg.project import read_manifest
from sqlmpeg.vars import declared_variables

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGES_DIR = REPO_ROOT / "packages"
QUERY_FILES = sorted(PACKAGES_DIR.glob("*/*/queries/*.sql"))
MANIFEST_FILES = sorted(PACKAGES_DIR.glob("*/*/sqlmpeg.json"))

_EXAMPLE_RE = re.compile(r"^-- example:", re.MULTILINE)

# Dummy values for every variable name used across packages/*/*/queries/*.sql
# -- chosen to match the synthetic probe below (language=eng, codec=aac,
# 1920x1080) so the track-row queries' WHERE predicates actually match a row
# rather than just happening to compile with an empty result.
_DUMMY_VALUES = {
    "source": "in.mp4",
    "dest": "out.mp4",
    "main": "in.mp4",
    "second": "in2.mp4",
    "overlay": "logo.mp4",
    "language": "eng",
    "codec": "aac",
    "width": "1920",
    "height": "1080",
    "start": "5",
    "end": "60",
    "factor": "2",
    "first": "one.mp4",
    "music": "music.m4a",
    "voice": "voiceover.wav",
    "subs": "subs.en.vtt",
    "cut": "120",
    "high": "1080p.mp4",
    "mid": "720p.mp4",
    "low": "480p.mp4",
    "high_w": "1920",
    "mid_w": "1280",
    "low_w": "854",
    "insert": "promo.mp4",
    "crf": "23",
    "gain": "0.5",
    "w": "640",
    "h": "480",
    "x": "100",
    "y": "50",
    "at": "10",
    "duration": "1",
    "dir": "clock",
    "rate": "1",
    "fps": "12",
    "prefix": "audio-",
    "ext": "m4a",
    "title": "My Film",
    "artist": "Me",
    "vcodec": "libx264",
    "acodec": "aac",
    "video_bitrate": "4M",
    "audio_bitrate": "192k",
    "preset": "slow",
    "scale": "iw/2",
    "i": "-23",
    "tp": "-2",
    "lra": "7",
}


def _variables(text: str) -> list[str]:
    declared = declared_variables(text)
    assert declared, "missing '-- variables:' header"
    return [variable.name for variable in declared]


@pytest.fixture(autouse=True)
def _synthetic_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    video = StreamMeta(
        type="video",
        index=0,
        metadata={},
        width=1920,
        height=1080,
        fps="30/1",
        sample_rate=None,
        codec="h264",
    )
    audio = StreamMeta(
        type="audio",
        index=0,
        metadata={"language": "eng"},
        width=None,
        height=None,
        fps=None,
        sample_rate=48000,
        codec="aac",
        channels=2,
        channel_layout="stereo",
    )
    subtitle = StreamMeta(
        type="subtitle",
        index=0,
        metadata={"language": "eng"},
        width=None,
        height=None,
        fps=None,
        sample_rate=None,
        codec="srt",
    )
    result = ProbeResult(
        streams=[video, audio, subtitle],
        # split-chapters.sql fans out over these, one output file per chapter.
        chapters=[
            ChapterMeta(index=1, start_t=0.0, end_t=30.0, title="Intro"),
            ChapterMeta(index=2, start_t=30.0, end_t=90.0, title="Credits"),
        ],
    )
    monkeypatch.setattr(compiler, "probe_path", lambda path: result)


def test_queries_dir_has_programs() -> None:
    assert len(QUERY_FILES) >= 6


def test_every_package_has_a_manifest() -> None:
    assert len(MANIFEST_FILES) == 8


@pytest.mark.parametrize("path", QUERY_FILES, ids=lambda p: p.relative_to(PACKAGES_DIR).as_posix())
def test_query_file_has_variables_header_and_example(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert declared_variables(text), f"{path.name}: missing '-- variables:' header"
    assert _EXAMPLE_RE.search(text), f"{path.name}: missing '-- example:' line"


@pytest.mark.parametrize("path", QUERY_FILES, ids=lambda p: p.relative_to(PACKAGES_DIR).as_posix())
def test_query_file_compiles(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    names = _variables(text)
    argv = ["validate", "-f", str(path)]
    for name in names:
        argv += ["-v", f"{name}={_DUMMY_VALUES[name]}"]
    code = cli.main(argv)
    assert code == 0, f"{path.name}: `sqlmpeg {' '.join(argv)}` did not compile"


@pytest.mark.parametrize(
    "manifest_path", MANIFEST_FILES, ids=lambda p: p.parent.relative_to(PACKAGES_DIR).as_posix()
)
def test_manifest_reads_and_names_every_query_file(manifest_path: Path) -> None:
    # `read_manifest` already refuses a `bin`/`bins` entry naming a file that
    # is not there -- a dangling entry is a rejection here, not a silent gap.
    package = read_manifest(manifest_path)
    named = {path.resolve() for path in package.programs.values()}
    present = {path.resolve() for path in (manifest_path.parent / "queries").glob("*.sql")}
    orphans = present - named
    assert not orphans, f"{manifest_path}: not named by bin/bins: {sorted(orphans)}"
