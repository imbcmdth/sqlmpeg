-- Resize to a target width, aspect ratio preserved.
-- variables: source (input media path), width (target width in pixels), height (target height in pixels, default -2 keeps the aspect ratio), dest (output path)
-- example: sqlmpeg compile -f packages/imbcmdth/video/queries/resize.sql -v source=in.mp4 -v width=1280 -v dest=out.mp4
COPY (
  SELECT scale(f.video[1], :width, COALESCE(:height, -2)), f.audio[1]
  FROM input(:'source') f
) TO :'dest'
