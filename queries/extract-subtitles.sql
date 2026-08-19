-- Extract the first subtitle track as its own file.
-- variables: source (input media path), dest (output subtitle path)
-- example: sqlmpeg compile -f queries/extract-subtitles.sql -v source=in.mkv -v dest=subs.en.srt
COPY (
  SELECT f.subtitle[1]
  FROM input(:'source') f
) TO :'dest'
