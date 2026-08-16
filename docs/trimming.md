# Trimming

`WHERE <alias>.t BETWEEN <start> AND <end>` on an `input()` alias lowers to an input seek, `-ss <start> -to <end>` placed right before that alias's own `-i`. No filtergraph node involved:

```sql
SELECT a.video[1]
FROM input('clip.mp4') a
WHERE a.t BETWEEN 5 AND 60
```

```
$ sqlmpeg compile --no-probe "SELECT a.video[1] FROM input('clip.mp4') a WHERE a.t BETWEEN 5 AND 60"
ffmpeg -ss 5 -to 60 -i clip.mp4 -map 0:v:0 -c:0 copy out.mp4
```

Earlier versions spliced `trim`/`atrim` filter nodes into the graph for every `WHERE`. The seek is better on every axis we could measure. Every alias owns exactly one `-i` and can carry at most one time window in the whole query, so the per-alias window maps 1:1 onto ffmpeg's per-input `-ss`/`-to`. The seek trims and rebases every stream of that input at once (video, audio, subtitle, data, selected or not), the demuxer skips the front of the file instead of decoding it just to throw it away, the graph shrinks, and a column nothing else filters gets to stay a plain `-c:0 copy` instead of being dragged through an encoder for the crime of having a start time.

A `WHERE` window on a CTE name still works the old way, because it has to: a CTE's output is a filtergraph pad, not an `-i`, so its window lowers to `trim`+`setpts` (or `atrim`+`asetpts`), spliced lazily and shared across every consumer. Video and audio only, down that path.

## Accuracy: decoded vs. stream-copied

Where the cut actually lands depends on whether the stream gets decoded:

- **Decoded** (filtered or re-encoded): frame-accurate. ffmpeg decodes from the previous keyframe and discards frames up to your requested point before anything downstream sees them.
- **Stream-copied**: the cut snaps back to the previous keyframe. This is not ffmpeg being lazy; a copied stream has no decoder in the loop, and a keyframe is the only place a copied stream can validly begin. Your output may start up to one full GOP early. (A GOP, for anyone lucky enough never to have needed the term, is the run of frames between one keyframe and the next. Everything in it depends on the keyframe. You cannot start mid-GOP for the same reason you cannot start reading a sentence at its fourth pronoun.)

Measured against `tests/fixtures/testsrc.mp4`: 30 frames, 15 fps, 2.000s, and exactly one keyframe at t=0, which makes the whole file one GOP. This is the pathological worst case, chosen on purpose.

`WHERE a.t BETWEEN 0.5 AND 1.5`, a 1.000s window:

| path | `ffmpeg` | measured output duration |
|---|---|---|
| stream-copied (`SELECT a.frame`, nothing filters it) | `-ss 0.5 -to 1.5 -i ... -c copy` | **1.367s**. Not 1.000s. Snapped all the way back to the file's only keyframe, at t=0 |
| decoded (wrapped in any filter, e.g. `scale(a.frame, 1)`) | `-ss 0.5 -to 1.5 -i ... -c:v libx264 ...` | **1.000s** exactly |

A normally-encoded file with keyframes every couple of seconds snaps by at most one GOP, not the whole clip, but the mechanism is identical. If the exact cut point matters more than a fast remux, wrap the column in a filter and eat the re-encode; there is no magic third option, and any tool claiming to offer one is quietly re-encoding.

### mkv duration metadata: do not believe it

After a stream-copied trim to Matroska, `ffprobe -show_entries format=duration` is not evidence of anything. The container-level duration and each track's own `DURATION` tag come from the muxer's bookkeeping, are not recomputed to match what actually got copied, and are perfectly willing to disagree with reality and each other simultaneously. Measured on a copy-trim of `tests/fixtures/avs.mkv` (video+audio+subtitle, same single-keyframe fixture) with a nominal 0.6s window (`WHERE a.t BETWEEN 0.3 AND 0.9`):

```
$ ffprobe -show_entries format=duration output.mkv
duration=1.323000
$ ffprobe -show_entries stream_tags=DURATION output.mkv
video:    DURATION=00:00:01.156000000
audio:    DURATION=00:00:00.905000000
subtitle: DURATION=00:00:01.323000000
```

Four numbers in play (0.6s requested, three reported), none of them agreeing, and `format=duration` turns out to just be the largest track tag. This is one measurement away from a wrong test assertion, a wrong monitoring alert, or a wrong invoice. When you need the real extent of a copy-trim, read the packet timestamps (`ffprobe -show_entries packet=pts_time`) and ignore the container's self-reported summary entirely.

## Captions: why trim + selected subtitles is rejected

The seek covers every stream of the alias, subtitle and data streams included, which is what makes a captioned file trimmable at all. And then ffmpeg declines to finish the job: caption packets are not retimed under an input `-ss`, on either the copy or the transcode path. The cues keep roughly their original timestamps while the video and audio rebase to the new zero. Measured on `tests/fixtures/avs.mkv`, whose subtitle cues sit at 0.023s / 0.723s / 1.423s, seeking `WHERE a.t BETWEEN 0.5 AND 1.5`:

| path | subtitle packet times in the output |
|---|---|
| stream-copied (everything `-c copy`) | 0.023 / 0.723 / 1.423, unchanged from the source |
| transcoded (video/audio re-encoded) | 0.000 / 0.700 / 1.400, still the original spacing, not shifted by the seek |

So the video now starts at what used to be 0.5s, and a caption written for the 0.723s moment still fires at ~0.7s of the new timeline, half a second before the moment it captions. Every cue, off by exactly your seek offset, for the whole clip. Also the cue from before the window survives the trim and shows up anyway, like a guest who didn't check which party.

sqlmpeg will not compile that. Trimming an alias and selecting its subtitle/data column in the same query is a typed rejection, not a deliverable with a latent sync bug:

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

- **Trimming an input whose captions are not selected.** An unmapped stream is seeked harmlessly; its wrong timestamps never reach the output because the stream itself never does:

  ```sql
  SELECT a.video[1], a.audio[1]
  FROM input('clip.mkv') a
  WHERE a.t BETWEEN 5 AND 60
  ```

  The video and audio trim and rebase normally. The captions, unselected, are dropped with everything else the query didn't ask for.
- **Selecting captions from an alias with no `WHERE` window.** Untouched passthrough, tags intact, business as usual.

There is no way to get a trimmed, in-sync caption track out of the same seeked input, because there is no ffmpeg incantation under the hood that produces one. The working move is to join an external subtitle file whose cues are already timed for the cut, as a second `input()` alias (see the README's captions section; it needs no special join syntax, it's streams-as-columns plus an ordinary cross join).

A `WHERE` window on a **CTE** carrying a subtitle/data column is rejected unconditionally, selected or not: a CTE trim is a filtergraph trim, and a filtergraph cannot carry captions in the first place. Same reason a subtitle column can't appear in a `UNION ALL` branch: `concat` has video and audio pads, and no amount of asking politely adds a third kind.
