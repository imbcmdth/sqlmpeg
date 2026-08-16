# 025 — Sink foundation: errors, IR, options table  (model: sonnet · main · wave 1)

Read plans/rfc-002-sinks.md (the design) first. Repo is green (533 + 33 exec);
stays green after every plan — this change is ADDITIVE.

## Deliverables
1. `sqlmpeg/errors.py`: add `UNKNOWN_SINK_OPTION`, `SINK_OPTION_TYPE`.
2. `sqlmpeg/ir.py`:
   ```python
   @dataclass
   class Sink:
       path: str
       options: dict[str, object]   # insertion-ordered, values already normalized
   ```
   `Graph.sink: Sink | None = None`. `to_dict()` includes a `"sink"` key ONLY
   when sink is not None (`{"path", "options"}`); `from_dict` tolerates the
   key's absence → None. This keeps every existing golden byte-identical.
3. `sqlmpeg/sink.py` (new) — the option table as DATA (guardrail #4):
   ```python
   OptionScope = Literal["video", "audio", "container"]
   @dataclass(frozen=True)
   class SinkOptionSpec:
       name: str
       scope: OptionScope
       type: Literal["str", "int", "bool"]
       doc: str                       # one line; drives docs + prompt
       # rendering data for emit (no logic outside the table):
       flag: str                      # e.g. "-c", "-crf", "-b", "-f", "-movflags"
       per_stream: bool               # True -> rendered as f"{flag}:{i}" per output
       value_template: str = "{v}"    # e.g. "+faststart" for the bool movflags case
   SINK_OPTIONS: dict[str, SinkOptionSpec]
   ```
   Entries exactly per RFC-002's v1 table (video_codec, audio_codec, crf,
   preset, pix_fmt, video_bitrate, audio_bitrate, sample_rate, format,
   faststart). `faststart`: scope container, type bool, flag "-movflags",
   value_template "+faststart" (rendered only when true; false = omit).
   `format`: flag "-f", per_stream False.
   Helper `validate_option(name, value, ...)` used by lower: unknown →
   UNKNOWN_SINK_OPTION (did-you-mean via difflib), wrong type →
   SINK_OPTION_TYPE (message names option, expected type, got value). Bools
   accept true/false; ints reject floats/strings.
4. `tests/test_sink.py`: table completeness (10 entries, scopes/types per
   RFC), validate_option happy/unknown/type paths, did-you-mean hint.
   Extend `tests/test_ir.py`: sink round-trip, and a no-sink graph's to_dict
   has NO "sink" key.
5. `docs/errors.md` + `docs/error-schema.json`: the two new codes (schema enum
   + errors.md sections; examples can be added by plan 028 once the parser
   accepts COPY — for now document with "raised when ..." prose and a
   placeholder note that the JSON example lands with the COPY feature; BUT
   tests/test_docs.py::test_schema_enum_matches_error_code and the heading
   test must pass — check what they require and satisfy them).

## Verify
ruff, mypy --strict on changed modules, `pytest tests/ -q` FULLY green
(533+ passing, no regressions), `pytest -m exec -q` green. No git commands.
