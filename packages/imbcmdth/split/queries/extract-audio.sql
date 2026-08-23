-- Extract audio: a language set writes one file named for it; unset extracts
-- every language, grouped so a language with several tracks (stereo + 5.1,
-- say) keeps them together in one file.
-- variables: source (input media path), language (ISO 639-2 tag, e.g. eng -- omit to extract every language), prefix (output file name prefix, e.g. audio-)
-- example: sqlmpeg compile -f packages/imbcmdth/split/queries/extract-audio.sql -v source=in.mp4 -v language=eng -v prefix=audio-
COPY (
  SELECT array_agg(t)
  FROM input(:'source') f, unnest(f.audio) t
  WHERE t.tags.language = COALESCE(:'language', t.tags.language)
  GROUP BY t.tags.language
) TO (:'prefix' || t.tags.language || '.m4a')
