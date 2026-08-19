-- Normalize loudness on every audio track at once, language tags preserved.
-- variables: source (input media path), dest (output path)
-- example: sqlmpeg compile -f queries/loudnorm-all.sql -v source=in.mp4 -v dest=out.mkv
COPY (
  SELECT f.video[1], ffmpeg.loudnorm(f.audio, I => -23)
  FROM input(:'source') f
) TO :'dest'
