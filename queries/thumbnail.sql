-- Grab a single poster frame at a timestamp.
-- variables: source (input media path), at (seek time in seconds), dest (output image path)
-- example: sqlmpeg compile -f queries/thumbnail.sql -v source=in.mp4 -v at=90 -v dest=poster.png
COPY (
  SELECT f.video[1] FROM input(:'source') f WHERE f.t >= :at
) TO :'dest' WITH (video_codec 'png', frames 1)
