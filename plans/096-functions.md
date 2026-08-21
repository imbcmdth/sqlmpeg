# 096 — Functions: reusable, parameterized query fragments

Runs after 095 (the type system supplies `RETURNS` a vocabulary) and
after 094. Ships in 0.26.0 with both.

The construct is Postgres's set-returning / scalar SQL function - a
parameterized view, which is what a view cannot be. Verified to parse
today: `CREATE FUNCTION f(a text) RETURNS TABLE(...) AS $$ ... $$
LANGUAGE sql`, and a call in FROM. (`PREPARE` is the wrong door: it is
a top-level statement, cannot appear in FROM, and sqlglot only keeps
it as an unparsed command.)

## Two flavors, each in its proper position

**Value-returning** - `RETURNS text` / `number` / `boolean` /
`<stream>_stream` / `<stream>_stream[]` / `chapter[]` / `cue[]`:
legal anywhere a value of that type is legal. The SELECT list, WHERE,
tag columns, fan-out destinations.

    CREATE FUNCTION normalize_lang(raw text) RETURNS text AS $$
      SELECT CASE WHEN raw IN ('en','english') THEN 'eng' ELSE raw END
    $$ LANGUAGE sql;

    SELECT t, normalize_lang(t.tags.language) AS language
    FROM input(:'src') f, unnest(f.audio) t

A `<kind>_stream[]` return SPLATS in the SELECT list exactly as a bare
array column does - same rule, different producer; `<kind>_stream`
is one stream, one `-map`. No new output model.

**Table-returning** - `RETURNS TABLE(...)`: FROM only, never the
SELECT list. Postgres allows `(f(x)).a` there but evaluates `f` once
PER FIELD ACCESS; for sqlmpeg that is not a performance wart but a
correctness bug - input identity is the ALIAS, so two expansions mint
two `-i` entries for one file. A set-returning call in the SELECT list
would also multiply rows, colliding with the one-row rule. Typed
rejection with a hint naming the FROM form.

    CREATE FUNCTION tagged_audio(file text, lang text)
    RETURNS TABLE(track audio_stream, ...) AS $$ ... $$ LANGUAGE sql;

    SELECT v.video[1], t FROM input(:'src') v,
           tagged_audio('some.wav','eng') AS t

## Semantics

- **Compile-time expansion.** Each call site inlines the body with
  arguments bound, then compiles as usual. No runtime concept, nothing
  new in the IR.
- **Hygiene is mandatory.** Names live in ONE flat script-wide
  namespace, so a body containing `a` breaks the moment the function
  is called twice. Expansion rewrites the body's internal aliases per
  call site (`a` -> `t__a`). A body may not reference an outer alias.
- **Diagnostics through two layers**: errors anchor on the CALL SITE
  with the body's line as secondary context. A rejection that points
  only inside an expanded body is useless.
- **Tags ride the stream.** A body that sets a tag column produces a
  stream carrying it, the way CTE body tags already do (084's
  mechanism, keyed by stream identity). That is what makes
  `RETURNS audio_stream` sufficient for the tagged case without a
  separate stream-plus-tags type.
- **Arguments** are values (the 095 value grammar), including `-v`
  variables. `input()` is NOT callable in an argument or anywhere in
  a value position: it is a FROM item that mints an `-i`, and a table
  reference in the SELECT list is not SQL. A function that needs a
  file takes its PATH as text and calls `input()` in its own FROM.
- **Recursion** (direct or mutual): typed rejection.
- **Overloading**: not supported; a duplicate name is a rejection.
  One name, one signature.
- Functions live in the same flat namespace as views, CTEs and
  aliases, and are visible to every statement after their definition
  in the script.
- `CREATE FUNCTION` outside a script (a bare function definition with
  no query) compiles to nothing: a typed rejection, like an unread
  view.

## Waves

1. Recipes red first: a scalar function (language normalizer used in
   a tag column) and a table function (the tagged-audio shape), both
   in docs/examples.md; queries/ gains one program that defines and
   uses a function.
2. Value-returning functions (parser: CREATE FUNCTION, dollar-quoted
   bodies, RETURNS over the 095 types; expansion + hygiene;
   diagnostics). Smaller, no row-multiplicity questions.
3. Table-returning functions (FROM-position calls, row sources, the
   SELECT-list rejection).
4. Docs: dialect.md gains the statement form, rows.md the function
   row source, prompt.py the surface; release.

## Follow-on, NOT in this plan

Cross-file reuse: `\i lib/tracks.sql` (psql's own include, so no
invented syntax) turns queries/ from a catalog of programs into a
library. That is where functions pay off most, and it is the shape the
engine wants for user-defined pipeline fragments - but it is a
separate plan.
