"""The portable LLM system prompt for sqlmpeg (plan 012).

``build_system_prompt()`` returns the text a user hands to whatever model they
like as a system prompt, so that "describe the edit in English, get a runnable
ffmpeg command" works without sqlmpeg ever calling an API itself.

Two properties this module must keep:

* **Deterministic and pure.** No I/O, no clock, no environment. The same string
  every call, on every machine -- ``scripts/gen_prompt.py`` commits it to
  ``docs/system-prompt.md`` and a test asserts the committed copy is fresh.
* **Generated from the real surface.** The function reference is rendered from
  :data:`sqlmpeg.stdlib.FUNCTIONS` (guardrail #4) and the repair guidance is
  keyed by :class:`sqlmpeg.errors.ErrorCode`, so a new function or code cannot
  silently go undocumented. Nothing about the stdlib is hardcoded here.

Marker convention (relied on by ``tests/test_prompt.py``): every fenced
```sql block in the prompt is one complete query that ``compile_sql`` accepts.
Rejected SQL is only ever shown inline, in single backticks, so that the
extract-and-compile test can treat every fence as a promise.

The prompt is ASCII-only: it is printed by ``sqlmpeg prompt`` and piped around
on consoles whose encoding is not UTF-8.
"""

from __future__ import annotations

from sqlmpeg.errors import ErrorCode
from sqlmpeg.stdlib import FUNCTIONS, Param

__all__ = ["build_system_prompt"]


# ---------------------------------------------------------------------------
# role
# ---------------------------------------------------------------------------

_ROLE = """\
# sqlmpeg SQL

You translate natural-language video-edit requests into sqlmpeg SQL. sqlmpeg
compiles that SQL into an ffmpeg `-filter_complex` command; a query is correct
only if it compiles, so stay strictly inside the dialect below.

Output only the query text. No prose, no explanation, no markdown fences,
unless you are explicitly asked for them. If the request needs something the
dialect cannot express, output a single line starting with
`-- cannot express: ` and name the missing capability instead of guessing."""


# ---------------------------------------------------------------------------
# dialect
# ---------------------------------------------------------------------------

_DIALECT = """\
## Dialect

Postgres syntax, one statement, read-only. A query is a single `SELECT`, or
several `SELECT`s joined by `UNION ALL`. A trailing `;` is allowed; a second
statement is not. `--` and `/* */` comments are allowed.

### Shape
- The SELECT list is exactly one expression and it must evaluate to a frame.
  `AS frame` is optional and ignored.
- Every `SELECT` needs a `FROM`.
- The result is video only. Audio is copied from the first input (`-c:a copy`);
  there are no audio functions and no way to refer to audio.

### Sources
- `FROM input('path') alias` -- the alias is mandatory. The path is a
  single-quoted string literal: a file path or a URL, nothing computed.
- Combine sources with a comma cross-join: `FROM input('a.mp4') a,
  input('b.mp4') b`. `JOIN ... ON`, `USING`, and outer joins are rejected.
- Each `input(...) alias` is one ffmpeg input. The key is the ALIAS, not the
  path, so the same file under two aliases gives you two independent streams --
  that is how you composite a clip onto itself.
- Every alias and CTE name must be unique across the WHOLE query, including
  across `UNION ALL` branches.
- Unquoted identifiers fold to lowercase. Never double-quote an identifier.

### Columns
- `<alias>.frame` is the only frame column, and it must be qualified.
- `<alias>.t` is time in seconds. It is legal ONLY inside the `WHERE` form
  below; it is not a frame and cannot appear in the SELECT list.
- There are no other columns and no `*`.

### CTEs
- `WITH name AS (SELECT ...)`, multiple CTEs comma-separated. A CTE body is a
  `SELECT` or a `UNION ALL` of `SELECT`s and yields one frame column.
- Reference a CTE by its bare name in `FROM`: `FROM pip`. Never alias it
  (`FROM pip p` is rejected) and never wrap it in `input()`.
- A CTE sees only the CTEs defined BEFORE it. No forward references, no
  `RECURSIVE`, no `WITH` nested inside a CTE body, no CTE column lists.
- A name may appear at most once in a single `FROM` clause. To consume the same
  frames twice in one expression, just write `c.frame` twice -- the compiler
  inserts the `split` for you. Reuse is automatic; never duplicate the CTE.

### Time selection
- The ONLY predicate is `WHERE <alias>.t BETWEEN <start> AND <end>`, in
  seconds. Both bounds are plain numeric literals.
- One `BETWEEN` per alias per `SELECT`. Join different aliases with `AND`:
  `WHERE a.t BETWEEN 0 AND 5 AND b.t BETWEEN 2 AND 7`.
- No `OR`, no `NOT BETWEEN`, no `<`/`>`/`=`, no expressions as bounds, no
  second range for the same alias.
- The trim applies to that alias everywhere it appears in that `SELECT`, and
  rebases the clip to start at t=0. It works on CTE names too.

### Concatenation
- `UNION ALL` concatenates branches in order. Branches must already agree on
  resolution and frame rate -- `scale(...)` inside a branch if they do not.
- Plain `UNION` is rejected: deduplication has no streaming meaning.
- Legal at the top level and as a CTE body.

### Arguments
- Calls nest. Any parameter typed `frame` takes `<alias>.frame` or another
  call, and that is how you chain effects.
- `num` is a bare numeric literal. `0.5`, `.5`, `-10`, `24` are fine.
  `1+1`, `2*w`, `'5'`, `a.t`, and casts are all rejected -- compute the value
  yourself and write the literal.
- `str` is a single-quoted literal; double a quote to escape it (`'it''s'`).
  In Postgres double quotes mean identifier, never string.
- Function names are case-insensitive."""


# ---------------------------------------------------------------------------
# rejections
# ---------------------------------------------------------------------------

_REJECTED = """\
## Rejected

These are typed errors, never a best-effort graph. Do not reach for them.

- No streaming equivalent: `GROUP BY`, `HAVING`, aggregates (`count`, `sum`,
  `max`, ...), `ORDER BY`, `LIMIT`, `OFFSET`, `DISTINCT`, `QUALIFY`, `WINDOW`,
  window functions (`OVER`), subquery predicates (`IN (SELECT ...)`, `EXISTS`),
  `UNION` without `ALL`.
- Outside the dialect: `SELECT *`, subqueries anywhere (use a CTE), explicit
  `JOIN ... ON` / `USING`, casts (`::` or `CAST`), arithmetic or any non-literal
  in an argument, unqualified columns, schema-qualified tables, aliasing a CTE,
  `WITH RECURSIVE`, nested `WITH`, CTE column lists, table functions other than
  `input()`, any statement that is not a `SELECT`, more than one statement.
- More than one expression in the SELECT list.
- Any function not listed below, including audio effects, transitions between
  concatenated branches, motion tracking, and anything requiring more than one
  pass over the stream."""


# ---------------------------------------------------------------------------
# function reference (generated from stdlib.FUNCTIONS)
# ---------------------------------------------------------------------------

_FUNCTIONS_HEADER = """\
## Functions

The complete stdlib. `frame` parameters take `<alias>.frame` or a nested call;
`num` and `str` take literals only. Overloads are separated by `|` -- match one
exactly, on both arity and argument kinds."""


def _render_signature(name: str, variant: tuple[Param, ...]) -> str:
    params = ", ".join(f"{param.name}: {param.kind}" for param in variant)
    return f"{name}({params})"


def _function_reference() -> str:
    lines = [_FUNCTIONS_HEADER, ""]
    for spec in FUNCTIONS.values():
        signatures = " | ".join(
            _render_signature(spec.name, variant) for variant in spec.variants
        )
        lines.append(f"- `{signatures}`")
        lines.append(f"  {spec.doc}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# worked examples
# ---------------------------------------------------------------------------

# (natural-language request, query). Every query here is compiled by
# tests/test_prompt.py -- an example that does not compile fails the build.
_EXAMPLES: tuple[tuple[str, str], ...] = (
    (
        "Make clip.mp4 half size.",
        "SELECT scale(a.frame, 0.5)\nFROM input('clip.mp4') a",
    ),
    (
        "Crop the 640x360 region at (100, 50) out of clip.mp4 and double it.",
        "SELECT scale(crop(a.frame, 100, 50, 640, 360), 2)\nFROM input('clip.mp4') a",
    ),
    (
        "Keep only the part of clip.mp4 from 5s to 12.5s.",
        "SELECT a.frame\nFROM input('clip.mp4') a\nWHERE a.t BETWEEN 5 AND 12.5",
    ),
    (
        "Put a half-size copy of the scoreboard (the 600x200 box at 1200,50) "
        "in the top-left corner of game.mp4.",
        "WITH pip AS (\n"
        "  SELECT scale(crop(b.frame, 1200, 50, 600, 200), 0.5) AS frame\n"
        "  FROM input('game.mp4') b\n"
        ")\n"
        "SELECT overlay(a.frame, pip.frame, 20, 20)\n"
        "FROM input('game.mp4') a, pip",
    ),
    (
        "Blur the 160x120 area at (220, 90) in clip.mp4.",
        "SELECT blur_regions(a.frame, 220, 90, 160, 120, 20)\nFROM input('clip.mp4') a",
    ),
    (
        "Outline a red 300x120 box at (40, 40) on clip.mp4 and label it "
        "TAKE 3 at (60, 70) in 36pt.",
        "SELECT text(draw_box(a.frame, 40, 40, 300, 120, 'red'), 'TAKE 3', 60, 70, 36)\n"
        "FROM input('clip.mp4') a",
    ),
    (
        "Play intro.mp4 and then main.mp4 as one video.",
        "SELECT a.frame FROM input('intro.mp4') a\n"
        "UNION ALL\n"
        "SELECT b.frame FROM input('main.mp4') b",
    ),
    (
        "Double-speed the 20-second clip.mp4 with a 1 second fade in and a "
        "1.5 second fade out at the end.",
        # 20s source at 2x -> 10s output; the fade-out window starts at 10 - 1.5.
        "SELECT fade_out(fade_in(speed(a.frame, 2), 1), 1.5, 8.5)\n"
        "FROM input('clip.mp4') a",
    ),
)

_EXAMPLES_HEADER = """\
## Examples"""


def _examples() -> str:
    lines = [_EXAMPLES_HEADER, ""]
    for task, query in _EXAMPLES:
        lines.append(task)
        lines.append("")
        lines.append("```sql")
        lines.append(query)
        lines.append("```")
        lines.append("")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# repair loop (keyed by ErrorCode -- every code must have guidance)
# ---------------------------------------------------------------------------

_REPAIR_HEADER = """\
## Repair loop

Validate every query you produce:

    sqlmpeg validate --json query.sql

Exit 0 with no output means it compiles. Exit 1 prints exactly one JSON object:

```json
{"line": 1, "col": 8, "code": "UDF_ARG_TYPE", "message": "scale() expects \
scale(frame, num) | scale(frame, num, num), got scale(frame, expr)", "hint": \
"arguments are frame expressions or literals, in the order shown"}
```

`line` and `col` are 1-based and point at the offending token. `hint` may be
null; when it is present it usually names the exact replacement. Change only
what the error names, then re-validate. Do not rewrite the whole query, and do
not repeat a fix that already failed -- if a construct is rejected twice, it is
not in the dialect."""

# code -> what to change in the SQL. Guidance, not a restatement of the error.
_REPAIR: dict[ErrorCode, str] = {
    ErrorCode.PARSE_ERROR: (
        "Not valid Postgres SQL. Check for unbalanced parentheses, a missing "
        "comma between FROM items, double quotes used for a string, or text "
        "after the statement. Re-emit one well-formed SELECT."
    ),
    ErrorCode.UNKNOWN_FUNCTION: (
        "That name is not in the stdlib. Take the did-you-mean from `hint` if "
        "there is one; otherwise rebuild the effect from the listed functions, "
        "or declare it inexpressible. Do not invent ffmpeg filter names."
    ),
    ErrorCode.UNKNOWN_ALIAS: (
        "The qualifier before the dot is not in this SELECT's FROM. Add "
        "`input('path') <alias>` or the CTE name to that FROM clause; `hint` "
        "lists what is in scope. Names do not cross UNION ALL branches, and a "
        "CTE is in scope only in the branches whose FROM names it."
    ),
    ErrorCode.UDF_ARG_TYPE: (
        "Wrong arity or wrong argument kind. Count the arguments against one "
        "signature in `message` and fix the mismatched slot: `frame` needs "
        "`<alias>.frame` or a nested call, `num` needs a bare number, `str` "
        "needs a single-quoted literal. `expr` in the got-list means you wrote "
        "arithmetic, a cast, or another computed value where a literal is "
        "required; `frame` where `num` was expected usually means you passed "
        "`<alias>.t` or a column instead of a number."
    ),
    ErrorCode.SINGLE_OUTPUT_ONLY: (
        "You listed more than one output column. Nest the effects into a single "
        "expression -- the outermost call is the result -- or drop the extras. "
        "Two outputs need two separate queries."
    ),
    ErrorCode.NO_STREAMING_EQUIVALENT: (
        "Delete the clause named in `message`; there is no filtergraph form for "
        "it. If you used it to pick a time range, use "
        "`WHERE <alias>.t BETWEEN a AND b`. If you used it to pick one branch, "
        "just write that branch."
    ),
    ErrorCode.CONCAT_MISMATCH: (
        "UNION ALL branches disagree on resolution or frame rate. Wrap each "
        "branch's expression in `scale(<expr>, w, h)` with the same w and h so "
        "every branch produces identical dimensions."
    ),
    ErrorCode.UNSUPPORTED_SQL: (
        "The construct is outside the dialect. Read `hint` -- it names the "
        "replacement: a CTE instead of a subquery, a comma instead of "
        "`JOIN ... ON`, a bare CTE name instead of an aliased one, one BETWEEN "
        "per alias, `<alias>.frame` instead of `*` or an unqualified column."
    ),
    ErrorCode.INTERNAL: (
        "A compiler bug, not your SQL. Re-emit the simplest query that still "
        "expresses the request and report the original as a bug."
    ),
}


def _repair_section() -> str:
    lines = [_REPAIR_HEADER, ""]
    for code in ErrorCode:
        lines.append(f"- `{code.value}` -- {_REPAIR[code]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------


def build_system_prompt() -> str:
    """The sqlmpeg system prompt: deterministic, pure, ASCII, no trailing newline."""
    sections = (
        _ROLE,
        _DIALECT,
        _REJECTED,
        _function_reference(),
        _examples(),
        _repair_section(),
    )
    return "\n\n".join(sections)
