# sqlmpeg

SQL in, ffmpeg command out. You write a `SELECT` statement, sqlmpeg compiles it into a `-filter_complex` invocation, and ffmpeg does the actual pixel-pushing. This tool never decodes a single frame: it's a compiler, and ffmpeg is the executor.

Why does this exist? The ffmpeg engine is a marvel. The filtergraph syntax is the hard part: hand-labeled pads that must each be consumed exactly once, `split` nodes you have to count yourself, and quoting rules deep enough that the official docs include a worked escaping example. SQL, meanwhile, has been describing dataflow DAGs for fifty years, and it's the language every developer (and every LLM) already speaks. This project connects the two.

## Install

```bash
pip install sqlmpeg
```

Or run it without installing anything: `uvx sqlmpeg` / `pipx run sqlmpeg`. Python 3.10+. ffmpeg and ffprobe are required and handled for you: a system install on `PATH` always wins, and on a machine without one, the bundled provisioner (`static-ffmpeg`) fetches both binaries on first use. Every filter call resolves against what your actual ffmpeg ships, and probing your files is what powers `SELECT *`, bare-array broadcasting, and the track-row columns.

## Ask before you act

`run` is the default subcommand, so a query is the whole invocation - and a query with no `COPY ... TO` is a **metadata query**: the answer is probed metadata, fully known the moment compilation ends, so sqlmpeg prints it as a table and never runs ffmpeg at all. This works on anything ffprobe can read, remote manifests included:

```
$ sqlmpeg "SELECT t.index, t.language, t.codec FROM input(:'src') f, unnest(f.audio) t WHERE t.codec = 'aac'" -v src=https://storage.googleapis.com/shaka-demo-assets/angel-one/dash.mpd
 index | language | codec
-------+----------+-------
 1     | es       | aac
 2     | de       | aac
 3     | en       | aac
 7     | fr       | aac
 10    | it       | aac
(5 rows)
```

`COPY ... TO STDOUT WITH (format 'csv')` is the scriptable spelling of the same thing - stock Postgres COPY, `header true` optional, or `TO 'tracks.csv'` to write a file:

```
$ sqlmpeg "COPY (SELECT t.language, t.codec FROM input(:'src') f, unnest(f.audio) t WHERE t.codec = 'aac') TO STDOUT WITH (format 'csv', header true)" -v src=https://storage.googleapis.com/shaka-demo-assets/angel-one/dash.mpd
language,codec
es,aac
de,aac
en,aac
fr,aac
it,aac
```

Media only moves when you ask for a file: a `COPY ... TO 'out.mkv'` inside the query, or `-o out.mkv` on the CLI (which is the same thing, spelled as a flag). Everything below is that second kind of query.

## PiP demo

Imagine you wanted to shrink `commentary.mkv` into the corner of `film.mkv`, and duck the commentary under the main mix. And both files carry two audio tracks - an English and a French language.

```sql
WITH pip AS (
  SELECT scale(c.frame, 'iw/4', -2) AS frame, c.audio AS sound
  FROM input('commentary.mkv') c
)
SELECT overlay(f.frame, pip.frame, 20, 20),
       amix(volume(f.audio, 0.65), volume(pip.sound, 0.35))
FROM input('film.mkv') f, pip
```

```
$ sqlmpeg compile -f query.sql -o pip.mkv
ffmpeg -i commentary.mkv -i film.mkv -filter_complex '[0:v:0]scale=width=iw/4:height=-2[n1];[1:v:0][n1]overlay=x=20:y=20[out0];[1:a:0]volume=volume=0.65[n3];[1:a:1]volume=volume=0.65[n4];[0:a:0]volume=volume=0.35[n5];[0:a:1]volume=volume=0.35[n6];[n3][n5]amix=inputs=2[out1];[n4][n6]amix=inputs=2[out2]' -map '[out0]' -map '[out1]' -metadata:s:1 language=eng -map '[out2]' -metadata:s:2 language=fra pip.mkv
```

Check out all the work you didn't need to do! No pad labels. No split bookkeeping. You never even said how many audio tracks there were: `c.audio` is the whole array, `volume` broadcasts over it (one node per language), and `amix` zips the two arrays elementwise, English with English, French with French. Each mixed track keeps its language tag, because both parents agreed on it. (`compile` shows the command; drop it - `sqlmpeg -f query.sql -o pip.mkv` - and the default `run` executes it instead.)

## Encoding

The query above describes the edit and says nothing about codecs, so ffmpeg picks its defaults. When you care about the encode, wrap the query in `COPY ... TO ... WITH (...)` - stock Postgres syntax - and the destination and codec settings ride along inside the query:

```sql
COPY (
  WITH pip AS (
    SELECT scale(c.frame, 'iw/4', -2) AS frame, c.audio AS sound
    FROM input('commentary.mkv') c
  )
  SELECT overlay(f.frame, pip.frame, 20, 20),
         amix(volume(f.audio, 0.65), volume(pip.sound, 0.35))
  FROM input('film.mkv') f, pip
) TO 'pip.mkv' WITH (
  video_codec 'libx264', crf 20, audio_codec 'aac', audio_bitrate '192k'
)
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i commentary.mkv -i film.mkv -filter_complex '[0:v:0]scale=width=iw/4:height=-2[n1];[1:v:0][n1]overlay=x=20:y=20[out0];[1:a:0]volume=volume=0.65[n3];[1:a:1]volume=volume=0.65[n4];[0:a:0]volume=volume=0.35[n5];[0:a:1]volume=volume=0.35[n6];[n3][n5]amix=inputs=2[out1];[n4][n6]amix=inputs=2[out2]' -map '[out0]' -map '[out1]' -metadata:s:1 language=eng -map '[out2]' -metadata:s:2 language=fra -c:0 libx264 -crf:0 20 -c:1 aac -c:2 aac -b:1 192k -b:2 192k pip.mkv
```

An explicit `video_codec` or `audio_codec` re-encodes every output of that type, including one that would otherwise stream-copy. That's deliberate: if you asked for a codec, you get that codec, every time. `-o` on the CLI still works and overrides only the path - same encode, different destination.

## Views and multiple outputs

A `CREATE VIEW name AS <query>;` followed by one or more `COPY (...) TO '<path>' WITH (...);` is a script - the ABR-ladder shape, one decode feeding several encodes. It still compiles to ONE ffmpeg invocation, one output group per COPY:

```sql
CREATE VIEW master AS
  SELECT scale(f.video[1], 1920, -2) AS v, volume(f.audio[1], 0.9) AS a
  FROM input('film.mkv') f;

COPY (SELECT scale(m.v, 1280, -2) AS v, m.a FROM master m)
TO '720.mp4' WITH (video_codec 'libx264', crf 21, audio_codec 'aac');

COPY (SELECT scale(m.v, 640, -2) AS v, m.a FROM master m)
TO '360.mp4' WITH (video_codec 'libx264', crf 26, audio_codec 'aac');

COPY (SELECT m.a FROM master m)
TO 'audio.m4a' WITH (audio_codec 'aac', audio_bitrate '128k')
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i film.mkv -filter_complex '[0:v:0]scale=width=1920:height=-2[n1];[0:a:0]volume=volume=0.9[n2];[n1]split=2[n1_split0][n1_split1];[n1_split0]scale=width=1280:height=-2[out0];[n1_split1]scale=width=640:height=-2[out2];[n2]asplit=3[out1][out3][out4]' -map '[out0]' -map '[out1]' -c:0 libx264 -crf:0 21 -c:1 aac 720.mp4 -map '[out2]' -map '[out3]' -c:0 libx264 -crf:0 26 -c:1 aac 360.mp4 -map '[out4]' -c:0 aac -b:0 128k audio.m4a
```

A view is to statements what a CTE is to branches: `master` decodes and filters `film.mkv` exactly once - `scale` and `volume` each appear a single time in the graph above - and the split pass hands out however many pads its readers need (`split=2` for the two video consumers, `asplit=3` for the three audio ones). Alias it in `FROM` (`FROM master m`) exactly like a CTE; view, CTE and alias names share one flat, script-wide namespace, and a view that nothing ever reads is a typo, rejected outright. `-o` on the CLI only makes sense with one destination - against a script with more than one COPY it's a usage error naming the sinks it found, so give each COPY its own path instead.

There's much more - watermarks, GIFs, subtitle muxing, multiband compression, generated test media - and it all lives in the **[cookbook](docs/examples.md)**: forty real tasks, simple to complex, every shown output rerun and byte-checked by the test suite - and most of them parameterized with `-v` variables, so they run against your files as-is.

## CLI reference

`run` is the default subcommand: any invocation that doesn't start with a subcommand name is `run`'s, so `sqlmpeg "SELECT ..."` and `sqlmpeg -f query.sql` just work. All four query commands take the SQL as text right on the command line, or from a file with `-f query.sql` (`-f -` reads stdin). Exactly one of the two. All four also take `-v name=value` (repeatable - psql's flag, psql's syntax): `:'name'` in the query becomes the value as an escaped string literal, bare `:name` becomes it raw, and an undefined variable is a compile-time error. A query file plus `-v` is a reusable program; [queries/](queries/) collects ready-made ones.

| command | what it does | flags |
|---|---|---|
| `run` | **the default** (`sqlmpeg "SELECT ..."` is enough): a query with a media destination compiles and executes ffmpeg; one without prints its result set as a table, psql-style, executing nothing | `-o PATH` (media output; makes any query a media query) · `--timeout SECS` (default 600) · `-y` (overwrite) |
| `compile` | print the full ffmpeg command | `--graph-only` (just the filtergraph string) · `-o PATH` (output path; default is the query's `COPY` sink path, else `out.mp4`) |
| `explain` | dump the compiled IR graph as JSON | |
| `validate` | exit 0 if the query compiles, else a line-anchored error | `--json` (machine-readable error object on stdout) |
| `prompt` | print the LLM system prompt | |

## The ideas, briefly

- **Streams are columns.** Every input exposes `<alias>.video`, `<alias>.audio`, `<alias>.subtitle`, `<alias>.data` (1-based subscripts; `<alias>.frame` is sugar for `video[1]`), and **the SELECT list is the result set** - in a media query (one with a `COPY` destination or `-o`), one column is one `-map`, in order, nothing implicit. A bare subscript no function touches stays a stream copy. `input()` takes per-input options (`loop => true` keeps a still image alive). `SELECT *` keeps everything.
- **Bare arrays broadcast.** `atempo(v.audio, 1.25)` fans out one node per track, each output keeping its language tag. Two arrays in one call zip elementwise.
- **Tracks are rows when you need them.** `unnest(f.audio)` turns a track array into a compile-time table whose columns are the probed metadata, so picking a track is `WHERE t.language = 'eng'` and aligning two files' tracks is a real SQL `JOIN` - inner, left, or full outer, with generated silence (or an empty caption track) standing in for what a file lacks. Selecting a computed column next to a track *edits its tags* (`CASE ... END AS language` retags a whole library in one expression), and `chapters(f)` is a table too - readable, and writable from a `VALUES` list. Every join is decided at compile time; ffmpeg only sees the wiring. [docs/tracks.md](docs/tracks.md) has the whole story.
- **A SELECT with no COPY prints a table.** The result set was fully known at compile time, so `sqlmpeg "SELECT * FROM input('film.mkv') f, unnest(f.audio) t"` prints the tracks as rows - ffprobe you can read, joins included - and `COPY (...) TO STDOUT WITH (FORMAT csv)` makes it scriptable. ffmpeg only runs when a `COPY` (or `-o`) names a media destination.
- **Trims are seeks.** `WHERE a.t BETWEEN 5 AND 60` (or either bound alone, open-ended) becomes `-ss`/`-to` on that alias's `-i`: fast, all stream types at once, stream-copy still possible. Decoded streams cut frame-accurate; copied ones snap to a keyframe. The measurements, and the caption caveat, are in [docs/trimming.md](docs/trimming.md).
- **Every filter, one convention.** All ~450 filters in your ffmpeg build are callable: streams first, then options - positionally in the exact order `ffmpeg -help filter=<name>` prints them, by name (`unsharp(a.frame, luma_amount => 1.5)`), or both. Every option is type-checked against what the binary reports. `ffmpeg.<name>(...)` always means the raw filter, including the eleven names Postgres grammar would otherwise eat; `sqlmpeg.<name>(...)` holds exactly four macros for jobs no single filter does (`delay`, `speed`, `blur_regions`, and `loudnorm2`, which measures a stream's loudness and corrects it in a second pass). A few multi-output filters (`channelsplit`, `acrossover`, `extractplanes`) return arrays. [docs/filters.md](docs/filters.md) has the whole story.
- **Generated sources live in FROM.** `ffmpeg.sine(frequency => 440, duration => 1) s` is a table function, not a file - the compiled command has no `-i` at all.
- **`enable` and expressions.** `gblur(a.frame, 12, enable => 'between(t,10,20)')` windows an effect in time; expression strings like `'(W-w)/2'` do per-frame geometry in any string-typed option.
- **Captions ride along, untouched.** Subtitle and data streams select, extract and mux like anything else, but they're passthrough-only - a filtergraph has no subtitle pads.
- **Errors are a feature.** Every rejection is a typed, line-anchored JSON object with a hint, documented with captured examples in [docs/errors.md](docs/errors.md).

## Use with an AI

sqlmpeg ships the system prompt. Bring whatever model you like.

```
$ sqlmpeg prompt > system.txt      # the dialect, the calling convention, your filters
```

Pipe that in as the system prompt, ask for the edit in English, and put the reply through the validator:

```
$ sqlmpeg validate --json -f query.sql
{"line": 1, "col": 8, "code": "UDF_ARG_TYPE", "message": "...", "hint": "..."}
```

Exit 0 with no output means it compiles. On exit 1, hand the JSON back to the model and ask for a repair; the prompt carries per-code repair guidance, so the loop converges in a round or two. Then `sqlmpeg -f query.sql -o out.mp4`.

The prompt's filter reference is rendered from the same registry the compiler resolves against - your installed ffmpeg - so it cannot drift, and the model works with your actual machine rather than a platonic ideal of one. A rendered copy of the base prompt lives in [docs/system-prompt.md](docs/system-prompt.md).

---

Docs: [cookbook](docs/examples.md) · [filters](docs/filters.md) · [track rows](docs/tracks.md) · [trimming](docs/trimming.md) · [error contract](docs/errors.md) · [project spec](sqlmpeg-project.md)
