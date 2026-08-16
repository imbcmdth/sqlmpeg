SELECT scale(a.frame, 'iw*2')
FROM input('clip.mp4') a
WHERE a.t BETWEEN 1 AND 5
