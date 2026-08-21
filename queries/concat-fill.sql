-- Concatenate two files whose audio track counts differ, filling gaps with silence.
-- variables: main (first file path), second (second file path), dest (output path)
-- example: sqlmpeg compile -f queries/concat-fill.sql -v main=part1.mp4 -v second=part2.mp4 -v dest=joined.mp4
COPY (
  SELECT f.video[1], array_agg(a)
  FROM input(:'main') f, input(:'second') g,
       unnest(f.audio) a FULL OUTER JOIN unnest(g.audio) b ON a.language = b.language
  GROUP BY f.video[1]
  UNION ALL
  SELECT g2.video[1], array_agg(COALESCE(b2, ffmpeg.anullsrc()))
  FROM input(:'main') f2, input(:'second') g2,
       unnest(f2.audio) a2 FULL OUTER JOIN unnest(g2.audio) b2 ON a2.language = b2.language
  GROUP BY g2.video[1]
) TO :'dest'
