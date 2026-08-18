# Trimming

`WHERE <alias>.t BETWEEN <start> AND <end>` on an `input()` alias lowers to an input seek, `-ss <start> -to <end>` placed right before that alias's own `-i`. No filtergraph node involved:

```sql
SELECT a.video[1]
FROM input('clip.mp4') a
WHERE a.t BETWEEN 5 AND 60
```

```
$ sqlmpeg compile "SELECT a.video[1] FROM input('clip.mp4') a WHERE a.t BETWEEN 5 AND 60"
ffmpeg -ss 5 -to 60 -i clip.mp4 -map 0:v:0 -c:0 copy out.mp4
```

Earlier versions spliced `trim`/`atrim` filter nodes into the graph for every `WHERE`. The seek is better on every axis we could measure. Every alias owns exactly one `-i` and can carry at most one time window in the whole query (one lower bound, one upper bound, from any combination of the forms below), so the per-alias window maps 1:1 onto ffmpeg's per-input `-ss`/`-to`. The seek trims and rebases every stream of that input at once (video, audio, subtitle, data, selected or not), the demuxer skips the front of the file instead of decoding it just to throw it away, the graph shrinks, and a column nothing else filters gets to stay a plain `-c:0 copy` instead of being forced through an encoder just because it has a start time.

A `WHERE` window on a CTE name still works the old way, because it has to: a CTE's output is a filtergraph pad, not an `-i`, so its window lowers to `trim`+`setpts` (or `atrim`+`asetpts`), spliced lazily and shared across every consumer. Video and audio only, down that path.

## Open-ended windows

A bound may be missing on either end. `<alias>.t >= <start>` seeks to `<start>` and reads to the end of the file (no `-to` at all); `<alias>.t <= <end>` reads from the start and stops at `<end>` (no `-ss`). Either operand order is accepted and means the same thing -- `<alias>.t >= 120` and `120 <= <alias>.t` are the exact same predicate, not an approximation of each other:

```sql
SELECT a.video[1]
FROM input('clip.mp4') a
WHERE a.t >= 120
```

```
$ sqlmpeg compile "SELECT a.video[1] FROM input('clip.mp4') a WHERE a.t >= 120"
ffmpeg -ss 120 -i clip.mp4 -map 0:v:0 -c:0 copy out.mp4
```

This is what kills the old `BETWEEN 120 AND 3600`-style placeholder for "to the end of the file" -- a wart the ad-splice pattern used to need (cookbook recipe 17 shows the full splice): a UNION ALL branch that used to end with a made-up, hopefully-large-enough upper bound now just writes `WHERE g.t >= 120` and means it exactly. The same works the other way for `<=`, and the two forms combine to build a closed window one bound at a time: `WHERE a.t >= 1 AND a.t <= 2` means exactly what `WHERE a.t BETWEEN 1 AND 2` means, merged into one seek.

Two things stay firmly rejected:

- **Strict `<` / `>`.** Seeks are time-based, and a strict bound has no frame-level meaning at that granularity -- `WHERE a.t > 120` is `UNSUPPORTED_SQL` with a hint pointing at `>=`/`<=`.
- **An empty window.** If both bounds end up present (from one `BETWEEN`, or from merging a `>=` and a `<=`) and the start is not strictly before the end, that is a compile-time `UNSUPPORTED_SQL` ("empty time window"), not an ffmpeg runtime surprise. A second bound of the same kind for one alias -- two lower bounds, or a `BETWEEN` overlapping a later `>=` -- is rejected the same way a second `BETWEEN` on the same alias always was: at most one lower and one upper bound per alias, however they were spelled.

On a CTE, an open window works the same way: `trim`/`atrim` only gets the args it actually has (`trim=start=3` with no `end=`, or vice versa), plus the usual `setpts`/`asetpts` rebase.

## Accuracy: decoded vs. stream-copied

Where the cut actually lands depends on whether the stream gets decoded:

- **Decoded** (filtered or re-encoded): frame-accurate. ffmpeg decodes from the previous keyframe and discards frames up to your requested point before anything downstream sees them.
- **Stream-copied**: the cut snaps back to the previous keyframe. This is not ffmpeg being lazy; a copied stream has no decoder in the loop, and a keyframe is the only place a copied stream can validly begin. Your output may start up to one full GOP early. (A GOP, if you haven't needed the term before, is the run of frames between one keyframe and the next. Every frame in it depends on the keyframe it follows, so a copied stream can't start in the middle of one - there'd be nothing for those frames to decode against.)

Measured against `tests/fixtures/testsrc.mp4`: 30 frames, 15 fps, 2.000s, and exactly one keyframe at t=0, which makes the whole file one GOP. This is the pathological worst case, chosen on purpose.

`WHERE a.t BETWEEN 0.5 AND 1.5`, a 1.000s window:

| path | `ffmpeg` | measured output duration |
|---|---|---|
| stream-copied (`SELECT a.frame`, nothing filters it) | `-ss 0.5 -to 1.5 -i ... -c copy` | **1.367s**. Not 1.000s. Snapped all the way back to the file's only keyframe, at t=0 |
| decoded (wrapped in any filter, e.g. `hflip(a.frame)`) | `-ss 0.5 -to 1.5 -i ... -c:v libx264 ...` | **1.000s** exactly |

A normally-encoded file with keyframes every couple of seconds snaps by at most one GOP, not the whole clip, but the mechanism is identical. If the exact cut point matters more than a fast remux, wrap the column in a filter and accept the re-encode. There is no third option: any tool that offers a fast, frame-accurate copy-trim is re-encoding somewhere.

### mkv durations: check the packets, not the summary

After a stream-copied trim to Matroska, `ffprobe -show_entries format=duration` is not reliable evidence of what happened. The container-level duration and each track's own `DURATION` tag come from the muxer's bookkeeping, are not recomputed to match what actually got copied, and can disagree both with reality and with each other. Measured on a copy-trim of `tests/fixtures/avs.mkv` (video+audio+subtitle, same single-keyframe fixture) with a nominal 0.6s window (`WHERE a.t BETWEEN 0.3 AND 0.9`):

```
$ ffprobe -show_entries format=duration output.mkv
duration=1.323000
$ ffprobe -show_entries stream_tags=DURATION output.mkv
video:    DURATION=00:00:01.156000000
audio:    DURATION=00:00:00.905000000
subtitle: DURATION=00:00:01.323000000
```

Four numbers in play (0.6s requested, three reported), none of them agreeing, and `format=duration` turns out to just be the largest track tag. It's an easy way to end up with a wrong test assertion or a wrong monitoring alert. When you need the real extent of a copy-trim, read the packet timestamps (`ffprobe -show_entries packet=pts_time`) rather than the container's self-reported summary.

## Captions: why trim + selected subtitles is rejected

The seek covers every stream of the alias, subtitle and data streams included, which is what makes a captioned file trimmable at all. But there is a gap on ffmpeg's side: caption packets are not retimed under an input `-ss`, on either the copy or the transcode path. The cues keep roughly their original timestamps while the video and audio rebase to the new zero. Measured on `tests/fixtures/avs.mkv`, whose subtitle cues sit at 0.023s / 0.723s / 1.423s, seeking `WHERE a.t BETWEEN 0.5 AND 1.5`:

| path | subtitle packet times in the output |
|---|---|
| stream-copied (everything `-c copy`) | 0.023 / 0.723 / 1.423, unchanged from the source |
| transcoded (video/audio re-encoded) | 0.000 / 0.700 / 1.400, still the original spacing, not shifted by the seek |

So the video now starts at what used to be 0.5s, and a caption written for the 0.723s moment still fires at ~0.7s of the new timeline, half a second before the moment it captions. Every cue, off by exactly your seek offset, for the whole clip. The cue from before the window even survives the trim and shows up in the output anyway.

The rejection below keys on whether the alias has ANY window recorded at all, not on its shape -- a tail-only `WHERE a.t >= 1` desyncs captions exactly as a closed `BETWEEN` does (there is still a nonzero seek offset), so it is rejected the same way.

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

There is no way to get a trimmed, in-sync caption track out of the same seeked input, because there is no ffmpeg incantation under the hood that produces one. The working move is to join an external subtitle file whose cues are already timed for the cut, as a second `input()` alias ([cookbook recipe 10](examples.md#10-mux-external-subtitles-in-or-pull-them-back-out) shows the join; it needs no special syntax, it's streams-as-columns plus an ordinary cross join).

A `WHERE` window on a **CTE** carrying a subtitle/data column is rejected unconditionally, selected or not: a CTE trim is a filtergraph trim, and a filtergraph cannot carry captions in the first place. The same constraint is why a subtitle column can't appear in a `UNION ALL` branch: `concat` has video and audio pads only.
