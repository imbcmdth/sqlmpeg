-- Normalize loudness on every audio track at once, language tags preserved.
-- variables: source (input media path), i (target integrated loudness in LUFS, e.g. -23), tp (true peak ceiling in dBTP, e.g. -2), lra (loudness range in LU, e.g. 7), dest (output path)
-- example: sqlmpeg compile -f packages/imbcmdth/audio/queries/loudnorm-all.sql -v source=in.mp4 -v i=-23 -v dest=out.mkv
COPY (
  SELECT f.video[1], ffmpeg.loudnorm(f.audio, I => :i, TP => :tp, LRA => :lra)
  FROM input(:'source') f
) TO :'dest'
