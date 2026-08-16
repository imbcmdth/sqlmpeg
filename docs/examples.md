# Cookbook

<!-- prose: TODO -->

## How to read this file

Fence convention (checked by `tests/test_examples.py`, which recompiles every
query on this page and compares byte for byte):

- ` ```sql ` — OFFLINE tier. Stdlib functions and explicit subscripts only; no
  probing, no filter registry. The command below it is what a machine with no
  ffmpeg installed at all prints.
- ` ```sql-exec ` — needs the installed ffmpeg: a filter outside the stdlib, a
  named option, a generated source, or a whole-array splat whose length only
  the file knows. Compiled (never executed) in an `exec`-marked test.
- Every query fence is followed by a plain ` ``` ` fence whose first line is
  the `$ sqlmpeg compile -f query.sql ...` invocation and whose remaining
  lines are exactly what it prints. A query fence with no command fence after
  it fails the harness.
- Paths are illustrative (`film.mp4` need not exist). Recipes that need a
  readable file name one from this repo's `tests/fixtures/` verbatim.

<!-- prose: TODO -->

## 1. Transcode a file to H.264/AAC

<!-- prose: TODO -->

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

<!-- prose: TODO -->

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

<!-- prose: TODO -->

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

<!-- prose: TODO (the copy path; link docs/trimming.md) -->

```sql
SELECT a.video[1], a.audio[1]
FROM input('clip.mp4') a
WHERE a.t BETWEEN 5 AND 60
```

```
$ sqlmpeg compile -f query.sql -o cut.mp4
ffmpeg -ss 5 -to 60 -i clip.mp4 -map 0:v:0 -c:0 copy -map 0:a:0 -c:1 copy cut.mp4
```

<!-- prose: TODO (the decoded path is frame-accurate) -->

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

<!-- prose: TODO (-2 keeps the aspect ratio, even) -->

```sql
SELECT scale(f.frame, 1280, -2), f.audio[1]
FROM input('film.mp4') f
```

```
$ sqlmpeg compile -f query.sql -o small.mp4
ffmpeg -i film.mp4 -filter_complex '[0:v:0]scale=w=1280:h=-2[out0]' -map '[out0]' -map 0:a:0 -c:1 copy small.mp4
```

<!-- prose: TODO (the one-argument scale-factor form) -->

```sql
SELECT scale(f.frame, 0.5), f.audio[1]
FROM input('film.mp4') f
```

```
$ sqlmpeg compile -f query.sql -o half.mp4
ffmpeg -i film.mp4 -filter_complex '[0:v:0]scale=w=iw*0.5:h=-2[out0]' -map '[out0]' -map 0:a:0 -c:1 copy half.mp4
```

## 6. Rotate a phone video 90 degrees

<!-- prose: TODO (tier-2 transpose; stdlib rotate(f, degrees) for other angles) -->

```sql-exec
SELECT transpose(v.frame, dir => 'clock'), v.audio[1]
FROM input('phone.mp4') v
```

```
$ sqlmpeg compile -f query.sql -o upright.mp4
ffmpeg -i phone.mp4 -filter_complex '[0:v:0]transpose=dir=clock[out0]' -map '[out0]' -map 0:a:0 -c:1 copy upright.mp4
```

## 7. Sharpen a soft-looking video

<!-- prose: TODO (named options reach the whole option set; sharpen() is the portable spelling) -->

```sql-exec
SELECT unsharp(a.frame, luma_msize_x => 7, luma_amount => 1.5), a.audio[1]
FROM input('clip.mp4') a
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i clip.mp4 -filter_complex '[0:v:0]unsharp=luma_msize_x=7:luma_amount=1.5[out0]' -map '[out0]' -map 0:a:0 -c:1 copy out.mp4
```

## 8. Concatenate two clips

<!-- prose: TODO (UNION ALL is concat's segment contract) -->

```sql
SELECT a.video[1], a.audio[1] FROM input('part1.mp4') a
UNION ALL
SELECT b.video[1], b.audio[1] FROM input('part2.mp4') b
```

```
$ sqlmpeg compile -f query.sql -o joined.mp4
ffmpeg -i part1.mp4 -i part2.mp4 -filter_complex '[0:v:0][0:a:0][1:v:0][1:a:0]concat=n=2:v=1:a=1[out0][out1]' -map '[out0]' -map '[out1]' joined.mp4
```

<!-- prose: TODO (the dual-language array pairing; fixtures because a splat needs a real file) -->

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

<!-- prose: TODO (loop => true on the still; the centering expression runs in ffmpeg) -->

```sql
SELECT overlay(f.frame, logo.frame, '(W-w)/2', '(H-h)/2'), f.audio[1]
FROM input('film.mp4') f, input('watermark.png', loop => true) logo
```

```
$ sqlmpeg compile -f query.sql -o branded.mp4
ffmpeg -i film.mp4 -loop 1 -i watermark.png -filter_complex '[0:v:0][1:v:0]overlay=x=(W-w)/2:y=(H-h)/2[out0]' -map '[out0]' -map 0:a:0 -c:1 copy branded.mp4
```

## 10. Mux external subtitles in, or pull them back out

<!-- prose: TODO (a subtitle file is just another input alias; mp4 needs mov_text) -->

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

<!-- prose: TODO (extraction is a shorter SELECT list) -->

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

<!-- prose: TODO (a filter, not a track: the file is read at run time) -->

```sql
SELECT subtitles(f.frame, 'subs.en.srt'), f.audio[1]
FROM input('film.mp4') f
```

```
$ sqlmpeg compile -f query.sql -o burned.mp4
ffmpeg -i film.mp4 -filter_complex '[0:v:0]subtitles=filename=subs.en.srt[out0]' -map '[out0]' -map 0:a:0 -c:1 copy burned.mp4
```

## 12. Speed a clip up 2x, picture and sound together

<!-- prose: TODO (speed restamps video, atempo resamples audio pitch-preserving) -->

```sql
SELECT speed(f.frame, 2), atempo(f.audio[1], 2)
FROM input('film.mp4') f
```

```
$ sqlmpeg compile -f query.sql -o fast.mp4
ffmpeg -i film.mp4 -filter_complex '[0:v:0]setpts=PTS/2[out0];[0:a:0]atempo=tempo=2[out1]' -map '[out0]' -map '[out1]' fast.mp4
```

## 13. Crossfade between two clips

<!-- prose: TODO (crossfade(a, b, dur, offset); acrossfade for the sound) -->

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

<!-- prose: TODO (palettegen/paletteuse round trip through a CTE; the split is inserted) -->

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

<!-- prose: TODO (video from one input, audio from another) -->

```sql
SELECT v.video[1], m.audio[1]
FROM input('film.mp4') v, input('voiceover.wav') m
```

```
$ sqlmpeg compile -f query.sql -o dubbed.mp4
ffmpeg -i film.mp4 -i voiceover.wav -map 0:v:0 -c:0 copy -map 1:a:0 -c:1 copy dubbed.mp4
```

<!-- prose: TODO (keep both, music turned down) -->

```sql
SELECT v.video[1], amix(v.audio[1], volume(m.audio[1], 0.2))
FROM input('film.mp4') v, input('music.m4a') m
```

```
$ sqlmpeg compile -f query.sql -o scored.mp4
ffmpeg -i film.mp4 -i music.m4a -filter_complex '[1:a:0]volume=volume=0.2[n1];[0:a:0][n1]amix=inputs=2[out1]' -map 0:v:0 -c:0 copy -map '[out1]' scored.mp4
```

<!-- prose: TODO (real ducking is a sidechain keyed off the dialogue; naming the column twice inserts the asplit) -->

```sql-exec
SELECT v.video[1], amix(v.audio[1], sidechaincompress(m.audio[1], v.audio[1], threshold => 0.03, ratio => 8))
FROM input('film.mp4') v, input('music.m4a') m
```

```
$ sqlmpeg compile -f query.sql -o ducked.mp4
ffmpeg -i film.mp4 -i music.m4a -filter_complex '[0:a:0]asplit=2[src_v_a_0_split0][src_v_a_0_split1];[1:a:0][src_v_a_0_split0]sidechaincompress=threshold=0.03:ratio=8[n1];[src_v_a_0_split1][n1]amix=inputs=2[out1]' -map 0:v:0 -c:0 copy -map '[out1]' ducked.mp4
```

## 16. Picture-in-picture

<!-- prose: TODO (the dual-language version is the README demo) -->

```sql
SELECT overlay(f.frame, scale(c.frame, 0.25), 'W-w-20', 'H-h-20'), f.audio[1]
FROM input('film.mp4') f, input('camera.mp4') c
```

```
$ sqlmpeg compile -f query.sql -o pip.mp4
ffmpeg -i film.mp4 -i camera.mp4 -filter_complex '[1:v:0]scale=w=iw*0.25:h=-2[n1];[0:v:0][n1]overlay=x=W-w-20:y=H-h-20[out0]' -map '[out0]' -map 0:a:0 -c:1 copy pip.mp4
```

## 17. Insert a clip at a timestamp

<!-- prose: TODO (the splice: three branches, two aliases over one file, open-ended windows) -->

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

<!-- prose: TODO (the overlay variant: delay makes a transparent canvas until its moment) -->

```sql
SELECT overlay(f.frame, delay(promo.frame, 120), 20, 20), f.audio[1]
FROM input('film.mp4') f, input('promo.mp4') promo
```

```
$ sqlmpeg compile -f query.sql -o overlaid.mp4
ffmpeg -i film.mp4 -i promo.mp4 -filter_complex '[1:v:0]format=pix_fmts=yuva420p,tpad=start_duration=120:stop=1:color=black@0[n2];[0:v:0][n2]overlay=x=20:y=20[out0]' -map '[out0]' -map 0:a:0 -c:1 copy overlaid.mp4
```

## 18. Normalize loudness on every language track at once

<!-- prose: TODO (broadcast over the bare array, tags preserved; needs a real file for the length. NB: the stdlib's normalize() is unreachable by its bare name -- sqlglot's NORMALIZE builtin claims it -- so this uses the tier-2 spelling) -->

```sql-exec
SELECT f.video[1], ffmpeg.loudnorm(f.audio, I => -23)
FROM input('tests/fixtures/av2.mp4') f
```

```
$ sqlmpeg compile -f query.sql -o broadcast.mkv
ffmpeg -i tests/fixtures/av2.mp4 -filter_complex '[0:a:0]loudnorm=I=-23[out1];[0:a:1]loudnorm=I=-23[out2]' -map 0:v:0 -c:0 copy -map '[out1]' -metadata:s:1 language=eng -map '[out2]' -metadata:s:2 language=fra broadcast.mkv
```

## 19. Blur a region, or blur during a time window

<!-- prose: TODO (blur_regions is crop+gblur+overlay in one call) -->

```sql
SELECT blur_regions(f.frame, 900, 60, 320, 180, 20), f.audio[1]
FROM input('interview.mp4') f
```

```
$ sqlmpeg compile -f query.sql -o anonymized.mp4
ffmpeg -i interview.mp4 -filter_complex '[0:v:0]split=2[src_f_v_0_split0][src_f_v_0_split1];[src_f_v_0_split0]crop=w=320:h=180:x=900:y=60,gblur=sigma=20[n2];[src_f_v_0_split1][n2]overlay=x=900:y=60[out0]' -map '[out0]' -map 0:a:0 -c:1 copy anonymized.mp4
```

<!-- prose: TODO (enable is the timeline switch: no trim, no branch, no concat) -->

```sql-exec
SELECT blur(a.frame, 12, enable => 'between(t,0.5,1.5)')
FROM input('clip.mp4') a
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i clip.mp4 -filter_complex '[0:v:0]gblur=sigma=12:enable=between(t\,0.5\,1.5)[out0]' -map '[out0]' out.mp4
```

## 20. Generate test media

<!-- prose: TODO (sources live in FROM, are not an -i, and need no file) -->

```sql-exec
SELECT t.frame, s.audio[1]
FROM ffmpeg.testsrc2(duration => 10, size => '1280x720', rate => 30) t,
     ffmpeg.sine(frequency => 440, duration => 10) s
```

```
$ sqlmpeg compile -f query.sql -o bars.mp4
ffmpeg -filter_complex 'testsrc2=duration=10:size=1280x720:rate=30[out0];sine=frequency=440:duration=10[out1]' -map '[out0]' -map '[out1]' bars.mp4
```

<!-- prose: TODO (anullsrc supplies the silent track a concat segment needs) -->

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

<!-- prose: TODO (array-returning filters, sized by one of their own options) -->

```sql-exec
SELECT ffmpeg.channelsplit(a.audio[1])
FROM input('stereo.mp4') a
```

```
$ sqlmpeg compile -f query.sql -o channels.mkv
ffmpeg -i stereo.mp4 -filter_complex '[0:a:0]channelsplit[out0][out1]' -map '[out0]' -map '[out1]' channels.mkv
```

<!-- prose: TODO (acrossover splits by frequency: multiband compression) -->

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

<!-- prose: TODO (a view + several COPYs is one ffmpeg invocation; the ABR ladder is in the README) -->

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
