-- Extract the video track, no re-encode -- drops the audio because it keeps only the picture.
-- variables: source (input media path), dest (output path)
-- example: sqlmpeg compile -f packages/imbcmdth/split/queries/extract-video.sql -v source=in.mp4 -v dest=out.mp4
COPY (
  SELECT f.video[1]
  FROM input(:'source') f
) TO :'dest'
