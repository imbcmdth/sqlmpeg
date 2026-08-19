-- Extract every audio track to its own file, named by its own language tag.
-- variables: source (input media path), ext (output container extension, e.g. m4a)
-- example: sqlmpeg compile -f queries/extract-languages.sql -v source=in.mp4 -v ext=m4a
COPY (
  SELECT t.track
  FROM input(:'source') f, unnest(f.audio) t
) TO (t.language || '.' || :'ext')
