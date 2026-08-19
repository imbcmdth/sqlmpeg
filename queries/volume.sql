-- Turn the volume up or down on every audio track.
-- variables: source (input media path), gain (volume multiplier, e.g. 0.5 for half, 2 for double), dest (output path)
-- example: sqlmpeg compile -f queries/volume.sql -v source=in.mp4 -v gain=0.5 -v dest=out.mp4
COPY (
  SELECT f.video[1], volume(f.audio, :gain)
  FROM input(:'source') f
) TO :'dest'
