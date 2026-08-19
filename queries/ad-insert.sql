-- Splice a clip into the main video at a timestamp.
-- variables: main (main video path), insert (clip to splice in), cut (splice point in seconds), dest (output path)
-- example: sqlmpeg compile -f queries/ad-insert.sql -v main=film.mp4 -v insert=promo.mp4 -v cut=120 -v dest=spliced.mp4
COPY (
  SELECT f.video[1], f.audio[1] FROM input(:'main') f WHERE f.t <= :cut
  UNION ALL
  SELECT ad.video[1], ad.audio[1] FROM input(:'insert') ad
  UNION ALL
  SELECT g.video[1], g.audio[1] FROM input(:'main') g WHERE g.t >= :cut
) TO :'dest'
