# 073 — Expand queries/ to a real library  (model: sonnet · branch
wrap-fences · firewall: do NOT touch docs/examples.md or
tests/test_examples.py - another agent owns them right now)

Maintainer: 6 programs is too few against 33 recipes; README table must
link each file.

## Deliverables
1. Grow queries/ to ~18-20 programs, distilled from cookbook recipes
   (read docs/examples.md for the shapes; copy technique, not prose).
   Candidates beyond the existing six: burn-subtitles, mux-subtitles,
   extract-subtitles, gif, poster-frame (single frame at a timestamp),
   clip (trim by :start/:end), speed (with :factor), crossfade,
   watermark, duck (music under voice), replace-audio, loudnorm-all,
   side-by-side, blur-region, ad-insert (splice at :cut), abr-ladder
   (view + 3 COPYs), split-channels. Skip any that would duplicate an
   existing file's shape with different constants.
2. Header convention (existing files show it): one-line purpose,
   `-- variables:` list with per-variable notes, one `-- example:`
   invocation. Short and factual.
3. queries/README.md: table rows for every file, the file column a
   RELATIVE MARKDOWN LINK (`[transcode.sql](transcode.sql)`), one-line
   description, variables column. Keep the intro paragraphs as they
   are.
4. tests/test_queries.py: the parametrized harness already sweeps
   queries/*.sql - extend the dummy-value table and the synthetic
   ProbeResult as needed (e.g. a subtitle stream for the subtitle
   programs; numeric dummies for :factor/:start/:cut - bare :name vars
   get numbers). Every program must compile hermetically (validate
   path); header test still passes for all files.

## Verify
ruff on tests/test_queries.py; `pytest tests/test_queries.py -q` green
(all programs); full default suite green. Report: file list with
variables, dummy-table additions, tails. No git.
