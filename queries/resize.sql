-- Resize to a target width, aspect ratio preserved.
-- variables: source (input media path), width (target width in pixels), dest (output path)
-- example: sqlmpeg compile -f queries/resize.sql -v source=in.mp4 -v width=1280 -v dest=out.mp4
COPY (
  SELECT scale(f.frame, :width, -2), f.audio[1]
  FROM input(:'source') f
) TO :'dest'
