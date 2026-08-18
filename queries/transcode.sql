-- Transcode a file to H.264/AAC.
-- variables: source (input media path), dest (output file path)
-- example: sqlmpeg compile -f queries/transcode.sql -v source=in.mp4 -v dest=out.mp4
COPY (
  SELECT f.video[1], f.audio[1]
  FROM input(:'source') f
) TO :'dest' WITH (
  video_codec 'libx264', crf 20, preset 'slow',
  audio_codec 'aac', audio_bitrate '192k', faststart true
)
