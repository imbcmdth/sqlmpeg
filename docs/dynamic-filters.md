# Dynamic filters (RFC-003)

The curated stdlib (see [docs/stdlib.md](stdlib.md)) is a couple dozen
functions; the ffmpeg most people have installed ships several hundred
filters. sqlmpeg exposes the rest of them too, with real type-checking, by
asking the installed `ffmpeg` binary what it supports (`ffmpeg -filters`,
`ffmpeg -help filter=<name>`) instead of waiting for a hand-written table
entry for each one.

## Two tiers

- **Tier 1 -- the curated stdlib.** Portable: every stdlib query compiles on
  any machine, with or without ffmpeg installed. Its argument order is the
  documented, hand-picked one (`scale(f, w, h)`, not ffmpeg's own option
  names), and it always wins a name collision -- `scale`, `crop`, `blur`, and
  a handful of others are both a stdlib function and a real ffmpeg filter
  name, and the stdlib entry is what a query calling that name gets.
- **Tier 2 -- the dynamic registry.** Every filter the installed `ffmpeg`
  reports, minus a v1 scope fence (below). EXPLICITLY machine-dependent: a
  query naming a tier-2 filter compiles only on a machine whose ffmpeg has
  that filter. The FOREVER-compiles promise is tier 1's alone; tier 2's
  promise is "whatever `ffmpeg -filters` says on this machine, right now".

Call a tier-2 filter directly by its ffmpeg name, with its stream inputs
positional (count and type read straight from the filter's pad signature)
and every option passed by name:

```sql
SELECT unsharp(a.frame, luma_msize_x => 7, luma_amount => 1.5)
FROM input('clip.mp4') a
```

`unsharp` is `V->V` (one video in, one video out), so it takes exactly one
positional stream argument; `xfade` is `VV->V` and takes two. Nothing else is
positional -- `unsharp(a.frame, 7, 1.5)` is rejected, because the tier-2
surface has no concept of ffmpeg's own positional option order, only names.

### Tier-1 named extras

Tier-1 (stdlib) calls can ALSO take trailing named arguments, reaching
through to the underlying filter's full option set -- the hand-picked
positional signature covers the common case, named extras cover the rest
without a second, wider variant of the same function:

```sql
blur(a.frame, 5, planes => 1)                    -- gblur, planes named
scale(a.frame, 1280, 720, flags => 'lanczos')
crossfade(a.frame, b.frame, 1, 8, transition => 'wipeleft')
```

A macro spec with no single underlying filter (`blur_regions`, which expands
to several chained filters) takes no named arguments at all -- there is no
one filter to validate them against. A named argument that collides with
something the positional signature already sets is rejected rather than
silently overridden: `crop(f, 0, 0, 10, 10, w => 5)` is a typed error naming
`'w'` as already set.

## Named arguments and validation

Both tiers use the same `<name> => <value>` syntax (native Postgres `Kwarg`
syntax, not something sqlmpeg invented) and the same validation path: the
option's real name, type, numeric range, and (for an enum option) its set of
named constants are all read out of the installed ffmpeg via
`ffmpeg -help filter=<name>`, never hand-maintained in sqlmpeg. Two error
codes cover it -- `UNKNOWN_FILTER_OPTION` (a did-you-mean against that
filter's real option names) and `FILTER_OPTION_TYPE` (the expected type, plus
the range or the constant list) -- documented with real captured examples in
[docs/errors.md](errors.md).

Values follow the same literal rules as everywhere else in the dialect: a
bare number, `true`/`false`, or a single-quoted string. An enum option takes
a quoted constant name (`transition => 'wipeleft'`), never its underlying
integer.

Because named arguments are checked against the installed ffmpeg, they carry
the same machine-dependence as tier 2 itself: a query using them compiles
only where that ffmpeg (or one whose options happen to match) is on `PATH`.
There is one rule for the whole feature: **named arguments are your installed
ffmpeg.** A query that compiles under `--portable` compiles everywhere.

## `--portable`

`compile`, `explain`, and `validate` all take `--portable`: it compiles
against the stdlib alone. No registry is even constructed -- nothing is
introspected, nothing shells out -- so the compile sees exactly what a
machine with no ffmpeg installed at all would see. A tier-2 filter name
becomes `UNKNOWN_FUNCTION`; a named argument (tier-2 or tier-1 extra) becomes
`UNSUPPORTED_SQL`. Use it to confirm a query will compile on someone else's
machine before you hand it to them, or to keep a saved query intentionally
portable.

`run` does not take `--portable` -- it always needs a real, installed ffmpeg
to execute the command against, so there is no offline mode for it to opt
into.

## v1 scope fence

Not every filter ffmpeg reports is usable from tier 2:

- Only a filter whose pad signature is STATIC with EXACTLY one output is
  included -- `V->V`, `A->A`, `AA->A`, `VV->V`, and so on. A variable pad
  count (`N`, e.g. `split`, `concat`), more than one output (e.g. `scale2ref`,
  `feedback`), and sources/sinks (`|`, e.g. `testsrc`, `anullsink`) are
  excluded -- calling one of those names is `UNSUPPORTED_SQL` naming the
  limitation, not a silent partial match.
- An option whose ffmpeg type is `binary` or `dictionary` is unrepresentable
  in the dialect and is rejected as `FILTER_OPTION_TYPE` if you try to set
  it; the filter itself is still usable if it has other, settable options.
- The `enable` timeline option and ffmpeg's per-filter runtime commands are
  out of scope entirely.

## Machine dependence and the introspection cache

`ffmpeg -filters` is parsed at most once per process, the first time any
tier-2 lookup needs it; each filter's `ffmpeg -help filter=<name>` is parsed
lazily, at most once per filter, the first time that filter is REFERENCED --
never all ~460 of them up front. Introspection is opportunistic, the same
policy as ffprobe-based probing (RFC-001): no ffmpeg on `PATH`, or output
that fails to parse, degrades to an empty (or partial) registry rather than
failing the compile -- a query that only needs the stdlib compiles regardless.

The parsed result is cached on disk under `~/.cache/sqlmpeg/` (falling back
to a temp directory if the home directory cannot be resolved), one file per
`(ffmpeg path, version string)` pair, keyed by a hash of `ffmpeg -version`'s
first line -- so upgrading ffmpeg, or pointing `PATH` at a different build,
transparently invalidates the cache and reintrospects. `sqlmpeg.registry
.clear_cache()` resets both the in-process memo and the on-disk file(s); it
is mainly for tests, but useful any time you suspect a stale cache (e.g. a
rebuild of ffmpeg under the same version string).

`sqlmpeg explain` annotates dynamically-resolved filters so the
machine-dependence stays visible in the IR dump; the graph shape itself is
unaffected -- a tier-2 node is an ordinary node, so split, emit and the
goldens neither know nor care where it came from.

## Known limitation: builtin-name collisions

A handful of ffmpeg filter names collide with grammar Postgres (and
therefore sqlglot, guardrail #2) parses specially, before lowering ever sees
an ordinary function call:

- **`overlay`.** Postgres has a builtin `OVERLAY(x PLACING y FROM n FOR m)`,
  so `overlay(...)` always parses with that grammar. This is harmless in
  practice -- `overlay` is also a stdlib function, and the stdlib always wins
  the name -- but it does mean `overlay`'s named extras are unreachable: a
  `=>` inside `overlay(...)` is a `PARSE_ERROR` before sqlmpeg's own named-
  argument handling ever runs. There is no way to reach the underlying
  filter's other options through the `overlay` name at all.
- **`trim`.** Postgres has a builtin string `TRIM(...)` (`TRIM(BOTH ... FROM
  ...)` and friends), and sqlglot parses a bare `trim(x)` call with that
  grammar too, folding the argument into a different part of the parsed
  expression than an ordinary function call would. sqlmpeg's own lowering
  still generates ffmpeg's `trim`/`atrim` filters internally for a
  `WHERE <alias>.t BETWEEN ...` window on a CTE name (RFC-004: a CTE output
  is a filtergraph pad, not an input, so its window stays a filter trim) --
  that path is unaffected, since it never goes through SQL call syntax. A
  window on an `input()` alias no longer generates a filter at all; it
  lowers to an input-level `-ss`/`-to` seek instead (see
  [docs/trimming.md](trimming.md)). Either way, calling `trim(...)` directly
  as a tier-2 filter name does not work: the argument sqlglot parsed does not
  land where the tier-2 call reader looks, so it presents as a wrong arity
  rather than the filter you asked for.
- **`split`.** ffmpeg's `split`/`asplit` filters have a variable pad count
  (`V->N`) and are already excluded by the v1 scope fence above, so this one
  is moot in practice -- but for the record, Postgres/sqlglot's own
  `SPLIT(...)`/`SPLIT_PART(...)` grammar would collide with it the same way
  `trim` does if it were ever includable.

This is a known wart, not a design choice, and is tracked separately from
this RFC. It is not fixed here: this document exists to name the limitation
so it is not a surprise, not to work around it.
