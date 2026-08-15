# sqlmpeg — SQL frontend for FFmpeg filtergraphs

A standalone CLI that compiles SQL into an ffmpeg `-filter_complex` invocation. Write a `SELECT` statement; get a runnable ffmpeg command. FFmpeg is the executor — this tool never touches pixels.

**Status: Work in progress (v0.2.0)**

## Example

A picture-in-picture composite: `commentary.mkv` shrinks into the corner of `film.mkv`, and every language track of both gets mixed under it. The CTE carries a video column AND a whole audio array; `volume` broadcasts over that array, one node per language track; `amix` zips the two arrays elementwise -- English with English, French with French -- so one query composites the video and mixes every language:

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
$ sqlmpeg run query.sql -o pip.mkv
ffmpeg -i commentary.mkv -i film.mkv -filter_complex '[0:v:0]scale=w=iw*0.25:h=-2[n1];[1:v:0][n1]overlay=x=20:y=20[out0];[1:a:0]volume=volume=0.65[n3];[1:a:1]volume=volume=0.65[n4];[0:a:0]volume=volume=0.35[n5];[0:a:1]volume=volume=0.35[n6];[n3][n5]amix=inputs=2[out1];[n4][n6]amix=inputs=2[out2]' -map '[out0]' -map '[out1]' -metadata:s:1 language=eng -map '[out2]' -metadata:s:2 language=fra pip.mkv
```

Both `amix` pairs mix one language with itself across the two sources, so each keeps that language's tag; the composited video carries none -- `overlay` mixes two streams too, and here neither side has a tag to agree on.

A second example -- two dual-language episodes, played back to back. Each branch splats `<alias>.audio` -- the whole audio array, not one track -- so `UNION ALL`'s column matching pairs the streams up for you: video with video, English with English, French with French.

```sql
SELECT a.frame, a.audio FROM input('episode1.mkv') a
UNION ALL
SELECT b.frame, b.audio FROM input('episode2.mkv') b
```

```
$ sqlmpeg run query.sql -o season.mkv
ffmpeg -i episode1.mkv -i episode2.mkv -filter_complex '[0:v:0][0:a:0][0:a:1][1:v:0][1:a:0][1:a:1]concat=n=2:v=1:a=2[out0][out1][out2]' -map '[out0]' -map '[out1]' -metadata:s:1 language=eng -map '[out2]' -metadata:s:2 language=fra season.mkv
```

That is the whole point of the frontend: SQL already demands that `UNION ALL` branches agree on column count, types and order, and that demand IS ffmpeg's concat segment contract -- `concat=n=2:v=1:a=2`, its inputs interleaved segment by segment. Arrays are no exception; two three-track episodes give `a=3` and nobody counts pads by hand. The `language` tags survive the concat because both segments agree on them.

## Streams

Every input exposes two array-typed pseudo-columns, `<alias>.video` and `<alias>.audio` (1-based indexing, Postgres array semantics; `<alias>.frame` is sugar for `<alias>.video[1]`). **The SELECT list is the output stream list** — one expression is one output stream, and column order is `-map` order. There is no implicit audio track: select it explicitly, or it is not in the output.

A bare subscript that no function ever touches is passed straight through as a stream copy (`-c:<n> copy`, nothing re-encoded); a subscript wrapped in a function is filtered and gets an `[out0]`, `[out1]`, ... label.

Remap only — first video stream, second audio stream, both copied:

```sql
SELECT a.video[1], a.audio[2]
FROM input('foo.mp4') a
```

```
$ sqlmpeg compile --no-probe query.sql
ffmpeg -i foo.mp4 -map 0:v:0 -c:0 copy -map 0:a:1 -c:1 copy out.mp4
```

A bare `<alias>.audio` (no subscript) is every audio stream of that input, in file order; handed to a function it broadcasts — one subgraph per stream, and each output keeps its source stream's `language` tag automatically. Adding reverb to every language track in a file:

```sql
SELECT v.video[1], reverb(v.audio, 0.3)
FROM input('film.mkv') v
```

```
$ sqlmpeg compile query.sql
ffmpeg -i film.mkv -filter_complex '[0:a:0]aecho=in_gain=0.8:out_gain=0.9:delays=60:decays=0.3[out1];[0:a:1]aecho=in_gain=0.8:out_gain=0.9:delays=60:decays=0.3[out2]' -map 0:v:0 -c:0 copy -map '[out1]' -metadata:s:1 language=eng -map '[out2]' -metadata:s:2 language=fra out.mp4
```

(That example needs a real, readable file to know how many audio streams to broadcast over — `sqlmpeg compile` opportunistically probes any local file that exists and falls back to a fully symbolic, offline compile otherwise; `--no-probe` forces the offline path even when the file is present, for byte-reproducible output.)

## Use with an AI

sqlmpeg ships the system prompt, not the API key — bring your own model.

```
$ sqlmpeg prompt > system.txt      # the dialect, the stdlib, worked examples
```

Pipe that as the system prompt, ask for the edit in English, and put the reply
through the validator:

```
$ sqlmpeg validate --json query.sql
{"line": 1, "col": 8, "code": "UDF_ARG_TYPE", "message": "...", "hint": "..."}
```

Exit 0 and silence means it compiles. On exit 1, hand the JSON object back to
the model and ask for a repair; the prompt carries per-code repair guidance, so
the loop converges in a round or two. Then `sqlmpeg run query.sql -o out.mp4`.

The prompt is generated from the function table, so it never drifts from the
compiler. A rendered copy lives in [docs/system-prompt.md](docs/system-prompt.md).

---

For full details, see the [project spec](sqlmpeg-project.md).
