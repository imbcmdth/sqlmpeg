-- Duck background music under dialogue with a sidechain compressor.
-- variables: main (dialogue video path), music (music track path), dest (output path)
-- example: sqlmpeg compile -f queries/duck.sql -v main=film.mp4 -v music=music.m4a -v dest=ducked.mp4
COPY (
  SELECT v.video[1], amix(v.audio[1], sidechaincompress(m.audio[1], v.audio[1], threshold => 0.03, ratio => 8))
  FROM input(:'main') v, input(:'music') m
) TO :'dest'
