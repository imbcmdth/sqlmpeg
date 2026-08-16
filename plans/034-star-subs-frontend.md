# 034 — RFC-004 wave 2: SELECT *, subtitle/data columns end-to-end
(model: opus · main)

Read plans/rfc-004-star-subtitles.md and plan 033's landed foundation
(ir StreamType/s-d refs, probe mapping, subtitle_codec, avs.mkv fixture).
NOTE: the input-seek TRIM REWRITE IS NOT THIS WAVE (plan 035 owns it —
lower+emit halves must land together); WHERE behavior is UNCHANGED here
except the new rejection below.

## Deliverables
1. parser.py: accept `SELECT *` and `SELECT <alias>.*` (probe sqlglot shapes
   empirically: exp.Star, Column(this=Star)?); mixed with other columns;
   subtitle/data column names join the input-alias whitelist (frame|video|
   audio|subtitle|data|t). Star still rejected inside expressions/functions.
2. lower.py:
   - `a.subtitle[k]` / `a.data[k]` / bare arrays: same machinery as
     video/audio (probed enumeration, bounds, INPUT_NOT_FOUND symbolic).
   - Passthrough-only: subtitle/data-typed value as a FUNCTION argument →
     UDF_ARG_TYPE ("subtitle streams cannot be filtered — only selected");
     they can only become Outputs (or CTE columns that stay passthrough).
   - `SELECT *`: expand to every stream of every FROM alias — FROM-clause
     order, file order within alias, all four types, passthrough columns
     with provenance metadata. Requires probe → INPUT_NOT_FOUND otherwise.
     `alias.*` = one alias. Star over a CTE name: its columns in order
     (static, no probe; array columns splat).
   - WHERE <alias>.t on an alias whose subtitle/data streams are CONSUMED in
     that branch → UNSUPPORTED_SQL ("captions cannot be trimmed yet";
     plan 035 lifts this for input aliases). WHERE on a CTE with s/d columns
     → same rejection (permanent per RFC).
   - Duplicate consumption of one subtitle/data src ref (two Outputs) is
     legal per RFC — see split/emit below.
3. split.py: subtitle/data refs exempt from splitting (never filtergraph
   pads); duplicates pass through untouched.
4. emit.py (narrow): `_TYPE_MARKERS` gains subtitle:"s", data:"d";
   consume-once check exempts subtitle/data SRC refs (repeated bare -map is
   legal ffmpeg); verify copy-suppression covers subtitle_codec (033's
   contract note says it generalizes — add the test 033 had to skip).
   NOTHING else in emit (no -ss/-to — that is 035).
5. Goldens: existing ones must stay byte-identical (star/subs are new
   surface; nothing existing changes). Add 100-star-remux? NO — star needs
   probing, goldens are symbolic; cover in exec. Add ONE symbolic golden:
   095-subtitle-join (explicit subscripts: video[1], audio[1], s.subtitle[1]
   from two inputs + COPY WITH subtitle_codec 'mov_text') — pins s-refs and
   the sink in IR.
6. Tests incl. exec: SELECT * on avs.mkv (3 passthrough outputs, subtitle
   language metadata); a.* mixed with b.audio[1]; vtt join compile+RUN
   (avs.mkv video/audio + subs.en.vtt subtitle → out.mkv, ffprobe 3 streams,
   subtitle language eng); extraction (COPY subtitle[1] TO .srt → run,
   file parses as srt); duplicate subtitle consumption end-to-end (two
   Outputs, same -map twice, runs); function-over-subtitle rejection;
   WHERE+captions rejection; CTE with a subtitle column passthrough.

## Verify
ruff; mypy --strict on changed modules; pytest tests/ -q FULLY green;
pytest -m exec -q green; git diff on pre-existing goldens empty. No git
commands. Report empirical star shapes + contract notes for 035.
