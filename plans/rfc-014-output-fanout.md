# RFC-014 — Set-driven output fan-out  (draft; NOT started)

Maintainer insight: compile already emits command sequences (0.17.0),
so "one output per chapter" is just one command per row, `&&`-chained.
SQL operates over sets; the output side now can too.

## The rule
In a media COPY whose FROM holds a compile-time row table, a `TO` that
is an EXPRESSION referencing that table's columns means: one ffmpeg
command per surviving row, each with that row's values bound. A
constant `TO` keeps today's semantics unchanged (tracks splat into one
file; chapters in a media query stay rejected).

    -- one file per chapter
    COPY (
      SELECT f.video[1], f.audio[1]
      FROM input(:'src') f, chapters(f) c
      WHERE f.t BETWEEN c.start_t AND c.end_t
    ) TO ('ch' || c.index || '-' || c.title || '.mkv') WITH (...)

    -- one file per language
    COPY (SELECT t.track FROM input(:'src') f, unnest(f.audio) t)
    TO (t.language || '.m4a')

- The trim window may reference row columns (`WHERE f.t BETWEEN
  c.start_t AND c.end_t`): per-row seek bounds, still an input seek.
- Path expressions use the existing value grammar (||, CASE, columns,
  literals). Path separators in a computed path: typed rejection with a
  hint (no directory traversal from metadata). Duplicate computed paths
  across rows: typed rejection naming the collision.
- Cross products compose (chapters x tracks = one file per pair).
  Row counts are small (dozens); no cap needed beyond the rejection on
  zero surviving rows.
- Interactions, v1: two_pass + fan-out reject; multi-COPY scripts with
  a fan-out COPY reject; table/CSV queries unaffected.
- run executes the sequence (exists); wrapping handles && (exists);
  -v variables compose in the path expression.

## When started
Failing recipes first: split-by-chapter (the headline) and
per-language extraction. Then likely two waves: parser/lower (TO
expressions, per-row binding, rejections) and emission (per-row command
rendering) - or one opus wave if the seams stay small. Update
queries/ with split-chapters.sql and extract-languages.sql after green.
