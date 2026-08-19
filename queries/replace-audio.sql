-- Replace a video's audio track with another file's.
-- variables: main (video path), voice (replacement audio path), dest (output path)
-- example: sqlmpeg compile -f queries/replace-audio.sql -v main=film.mp4 -v voice=voiceover.wav -v dest=dubbed.mp4
COPY (
  SELECT v.video[1], m.audio[1]
  FROM input(:'main') v, input(:'voice') m
) TO :'dest'
