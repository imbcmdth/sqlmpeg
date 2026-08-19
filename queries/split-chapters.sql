-- Split a file into one output per chapter, chained into a single run.
-- variables: source (input media path), prefix (output name prefix, e.g. ch)
-- example: sqlmpeg compile -f queries/split-chapters.sql -v source=in.mkv -v prefix=ch
COPY (
  SELECT f.video[1], f.audio[1]
  FROM input(:'source') f, chapters(f) c
  WHERE f.t BETWEEN c.start_t AND c.end_t
) TO (:'prefix' || c.index::text || '.mkv')
