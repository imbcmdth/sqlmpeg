# RFC-006 — Views, multiple sinks, and channelsplit

Status: accepted 2026-08-16. Target v0.9.0.
Waves: 045 parser scripts+views → 046 multi-sink core → 047 channelsplit →
048 polish.

## Views + multiple outputs (the ABR ladder)

A script is `CREATE VIEW name AS <query>;`* followed by `COPY (...) TO
'<path>' WITH (...);`+ — valid Postgres as a script, compiled as ONE ffmpeg
invocation with one output group per COPY:

```sql
CREATE VIEW master AS
  SELECT overlay(...) AS v, amix(...) AS a FROM input('film.mkv') f, ...;

COPY (SELECT scale(m.v, 1920, -2), m.a FROM master m) TO '1080.mp4' WITH (...);
COPY (SELECT scale(m.v, 1280, -2), m.a FROM master m) TO '720.mp4'  WITH (...);
COPY (SELECT m.a FROM master m) TO 'audio.m4a' WITH (...);
```

- A view is to STATEMENTS what a CTE is to branches: a named shared subgraph.
  Decode once; the split pass fans view pads across every consumer.
- Namespace: view names share the flat alias/CTE namespace (unique;
  `ffmpeg` stays reserved). Views may reference earlier views, no forward
  refs. A view BODY is a full query: its own WITH is allowed (it is a
  statement), same dialect surface otherwise. View columns are its SELECT's
  AS names (CTE-column rules).
- Rejected, typed: OR REPLACE, TEMP/TEMPORARY, MATERIALIZED, RECURSIVE,
  IF NOT EXISTS, view column lists, DROP/ALTER anything, a bare SELECT
  inside a multi-statement script (only COPY carries a destination), a
  script with zero COPYs, an UNUSED view (typo guard; line-anchored).
- Single-statement queries behave exactly as today (bare SELECT + -o, or
  one COPY).

## IR / emit / CLI

- MIGRATION: `Graph.outputs` + `Graph.sink` are replaced by
  `Graph.sinks: list[SinkUnit]`, `SinkUnit = {outputs: list[Output],
  path: str | None, options: dict}` (path None only for the bare-SELECT
  single-sink case). to_dict emits `"sinks": [...]`; ALL goldens regen
  (mechanical shape change; eyeball per usual). Split/consume-once count
  outputs across all sinks (union).
- Emit: `Emitted.groups` mirrors sinks; build_ffmpeg_args renders ffmpeg's
  native multi-output form — per group: maps, per-stream options, sink
  options, then the path. Output stream indices are per-FILE (ffmpeg
  numbering restarts per output) — the -c:<i>/-metadata:s:<i> logic
  becomes per-group.
- CLI: `-o` legal only when there is exactly one sink (error otherwise,
  naming the sinks found); `run` executes the one command; `compile`
  prints it; `explain` shows the sinks list.

## channelsplit (the one multi-output filter with a natural home)

`ffmpeg.channelsplit(a.audio[1])` returns `audio[]` — the first
array-RETURNING call. Element count comes from the `channel_layout` option
(default 'stereo' → 2), resolved against a curated layout→count table kept
as data (mono 1, stereo 2, 2.1 3, quad 4, 5.0 5, 5.1 6, 7.1 8, ...);
unknown layout value → FILTER_OPTION_TYPE listing the table. The node is
Node(outputs=["audio"]*N) — multi-pad nodes are established IR (split,
concat). The result value splats/subscripts/broadcasts via the existing
array machinery; provenance threads the single source stream to every
element. Remains namespace-only (it is fenced from bare tier-2 by pad
shape; the special case lives in lower, table-driven).

## Non-goals

DROP/schema management; view persistence of any kind; tee-muxer
same-encode fanout (each COPY encodes independently; sharing an encode
between containers is a later optimization); other dynamic-pad filters
(amerge/join stay fenced); cross-file output stream references.
