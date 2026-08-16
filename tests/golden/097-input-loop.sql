SELECT overlay(f.frame, logo.frame, 20, 20)
FROM input('film.mp4') f, input('logo.png', loop => true, framerate => 15) logo
