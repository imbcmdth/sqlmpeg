-- Transcode a file, every stream carried through, codecs and quality all optional.
-- variables: source (input media path), dest (output file path), vcodec (video codec, e.g. libx264), crf (quality target, lower is bigger/better, e.g. 20), preset (encoder speed/quality preset, e.g. slow), video_bitrate (target video bitrate, e.g. 4M), acodec (audio codec, e.g. aac), audio_bitrate (target audio bitrate, e.g. 192k)
-- example: sqlmpeg compile -f packages/imbcmdth/encode/queries/transcode.sql -v source=in.mp4 -v dest=out.mp4 -v vcodec=libx264 -v crf=20 -v preset=slow -v acodec=aac -v audio_bitrate=192k
COPY (
  SELECT f.video, f.audio, f.subtitle
  FROM input(:'source') f
) TO :'dest' WITH (
  video_codec :'vcodec', crf :crf, preset :'preset', video_bitrate :'video_bitrate',
  audio_codec :'acodec', audio_bitrate :'audio_bitrate'
)
