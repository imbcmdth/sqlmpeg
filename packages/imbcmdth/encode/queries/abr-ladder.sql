-- Rendition ladder: one decode, three output qualities, rung widths optional.
-- variables: source (input media path), high (1080p output path), mid (720p output path), low (480p output path), high_w (high rung width, default 1920), mid_w (mid rung width, default 1280), low_w (low rung width, default 854)
-- example: sqlmpeg compile -f packages/imbcmdth/encode/queries/abr-ladder.sql -v source=in.mp4 -v high=1080p.mp4 -v mid=720p.mp4 -v low=480p.mp4
CREATE VIEW decoded AS
  SELECT f.video[1] AS v, f.audio[1] AS a
  FROM input(:'source') f;

COPY (SELECT scale(d.v, COALESCE(:high_w, 1920), -2) AS v, d.a FROM decoded d)
TO :'high' WITH (video_codec 'libx264', crf 20, audio_codec 'aac');

COPY (SELECT scale(d.v, COALESCE(:mid_w, 1280), -2) AS v, d.a FROM decoded d)
TO :'mid' WITH (video_codec 'libx264', crf 22, audio_codec 'aac');

COPY (SELECT scale(d.v, COALESCE(:low_w, 854), -2) AS v, d.a FROM decoded d)
TO :'low' WITH (video_codec 'libx264', crf 24, audio_codec 'aac')
