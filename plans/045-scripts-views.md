# 045 — Parser: scripts + CREATE VIEW  (model: opus · main · RFC-006 wave 1)

Read plans/rfc-006-views-multisink.md. Parser/resolve ONLY — lowering stays
single-sink this wave, so: a script that parses/resolves cleanly but has >1
COPY gets a TEMPORARY typed rejection at the lower boundary
("multiple sinks land in the next wave" — UNSUPPORTED_SQL, tested), keeping
the repo green while 046 builds the core. Single-statement behavior
unchanged. Views ARE fully usable this wave when the script has exactly one
COPY (a view + one sink is legal and useful on its own — lower treats a
view like a cross-statement CTE; that part IS this wave: see below).

## Deliverables
1. parser.py: `parse()` handles scripts — empirically probe sqlglot 30.17
   shapes for: `CREATE VIEW x AS SELECT ...` (exp.Create kind/this/
   expression layout), OR REPLACE / TEMP / MATERIALIZED / IF NOT EXISTS /
   column-list variants (all rejected typed), `stmt; stmt` via
   sqlglot.parse() vs parse_one's Block, semicolon/whitespace edge cases,
   positions available on Create nodes. `Resolved` gains
   `views: dict[name, QueryExpr]` (definition order) and
   `sinks: list[RawSink]` replacing the single `sink` field (SHAPE CHANGE —
   fix all consumers; single-query scripts produce len 0/1 lists).
2. resolve: script rules per the RFC — CREATE VIEWs first then COPYs
   (interleaving rejected? Postgres scripts allow any order but forward
   refs are already banned; simplest rule: views must precede all COPYs —
   reject otherwise, typed, revisit if ugly); flat-namespace uniqueness;
   view bodies validated as full queries (own WITH allowed — adjust the
   nested-WITH rejection to apply only to CTE bodies, not view bodies);
   unused-view detection (a view no later view/COPY references →
   UNSUPPORTED_SQL at its CREATE, "view 'x' is never used"); bare SELECT
   in a multi-statement script rejected; zero-COPY script rejected;
   `ffmpeg` reserved as a view name.
3. lower.py (bounded): views lower exactly like CTEs defined before
   everything (reuse the CTE machinery — a view's columns/arrays/refs
   behave identically; the view name resolves in FROM the same way).
   Multi-COPY scripts hit the temporary rejection AFTER resolve (so parser
   tests can assert clean resolution). Single-COPY-with-views compiles
   end-to-end.
4. Tests: parser script shapes (~20); view semantics via single-sink
   scripts (view+COPY compiles, view referencing view, view with WITH
   inside, unused view rejected, name clashes, reserved name); the
   temporary multi-sink rejection; exec: a view-based single-sink query
   runs. Golden: 099-view-single-sink (symbolic; pins that a view lowers
   into the same IR a CTE would).
5. Docs: none this wave (048 owns).

## Verify
ruff; mypy --strict changed modules; pytest tests/ -q FULLY green; -m exec
green; git diff pre-existing goldens empty (099 new). Baseline 1213 + 96.
No git commands; no version bump. Report Create parse shapes + contract
notes for 046 (the sinks-list shape, where the temporary rejection lives).
