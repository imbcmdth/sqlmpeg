# 079 — Two-pass encoding: compiled command sequences  (model: opus ·
branch gaps-batch-2 · runs AFTER 078; recipe 43 is the failing test)

The architectural piece: a compile may now produce a SEQUENCE of
commands. Two-pass x264 is the first user.

## Surface
Sink option `two_pass` (bool, container-ish). Rules:
- requires `video_codec` in a family that supports `-pass` (libx264,
  libx265) AND `video_bitrate` (two-pass exists to hit a bitrate);
  `crf` + `two_pass` -> reject (contradictory rate control).
- Multi-sink scripts with two_pass anywhere: reject in v1 (sequencing
  per-COPY passes is a design question nobody has asked yet; note it).

## Emission (recipe 43 pins the shape)
- emit produces a LIST of argv lists; build_ffmpeg_args grows the shape
  (or a sibling function - pick the least-churn seam and say why).
  Single-command queries: list of one, nothing else changes anywhere.
- Pass 1: the same graph, VIDEO outputs only (audio maps omitted
  entirely - deterministic, no -an needed), `-pass 1 -passlogfile
  <dest> -f null -`. Pass 2: the full command + `-pass 2 -passlogfile
  <dest>` writing dest. VERIFY the whole chain against real ffmpeg
  end-to-end (encode a fixture, check the output exists and the stats
  file appeared) - if pass 1 needs different flags than the pin
  guesses, true-bytes report.
- compile prints the commands joined ` && ` as ONE stdout line;
  wrap_command handles the fence display (&& is an ordinary token).
- run executes the sequence in order, stopping at the first nonzero
  exit and returning it. Timeout applies per command.

## Tests
Unit: the sequence shape (one command stays one), the option rules and
rejections, pass-1 map filtering, passlogfile derivation. Exec: the
full two-pass encode of a fixture runs, output plays (ffprobe reads
it), stats file cleanup NOT attempted (ffmpeg owns its temp files;
document that the log file remains beside dest). Recipe 43 green.

## Verify
ruff + mypy --strict; full default suite green; full -m exec green
INCLUDING recipes 41-44; tails. Report: the emit seam chosen, pass-1
flag verification evidence, tails. No git.
