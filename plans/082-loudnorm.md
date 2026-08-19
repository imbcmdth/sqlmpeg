# 082 — Two-pass loudnorm  (model: opus · branch loudnorm · recipe 49
is the failing test)

Read plans/rfc-013-loudnorm-two-pass.md (accepted) and recipe 49 - its
pin is the full eval-chain, hand-authored to this spec:
- Pass 1: the macro's stream through
  `loudnorm=<written opts>:print_format=json`, mapped, `-f null -`,
  stderr piped (2>&1) into `sqlmpeg loudnorm2env`.
- `loudnorm2env`: new CLI subcommand, stdin -> `export SQLMPEG_LN_I=...`
  etc. for input_i, input_tp, input_lra, input_thresh, target_offset
  (names LN_I, LN_TP, LN_LRA, LN_THRESH, LN_OFFSET). Finds the LAST
  JSON block in the input (ffmpeg logs precede it). Non-JSON input:
  exit 1 with a plain error line on stderr.
- Pass 2: same written options + measured_* + offset + linear=true; the
  `${SQLMPEG_LN_*}` references splice via adjacent-quote concatenation
  exactly as pinned.
- compile joins with ` && ` and prints ONE line; the wrapper does not
  touch lines starting `eval ` (verify: it keys on `ffmpeg ` - add a
  unit test pinning that pass-through so nobody "fixes" it later).
- run: no shell - execute pass 1 capturing stderr, parse the JSON in-
  process (share the parser with loudnorm2env), substitute into pass
  2's argv, execute. Timeout per command.

Surface: `sqlmpeg.loudnorm2(stream, I => ..., TP => ..., LRA => ...)`
macro (audio only; options named-only with these three names, all
optional with loudnorm's own defaults if omitted - render only what was
written, plus print_format/measured/linear per phase). Fences from the
RFC: one loudnorm2 per query; + two_pass reject; + fan-out reject;
inside table/CSV queries reject (it is a filter).

## Tests
Unit: macro validation and both phases' filtergraph rendering; the
env-splice quoting; loudnorm2env parsing (real captured ffmpeg stderr
as fixture text, noise before JSON, missing keys -> error); the wrapper
pass-through; the fences. Exec: the full run path on a fixture -
measure, substitute, encode - then ffprobe/second-measure the output
and assert integrated loudness within 1 LU of the target. A shell test
of the printed command itself via bash IF bash is available on the
runner (skip cleanly if not - Windows dev boxes have Git Bash;
document what you did).

## Verify
ruff + mypy --strict; recipe 49 green (true-bytes on trivia; STOP on
semantics); full default suite; full -m exec attributed; regen
docs/system-prompt.md (macro list changes). Report: subcommand
mechanics, run-path evidence with measured loudness numbers, tails.
No git.
