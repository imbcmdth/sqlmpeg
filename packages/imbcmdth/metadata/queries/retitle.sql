-- Rewrite the container's title and artist tags, streams copied untouched.
-- Either omitted keeps the file's own tag rather than clearing it.
-- variables: source (input media path), title (new title tag, omit to keep the file's own), artist (new artist tag, omit to keep the file's own), dest (output file path)
-- example: sqlmpeg compile -f packages/imbcmdth/metadata/queries/retitle.sql -v source=in.mp4 -v title='My Film' -v artist='Me' -v dest=out.mp4
COPY (
  SELECT f.video[1], f.audio[1],
         COALESCE(:'title', f.tags.title) AS title,
         COALESCE(:'artist', f.tags.artist) AS artist
  FROM input(:'source') f
) TO :'dest'
