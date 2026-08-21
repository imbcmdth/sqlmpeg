-- Rotate a quarter turn (phone-video fix).
-- variables: source (input media path), dir (transpose direction: clock, cclock, clock_flip, cclock_flip), dest (output path)
-- example: sqlmpeg compile -f queries/rotate.sql -v source=in.mp4 -v dir=clock -v dest=out.mp4
COPY (
  SELECT transpose(v.video[1], dir => :'dir'), v.audio[1]
  FROM input(:'source') v
) TO :'dest'
