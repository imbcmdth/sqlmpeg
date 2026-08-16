# Cookbook

Real tasks, in roughly the order people meet them. Each recipe is the question as it's usually asked, the query that answers it, and the exact ffmpeg command that query compiles to. Every command on this page is real output - a test recompiles all of them and diffs byte for byte, so if a recipe is here, it works.

## How to read this file

Two kinds of query fence, checked by `tests/test_examples.py`:

- ` ```sql ` - compiles anywhere: stdlib functions and explicit stream subscripts, no probing, no filter registry. The command shown is what a machine with no ffmpeg installed at all would print.
- ` ```sql-exec ` - needs the installed ffmpeg to compile: a filter outside the stdlib, a named option, a generated source, or a whole-array splat whose length only a real file knows.

Every query fence is followed by a plain fence holding the `$ sqlmpeg compile -f query.sql ...` invocation and exactly what it prints. Paths are illustrative (`film.mp4` doesn't need to exist to compile); the recipes that genuinely need a readable file use this repo's `tests/fixtures/` paths verbatim.

## 1. Transcode a file to H.264/AAC

The most-asked ffmpeg question there is. Select the streams, name the codecs in the sink, done - `faststart` moves the index to the front so the file starts playing before it finishes downloading:

```sql
COPY (
  SELECT f.video[1], f.audio[1]
  FROM input('film.mkv') f
) TO 'film.mp4' WITH (
  video_codec 'libx264', crf 20, preset 'slow',
  audio_codec 'aac', audio_bitrate '192k', faststart true
)
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i film.mkv -map 0:v:0 -map 0:a:0 -c:0 libx264 -crf:0 20 -preset:0 slow -c:1 aac -b:1 192k -movflags +faststart film.mp4
```

## 2. Remux into another container without re-encoding

`SELECT *` means keep everything: every stream, untouched, tags intact. Nothing decodes; this runs as fast as the disk. The one wrinkle is captions - mp4 only carries `mov_text`, so the subtitle track transcodes while video and audio copy:

```sql-exec
COPY (
  SELECT * FROM input('tests/fixtures/avs.mkv') a
) TO 'film.mp4' WITH (subtitle_codec 'mov_text')
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i tests/fixtures/avs.mkv -map 0:v:0 -c:0 copy -map 0:a:0 -c:1 copy -map 0:s:0 -metadata:s:2 language=eng -c:2 mov_text film.mp4
```

## 3. Extract the audio track to its own file

The SELECT list is the output. Select only the audio and that's the whole file - stream-copied, no generation loss:

```sql
COPY (
  SELECT f.audio[1]
  FROM input('film.mkv') f
) TO 'soundtrack.m4a'
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i film.mkv -map 0:a:0 -c:0 copy soundtrack.m4a
```

## 4. Trim a clip: fast stream copy, or frame-accurate re-encode

`WHERE t BETWEEN` becomes an input seek, and a stream nothing filters stays a copy - instant, but the cut snaps back to the previous keyframe, so it can start a little early:

```sql
SELECT a.video[1], a.audio[1]
FROM input('clip.mp4') a
WHERE a.t BETWEEN 5 AND 60
```

```
$ sqlmpeg compile -f query.sql -o cut.mp4
ffmpeg -ss 5 -to 60 -i clip.mp4 -map 0:v:0 -c:0 copy -map 0:a:0 -c:1 copy cut.mp4
```

When the exact cut point matters, re-encode: a decoded stream trims frame-accurate. The trade and the measurements behind it are in [docs/trimming.md](trimming.md):

```sql
COPY (
  SELECT a.video[1], a.audio[1]
  FROM input('clip.mp4') a
  WHERE a.t BETWEEN 5 AND 60
) TO 'cut.mp4' WITH (video_codec 'libx264', crf 18, audio_codec 'aac')
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -ss 5 -to 60 -i clip.mp4 -map 0:v:0 -map 0:a:0 -c:0 libx264 -crf:0 18 -c:1 aac cut.mp4
```

## 5. Resize to 1280 wide, or to half size

`-2` for the height means "keep the aspect ratio, rounded to an even number" - encoders insist on even dimensions, and this saves you doing the arithmetic:

```sql
SELECT scale(f.frame, 1280, -2), f.audio[1]
FROM input('film.mp4') f
```

```
$ sqlmpeg compile -f query.sql -o small.mp4
ffmpeg -i film.mp4 -filter_complex '[0:v:0]scale=w=1280:h=-2[out0]' -map '[out0]' -map 0:a:0 -c:1 copy small.mp4
```

Or give `scale` one factor instead of a width and height:

```sql
SELECT scale(f.frame, 0.5), f.audio[1]
FROM input('film.mp4') f
```

```
$ sqlmpeg compile -f query.sql -o half.mp4
ffmpeg -i film.mp4 -filter_complex '[0:v:0]scale=w=iw*0.5:h=-2[out0]' -map '[out0]' -map 0:a:0 -c:1 copy half.mp4
```

## 6. Rotate a phone video 90 degrees

For quarter turns, ffmpeg's `transpose` is the right tool (it swaps the axes rather than resampling). The stdlib's `rotate(f.frame, degrees)` handles arbitrary angles:

```sql-exec
SELECT transpose(v.frame, dir => 'clock'), v.audio[1]
FROM input('phone.mp4') v
```

```
$ sqlmpeg compile -f query.sql -o upright.mp4
ffmpeg -i phone.mp4 -filter_complex '[0:v:0]transpose=dir=clock[out0]' -map '[out0]' -map 0:a:0 -c:1 copy upright.mp4
```

## 7. Sharpen a soft-looking video

Any of your ffmpeg's filters is callable directly, options by name, checked against what the binary actually supports. (`sharpen(f.frame, amount)` is the portable one-knob version if you don't need the fine control.)

```sql-exec
SELECT unsharp(a.frame, luma_msize_x => 7, luma_amount => 1.5), a.audio[1]
FROM input('clip.mp4') a
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i clip.mp4 -filter_complex '[0:v:0]unsharp=luma_msize_x=7:luma_amount=1.5[out0]' -map '[out0]' -map 0:a:0 -c:1 copy out.mp4
```

## 8. Concatenate two clips

`UNION ALL` is ffmpeg's concat. SQL requires the branches to agree on column count, type and order, and that is exactly concat's segment contract - the interleaving that's so easy to get wrong by hand is generated for you:

```sql
SELECT a.video[1], a.audio[1] FROM input('part1.mp4') a
UNION ALL
SELECT b.video[1], b.audio[1] FROM input('part2.mp4') b
```

```
$ sqlmpeg compile -f query.sql -o joined.mp4
ffmpeg -i part1.mp4 -i part2.mp4 -filter_complex '[0:v:0][0:a:0][1:v:0][1:a:0]concat=n=2:v=1:a=1[out0][out1]' -map '[out0]' -map '[out1]' joined.mp4
```

And it scales to files you'd rather not count streams in: splat the whole audio array and the languages pair up positionally, English with English, French with French, tags surviving. (This one needs real files - a splat has to know how many tracks there are.)

```sql-exec
SELECT a.frame, a.audio FROM input('tests/fixtures/av2.mp4') a
UNION ALL
SELECT b.frame, b.audio FROM input('tests/fixtures/av3.mp4') b
```

```
$ sqlmpeg compile -f query.sql -o season.mkv
ffmpeg -i tests/fixtures/av2.mp4 -i tests/fixtures/av3.mp4 -filter_complex '[0:v:0][0:a:0][0:a:1][1:v:0][1:a:0][1:a:1]concat=n=2:v=1:a=2[out0][out1][out2]' -map '[out0]' -map '[out1]' -metadata:s:1 language=eng -map '[out2]' -metadata:s:2 language=fra season.mkv
```

## 9. Watermark a video

`loop => true` keeps a still image alive for the whole duration, and the position is an ffmpeg expression - `(W-w)/2` centers it without you knowing either file's dimensions:

```sql
SELECT overlay(f.frame, logo.frame, '(W-w)/2', '(H-h)/2'), f.audio[1]
FROM input('film.mp4') f, input('watermark.png', loop => true) logo
```

```
$ sqlmpeg compile -f query.sql -o branded.mp4
ffmpeg -i film.mp4 -loop 1 -i watermark.png -filter_complex '[0:v:0][1:v:0]overlay=x=(W-w)/2:y=(H-h)/2[out0]' -map '[out0]' -map 0:a:0 -c:1 copy branded.mp4
```

## 10. Mux external subtitles in, or pull them back out

A subtitle file is just another input. Select its track next to your video and audio; mp4 demands `mov_text`, mkv would take `srt` or `webvtt`:

```sql
COPY (
  SELECT f.video[1], f.audio[1], s.subtitle[1]
  FROM input('film.mp4') f, input('subs.en.vtt') s
) TO 'captioned.mp4' WITH (subtitle_codec 'mov_text')
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i film.mp4 -i subs.en.vtt -map 0:v:0 -c:0 copy -map 0:a:0 -c:1 copy -map 1:s:0 -c:2 mov_text captioned.mp4
```

Extraction is the same idea with a shorter SELECT list - the container implies the format:

```sql
COPY (
  SELECT f.subtitle[1]
  FROM input('film.mkv') f
) TO 'subs.en.srt'
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i film.mkv -map 0:s:0 -c:0 copy subs.en.srt
```

## 11. Burn subtitles into the picture

Different from muxing a track: `subtitles()` is a video filter that renders the cues into the pixels. The subtitle file is read when ffmpeg runs, so it needs to exist then, not now:

```sql
SELECT subtitles(f.frame, 'subs.en.srt'), f.audio[1]
FROM input('film.mp4') f
```

```
$ sqlmpeg compile -f query.sql -o burned.mp4
ffmpeg -i film.mp4 -filter_complex '[0:v:0]subtitles=filename=subs.en.srt[out0]' -map '[out0]' -map 0:a:0 -c:1 copy burned.mp4
```

## 12. Speed a clip up 2x, picture and sound together

Two functions because the two stream types speed up differently: `speed` restamps video frames, `atempo` resamples audio while keeping the pitch (so nobody turns into a chipmunk):

```sql
SELECT speed(f.frame, 2), atempo(f.audio[1], 2)
FROM input('film.mp4') f
```

```
$ sqlmpeg compile -f query.sql -o fast.mp4
ffmpeg -i film.mp4 -filter_complex '[0:v:0]setpts=PTS/2[out0];[0:a:0]atempo=tempo=2[out1]' -map '[out0]' -map '[out1]' fast.mp4
```

## 13. Crossfade between two clips

`crossfade(a, b, duration, offset)` - the offset is seconds into the FIRST clip where the fade begins, so a 10-second clip with a 1-second fade starts dissolving at 9. `acrossfade` does the same for the sound:

```sql
SELECT crossfade(a.frame, b.frame, 1, 9),
       acrossfade(a.audio[1], b.audio[1], 1)
FROM input('one.mp4') a, input('two.mp4') b
```

```
$ sqlmpeg compile -f query.sql -o dissolve.mp4
ffmpeg -i one.mp4 -i two.mp4 -filter_complex '[0:v:0][1:v:0]xfade=duration=1:offset=9[out0];[0:a:0][1:a:0]acrossfade=d=1[out1]' -map '[out0]' -map '[out1]' dissolve.mp4
```

## 14. Turn a clip into a GIF

The good-looking way needs two passes over the frames - one to build a palette, one to use it. Write it as a CTE consumed twice; the compiler inserts the split:

```sql-exec
WITH small AS (
  SELECT fps(scale(v.frame, 480, -2), 12) AS frame
  FROM input('clip.mp4') v
)
SELECT paletteuse(small.frame, palettegen(small.frame))
FROM small
```

```
$ sqlmpeg compile -f query.sql -o clip.gif
ffmpeg -i clip.mp4 -filter_complex '[0:v:0]scale=w=480:h=-2,fps=fps=12,split=2[n2_split0][n2_split1];[n2_split0]palettegen[n3];[n2_split1][n3]paletteuse[out0]' -map '[out0]' clip.gif
```

## 15. Replace a video's audio, or duck music under the dialogue

Swapping is just selecting video from one input and audio from another:

```sql
SELECT v.video[1], m.audio[1]
FROM input('film.mp4') v, input('voiceover.wav') m
```

```
$ sqlmpeg compile -f query.sql -o dubbed.mp4
ffmpeg -i film.mp4 -i voiceover.wav -map 0:v:0 -c:0 copy -map 1:a:0 -c:1 copy dubbed.mp4
```

Keeping both, with the music turned down, is a mix:

```sql
SELECT v.video[1], amix(v.audio[1], volume(m.audio[1], 0.2))
FROM input('film.mp4') v, input('music.m4a') m
```

```
$ sqlmpeg compile -f query.sql -o scored.mp4
ffmpeg -i film.mp4 -i music.m4a -filter_complex '[1:a:0]volume=volume=0.2[n1];[0:a:0][n1]amix=inputs=2[out1]' -map 0:v:0 -c:0 copy -map '[out1]' scored.mp4
```

Real ducking - music that dips when someone speaks - is a sidechain compressor keyed off the dialogue. Naming `v.audio[1]` twice is fine; the compiler inserts the split:

```sql-exec
SELECT v.video[1], amix(v.audio[1], sidechaincompress(m.audio[1], v.audio[1], threshold => 0.03, ratio => 8))
FROM input('film.mp4') v, input('music.m4a') m
```

```
$ sqlmpeg compile -f query.sql -o ducked.mp4
ffmpeg -i film.mp4 -i music.m4a -filter_complex '[0:a:0]asplit=2[src_v_a_0_split0][src_v_a_0_split1];[1:a:0][src_v_a_0_split0]sidechaincompress=threshold=0.03:ratio=8[n1];[src_v_a_0_split1][n1]amix=inputs=2[out1]' -map 0:v:0 -c:0 copy -map '[out1]' ducked.mp4
```

## 16. Picture-in-picture

A quarter-size camera in the bottom-right corner, 20 pixels off each edge - the expressions mean the position holds whatever the two resolutions are. (The dual-language version, with the audio mixed per language, is the README's opening demo.)

```sql
SELECT overlay(f.frame, scale(c.frame, 0.25), 'W-w-20', 'H-h-20'), f.audio[1]
FROM input('film.mp4') f, input('camera.mp4') c
```

```
$ sqlmpeg compile -f query.sql -o pip.mp4
ffmpeg -i film.mp4 -i camera.mp4 -filter_complex '[1:v:0]scale=w=iw*0.25:h=-2[n1];[0:v:0][n1]overlay=x=W-w-20:y=H-h-20[out0]' -map '[out0]' -map 0:a:0 -c:1 copy pip.mp4
```

## 17. Insert a clip at a timestamp

The splice: cut away to the insert, then resume. The same file appears under two aliases with two windows, and the tail's `>= 120` means "to the end" with no made-up end time:

```sql
SELECT f.video[1], f.audio[1] FROM input('film.mp4') f WHERE f.t <= 120
UNION ALL
SELECT ad.video[1], ad.audio[1] FROM input('promo.mp4') ad
UNION ALL
SELECT g.video[1], g.audio[1] FROM input('film.mp4') g WHERE g.t >= 120
```

```
$ sqlmpeg compile -f query.sql -o spliced.mp4
ffmpeg -to 120 -i film.mp4 -i promo.mp4 -ss 120 -i film.mp4 -filter_complex '[0:v:0][0:a:0][1:v:0][1:a:0][2:v:0][2:a:0]concat=n=3:v=1:a=1[out0][out1]' -map '[out0]' -map '[out1]' spliced.mp4
```

Or keep the main video playing and overlay the insert on top: a delayed video stream is transparent until its start time (and after it ends), so it composes with a plain `overlay` - no timeline bookkeeping:

```sql
SELECT overlay(f.frame, delay(promo.frame, 120), 20, 20), f.audio[1]
FROM input('film.mp4') f, input('promo.mp4') promo
```

```
$ sqlmpeg compile -f query.sql -o overlaid.mp4
ffmpeg -i film.mp4 -i promo.mp4 -filter_complex '[1:v:0]format=pix_fmts=yuva420p,tpad=start_duration=120:stop=1:color=black@0[n2];[0:v:0][n2]overlay=x=20:y=20[out0]' -map '[out0]' -map 0:a:0 -c:1 copy overlaid.mp4
```

## 18. Normalize loudness on every language track at once

A bare `.audio` is the whole track array; handing it to a filter broadcasts, one node per language, and every output keeps its language tag. (One honest wart: the stdlib's `normalize()` is currently unreachable by its bare name - Postgres grammar claims it, the same way it claims `pad` - so this recipe uses the filter's own spelling. `I` is EBU R128 integrated loudness, and yes, it's a capital I.)

```sql-exec
SELECT f.video[1], ffmpeg.loudnorm(f.audio, I => -23)
FROM input('tests/fixtures/av2.mp4') f
```

```
$ sqlmpeg compile -f query.sql -o broadcast.mkv
ffmpeg -i tests/fixtures/av2.mp4 -filter_complex '[0:a:0]loudnorm=I=-23[out1];[0:a:1]loudnorm=I=-23[out2]' -map 0:v:0 -c:0 copy -map '[out1]' -metadata:s:1 language=eng -map '[out2]' -metadata:s:2 language=fra broadcast.mkv
```

## 19. Blur a region, or blur during a time window

`blur_regions` is crop, blur and overlay in one call - the license-plate special:

```sql
SELECT blur_regions(f.frame, 900, 60, 320, 180, 20), f.audio[1]
FROM input('interview.mp4') f
```

```
$ sqlmpeg compile -f query.sql -o anonymized.mp4
ffmpeg -i interview.mp4 -filter_complex '[0:v:0]split=2[src_f_v_0_split0][src_f_v_0_split1];[src_f_v_0_split0]crop=w=320:h=180:x=900:y=60,gblur=sigma=20[n2];[src_f_v_0_split1][n2]overlay=x=900:y=60[out0]' -map '[out0]' -map 0:a:0 -c:1 copy anonymized.mp4
```

To apply an effect only during a time window, `enable` is the switch - no trimming, no branches, no concat, just a filter that turns itself on and off:

```sql-exec
SELECT blur(a.frame, 12, enable => 'between(t,0.5,1.5)')
FROM input('clip.mp4') a
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i clip.mp4 -filter_complex '[0:v:0]gblur=sigma=12:enable=between(t\,0.5\,1.5)[out0]' -map '[out0]' out.mp4
```

## 20. Generate test media

Sources live in FROM and consume no input file at all - note the command below has no `-i`:

```sql-exec
SELECT t.frame, s.audio[1]
FROM ffmpeg.testsrc2(duration => 10, size => '1280x720', rate => 30) t,
     ffmpeg.sine(frequency => 440, duration => 10) s
```

```
$ sqlmpeg compile -f query.sql -o bars.mp4
ffmpeg -filter_complex 'testsrc2=duration=10:size=1280x720:rate=30[out0];sine=frequency=440:duration=10[out1]' -map '[out0]' -map '[out1]' bars.mp4
```

They also solve a quieter problem: `UNION ALL` branches must match column for column, so appending a slate to a clip needs a silent audio track from somewhere. `anullsrc` is that somewhere:

```sql-exec
SELECT f.video[1], f.audio[1] FROM input('clip.mp4') f
UNION ALL
SELECT t.video[1], s.audio[1]
FROM ffmpeg.color(color => 'black', duration => 3, size => '1280x720', rate => 30) t,
     ffmpeg.anullsrc(duration => 3) s
```

```
$ sqlmpeg compile -f query.sql -o with-slate.mp4
ffmpeg -i clip.mp4 -filter_complex 'color=color=black:duration=3:size=1280x720:rate=30[n1];anullsrc=duration=3[n2];[0:v:0][0:a:0][n1][n2]concat=n=2:v=1:a=1[out0][out1]' -map '[out0]' -map '[out1]' with-slate.mp4
```

## 21. Split a stereo track, or compress it in bands

A few filters return a whole array, sized by one of their own options. `channelsplit` turns one stereo track into two mono streams; splatted into the SELECT list, each becomes its own output:

```sql-exec
SELECT ffmpeg.channelsplit(a.audio[1])
FROM input('stereo.mp4') a
```

```
$ sqlmpeg compile -f query.sql -o channels.mkv
ffmpeg -i stereo.mp4 -filter_complex '[0:a:0]channelsplit[out0][out1]' -map '[out0]' -map '[out1]' channels.mkv
```

`acrossover` splits by frequency instead - two split points make three bands - and that's the shape of multiband compression: split, compress each band on its own settings, mix back:

```sql-exec
WITH bands AS (
  SELECT ffmpeg.acrossover(a.audio[1], split => '300 3000') AS b
  FROM input('song.m4a') a
)
SELECT amix(amix(acompressor(bands.b[1], threshold => 0.1, ratio => 4),
                 acompressor(bands.b[2], threshold => 0.05, ratio => 6)),
            acompressor(bands.b[3], threshold => 0.1, ratio => 4))
FROM bands
```

```
$ sqlmpeg compile -f query.sql -o mastered.m4a
ffmpeg -i song.m4a -filter_complex '[0:a:0]acrossover=split=300\ 3000[n10][n11][n12];[n10]acompressor=threshold=0.1:ratio=4[n2];[n11]acompressor=threshold=0.05:ratio=6[n3];[n2][n3]amix=inputs=2[n4];[n12]acompressor=threshold=0.1:ratio=4[n5];[n4][n5]amix=inputs=2[out0]' -map '[out0]' mastered.m4a
```

## 22. One decode, several outputs

A `CREATE VIEW` is a named, shared piece of the graph, and each `COPY` after it is one output file - the whole script is a single ffmpeg run, so the watermarking happens once no matter how many files consume it. (The classic version of this, the ABR rendition ladder, is in the README.)

```sql
CREATE VIEW branded AS
  SELECT overlay(f.frame, logo.frame, 'W-w-20', 20) AS v, f.audio[1] AS a
  FROM input('film.mp4') f, input('watermark.png', loop => true) logo;

COPY (SELECT scale(b.v, 1280, -2) AS v, b.a FROM branded b)
TO 'web.mp4' WITH (video_codec 'libx264', crf 21, audio_codec 'aac');

COPY (SELECT b.a FROM branded b)
TO 'podcast.m4a' WITH (audio_codec 'aac', audio_bitrate '128k')
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i film.mp4 -loop 1 -i watermark.png -filter_complex '[0:v:0][1:v:0]overlay=x=W-w-20:y=20,scale=w=1280:h=-2[out0]' -map '[out0]' -map 0:a:0 -c:0 libx264 -crf:0 21 -c:1 aac web.mp4 -map 0:a:0 -c:0 aac -b:0 128k podcast.m4a
```
