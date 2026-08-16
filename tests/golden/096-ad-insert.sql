SELECT overlay(f.video[1], delay(scale(a.video[1], 0.33), 1), 20, 20) AS frame,
       amix(f.audio[1], volume(delay(a.audio[1], 1), 0.5)) AS sound
FROM input('film.mp4') f, input('ad.mp4') a
