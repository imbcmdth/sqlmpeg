-- Pick a video track by resolution and an audio track by codec, from a remote URL.
-- variables: source (input URL), width (video width, e.g. 1920), height (video height, e.g. 1080), codec (audio codec name, e.g. aac), dest (output path)
-- example: sqlmpeg compile -f queries/remote-tracks.sql -v source=https://example.com/in.m3u8 -v width=1920 -v height=1080 -v codec=aac -v dest=out.mp4
COPY (
  SELECT array_agg(v), array_agg(a)
  FROM input(:'source') f, unnest(f.video) v, unnest(f.audio) a
  WHERE v.width = :width AND v.height = :height AND a.codec = :'codec'
) TO :'dest'
