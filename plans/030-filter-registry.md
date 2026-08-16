# 030 — Dynamic filter registry  (model: sonnet · main · RFC-003 wave 1)

Read plans/rfc-003-dynamic-filters.md (§ Registry mechanics, § Type mapping,
§ v1 scope fence). Standalone module — NOTHING else imports it yet (lower
integration is plan 031). Mirror sqlmpeg/probe.py's style and hygiene.

## Deliverables
1. `sqlmpeg/registry.py`:
   ```python
   @dataclass(frozen=True)
   class FilterOption:
       name: str
       type: Literal["num", "str", "bool"]   # mapped per RFC table
       doc: str
       minimum: float | None; maximum: float | None
       default: str | None                    # verbatim text, doc use only
       constants: tuple[str, ...]             # enum names, () if not enum
   @dataclass(frozen=True)
   class DynamicFilter:
       name: str
       inputs: tuple[StreamType, ...]         # from pad spec, e.g. (video, video)
       output: StreamType
       doc: str
       # options loaded lazily:
   class Registry:
       def available(self) -> bool
       def get(self, name: str) -> DynamicFilter | None      # None: unknown OR excluded
       def options(self, name: str) -> dict[str, FilterOption] | None
       def names(self) -> list[str]           # for did-you-mean + prompt --dynamic
   def load() -> Registry                      # module-level memo; clear_cache()
   ```
2. Parsing:
   - `ffmpeg -hide_banner -filters`: skip the header/legend; per line parse
     flags, name, pad spec, description. EXCLUDE at parse time: specs with
     `N`, `|`, more than one output char, zero inputs. `V->V` ->
     inputs=("video",), output "video"; `AA->A` -> two audio in; `VV->V`;
     mixed specs parse verbatim per char.
   - `ffmpeg -hide_banner -help filter=<name>` lazily per get()/options()
     call: parse `name <type> flags description` lines + `(from A to B)` +
     `(default X)`; enum constants are the indented following lines (verify
     the exact layout empirically with e.g. `-help filter=xfade` which has a
     transition enum). Type mapping per RFC (int/int64/float/double/rational
     -> num; boolean -> bool; string/color/duration/image_size/video_rate ->
     str; enum -> str + constants; flags -> str; binary/dictionary ->
     mark option unusable (a flag on FilterOption? simplest: type "str" +
     `unusable: bool = False` field — plan 031 rejects usage). Unparseable
     OPTION lines degrade to type "str"; unparseable FILTER help -> filter
     stays listed with empty options dict (permissive).
   - Duplicate option aliases (ffmpeg lists short+long forms as separate
     AVOptions lines with the same description, e.g. `sigma`/`s`? verify —
     if a short alias exists keep only the FIRST/longest name; check
     empirically with a couple of filters).
3. Subprocess hygiene (guardrail #6): argv lists, 10s timeout, never raises —
   any failure -> Registry.available() False / empty.
4. Caching: process memo + on-disk JSON cache under
   `platformdirs`-free location: `~/.cache/sqlmpeg/` (use
   `Path.home()/".cache"/"sqlmpeg"` — fine cross-platform for v1) keyed by
   the first line of `ffmpeg -version` (hash it for the filename). Disk cache
   stores the parsed -filters list AND per-filter options as they get
   loaded (rewrite file on new additions, atomic-ish via temp+replace).
   Corrupt cache file -> ignore and rebuild. clear_cache() removes both.
5. `tests/test_registry.py`: parsing units against CAPTURED text fixtures
   (embed real `-filters` / `-help filter=gblur|xfade|overlay|hqdn3d` output
   snippets as string constants — deterministic, offline); exclusion rules
   (N, |, sources); type mapping incl. enum constants and range; degradation
   paths (garbage output, missing ffmpeg via monkeypatched which -> empty);
   cache behavior (monkeypatch subprocess, count calls; corrupt cache file).
   Plus @pytest.mark.exec tests against the REAL ffmpeg: registry loads,
   len(names()) > 300, gblur present with sigma option typed num, overlay
   present as (video, video) -> video, `split` absent (N spec)... verify
   split's actual spec first — it may be V->N. testsrc absent (source).

## Verify
ruff, mypy --strict sqlmpeg/registry.py, `pytest tests/test_registry.py -q`
green, `pytest -m exec tests/test_registry.py -q` green, full `pytest
tests/ -q` still green. No git commands. Files: sqlmpeg/registry.py,
tests/test_registry.py only.
