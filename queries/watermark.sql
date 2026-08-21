-- Overlay a logo image, centered, for the whole clip.
-- variables: main (video path), overlay (logo image path), dest (output path)
-- example: sqlmpeg compile -f queries/watermark.sql -v main=film.mp4 -v overlay=watermark.png -v dest=branded.mp4
COPY (
  SELECT overlay(f.video[1], logo.video[1], '(W-w)/2', '(H-h)/2'), f.audio[1]
  FROM input(:'main') f, input(:'overlay', loop => true) logo
) TO :'dest'
