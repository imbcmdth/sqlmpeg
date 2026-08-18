"""Tests for queries/ -- ready-to-run programs distilled from the cookbook.

Parametrized over every ``queries/*.sql`` file: the header comment's
``-- variables:`` list supplies dummy ``-v NAME=VALUE`` pairs, and the file is
compiled through ``sqlmpeg.cli.main``. ``validate`` is the harness verb rather
than ``compile`` because ``tracks-to-csv.sql`` is a metadata/CSV query, which
the ``compile`` subcommand refuses by design (``compile`` wants an
ffmpeg command, ``validate`` accepts any compilable query, table or media) --
one verb, uniform across all the query shapes here.

HERMETIC: the registry comes from ``tests/conftest.py``'s snapshot shim
(autouse outside the ``exec`` tier); ``probe_path`` is stubbed here with a
rich synthetic ``ProbeResult`` (one video row, one audio row) carrying the
language/codec/channel_layout/resolution metadata the track-row queries
filter on, so those queries compile without touching a real file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sqlmpeg import cli, compiler
from sqlmpeg.probe import ProbeResult, StreamMeta

REPO_ROOT = Path(__file__).resolve().parent.parent
QUERIES_DIR = REPO_ROOT / "queries"
QUERY_FILES = sorted(QUERIES_DIR.glob("*.sql"))

_VARIABLES_RE = re.compile(r"^-- variables:\s*(?P<body>.+)$", re.MULTILINE)
_EXAMPLE_RE = re.compile(r"^-- example:", re.MULTILINE)
_NAME_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")

# Dummy values for every variable name used across queries/*.sql -- chosen to
# match the synthetic probe below (language=eng, codec=aac, 1920x1080) so the
# track-row queries' WHERE predicates actually match a row rather than just
# happening to compile with an empty result.
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
}


def _variables(text: str) -> list[str]:
    match = _VARIABLES_RE.search(text)
    assert match is not None, "missing '-- variables:' header"
    return _NAME_RE.findall(match.group("body"))


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
    result = ProbeResult(streams=[video, audio])
    monkeypatch.setattr(compiler, "probe_path", lambda path: result)


def test_queries_dir_has_programs() -> None:
    assert len(QUERY_FILES) >= 6


@pytest.mark.parametrize("path", QUERY_FILES, ids=lambda p: p.name)
def test_query_file_has_variables_header_and_example(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert _VARIABLES_RE.search(text), f"{path.name}: missing '-- variables:' header"
    assert _EXAMPLE_RE.search(text), f"{path.name}: missing '-- example:' line"


@pytest.mark.parametrize("path", QUERY_FILES, ids=lambda p: p.name)
def test_query_file_compiles(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    names = _variables(text)
    argv = ["validate", "-f", str(path)]
    for name in names:
        argv += ["-v", f"{name}={_DUMMY_VALUES[name]}"]
    code = cli.main(argv)
    assert code == 0, f"{path.name}: `sqlmpeg {' '.join(argv)}` did not compile"
