-- Export a frame-rate-limited image sequence (frame-0001.png, frame-0002.png, ...).
-- variables: source (input media path), rate (frames per second to keep), dest (output pattern path, e.g. frame-%04d.png)
-- example: sqlmpeg compile -f queries/extract-frames.sql -v source=in.mp4 -v rate=1 -v dest=frame-%04d.png
COPY (
  SELECT fps(f.frame, :rate)
  FROM input(:'source') f
) TO :'dest'
