-- Drop the audio, keep the picture, no re-encode.
-- variables: source (input media path), dest (output path)
-- example: sqlmpeg compile -f queries/strip-audio.sql -v source=in.mp4 -v dest=out.mp4
COPY (
  SELECT f.video[1]
  FROM input(:'source') f
) TO :'dest'
