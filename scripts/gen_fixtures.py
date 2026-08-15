"""Generate tiny synthetic media fixtures for exec tests.

Uses ffmpeg's ``lavfi`` test-pattern sources (``testsrc2``, ``smptebars``) --
nothing copyrighted, nothing externally sourced, nothing large (guardrail #8
in sqlmpeg-project.md). Output goes to ``tests/fixtures/``, which is
gitignored.

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


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _generate(name: str, lavfi: str) -> None:
    out_path = FIXTURES_DIR / name
    if out_path.exists():
        print(f"skip (already exists): {out_path}")
        return
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    args = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        lavfi,
        "-pix_fmt",
        "yuv420p",
        str(out_path),
    ]
    print(f"generating: {out_path}")
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise SystemExit(f"ffmpeg failed generating {out_path}")


def _generate_av() -> None:
    """testsrc2 video + sine audio -- the only fixture with an audio stream."""
    out_path = FIXTURES_DIR / _AV_NAME
    if out_path.exists():
        print(f"skip (already exists): {out_path}")
        return
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    args = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=duration={_DURATION}:size={_SIZE}:rate={_RATE}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={_DURATION}",
        "-pix_fmt",
        "yuv420p",
        "-shortest",
        str(out_path),
    ]
    print(f"generating: {out_path}")
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise SystemExit(f"ffmpeg failed generating {out_path}")


def main() -> int:
    if not _ffmpeg_available():
        print("error: ffmpeg not found on PATH", file=sys.stderr)
        return 1
    for name, lavfi in _SOURCES.items():
        _generate(name, lavfi)
    _generate_av()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
