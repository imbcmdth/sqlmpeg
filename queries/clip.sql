-- Trim a clip to a time range, frame-accurate.
-- variables: source (input media path), start (start time in seconds), end (end time in seconds), dest (output path)
-- example: sqlmpeg compile -f queries/clip.sql -v source=in.mp4 -v start=5 -v end=60 -v dest=out.mp4
COPY (
  SELECT a.video[1], a.audio[1]
  FROM input(:'source') a
  WHERE a.t BETWEEN :start AND :end
) TO :'dest' WITH (video_codec 'libx264', crf 18, audio_codec 'aac')
