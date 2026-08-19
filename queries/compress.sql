-- Shrink a file's size with a variable quality target.
-- variables: source (input media path), crf (quality target, lower is bigger/better, e.g. 23), dest (output path)
-- example: sqlmpeg compile -f queries/compress.sql -v source=in.mp4 -v crf=23 -v dest=out.mp4
COPY (
  SELECT f.video[1], f.audio[1]
  FROM input(:'source') f
) TO :'dest' WITH (video_codec 'libx264', crf :crf, audio_codec 'aac')
