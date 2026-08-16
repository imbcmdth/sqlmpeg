# Trimming (RFC-004: input-level seeking)

`WHERE <alias>.t BETWEEN <start> AND <end>` on an `input()` alias lowers to
an INPUT seek — `-ss <start> -to <end>` immediately before that alias's own
`-i` — not a filtergraph node:

```sql
SELECT a.video[1]
FROM input('clip.mp4') a
WHERE a.t BETWEEN 5 AND 60
```

```
$ sqlmpeg compile --no-probe "SELECT a.video[1] FROM input('clip.mp4') a WHERE a.t BETWEEN 5 AND 60"
ffmpeg -ss 5 -to 60 -i clip.mp4 -map 0:v:0 -c:0 copy out.mp4
```

This is a change from earlier versions, which spliced `trim`/`atrim` filter
nodes into the graph for every `WHERE`. Aliases are globally unique and each
owns exactly one `-i`, so an alias can carry at most one time window in the
whole query — that per-alias window maps 1:1 onto ffmpeg's per-input `-ss`/
`-to`. The payoff: the seek trims and rebases EVERY stream of that input —
video, audio, subtitle, data, selected or not — so a column nothing else
filters can stay a plain stream copy instead of forcing a re-encode
(`-c:0 copy`, not a `trim`+`setpts` pair). It is also faster (a demuxer seek,
no decode-and-discard) and produces a smaller graph.

A `WHERE` window on a CTE name is unaffected by any of this: a CTE's output
is a filtergraph pad, not an `-i`, so its window still lowers to `trim`+
`setpts` / `atrim`+`asetpts`, spliced lazily and shared across every consumer
of that stream — exactly the pre-RFC-004 behavior, and video/audio only.

## Accuracy: decoded vs. stream-copied

The trim point ffmpeg actually lands on depends on whether that stream ends
up decoded or copied:

- **Decoded** (anything filtered or re-encoded): frame-accurate. Modern
  ffmpeg decodes from the previous keyframe and discards frames up to the
  requested point before anything downstream ever sees them.
- **Stream-copied**: the cut snaps to the previous keyframe — it CANNOT be
  frame-accurate, because copying means no decode happens at all, and a
  keyframe is the only point a copied stream can validly start from. The
  output may start up to one whole GOP early.

Measured against `tests/fixtures/testsrc.mp4` (30 frames, 15 fps, 2.000s,
exactly ONE keyframe at t=0 — i.e. the GOP is the whole file, the worst case
for this effect):

`WHERE a.t BETWEEN 0.5 AND 1.5` (a 1.000s window):

| path | `ffmpeg` | measured output duration |
|---|---|---|
| stream-copied (`SELECT a.frame`, nothing filters it) | `-ss 0.5 -to 1.5 -i ... -c copy` | **1.367s** (not 1.000s — snapped back to the only keyframe, at t=0) |
| decoded (wrapped in any filter, e.g. `scale(a.frame, 1)`) | `-ss 0.5 -to 1.5 -i ... -c:v libx264 ...` | **1.000s** exactly |

A file with keyframes every few seconds (a normal encode, not this
single-GOP fixture) snaps by at most one GOP, not the whole clip — but the
mechanism is the same, and worth knowing before reaching for `--no-probe`/
stream-copy on a query where the exact cut point matters. There is no
`strict`-style knob to force re-encoding for exactness; wrap the column in a
filter (even a no-op-ish one) if frame accuracy matters more than a fast
remux.

### mkv: `format=duration` can lie about the trimmed length

For a Matroska (`.mkv`) output, do not trust `ffprobe -show_entries
format=duration` to confirm a stream-copied trim actually worked — the
container-level duration and each track's own `DURATION` tag are written
from the muxer's own bookkeeping, not always recomputed to match what
actually got copied, and different tracks can disagree with each other.
Measured trimming `tests/fixtures/avs.mkv` (video+audio+subtitle, again a
single-keyframe fixture) with `WHERE a.t BETWEEN 0.3 AND 0.9` (a 0.6s
window), stream-copied:

```
$ ffprobe -show_entries format=duration output.mkv
duration=1.323000
$ ffprobe -show_entries stream_tags=DURATION output.mkv
video:    DURATION=00:00:01.156000000
audio:    DURATION=00:00:00.905000000
subtitle: DURATION=00:00:01.323000000
```

Three different numbers, none of them 0.6s, and the container-level
`format=duration` simply took the largest one (the subtitle track's, which
per the section below is barely trimmed at all). Check the actual packet
timestamps (`ffprobe -show_entries packet=pts_time`) if you need to verify a
copy-trim's real extent, not the container's summary duration.

## Captions: why a selected, trimmed subtitle track is rejected

The input seek covers every stream of an alias, including subtitle/data
ones — that is what makes a captioned file trimmable at all. But ffmpeg does
**not** retime subtitle/data packets under an input `-ss`, on either the
copy or the transcode path: cue timestamps stay close to their ORIGINAL,
un-seeked values while the video (and audio) rebase to the new window.
Measured on `tests/fixtures/avs.mkv`, whose original subtitle cues sit at
0.023s / 0.723s / 1.423s, trimmed with `WHERE a.t BETWEEN 0.5 AND 1.5`:

| path | subtitle packet times in the output |
|---|---|
| stream-copied (video, audio, subtitle all `-c copy`) | 0.023 / 0.723 / 1.423 — unchanged from the source |
| transcoded (video/audio re-encoded, subtitle `-c copy`) | 0.000 / 0.700 / 1.400 — still essentially the ORIGINAL spacing, not shifted to the seek |

Either way, the cues never move to line up with the rebased video. A track
seeked-and-selected in the same query would therefore play roughly `start`
seconds ahead of where it belongs — for a 0.5s seek, a caption meant for
0.723s plays back near 0.723s again, but the video it was timed against now
begins at (video) t=0 instead of (source) t=0.5, so the caption is
effectively `start` seconds too early throughout the clip.

sqlmpeg does not ship a broken result: `WHERE <alias>.t BETWEEN ...` on an
alias whose subtitle/data column is ALSO selected in that same query is a
typed rejection, not a silent desync:

```sql
SELECT a.subtitle[1]
FROM input('tests/fixtures/avs.mkv') a
WHERE a.t BETWEEN 1 AND 2
```

```
$ sqlmpeg validate --json "SELECT a.subtitle[1] FROM input('tests/fixtures/avs.mkv') a WHERE a.t BETWEEN 1 AND 2"
{"line": 1, "col": 8, "code": "UNSUPPORTED_SQL", "message": "'WHERE a.t' cannot trim a selected subtitle stream: ffmpeg does not retime caption packets under an input seek, so they would play out of sync with the trimmed video", "hint": "trim the video/audio without selecting the subtitle/data columns, or select them in a query without a WHERE time range; to caption a trimmed clip, join an external subtitle file whose cues are timed for the cut"}
```

Two things stay legal:

- **Trimming an input whose captions are NOT selected.** An unmapped stream
  is seeked harmlessly — nothing reads its (wrong) timestamps, because it
  never reaches the output:

  ```sql
  SELECT a.video[1], a.audio[1]
  FROM input('clip.mkv') a
  WHERE a.t BETWEEN 5 AND 60
  ```

  This trims and rebases the video/audio normally; `clip.mkv`'s captions,
  simply not selected, are dropped along with everything else the query
  didn't ask for.
- **Selecting captions from an alias that carries no `WHERE` window at
  all.** The captions come through untouched, exactly like today.

There is no way to select a trimmed, in-sync caption track from the SAME
seeked input — join an external subtitle file whose cues are already timed
for the cut instead, as a second `input()` alias (see the README's
"Streams" section and `docs/system-prompt.md`'s Columns section for the
join syntax; it needs no special support, it falls out of streams-as-columns
plus a normal cross join).

A `WHERE` window on a **CTE** name that carries a subtitle/data column is
rejected unconditionally, whether or not that column is selected — a CTE
trim is a filtergraph trim (`trim`/`atrim`), and a filtergraph cannot carry
captions at all, seeked or not (RFC-004's passthrough-only constraint). A
subtitle/data column inside a `UNION ALL` branch is rejected too, for the
same underlying reason: `concat` has video/audio pads only.
