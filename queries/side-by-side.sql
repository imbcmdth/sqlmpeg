-- Pair two files' video tracks by resolution and stack them side by side.
-- variables: main (first input path), second (second input path), dest (output path)
-- example: sqlmpeg compile -f queries/side-by-side.sql -v main=left.mp4 -v second=right.mp4 -v dest=sxs.mp4
COPY (
  SELECT array_agg(hstack(a, b))
  FROM input(:'main') f, input(:'second') g,
       unnest(f.video) a JOIN unnest(g.video) b ON a.width = b.width AND a.height = b.height
) TO :'dest'
