SELECT overlay(a.frame, scale(b.frame, 'iw*0.3'), 50, 50)
FROM input('main.mp4') a, input('logo.mp4') b
