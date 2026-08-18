# RFC-008 — Track alignment: sqlmpeg.tracks_union / tracks_intersection

Status: draft 2026-08-17. The first array-consuming macros (the queued
"variadic array-consuming generalization", landed as a concrete feature).

## The problem

Concatenating or mixing two inputs with different audio track counts is a
wall today: `UNION ALL` demands identical column shapes (CONCAT_MISMATCH)
and zipping demands equal lengths (BROADCAST_MISMATCH). The manual fix -
figure out which side has more tracks, mint silence for the other, keep
the language tags straight, only then splat - is exactly the bookkeeping
sqlmpeg exists to delete.

## The functions

Two array-in, array-out macros in the sqlmpeg namespace (v1: AUDIO arrays
only; both are rejections on video arrays with a fence hint):

    sqlmpeg.tracks_union(arr1, arr2, ..., by => 'language', take => <k>, duration => <s>)
    sqlmpeg.tracks_intersection(arr1, arr2, ..., by => 'language', take => <k>)

The **first argument is the canonical source**: it defines the track
ORDER (track order is player-visible surface - players default to the
first audio track, which is why files lead with English - so it is never
resorted). The union template is arr1's keys in arr1's order, then keys
arr1 lacks appended in their own source's order, sources in argument
order; the intersection template is arr1's order filtered to the keys
every argument has.

**`take => k`** (1-based over the arguments, default 1) selects WHOSE
tracks come back: argument k's tracks, matched by key and reordered to
the canonical template. For `tracks_union`, keys argument k has no track
for get a **silence fill** (anullsrc node, finite duration, the
counterpart's sample_rate and language tag). `tracks_intersection` never
fills - the template only holds keys everyone has.

Every cooperating call site writes the SAME argument list and varies only
`take`, so all sites share one template by construction:

    -- concat: one template (f's order + g's extras), each branch takes its own side
    SELECT f.video[1], sqlmpeg.tracks_union(f.audio, g.audio)            FROM input('a.mkv') f
    UNION ALL
    SELECT g.video[1], sqlmpeg.tracks_union(f.audio, g.audio, take => 2) FROM input('b.mkv') g

    -- pairwise mix by language, silence where one side is missing one
    SELECT amix(sqlmpeg.tracks_union(f.audio, p.audio),
                sqlmpeg.tracks_union(f.audio, p.audio, take => 2))
    FROM input('film.mkv') f, input('pip.mkv') p

(An earlier draft ordered the template by sorted key so that swapped
argument lists would agree; rejected - it resorts the player-visible
track order. Source-canonical order plus an explicit `take` keeps arr1's
order authoritative AND keeps cross-site pairing correct: swapped-list
calls under source order would cross-pair eng with fra whenever the two
files order their tracks differently.)

## Matching

`by => 'language'` is the default: tracks pair by their probed language
tag. When NO track on any side carries a tag, language matching falls
back to index automatically. `by => 'index'` forces positional: union
pads to the longest array, intersection truncates to the shortest -
`take` still selects the side, order is index order either way. Duplicate
tags within one array are a typed rejection under `by => 'language'`
(there is no honest way to pair two French tracks against one).

## The silence fill

A `tracks_union` fill is one `anullsrc` node (zero inputs, like any
generated source in the IR): `duration` from the probed container
duration of the TAKEN argument's input (the side the fill stands in for -
its siblings set the segment length), `sample_rate` from the counterpart
track that put the key in the union (when probed), language tag threaded
as the fill's provenance so `-metadata:s:N language=X` emits exactly as
if the track existed. `duration => <seconds>` overrides; with neither a probed
duration nor the option, a typed rejection says to pass one (an infinite
silence track would hang concat - not shipped broken).

## Fences (v1)

- Audio arrays only. Video union means black-fill semantics (resolution/
  fps decisions) - deferred until it earns its keep.
- Arguments are bare input arrays (`<alias>.audio`), not CTE columns: the
  fill needs the backing file's duration and the elements' probed tags,
  which a bare array carries and a CTE column may not. Typed rejection
  with that explanation.
- Inputs must be probeable (bare arrays already require this - lengths).

## Plumbing this needs

1. `probe.py`: `ProbeResult.duration: float | None` from ffprobe's
   `-show_format` (opportunistic, like everything probed). ~10 lines.
2. A second macro table (`ARRAY_MACROS` in `sqlmpeg/macros.py`): variadic
   array params + named options - the existing `MACROS` shape doesn't fit
   (fixed positional scalars, no options). `_lower_macro_call` checks
   both tables; unknown names did-you-mean across both.
3. Lowering returns a `_Value` array whose fill elements are `_Stream`s
   over freshly minted anullsrc nodes with `source=` the counterpart's
   `StreamMeta` (provenance and `-metadata` emission come free).
   Downstream (split/emit/goldens) sees ordinary nodes - no changes.

## Waves

- 056 (sonnet): probe duration + `ARRAY_MACROS` mechanism + both macros +
  tests (unit: alignment/order/fill/provenance/errors against synthetic
  probes; exec: the concat and the pairwise-mix pipelines against
  two-language fixtures, output track count/tags/duration ffprobe-checked).
- 057: docs (mine - filters.md section, cookbook recipe, README bullet
  touch), prompt.py mention (agent-eligible), goldens if any.

## Non-goals

Video fill; CTE-column arguments; `order =>` beyond the canonical rule
(revisit if asked); resolution/fps keys for `by` (video-era concerns);
channel-layout matching on fills (StreamMeta doesn't carry it; anullsrc
default stereo, documented).
