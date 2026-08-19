# RFC-013 — Two-pass loudnorm  (accepted 2026-08-19; runs after
RFC-014 - third of the approved sequence)

Maintainer-designed shape: keep it a compiled command, no orchestrator.
A pipe cannot carry environment variables between pipeline siblings
(parallel spawn, env flows parent->child only), so the working form is
substitution-then-chain:

    eval "$(ffmpeg <pass 1: loudnorm=...:print_format=json -f null -> \
      2>&1 | sqlmpeg loudnorm2env)" && \
    ffmpeg ... loudnorm=...:measured_I='"${SQLMPEG_LN_I}"':...linear=true ...

## Pieces
- `sqlmpeg loudnorm2env` subcommand: stdin -> `export SQLMPEG_LN_*=...`
  lines parsed from loudnorm's JSON block (input_i, input_tp, input_lra,
  input_thresh, target_offset). ~20 lines, shipped in the CLI.
- SQL surface: `sqlmpeg.loudnorm2(stream, I => -16, TP => -1.5,
  LRA => 11)` macro; its presence flips emission to the two-phase
  sequence (the machinery two-pass x264 built). Pass 1 measures the
  same stream; pass 2's filtergraph splices `${SQLMPEG_LN_*}` via
  adjacent-quote concatenation.
- `run` does it in-process: execute pass 1, parse the JSON from stderr,
  substitute into pass 2's argv, execute. No shell, works on Windows.

## Costs, stated plainly (docs must carry both)
- The printed command depends on `sqlmpeg loudnorm2env` existing at run
  time - the first compiled output that is not pure ffmpeg. Escape
  hatch: run pass 1 by hand, paste the five numbers.
- The printed command is POSIX-shell only (`eval`, `$()`, `${}`);
  cmd.exe/PowerShell users use `run`.

## Fences
One loudnorm2 per query (v1); loudnorm2 + two_pass together: reject;
loudnorm2 inside a fan-out (RFC-014): reject in v1.

## When started
Failing recipe first (pin the full eval-chain), then one wave: macro +
subcommand + emission + run path + exec test (encode a fixture, ffprobe
the output loudness is near target).
