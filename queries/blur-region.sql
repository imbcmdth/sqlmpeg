-- Blur a rectangular region for the whole clip (the license-plate special).
-- variables: source (input media path), dest (output path)
-- example: sqlmpeg compile -f queries/blur-region.sql -v source=interview.mp4 -v dest=anonymized.mp4
COPY (
  SELECT sqlmpeg.blur_regions(f.video[1], 900, 60, 320, 180, 20), f.audio[1]
  FROM input(:'source') f
) TO :'dest'
