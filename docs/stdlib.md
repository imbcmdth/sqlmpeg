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
