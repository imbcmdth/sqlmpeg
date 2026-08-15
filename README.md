# sqlmpeg — SQL frontend for FFmpeg filtergraphs

A standalone CLI that compiles SQL into an ffmpeg `-filter_complex` invocation. Write a `SELECT` statement; get a runnable ffmpeg command. FFmpeg is the executor — this tool never touches pixels.

**Status: Work in progress (v0.1.0)**

## Example

```sql
WITH pip AS (
  SELECT scale(crop(b.frame, 1200, 50, 600, 200), 0.5) AS frame
  FROM input('game.mp4') b
)
SELECT overlay(a.frame, pip.frame, 20, 20)
FROM input('game.mp4') a, pip
```

```
$ sqlmpeg run query.sql -o out.mp4
ffmpeg -i game.mp4 -i game.mp4 -filter_complex \
  "[1:v]crop=600:200:1200:50,scale=iw*0.5:-2[pip]; \
   [0:v][pip]overlay=20:20[out]" -map "[out]" out.mp4
```

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

## Audio handling

**Audio: v0 copies audio from the first input (`-c:a copy`); SQL is video-only.** This is a deliberate constraint, not a limitation. Full audio filter support requires a matching SQL dialect and stdlib — that is a v1 expansion. For now, the tool handles video transforms and carries audio unchanged.

---

For full details, see the [project spec](sqlmpeg-project.md).
