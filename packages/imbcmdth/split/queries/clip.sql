-- Trim a clip to a time range, frame-accurate. Either bound alone is an open-ended trim.
-- variables: source (input media path), start (start time in seconds, default the beginning), end (end time in seconds, default the file's own duration), dest (output path)
-- example: sqlmpeg compile -f packages/imbcmdth/split/queries/clip.sql -v source=in.mp4 -v start=5 -v end=60 -v dest=out.mp4
COPY (
  SELECT a.video[1], a.audio[1]
  FROM input(:'source') a
  WHERE a.t >= COALESCE(:start, 0) AND a.t <= COALESCE(:end, a.duration)
) TO :'dest' WITH (video_codec 'libx264', crf 18, audio_codec 'aac')
