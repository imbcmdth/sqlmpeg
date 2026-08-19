-- Rewrite the container's title and artist tags, streams copied untouched.
-- variables: source (input media path), title (new title tag), artist (new artist tag), dest (output file path)
-- example: sqlmpeg compile -f queries/retitle.sql -v source=in.mp4 -v title='My Film' -v artist='Me' -v dest=out.mp4
COPY (
  SELECT f.video[1], f.audio[1], :'title' AS title, :'artist' AS artist
  FROM input(:'source') f
) TO :'dest'
