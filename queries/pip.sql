-- Picture-in-picture: a quarter-size inset in the bottom-right corner.
-- variables: main (background video path), overlay (inset video path), dest (output path)
-- example: sqlmpeg compile -f queries/pip.sql -v main=film.mp4 -v overlay=camera.mp4 -v dest=pip.mp4
COPY (
  SELECT overlay(f.frame, scale(c.frame, 'iw/4', -2), 'W-w-20', 'H-h-20'), f.audio[1]
  FROM input(:'main') f, input(:'overlay') c
) TO :'dest'
