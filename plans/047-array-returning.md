# 047 — Array-returning calls: channelsplit, acrossover, extractplanes
(model: opus · main · RFC-006 wave 3)

Read plans/rfc-006-views-multisink.md § channelsplit, generalized per the
2026-08-16 gap discussion: a TABLE-DRIVEN mechanism (guardrail #4) covering
the three filters whose output count derives statically from an option.
Trimmable to channelsplit alone if review finds the others hairy; say so
rather than forcing them.

## The table (data, likely in lower.py or a small module)
ARRAY_RETURNING: filter -> (input type, element type, count rule):
- channelsplit (audio -> audio[N]): N = channel count of the
  channel_layout option (default 'stereo'); curated layout->count map:
  mono 1, stereo 2, 2.1 3, 3.0 3, quad 4, 5.0 5, 5.1 6, 5.1(side) 6,
  6.1 7, 7.1 8 (verify each against `ffmpeg -layouts` and trim/extend to
  what that output actually lists; unknown value -> FILTER_OPTION_TYPE
  listing the supported layouts).
- acrossover (audio -> audio[N]): N = len(split frequencies) + 1; the
  split option is a space/|-separated list — parse the literal the user
  passed (str), count entries; malformed -> FILTER_OPTION_TYPE.
- extractplanes (video -> video[N]): N = number of +-separated plane names
  in the planes option (default 'yuv' -> ... verify the real default via
  -help; count the requested planes; invalid plane letter is ffmpeg's
  runtime problem unless the enum constants catch it — check what the
  registry typed `planes` as).

## Mechanics (plan 046's contract notes, verified by that author)
- Hook in _Lowerer's dynamic-call path only (these are namespace/tier-2
  calls: ffmpeg.channelsplit(...); the registry FENCES them today —
  resolution must consult ARRAY_RETURNING before the fence verdict, i.e.
  a fenced name that is in the table becomes callable with the table's
  shape; other fenced names keep today's errors).
- _NodeFactory.node() already mints multi-pad nodes; build
  Node(outputs=[elem]*N), result value _array(elem, streams) with
  is_array=True EVEN when N == 1, refs "n<k>:<pad>".
- Provenance: 1:N fan — thread the single input _Stream.source to every
  element (not _agreed_source).
- Splat/subscript/broadcast on the result ride the existing machinery;
  channelsplit pads are ordinary pads (consume-once across groups; a pad
  read by two sinks gets asplit — already correct per 046).
- Other options of these filters validate normally via registry.options().
  enable: none of the three is T-flagged (verify) — normal rejection.

## Tests
- Offline (fixture registry gains the three + help blocks): count
  derivation per filter incl. defaults and malformed values; is_array on
  N==1 (channelsplit mono); splat into SELECT; subscript; broadcast a
  per-element op (volume over channelsplit result); provenance threading;
  fenced-but-not-in-table names still error (amerge).
- Exec: real stereo fixture NEEDED — extend scripts/gen_fixtures.py with
  a stereo-audio file (sine L + sine R via aeval or two sines + join?
  simplest: `-f lavfi -i "sine=440,aformat=channel_layouts=stereo"`?
  verify an incantation that yields genuine 2-channel audio; document it).
  Then: channelsplit -> two mono outputs (ffprobe channels=1 each);
  the round-trip split -> volume each -> amix back -> one stereo-ish out;
  acrossover 2-band run; extractplanes y-only run (gray output).
- Golden: none (registry-dependent).

## Docs
Minimal here (048 owns prose): dynamic-filters.md fence section gains one
paragraph ("three fenced filters are callable after all — array-returning:
...") — keep it accurate to what ships.

## Verify
ruff; mypy --strict changed modules; pytest tests/ -q FULLY green; -m exec
green; git diff tests/golden empty. Baseline 1291 + 99. No git commands; no
version bump (048).
