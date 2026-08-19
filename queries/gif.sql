-- Turn a clip into a palette-optimized GIF.
-- variables: source (input media path), dest (output .gif path)
-- example: sqlmpeg compile -f queries/gif.sql -v source=clip.mp4 -v dest=clip.gif
COPY (
  WITH small AS (
    SELECT fps(scale(v.frame, 480, -2), 12) AS frame
    FROM input(:'source') v
  )
  SELECT paletteuse(small.frame, palettegen(small.frame))
  FROM small
) TO :'dest'
