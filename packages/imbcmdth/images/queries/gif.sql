-- Turn a clip into a palette-optimized GIF.
-- variables: source (input media path), width (frame width in pixels, default 480), fps (frame rate to keep, default 12), dest (output .gif path)
-- example: sqlmpeg compile -f packages/imbcmdth/images/queries/gif.sql -v source=clip.mp4 -v dest=clip.gif
COPY (
  WITH small AS (
    SELECT fps(scale(v.video[1], COALESCE(:width, 480), -2), COALESCE(:fps, 12)) AS frame
    FROM input(:'source') v
  )
  SELECT paletteuse(small.frame, palettegen(small.frame))
  FROM small
) TO :'dest'
