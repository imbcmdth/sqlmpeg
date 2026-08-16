# sqlmpeg stdlib

GENERATED FILE. Do not edit by hand -- this is rendered from the
`FUNCTIONS` table in `sqlmpeg/stdlib.py` (guardrail #4: the function table
is the single source of truth for names, arities, arg mapping, and docs).
Regenerate with:

    python scripts/gen_docs.py

## scale

`scale(f: video, factor: num)`

`scale(f: video, w: num, h: num)`

Resize a frame by a scale factor, or to explicit width/height.

FFmpeg filter(s): `scale`

## crop

`crop(f: video, x: num, y: num, w: num, h: num)`

Crop a frame to a w x h rectangle at (x, y).

FFmpeg filter(s): `crop`

## overlay

`overlay(base: video, top: video, x: num, y: num)`

Composite top over base at position (x, y).

FFmpeg filter(s): `overlay`

## hflip

`hflip(f: video)`

Flip a frame horizontally.

FFmpeg filter(s): `hflip`

## vflip

`vflip(f: video)`

Flip a frame vertically.

FFmpeg filter(s): `vflip`

## blur

`blur(f: video, sigma: num)`

Apply a Gaussian blur with the given sigma.

FFmpeg filter(s): `gblur`

## blur_regions

`blur_regions(f: video, x: num, y: num, w: num, h: num, sigma: num)`

Blur a w x h rectangle at (x, y) and composite it back over the frame.

FFmpeg filter(s): `crop`, `gblur`, `overlay`

## draw_box

`draw_box(f: video, x: num, y: num, w: num, h: num, color: str)`

Draw an outlined box at (x, y) sized w x h in the given color.

FFmpeg filter(s): `drawbox`

## text

`text(f: video, s: str, x: num, y: num, size: num)`

Draw text s at (x, y) with the given font size.

FFmpeg filter(s): `drawtext`

## speed

`speed(f: video, factor: num)`

Change a video stream's playback speed by factor (use atempo for audio).

FFmpeg filter(s): `setpts`

## fade_in

`fade_in(f: video, dur: num)`

Fade in from black over dur seconds starting at t=0.

FFmpeg filter(s): `fade`

## fade_out

`fade_out(f: video, dur: num)`

`fade_out(f: video, dur: num, at: num)`

Fade out to black over dur seconds starting at `at` seconds (without `at` the fade starts at t=0 and every later frame is black; pass at = clip length - dur to fade at the end).

FFmpeg filter(s): `fade`

## volume

`volume(a: audio, factor: num)`

Scale audio volume by a linear factor.

FFmpeg filter(s): `volume`

## amix

`amix(a: audio, b: audio)`

Mix two audio streams together (equal weight, ffmpeg amix defaults).

FFmpeg filter(s): `amix`

## atempo

`atempo(a: audio, factor: num)`

Change audio playback tempo by factor (pitch-preserving, audio-only).

FFmpeg filter(s): `atempo`

## afade_in

`afade_in(a: audio, dur: num)`

Fade audio in from silence over dur seconds starting at t=0.

FFmpeg filter(s): `afade`

## afade_out

`afade_out(a: audio, dur: num)`

`afade_out(a: audio, dur: num, at: num)`

Fade audio out to silence over dur seconds starting at `at` seconds (without `at` the fade starts at t=0 and every later sample is silent; pass at = clip length - dur to fade at the end).

FFmpeg filter(s): `afade`

## reverb

`reverb(a: audio, decay: num)`

Approximate reverb via a single-tap echo (aecho); not a true convolution reverb, but a cheap, dependency-free stand-in.

FFmpeg filter(s): `aecho`

## rotate

`rotate(f: video, degrees: num)`

Rotate a frame degrees clockwise (ffmpeg evaluates the angle expression).

FFmpeg filter(s): `rotate`

## pad

`pad(f: video, w: num, h: num)`

`pad(f: video, w: num, h: num, color: str)`

`pad(f: video, w: num, h: num, x: num, y: num)`

`pad(f: video, w: num, h: num, x: num, y: num, color: str)`

Pad a frame to w x h; with just (w, h) the original is centered on a black background, (x, y) place it explicitly, and a trailing color string sets the background.

FFmpeg filter(s): `pad`

## hstack

`hstack(a: video, b: video)`

Stack two frames side by side horizontally (inputs should share height).

FFmpeg filter(s): `hstack`

## vstack

`vstack(a: video, b: video)`

Stack two frames vertically (inputs should share width).

FFmpeg filter(s): `vstack`

## fps

`fps(f: video, rate: num)`

Force a constant output frame rate, duplicating or dropping frames as needed.

FFmpeg filter(s): `fps`

## sharpen

`sharpen(f: video, amount: num)`

Sharpen a frame by the given luma amount (ffmpeg unsharp, 5x5 matrix).

FFmpeg filter(s): `unsharp`

## deinterlace

`deinterlace(f: video)`

Deinterlace a frame (ffmpeg yadif, default mode/parity).

FFmpeg filter(s): `yadif`

## denoise

`denoise(f: video, strength: num)`

Denoise a frame by the given luma spatial strength (ffmpeg hqdn3d).

FFmpeg filter(s): `hqdn3d`

## brightness

`brightness(f: video, v: num)`

Adjust brightness by v (-1..1, 0 = unchanged; ffmpeg eq).

FFmpeg filter(s): `eq`

## contrast

`contrast(f: video, v: num)`

Adjust contrast by v (0..2, 1 = unchanged; ffmpeg eq).

FFmpeg filter(s): `eq`

## saturate

`saturate(f: video, v: num)`

Adjust saturation by v (0..3, 1 = unchanged; ffmpeg eq).

FFmpeg filter(s): `eq`

## grayscale

`grayscale(f: video)`

Desaturate a frame to grayscale (ffmpeg hue, s=0).

FFmpeg filter(s): `hue`

## crossfade

`crossfade(a: video, b: video, dur: num, offset: num)`

`crossfade(a: video, b: video, dur: num, offset: num, transition: str)`

Cross fade from a to b over dur seconds, starting offset seconds into a (default transition 'fade'); inputs must share resolution and fps.

FFmpeg filter(s): `xfade`

## subtitles

`subtitles(f: video, path: str)`

Burn subtitles from path into a frame at run time (the file must exist then).

FFmpeg filter(s): `subtitles`

## reverse

`reverse(f: video)`

Reverse a video stream (buffers the entire stream in memory).

FFmpeg filter(s): `reverse`

## normalize

`normalize(a: audio)`

`normalize(a: audio, lufs: num)`

Normalize loudness to EBU R128 (default -24 LUFS, or the given target).

FFmpeg filter(s): `loudnorm`

## highpass

`highpass(a: audio, freq: num)`

Attenuate frequencies below freq Hz (ffmpeg highpass).

FFmpeg filter(s): `highpass`

## lowpass

`lowpass(a: audio, freq: num)`

Attenuate frequencies above freq Hz (ffmpeg lowpass).

FFmpeg filter(s): `lowpass`

## delay

`delay(a: audio, seconds: num)`

`delay(f: video, seconds: num)`

Delay a stream by seconds: audio shifts by that many milliseconds (adelay), video becomes a transparent canvas that is empty before the clip starts and after it ends (format + tpad), so it composites straight into overlay.

FFmpeg filter(s): `adelay`, `format`, `tpad`

## acrossfade

`acrossfade(a: audio, b: audio, dur: num)`

Cross fade from a to b over dur seconds of audio.

FFmpeg filter(s): `acrossfade`

## areverse

`areverse(a: audio)`

Reverse an audio stream (buffers the entire stream in memory).

FFmpeg filter(s): `areverse`
