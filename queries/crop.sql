-- Crop to a fixed rectangle.
-- variables: source (input media path), w (crop width), h (crop height), x (crop left edge), y (crop top edge), dest (output path)
-- example: sqlmpeg compile -f queries/crop.sql -v source=in.mp4 -v w=640 -v h=480 -v x=100 -v y=50 -v dest=out.mp4
COPY (
  SELECT crop(f.frame, out_w => :w, out_h => :h, x => :x, y => :y), f.audio[1]
  FROM input(:'source') f
) TO :'dest'
