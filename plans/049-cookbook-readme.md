# 049 — Cookbook + README refocus  (model: opus · main)

Context: sqlmpeg is NOW ON PYPI (published by the maintainer). The README
becomes the package storefront; the example gallery moves to a cookbook.

## Deliverable 1: docs/examples.md — the cookbook

Real-world, repeatedly-asked tasks (the ffmpeg StackOverflow canon), simple
to complex. Each entry: the ask in one plain-language line, the SQL, the
real compiled command. Suggested ladder (adjust freely; every entry must be
idiomatic and COMPILE — drop any that fights the dialect and say so):

1.  Transcode a file to H.264/AAC (COPY WITH) — the #1 ask
2.  Extract the audio track to its own file
3.  Trim a clip (fast copy vs frame-accurate re-encode; link trimming.md)
4.  Resize to 1280 wide / half size
5.  Rotate a phone video 90 degrees
6.  Concatenate two clips (incl. the dual-language array pairing)
7.  Watermark a video (PNG + loop + centered overlay expression)
8.  Mux external subtitles in (vtt -> mov_text) / extract subtitles out
9.  Burn subtitles into the picture
10. Speed up 2x (video + audio together)
11. Crossfade between two clips
12. Video -> GIF (palettegen/paletteuse round trip via a view or CTE)
13. Replace a video's audio / add ducked background music
14. Picture-in-picture
15. Insert a clip at a timestamp (delay+overlay pip AND the splice variant)
16. Normalize loudness (all language tracks at once — broadcast)
17. Blur a region (blur_regions), blur during a time window (enable)
18. Generate test media (test pattern + tone; silent track for concat)
19. Stereo -> two mono tracks; multiband compression (channelsplit/
    acrossover round trips)
20. The ABR ladder (views + multiple outputs)

Fence convention (documented at the top of the file, mirroring prompt.py's):
- ```sql fence followed by a ``` command fence = compiled OFFLINE
  (--no-probe-safe, no registry: stdlib + explicit subscripts only).
- ```sql-exec fence = needs probe/registry; compiled (not run) against the
  real machine in an exec-marked test; its command fence is checked there.
- Generic harness in tests/test_examples.py: parse examples.md, extract
  (sql, command) pairs, compile each, assert the command matches
  byte-for-byte. Two parametrized tests (offline / exec). A pair whose sql
  fence has no following command fence fails with instructions. This
  REPLACES per-example hand tests; keep it simple and data-driven.
- Paths in examples are illustrative (film.mp4 etc.); exec-tier examples
  that need real files use tests/fixtures paths shown verbatim (the
  cookbook says so in a footnote) OR are compiled with the registry but
  probe-free where possible — prefer registry-only examples staying
  fixture-free (dynamic filters compile without probing).

Voice: the established earnest register. One line of context per example,
not an essay; deep links to docs/*.md where the long story lives.

## Deliverable 2: README refocus

New structure (keep the earnest voice; the deep narratives now live in
docs/ and the cookbook):
1. Title + the 3-sentence pitch + status line (now: on PyPI).
2. Install: `pip install sqlmpeg` / `uvx sqlmpeg` / `pipx run sqlmpeg`;
   ffmpeg on PATH unlocks probing + dynamic filters + run (stdlib compiles
   without it) — one short paragraph.
3. Quickstart: keep the PiP demo (fences byte-identical — its drift pins
   survive) and the ladder (same). Everything else moves to the cookbook.
4. CLI reference: a table per subcommand (compile/explain/validate/run/
   prompt) with flags and one-line meanings: -f/--file, -o, --graph-only,
   --json, --no-probe, --portable, --timeout, -y, --dynamic. Derive the
   text from cli.py's real help strings (do not invent).
5. Concepts in brief: 6-8 bullets (SELECT list = output streams; probe
   policy; two filter tiers + namespace; trims are seeks; sinks; views/
   multi-output) each linking to its doc.
6. Links: cookbook, docs/*.md, system prompt.
- MIGRATE the drift pins: README tests in test_lower.py whose sections
  move to the cookbook are deleted there (their coverage transfers to
  test_examples.py); the PiP + ladder + any retained README fences keep
  their existing pins (fences byte-identical). Audit every _readme_block
  needle against the new README and account for each in the report.

## Deliverable 3: packaging metadata

pyproject.toml gains `readme = "README.md"` in [project] (so PyPI renders
it on the next publish). ONLY that line; the user's metadata is otherwise
untouched. No version bump (docs-only; the maintainer decides when to
republish).

## Verify
ruff; mypy --strict on any new test module; pytest tests/ -q FULLY green;
-m exec green; git diff tests/golden empty; `pip install -e .` still clean
with the readme field. Baseline 1336 + 105. Report: the example list as
shipped (with any dropped + why), the pin-migration audit, gate outputs.
