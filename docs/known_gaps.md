# Known gaps

What sqlmpeg cannot express today, and the sharp edges of what it can.
If one of these blocks you, run ffmpeg directly for that step — sqlmpeg
output is plain ffmpeg, so the two mix freely in a script.

## Not expressible

| gap | ffmpeg surface | notes |
| --- | --- | --- |
| Attachments and cover art | `-attach`, `attached_pic` disposition | No sink surface for attaching files (fonts, thumbnails). |
| HLS / DASH packaging | `hls_*`, segment muxer options | Format-specific muxer option families are not modeled. Writing to an `.m3u8` path may work for defaults, but segment length, playlist type, and encryption options have no spelling. |
| Protocol options | `-headers`, `-user_agent`, `-rtsp_transport`, `-timeout` | Network inputs and outputs are passed to ffmpeg verbatim; per-protocol tuning options have no input/sink spelling. Authenticated URLs work only if the credential fits in the URL itself. |
| Lossless concat | concat demuxer (`-f concat -i list.txt -c copy`) | Joining files without re-encoding needs the demuxer's list-file protocol. `concat` in sqlmpeg is the filter, which re-encodes. |

## Sharp edges

- **Stream-copied splits snap to keyframes.** An output fan-out that
  splits by chapter (or any time window) with stream copy starts each
  piece at the nearest preceding keyframe, exactly as ffmpeg does.
  Re-encode the video for frame-accurate cuts.
- **The printed `loudnorm2` chain is POSIX-shell only.** It uses
  `eval`, `$()`, and environment splices, and calls `sqlmpeg
  loudnorm2env` at run time. On cmd.exe or PowerShell, use `sqlmpeg
  run`, which performs the substitution in-process.
- **`drawtext` needs a font on some builds.** The filter works out of
  the box; pass `fontfile` like any other option. Omitting it falls
  back to fontconfig, which depends on how the local ffmpeg was built —
  some Windows builds crash instead of picking a default. When in
  doubt, name the font.
- **A CTE-keyed group cannot fan out.** A fan-out `TO (expression)`
  builds its filename from row metadata columns, and a CTE's columns
  are streams - so a `GROUP BY` over a CTE column with more than one
  group has no way to name its files yet. Group inside the CTE's body
  (where metadata columns exist) instead.
- **Streams ffmpeg cannot identify are rejected at compile time.**
  Some sources carry streams with no detectable codec (certain DASH
  text tracks, for example). Selecting one is a compile error; table
  queries over the same source still work.
