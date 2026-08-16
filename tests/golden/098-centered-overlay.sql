SELECT overlay(f.frame, p.frame, '(W-w)/2', '(H-h)/2')
FROM input('film.mp4') f, input('logo.png') p
