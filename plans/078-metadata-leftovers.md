# 078 — Metadata leftovers + N-input filter additions  (model: sonnet ·
branch gaps-batch-2 · recipes 41, 42, 44 are the failing tests; recipe
43 is the NEXT wave's, leave it red)

## Item 1: finish the metadata story
- `disposition` as a RESERVED tag key: a tag column aliased
  `disposition` emits `-disposition:<N> <value>` instead of
  `-metadata:s:<N>`. Value is ffmpeg's spec string ('default',
  'forced', 'default+forced', '0' to clear) - text, not validated
  beyond being text (ffmpeg owns the vocabulary). Rides the whole tag
  machinery (CASE etc.) - recipe 41.
- Sink options: `title` and `comment` (str, container) ->
  `-metadata title=<v>` / `-metadata comment=<v>` (global, no :s).
- Sink option `metadata_from <alias>` (identifier value, like
  chapters_from) -> `-map_metadata <input index>`. Recipe 42.
- Sink option `strip_metadata` (bool) -> `-map_metadata -1` (drops the
  tags the muxer would copy implicitly; sqlmpeg-threaded per-stream
  tags still emit and win). Conflicts: strip_metadata + metadata_from
  both set -> reject.

## Item 2: N-input filters
Add to the N_INPUT table (registry precedence + the acrossfade pattern
- check each filter's fenced option NAME in the registry first):
- `amerge` (audio -> audio, option `inputs`)  - recipe 44
- `join` (audio -> audio, option `inputs`)
- `interleave` (video -> video) and `ainterleave` (audio -> audio) -
  their count option is `nb_inputs` (VERIFY via fenced_options; the
  table's `option` field exists for exactly this)
emit_default=True for all four (always-fenced family, amix precedent).

## Tests
Unit per option/filter (emission, conflicts, disposition-vs-metadata
routing, strip semantics); exec: disposition read back via ffprobe
(disposition block in -show_streams), a real amerge run, metadata_from
round-trip. Recipes 41/42/44 green (true-bytes reports on wrap/order
trivia - pins hand-authored); recipe 43 stays red. Regen
docs/system-prompt.md (option tables render there).

## Verify
ruff + mypy --strict; full default suite green except recipe 43's exec
case; full -m exec attributed the same. Report: as-implemented list,
the interleave option-name verdict, tails. No git.
