-- Burn subtitles into the picture (not muxed, rendered into the pixels).
-- variables: source (input media path), subs (subtitle file path, read when ffmpeg runs), dest (output path)
-- example: sqlmpeg compile -f queries/burn-subtitles.sql -v source=in.mp4 -v subs=subs.en.srt -v dest=out.mp4
COPY (
  SELECT subtitles(f.video[1], filename => :'subs'), f.audio[1]
  FROM input(:'source') f
) TO :'dest'
