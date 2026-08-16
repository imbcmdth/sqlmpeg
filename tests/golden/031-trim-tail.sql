SELECT scale(a.frame, 2)
FROM input('clip.mp4') a
WHERE a.t >= 5
