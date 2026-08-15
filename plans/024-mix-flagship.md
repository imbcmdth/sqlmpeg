# 024 — PiP composite + broadcast-zip mix as the flagship  (model: sonnet · main)

User direction (2026-08-15, two messages): the flagship should MIX two
multi-language sources with differing volumes via splatting, AND composite the
video as picture-in-picture. Verified working today (av2+av3):

```sql
WITH pip AS (
  SELECT scale(c.frame, 0.25) AS frame, c.audio AS sound
  FROM input('commentary.mkv') c
)
SELECT overlay(f.frame, pip.frame, 20, 20),
       amix(volume(f.audio, 0.65), volume(pip.sound, 0.35))
FROM input('film.mkv') f, pip
```
→ scale→overlay video chain, volume broadcast over each array (4 nodes), amix
zips the pairs (2 nodes), three outputs. The CTE carries a scalar video column
AND an array audio column. Missing: mixed outputs carry no language tags (amix
drops provenance unconditionally).

## Deliverables
1. `sqlmpeg/lower.py` — generalize provenance agreement to multi-stream-input
   CALLS: in `_lower_call`'s provenance threading, when a call has MORE than
   one stream argument, gather the input `_Stream`s and apply the existing
   `_agreed_source` (same rule as concat: all filtered provenance dicts
   non-empty and identical → thread first source; else None). The
   single-stream path is unchanged. Applies to amix and overlay alike (mixing
   two eng streams yields an eng stream; compositing two tagged video streams
   likewise). Update the three provenance docstrings (module header,
   `_Stream.source`, `_provenance`) — the rule is now uniform: 1:1 chains
   thread; multi-stream joins (amix/overlay/concat) thread only on agreement.
2. Tests (`tests/test_lower.py`): amix agreement (av2+av3 pairwise → outputs
   carry eng/fra, exec); amix disagreement (f.audio[1] eng + c.audio[2] fra →
   {}); overlay agreement path (probed, two video streams same source → tag
   kept — use av2 twice under two aliases); unprobed side → {}. Adjust any
   existing test that pinned "amix always drops provenance".
3. README: the PiP+mix query above becomes the HEADLINE (paths film.mkv /
   commentary.mkv; shown command = real compilation against av2/av3 with
   paths genericized — after deliverable 1 it should include
   -metadata:s:1 language=eng / -metadata:s:2 language=fra; verify and show
   the real thing). This SUPERSEDES the old game.mp4 PiP-audio example —
   remove it (the new headline covers CTE+overlay+mix in one). Union-splat
   example moves to second billing (keep it — different point: concat
   pairing). Keep the prose tight: the headline points are "the CTE carries
   video and a whole audio array; volume broadcasts over each array; amix
   zips the pairs — one query, composited video, every language mixed".
   NOTE: removing the game.mp4 example breaks the "WITH pip"-keyed README
   test and the 011-readme-pip-audio golden's README linkage — the golden
   FILE stays (it pins compiler behavior; goldens need no README tie), but
   rework the content-keyed README tests to match the new fence set.
4. `tests/test_lower.py` README dispatch: add the new fence via the
   content-keyed `_readme_block` pattern from plan 023 (needle e.g.
   "commentary"); exec-marked compile assertions (4 volume + 2 amix nodes,
   outputs types/metadata); keep the drift test pattern — extend
   `test_readme_headline_command_is_the_real_compilation` (or add a sibling)
   so the NEW headline command is pinned to real compiler output
   (film.mkv→av2.mp4, commentary.mkv→av3.mp4).
5. `sqlmpeg/prompt.py`: Broadcasting section — one line on zip provenance
   ("a zipped mix keeps a tag only when every zipped input agrees"); swap the
   worked sql-probed example set to include the mix flagship. Regen
   docs/system-prompt.md (LF).
6. Full gate: pytest, pytest -m exec, ruff, mypy sqlmpeg/ — all green.

## Do NOT
Touch emit/split/parser/ir/probe/stdlib, goldens, or gen_fixtures (av2/av3
suffice). No git commands.
