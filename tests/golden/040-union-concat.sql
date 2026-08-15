SELECT a.frame
FROM input('one.mp4') a
UNION ALL
SELECT b.frame
FROM input('two.mp4') b
