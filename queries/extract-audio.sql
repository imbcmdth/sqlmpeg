-- Extract the audio track matching a language, picked by row rather than index.
-- variables: source (input media path), language (ISO 639-2 tag, e.g. eng), dest (output audio path)
-- example: sqlmpeg compile -f queries/extract-audio.sql -v source=in.mp4 -v language=eng -v dest=out.m4a
COPY (
  SELECT t.track
  FROM input(:'source') f, unnest(f.audio) t
  WHERE t.language = :'language'
) TO :'dest'
