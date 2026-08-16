# sqlmpeg

SQL in, ffmpeg command out. You write a `SELECT` statement, sqlmpeg compiles it into a `-filter_complex` invocation, and ffmpeg does the actual pixel-pushing. This tool never decodes a single frame: it's a compiler, and ffmpeg is the executor.

**Status: v0.5.0, not yet on PyPI. Works on my machine (and the CI machine).**

Why does this exist? The ffmpeg engine is a marvel. The filtergraph syntax is the hard part: hand-labeled pads that must each be consumed exactly once, positional arguments in surprising orders (`crop` takes `w:h:x:y`, when everyone thinks in `x,y,w,h`), and quoting rules deep enough that the official docs include a worked escaping example. SQL, meanwhile, has been describing dataflow DAGs for fifty years, and it's the language every developer (and every LLM) already speaks. This project connects the two.

## PiP demo

Imagine you wanted to shrink `commentary.mkv` into the corner of `film.mkv`, and duck the commentary under the main mix. And both files carry two audio tracks - an English and a French language.

```sql
WITH pip AS (
  SELECT scale(c.frame, 0.25) AS frame, c.audio AS sound
  FROM input('commentary.mkv') c
)
SELECT overlay(f.frame, pip.frame, 20, 20),
       amix(volume(f.audio, 0.65), volume(pip.sound, 0.35))
FROM input('film.mkv') f, pip
```

```
$ sqlmpeg run -f query.sql -o pip.mkv
ffmpeg -i commentary.mkv -i film.mkv -filter_complex '[0:v:0]scale=w=iw*0.25:h=-2[n1];[1:v:0][n1]overlay=x=20:y=20[out0];[1:a:0]volume=volume=0.65[n3];[1:a:1]volume=volume=0.65[n4];[0:a:0]volume=volume=0.35[n5];[0:a:1]volume=volume=0.35[n6];[n3][n5]amix=inputs=2[out1];[n4][n6]amix=inputs=2[out2]' -map '[out0]' -map '[out1]' -metadata:s:1 language=eng -map '[out2]' -metadata:s:2 language=fra pip.mkv
```

Check out all the work you didn't need to do! No pad labels. No split bookkeeping. You never even said how many audio tracks there were: `c.audio` is the whole array, `volume` broadcasts over it (one node per language), and `amix` zips the two arrays elementwise, English with English, French with French. Each mixed track keeps its language tag, because both parents agreed on it.

## Encoding

The query above describes the edit and says nothing about codecs, so ffmpeg picks its defaults. When you care about the encode, wrap the query in `COPY ... TO ... WITH (...)` - stock Postgres syntax - and the destination and codec settings ride along inside the query:

```sql
COPY (
  WITH pip AS (
    SELECT scale(c.frame, 0.25) AS frame, c.audio AS sound
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
$ sqlmpeg run -f query.sql
ffmpeg -i commentary.mkv -i film.mkv -filter_complex '[0:v:0]scale=w=iw*0.25:h=-2[n1];[1:v:0][n1]overlay=x=20:y=20[out0];[1:a:0]volume=volume=0.65[n3];[1:a:1]volume=volume=0.65[n4];[0:a:0]volume=volume=0.35[n5];[0:a:1]volume=volume=0.35[n6];[n3][n5]amix=inputs=2[out1];[n4][n6]amix=inputs=2[out2]' -map '[out0]' -map '[out1]' -metadata:s:1 language=eng -map '[out2]' -metadata:s:2 language=fra -c:0 libx264 -crf:0 20 -c:1 aac -c:2 aac -b:1 192k -b:2 192k pip.mkv
```

An explicit `video_codec` or `audio_codec` re-encodes every output of that type, including one that would otherwise stream-copy. That's deliberate: if you asked for a codec, you get that codec, every time. `-o` on the CLI still works and overrides only the path - same encode, different destination.

One more: two dual-language episodes, played back to back. Each branch splats `<alias>.audio`, the whole array, and `UNION ALL` pairs the columns up for you.

```sql
SELECT a.frame, a.audio FROM input('episode1.mkv') a
UNION ALL
SELECT b.frame, b.audio FROM input('episode2.mkv') b
```

```
$ sqlmpeg run -f query.sql -o season.mkv
ffmpeg -i episode1.mkv -i episode2.mkv -filter_complex '[0:v:0][0:a:0][0:a:1][1:v:0][1:a:0][1:a:1]concat=n=2:v=1:a=2[out0][out1][out2]' -map '[out0]' -map '[out1]' -metadata:s:1 language=eng -map '[out2]' -metadata:s:2 language=fra season.mkv
```

This example is my favorite, because it shows why SQL is such a natural fit. SQL requires `UNION ALL` branches to agree on column count, type, and order. That happens to be, verbatim, ffmpeg's concat segment contract: n segments, each contributing its videos then its audios, interleaved in exactly the right order - which is genuinely tricky to get right by hand. Two three-track episodes would give you `a=3` without anyone counting pads. And the language tags survive the concat, because every segment agrees on them.

## Any ffmpeg filter

The stdlib is a few dozen functions with hand-picked, portable signatures: degrees instead of radians, seconds instead of milliseconds, arguments in the order a person would guess. But your installed ffmpeg ships somewhere north of 450 filters, and sqlmpeg exposes every one of them. It asks the binary what it has (`ffmpeg -filters`, then `-help filter=<name>` per filter, cached), then type-checks your calls against the answer: stream inputs positional, options by name.

```sql
SELECT unsharp(a.frame, luma_msize_x => 7, luma_amount => 1.5), a.audio[1]
FROM input('clip.mp4') a
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i clip.mp4 -filter_complex '[0:v:0]unsharp=luma_msize_x=7:luma_amount=1.5[out0]' -map '[out0]' -map 0:a:0 -c:1 copy out.mp4
```

This is machine-dependent on purpose: the query compiles where an `unsharp` filter exists and nowhere else, and the option names, types, ranges, and enum constants are all validated against what your binary actually reports. Named options also reach through stdlib calls to the underlying filter's full option set (`blur(a.frame, 5, planes => 1)` sets `gblur`'s `planes`, which no table anywhere lists). When a query needs to travel, `--portable` compiles it against the stdlib alone and tells you exactly what a machine with no ffmpeg would think of it. Details, including the one known wart, live in [docs/dynamic-filters.md](docs/dynamic-filters.md).

## Streams are columns

Every input exposes four array-typed pseudo-columns: `<alias>.video`, `<alias>.audio`, `<alias>.subtitle`, and `<alias>.data`. Subscripts are 1-based, matching Postgres array semantics (`<alias>.frame` is sugar for `<alias>.video[1]`). The rule everything else falls out of: **the SELECT list is the output stream list.** One column, one output stream, in `-map` order. Nothing rides along implicitly. If you didn't select the audio, the output has no audio - and that's a feature, because it means your output's stream layout is exactly the SELECT list you can read at the top of the query.

A bare subscript no function touches passes straight through as a stream copy (`-c:<n> copy`, zero re-encoding, zero generation loss). Wrap it in a function and it goes through the filtergraph instead.

Remap only, first video stream and second audio stream, both copied:

```sql
SELECT a.video[1], a.audio[2]
FROM input('foo.mp4') a
```

```
$ sqlmpeg compile --no-probe "SELECT a.video[1], a.audio[2] FROM input('foo.mp4') a"
ffmpeg -i foo.mp4 -map 0:v:0 -c:0 copy -map 0:a:1 -c:1 copy out.mp4
```

A bare `<alias>.audio` with no subscript is every audio stream of that input, in file order. Hand it to a function and it broadcasts, one subgraph per track, and each output keeps its source's language tag. Reverb on every language at once:

```sql
SELECT v.video[1], reverb(v.audio, 0.3)
FROM input('film.mkv') v
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i film.mkv -filter_complex '[0:a:0]aecho=in_gain=0.8:out_gain=0.9:delays=60:decays=0.3[out1];[0:a:1]aecho=in_gain=0.8:out_gain=0.9:delays=60:decays=0.3[out2]' -map 0:v:0 -c:0 copy -map '[out1]' -metadata:s:1 language=eng -map '[out2]' -metadata:s:2 language=fra out.mp4
```

(That one needs a real, readable file, since the compiler has to know how many tracks to fan out over. sqlmpeg probes any local file that exists and quietly falls back to a fully symbolic, offline compile otherwise. `--no-probe` forces the symbolic path even when the file is present, for byte-reproducible output.)

`SELECT *` means what you hope it means: every stream of every alias, all four types, remuxed untouched.

```sql
SELECT * FROM input('tests/fixtures/avs.mkv') a
```

```
$ sqlmpeg compile "SELECT * FROM input('tests/fixtures/avs.mkv') a"
ffmpeg -i tests/fixtures/avs.mkv -map 0:v:0 -c:0 copy -map 0:a:0 -c:1 copy -map 0:s:0 -c:2 copy -metadata:s:2 language=eng out.mp4
```

## Trims

`WHERE <alias>.t BETWEEN <start> AND <end>` on an input alias becomes an input seek (`-ss <start> -to <end>` in front of that alias's own `-i`), not a filtergraph node. The demuxer skips the front of the file instead of decoding and discarding it, and a trimmed column nothing else touches gets to stay a stream copy:

```sql
SELECT a.video[1], a.audio[1]
FROM input('clip.mp4') a
WHERE a.t BETWEEN 5 AND 60
```

```
$ sqlmpeg compile --no-probe "SELECT a.video[1], a.audio[1] FROM input('clip.mp4') a WHERE a.t BETWEEN 5 AND 60"
ffmpeg -ss 5 -to 60 -i clip.mp4 -map 0:v:0 -c:0 copy -map 0:a:0 -c:1 copy out.mp4
```

Accuracy is the classic trade. A decoded stream trims frame-accurate; a stream-copied one snaps back to the previous keyframe, because a copied stream can only begin at a keyframe. One caveat we measured the hard way: the seek covers every stream of the input, captions included, but ffmpeg does not retime caption packets under an input seek. Rather than hand you subtitles that drift by exactly your seek offset, sqlmpeg rejects a query that trims an alias and also selects its captions. Trim without the captions, or join a subtitle file already timed for the cut (next section). The measured numbers, including an important caveat about mkv's self-reported durations, are in [docs/trimming.md](docs/trimming.md).

## Captions and data streams

Subtitle and data streams select like anything else: bare, subscripted, splatted, through a CTE, or swept up by `SELECT *`. The one rule is that they are passthrough-only. An ffmpeg filtergraph has no subtitle pads at all, so no function accepts one and they have no place in a `UNION ALL`. They get `-map`'d with their tags intact, and that's the entire feature.

Extraction is just a sink with nothing else selected: `COPY (SELECT a.subtitle[1] FROM input('film.mkv') a) TO 'subs.en.srt'`. And muxing an external WebVTT in as a track, one of the most-searched-for ffmpeg tasks there is, needs no special syntax at all: add the subtitle file as a second `input()` alias, select its `.subtitle[1]` next to your video and audio, and set `subtitle_codec` in the `WITH (...)` if the container needs a transcode (`'mov_text'` for mp4, which accepts nothing else).

## Use with an AI

`sqlmpeg` ships with prebaked system prompt. Drop it into whatever model you like.

```
$ sqlmpeg prompt > system.txt      # the dialect, the stdlib, worked examples
```

Pipe that in as the system prompt, ask for the edit in English, and put the reply through the validator:

```
$ sqlmpeg validate --json -f query.sql
{"line": 1, "col": 8, "code": "UDF_ARG_TYPE", "message": "...", "hint": "..."}
```

Exit 0 with no output means it compiles. On exit 1, hand the JSON back to the model and ask for a repair; the prompt carries per-code repair guidance, so the loop converges in a round or two. Then `sqlmpeg run -f query.sql -o out.mp4`.

The prompt is generated from the same function table the compiler uses, so it cannot drift. A rendered copy lives in [docs/system-prompt.md](docs/system-prompt.md), and `sqlmpeg prompt --dynamic` appends every filter your particular ffmpeg reports, so the model works with your actual machine rather than a platonic ideal of one.
