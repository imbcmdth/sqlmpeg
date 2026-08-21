-- Export a file's audio track metadata as CSV, scriptable via STDOUT.
-- variables: source (input media path)
-- example: sqlmpeg -f queries/tracks-to-csv.sql -v source=in.mp4
COPY (
  SELECT t.tags.language, t.codec, t.channel_layout
  FROM input(:'source') f, unnest(f.audio) t
) TO STDOUT WITH (format 'csv', header true)
