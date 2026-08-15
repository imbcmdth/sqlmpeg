SELECT scale(crop(a.frame, 10, 20, 300, 400), 640, 480)
FROM input('clip.mp4') a
