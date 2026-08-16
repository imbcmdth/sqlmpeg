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

### `enable`: turning a filter on and off as the stream plays

One named argument is not an option of any filter. `enable` is ffmpeg's *timeline* switch, implemented in the filter framework rather than in the filters themselves, and it takes an expression that ffmpeg evaluates per frame: while it is true the filter runs, while it is false the frame passes through untouched.

```sql
SELECT blur(a.frame, 12, enable => 'between(t,0.5,1.5)')
FROM input('clip.mp4') a
```

That blurs exactly one second in the middle of the clip and leaves the rest sharp — with no trim, no branch, and no concat. It is accepted on both tiers, positionally identical to any other named argument: on a tier-2 call (`ffmpeg.drawbox(a.frame, x => 10, y => 10, w => 40, h => 40, enable => 'gt(t,1)')`) and as a tier-1 named extra, where it reaches through to the filter the stdlib function expands to.

Because `enable` is framework-level it appears in no filter's `-help` output, so the option validator special-cases the name rather than looking it up. What decides whether a given filter accepts it is the `T` flag in the first column of `ffmpeg -filters` — the same introspection everything else in this document is built on:

```
 TSC gblur             V->V       Apply Gaussian Blur filter.
 ..C scale             V->V       Scale the input video size and/or convert the image format.
```

So `blur(a.frame, 5, enable => '...')` compiles and `scale(a.frame, 640, 360, enable => '...')` is `UNKNOWN_FILTER_OPTION`, saying that *this* ffmpeg does not flag `scale` as timeline-capable. A generated source never takes it either: `ffmpeg.testsrc` makes frames rather than transforming them, so there is nothing to switch off.

The expression's vocabulary is `t` (timestamp in seconds), `n` (frame number) and `pos`, wrapped in ffmpeg's expression functions (`between(t,a,b)`, `gt(t,x)`, `lt`, `if`, ...). Its *content* is not validated at compile time and deliberately so: the variables available differ per filter and ffmpeg exposes no way to enumerate them, so a typo inside the expression surfaces when the command runs, not when it compiles. That is the same line RFC-005 draws for expression arguments generally (see `expr` parameters in [docs/stdlib.md](stdlib.md)).

## `--portable`

`compile`, `explain`, and `validate` all take `--portable`: stdlib only, no registry constructed, nothing introspected, nothing shells out. The compile sees exactly what a machine with no ffmpeg would see. A tier-2 filter name becomes `UNKNOWN_FUNCTION`; any named argument becomes `UNSUPPORTED_SQL`. Run it before handing a query to someone whose machine you don't control.

`run` doesn't take the flag: it executes ffmpeg, so there is nothing for an offline mode to do.

## The v1 scope fence

Not everything ffmpeg reports is callable:

- Only filters with a static pad signature and exactly one output make the cut: `V->V`, `A->A`, `AA->A`, `VV->V`, and friends. Variable pad counts (`N`: `split`, `concat`), multiple outputs (`scale2ref`, `feedback`), and sources/sinks (`|`: `testsrc`, `anullsink`) are excluded, and calling one gets an `UNSUPPORTED_SQL` naming the limitation rather than a partial guess.
- **Three fenced filters are callable after all**, because their output count is not really variable: it is fixed, statically, by one of their options. `ffmpeg.channelsplit(...)` (one stream per channel of `channel_layout`, or of the narrower `channels` subset), `ffmpeg.acrossover(...)` (one band per `split` frequency, plus one) and `ffmpeg.extractplanes(...)` (one per requested plane) each return an **array** of streams — the first calls that do — so the result splats into a SELECT list, subscripts out of a CTE column (`s.ch[2]`), and broadcasts a per-element call over every element. They are reachable through the `ffmpeg.` namespace only; the bare names stay unknown. A layout or split list that is well-typed but not a count ffmpeg could produce is `FILTER_OPTION_TYPE`, naming the option that decides the count. Every other `->N` filter (`amerge`, `join`, `concat`, `split`) is fenced exactly as before. None of the three is `T`-flagged for timeline support in `ffmpeg -filters` either, so `enable` on any of them is rejected the ordinary way (`UNKNOWN_FILTER_OPTION`), not silently accepted.
- Options typed `binary` or `dictionary` have no representation in the dialect; setting one is `FILTER_OPTION_TYPE`. The filter's other options still work.
- ffmpeg's runtime filter commands (`sendcmd`, `zmq`) are out of scope entirely.

### Array-returning filters, worked

Split a stereo track into its two channels, gain each one separately, and mix them back into one — the shape all three array-returning filters exist for:

```sql
WITH ch AS (
  SELECT ffmpeg.channelsplit(a.audio[1]) AS lr FROM input('stereo.mp4') a
)
SELECT amix(volume(ch.lr[1], 0.5), volume(ch.lr[2], 2.0))
FROM ch
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i stereo.mp4 -filter_complex '[0:a:0]channelsplit[n10][n11];[n10]volume=volume=0.5[n2];[n11]volume=volume=2.0[n3];[n2][n3]amix=inputs=2[out0]' -map '[out0]' out.mp4
```

One `channelsplit` node makes both pads at once — `channel_layout` defaults to `'stereo'`, so two, without probing the file at all — and `ch.lr[1]`/`ch.lr[2]` subscript them out of the CTE column exactly like any other array column. `acrossover` and `extractplanes` work the same way, sized off `split` and `planes` respectively instead of `channel_layout`.

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

## Generated sources in `FROM`

A handful of ffmpeg's filters take no input at all — they *make* a stream. `testsrc` draws a test pattern, `color` fills a rectangle, `anullsrc` produces silence, `sine` a tone. They are not callable as functions (a function needs something to be called *on*), so they live in `FROM`, under the same namespace and with the same mandatory alias `input()` has:

```sql
SELECT f.video[1], f.audio[1] FROM input('clip.mp4') f
UNION ALL
SELECT t.video[1], s.audio[1]
FROM ffmpeg.testsrc2(duration => 1, size => '320x240', rate => 15) t,
     ffmpeg.anullsrc(duration => 1) s
```

That is the motivating case: ffmpeg's `concat` needs every segment to have the same pad shape, so a generated video segment cannot join a clip that has audio unless something supplies the missing track. `ffmpeg.anullsrc` is that something, and it costs one filter node rather than a silent `.wav` on disk.

The namespace is **mandatory** here — `FROM testsrc(...) t` is not a source, it is an unknown table function. One rule, and it earns its keep twice: bare names like `random` and `color` are things Postgres or a future dialect could reasonably claim, and the `ffmpeg.` prefix is the same marker of machine-dependence it is in call position.

Sources take **no positional arguments at all** (they have no input pads — that is what makes them sources) and every option by name, validated exactly like a tier-2 call's, with the same `UNKNOWN_FILTER_OPTION` / `FILTER_OPTION_TYPE` codes.

### What a source alias exposes

Exactly one stream, of one type, known before anything is read:

| written | video source (`testsrc`, `color`, ...) | audio source (`anullsrc`, `sine`, ...) |
| --- | --- | --- |
| `t.frame` | the stream | `STREAM_NOT_FOUND` |
| `t.video[1]` / `t.audio[1]` | the stream / `STREAM_NOT_FOUND` | `STREAM_NOT_FOUND` / the stream |
| `t.video` / `t.audio` (bare) | an array of length 1 | an array of length 1 |
| `t.*` | that one column | that one column |
| `t.video[2]` | `STREAM_NOT_FOUND` | `STREAM_NOT_FOUND` |

No probe is involved in any of it: there is no file. For the same reason a source output carries no `language`/`title` provenance, and `WHERE t.t BETWEEN ...` is rejected — there is no input to seek. A source's length is its own `duration =>` option.

A source is **not an `-i`**. It compiles to a zero-input filter node, so `SELECT s.audio[1] FROM ffmpeg.sine(frequency => 440, duration => 1) s` is a whole ffmpeg command with no input file at all. Referencing one alias twice inserts the usual `split`/`asplit`; the generator itself is built once.

Sources are legal wherever an `input()` alias is: comma-joined with real inputs, inside a CTE body, and in a `UNION ALL` branch.

### The source fence

The same v1 scope fence applies. A source must have exactly one output pad, so `avsynctest` (`|->AV`, two pads) and `movie`/`amovie` (`|->N`, a variable count) are excluded, as are all four sinks (`->|`) — the registry never retains them, so they read as unknown names with a hint that states the fence. A name that *is* a real filter but takes inputs (`FROM ffmpeg.gblur(...)`) gets a rejection saying exactly that. With no ffmpeg — or under `--portable` — the whole `FROM ffmpeg.<source>` surface is gone, the same way the call namespace is.

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

Which shape a collision takes depends on the argument count (`overlay(a, b, 1, 2)` parses as the builtin, `overlay(a)` is a `PARSE_ERROR`), which is exactly why the census tests several arities rather than reasoning about the grammar. Four of the eleven — `overlay`, `pad`, `normalize`, `reverse` — are stdlib function names too. Three of those four are fine: `overlay`, `normalize` and `reverse`'s bare spelling was already the stdlib's, and the collision only ever hid the raw filter. `pad` is not: sqlglot's `PAD` grammar claims the bare name at every arity the stdlib entry uses (3-arg `pad(f, w, h)` is a `PARSE_ERROR`, "Required keyword: 'is_left' missing"; 4-arg `pad(f, w, h, color)` arrives as an argless call and rejects with an empty got-list), so the stdlib `pad` entry currently sits in the function table unreachable by its own bare name. Use `ffmpeg.pad(...)` with named options instead, which loses the centered-by-default ergonomics the stdlib entry was written for — a rename of the stdlib entry is under consideration.

`split` would collide the same way via `SPLIT_PART(...)`, but ffmpeg's `split`/`asplit` are variable-pad (`V->N`) and outside the scope fence, so it is `UNKNOWN_FUNCTION` under either spelling.

Two things the collisions never touched: `overlay`'s stdlib entry works positionally as it always has (`overlay(base, top, x, y)`), and sqlmpeg's own internal `trim`/`atrim` nodes — a CTE's `WHERE` window — are built directly, never through SQL call syntax (an input alias's window is not even a filter; it is an input seek, see [docs/trimming.md](trimming.md)).
