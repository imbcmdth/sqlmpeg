-- Crossfade from one clip into another, video and audio together.
-- variables: first (first clip path), second (second clip path), dest (output path)
-- example: sqlmpeg compile -f queries/crossfade.sql -v first=one.mp4 -v second=two.mp4 -v dest=dissolve.mp4
COPY (
  SELECT xfade(a.frame, b.frame, duration => 1, offset => 9),
         acrossfade(a.audio[1], b.audio[1], duration => 1)
  FROM input(:'first') a, input(:'second') b
) TO :'dest'
