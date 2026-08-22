# Cookbook

Real tasks. Every shown output on this page is real - a test reruns all of them and diffs the resulting ffmpeg commands, so if a recipe is here, it works.

Most recipes are parameterized (`:'source'`-style variables, filled by the `-v` flags in the shown command), so they are programs: swap the `-v` values and they run against your files. Recipe 33 explains the mechanism; [queries/](../queries/) collects a few handy ready-made programs.

## 1. Transcode a file to H.264/AAC

The most-asked ffmpeg question there is. Select the streams, name the codecs in the sink, done - `faststart` moves the index to the front so the file starts playing before it finishes downloading:

```sql
COPY (
  SELECT f.video[1], f.audio[1]
  FROM input(:'source') f
) TO :'dest' WITH (
  video_codec 'libx264', crf 20, preset 'slow',
  audio_codec 'aac', audio_bitrate '192k', faststart true
)
```

```
$ sqlmpeg compile -f query.sql -v source=film.mkv -v dest=film.mp4
ffmpeg -i film.mkv -map 0:v:0 -map 0:a:0 -c:0 libx264 -crf:0 20 -preset:0 slow -c:1 aac \
  -b:1 192k -movflags +faststart film.mp4
```

## 2. Remux into another container without re-encoding

`SELECT *` means keep everything: the container's stream arrays - video, audio, subtitle, data, in that order - untouched, tags intact, and chapters riding through as ffmpeg's own default. Nothing decodes; this runs as fast as the disk. The one wrinkle is captions - mp4 only carries `mov_text`, so the subtitle track transcodes while video and audio copy:

```pgsql
COPY (
  SELECT * FROM input('tests/fixtures/avs.mkv') a
) TO 'film.mp4' WITH (subtitle_codec 'mov_text')
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i tests/fixtures/avs.mkv -map 0:v:0 -c:0 copy -map 0:a:0 -c:1 copy -map 0:s:0 \
  -metadata:s:2 language=eng -c:2 mov_text film.mp4
```

## 3. Extract the audio track to its own file

The SELECT list is the output. Select only the audio and that's the whole file - stream-copied, no generation loss:

```sql
COPY (
  SELECT f.audio[1]
  FROM input(:'source') f
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v source=film.mkv -v dest=soundtrack.m4a
ffmpeg -i film.mkv -map 0:a:0 -c:0 copy soundtrack.m4a
```

## 4. Trim a clip: fast stream copy, or frame-accurate re-encode

`WHERE t BETWEEN` becomes an input seek, and a stream nothing filters stays a copy - instant, but the cut snaps back to the previous keyframe, so it can start a little early:

```sql
COPY (
  SELECT a.video[1], a.audio[1]
  FROM input(:'source') a
  WHERE a.t BETWEEN 5 AND 60
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v source=clip.mp4 -v dest=cut.mp4
ffmpeg -ss 5 -to 60 -i clip.mp4 -map 0:v:0 -c:0 copy -map 0:a:0 -c:1 copy cut.mp4
```

When the exact cut point matters, re-encode: a decoded stream trims frame-accurate. The trade and the measurements behind it are in [docs/trimming.md](trimming.md):

```sql
COPY (
  SELECT a.video[1], a.audio[1]
  FROM input(:'source') a
  WHERE a.t BETWEEN 5 AND 60
) TO :'dest' WITH (video_codec 'libx264', crf 18, audio_codec 'aac')
```

```
$ sqlmpeg compile -f query.sql -v source=clip.mp4 -v dest=cut.mp4
ffmpeg -ss 5 -to 60 -i clip.mp4 -map 0:v:0 -map 0:a:0 -c:0 libx264 -crf:0 18 -c:1 aac \
  cut.mp4
```

## 5. Resize to 1280 wide, or to half size

`-2` for the height means "keep the aspect ratio, rounded to an even number" - encoders insist on even dimensions, and this saves you doing the arithmetic:

```pgsql
COPY (
  SELECT scale(f.video[1], 1280, -2), f.audio[1]
  FROM input(:'source') f
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v source=film.mp4 -v dest=small.mp4
ffmpeg -i film.mp4 -filter_complex '[0:v:0]scale=width=1280:height=-2[out0]' -map \
  '[out0]' -map 0:a:0 -c:1 copy small.mp4
```

Or express the width relative to the input - any string-typed option takes an ffmpeg expression - and let `-2` keep the aspect:

```pgsql
COPY (
  SELECT scale(f.video[1], 'iw/2', -2), f.audio[1]
  FROM input(:'source') f
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v source=film.mp4 -v dest=half.mp4
ffmpeg -i film.mp4 -filter_complex '[0:v:0]scale=width=iw/2:height=-2[out0]' -map \
  '[out0]' -map 0:a:0 -c:1 copy half.mp4
```

## 6. Rotate a phone video 90 degrees

For quarter turns, ffmpeg's `transpose` is the right tool (it swaps the axes rather than resampling). For arbitrary angles there's `rotate`, whose angle is an expression in radians - `rotate(f.video[1], '7*PI/180')` leans a clip seven degrees:

```pgsql
COPY (
  SELECT transpose(v.video[1], dir => 'clock'), v.audio[1]
  FROM input(:'source') v
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v source=phone.mp4 -v dest=upright.mp4
ffmpeg -i phone.mp4 -filter_complex '[0:v:0]transpose=dir=clock[out0]' -map '[out0]' \
  -map 0:a:0 -c:1 copy upright.mp4
```

## 7. Sharpen a soft-looking video

Any of your ffmpeg's filters is callable directly, options by name, checked against what the binary actually supports. (The one-knob version, if you don't need the fine control: `unsharp(f.video[1], 5, 5, 1.5)`, matrix sizes then amount, positionally in unsharp's own order.)

```pgsql
COPY (
  SELECT unsharp(a.video[1], luma_msize_x => 7, luma_amount => 1.5), a.audio[1]
  FROM input(:'source') a
) TO 'out.mp4'
```

```
$ sqlmpeg compile -f query.sql -v source=clip.mp4
ffmpeg -i clip.mp4 -filter_complex '[0:v:0]unsharp=luma_msize_x=7:luma_amount=1.5[out0]' \
  -map '[out0]' -map 0:a:0 -c:1 copy out.mp4
```

## 8. Concatenate two clips

`UNION ALL` is ffmpeg's concat. SQL requires the branches to agree on column count, type and order, and that is exactly concat's segment contract - the interleaving that's so easy to get wrong by hand is generated for you:

```sql
COPY (
  SELECT a.video[1], a.audio[1] FROM input(:'first') a
  UNION ALL
  SELECT b.video[1], b.audio[1] FROM input(:'second') b
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v first=part1.mp4 -v second=part2.mp4 -v dest=joined.mp4
ffmpeg -i part1.mp4 -i part2.mp4 -filter_complex \
  '[0:v:0][0:a:0][1:v:0][1:a:0]concat=n=2:v=1:a=1[out0][out1]' -map '[out0]' -map \
  '[out1]' joined.mp4
```

And it scales to files you'd rather not count streams in: splat the whole audio array and the languages pair up positionally, English with English, French with French, tags surviving. (This one needs real files - a splat has to know how many tracks there are.)

```pgsql
COPY (
  SELECT a.video[1], a.audio FROM input('tests/fixtures/av2.mp4') a
  UNION ALL
  SELECT b.video[1], b.audio FROM input('tests/fixtures/av3.mp4') b
) TO 'season.mkv'
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i tests/fixtures/av2.mp4 -i tests/fixtures/av3.mp4 -filter_complex \
  '[0:v:0][0:a:0][0:a:1][1:v:0][1:a:0][1:a:1]concat=n=2:v=1:a=2[out0][out1][out2]' -map \
  '[out0]' -map '[out1]' -metadata:s:1 language=eng -map '[out2]' -metadata:s:2 \
  language=fra season.mkv
```

## 9. Watermark a video

`loop => true` keeps a still image alive for the whole duration, and the position is an ffmpeg expression - `(W-w)/2` centers it without you knowing either file's dimensions:

```pgsql
COPY (
  SELECT overlay(f.video[1], logo.video[1], '(W-w)/2', '(H-h)/2'), f.audio[1]
  FROM input(:'main') f, input(:'overlay', loop => true) logo
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v main=film.mp4 -v overlay=watermark.png -v dest=branded.mp4
ffmpeg -i film.mp4 -loop 1 -i watermark.png -filter_complex \
  '[0:v:0][1:v:0]overlay=x=(W-w)/2:y=(H-h)/2[out0]' -map '[out0]' -map 0:a:0 -c:1 copy \
  branded.mp4
```

## 10. Mux external subtitles in, or pull them back out

A subtitle file is just another input. Select its track next to your video and audio; mp4 demands `mov_text`, mkv would take `srt` or `webvtt`:

```sql
COPY (
  SELECT f.video[1], f.audio[1], s.subtitle[1]
  FROM input(:'main') f, input(:'subs') s
) TO :'dest' WITH (subtitle_codec 'mov_text')
```

```
$ sqlmpeg compile -f query.sql -v main=film.mp4 -v subs=subs.en.vtt -v dest=captioned.mp4
ffmpeg -i film.mp4 -i subs.en.vtt -map 0:v:0 -c:0 copy -map 0:a:0 -c:1 copy -map 1:s:0 \
  -c:2 mov_text captioned.mp4
```

Extraction is the same idea with a shorter SELECT list - the container implies the format:

```sql
COPY (
  SELECT f.subtitle[1]
  FROM input(:'source') f
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v source=film.mkv -v dest=subs.en.srt
ffmpeg -i film.mkv -map 0:s:0 -c:0 copy subs.en.srt
```

## 11. Burn subtitles into the picture

Different from muxing a track: `subtitles()` is a video filter that renders the cues into the pixels. The subtitle file is read when ffmpeg runs, so it needs to exist then, not now:

```pgsql
COPY (
  SELECT subtitles(f.video[1], 'subs.en.srt'), f.audio[1]
  FROM input(:'source') f
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v source=film.mp4 -v dest=burned.mp4
ffmpeg -i film.mp4 -filter_complex '[0:v:0]subtitles=filename=subs.en.srt[out0]' -map \
  '[out0]' -map 0:a:0 -c:1 copy burned.mp4
```

## 12. Speed a clip up 2x, picture and sound together

Two functions because the two stream types speed up differently: `sqlmpeg.speed` restamps video frames, `atempo` resamples audio while keeping the pitch (so nobody turns into a chipmunk):

```pgsql
COPY (
  SELECT sqlmpeg.speed(f.video[1], :factor), atempo(f.audio[1], :factor)
  FROM input(:'source') f
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v source=film.mp4 -v factor=2 -v dest=fast.mp4
ffmpeg -i film.mp4 -filter_complex \
  '[0:v:0]setpts=PTS/2[out0];[0:a:0]atempo=tempo=2[out1]' -map '[out0]' -map '[out1]' \
  fast.mp4
```

## 13. Crossfade between two clips

`xfade` takes both clips, then `duration` and `offset` by name (its first option is the transition style, which defaults to a plain dissolve) - the offset is seconds into the FIRST clip where the fade begins, so a 10-second clip with a 1-second fade starts dissolving at 9. `acrossfade` does the same for the sound:

```pgsql
COPY (
  SELECT xfade(a.video[1], b.video[1], duration => 1, offset => 9),
         acrossfade(a.audio[1], b.audio[1], duration => 1)
  FROM input(:'first') a, input(:'second') b
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v first=one.mp4 -v second=two.mp4 -v dest=dissolve.mp4
ffmpeg -i one.mp4 -i two.mp4 -filter_complex \
  '[0:v:0][1:v:0]xfade=duration=1:offset=9[out0];'\
'[0:a:0][1:a:0]acrossfade=duration=1[out1]' -map '[out0]' -map '[out1]' dissolve.mp4
```

## 14. Turn a clip into a GIF

The good-looking way needs two passes over the frames - one to build a palette, one to use it. Write it as a CTE consumed twice; the compiler inserts the split:

```pgsql
COPY (
  WITH small AS (
    SELECT fps(scale(v.video[1], 480, -2), 12) AS frame
    FROM input(:'source') v
  )
  SELECT paletteuse(small.frame, palettegen(small.frame))
  FROM small
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v source=clip.mp4 -v dest=clip.gif
ffmpeg -i clip.mp4 -filter_complex \
  '[0:v:0]scale=width=480:height=-2,fps=fps=12,split=2[n2_split0][n2_split1];'\
'[n2_split0]palettegen[n3];[n2_split1][n3]paletteuse[out0]' -map '[out0]' clip.gif
```

## 15. Replace a video's audio, or duck music under the dialogue

Swapping is just selecting video from one input and audio from another:

```sql
COPY (
  SELECT v.video[1], m.audio[1]
  FROM input(:'main') v, input(:'voice') m
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v main=film.mp4 -v voice=voiceover.wav -v dest=dubbed.mp4
ffmpeg -i film.mp4 -i voiceover.wav -map 0:v:0 -c:0 copy -map 1:a:0 -c:1 copy dubbed.mp4
```

Keeping both, with the music turned down, is a mix:

```pgsql
COPY (
  SELECT v.video[1], amix(v.audio[1], volume(m.audio[1], 0.2))
  FROM input(:'main') v, input(:'music') m
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v main=film.mp4 -v music=music.m4a -v dest=scored.mp4
ffmpeg -i film.mp4 -i music.m4a -filter_complex \
  '[1:a:0]volume=volume=0.2[n1];[0:a:0][n1]amix=inputs=2[out1]' -map 0:v:0 -c:0 copy \
  -map '[out1]' scored.mp4
```

Real ducking - music that dips when someone speaks - is a sidechain compressor keyed off the dialogue. Naming `v.audio[1]` twice is fine; the compiler inserts the split:

```pgsql
COPY (
  SELECT v.video[1], amix(v.audio[1], sidechaincompress(m.audio[1], v.audio[1], threshold => 0.03, ratio => 8))
  FROM input(:'main') v, input(:'music') m
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v main=film.mp4 -v music=music.m4a -v dest=ducked.mp4
ffmpeg -i film.mp4 -i music.m4a -filter_complex \
  '[0:a:0]asplit=2[src_v_a_0_split0][src_v_a_0_split1];'\
'[1:a:0][src_v_a_0_split0]sidechaincompress=threshold=0.03:ratio=8[n1];'\
'[src_v_a_0_split1][n1]amix=inputs=2[out1]' -map 0:v:0 -c:0 copy -map '[out1]' \
  ducked.mp4
```

## 16. Picture-in-picture

A quarter-size camera in the bottom-right corner, 20 pixels off each edge - the expressions mean the position holds whatever the two resolutions are. (The dual-language version, with the audio mixed per language, is the README's opening demo.)

```pgsql
COPY (
  SELECT overlay(f.video[1], scale(c.video[1], 'iw/4', -2), 'W-w-20', 'H-h-20'), f.audio[1]
  FROM input(:'main') f, input(:'overlay') c
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v main=film.mp4 -v overlay=camera.mp4 -v dest=pip.mp4
ffmpeg -i film.mp4 -i camera.mp4 -filter_complex \
  '[1:v:0]scale=width=iw/4:height=-2[n1];[0:v:0][n1]overlay=x=W-w-20:y=H-h-20[out0]' \
  -map '[out0]' -map 0:a:0 -c:1 copy pip.mp4
```

## 17. Insert a clip at a timestamp

The splice: cut away to the insert, then resume. The same file appears under two aliases with two windows, and the tail's `>= 120` means "to the end" with no made-up end time:

```sql
COPY (
  SELECT f.video[1], f.audio[1] FROM input(:'main') f WHERE f.t <= :cut
  UNION ALL
  SELECT ad.video[1], ad.audio[1] FROM input(:'insert') ad
  UNION ALL
  SELECT g.video[1], g.audio[1] FROM input(:'main') g WHERE g.t >= :cut
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v main=film.mp4 -v insert=promo.mp4 -v cut=120 -v dest=spliced.mp4
ffmpeg -to 120 -i film.mp4 -i promo.mp4 -ss 120 -i film.mp4 -filter_complex \
  '[0:v:0][0:a:0][1:v:0][1:a:0][2:v:0][2:a:0]concat=n=3:v=1:a=1[out0][out1]' -map \
  '[out0]' -map '[out1]' spliced.mp4
```

Or keep the main video playing and overlay the insert on top: a delayed video stream is transparent until its start time (and after it ends), so it composes with a plain `overlay` - no timeline bookkeeping:

```pgsql
COPY (
  SELECT overlay(f.video[1], sqlmpeg.delay(promo.video[1], 120), 20, 20), f.audio[1]
  FROM input(:'main') f, input(:'insert') promo
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v main=film.mp4 -v insert=promo.mp4 -v dest=overlaid.mp4
ffmpeg -i film.mp4 -i promo.mp4 -filter_complex \
  '[1:v:0]format=pix_fmts=yuva420p,tpad=start_duration=120:stop=1:color=black@0[n2];'\
'[0:v:0][n2]overlay=x=20:y=20[out0]' -map '[out0]' -map 0:a:0 -c:1 copy overlaid.mp4
```

## 18. Normalize loudness on every language track at once

A bare `.audio` is the whole track array; handing it to a filter broadcasts, one node per language, and every output keeps its language tag. (`ffmpeg.loudnorm` rather than bare `loudnorm` only out of habit here - the bare name works too; the namespace is the spelling that never collides with Postgres grammar. `I` is EBU R128 integrated loudness, and yes, it's a capital I.)

```pgsql
COPY (
  SELECT f.video[1], ffmpeg.loudnorm(f.audio, I => -23)
  FROM input('tests/fixtures/av2.mp4') f
) TO 'broadcast.mkv'
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i tests/fixtures/av2.mp4 -filter_complex \
  '[0:a:0]loudnorm=I=-23[out1];[0:a:1]loudnorm=I=-23[out2]' -map 0:v:0 -c:0 copy -map \
  '[out1]' -metadata:s:1 language=eng -map '[out2]' -metadata:s:2 language=fra \
  broadcast.mkv
```

## 19. Blur a region, or blur during a time window

`sqlmpeg.blur_regions` is crop, blur and overlay in one call - the license-plate special:

```pgsql
COPY (
  SELECT sqlmpeg.blur_regions(f.video[1], 900, 60, 320, 180, 20), f.audio[1]
  FROM input(:'source') f
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v source=interview.mp4 -v dest=anonymized.mp4
ffmpeg -i interview.mp4 -filter_complex \
  '[0:v:0]split=2[src_f_v_0_split0][src_f_v_0_split1];'\
'[src_f_v_0_split0]crop=out_w=320:out_h=180:x=900:y=60,gblur=sigma=20[n2];'\
'[src_f_v_0_split1][n2]overlay=x=900:y=60[out0]' -map '[out0]' -map 0:a:0 -c:1 copy \
  anonymized.mp4
```

To apply an effect only during a time window, `enable` is the switch - no trimming, no branches, no concat, just a filter that turns itself on and off:

```pgsql
COPY (
  SELECT gblur(a.video[1], 12, enable => 'between(t,0.5,1.5)')
  FROM input(:'source') a
) TO 'out.mp4'
```

```
$ sqlmpeg compile -f query.sql -v source=clip.mp4
ffmpeg -i clip.mp4 -filter_complex \
  '[0:v:0]gblur=sigma=12:enable=between(t\,0.5\,1.5)[out0]' -map '[out0]' out.mp4
```

## 20. Generate test media

Sources live in FROM and consume no input file at all - note the command below has no `-i`:

```pgsql
COPY (
  SELECT t.video[1], s.audio[1]
  FROM ffmpeg.testsrc2(duration => 10, size => '1280x720', rate => 30) t,
       ffmpeg.sine(frequency => 440, duration => 10) s
) TO 'bars.mp4'
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -filter_complex \
  'testsrc2=duration=10:size=1280x720:rate=30[out0];'\
'sine=frequency=440:duration=10[out1]' -map '[out0]' -map '[out1]' bars.mp4
```

They also solve a quieter problem: `UNION ALL` branches must match column for column, so appending a slate to a clip needs a silent audio track from somewhere. `anullsrc` is that somewhere:

```pgsql
COPY (
  SELECT f.video[1], f.audio[1] FROM input(:'source') f
  UNION ALL
  SELECT t.video[1], s.audio[1]
  FROM ffmpeg.color(color => 'black', duration => 3, size => '1280x720', rate => 30) t,
       ffmpeg.anullsrc(duration => 3) s
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v source=clip.mp4 -v dest=with-slate.mp4
ffmpeg -i clip.mp4 -filter_complex \
  'color=color=black:duration=3:size=1280x720:rate=30[n1];anullsrc=duration=3[n2];'\
'[0:v:0][0:a:0][n1][n2]concat=n=2:v=1:a=1[out0][out1]' -map '[out0]' -map '[out1]' \
  with-slate.mp4
```

## 21. Split a stereo track, or compress it in bands

A few filters return a whole array, sized by one of their own options. `channelsplit` turns one stereo track into two mono streams; splatted into the SELECT list, each becomes its own output:

```pgsql
COPY (
  SELECT ffmpeg.channelsplit(a.audio[1])
  FROM input(:'source') a
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v source=stereo.mp4 -v dest=channels.mkv
ffmpeg -i stereo.mp4 -filter_complex '[0:a:0]channelsplit[out0][out1]' -map '[out0]' \
  -map '[out1]' channels.mkv
```

`acrossover` splits by frequency instead - two split points make three bands - and that's the shape of multiband compression: split, compress each band on its own settings, mix back:

```pgsql
COPY (
  WITH bands AS (
    SELECT ffmpeg.acrossover(a.audio[1], split => '300 3000') AS b
    FROM input(:'source') a
  )
  SELECT amix(amix(acompressor(bands.b[1], threshold => 0.1, ratio => 4),
                   acompressor(bands.b[2], threshold => 0.05, ratio => 6)),
              acompressor(bands.b[3], threshold => 0.1, ratio => 4))
  FROM bands
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v source=song.m4a -v dest=mastered.m4a
ffmpeg -i song.m4a -filter_complex \
  '[0:a:0]acrossover=split=300\ 3000[n10][n11][n12];'\
'[n10]acompressor=threshold=0.1:ratio=4[n2];'\
'[n11]acompressor=threshold=0.05:ratio=6[n3];[n2][n3]amix=inputs=2[n4];'\
'[n12]acompressor=threshold=0.1:ratio=4[n5];[n4][n5]amix=inputs=2[out0]' -map '[out0]' \
  mastered.m4a
```

## 22. One decode, several outputs

A `CREATE VIEW` is a named, shared piece of the graph, and each `COPY` after it is one output file - the whole script is a single ffmpeg run, so the watermarking happens once no matter how many files consume it. (The classic version of this, the ABR rendition ladder, is in the README.)

```pgsql
CREATE VIEW branded AS
  SELECT overlay(f.video[1], logo.video[1], 'W-w-20', 20) AS v, f.audio[1] AS a
  FROM input(:'main') f, input(:'overlay', loop => true) logo;

COPY (SELECT scale(b.v, 1280, -2) AS v, b.a FROM branded b)
TO :'web' WITH (video_codec 'libx264', crf 21, audio_codec 'aac');

COPY (SELECT b.a FROM branded b)
TO :'podcast' WITH (audio_codec 'aac', audio_bitrate '128k')
```

```
$ sqlmpeg compile -f query.sql -v main=film.mp4 -v overlay=watermark.png -v web=web.mp4 -v podcast=podcast.m4a
ffmpeg -i film.mp4 -loop 1 -i watermark.png -filter_complex \
  '[0:v:0][1:v:0]overlay=x=W-w-20:y=20,scale=width=1280:height=-2[out0]' -map '[out0]' \
  -map 0:a:0 -c:0 libx264 -crf:0 21 -c:1 aac web.mp4 -map 0:a:0 -c:0 aac -b:0 128k \
  podcast.m4a
```

## 23. Pick a track by what it is, not where it sits

`unnest` turns a track array into rows - one per track, with the probed metadata as real columns - and a `WHERE` over those columns is track selection that says what you mean. The row IS the track: a bare `t` where a stream is expected selects it, filters it, or gathers it, and the columns are the metadata about it. No more counting streams in ffprobe output to learn that English is `[2]` this time:

```pgsql
COPY (
  SELECT t
  FROM input('tests/fixtures/av2.mp4') f, unnest(f.audio) t
  WHERE t.tags.language = 'eng'
) TO 'eng.m4a'
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i tests/fixtures/av2.mp4 -map 0:a:0 -c:0 copy -metadata:s:0 language=eng eng.m4a
```

Audio rows carry `tags` (read by path: `t.tags.language`, `t.tags.title`, any key), `codec`, `channels`, `channel_layout`, `sample_rate`, `bitrate` and `duration`; video rows carry `width`, `height`, `fps` and friends instead. A track nobody probed has NULL in every metadata column, and NULL matches nothing - standard SQL, no new rules.

## 24. Extract captions by language

Caption arrays unnest the same way (columns: `tags`, `codec`), so pulling the English subtitles out of a many-language file is a `WHERE`, not a subscript:

```pgsql
COPY (
  SELECT s
  FROM input('tests/fixtures/avs.mkv') f, unnest(f.subtitle) s
  WHERE s.tags.language = 'eng'
) TO 'subs.srt'
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i tests/fixtures/avs.mkv -map 0:s:0 -c:0 copy -metadata:s:0 language=eng \
  subs.srt
```

## 25. Mix two files' tracks pairwise, matched by language

Two multi-language files, and every track should mix with its counterpart - English with English, French with French, whatever order each file stores them in. That is a JOIN, written exactly the way Postgres writes it, evaluated entirely at compile time (the metadata is probed, so ffmpeg only ever sees the wiring the join decided). The join leaves one row per pair, and `array_agg` is what puts all those pairs in one file:

```pgsql
COPY (
  SELECT array_agg(amix(a, b))
  FROM input('tests/fixtures/av2.mp4') f, input('tests/fixtures/av3.mp4') g,
       unnest(f.audio) a JOIN unnest(g.audio) b ON a.tags.language = b.tags.language
) TO 'mixed.mka'
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i tests/fixtures/av2.mp4 -i tests/fixtures/av3.mp4 -filter_complex \
  '[0:a:0][1:a:0]amix=inputs=2[out0];[0:a:1][1:a:1]amix=inputs=2[out1]' -map '[out0]' \
  -metadata:s:0 language=eng -map '[out1]' -metadata:s:1 language=fra mixed.mka
```

Result rows follow the LEFT side's track order, so the output track order is `f`'s - track order is player-visible surface, and nothing here resorts it. And when one file carries two English tracks (a 5.1 and a stereo, say), that's not an error, it's two pairs - real join semantics - and the fix is a wider key: `ON a.tags.language = b.tags.language AND a.channel_layout = b.channel_layout`.

## 26. Mix everything the files have, missing tracks count as silence

An outer join keeps the rows only one side has, and `COALESCE` fills the gap - for audio, with generated silence:

```pgsql
COPY (
  SELECT array_agg(amix(a, COALESCE(b, ffmpeg.anullsrc(duration => 4))))
  FROM input('tests/fixtures/av2.mp4') f, input('tests/fixtures/av-eng.mp4') g,
       unnest(f.audio) a FULL OUTER JOIN unnest(g.audio) b ON a.tags.language = b.tags.language
) TO 'full.mka'
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i tests/fixtures/av2.mp4 -i tests/fixtures/av-eng.mp4 -filter_complex \
  'anullsrc=duration=4[n1];[0:a:0][1:a:0]amix=inputs=2[out0];'\
'[0:a:1][n1]amix=inputs=2[out1]' -map '[out0]' -metadata:s:0 language=eng -map '[out1]' \
  -metadata:s:1 language=fra full.mka
```

The second file has no French, so the French mix gets silence in that slot - and keeps its `fra` tag, because the tag came from the side that existed.

## 27. Concatenate files with different track counts

The founding case. `concat` demands identical segment shapes, so the file that lacks a French track needs a silent stand-in - which is the same outer join, once per branch, each branch selecting its own side and gathering its rows into that segment. (Aliases respell in the second branch because alias names are script-wide.)

```pgsql
COPY (
  SELECT f.video[1], array_agg(a)
  FROM input('tests/fixtures/av2.mp4') f, input('tests/fixtures/av-eng.mp4') g,
       unnest(f.audio) a FULL OUTER JOIN unnest(g.audio) b ON a.tags.language = b.tags.language
  GROUP BY f.video[1]
  UNION ALL
  SELECT g2.video[1], array_agg(COALESCE(b2, ffmpeg.anullsrc(duration => 4)))
  FROM input('tests/fixtures/av2.mp4') f2, input('tests/fixtures/av-eng.mp4') g2,
       unnest(f2.audio) a2 FULL OUTER JOIN unnest(g2.audio) b2 ON a2.tags.language = b2.tags.language
  GROUP BY g2.video[1]
) TO 'joined.mp4'
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i tests/fixtures/av2.mp4 -i tests/fixtures/av-eng.mp4 -filter_complex \
  'anullsrc=duration=4[n1];'\
'[0:v:0][0:a:0][0:a:1][1:v:0][1:a:0][n1]concat=n=2:v=1:a=2[out0][out1][out2]' -map \
  '[out0]' -map '[out1]' -metadata:s:1 language=eng -map '[out2]' -metadata:s:2 \
  language=fra joined.mp4
```

Both branches share one join shape, so both agree on track order, and eng concatenates with eng. Each file appears in two branches but gets ONE `-i`: untrimmed aliases over the same path share an input.

## 28. Side by side, matched by resolution

Video arrays unnest too - `width`, `height`, `fps`, `codec`, `bitrate` are the columns - so pairing renditions for a comparison strip is a join on the numbers that matter:

```pgsql
COPY (
  SELECT hstack(a, b)
  FROM input('tests/fixtures/testsrc.mp4') f, input('tests/fixtures/smptebars.mp4') g,
       unnest(f.video) a JOIN unnest(g.video) b
         ON a.width = b.width AND a.height = b.height
) TO 'sxs.mp4'
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i tests/fixtures/testsrc.mp4 -i tests/fixtures/smptebars.mp4 -filter_complex \
  '[0:v:0][1:v:0]hstack=inputs=2[out0]' -map '[out0]' sxs.mp4
```

A video gap in an outer join fills with `COALESCE(b, ffmpeg.color())` - black by default, size, rate and duration inherited from the paired row. A caption gap fills with `COALESCE(b, sqlmpeg.empty_captions())`: the track exists and takes its language tag, it just contains zero cues - nobody generates your subtitles for you.

## 29. Assert what you're shipping

A subscripted track has the same metadata columns a row does: `f.audio[1].tags.language` is the first track's tag, right there in a `WHERE`. Since the predicate evaluates at compile time, this is an assertion - if track 1 isn't English, the script refuses to compile instead of quietly shipping the wrong language. (The strictly-Postgres spelling `(f.audio[1]).tags.language` works too.)

```pgsql
COPY (
  SELECT f.audio[1] FROM input('tests/fixtures/av2.mp4') f
  WHERE f.audio[1].tags.language = 'eng'
) TO 'eng.m4a'
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i tests/fixtures/av2.mp4 -map 0:a:0 -c:0 copy -metadata:s:0 language=eng eng.m4a
```

Recipe 23 answers "give me whichever track is English"; this one answers "I believe track 1 is English - stop me if I'm wrong". Same wiring out the other end, different contract.

## 30. Look at a file's tracks as a table

A SELECT with no COPY is a table query: `run` (the default subcommand, so no subcommand at all) prints the result set and executes nothing - the whole answer was known at compile time. The columns are the probed metadata, so this is ffprobe you can read:

```pgsql
SELECT t.index, t.tags.language, t.codec, t.channel_layout
FROM input('tests/fixtures/av2.mp4') f, unnest(f.audio) t
```

```
$ sqlmpeg -f query.sql
 index | language | codec | channel_layout
-------+----------+-------+----------------
 1     | eng      | aac   | mono
 2     | fra      | aac   | mono
(2 rows)
```

`SELECT t.*` prints the row's whole scalar shape instead - `index`, `codec`, and whatever else that stream type carries. The map columns stay out of the star: one `disposition` cell is every flag ffmpeg knows. Name them - `t.tags.language`, `t.disposition.forced` - to print them. `SELECT *` over the input alias `f` prints its array columns, one cell each: `video`, `audio`, `subtitle`, `data`, `chapters`.

## 31. Inspect a join before you trust it

Stream-valued cells print as placeholders carrying the stream spec, so a table query over a join shows exactly which track paired with which - and an empty cell is an outer join's gap, before you've committed to a fill:

```pgsql
SELECT a.tags.language, a AS film, b AS promo
FROM input('tests/fixtures/av2.mp4') f, input('tests/fixtures/av-eng.mp4') g,
     unnest(f.audio) a FULL OUTER JOIN unnest(g.audio) b ON a.tags.language = b.tags.language
```

```
$ sqlmpeg -f query.sql
 language | film          | promo
----------+---------------+---------------
 eng      | <audio 0:a:0> | <audio 1:a:0>
 fra      | <audio 0:a:1> |
(2 rows)
```

## 32. Export track metadata as CSV

`COPY ... TO STDOUT WITH (FORMAT csv)` is stock Postgres, and here it makes the table query scriptable - pipe it wherever your inventory lives. `TO '<path>.csv'` writes a file instead; `header true` adds the column row:

```pgsql
COPY (
  SELECT t.tags.language, t.codec, t.channel_layout
  FROM input('tests/fixtures/av2.mp4') f, unnest(f.audio) t
) TO STDOUT WITH (format 'csv', header true)
```

```
$ sqlmpeg -f query.sql
language,codec,channel_layout
eng,aac,mono
fra,aac,mono
```

## 33. One query, many files

A query file with variables is a program. `-v name=value` is psql's own flag and `:'name'` is psql's own interpolation - the value lands as a properly escaped string literal (bare `:name` substitutes raw, for numbers), and an undefined variable is a compile-time error, not a surprise:

```sql
COPY (SELECT f.video[1], f.audio[1] FROM input(:'source') f)
TO :'dest' WITH (video_codec 'libx264', crf 20, audio_codec 'aac')
```

```
$ sqlmpeg compile -f query.sql -v source=film.mkv -v dest=out.mkv
ffmpeg -i film.mkv -map 0:v:0 -map 0:a:0 -c:0 libx264 -crf:0 20 -c:1 aac out.mkv
```

Swap the `-v` values and the same file transcodes anything. The [queries/](../queries/) directory collects ready-to-run programs built this way.

## 34. Grab a poster frame

`frames 1` stops the output after one frame, and `video_codec 'png'` forces the decode a PNG needs (an unfiltered stream would otherwise try to stream-copy). The seek puts the frame where you want it:

```pgsql
COPY (
  SELECT f.video[1] FROM input(:'source') f WHERE f.t >= :at
) TO :'dest' WITH (video_codec 'png', frames 1)
```

```
$ sqlmpeg compile -f query.sql -v source=film.mkv -v at=90 -v dest=poster.png
ffmpeg -ss 90 -i film.mkv -map 0:v:0 -c:0 png -frames:0 1 poster.png
```

## 35. Hit a delivery spec

Device and platform specs name a profile, a level, and a rate-control ceiling; they map straight onto sink options. `-t`-style output limiting rides along as `duration`:

```pgsql
COPY (
  SELECT f.video[1], f.audio[1] FROM input(:'source') f
) TO :'dest' WITH (
  video_codec 'libx264', profile 'baseline', level '3.1',
  maxrate '2675k', bufsize '5350k', audio_codec 'aac', duration 30
)
```

```
$ sqlmpeg compile -f query.sql -v source=in.mkv -v dest=out.mp4
ffmpeg -i in.mkv -map 0:v:0 -map 0:a:0 -c:0 libx264 -profile:0 baseline -level:0 3.1 \
  -maxrate:0 2675k -bufsize:0 5350k -c:1 aac -t 30 out.mp4
```

## 36. Keep the last minute

`seek_end` seeks from the END of the file - no need to know its length. Stream copy applies, keyframe snapping included, same as any input seek:

```pgsql
COPY (
  SELECT a.video[1], a.audio[1]
  FROM input(:'source', seek_end => 60) a
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v source=clip.mp4 -v dest=tail.mp4
ffmpeg -sseof -60 -i clip.mp4 -map 0:v:0 -c:0 copy -map 0:a:0 -c:1 copy tail.mp4
```

## 37. Retitle tracks from their own metadata

A non-stream column in a media query sets a tag on that row's output. The alias names the tag, the value is any compile-time expression over the row - here a title built from the language tag with `||`:

```pgsql
COPY (
  SELECT t, 'Audio (' || t.tags.language || ')' AS title
  FROM input('tests/fixtures/av-eng.mp4') f, unnest(f.audio) t
) TO 'titled.mka'
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i tests/fixtures/av-eng.mp4 -map 0:a:0 -c:0 copy -metadata:s:0 language=eng \
  -metadata:s:0 'title=Audio (eng)' titled.mka
```

## 38. Normalize language tags

CASE makes the edit conditional, and it runs over every row - one expression fixes the whole file. Tags you don't select pass through unchanged; `NULL` as the value clears one. Rows are tracks inside the `WITH`, which is where a tag column can name one; the outer `array_agg` puts the tagged tracks in the file:

```pgsql
COPY (
  WITH retagged AS (
    SELECT t AS track,
           CASE WHEN t.tags.language = 'fra' THEN 'fre' ELSE t.tags.language END AS language
    FROM input('tests/fixtures/av2.mp4') f, unnest(f.audio) t
  )
  SELECT array_agg(retagged.track) FROM retagged
) TO 'retagged.mka'
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i tests/fixtures/av2.mp4 -map 0:a:0 -c:0 copy -metadata:s:0 language=eng -map \
  0:a:1 -c:1 copy -metadata:s:1 language=fre retagged.mka
```

## 39. List a file's chapters

`chapters` is an array column of the input, like the stream arrays: unnest it and each chapter is a row, straight from the container. Like every metadata query, no COPY means it prints and nothing runs:

```pgsql
SELECT c.index, c.title, c.start_t, c.end_t
FROM input('tests/fixtures/av-chapters.mkv') f, unnest(f.chapters) c
```

```
$ sqlmpeg -f query.sql
 index | title     | start_t | end_t
-------+-----------+---------+-------
 1     | Intro     | 0.0     | 1.0
 2     | Chapter 1 | 1.0     | 2.0
 3     | Chapter 2 | 2.0     | 3.0
 4     | Credits   | 3.0     | 4.0
(4 rows)
```

## 40. Write chapters

A `chapters` column IS the file's chapter list, the same shape `unnest(f.chapters)` reads. Build it from `chapter` records; it compiles to one extra self-contained input - no file on disk:

```pgsql
COPY (
  SELECT f.video[1], f.audio[1],
         ARRAY[ROW('Intro', 0, 60)::chapter,
               ROW('Act One', 60, 300)::chapter] AS chapters
  FROM input(:'source') f
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v source=film.mkv -v dest=chaptered.mkv
ffmpeg -i film.mkv -f ffmetadata -i \
  'data:text/plain;'\
'base64,'\
'O0ZGTUVUQURBVEExCltDSEFQVEVSXQpUSU1FQkFTRT0xLzEKU1RBUlQ9MApFTkQ9NjAKdGl0bGU9SW50cm8KW0NIQVBURVJdClRJTUVCQVNFPTEvMQpTVEFSVD02MApFTkQ9MzAwCnRpdGxlPUFjdCBPbmUK' \
  -map 0:v:0 -c:0 copy -map 0:a:0 -c:1 copy -map_chapters 1 chaptered.mkv
```

## 41. Flag the default track

`disposition` is a field of the row, not a tag: its value is ffmpeg's disposition spec ('default', 'forced', 'default+forced'; '0' clears), and it says what the track's whole flag map is. Read it back by path, `t.disposition.default`. Players open the default track first, so this decides what people hear. Same two levels as recipe 38: flag the rows in the `WITH`, gather them outside it:

```pgsql
COPY (
  WITH flagged AS (
    SELECT t AS track,
           CASE WHEN t.tags.language = 'eng' THEN 'default' ELSE '0' END AS disposition
    FROM input('tests/fixtures/av2.mp4') f, unnest(f.audio) t
  )
  SELECT array_agg(flagged.track) FROM flagged
) TO 'flagged.mka'
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i tests/fixtures/av2.mp4 -map 0:a:0 -c:0 copy -metadata:s:0 language=eng \
  -disposition:0 default -map 0:a:1 -c:1 copy -metadata:s:1 language=fra -disposition:1 \
  0 flagged.mka
```

Reading the flags back is the same path form the tags take, one key at a time, and each one is a boolean:

```pgsql
SELECT t.index, t.tags.language, t.disposition.default, t.disposition.forced
FROM input('tests/fixtures/av2.mp4') f, unnest(f.audio) t
```

```
$ sqlmpeg -f query.sql
 index | language | default | forced
-------+----------+---------+--------
 1     | eng      | true    | false
 2     | fra      | false   | false
(2 rows)
```

## 42. Title the file and keep its global tags

In a query without track rows an aliased non-stream column tags the container (not a stream); `metadata_from` copies an input's global tags through, and the tag column overrides its key:

```pgsql
COPY (
  SELECT f.video[1], f.audio[1], 'Director Cut' AS title
  FROM input(:'source') f
) TO :'dest' WITH (metadata_from f)
```

```
$ sqlmpeg compile -f query.sql -v source=film.mkv -v dest=cut.mkv
ffmpeg -i film.mkv -map 0:v:0 -c:0 copy -map 0:a:0 -c:1 copy -metadata \
  'title=Director Cut' -map_metadata 0 cut.mkv
```

## 43. Two-pass encode to a target bitrate

`two_pass true` compiles to TWO chained ffmpeg commands: pass 1 encodes video only into ffmpeg's stats file and discards the output, pass 2 reads the stats and writes the file. `run` executes both in order; requires `video_bitrate` (two-pass exists to hit a bitrate):

```pgsql
COPY (SELECT f.video[1], f.audio[1] FROM input(:'source') f)
TO :'dest' WITH (video_codec 'libx264', video_bitrate '2500k', two_pass true, audio_codec 'aac')
```

```
$ sqlmpeg compile -f query.sql -v source=in.mkv -v dest=out.mp4
ffmpeg -i in.mkv -map 0:v:0 -c:0 libx264 -b:0 2500k -pass 1 -passlogfile out.mp4 -f null \
  - && ffmpeg -i in.mkv -map 0:v:0 -map 0:a:0 -c:0 libx264 -b:0 2500k -pass 2 \
  -passlogfile out.mp4 -c:1 aac out.mp4
```

## 44. Merge two audio tracks into one

`amerge` combines tracks into a single multichannel stream (unlike `amix`, which sums them):

```pgsql
COPY (
  SELECT amerge(a.audio[1], b.audio[1])
  FROM input(:'first') a, input(:'second') b
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v first=one.mp4 -v second=two.mp4 -v dest=merged.mka
ffmpeg -i one.mp4 -i two.mp4 -filter_complex '[0:a:0][1:a:0]amerge=inputs=2[out0]' -map \
  '[out0]' merged.mka
```

## 45. Scale each track relative to itself

A filter argument over a row table's columns is computed per row, at compile time - each rendition scaled against its own probed width:

```pgsql
COPY (
  SELECT scale(t, t.width / 2, -2)
  FROM input('tests/fixtures/av2.mp4') f, unnest(f.video) t
) TO 'half.mp4'
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i tests/fixtures/av2.mp4 -filter_complex \
  '[0:v:0]scale=width=160:height=-2[out0]' -map '[out0]' half.mp4
```

## 46. Keep everything but the end

`f.duration` is the probed container duration, and trim bounds take arithmetic - so "all but the last half second" needs no known length:

```pgsql
COPY (
  SELECT f.video[1], f.audio[1]
  FROM input('tests/fixtures/av2.mp4') f
  WHERE f.t <= f.duration - 0.5
) TO 'trimmed.mp4'
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -to 3.5 -i tests/fixtures/av2.mp4 -map 0:v:0 -c:0 copy -map 0:a:0 -c:1 copy \
  -metadata:s:1 language=eng trimmed.mp4
```

## 47. Split a file by its chapters

A `TO` expression over a row table's columns means one output file per row - the chapters drive the seeks and the filenames. Two ways to cut, and the trade matters:

**Stream copy** - fastest, nothing decodes, but each cut snaps back to the previous keyframe, so pieces can start early. Copied trims need one ffmpeg command per piece, chained with `&&`:

```pgsql
COPY (
  SELECT f.video, f.audio
  FROM input('tests/fixtures/av-chapters.mkv') f, unnest(f.chapters) c
  WHERE f.t BETWEEN c.start_t AND c.end_t
) TO ('ch-' || c.title || '.mkv')
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -ss 0.0 -to 1.0 -i tests/fixtures/av-chapters.mkv -map 0:v:0 -c:0 copy -map 0:a:0 \
  -c:1 copy -metadata:s:1 language=eng -map 0:a:1 -c:2 copy -metadata:s:2 language=fra \
  ch-Intro.mkv && ffmpeg -ss 1.0 -to 2.0 -i tests/fixtures/av-chapters.mkv -map 0:v:0 \
  -c:0 copy -map 0:a:0 -c:1 copy -metadata:s:1 language=eng -map 0:a:1 -c:2 copy \
  -metadata:s:2 language=fra 'ch-Chapter 1.mkv' && ffmpeg -ss 2.0 -to 3.0 -i \
  tests/fixtures/av-chapters.mkv -map 0:v:0 -c:0 copy -map 0:a:0 -c:1 copy -metadata:s:1 \
  language=eng -map 0:a:1 -c:2 copy -metadata:s:2 language=fra 'ch-Chapter 2.mkv' && \
  ffmpeg -ss 3.0 -to 4.0 -i tests/fixtures/av-chapters.mkv -map 0:v:0 -c:0 copy -map \
  0:a:0 -c:1 copy -metadata:s:1 language=eng -map 0:a:1 -c:2 copy -metadata:s:2 \
  language=fra ch-Credits.mkv
```

**Re-encode** - frame-accurate cuts, and the whole split is ONE command: the source decodes once no matter how many chapters, with each output taking its own `-ss`/`-to`:

```pgsql
COPY (
  SELECT f.video, f.audio
  FROM input('tests/fixtures/av-chapters.mkv') f, unnest(f.chapters) c
  WHERE f.t BETWEEN c.start_t AND c.end_t
) TO ('ch-' || c.title || '.mkv') WITH (video_codec 'libx264', crf 18, audio_codec 'aac')
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i tests/fixtures/av-chapters.mkv -ss 0.0 -to 1.0 -map 0:v:0 -map 0:a:0 \
  -metadata:s:1 language=eng -map 0:a:1 -metadata:s:2 language=fra -c:0 libx264 -crf:0 \
  18 -c:1 aac -c:2 aac ch-Intro.mkv -ss 1.0 -to 2.0 -map 0:v:0 -map 0:a:0 -metadata:s:1 \
  language=eng -map 0:a:1 -metadata:s:2 language=fra -c:0 libx264 -crf:0 18 -c:1 aac \
  -c:2 aac 'ch-Chapter 1.mkv' -ss 2.0 -to 3.0 -map 0:v:0 -map 0:a:0 -metadata:s:1 \
  language=eng -map 0:a:1 -metadata:s:2 language=fra -c:0 libx264 -crf:0 18 -c:1 aac \
  -c:2 aac 'ch-Chapter 2.mkv' -ss 3.0 -to 4.0 -map 0:v:0 -map 0:a:0 -metadata:s:1 \
  language=eng -map 0:a:1 -metadata:s:2 language=fra -c:0 libx264 -crf:0 18 -c:1 aac \
  -c:2 aac ch-Credits.mkv
```

The chain is the exception, not the rule: it survives only while EVERY stream of every piece is a stream copy. Name one codec, or wrap one column in a filter, and the whole split becomes the single invocation above - the streams you left alone go along with it, taking the container's default encoder instead of `-c copy`.

## 48. Extract every language to its own file

The same rule over track rows: each row's stream goes to a filename built from its own metadata:

```pgsql
COPY (SELECT t FROM input('tests/fixtures/av2.mp4') f, unnest(f.audio) t)
TO (t.tags.language || '.m4a')
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i tests/fixtures/av2.mp4 -map 0:a:0 -c:0 copy -metadata:s:0 language=eng eng.m4a \
  -map 0:a:1 -c:0 copy -metadata:s:0 language=fra fra.m4a
```

## 49. Normalize loudness properly (two-pass)

`sqlmpeg.loudnorm2` measures first and corrects second - the broadcast-compliant way. It compiles to a shell chain: pass 1 prints measurements, `sqlmpeg loudnorm2env` turns them into environment variables, and pass 2 splices them into its filter. (POSIX shells only; `run` does the substitution itself and works everywhere. This is the one command line the cookbook shows unwrapped - its quoting cannot be split.)

```pgsql
COPY (
  SELECT sqlmpeg.loudnorm2(f.audio[1], I => -16, TP => -1.5, LRA => 11)
  FROM input(:'source') f
) TO :'dest' WITH (audio_codec 'aac')
```

```
$ sqlmpeg compile -f query.sql -v source=film.mkv -v dest=out.m4a
eval "$(ffmpeg -i film.mkv -filter_complex '[0:a:0]loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json[out0]' -map '[out0]' -f null - 2>&1 | sqlmpeg loudnorm2env)" && ffmpeg -i film.mkv -filter_complex '[0:a:0]loudnorm=I=-16:TP=-1.5:LRA=11:measured_I='"${SQLMPEG_LN_I}"':measured_TP='"${SQLMPEG_LN_TP}"':measured_LRA='"${SQLMPEG_LN_LRA}"':measured_thresh='"${SQLMPEG_LN_THRESH}"':offset='"${SQLMPEG_LN_OFFSET}"':linear=true[out0]' -map '[out0]' -c:0 aac out.m4a
```

## 50. Stream instead of writing a file

A sink path can be a protocol URL - rtmp, srt, udp - and ffmpeg owns it from there. Name the muxer with `format` (a URL has no extension to infer from):

```pgsql
COPY (SELECT f.video[1], f.audio[1] FROM input(:'source') f)
TO :'dest' WITH (format 'flv', video_codec 'libx264', audio_codec 'aac')
```

```
$ sqlmpeg compile -f query.sql -v source=film.mkv -v dest=rtmp://live.example.com/app/streamkey
ffmpeg -i film.mkv -map 0:v:0 -map 0:a:0 -f flv -c:0 libx264 -c:1 aac \
  rtmp://live.example.com/app/streamkey
```

For SRT use `format 'mpegts'` with an `srt://` destination; UDP the same. Verified end to end: a query streamed over `udp://` to a listening receiver arrives intact, video and audio.

## 51. Set the container's title, clear its artist

In a query without track rows, an aliased non-stream column tags the CONTAINER - the alias is the key, free-form, same as track-row tags. `NULL` clears the tag in the output (ffmpeg copies input globals by default, so clearing is explicit):

```pgsql
COPY (
  SELECT f.video[1], f.audio[1], 'Remastered 2026' AS title, NULL AS artist
  FROM input(:'source') f
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v source=film.mkv -v dest=out.mkv
ffmpeg -i film.mkv -map 0:v:0 -c:0 copy -map 0:a:0 -c:1 copy -metadata artist= -metadata \
  'title=Remastered 2026' out.mkv
```

## 52. Read the container's tags, rewrite them with CASE

Container tags are a map on the input alias, read by path - `f.tags.title`, `f.tags.artist`, `f.tags.comment`, any key the file carries - NULL when it doesn't carry them. So the full CASE toolkit works: fill missing tags, build new ones from old:

```pgsql
COPY (
  SELECT f.video[1], f.audio[1],
    f.tags.title || ' (restored)' AS title,
    CASE WHEN f.tags.comment IS NULL THEN 'no notes' ELSE f.tags.comment END AS comment
  FROM input('tests/fixtures/tagged.mp4') f
) TO 'restored.mp4'
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i tests/fixtures/tagged.mp4 -map 0:v:0 -c:0 copy -map 0:a:0 -c:1 copy -metadata \
  'comment=no notes' -metadata 'title=Angel One (restored)' restored.mp4
```

Reading needs the probe (the values live in the file), so this one is fixture-bound. The same paths work in table queries: `select f.tags.title, f.tags.artist, f.duration from input('movie.mp4') f` prints them, and a bare `f.tags` prints the whole map.

## 53. Tag the tracks and the container in one query

Two levels, two scopes, visible in the query text: inside the `WITH`, rows are tracks, so the tag column titles each stream; outside it, the CTE's streams are just streams, so the tag column titles the container:

```pgsql
COPY (
  WITH tagged AS (
    SELECT a AS track, 'Audio (' || a.tags.language || ')' AS title
    FROM input('tests/fixtures/av2.mp4') f, unnest(f.audio) a
  )
  SELECT g.video, array_agg(tagged.track), 'Director Cut' AS title
  FROM input('tests/fixtures/av2.mp4') g, tagged
  GROUP BY g.video
) TO 'out.mkv'
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i tests/fixtures/av2.mp4 -map 0:v:0 -c:0 copy -map 0:a:0 -c:1 copy -metadata:s:1 \
  language=eng -metadata:s:1 'title=Audio (eng)' -map 0:a:1 -c:2 copy -metadata:s:2 \
  language=fra -metadata:s:2 'title=Audio (fra)' -metadata 'title=Director Cut' out.mkv
```

## 54. Gather rows into one file

A single destination takes exactly one row, so a multi-row query says how its rows combine: `array_agg` gathers streams in row order, `GROUP BY` names what stays constant. (Without them, a multi-row query into one path is a compile error naming both ways out.)

```pgsql
COPY (
  SELECT f.video, array_agg(a)
  FROM input('tests/fixtures/av2.mp4') f, unnest(f.audio) a
  GROUP BY f.video
) TO 'out.mp4'
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i tests/fixtures/av2.mp4 -map 0:v:0 -c:0 copy -map 0:a:0 -c:1 copy -metadata:s:1 \
  language=eng -map 0:a:1 -c:2 copy -metadata:s:2 language=fra out.mp4
```

## 55. One file per language, all its tracks inside

Explicit grouping unlocks what the plain fan-out rejects as a collision: rows that SHARE a destination. `GROUP BY` a row column, aggregate the tracks, and fan out over the key - the group key doubles as each file's title:

```pgsql
COPY (
  SELECT array_agg(a), a.tags.language AS title
  FROM input('tests/fixtures/av-2eng.mp4') f, unnest(f.audio) a
  GROUP BY a.tags.language
) TO (a.tags.language || '.mka')
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i tests/fixtures/av-2eng.mp4 -map 0:a:0 -c:0 copy -metadata:s:0 language=eng \
  -map 0:a:1 -c:1 copy -metadata:s:1 language=eng -metadata title=eng eng.mka -map 0:a:2 \
  -c:0 copy -metadata:s:0 language=fra -metadata title=fra fra.mka
```

## 56. Preview a grouped shape as a table

Grouping works in table queries too - drop the COPY and the same relation prints instead of writing, one row per group, arrays in braces. The single-group form shows what a one-file COPY would carry:

```pgsql
SELECT f.video, array_agg(a)
FROM input('tests/fixtures/av2.mp4') f, unnest(f.audio) a
GROUP BY f.video
```

```
$ sqlmpeg -f query.sql
 video           | array_agg
-----------------+-------------------------------
 {<video 0:v:0>} | {<audio 0:a:0>,<audio 0:a:1>}
(1 row)
```

And grouping by a row column previews a fan-out's partitions before any file is written - here, recipe 55's per-language split:

```pgsql
SELECT a.tags.language, array_agg(a)
FROM input('tests/fixtures/av-2eng.mp4') f, unnest(f.audio) a
GROUP BY a.tags.language
```

```
$ sqlmpeg -f query.sql
 language | array_agg
----------+-------------------------------
 eng      | {<audio 0:a:0>,<audio 0:a:1>}
 fra      | {<audio 0:a:2>}
(2 rows)
```

## 57. Combine tracks selected by separate CTEs

Each CTE picks its tracks with its own WHERE; the outer query is plain SQL over their rows - `FROM vid, aud` is a cross join, so gather the audio with `array_agg` and group by the video to get one row. The table form previews it; wrap it in COPY and the same relation becomes the file:

```pgsql
WITH vid AS (
  SELECT v AS track FROM input('tests/fixtures/av-2eng.mp4') i1, unnest(i1.video) v
),
aud AS (
  SELECT a AS track FROM input('tests/fixtures/av-2eng.mp4') i2, unnest(i2.audio) a
  WHERE a.tags.language = 'eng'
)
SELECT vid.track, array_agg(aud.track) FROM vid, aud GROUP BY vid.track
```

```
$ sqlmpeg -f query.sql
 track         | array_agg
---------------+-------------------------------
 <video 0:v:0> | {<audio 0:a:0>,<audio 0:a:1>}
(1 row)
```

The same SELECT inside `COPY (...) TO 'combo.mkv'` compiles to:

```pgsql
COPY (
  WITH vid AS (
    SELECT v AS track FROM input('tests/fixtures/av-2eng.mp4') i1, unnest(i1.video) v
  ),
  aud AS (
    SELECT a AS track FROM input('tests/fixtures/av-2eng.mp4') i2, unnest(i2.audio) a
    WHERE a.tags.language = 'eng'
  )
  SELECT vid.track, array_agg(aud.track) FROM vid, aud GROUP BY vid.track
) TO 'combo.mkv'
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i tests/fixtures/av-2eng.mp4 -map 0:v:0 -c:0 copy -map 0:a:0 -c:1 copy \
  -metadata:s:1 language=eng -map 0:a:1 -c:2 copy -metadata:s:2 language=eng combo.mkv
```

## 58. Burn a title onto the picture

`drawtext` works out of the box; the font is an option like any other, so name one - fontconfig fallbacks vary by build, and a named file is the same everywhere:

```pgsql
COPY (
  SELECT drawtext(f.video[1], text => :'text', fontfile => :'font', fontsize => 48, x => 20, y => 20, fontcolor => 'white'),
         f.audio[1]
  FROM input(:'source') f
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v text=Hello -v font=arial.ttf -v source=film.mkv -v dest=titled.mp4
ffmpeg -i film.mkv -filter_complex \
  '[0:v:0]drawtext=text=Hello:fontfile=arial.ttf:fontsize=48:x=20:y=20:fontcolor=white[out0]' \
  -map '[out0]' -map 0:a:0 -c:1 copy titled.mp4
```

## 59. Turn an image sequence into a video, and back

An image sequence is an input like any other - ffmpeg's `%04d` pattern names the files, and `framerate` says how fast to play them:

```sql
COPY (SELECT f.video[1] FROM input(:'frames', framerate => 24) f)
TO :'dest' WITH (video_codec 'libx264', crf 18)
```

```
$ sqlmpeg compile -f query.sql -v frames=frames/%04d.png -v dest=out.mp4
ffmpeg -framerate 24 -i frames/%04d.png -map 0:v:0 -c:0 libx264 -crf:0 18 out.mp4
```

The reverse is a pattern in the destination - here one frame per second:

```pgsql
COPY (SELECT fps(f.video[1], 1) FROM input(:'source') f) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v source=film.mkv -v dest=frame-%04d.png
ffmpeg -i film.mkv -filter_complex '[0:v:0]fps=fps=1[out0]' -map '[out0]' frame-%04d.png
```

## 60. Draw a waveform for an audio file

`showwaves` is an audio-to-video filter: it takes the track and returns a picture. Select the same track again as audio and the result is a video with sound - the compiler splits the stream for you:

```pgsql
COPY (
  SELECT showwaves(f.audio[1], size => '1280x240', mode => 'line'), f.audio[1]
  FROM input(:'source') f
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v source=song.mp3 -v dest=waves.mp4
ffmpeg -i song.mp3 -filter_complex \
  '[0:a:0]asplit=2[src_f_a_0_split0][out1];'\
'[src_f_a_0_split0]showwaves=size=1280x240:mode=line[out0]' -map '[out0]' -map '[out1]' \
  waves.mp4
```

## 61. Record a stream

A URL is an input path; ffmpeg owns the protocol. Stream-copy a live HLS or RTMP source straight to disk:

```sql
COPY (SELECT f.video[1], f.audio[1] FROM input(:'url') f) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v url=https://example.com/live/stream.m3u8 -v dest=capture.mp4
ffmpeg -i https://example.com/live/stream.m3u8 -map 0:v:0 -c:0 copy -map 0:a:0 -c:1 copy \
  capture.mp4
```

Add `WITH (duration 60)` to stop after a minute; per-protocol options (headers, transports) are not expressible yet - see [known_gaps.md](known_gaps.md).

## 62. Use a plugin filter

`frei0r` loads effect plugins at runtime (most builds ship it enabled); its options name the plugin and pass its parameters, and it compiles like any other filter:

```pgsql
COPY (
  SELECT frei0r(f.video[1], filter_name => 'glow', filter_params => '0.5'), f.audio[1]
  FROM input(:'source') f
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v source=film.mkv -v dest=glow.mp4
ffmpeg -i film.mkv -filter_complex \
  '[0:v:0]frei0r=filter_name=glow:filter_params=0.5[out0]' -map '[out0]' -map 0:a:0 -c:1 \
  copy glow.mp4
```

ffmpeg finds plugins through the `FREI0R_PATH` environment variable. Audio plugins go through `ladspa` the same way.

## 63. Copy or rebuild a chapter list

`g.chapters AS chapters` takes another file's chapters wholesale, and `NULL AS chapters` writes none at all:

```pgsql
COPY (
  SELECT f.video[1], f.audio[1], g.chapters AS chapters
  FROM input('tests/fixtures/av2.mp4') f, input('tests/fixtures/av-chapters.mkv') g
) TO 'borrowed.mkv'
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i tests/fixtures/av2.mp4 -i tests/fixtures/av-chapters.mkv -map 0:v:0 -c:0 copy \
  -map 0:a:0 -c:1 copy -metadata:s:1 language=eng -map_chapters 1 borrowed.mkv
```

Gathering rows builds one instead. A `VALUES` list is just another row source, so this compiles to exactly the same command as [recipe 40](#40-write-chapters) - two spellings, one file:

```sql
COPY (
  WITH marks(start_t, end_t, title) AS (
    VALUES (0, 60, 'Intro'), (60, 300, 'Act One')
  )
  SELECT f.video[1], f.audio[1],
         array_agg(ROW(m.title, m.start_t, m.end_t)::chapter) AS chapters
  FROM input(:'source') f, marks m
  GROUP BY f.video[1], f.audio[1]
) TO :'dest'
```

```
$ sqlmpeg compile -f query.sql -v source=film.mkv -v dest=chaptered.mkv
ffmpeg -i film.mkv -f ffmetadata -i \
  'data:text/plain;'\
'base64,'\
'O0ZGTUVUQURBVEExCltDSEFQVEVSXQpUSU1FQkFTRT0xLzEKU1RBUlQ9MApFTkQ9NjAKdGl0bGU9SW50cm8KW0NIQVBURVJdClRJTUVCQVNFPTEvMQpTVEFSVD02MApFTkQ9MzAwCnRpdGxlPUFjdCBPbmUK' \
  -map 0:v:0 -c:0 copy -map 0:a:0 -c:1 copy -map_chapters 1 chaptered.mkv
```

## 64. Read a subtitle file's cues

A WebVTT track's cues are rows, the way a container's chapters are - `index`, `start_t`, `end_t` and the cue `text`. ffprobe does not enumerate them, so sqlmpeg reads the file itself:

```pgsql
SELECT c.index, c.start_t, c.end_t, c.text
FROM input('tests/fixtures/subs.en.vtt') v, unnest(v.cues) c
```

```
$ sqlmpeg -f query.sql
 index | start_t | end_t | text
-------+---------+-------+------------
 1     | 0.0     | 0.6   | Cue one.
 2     | 0.7     | 1.3   | Cue two.
 3     | 1.4     | 2.0   | Cue three.
(3 rows)
```

## 65. Turn chapters into a subtitle track, and back

Cues and chapters are the same shape - a title over a time span - so converting either way is an `array_agg` over the other's rows. WebVTT is what HLS uses for chapter metadata, so this is the canonical export:

```pgsql
COPY (
  SELECT f.video[1], f.audio[1],
         array_agg(ROW(c.title, c.start_t, c.end_t)::cue)
  FROM input('tests/fixtures/av-chapters.mkv') f, unnest(f.chapters) c
  GROUP BY f.video[1], f.audio[1]
) TO 'with-cues.mkv'
```

```
$ sqlmpeg compile -f query.sql
ffmpeg -i tests/fixtures/av-chapters.mkv -f webvtt -i   'data:text/vtt;''base64,''V0VCVlRUCgowMDowMDowMC4wMDAgLS0+IDAwOjAwOjAxLjAwMApJbnRybwoKMDA6MDA6MDEuMDAwIC0tPiAwMDowMDowMi4wMDAKQ2hhcHRlciAxCgowMDowMDowMi4wMDAgLS0+IDAwOjAwOjAzLjAwMApDaGFwdGVyIDIKCjAwOjAwOjAzLjAwMCAtLT4gMDA6MDA6MDQuMDAwCkNyZWRpdHMK'   -map 0:v:0 -c:0 copy -map 0:a:0 -c:1 copy -map 1:s:0 -c:2 webvtt with-cues.mkv
```

The reverse - a `.vtt` file's cues becoming a chapter list - is the same expression with the types swapped: `array_agg(ROW(c.text, c.start_t, c.end_t)::chapter) AS chapters` over `unnest(v.cues) c`.

