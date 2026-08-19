-- Rendition ladder: one decode, three output qualities.
-- variables: source (input media path), high (1080p output path), mid (720p output path), low (480p output path)
-- example: sqlmpeg compile -f queries/abr-ladder.sql -v source=in.mp4 -v high=1080p.mp4 -v mid=720p.mp4 -v low=480p.mp4
CREATE VIEW decoded AS
  SELECT f.frame AS v, f.audio[1] AS a
  FROM input(:'source') f;

COPY (SELECT scale(d.v, 1920, -2) AS v, d.a FROM decoded d)
TO :'high' WITH (video_codec 'libx264', crf 20, audio_codec 'aac');

COPY (SELECT scale(d.v, 1280, -2) AS v, d.a FROM decoded d)
TO :'mid' WITH (video_codec 'libx264', crf 22, audio_codec 'aac');

COPY (SELECT scale(d.v, 854, -2) AS v, d.a FROM decoded d)
TO :'low' WITH (video_codec 'libx264', crf 24, audio_codec 'aac')
