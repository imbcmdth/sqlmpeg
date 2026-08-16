# RFC-003 — Dynamic filters: the whole installed ffmpeg (draft)

Status: draft for discussion · 2026-08-15

## Motivation

The curated stdlib is 18 functions; ffmpeg ships ~450 filters. Users should be
able to call any filter their installed ffmpeg supports, with real
type-checking, without waiting for a table entry. The installed binary already
knows everything needed: `ffmpeg -filters` (names + pad signatures) and
`ffmpeg -help filter=X` (typed options, ranges, defaults, enum constants).

VERIFIED on ffmpeg 7.x:
- `-filters`: one line per filter — flags, name, pad spec (`V->V`, `AA->A`,
  `N` = dynamic pads, `|` = source/sink), description. ~450 entries.
- `-help filter=X`: per-option `name <type> flags description (from A to B)
  (default D)`; enum options list their constants on following lines.
- sqlglot 30.17 `read="postgres"` parses `f(a.frame, sigma => 5)` with
  `exp.Kwarg(this=Var(sigma), expression=Literal(5))` — named arguments are
  native Postgres syntax (guardrail #2 holds).

## Two-tier function model

- **Tier 1 — curated stdlib (unchanged):** portable across machines, macros
  (blur_regions), arg-order remapping (crop), positional args, documented
  forever promises. Wins every name collision.
- **Tier 2 — dynamic registry:** every filter reported by the installed
  ffmpeg, minus the v1 exclusions below. EXPLICITLY machine-dependent: a
  query using tier 2 compiles only where that filter exists. This revises
  guardrail language: the FOREVER promise applies to the dialect + tier 1;
  tier 2's promise is "whatever `ffmpeg -filters` says on this machine".

Call syntax for tier 2: stream inputs positionally (count/types from the pad
signature), options by NAME with `=>`:

```sql
SELECT unsharp(a.frame, luma_msize_x => 7, luma_amount => 1.5) FROM ...
SELECT compand(a.audio[1], attacks => '0.3', decays => '0.8') FROM ...
```

Tier 1 keeps positional-only (its signatures are the documented contract).
Named args on a tier-1 function → UDF_ARG_TYPE with a hint. Unknown names
search both tiers for did-you-mean; a tier-2 match when ffmpeg is ABSENT gets
a dedicated hint ("filter exists in ffmpeg but compile-time introspection
needs ffmpeg on PATH").

## Registry mechanics (mirrors the probe pattern)

`sqlmpeg/registry.py`:
- `-filters` parsed once per process on first tier-2 lookup; per-filter
  `-help filter=X` parsed lazily on first REFERENCE (never 450 calls).
- Disk cache under the user cache dir keyed by (ffmpeg path, version string
  from `ffmpeg -version` first line); process memo on top. `clear_cache()`.
- NEVER raises: no ffmpeg / unparseable output → empty registry / filter
  skipped. Unparseable single options degrade to type str (accept anything
  renderable) rather than dropping the filter — log nothing, stay quiet.
- Subprocess hygiene: argv lists, timeouts, guardrail #6.

## Type mapping and validation

| ffmpeg option type | SQL value | checks |
|---|---|---|
| int, int64, float, double, rational | num | range (from A to B) when parseable |
| boolean | bool | true/false |
| string, color, duration, image_size, video_rate | str | none |
| enum ("named constants") | str | must be one of the constants (or its int value) |
| flags | str | passthrough |
| binary, dictionary | rejected | option unusable → FILTER_OPTION_TYPE |

Expression-valued options (crop/scale accept "iw/2") type as str — quoted in
SQL. New error codes: `UNKNOWN_FILTER_OPTION` (did-you-mean from the filter's
real options), `FILTER_OPTION_TYPE` (expected type + range/constants in the
message). Both line-anchored (Kwarg values are literals → positions exist;
Var names likely carry none — same coarse-anchor caveat as sink options).

## v1 scope fence (typed rejections, not silence)

- Pad signature must be static with EXACTLY one output: `V->V`, `A->A`,
  `AA->A`, `VV->V`, etc. Dynamic (`N`) pads, multi-output, sources and sinks
  (`|`) → UNSUPPORTED_SQL naming the limitation.
- Input stream types checked against the signature (audio into `V->V` →
  UDF_ARG_TYPE).
- Broadcasting/zip works identically for tier 2 (the expansion machinery is
  type-driven and does not care where the spec came from).
- The `enable` timeline option and per-filter commands: out of scope.

## Determinism & availability

- Same policy as probing: introspection is opportunistic. Tier-1-only queries
  compile everywhere, ffmpeg or not. Tier-2 references without ffmpeg →
  typed error (not INTERNAL).
- `--portable` flag on compile/validate: reject tier-2 usage outright — for
  queries that must run on other machines.
- `explain` output annotates dynamically-resolved filters (name + the ffmpeg
  version they came from) so the machine-dependence is visible in the IR
  dump. Graph/IR shape itself is UNCHANGED (a node is a node; goldens
  unaffected).
- Goldens stay tier-1/symbolic. Tier-2 tests are exec-marked (they introspect
  the real ffmpeg).

## LLM surface

`sqlmpeg prompt --dynamic`: appends the installed ffmpeg's filter list
(name, signature, one-line description; options on the top ~N most useful? —
no: full option dumps for 450 filters would be enormous. Ship name +
signature + description for all, and note the model can rely on
validate --json's option-level errors to correct usage — the repair loop
covers the long tail). Base `sqlmpeg prompt` stays tier-1 and portable.

## Docs

- docs/stdlib.md unchanged (tier 1). New docs/dynamic-filters.md explains
  the tier model, the machine-dependence, and the introspection cache.
- README: short section with the unsharp example.

## Non-goals (this RFC)

Sources as table functions (`FROM testsrc(...)` — lovely, later), multi-output
filters, filter commands/timeline enable, sub-filtergraph macros over tier-2,
option expressions beyond quoted strings.
