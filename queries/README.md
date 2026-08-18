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
| `transcode.sql` | H.264/AAC transcode with sane defaults | `source`, `dest` |
| `extract-audio.sql` | pull one audio track, selected by language tag | `source`, `language`, `dest` |
| `concat-fill.sql` | concatenate two files, silence-filling audio tracks one of them lacks | `main`, `second`, `dest` |
| `pip.sql` | picture-in-picture composite, corner-anchored | `main`, `overlay`, `dest` |
| `tracks-to-csv.sql` | every track's metadata as CSV on stdout | `source` |
| `remote-tracks.sql` | select a rendition by resolution and audio by codec from a remote manifest | `source`, `width`, `height`, `codec`, `dest` |

An undefined variable is a compile-time error naming it, so a typo'd `-v`
fails before anything runs. The [cookbook](../docs/examples.md) explains
every technique these files use; recipe 33 is the pattern itself.
