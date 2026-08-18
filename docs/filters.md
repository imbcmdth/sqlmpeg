# Filters

The ffmpeg on your machine ships several hundred filters, everything from workhorse scalers to a generator whose whole job is rendering a vintage TV test pattern. sqlmpeg exposes all of them, with real type-checking, by asking the installed binary what it has (`ffmpeg -filters`, then `ffmpeg -help filter=<name>`) rather than maintaining a hand-written table that would always be behind.

There is exactly one calling convention, and it is ffmpeg's own:

```
<name>(<stream inputs...>, <positional options...>, <named options...>)
```

- **Stream inputs come first.** Their count and types come from the filter's pad signature: `unsharp` is `V->V`, so exactly one video in; `xfade` is `VV->V`, so two. Getting this wrong is a `UDF_ARG_TYPE` naming the signature.
- **Positional options bind in declared order** - the exact order `ffmpeg -help filter=<name>` prints them, which is also exactly how `gblur=5:1` binds inside a hand-written filtergraph. This is ffmpeg's contract, not ours: ffmpeg cannot reorder a filter's options without breaking its own positional syntax, so a query that binds positionally is precisely as stable as the filtergraph it compiles to. `gblur(a.frame, 5, 1)` is `sigma=5:steps=1`; `crop(a.frame, 640, 360, 0, 0)` is `out_w=640:out_h=360:x=0:y=0`, width and height before position, because that is crop's declared order.
- **Named options may follow** with Postgres's own `name => value` notation: `unsharp(a.frame, luma_amount => 1.5)`, or mixed, `unsharp(a.frame, 7, 7, luma_amount => 1.5)`. A positional after a named argument is rejected (standard Postgres rule), and a named argument that collides with an option already bound positionally is a typed error, never a silent override: `gblur(a.frame, 5, sigma => 9)` tells you `sigma` is already set.

A positional argument validates as the option it lands on, so the error surface is uniform whichever spelling you use: `UNKNOWN_FILTER_OPTION` (with a did-you-mean against that filter's actual options) and `FILTER_OPTION_TYPE` (with the expected type plus the range or constant list), both documented with captured examples in [docs/errors.md](errors.md).

## Values

The dialect's usual literal rules: a bare number, `true`/`false`, or a single-quoted string. Enum options take the quoted constant name (`transition => 'wipeleft'`), never the underlying integer, because `'wipeleft'` will still mean something to you in six months. String-typed options accept ffmpeg expressions - `scale(a.frame, 'iw/2', -2)` halves the width, `overlay(a.frame, b.frame, '(W-w)/2', '(H-h)/2')` centers the top layer - evaluated per frame by ffmpeg itself. The expression's content is deliberately not validated at compile time: the variables available differ per filter and ffmpeg exposes no way to enumerate them, so a typo inside the quotes surfaces when the command runs.

## `enable`: turning a filter on and off as the stream plays

One named argument is not an option of any filter. `enable` is ffmpeg's *timeline* switch, implemented in the filter framework rather than in the filters themselves, and it takes an expression evaluated per frame: while it is true the filter runs, while it is false the frame passes through untouched.

```sql
SELECT gblur(a.frame, 12, enable => 'between(t,0.5,1.5)')
FROM input('clip.mp4') a
```

That blurs exactly one second in the middle of the clip and leaves the rest sharp, with no trim, no branch, and no concat. Because `enable` is framework-level it appears in no filter's `-help` output, so the option validator special-cases the name. What decides whether a given filter accepts it is the `T` flag in the first column of `ffmpeg -filters`:

```
 TSC gblur             V->V       Apply Gaussian Blur filter.
 ..C scale             V->V       Scale the input video size and/or convert the image format.
```

So `gblur(..., enable => '...')` compiles and `scale(..., enable => '...')` is `UNKNOWN_FILTER_OPTION`, saying that *this* ffmpeg does not flag `scale` as timeline-capable.

## The `ffmpeg.` namespace: names Postgres got to first

Some ffmpeg filter names are also Postgres grammar. `overlay` is `OVERLAY(x PLACING y FROM n FOR m)`, `trim` is `TRIM(BOTH ... FROM ...)`, `format` is the `FORMAT(...)` builtin - sqlglot (which parses every query, per guardrail #2) recognizes the special form before anything of sqlmpeg's runs, and the call arrives mangled or not at all. Every filter is therefore also reachable as **`ffmpeg.<name>(...)`**, which never collides:

```sql
SELECT ffmpeg.trim(a.frame, start => 1, end => 4),
       ffmpeg.overlay(a.frame, b.frame, x => 20, y => 20, eof_action => 'pass')
FROM input('a.mp4') a, input('b.mp4') b
```

The special-form grammars key on a *bare* name, so qualifying the call bypasses all of them at once. The namespace is strictly filters: same convention, same validation, an ordinary node in the IR. Measured against ffmpeg 7.1 and sqlglot 30.17, eleven bare names need it:

| filter | what Postgres does with the bare name |
| --- | --- |
| `copy` | `COPY` statement grammar |
| `corr` | aggregate `corr(x, y)` |
| `format` | `FORMAT(...)` builtin |
| `median` | `MEDIAN(...)` builtin |
| `normalize` | `NORMALIZE(...)` builtin |
| `null` | the `NULL` keyword |
| `overlay` | `OVERLAY(x PLACING y FROM n FOR m)` |
| `pad` | `PAD` grammar |
| `random` | `RANDOM()` builtin |
| `reverse` | `REVERSE(...)` builtin |
| `trim` | `TRIM(...)` builtin |

Which shape a collision takes depends on the argument count (`overlay(a, b, 1, 2)` happens to survive its builtin grammar and lowering un-parks the arguments; `overlay(a, b)` does not), which is why the census is a test that parses several arities per filter rather than an argument about grammar. When in doubt, or in generated code, use the namespace; it costs seven characters and removes the whole category.

`ffmpeg` is a reserved name: an alias, CTE, or view called `ffmpeg` is `UNSUPPORTED_SQL`.

## The `sqlmpeg.` namespace: three macros

`sqlmpeg.<name>(...)` holds ergonomic macros: things that expand to a small subgraph no single filter provides. The namespace is small by design, three entries, and the admission bar for a fourth is "cannot be expressed as one filter call, and people keep asking." (`sqlmpeg` is reserved the same way `ffmpeg` is.)

- **`sqlmpeg.delay(f, seconds)`** - delay a *video* stream on a transparent canvas: `format=pix_fmts=yuva420p` then `tpad=start_duration=<s>:stop=1:color=black@0`. This is the composition primitive for timed overlays - the delayed stream stays invisible until its start time, so overlaying it inserts it mid-timeline:

  ```sql
  SELECT overlay(f.frame, sqlmpeg.delay(p.frame, 120), 20, 20)
  FROM input('film.mkv') f, input('ad.mp4') p
  ```

  ```
  $ sqlmpeg compile -f query.sql
  ffmpeg -i film.mkv -i ad.mp4 -filter_complex '[1:v:0]format=pix_fmts=yuva420p,tpad=start_duration=120:stop=1:color=black@0[n2];[0:v:0][n2]overlay=x=20:y=20[out0]' -map '[out0]' out.mp4
  ```

  Audio has no canvas to be transparent on; delay an audio stream with the bare filter directly, in milliseconds: `adelay(a.audio[1], delays => '120000')`.

- **`sqlmpeg.speed(f, factor)`** - `setpts=PTS/<factor>`: 2 is double speed, 0.5 is half.

- **`sqlmpeg.blur_regions(f, x, y, w, h, sigma)`** - blur one rectangle and leave the rest alone: crop the region, `gblur` it, overlay it back at the same spot. The region is named once instead of three times.

Macros take positional arguments only, in the order documented here; there is no underlying option table for a named argument to reach, so `seconds => 120` is rejected with the macro's signature.

## Generated sources in `FROM`

A handful of filters take no input at all - they *make* a stream. `testsrc` draws a test pattern, `color` fills a rectangle, `anullsrc` produces silence, `sine` a tone. They are not callable as functions (a function needs something to be called *on*), so they live in `FROM`, under the `ffmpeg.` namespace and with the same mandatory alias `input()` has:

```sql
SELECT f.video[1], f.audio[1] FROM input('clip.mp4') f
UNION ALL
SELECT t.video[1], s.audio[1]
FROM ffmpeg.testsrc2(duration => 1, size => '320x240', rate => 15) t,
     ffmpeg.anullsrc(duration => 1) s
```

That is the motivating case: `concat` needs every segment to have the same pad shape, so a generated video segment cannot join a clip that has audio unless something supplies the missing track. `ffmpeg.anullsrc` is that something, and it costs one filter node rather than a silent `.wav` on disk.

The namespace is **mandatory** here (`FROM testsrc(...) t` is an unknown table function), and sources take options by name only - they have no input pads, which is what makes them sources, so a bare positional value has no stream slot to land in. A source alias exposes exactly one stream of one known type (`t.frame` / `t.video[1]` for a video source, `s.audio[1]` for an audio one), carries no file provenance, cannot be `WHERE`-trimmed (there is no input to seek; a source's length is its own `duration =>` option), and is **not an `-i`**: `SELECT s.audio[1] FROM ffmpeg.sine(frequency => 440, duration => 1) s` compiles to a whole ffmpeg command with no input file at all.

## Filters that return arrays, and filters that eat them

Three filters have an output count that is fixed statically by one of their options, so their calls return an **array** of streams: `ffmpeg.channelsplit(...)` (one stream per channel of `channel_layout`), `ffmpeg.acrossover(...)` (one band per `split` frequency, plus one), and `ffmpeg.extractplanes(...)` (one per requested plane). The array splats into a SELECT list, subscripts out of a CTE column, and broadcasts like any other:

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

In the other direction, three N-input filters take however many streams you hand them: `amix`, `hstack`, and `vstack`. Their `inputs` option is set from the stream count automatically (that `amix=inputs=2` above), and passing `inputs` a number that disagrees with the streams you actually passed is a typed error naming both.

When the QUESTION is which tracks to hand a filter in the first place - select by language, align two files' track sets, fill the gaps with silence - that is the track-row surface: `unnest`, metadata columns, and compile-time joins, in [docs/tracks.md](tracks.md).

## The scope fence

Not everything ffmpeg reports is callable:

- Filters need a static pad signature. Variable pad counts (`split`, `concat`) and multi-output filters (`scale2ref`, `feedback`) are excluded, with the carve-outs above: the array-returning trio and the N-input trio, whose counts are static once the query is in hand. Calling a fenced filter is an `UNSUPPORTED_SQL` naming the limitation.
- Sources must have exactly one output pad (`avsynctest` and `movie` are out), and sinks are never retained.
- Options typed `binary` or `dictionary` have no representation in the dialect; setting one is `FILTER_OPTION_TYPE`. The filter's other options still work.
- Runtime filter commands (`sendcmd`, `zmq`) are out of scope entirely.

## The introspection cache

`ffmpeg -filters` is parsed at most once per process, on the first filter lookup; each filter's `-help filter=<name>` lazily, at most once, when that filter is first referenced. Nobody shells out 460 times up front. Parsed results are cached on disk under `~/.cache/sqlmpeg/`, one file per ffmpeg build, keyed by a hash of `ffmpeg -version`'s first line, so upgrading ffmpeg or repointing `PATH` invalidates the cache by itself. `sqlmpeg.registry.clear_cache()` wipes both the in-process memo and the disk cache; it exists for tests, and for the day you rebuild ffmpeg under an unchanged version string and spend twenty minutes doubting your sanity.

ffmpeg and ffprobe are required, and sqlmpeg arranges that itself: a system install on `PATH` always wins, and a bare machine gets both binaries from the bundled `static-ffmpeg` provisioner on first use. A query compiles against the ffmpeg that will run it - that is the honest contract, and `sqlmpeg explain` will show you exactly what the registry resolved. If the provisioner ever fails, the errors say so in as many words rather than guessing about what your machine might have.
