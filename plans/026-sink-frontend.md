# 026 — Sink frontend: parser + lower  (model: opus · main · wave 2)

Read plans/rfc-002-sinks.md, then the committed sqlmpeg/sink.py + ir.Sink +
new error codes (plan 025), and the current parser.py/lower.py/compiler.py.

## Deliverables
1. `sqlmpeg/parser.py`: accept `exp.Copy` as the top-level statement.
   VERIFIED shape (sqlglot 30.17, read="postgres"): Copy(this=Subquery(query),
   kind=False, files=[Literal(path)], params=[CopyParameter(Var(name), value)]).
   Probe empirically for variants: COPY without WITH; multiple files; kind
   (FROM vs TO — `kind` False means TO? verify and reject COPY FROM);
   credentials; option value shapes (quoted str → Literal, bare int →
   Literal, true/false → what?). Rules:
   - Exactly one target, a plain string literal path → else UNSUPPORTED_SQL.
   - COPY FROM (loading) → UNSUPPORTED_SQL, hint "only COPY (query) TO 'file'".
   - Option names folded lowercase; values passed through as sqlglot nodes
     with positions (lower validates against the table). Duplicate option
     name → UNSUPPORTED_SQL.
   - The inner query goes through the EXISTING validation unchanged.
   - `Resolved` gains `sink: RawSink | None` (`RawSink`: path, path_node,
     options: list[(name, value_node, name_node)]). Bare SELECT → None.
   - Nested COPY / COPY inside CTE → whatever sqlglot does, reject typed.
2. `sqlmpeg/lower.py`: when `res.sink` is set, validate each option via
   `sink.validate_option` (typed, line-anchored via the recorded nodes;
   int/str/bool coercion from sqlglot literals — reuse the existing literal
   helpers) → `Graph.sink = Sink(path, normalized_options)`. No graph-shape
   change. compiler.py unchanged (lower signature keeps (res, probes)).
3. Tests (test_parser.py + test_lower.py): COPY accepted (sink populated,
   inner query still validated — e.g. COPY (SELECT with GROUP BY) still
   NO_STREAMING_EQUIVALENT); COPY FROM rejected; non-literal path rejected;
   duplicate option rejected; unknown option → UNKNOWN_SINK_OPTION at the
   option's line/col; crf 'high' → SINK_OPTION_TYPE; faststart true/false;
   bare SELECT still sink=None; golden-style full-pipeline compile of a COPY
   query asserting Graph.to_dict()["sink"].

## Verify
ruff, mypy --strict sqlmpeg/parser.py sqlmpeg/lower.py, `pytest tests/ -q`
FULLY green, `pytest -m exec -q` green. No git commands. Report sqlglot Copy
quirks discovered (plan 027/028 authors read your report).
