# Cookbook

Real tasks, in roughly the order people meet them. Each recipe is the question as it's usually asked, the query that answers it, and exactly what sqlmpeg prints for it - the compiled ffmpeg command for a media query, the result table or CSV for a metadata one. Every shown output on this page is real - a test reruns all of them and diffs byte for byte, so if a recipe is here, it works. Most recipes are parameterized (`:'source'`-style variables, filled by the `-v` flags in the shown command), so they are programs: swap the `-v` values and they run against your files. Recipe 33 explains the mechanism; [queries/](../queries/) collects ready-made programs.

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

`SELECT *` means keep everything: every stream, untouched, tags intact. Nothing decodes; this runs as fast as the disk. The one wrinkle is captions - mp4 only carries `mov_text`, so the subtitle track transcodes while video and audio copy:

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
SELECT a.video[1], a.audio[1]
FROM input(:'source') a
WHERE a.t BETWEEN 5 AND 60
```

```
$ sqlmpeg compile -f query.sql -o cut.mp4 -v source=clip.mp4
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
SELECT scale(f.frame, 1280, -2), f.audio[1]
FROM input(:'source') f
```

```
$ sqlmpeg compile -f query.sql -o small.mp4 -v source=film.mp4
ffmpeg -i film.mp4 -filter_complex '[0:v:0]scale=width=1280:height=-2[out0]' -map \
  '[out0]' -map 0:a:0 -c:1 copy small.mp4
```

Or express the width relative to the input - any string-typed option takes an ffmpeg expression - and let `-2` keep the aspect:

```pgsql
SELECT scale(f.frame, 'iw/2', -2), f.audio[1]
FROM input(:'source') f
```

```
$ sqlmpeg compile -f query.sql -o half.mp4 -v source=film.mp4
ffmpeg -i film.mp4 -filter_complex '[0:v:0]scale=width=iw/2:height=-2[out0]' -map \
  '[out0]' -map 0:a:0 -c:1 copy half.mp4
```

## 6. Rotate a phone video 90 degrees

For quarter turns, ffmpeg's `transpose` is the right tool (it swaps the axes rather than resampling). For arbitrary angles there's `rotate`, whose angle is an expression in radians - `rotate(f.frame, '7*PI/180')` leans a clip seven degrees:

```pgsql
SELECT transpose(v.frame, dir => 'clock'), v.audio[1]
FROM input(:'source') v
```

```
$ sqlmpeg compile -f query.sql -o upright.mp4 -v source=phone.mp4
ffmpeg -i phone.mp4 -filter_complex '[0:v:0]transpose=dir=clock[out0]' -map '[out0]' \
  -map 0:a:0 -c:1 copy upright.mp4
```

## 7. Sharpen a soft-looking video

Any of your ffmpeg's filters is callable directly, options by name, checked against what the binary actually supports. (The one-knob version, if you don't need the fine control: `unsharp(f.frame, 5, 5, 1.5)`, matrix sizes then amount, positionally in unsharp's own order.)

```pgsql
SELECT unsharp(a.frame, luma_msize_x => 7, luma_amount => 1.5), a.audio[1]
FROM input(:'source') a
```

```
$ sqlmpeg compile -f query.sql -v source=clip.mp4
ffmpeg -i clip.mp4 -filter_complex '[0:v:0]unsharp=luma_msize_x=7:luma_amount=1.5[out0]' \
  -map '[out0]' -map 0:a:0 -c:1 copy out.mp4
```

## 8. Concatenate two clips

`UNION ALL` is ffmpeg's concat. SQL requires the branches to agree on column count, type and order, and that is exactly concat's segment contract - the interleaving that's so easy to get wrong by hand is generated for you:

```sql
SELECT a.video[1], a.audio[1] FROM input(:'first') a
UNION ALL
SELECT b.video[1], b.audio[1] FROM input(:'second') b
```

```
$ sqlmpeg compile -f query.sql -o joined.mp4 -v first=part1.mp4 -v second=part2.mp4
ffmpeg -i part1.mp4 -i part2.mp4 -filter_complex \
  '[0:v:0][0:a:0][1:v:0][1:a:0]concat=n=2:v=1:a=1[out0][out1]' -map '[out0]' -map \
  '[out1]' joined.mp4
```

And it scales to files you'd rather not count streams in: splat the whole audio array and the languages pair up positionally, English with English, French with French, tags surviving. (This one needs real files - a splat has to know how many tracks there are.)

```pgsql
SELECT a.frame, a.audio FROM input('tests/fixtures/av2.mp4') a
UNION ALL
SELECT b.frame, b.audio FROM input('tests/fixtures/av3.mp4') b
```

```
$ sqlmpeg compile -f query.sql -o season.mkv
ffmpeg -i tests/fixtures/av2.mp4 -i tests/fixtures/av3.mp4 -filter_complex \
  '[0:v:0][0:a:0][0:a:1][1:v:0][1:a:0][1:a:1]concat=n=2:v=1:a=2[out0][out1][out2]' -map \
  '[out0]' -map '[out1]' -metadata:s:1 language=eng -map '[out2]' -metadata:s:2 \
  language=fra season.mkv
```

## 9. Watermark a video

`loop => true` keeps a still image alive for the whole duration, and the position is an ffmpeg expression - `(W-w)/2` centers it without you knowing either file's dimensions:

```pgsql
SELECT overlay(f.frame, logo.frame, '(W-w)/2', '(H-h)/2'), f.audio[1]
FROM input(:'main') f, input(:'overlay', loop => true) logo
```

```
$ sqlmpeg compile -f query.sql -o branded.mp4 -v main=film.mp4 -v overlay=watermark.png
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
SELECT subtitles(f.frame, 'subs.en.srt'), f.audio[1]
FROM input(:'source') f
```

```
$ sqlmpeg compile -f query.sql -o burned.mp4 -v source=film.mp4
ffmpeg -i film.mp4 -filter_complex '[0:v:0]subtitles=filename=subs.en.srt[out0]' -map \
  '[out0]' -map 0:a:0 -c:1 copy burned.mp4
```

## 12. Speed a clip up 2x, picture and sound together

Two functions because the two stream types speed up differently: `sqlmpeg.speed` restamps video frames, `atempo` resamples audio while keeping the pitch (so nobody turns into a chipmunk):

```pgsql
SELECT sqlmpeg.speed(f.frame, :factor), atempo(f.audio[1], :factor)
FROM input(:'source') f
```

```
$ sqlmpeg compile -f query.sql -o fast.mp4 -v source=film.mp4 -v factor=2
ffmpeg -i film.mp4 -filter_complex \
  '[0:v:0]setpts=PTS/2[out0];[0:a:0]atempo=tempo=2[out1]' -map '[out0]' -map '[out1]' \
  fast.mp4
```

## 13. Crossfade between two clips

`xfade` takes both clips, then `duration` and `offset` by name (its first option is the transition style, which defaults to a plain dissolve) - the offset is seconds into the FIRST clip where the fade begins, so a 10-second clip with a 1-second fade starts dissolving at 9. `acrossfade` does the same for the sound:

```pgsql
SELECT xfade(a.frame, b.frame, duration => 1, offset => 9),
       acrossfade(a.audio[1], b.audio[1], duration => 1)
FROM input(:'first') a, input(:'second') b
```

```
$ sqlmpeg compile -f query.sql -o dissolve.mp4 -v first=one.mp4 -v second=two.mp4
ffmpeg -i one.mp4 -i two.mp4 -filter_complex \
  '[0:v:0][1:v:0]xfade=duration=1:offset=9[out0];'\
'[0:a:0][1:a:0]acrossfade=duration=1[out1]' -map '[out0]' -map '[out1]' dissolve.mp4
```

## 14. Turn a clip into a GIF

The good-looking way needs two passes over the frames - one to build a palette, one to use it. Write it as a CTE consumed twice; the compiler inserts the split:

```pgsql
WITH small AS (
  SELECT fps(scale(v.frame, 480, -2), 12) AS frame
  FROM input(:'source') v
)
SELECT paletteuse(small.frame, palettegen(small.frame))
FROM small
```

```
$ sqlmpeg compile -f query.sql -o clip.gif -v source=clip.mp4
ffmpeg -i clip.mp4 -filter_complex \
  '[0:v:0]scale=width=480:height=-2,fps=fps=12,split=2[n2_split0][n2_split1];'\
'[n2_split0]palettegen[n3];[n2_split1][n3]paletteuse[out0]' -map '[out0]' clip.gif
```

## 15. Replace a video's audio, or duck music under the dialogue

Swapping is just selecting video from one input and audio from another:

```sql
SELECT v.video[1], m.audio[1]
FROM input(:'main') v, input(:'voice') m
```

```
$ sqlmpeg compile -f query.sql -o dubbed.mp4 -v main=film.mp4 -v voice=voiceover.wav
ffmpeg -i film.mp4 -i voiceover.wav -map 0:v:0 -c:0 copy -map 1:a:0 -c:1 copy dubbed.mp4
```

Keeping both, with the music turned down, is a mix:

```pgsql
SELECT v.video[1], amix(v.audio[1], volume(m.audio[1], 0.2))
FROM input(:'main') v, input(:'music') m
```

```
$ sqlmpeg compile -f query.sql -o scored.mp4 -v main=film.mp4 -v music=music.m4a
ffmpeg -i film.mp4 -i music.m4a -filter_complex \
  '[1:a:0]volume=volume=0.2[n1];[0:a:0][n1]amix=inputs=2[out1]' -map 0:v:0 -c:0 copy \
  -map '[out1]' scored.mp4
```

Real ducking - music that dips when someone speaks - is a sidechain compressor keyed off the dialogue. Naming `v.audio[1]` twice is fine; the compiler inserts the split:

```pgsql
SELECT v.video[1], amix(v.audio[1], sidechaincompress(m.audio[1], v.audio[1], threshold => 0.03, ratio => 8))
FROM input(:'main') v, input(:'music') m
```

```
$ sqlmpeg compile -f query.sql -o ducked.mp4 -v main=film.mp4 -v music=music.m4a
ffmpeg -i film.mp4 -i music.m4a -filter_complex \
  '[0:a:0]asplit=2[src_v_a_0_split0][src_v_a_0_split1];'\
'[1:a:0][src_v_a_0_split0]sidechaincompress=threshold=0.03:ratio=8[n1];'\
'[src_v_a_0_split1][n1]amix=inputs=2[out1]' -map 0:v:0 -c:0 copy -map '[out1]' \
  ducked.mp4
```

## 16. Picture-in-picture

A quarter-size camera in the bottom-right corner, 20 pixels off each edge - the expressions mean the position holds whatever the two resolutions are. (The dual-language version, with the audio mixed per language, is the README's opening demo.)

```pgsql
SELECT overlay(f.frame, scale(c.frame, 'iw/4', -2), 'W-w-20', 'H-h-20'), f.audio[1]
FROM input(:'main') f, input(:'overlay') c
```

```
$ sqlmpeg compile -f query.sql -o pip.mp4 -v main=film.mp4 -v overlay=camera.mp4
ffmpeg -i film.mp4 -i camera.mp4 -filter_complex \
  '[1:v:0]scale=width=iw/4:height=-2[n1];[0:v:0][n1]overlay=x=W-w-20:y=H-h-20[out0]' \
  -map '[out0]' -map 0:a:0 -c:1 copy pip.mp4
```

## 17. Insert a clip at a timestamp

The splice: cut away to the insert, then resume. The same file appears under two aliases with two windows, and the tail's `>= 120` means "to the end" with no made-up end time:

```sql
SELECT f.video[1], f.audio[1] FROM input(:'main') f WHERE f.t <= :cut
UNION ALL
SELECT ad.video[1], ad.audio[1] FROM input(:'insert') ad
UNION ALL
SELECT g.video[1], g.audio[1] FROM input(:'main') g WHERE g.t >= :cut
```

```
$ sqlmpeg compile -f query.sql -o spliced.mp4 -v main=film.mp4 -v insert=promo.mp4 -v cut=120
ffmpeg -to 120 -i film.mp4 -i promo.mp4 -ss 120 -i film.mp4 -filter_complex \
  '[0:v:0][0:a:0][1:v:0][1:a:0][2:v:0][2:a:0]concat=n=3:v=1:a=1[out0][out1]' -map \
  '[out0]' -map '[out1]' spliced.mp4
```

Or keep the main video playing and overlay the insert on top: a delayed video stream is transparent until its start time (and after it ends), so it composes with a plain `overlay` - no timeline bookkeeping:

```pgsql
SELECT overlay(f.frame, sqlmpeg.delay(promo.frame, 120), 20, 20), f.audio[1]
FROM input(:'main') f, input(:'insert') promo
```

```
$ sqlmpeg compile -f query.sql -o overlaid.mp4 -v main=film.mp4 -v insert=promo.mp4
ffmpeg -i film.mp4 -i promo.mp4 -filter_complex \
  '[1:v:0]format=pix_fmts=yuva420p,tpad=start_duration=120:stop=1:color=black@0[n2];'\
'[0:v:0][n2]overlay=x=20:y=20[out0]' -map '[out0]' -map 0:a:0 -c:1 copy overlaid.mp4
```

## 18. Normalize loudness on every language track at once

A bare `.audio` is the whole track array; handing it to a filter broadcasts, one node per language, and every output keeps its language tag. (`ffmpeg.loudnorm` rather than bare `loudnorm` only out of habit here - the bare name works too; the namespace is the spelling that never collides with Postgres grammar. `I` is EBU R128 integrated loudness, and yes, it's a capital I.)

```pgsql
SELECT f.video[1], ffmpeg.loudnorm(f.audio, I => -23)
FROM input('tests/fixtures/av2.mp4') f
```

```
$ sqlmpeg compile -f query.sql -o broadcast.mkv
ffmpeg -i tests/fixtures/av2.mp4 -filter_complex \
  '[0:a:0]loudnorm=I=-23[out1];[0:a:1]loudnorm=I=-23[out2]' -map 0:v:0 -c:0 copy -map \
  '[out1]' -metadata:s:1 language=eng -map '[out2]' -metadata:s:2 language=fra \
  broadcast.mkv
```

## 19. Blur a region, or blur during a time window

`sqlmpeg.blur_regions` is crop, blur and overlay in one call - the license-plate special:

```pgsql
SELECT sqlmpeg.blur_regions(f.frame, 900, 60, 320, 180, 20), f.audio[1]
FROM input(:'source') f
```

```
$ sqlmpeg compile -f query.sql -o anonymized.mp4 -v source=interview.mp4
ffmpeg -i interview.mp4 -filter_complex \
  '[0:v:0]split=2[src_f_v_0_split0][src_f_v_0_split1];'\
'[src_f_v_0_split0]crop=out_w=320:out_h=180:x=900:y=60,gblur=sigma=20[n2];'\
'[src_f_v_0_split1][n2]overlay=x=900:y=60[out0]' -map '[out0]' -map 0:a:0 -c:1 copy \
  anonymized.mp4
```

To apply an effect only during a time window, `enable` is the switch - no trimming, no branches, no concat, just a filter that turns itself on and off:

```pgsql
SELECT gblur(a.frame, 12, enable => 'between(t,0.5,1.5)')
FROM input(:'source') a
```

```
$ sqlmpeg compile -f query.sql -v source=clip.mp4
ffmpeg -i clip.mp4 -filter_complex \
  '[0:v:0]gblur=sigma=12:enable=between(t\,0.5\,1.5)[out0]' -map '[out0]' out.mp4
```

## 20. Generate test media

Sources live in FROM and consume no input file at all - note the command below has no `-i`:

```pgsql
SELECT t.frame, s.audio[1]
FROM ffmpeg.testsrc2(duration => 10, size => '1280x720', rate => 30) t,
     ffmpeg.sine(frequency => 440, duration => 10) s
```

```
$ sqlmpeg compile -f query.sql -o bars.mp4
ffmpeg -filter_complex \
  'testsrc2=duration=10:size=1280x720:rate=30[out0];'\
'sine=frequency=440:duration=10[out1]' -map '[out0]' -map '[out1]' bars.mp4
```

They also solve a quieter problem: `UNION ALL` branches must match column for column, so appending a slate to a clip needs a silent audio track from somewhere. `anullsrc` is that somewhere:

```pgsql
SELECT f.video[1], f.audio[1] FROM input(:'source') f
UNION ALL
SELECT t.video[1], s.audio[1]
FROM ffmpeg.color(color => 'black', duration => 3, size => '1280x720', rate => 30) t,
     ffmpeg.anullsrc(duration => 3) s
```

```
$ sqlmpeg compile -f query.sql -o with-slate.mp4 -v source=clip.mp4
ffmpeg -i clip.mp4 -filter_complex \
  'color=color=black:duration=3:size=1280x720:rate=30[n1];anullsrc=duration=3[n2];'\
'[0:v:0][0:a:0][n1][n2]concat=n=2:v=1:a=1[out0][out1]' -map '[out0]' -map '[out1]' \
  with-slate.mp4
```

## 21. Split a stereo track, or compress it in bands

A few filters return a whole array, sized by one of their own options. `channelsplit` turns one stereo track into two mono streams; splatted into the SELECT list, each becomes its own output:

```pgsql
SELECT ffmpeg.channelsplit(a.audio[1])
FROM input(:'source') a
```

```
$ sqlmpeg compile -f query.sql -o channels.mkv -v source=stereo.mp4
ffmpeg -i stereo.mp4 -filter_complex '[0:a:0]channelsplit[out0][out1]' -map '[out0]' \
  -map '[out1]' channels.mkv
```

`acrossover` splits by frequency instead - two split points make three bands - and that's the shape of multiband compression: split, compress each band on its own settings, mix back:

```pgsql
WITH bands AS (
  SELECT ffmpeg.acrossover(a.audio[1], split => '300 3000') AS b
  FROM input(:'source') a
)
SELECT amix(amix(acompressor(bands.b[1], threshold => 0.1, ratio => 4),
                 acompressor(bands.b[2], threshold => 0.05, ratio => 6)),
            acompressor(bands.b[3], threshold => 0.1, ratio => 4))
FROM bands
```

```
$ sqlmpeg compile -f query.sql -o mastered.m4a -v source=song.m4a
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
  SELECT overlay(f.frame, logo.frame, 'W-w-20', 20) AS v, f.audio[1] AS a
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

`unnest` turns a track array into rows - one per track, with the probed metadata as real columns - and a `WHERE` over those columns is track selection that says what you mean. No more counting streams in ffprobe output to learn that English is `[2]` this time:

```pgsql
SELECT t.track
FROM input('tests/fixtures/av2.mp4') f, unnest(f.audio) t
WHERE t.language = 'eng'
```

```
$ sqlmpeg compile -f query.sql -o eng.m4a
ffmpeg -i tests/fixtures/av2.mp4 -map 0:a:0 -c:0 copy -metadata:s:0 language=eng eng.m4a
```

Audio rows carry `language`, `title`, `codec`, `channels`, `channel_layout`, `sample_rate`, `bitrate` and `duration`; video rows carry `width`, `height`, `fps` and friends instead. A track nobody probed has NULL in every metadata column, and NULL matches nothing - standard SQL, no new rules.

## 24. Extract captions by language

Caption arrays unnest the same way (columns: `language`, `title`, `codec`), so pulling the English subtitles out of a many-language file is a `WHERE`, not a subscript:

```pgsql
SELECT s.track
FROM input('tests/fixtures/avs.mkv') f, unnest(f.subtitle) s
WHERE s.language = 'eng'
```

```
$ sqlmpeg compile -f query.sql -o subs.srt
ffmpeg -i tests/fixtures/avs.mkv -map 0:s:0 -c:0 copy -metadata:s:0 language=eng \
  subs.srt
```

## 25. Mix two files' tracks pairwise, matched by language

Two multi-language files, and every track should mix with its counterpart - English with English, French with French, whatever order each file stores them in. That is a JOIN, written exactly the way Postgres writes it, evaluated entirely at compile time (the metadata is probed, so ffmpeg only ever sees the wiring the join decided):

```pgsql
SELECT amix(a.track, b.track)
FROM input('tests/fixtures/av2.mp4') f, input('tests/fixtures/av3.mp4') g,
     unnest(f.audio) a JOIN unnest(g.audio) b ON a.language = b.language
```

```
$ sqlmpeg compile -f query.sql -o mixed.mka
ffmpeg -i tests/fixtures/av2.mp4 -i tests/fixtures/av3.mp4 -filter_complex \
  '[0:a:0][1:a:0]amix=inputs=2[out0];[0:a:1][1:a:1]amix=inputs=2[out1]' -map '[out0]' \
  -metadata:s:0 language=eng -map '[out1]' -metadata:s:1 language=fra mixed.mka
```

Result rows follow the LEFT side's track order, so the output track order is `f`'s - track order is player-visible surface, and nothing here resorts it. And when one file carries two English tracks (a 5.1 and a stereo, say), that's not an error, it's two pairs - real join semantics - and the fix is a wider key: `ON a.language = b.language AND a.channel_layout = b.channel_layout`.

## 26. Mix everything the files have, missing tracks count as silence

An outer join keeps the rows only one side has, and `COALESCE` fills the gap - for audio, with generated silence:

```pgsql
SELECT amix(a.track, COALESCE(b.track, ffmpeg.anullsrc(duration => 2)))
FROM input('tests/fixtures/av2.mp4') f, input('tests/fixtures/av-eng.mp4') g,
     unnest(f.audio) a FULL OUTER JOIN unnest(g.audio) b ON a.language = b.language
```

```
$ sqlmpeg compile -f query.sql -o full.mka
ffmpeg -i tests/fixtures/av2.mp4 -i tests/fixtures/av-eng.mp4 -filter_complex \
  'anullsrc=duration=2[n1];[0:a:0][1:a:0]amix=inputs=2[out0];'\
'[0:a:1][n1]amix=inputs=2[out1]' -map '[out0]' -metadata:s:0 language=eng -map '[out1]' \
  -metadata:s:1 language=fra full.mka
```

The second file has no French, so the French mix gets silence in that slot - and keeps its `fra` tag, because the tag came from the side that existed.

## 27. Concatenate files with different track counts

The founding case. `concat` demands identical segment shapes, so the file that lacks a French track needs a silent stand-in - which is the same outer join, once per branch, each branch selecting its own side. (Aliases respell in the second branch because alias names are script-wide.)

```pgsql
SELECT f.video[1], a.track
FROM input('tests/fixtures/av2.mp4') f, input('tests/fixtures/av-eng.mp4') g,
     unnest(f.audio) a FULL OUTER JOIN unnest(g.audio) b ON a.language = b.language
UNION ALL
SELECT g2.video[1], COALESCE(b2.track, ffmpeg.anullsrc(duration => 2))
FROM input('tests/fixtures/av2.mp4') f2, input('tests/fixtures/av-eng.mp4') g2,
     unnest(f2.audio) a2 FULL OUTER JOIN unnest(g2.audio) b2 ON a2.language = b2.language
```

```
$ sqlmpeg compile -f query.sql -o joined.mp4
ffmpeg -i tests/fixtures/av2.mp4 -i tests/fixtures/av-eng.mp4 -filter_complex \
  'anullsrc=duration=2[n1];'\
'[0:v:0][0:a:0][0:a:1][1:v:0][1:a:0][n1]concat=n=2:v=1:a=2[out0][out1][out2]' -map \
  '[out0]' -map '[out1]' -metadata:s:1 language=eng -map '[out2]' -metadata:s:2 \
  language=fra joined.mp4
```

Both branches share one join shape, so both agree on track order, and eng concatenates with eng. Each file appears in two branches but gets ONE `-i`: untrimmed aliases over the same path share an input.

## 28. Side by side, matched by resolution

Video arrays unnest too - `width`, `height`, `fps`, `codec`, `bitrate` are the columns - so pairing renditions for a comparison strip is a join on the numbers that matter:

```pgsql
SELECT hstack(a.track, b.track)
FROM input('tests/fixtures/testsrc.mp4') f, input('tests/fixtures/smptebars.mp4') g,
     unnest(f.video) a JOIN unnest(g.video) b
       ON a.width = b.width AND a.height = b.height
```

```
$ sqlmpeg compile -f query.sql -o sxs.mp4
ffmpeg -i tests/fixtures/testsrc.mp4 -i tests/fixtures/smptebars.mp4 -filter_complex \
  '[0:v:0][1:v:0]hstack=inputs=2[out0]' -map '[out0]' sxs.mp4
```

A video gap in an outer join fills with `COALESCE(b.track, ffmpeg.color())` - black by default, size, rate and duration inherited from the paired row. A caption gap fills with `COALESCE(b.track, sqlmpeg.empty_captions())`: the track exists and takes its language tag, it just contains zero cues - nobody generates your subtitles for you.

## 29. Assert what you're shipping

A subscripted track has the same metadata columns a row does: `f.audio[1].language` is the first track's tag, right there in a `WHERE`. Since the predicate evaluates at compile time, this is an assertion - if track 1 isn't English, the script refuses to compile instead of quietly shipping the wrong language. (`f.audio[1]` itself is sugar for `f.audio[1].track`; the strictly-Postgres spelling `(f.audio[1]).language` works too.)

```pgsql
SELECT f.audio[1] FROM input('tests/fixtures/av2.mp4') f
WHERE f.audio[1].language = 'eng'
```

```
$ sqlmpeg compile -f query.sql -o eng.m4a
ffmpeg -i tests/fixtures/av2.mp4 -map 0:a:0 -c:0 copy -metadata:s:0 language=eng eng.m4a
```

Recipe 23 answers "give me whichever track is English"; this one answers "I believe track 1 is English - stop me if I'm wrong". Same wiring out the other end, different contract.

## 30. Look at a file's tracks as a table

A SELECT with no COPY and no `-o` is a table query: `run` (the default subcommand, so no subcommand at all) prints the result set and executes nothing - the whole answer was known at compile time. The columns are the probed metadata, so this is ffprobe you can read:

```pgsql
SELECT t.index, t.language, t.codec, t.channel_layout
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

## 31. Inspect a join before you trust it

Stream-valued cells print as placeholders carrying the stream spec, so a table query over a join shows exactly which track paired with which - and an empty cell is an outer join's gap, before you've committed to a fill:

```pgsql
SELECT a.language, a.track AS film, b.track AS promo
FROM input('tests/fixtures/av2.mp4') f, input('tests/fixtures/av-eng.mp4') g,
     unnest(f.audio) a FULL OUTER JOIN unnest(g.audio) b ON a.language = b.language
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
  SELECT t.language, t.codec, t.channel_layout
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
