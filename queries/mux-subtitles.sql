-- Mux an external subtitle file in as its own track.
-- variables: main (video path), subs (subtitle file path), dest (output path, mp4 needs mov_text)
-- example: sqlmpeg compile -f queries/mux-subtitles.sql -v main=film.mp4 -v subs=subs.en.vtt -v dest=captioned.mp4
COPY (
  SELECT f.video[1], f.audio[1], s.subtitle[1]
  FROM input(:'main') f, input(:'subs') s
) TO :'dest' WITH (subtitle_codec 'mov_text')
