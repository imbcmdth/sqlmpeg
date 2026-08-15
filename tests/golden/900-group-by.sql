SELECT a.frame
FROM input('clip.mp4') a
GROUP BY a.frame
