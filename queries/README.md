# queries/

Ready-to-run programs. Each file is a complete sqlmpeg query whose inputs
and outputs are `:'variables'`, filled from the command line with
`-v name=value` (psql's flag, psql's syntax). The header comment of every
file lists its variables and a worked invocation - the short version:

```bash
sqlmpeg -f queries/transcode.sql -v source=film.mkv -v dest=film.mp4
```

| file | does | variables |
| --- | --- | --- |
| [transcode.sql](transcode.sql) | H.264/AAC transcode with sane defaults | `source`, `dest` |
| [extract-audio.sql](extract-audio.sql) | pull one audio track, selected by language tag | `source`, `language`, `dest` |
| [concat-fill.sql](concat-fill.sql) | concatenate two files, silence-filling audio tracks one of them lacks | `main`, `second`, `dest` |
| [pip.sql](pip.sql) | picture-in-picture composite, corner-anchored | `main`, `overlay`, `dest` |
| [tracks-to-csv.sql](tracks-to-csv.sql) | every track's metadata as CSV on stdout | `source` |
| [remote-tracks.sql](remote-tracks.sql) | select a rendition by resolution and audio by codec from a remote manifest | `source`, `width`, `height`, `codec`, `dest` |
| [clip.sql](clip.sql) | trim to a time range, frame-accurate re-encode | `source`, `start`, `end`, `dest` |
| [speed.sql](speed.sql) | speed a clip up or down, video and audio together | `source`, `factor`, `dest` |
| [crossfade.sql](crossfade.sql) | dissolve from one clip into another | `first`, `second`, `dest` |
| [watermark.sql](watermark.sql) | overlay a centered logo image for the whole clip | `main`, `overlay`, `dest` |
| [duck.sql](duck.sql) | duck music under dialogue with a sidechain compressor | `main`, `music`, `dest` |
| [replace-audio.sql](replace-audio.sql) | swap in another file's audio track | `main`, `voice`, `dest` |
| [loudnorm-all.sql](loudnorm-all.sql) | EBU R128 loudness normalize every audio track at once | `source`, `dest` |
| [side-by-side.sql](side-by-side.sql) | hstack two files' video tracks, paired by resolution | `main`, `second`, `dest` |
| [blur-region.sql](blur-region.sql) | blur a fixed rectangular region for the whole clip | `source`, `dest` |
| [ad-insert.sql](ad-insert.sql) | splice a clip into the main video at a timestamp | `main`, `insert`, `cut`, `dest` |
| [abr-ladder.sql](abr-ladder.sql) | one decode, three output renditions (1080p/720p/480p) | `source`, `high`, `mid`, `low` |
| [split-channels.sql](split-channels.sql) | split a stereo track into two mono outputs | `source`, `dest` |
| [gif.sql](gif.sql) | palette-optimized GIF via a two-pass CTE | `source`, `dest` |
| [mux-subtitles.sql](mux-subtitles.sql) | mux an external subtitle file in as its own track | `main`, `subs`, `dest` |
| [resize.sql](resize.sql) | resize to a target width, aspect ratio preserved | `source`, `width`, `dest` |
| [compress.sql](compress.sql) | shrink a file's size with a variable quality target | `source`, `crf`, `dest` |
| [strip-audio.sql](strip-audio.sql) | drop the audio, keep the picture, no re-encode | `source`, `dest` |
| [volume.sql](volume.sql) | turn the volume up or down on every audio track | `source`, `gain`, `dest` |
| [rotate.sql](rotate.sql) | rotate a quarter turn (phone-video fix) | `source`, `dir`, `dest` |
| [crop.sql](crop.sql) | crop to a fixed rectangle | `source`, `w`, `h`, `x`, `y`, `dest` |
| [fade.sql](fade.sql) | fade in from black at the start | `source`, `duration`, `dest` |
| [burn-subtitles.sql](burn-subtitles.sql) | burn subtitles into the picture | `source`, `subs`, `dest` |
| [extract-subtitles.sql](extract-subtitles.sql) | extract the first subtitle track as its own file | `source`, `dest` |
| [thumbnail.sql](thumbnail.sql) | grab a single poster frame at a timestamp | `source`, `at`, `dest` |
| [extract-frames.sql](extract-frames.sql) | export a frame-rate-limited image sequence | `source`, `rate`, `dest` |
| [retitle.sql](retitle.sql) | rewrite the container's title and artist tags, no re-encode | `source`, `title`, `artist`, `dest` |

An undefined variable is a compile-time error naming it, so a typo'd `-v`
fails before anything runs. The [cookbook](../docs/examples.md) explains
every technique these files use; recipe 33 is the pattern itself.
