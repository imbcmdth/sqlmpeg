"""Generate tiny synthetic media fixtures for exec tests.

Uses ffmpeg's ``lavfi`` test-pattern sources (``testsrc2``, ``smptebars``,
``sine``) -- nothing copyrighted, nothing externally sourced, nothing large
(guardrail #8 in sqlmpeg-project.md). Output goes to ``tests/fixtures/``,
which is gitignored.

The set: ``testsrc.mp4`` / ``smptebars.mp4`` (video only), ``av.mp4`` (video +
one audio track), and ``av2.mp4`` / ``av3.mp4`` (video + TWO audio tracks
tagged ``language=eng`` / ``language=fra``), which is what the broadcasting
tests expand over. av2 and av3 differ only in their sine frequencies, so a
``UNION ALL`` of the two concatenates two distinguishable multi-language
sources whose language tags agree track for track.

Idempotent: a fixture whose output file already exists is skipped, so this
is safe to run repeatedly, including once per CI job right before the exec
test suite.

Usage::

    python scripts/gen_fixtures.py

Stdlib only -- no third-party imports, so this script itself never needs the
``[dev]`` extra installed.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

_DURATION = 2
_SIZE = "320x240"
_RATE = 15

# name -> lavfi source description
_SOURCES: dict[str, str] = {
    "testsrc.mp4": f"testsrc2=duration={_DURATION}:size={_SIZE}:rate={_RATE}",
    "smptebars.mp4": f"smptebars=duration={_DURATION}:size={_SIZE}:rate={_RATE}",
}

_AV_NAME = "av.mp4"
_AV2_NAME = "av2.mp4"
_AV3_NAME = "av3.mp4"
_SUBS_NAME = "subs.en.vtt"
_AVS_NAME = "avs.mkv"
_FRAME_PNG_NAME = "frame.png"

_SUBS_VTT = """WEBVTT

00:00:00.000 --> 00:00:00.600
Cue one.

00:00:00.700 --> 00:00:01.300
Cue two.

00:00:01.400 --> 00:00:02.000
Cue three.
"""


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _run(out_path: Path, args: list[str]) -> None:
    """Run one ffmpeg invocation, unless `out_path` is already there."""
    if out_path.exists():
        print(f"skip (already exists): {out_path}")
        return
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    print(f"generating: {out_path}")
    result = subprocess.run(["ffmpeg", "-y", *args, str(out_path)], capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise SystemExit(f"ffmpeg failed generating {out_path}")


def _generate(name: str, lavfi: str) -> None:
    _run(FIXTURES_DIR / name, ["-f", "lavfi", "-i", lavfi, "-pix_fmt", "yuv420p"])


def _generate_av() -> None:
    """testsrc2 video + one sine audio track: the simplest A/V fixture."""
    _run(
        FIXTURES_DIR / _AV_NAME,
        [
            "-f", "lavfi", "-i", f"testsrc2=duration={_DURATION}:size={_SIZE}:rate={_RATE}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={_DURATION}",
            "-pix_fmt", "yuv420p",
            "-shortest",
        ],
    )


def _generate_av2() -> None:
    """testsrc2 video + TWO language-tagged audio tracks (sine 440 eng, 880 fra).

    The broadcasting fixture (plan 020): a bare `a.audio` over this file is a
    2-element array, and each element carries a distinct language tag, so an
    expanded query can be checked for both its node count and its provenance.
    """
    _run(
        FIXTURES_DIR / _AV2_NAME,
        [
            "-f", "lavfi", "-i", f"testsrc2=duration={_DURATION}:size={_SIZE}:rate={_RATE}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={_DURATION}",
            "-f", "lavfi", "-i", f"sine=frequency=880:duration={_DURATION}",
            "-map", "0:v:0", "-map", "1:a:0", "-map", "2:a:0",
            "-metadata:s:a:0", "language=eng",
            "-metadata:s:a:1", "language=fra",
            "-pix_fmt", "yuv420p",
            "-shortest",
        ],
    )


def _generate_av3() -> None:
    """A second two-language fixture (sine 550 eng, 990 fra): av2's concat partner.

    Same shape as av2.mp4 -- same size, rate, duration and language tags, so a
    ``UNION ALL`` of the two is a legal concat -- but different tones, so the
    two segments of a concatenated output are told apart by ear.
    """
    _run(
        FIXTURES_DIR / _AV3_NAME,
        [
            "-f", "lavfi", "-i", f"testsrc2=duration={_DURATION}:size={_SIZE}:rate={_RATE}",
            "-f", "lavfi", "-i", f"sine=frequency=550:duration={_DURATION}",
            "-f", "lavfi", "-i", f"sine=frequency=990:duration={_DURATION}",
            "-map", "0:v:0", "-map", "1:a:0", "-map", "2:a:0",
            "-metadata:s:a:0", "language=eng",
            "-metadata:s:a:1", "language=fra",
            "-pix_fmt", "yuv420p",
            "-shortest",
        ],
    )


def _generate_subs_vtt() -> Path:
    """A tiny, valid WEBVTT file (3 cues over ~2s) -- plain text, no ffmpeg needed."""
    out_path = FIXTURES_DIR / _SUBS_NAME
    if out_path.exists():
        print(f"skip (already exists): {out_path}")
        return out_path
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    print(f"generating: {out_path}")
    out_path.write_text(_SUBS_VTT, encoding="utf-8")
    return out_path


def _generate_avs(subs_path: Path) -> None:
    """av.mp4's video+audio muxed with subs.en.vtt's caption track into an mkv.

    RFC-004's caption story: webvtt -> srt inside an mkv container, with a
    language tag on the subtitle stream so the fixture also exercises
    provenance-tag passthrough. `-c copy` covers video/audio; `-c:s srt`
    overrides the subtitle stream specifically (ffmpeg logs a benign
    "multiple -c options" notice for this because `-c copy` also nominally
    covers the subtitle stream before being overridden -- exit code is 0 and
    the resulting subtitle codec is srt, verified by an exec test).
    """
    _run(
        FIXTURES_DIR / _AVS_NAME,
        [
            "-i", str(FIXTURES_DIR / _AV_NAME),
            "-i", str(subs_path),
            "-map", "0:v:0", "-map", "0:a:0", "-map", "1:s:0",
            "-c", "copy",
            "-c:s", "srt",
            "-metadata:s:s:0", "language=eng",
        ],
    )


def _generate_frame_png() -> None:
    """One still frame of testsrc.mp4 (RFC-005 SS4, plan 041's PNG title-card
    exec test): `input(frame.png, loop => true, framerate => 15)` needs a
    single-frame image input, and this is the cheapest way to make one that
    is still visually distinguishable from a blank canvas. Must run after
    testsrc.mp4 exists."""
    _run(
        FIXTURES_DIR / _FRAME_PNG_NAME,
        [
            "-i", str(FIXTURES_DIR / "testsrc.mp4"),
            "-frames:v", "1",
            "-update", "1",
        ],
    )


def main() -> int:
    if not _ffmpeg_available():
        print("error: ffmpeg not found on PATH", file=sys.stderr)
        return 1
    for name, lavfi in _SOURCES.items():
        _generate(name, lavfi)
    _generate_av()
    _generate_av2()
    _generate_av3()
    subs_path = _generate_subs_vtt()
    _generate_avs(subs_path)
    _generate_frame_png()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
