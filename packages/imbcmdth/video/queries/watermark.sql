-- Overlay a logo image, centered and unscaled by default, for the whole clip.
-- variables: main (video path), overlay (logo image path), x (overlay x position, an ffmpeg expression, e.g. 20), y (overlay y position, an ffmpeg expression, e.g. 20), scale (logo width, an ffmpeg scale expression, e.g. iw/2 for half size), dest (output path)
-- example: sqlmpeg compile -f packages/imbcmdth/video/queries/watermark.sql -v main=film.mp4 -v overlay=watermark.png -v dest=branded.mp4
COPY (
  SELECT overlay(f.video[1], scale(logo.video[1], COALESCE(:'scale', 'iw'), -1),
                  COALESCE(:'x', '(W-w)/2'), COALESCE(:'y', '(H-h)/2')),
         f.audio[1]
  FROM input(:'main') f, input(:'overlay', loop => true) logo
) TO :'dest'
