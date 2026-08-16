# Dynamic filters

The curated stdlib ([docs/stdlib.md](stdlib.md)) is a few dozen functions. The ffmpeg on your machine ships several hundred filters, everything from workhorse scalers to a generator whose whole job is rendering a vintage TV test pattern. sqlmpeg exposes all of them, with real type-checking, by asking the installed binary what it has (`ffmpeg -filters`, then `ffmpeg -help filter=<name>`) instead of waiting for someone to hand-write a table entry per filter.

## Two tiers

- **Tier 1, the curated stdlib.** Portable: every stdlib query compiles on any machine, with or without ffmpeg installed. Its argument order is the hand-picked, documented one (`scale(f, w, h)` rather than ffmpeg's own option spellings), and it wins every name collision. `scale`, `crop`, `blur` and a handful of others are both a stdlib function and a real filter name; the bare name is what the stdlib entry answers to, always, and `ffmpeg.scale(...)` is how you ask for the filter instead.
- **Tier 2, the dynamic registry.** Every filter the installed ffmpeg reports, minus the scope fence below. Machine-dependent on purpose: a query naming a tier-2 filter compiles only on a machine whose ffmpeg has that filter. The compiles-forever promise belongs to tier 1 alone; tier 2 promises exactly "whatever `ffmpeg -filters` says on this machine, right now," no more and no less.

Call a tier-2 filter by its ffmpeg name, stream inputs positional, every option by name:

```sql
SELECT unsharp(a.frame, luma_msize_x => 7, luma_amount => 1.5)
FROM input('clip.mp4') a
```

The positional count and types come straight from the filter's pad signature: `unsharp` is `V->V`, so exactly one video in; `xfade` is `VV->V`, so two. Options are named-only. `unsharp(a.frame, 7, 1.5)` is rejected, because tier 2 has no concept of ffmpeg's option order on purpose: `unsharp` has thirteen options, and named arguments mean you never have to remember which two the bare numbers would have bound to.

The same filter is also callable as `ffmpeg.unsharp(...)`. That spelling resolves in the registry alone, so it both reaches filters whose names Postgres has claimed and asks for the raw filter where a stdlib function shares the name — see [the `ffmpeg.` namespace](#the-ffmpeg-namespace-names-postgres-got-to-first) below.

### Tier-1 named extras

Stdlib calls also take trailing named arguments, reaching through to the underlying filter's full option set. The hand-picked positional signature covers the everyday case; named extras cover the other 90% of the option surface without anyone writing a second, wider variant of the same function:

```sql
blur(a.frame, 5, planes => 1)                    -- gblur, planes named
scale(a.frame, 1280, 720, flags => 'lanczos')
crossfade(a.frame, b.frame, 1, 8, transition => 'wipeleft')
```

A macro with no single underlying filter (`blur_regions` expands to a three-filter subgraph) takes no named arguments; there is no one filter to validate them against. That is per OVERLOAD, not per function: `delay` on audio is one `adelay` node and takes them, `delay` on video is a `format`+`tpad` macro and does not. A named argument colliding with something the positional signature already set is a typed error, never a silent override: `crop(f, 0, 0, 10, 10, w => 5)` tells you `'w'` is already taken.

## Named arguments and validation

Both tiers use the same `<name> => <value>` syntax. That is native Postgres named-argument notation, not an invention of this project, so guardrail #2 (every accepted query is valid Postgres) survives intact. The validation data is never hand-maintained either: the option's real name, type, numeric range, and enum constants all come out of `ffmpeg -help filter=<name>` at compile time. Two error codes cover the failure modes, `UNKNOWN_FILTER_OPTION` (with a did-you-mean against that filter's actual options) and `FILTER_OPTION_TYPE` (with the expected type plus the range or constant list), both documented with captured examples in [docs/errors.md](errors.md).

Values follow the dialect's usual literal rules: a bare number, `true`/`false`, or a single-quoted string. Enum options take the quoted constant name (`transition => 'wipeleft'`), never the underlying integer, because `'wipeleft'` will still mean something to you in six months.

Since named arguments are checked against the installed ffmpeg, they carry the same machine-dependence as tier 2 itself. One rule for the whole feature: **named arguments are your installed ffmpeg.** A query that compiles under `--portable` compiles everywhere.

## `--portable`

`compile`, `explain`, and `validate` all take `--portable`: stdlib only, no registry constructed, nothing introspected, nothing shells out. The compile sees exactly what a machine with no ffmpeg would see. A tier-2 filter name becomes `UNKNOWN_FUNCTION`; any named argument becomes `UNSUPPORTED_SQL`. Run it before handing a query to someone whose machine you don't control.

`run` doesn't take the flag: it executes ffmpeg, so there is nothing for an offline mode to do.

## The v1 scope fence

Not everything ffmpeg reports is callable:

- Only filters with a static pad signature and exactly one output make the cut: `V->V`, `A->A`, `AA->A`, `VV->V`, and friends. Variable pad counts (`N`: `split`, `concat`), multiple outputs (`scale2ref`, `feedback`), and sources/sinks (`|`: `testsrc`, `anullsink`) are excluded, and calling one gets an `UNSUPPORTED_SQL` naming the limitation rather than a partial guess.
- Options typed `binary` or `dictionary` have no representation in the dialect; setting one is `FILTER_OPTION_TYPE`. The filter's other options still work.
- The `enable` timeline expression and ffmpeg's runtime filter commands are out of scope entirely.

## The introspection cache

`ffmpeg -filters` is parsed at most once per process, on the first tier-2 lookup. Each filter's `-help filter=<name>` is parsed lazily, at most once, when that filter is first referenced. Nobody shells out 460 times up front. Introspection follows the same opportunistic policy as ffprobe-based probing: no ffmpeg on `PATH`, or output that fails to parse, degrades to an empty or partial registry rather than failing the compile. A stdlib-only query never notices.

Parsed results are cached on disk under `~/.cache/sqlmpeg/`, one file per ffmpeg build, keyed by a hash of `ffmpeg -version`'s first line. Upgrade ffmpeg or repoint `PATH` and the cache invalidates itself. `sqlmpeg.registry.clear_cache()` wipes both the in-process memo and the disk cache; it exists for tests, and for the day you rebuild ffmpeg under an unchanged version string and spend twenty minutes doubting your sanity.

`sqlmpeg explain` annotates dynamically-resolved filters in the IR dump, so the machine-dependence stays visible. The graph itself doesn't care: a tier-2 node is an ordinary node, and split, emit, and the golden tests neither know nor ask where it came from.

## The `ffmpeg.` namespace: names Postgres got to first

Some ffmpeg filter names are also Postgres grammar. `overlay` is `OVERLAY(x PLACING y FROM n FOR m)`, `trim` is `TRIM(BOTH ... FROM ...)`, `format` is the `FORMAT(...)` builtin — sqlglot (which parses every query, per guardrail #2) recognizes the special form before anything of sqlmpeg's runs, and the call arrives as something other than an ordinary function call: with its arguments folded into the wrong slot, or as a `PARSE_ERROR`.

Every filter is therefore also reachable as **`ffmpeg.<name>(...)`**, which never collides:

```sql
SELECT ffmpeg.trim(a.frame, start => 1, end => 4),
       ffmpeg.overlay(a.frame, b.frame, x => 20, y => 20, eof_action => 'pass')
FROM input('a.mp4') a, input('b.mp4') b
```

The special-form grammars key on a *bare* name, so qualifying the call bypasses all of them at once: `ffmpeg.<anything>(...)` parses uniformly, arguments and `=>` options intact. A namespaced call resolves in the dynamic registry **only** — never the stdlib, never a builtin — so `ffmpeg.scale(a.frame, w => 640, h => -2)` is ffmpeg's `scale` filter with its own options, while bare `scale(a.frame, 0.5)` stays the stdlib function. It is otherwise an ordinary tier-2 call in every respect: stream inputs positional from the pad signature, options named, the same scope fence, the same `UNKNOWN_FUNCTION` when there is no ffmpeg (or under `--portable`), and an ordinary node in the IR that split, emit and the goldens can't tell apart. `ffmpeg` is a reserved name: an alias or CTE called `ffmpeg` is `UNSUPPORTED_SQL`.

### The collision census

Measured, not guessed — a test parses `<name>(a.frame)` (plus two-, four-argument and `=>` forms) under `read="postgres"` for every filter in the registry and reports the names that do not arrive as an ordinary call. Against ffmpeg 7.1's 464 in-fence filters and sqlglot 30.17, eleven names collide:

| filter | what Postgres does with the bare name |
| --- | --- |
| `copy` | `COPY` statement grammar — `PARSE_ERROR` |
| `corr` | aggregate `corr(x, y)` |
| `format` | `FORMAT(...)` builtin — argument lost |
| `median` | `MEDIAN(...)` builtin |
| `normalize` | `NORMALIZE(...)` builtin |
| `null` | the `NULL` keyword — `PARSE_ERROR` |
| `overlay` | `OVERLAY(x PLACING y FROM n FOR m)` |
| `pad` | `PAD` grammar |
| `random` | `RANDOM()` builtin |
| `reverse` | `REVERSE(...)` builtin |
| `trim` | `TRIM(...)` builtin — argument lost |

Which shape a collision takes depends on the argument count (`overlay(a, b, 1, 2)` parses as the builtin, `overlay(a)` is a `PARSE_ERROR`), which is exactly why the census tests several arities rather than reasoning about the grammar. Four of the eleven — `overlay`, `pad`, `normalize`, `reverse` — are stdlib function names too, so their bare spelling was already the stdlib's and the collision only ever hid the raw filter.

`split` would collide the same way via `SPLIT_PART(...)`, but ffmpeg's `split`/`asplit` are variable-pad (`V->N`) and outside the scope fence, so it is `UNKNOWN_FUNCTION` under either spelling.

Two things the collisions never touched: `overlay`'s stdlib entry works positionally as it always has (`overlay(base, top, x, y)`), and sqlmpeg's own internal `trim`/`atrim` nodes — a CTE's `WHERE` window — are built directly, never through SQL call syntax (an input alias's window is not even a filter; it is an input seek, see [docs/trimming.md](trimming.md)).
