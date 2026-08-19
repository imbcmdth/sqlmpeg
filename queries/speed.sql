-- Speed a clip up or down, picture and sound together.
-- variables: source (input media path), factor (speed multiplier, e.g. 2), dest (output path)
-- example: sqlmpeg compile -f queries/speed.sql -v source=in.mp4 -v factor=2 -v dest=out.mp4
COPY (
  SELECT sqlmpeg.speed(f.frame, :factor), atempo(f.audio[1], :factor)
  FROM input(:'source') f
) TO :'dest'
