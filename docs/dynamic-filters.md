# Dynamic filters

The curated stdlib ([docs/stdlib.md](stdlib.md)) is a few dozen functions. The ffmpeg on your machine ships several hundred filters, some of which you need, most of which you will never touch, and at least one of which exists solely to render an analog TV test pattern from 1950s Britain. sqlmpeg exposes all of them, with real type-checking, by asking the installed binary what it has (`ffmpeg -filters`, then `ffmpeg -help filter=<name>`) instead of waiting for someone to hand-write a table entry per filter.

## Two tiers

- **Tier 1, the curated stdlib.** Portable: every stdlib query compiles on any machine, with or without ffmpeg installed. Its argument order is the hand-picked, documented one (`scale(f, w, h)` rather than ffmpeg's own option spellings), and it wins every name collision. `scale`, `crop`, `blur` and a handful of others are both a stdlib function and a real filter name; the stdlib entry is what those names resolve to, always.
- **Tier 2, the dynamic registry.** Every filter the installed ffmpeg reports, minus the scope fence below. Machine-dependent on purpose: a query naming a tier-2 filter compiles only on a machine whose ffmpeg has that filter. The compiles-forever promise belongs to tier 1 alone; tier 2 promises "whatever `ffmpeg -filters` says on this box, right now," which is exactly as strong a promise as it sounds like.

Call a tier-2 filter by its ffmpeg name, stream inputs positional, every option by name:

```sql
SELECT unsharp(a.frame, luma_msize_x => 7, luma_amount => 1.5)
FROM input('clip.mp4') a
```

The positional count and types come straight from the filter's pad signature: `unsharp` is `V->V`, so exactly one video in; `xfade` is `VV->V`, so two. Options are named-only. `unsharp(a.frame, 7, 1.5)` is rejected, because tier 2 has no concept of ffmpeg's option order, and honestly, neither should you (unsharp has thirteen options; guess which two the bare numbers would bind to. Wrong).

### Tier-1 named extras

Stdlib calls also take trailing named arguments, reaching through to the underlying filter's full option set. The hand-picked positional signature covers the everyday case; named extras cover the other 90% of the option surface without anyone writing a second, wider variant of the same function:

```sql
blur(a.frame, 5, planes => 1)                    -- gblur, planes named
scale(a.frame, 1280, 720, flags => 'lanczos')
crossfade(a.frame, b.frame, 1, 8, transition => 'wipeleft')
```

A macro with no single underlying filter (`blur_regions` expands to a three-filter subgraph) takes no named arguments; there is no one filter to validate them against. A named argument colliding with something the positional signature already set is a typed error, never a silent override: `crop(f, 0, 0, 10, 10, w => 5)` tells you `'w'` is already taken.

## Named arguments and validation

Both tiers use the same `<name> => <value>` syntax. That is native Postgres named-argument notation, not an invention of this project, so guardrail #2 (every accepted query is valid Postgres) survives intact. The validation data is never hand-maintained either: the option's real name, type, numeric range, and enum constants all come out of `ffmpeg -help filter=<name>` at compile time. Two error codes cover the failure modes, `UNKNOWN_FILTER_OPTION` (with a did-you-mean against that filter's actual options) and `FILTER_OPTION_TYPE` (with the expected type plus the range or constant list), both documented with captured examples in [docs/errors.md](errors.md).

Values follow the dialect's usual literal rules: a bare number, `true`/`false`, or a single-quoted string. Enum options take the quoted constant name (`transition => 'wipeleft'`), never the underlying integer, on the theory that `'wipeleft'` will still mean something to you in six months and `21` will not.

Since named arguments are checked against the installed ffmpeg, they carry the same machine-dependence as tier 2 itself. One rule for the whole feature: **named arguments are your installed ffmpeg.** A query that compiles under `--portable` compiles everywhere.

## `--portable`

`compile`, `explain`, and `validate` all take `--portable`: stdlib only, no registry constructed, nothing introspected, nothing shells out. The compile sees exactly what a machine with no ffmpeg would see. A tier-2 filter name becomes `UNKNOWN_FUNCTION`; any named argument becomes `UNSUPPORTED_SQL`. Run it before handing a query to someone whose machine you don't control, which is to say, before handing a query to anyone.

`run` doesn't take the flag. It executes ffmpeg; an offline mode for it would be performance art.

## The v1 scope fence

Not everything ffmpeg reports is callable:

- Only filters with a static pad signature and exactly one output make the cut: `V->V`, `A->A`, `AA->A`, `VV->V`, and friends. Variable pad counts (`N`: `split`, `concat`), multiple outputs (`scale2ref`, `feedback`), and sources/sinks (`|`: `testsrc`, `anullsink`) are excluded, and calling one gets an `UNSUPPORTED_SQL` naming the limitation rather than a partial guess.
- Options typed `binary` or `dictionary` have no representation in the dialect; setting one is `FILTER_OPTION_TYPE`. The filter's other options still work.
- The `enable` timeline expression and ffmpeg's runtime filter commands are out of scope entirely.

## The introspection cache

`ffmpeg -filters` is parsed at most once per process, on the first tier-2 lookup. Each filter's `-help filter=<name>` is parsed lazily, at most once, when that filter is first referenced. Nobody shells out 460 times up front. Introspection follows the same opportunistic policy as ffprobe-based probing: no ffmpeg on `PATH`, or output that fails to parse, degrades to an empty or partial registry rather than failing the compile. A stdlib-only query never notices.

Parsed results are cached on disk under `~/.cache/sqlmpeg/`, one file per ffmpeg build, keyed by a hash of `ffmpeg -version`'s first line. Upgrade ffmpeg or repoint `PATH` and the cache invalidates itself. `sqlmpeg.registry.clear_cache()` wipes both the in-process memo and the disk cache; it exists for tests, and for the day you rebuild ffmpeg under an unchanged version string and spend twenty minutes doubting your sanity.

`sqlmpeg explain` annotates dynamically-resolved filters in the IR dump, so the machine-dependence stays visible. The graph itself doesn't care: a tier-2 node is an ordinary node, and split, emit, and the golden tests neither know nor ask where it came from.

## Known limitation: names Postgres got to first

A few ffmpeg filter names collide with grammar that Postgres (and therefore sqlglot, per guardrail #2) parses specially, before lowering ever sees an ordinary function call:

- **`overlay`.** Postgres has a builtin `OVERLAY(x PLACING y FROM n FOR m)`, so `overlay(...)` always parses with that grammar. Harmless day to day, since `overlay` is a stdlib function and the stdlib wins the name, but it means `overlay`'s named extras are unreachable: a `=>` inside `overlay(...)` is a `PARSE_ERROR` before sqlmpeg's handling ever runs.
- **`trim`.** Postgres owns string `TRIM(...)` (`TRIM(BOTH ... FROM ...)` and its relatives), and sqlglot parses a bare `trim(x)` with that grammar, folding the argument somewhere an ordinary call reader doesn't look. Calling `trim(...)` as a tier-2 filter therefore presents as a wrong arity instead of the filter you meant. Nothing internal is affected: a CTE's `WHERE` window still generates `trim`/`atrim` nodes directly (never through SQL call syntax), and an input alias's window doesn't generate a filter at all, it becomes an input seek (see [docs/trimming.md](trimming.md)).
- **`split`.** Would collide the same way via `SPLIT_PART(...)` grammar, but ffmpeg's `split`/`asplit` are variable-pad (`V->N`) and already outside the scope fence, so the collision never gets its moment.

A known wart, tracked separately, and documented here so it costs you a paragraph instead of an afternoon.
