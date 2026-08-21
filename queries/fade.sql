-- Fade in from black at the start (fade-out needs the file's length, not known at compile time).
-- variables: source (input media path), duration (fade length in seconds), dest (output path)
-- example: sqlmpeg compile -f queries/fade.sql -v source=in.mp4 -v duration=1 -v dest=out.mp4
COPY (
  SELECT fade(f.video[1], type => 'in', duration => :duration), f.audio[1]
  FROM input(:'source') f
) TO :'dest'
