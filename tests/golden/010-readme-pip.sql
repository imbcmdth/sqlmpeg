WITH pip AS (
  SELECT scale(crop(b.frame, 1200, 50, 600, 200), 0.5) AS frame
  FROM input('game.mp4') b
)
SELECT overlay(a.frame, pip.frame, 20, 20)
FROM input('game.mp4') a, pip
