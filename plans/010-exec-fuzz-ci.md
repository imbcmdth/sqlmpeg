# 010 — Exec tests, fuzz, CI  (model: sonnet · wave 5)

Read spec "Testing", guardrails #7/#8, existing code + tests.

## Deliverables
1. `scripts/gen_fixtures.py` — generates tiny synthetic media into `tests/fixtures/`
   (gitignored) with ffmpeg: `testsrc2=duration=2:size=320x240:rate=15` and
   `smptebars` variants, ~2s each. Idempotent (skip if exists). Stdlib only.
2. `tests/exec/test_exec.py` — marked `@pytest.mark.exec` (registered in pyproject;
   excluded by default via addopts `-m "not exec"`):
   - compile+run a scale query on a fixture → assert output exists, ffprobe reports
     expected width/height (parse `ffprobe -v error -select_streams v:0 -show_entries
     stream=width,height -of json`).
   - trim query → duration ≈ expected (±0.2s).
   - hflip query → perceptual-hash comparison: hash of hflipped output frame vs
     PIL-flipped source frame within threshold (imagehash; extract one frame via
     ffmpeg to png). Skip cleanly (`pytest.skip`) if ffmpeg/ffprobe missing.
3. `tests/test_fuzz.py` — hypothesis: strategy = pick a valid corpus query from
   tests/golden/*.sql, mutate (delete/dup/swap random slices, inject random tokens);
   property: `compile_sql` either returns Graph or raises SqlmpegError — assert
   `e.code is not ErrorCode.INTERNAL` for pure-SQL-syntax mutations is TOO STRICT;
   correct property: never a non-SqlmpegError exception. `max_examples=200`.
4. `.github/workflows/ci.yml` — ubuntu-latest, py 3.10 + 3.12 matrix: pip install
   -e .[dev]; ruff check; mypy (strict) on sqlmpeg/; pytest (default, no exec);
   then `sudo apt-get install -y ffmpeg`, gen_fixtures, `pytest -m exec`.
5. Add `tests/fixtures/` to .gitignore.

## Verify
ruff, pytest (fuzz included), and `pytest -m exec` locally (ffmpeg is on PATH here) —
all green. Report any compiler bugs the fuzz/exec tests uncover; do NOT paper over
them by weakening the property. No git commit.
