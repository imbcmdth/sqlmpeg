-- Extract each language's audio to its own file - grouped, so a language
-- with several tracks (stereo + 5.1, say) keeps them together in one file.
-- variables: source (input media path), ext (output container extension, e.g. m4a)
-- example: sqlmpeg compile -f queries/extract-languages.sql -v source=in.mp4 -v ext=m4a
COPY (
  SELECT array_agg(t)
  FROM input(:'source') f, unnest(f.audio) t
  GROUP BY t.tags.language
) TO (t.tags.language || '.' || :'ext')
