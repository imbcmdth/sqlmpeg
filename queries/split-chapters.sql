-- Split a file into one output per chapter, in a single run. Stream copies,
-- so the cuts snap to keyframes; add WITH (video_codec ..., audio_codec ...)
-- for frame-accurate ones out of a single decode.
-- variables: source (input media path), prefix (output name prefix, e.g. ch)
-- example: sqlmpeg compile -f queries/split-chapters.sql -v source=in.mkv -v prefix=ch
COPY (
  SELECT f.video[1], f.audio[1]
  FROM input(:'source') f, chapters(f) c
  WHERE f.t BETWEEN c.start_t AND c.end_t
) TO (:'prefix' || c.index::text || '.mkv')
