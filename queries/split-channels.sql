-- Split a stereo track into two mono files.
-- variables: source (input media path), dest (output path, e.g. channels.mkv)
-- example: sqlmpeg compile -f queries/split-channels.sql -v source=stereo.mp4 -v dest=channels.mkv
COPY (
  SELECT ffmpeg.channelsplit(a.audio[1])
  FROM input(:'source') a
) TO :'dest'
