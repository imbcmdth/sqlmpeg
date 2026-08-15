WITH c AS (
  SELECT scale(a.frame, 0.5) AS frame
  FROM input('clip.mp4') a
)
SELECT overlay(c.frame, c.frame, 10, 10)
FROM c
