# RFC-012 — Metadata editing and chapters

Status: draft 2026-08-20, design settled with maintainer.

## Metadata editing: selected columns ARE the tags

In a media query over track rows, a non-stream column sets a tag on the
row's output stream(s). Row-scoped: the tag applies to every stream the
row produces. The alias is the tag key (free-form; quoted identifiers
for unusual keys - the container decides what survives, same honesty as
filter options). The value is any compile-time expression over the row:
literals, row columns, CASE, `||` concatenation, NULL (clears the tag).
An unselected tag flows through from provenance as today.

    -- normalize a library's language tags
    SELECT t.track,
           CASE WHEN t.language IN ('en','eng','english') THEN 'eng'
                WHEN t.language IN ('fr','fra','french')  THEN 'fra'
                ELSE t.language END AS language
    FROM input(:'src') f, unnest(f.audio) t

    -- borrow a tag across a join; generated titles via ||
    SELECT a.track, b.language AS language,
           'Audio (' || b.language || ')' AS title
    FROM ..., unnest(f.audio) a JOIN unnest(g.audio) b ON ...

Mechanics: tags land in the output's provenance override; emission is
the existing `-metadata:s:<N> key=value` path. Zero filter nodes.
Grammar: CASE and `||` join the compile-time expression evaluator
(WHERE/ON get them too, same grammar). The media-query rejection of
metadata columns narrows to: a non-stream column in a row-less media
query (no unnest) stays rejected.

Container-level tags are sink options: `WITH (title 'My Film',
comment '...')` -> `-metadata title=...`. Small curated set + a
`metadata_<key>`-style escape? NO - free-form container keys via
`WITH (tag_title '...', tag_comment '...')`? DECIDE at plan time; the
simple curated pair title/comment covers the real asks.

## Chapters: read

`chapters(f)` in FROM (table function over an input alias) yields rows
`index, start_t, end_t, title` from `ffprobe -show_chapters`.
Composes with table queries and CSV. Compile-time like all row tables;
unprobeable input rejects. (Chapter-driven splitting waits on scalar
subqueries; the read table + queries/clip.sql covers the manual flow.)

## Chapters: write

Stock Postgres VALUES defines them; a sink option consumes them:

    WITH marks(start_t, end_t, title) AS (
      VALUES (0, 300, 'Intro'), (300, 1500, 'Act One')
    )
    COPY (SELECT ...) TO 'out.mkv' WITH (chapters marks)

Compiles to one extra self-contained input: an ffmetadata file as a
`data:` URI (the empty-captions mechanism) carrying `[CHAPTER]` blocks,
plus `-map_metadata`-style chapter mapping. Also `chapters_from <alias>`
to copy an input's chapters through (`-map_chapters <i>`).

## Waves (after acceptance)

1. Failing cookbook recipes: tag normalization; a borrow-across-join;
   chapters listed as a table; VALUES-defined chapters written.
2. Evaluator: CASE + `||` (+ WHERE/ON parity). Parser: VALUES CTEs,
   chapters(f) FROM shape - sqlglot parse-shape checks first, as always.
3. Tag semantics in lowering + emission; container-tag sink options.
4. Chapters read (probe grows -show_chapters) and write (ffmetadata
   data: URI builder); conflict rules (chapters + chapters_from: reject).
5. Docs (orchestrator): tracks.md section, cookbook prose, README
   bullet touch.

## Non-goals

Editing tags in place without a remux (that is mkvpropedit's job, not
ffmpeg's); chapter-driven automatic splitting (scalar subqueries,
queued); reading/writing attachments.
