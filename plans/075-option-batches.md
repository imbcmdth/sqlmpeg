# 075 — Output-option and input-option batches  (model: sonnet · branch
option-batches · cookbook recipes 35 and 36 are the failing tests)

Maintainer approved both batches from the gaps review. Metadata writing
and disposition are OUT (separate design conversation).

## Sink options (sink.py + emit rendering per existing scopes)
Mirror existing specs; per-stream video scope where ffmpeg's flag takes
a stream index, container scope where global:
- `duration` (number, container) -> `-t <v>`  [recipe 35]
- `max_size` (str, container) -> `-fs <v>`
- `shortest` (bool, container) -> `-shortest`
- `maxrate` / `bufsize` (str, video) -> `-maxrate:<i>` / `-bufsize:<i>`  [35]
- `gop` (int, video) -> `-g:<i>`
- `profile` (str, video) -> `-profile:<i>`  [35]
- `level` (str, video) -> `-level:<i>`  [35]
- `tune` (str, video) -> `-tune:<i>`
- `codec_params` (str, video) -> pass-through private options: emit as
  `-x264-params`-style is codec-specific - DECIDE: emit
  `-<video_codec>-params:<i>` derived from the written video_codec when
  it is one of x264/x265/svtav1 (libx264->x264-params etc.); reject
  codec_params without a matching video_codec. Verify one such command
  against real ffmpeg before finalizing; report the choice.
- `movflags` (str, container) -> `-movflags <v>`; `faststart true`
  stays as sugar; both together: reject (one wins is a silent surprise).

## Input options (inputs.py)
- `seek_end` (number) -> `-sseof -<v>` (value is seconds from end;
  emit NEGATED)  [recipe 36]. Interaction with a WHERE window on the
  same alias: reject (two seek origins on one -i is incoherent).
- `format` (str) -> `-f <v>` before -i (capture devices, rawvideo,
  image2). This is USER-facing; the internal option added for
  empty-captions must remain internal - keep the two paths distinct
  and test that user `format =>` works on ordinary inputs.
- `realtime` (bool) -> `-re`
- `sub_charenc` (str) -> `-sub_charenc <v>`
- `start_number` (int) -> `-start_number <v>`
- `subtitle_decoder` (str) -> `-c:s <v>` before -i

## Tests
Unit per option (sink table + emit rendering + validation types/ranges;
input table + per-input placement + the seek_end/WHERE conflict + the
internal-format separation). Recipes 35/36 green through the harness
(report true bytes on wrapping/order trivia - the pins were hand-
wrapped). One exec test: a real encode with profile/level/maxrate runs;
a real seek_end run produces a shorter file than the source.
docs/system-prompt.md regen (SINK_OPTIONS/INPUT_OPTIONS render there).

## Verify
ruff + mypy --strict on touched sqlmpeg/ modules; test_sink, test_emit,
test_lower (input options live there?) - find the input-option tests'
home and extend in place; full default suite; full -m exec. Report:
option list as implemented with flag forms, the codec_params decision
with its ffmpeg verification, recipe verdicts, tails. No git.
