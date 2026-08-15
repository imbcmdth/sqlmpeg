# sqlmpeg stdlib

GENERATED FILE. Do not edit by hand -- this is rendered from the
`FUNCTIONS` table in `sqlmpeg/stdlib.py` (guardrail #4: the function table
is the single source of truth for names, arities, arg mapping, and docs).
Regenerate with:

    python scripts/gen_docs.py

## scale

`scale(f: frame, factor: num)`

`scale(f: frame, w: num, h: num)`

Resize a frame by a scale factor, or to explicit width/height.

FFmpeg filter(s): `scale`

## crop

`crop(f: frame, x: num, y: num, w: num, h: num)`

Crop a frame to a w x h rectangle at (x, y).

FFmpeg filter(s): `crop`

## overlay

`overlay(base: frame, top: frame, x: num, y: num)`

Composite top over base at position (x, y).

FFmpeg filter(s): `overlay`

## hflip

`hflip(f: frame)`

Flip a frame horizontally.

FFmpeg filter(s): `hflip`

## vflip

`vflip(f: frame)`

Flip a frame vertically.

FFmpeg filter(s): `vflip`

## blur

`blur(f: frame, sigma: num)`

Apply a Gaussian blur with the given sigma.

FFmpeg filter(s): `gblur`

## blur_regions

`blur_regions(f: frame, x: num, y: num, w: num, h: num, sigma: num)`

Blur a w x h rectangle at (x, y) and composite it back over the frame.

FFmpeg filter(s): `crop`, `gblur`, `overlay`

## draw_box

`draw_box(f: frame, x: num, y: num, w: num, h: num, color: str)`

Draw an outlined box at (x, y) sized w x h in the given color.

FFmpeg filter(s): `drawbox`

## text

`text(f: frame, s: str, x: num, y: num, size: num)`

Draw text s at (x, y) with the given font size.

FFmpeg filter(s): `drawtext`

## speed

`speed(f: frame, factor: num)`

Change playback speed by factor (video-only in v0).

FFmpeg filter(s): `setpts`

## fade_in

`fade_in(f: frame, dur: num)`

Fade in from black over dur seconds starting at t=0.

FFmpeg filter(s): `fade`

## fade_out

`fade_out(f: frame, dur: num)`

Fade out to black over dur seconds.

FFmpeg filter(s): `fade`
