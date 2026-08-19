# 072 — Wrap cookbook ffmpeg lines with bash continuations  (model:
sonnet · branch wrap-fences)

Maintainer directive: the pinned ffmpeg lines in docs/examples.md are
too long for GitHub's code width; wrap them with bash `\` continuations
at <= 90 chars/line. The byte-checked discipline survives by inverting
the comparison: the fence must equal the CANONICAL wrapping of the
actual stdout.

## The wrapper (canonical, deterministic)
New function `wrap_command(line: str, width: int = 90) -> str` in
tests/test_examples.py (it is harness policy, not product code):
- Applies only to lines starting `ffmpeg ` (table/CSV outputs and `$`
  lines stay single-line).
- Tokenize with shlex-compatible scanning of the emitted form (the
  emitter produces space-separated tokens, single-quote quoting only).
- Pack tokens onto lines <= width. Continuation lines indent 2 spaces.
  A token-boundary break puts a space before the ` \` so the splice is
  shell-exact.
- A single token longer than the remaining width AND of the simple form
  `'...'` (no embedded quote escapes) is split at `;` boundaries into
  adjacent quoted chunks - `'chunk1;'\` newline `'chunk2'` - relying on
  shell string concatenation. `,` is the fallback split point inside an
  oversized `;`-free segment. A token with no safe split point stays
  long (correctness over aesthetics).
- Deterministic: same input, same output, always.

## Harness change (tests/test_examples.py)
- Comparison becomes: fence body after the `$` line ==
  `wrap_command`-canonicalized actual stdout (apply wrap to each actual
  line; non-ffmpeg lines pass through). Byte-for-byte, still.
- Invariant test: for every wrapped fence,
  `shlex.split(fence_command_text)` == `shlex.split(actual_single_line)`
  - shlex implements both backslash-newline splicing and adjacent-quote
  concatenation, so this IS the proof the wrapped text is the same
  shell command.
- Unit tests for wrap_command: short line untouched; token packing at
  the boundary; the quoted-filtergraph chunk split (roundtrip via
  shlex.split); the no-safe-split-point long token; determinism.

## The regen
Rewrap every command fence in docs/examples.md mechanically (a
throwaway script driving wrap_command is fine - run it, do not commit
it). PROSE untouched. Then the whole cookbook harness green both tiers
proves every fence is the canonical wrapping.

## Out of scope
README fences (separate call), the `$ sqlmpeg` invocation lines,
docs/system-prompt.md, emit's actual output (the compiler still prints
one line - wrapping is presentation).

## Verify
ruff on test_examples.py; `pytest tests/test_examples.py -q` and
`-m exec -q` fully green; full default suite green; full -m exec tail
attributed. Report: wrap rules as implemented, longest remaining line,
fences touched count, tails.
