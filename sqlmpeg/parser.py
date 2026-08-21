"""Parse + resolve passes for sqlmpeg.

``parse`` turns SQL text into a sqlglot AST (always ``read="postgres"``,
guardrail #2). ``resolve`` validates that the AST stays inside the dialect
surface, builds the alias/CTE table, and assigns ffmpeg input indices.

Neither ever lets a sqlglot (or any other) exception escape: every rejection is
a :class:`sqlmpeg.errors.SqlmpegError` with a typed code and, where sqlglot
gives token positions, a line/col anchor.

Notes for downstream passes (lower):

* Input indices are keyed by ALIAS, not by path. Two aliases over one file
  produce two ``-i`` entries, so ``input_paths`` may contain duplicates.
* ``Resolved.select`` may be an ``exp.Union`` (a ``UNION ALL`` query). Use
  ``Resolved.branches``, or :func:`union_branches` for CTE bodies, for the
  flat list of branch selects.
* Identifier names are normalized the Postgres way: unquoted lowercased,
  quoted verbatim. ``sources`` keys are normalized.
* The SELECT list may hold MORE THAN ONE projection (one column = one output
  stream); only an empty projection list is rejected.
* A statement may be wrapped in ``COPY (<query>) TO '<path>' WITH (<options>)``.
  The COPY is peeled off into a ``Resolved.sinks`` entry and the query it wraps
  goes through the EXACT same validation a bare SELECT does; a bare SELECT
  leaves ``sinks`` empty. Only the shape is checked here — option NAMES and
  VALUES are validated against ``sqlmpeg.sink.SINK_OPTIONS`` by lower.
* A media COPY's destination may be a parenthesized EXPRESSION,
  ``TO ('ch' || c.index::text || '.mkv')``, kept on ``RawSink.path_expr``. One
  reading a track-row column fans the COPY out into one command per surviving
  row (lower evaluates it per row); one reading none is a constant path. The
  fan-out shape also reopens a WHERE mix resolve otherwise rejects: a time
  window on an input alias whose bounds are row columns.

* Input text may be a SCRIPT: ``CREATE VIEW <name> AS <query>;``* followed by
  ``COPY ...;``+. See :func:`_statements` and :meth:`_Resolver._view` for the
  VERIFIED sqlglot shapes. A view is to STATEMENTS what a CTE is to branches,
  so it is stored as one: ``Resolved.ctes`` is the flat, ordered binding table
  (views AND CTEs, in definition order) that lower walks, and
  ``Resolved.views`` names the subset from a ``CREATE VIEW``. A binding may be
  ALIASED in FROM (``FROM master m``): the alias is branch-local — two
  branches, or two COPYs, may both spell it ``m`` — but may not shadow the flat
  namespace (:meth:`_Resolver._local_alias`).
* Named function arguments (``gblur(a.video[1], sigma => 5)``) are native Postgres
  syntax: sqlglot parses each into an ``exp.Kwarg(this=Var(name),
  expression=<value>)`` inside the call's ``expressions`` list. The resolver
  checks their SHAPE only — named args must TRAIL the positional ones and may
  not repeat — since which options exist is a property of the installed ffmpeg,
  which only lower (and its registry) knows. Names are kept VERBATIM, never
  folded: ffmpeg option names are case-sensitive (``gblur``'s ``sigmaV``).

* ``SELECT *`` / ``SELECT <alias>.*`` are accepted in PROJECTION position only.
  VERIFIED shapes under sqlglot 30.17 ``read="postgres"``: ``SELECT *`` puts a
  bare ``exp.Star()`` in the projection list, ``SELECT a.*`` puts an
  ``exp.Column(this=Star(), table=Identifier(a))`` — two shapes, both
  recognized by :func:`star_qualifier`. Every other position a star can appear
  in (``scale(a.*, 0.5)``, ``a.*[1]``, ``* AS x``, ``count(*)``, a star in
  WHERE) is rejected: which streams a star stands for is knowable only after
  probing, so it can BE a column but never feed one. The resolver checks the
  qualifier is a known alias; lower, which has the probes, expands it.

* The ``ffmpeg.<filter>(...)`` namespace needs nothing from this pass but a
  reserved name. VERIFIED under sqlglot 30.17 ``read="postgres"``: a qualified
  call parses as ``exp.Dot(this=Identifier(ffmpeg),
  expression=exp.Anonymous(this=<filter>, expressions=[...]))`` for EVERY
  filter name, collision victims included — the builtin special-form grammars
  (``OVERLAY ... PLACING``, ``TRIM``, ``FORMAT``, ...) key on a bare name, so
  qualifying bypasses them completely, and ``=>`` arguments land in the
  ``Anonymous`` as ordinary ``exp.Kwarg``s. The resolver only RESERVES the name
  ``ffmpeg`` (:meth:`_Resolver._reserve`) so no alias or CTE can shadow the
  namespace; lower does the resolution.

* The ``sqlmpeg.<name>(...)`` macro namespace is the SAME shape, re-probed
  empirically and IDENTICAL: ``exp.Dot(this=Identifier(sqlmpeg),
  expression=exp.Anonymous(this=<macro>, expressions=[...]))`` for all three
  macro names (none collides with a Postgres special form, so the namespace is
  optional there, but the shape does not care). ``sqlmpeg`` joins ``ffmpeg`` as
  a second reserved qualifier (:data:`MACRO_NAMESPACE`); lower resolves the
  call against ``sqlmpeg.macros.MACROS`` and nowhere else.

* ``FROM ffmpeg.<source>(<named options>) alias`` is the same namespace in
  TABLE position, and a DIFFERENT sqlglot shape from the call above — no
  ``Dot`` appears. VERIFIED under sqlglot 30.17 ``read="postgres"``, see the
  table in :meth:`_Resolver._add_source`: ``FROM ffmpeg.testsrc(duration => 2)
  t`` parses as ``exp.Table(this=Anonymous(testsrc, [Kwarg...]),
  db=Identifier(ffmpeg), alias=TableAlias(t))`` — the qualifier lands in the
  Table's ``db`` slot (``catalog`` too, for a three-part name), NOT wrapped
  around the call. The parenthesis-less ``FROM ffmpeg.testsrc t`` is the same
  Table with an ``Identifier`` rather than an ``Anonymous`` ``this``, which is
  how that form is told apart and rejected with a hint. A generated source
  takes NO ffmpeg input index — a zero-input filter node has no ``-i`` — so it
  never enters ``input_paths``/``sources``; its record goes into
  ``Resolved.source_filters``. Which source names exist, and which options they
  take, is the installed ffmpeg's business: this pass checks SHAPE only (alias
  mandatory, arguments named-only).

* Stream subscripts (``a.video[1]``) arrive as ``exp.Bracket`` wrapping the
  ``exp.Column``. **sqlglot rebases the index at parse time**: under
  ``read="postgres"`` (``INDEX_OFFSET = 1``) it rewrites the single subscript
  expression to ``expr - 1`` whenever it annotates it as an integer type, so
  ``a.video[1]`` holds ``Literal(0)`` and ``a.video[0]`` holds ``Neg(Literal(1))``.
  Never read ``Bracket.expressions[0]`` directly — use :func:`subscript_index`,
  which undoes the rebase and returns the 1-based number the user wrote.

* ``unnest(<alias>.<type>) <row alias>`` in FROM is a TRACK-ROW table: one row
  per stream of that array, one column per piece of probed metadata
  (:data:`ROW_SCHEMAS`). Shapes VERIFIED under sqlglot 30.17
  ``read="postgres"``:

  ==================================================== ==========================
  written                                              how it lands here
  ==================================================== ==========================
  ``FROM input('f') f, unnest(f.audio) t``             ``Select.joins[0]`` is an
                                                       ``exp.Join`` whose ONLY
                                                       arg is ``this=exp.Unnest``
                                                       — a comma source and an
                                                       explicit JOIN are the same
                                                       ``joins`` list, told apart
                                                       by ``side``/``kind``/``on``
  ``FROM unnest(f.audio) t, ...``                      ``From.this`` IS the
                                                       ``exp.Unnest`` (no wrapper
                                                       ``exp.Table``)
  ``unnest(f.audio) t`` / ``... AS t``                 identical:
                                                       ``alias=TableAlias(t)``
  ``unnest(f.audio)``                                  no ``alias`` at all
  ``unnest(f.audio) t(x)``                             ``TableAlias`` also carries
                                                       ``columns=[Identifier(x)]``
  ``unnest(f.audio) WITH ORDINALITY t``                ``offset=True`` (it is
                                                       present-and-False
                                                       otherwise)
  ``unnest(f.audio, f.video) t``                       TWO ``expressions``
  ``unnest(f.audio[1]) t``                             ``expressions[0]`` is an
                                                       ``exp.Bracket``
  ``unnest(unnest(f.audio)) t``                        inner call becomes
                                                       ``exp.Explode``
  ``unnest() t``                                       ParseError (never reaches
                                                       the resolver)
  ==================================================== ==========================

  VERIFIED JOIN node shapes (all ``exp.Join`` entries in the SAME
  ``Select.joins`` list a comma source uses): ``JOIN`` -> ``args = this, on,
  pivots`` (no ``side``, no ``kind``); ``INNER JOIN`` -> ``kind='INNER'`` (no
  ``side``); ``LEFT JOIN`` -> ``side='LEFT'``; ``LEFT OUTER JOIN`` ->
  ``side='LEFT', kind='OUTER'``; ``RIGHT JOIN`` -> ``side='RIGHT'``;
  ``FULL OUTER JOIN`` -> ``side='FULL', kind='OUTER'``; ``FULL JOIN`` ->
  ``side='FULL'`` with NO ``kind``; ``CROSS JOIN`` -> ``kind='CROSS'`` with no
  ``side`` and no ``on``; ``NATURAL JOIN`` -> ``method='NATURAL'``;
  ``USING (col)`` -> ``using=[Identifier]``. A comma source carries none.

* ``JOIN`` between two ``unnest`` tables is admitted — INNER (``JOIN`` /
  ``INNER JOIN``), ``LEFT [OUTER]`` and ``FULL [OUTER]``, each with a mandatory
  ``ON`` — and nowhere else: a join whose right side is not an ``unnest``, or
  that stands before any row table is bound, is rejected, as are ``RIGHT``,
  ``CROSS``, ``NATURAL`` and ``USING``. :func:`from_entries` pairs each FROM
  item with the :class:`RawRowJoin` describing how it attaches (``"cross"`` for
  a comma source), which is what lower evaluates the join from. The ``ON``
  predicate is the row grammar plus one addition: both operands may be row
  COLUMNS of any row table in scope — ``a.tags.language = b.tags.language``.

* ``ORDER BY`` over track-row columns is the ONE carve-out in the
  ``NO_STREAMING_EQUIVALENT`` rejection: ``Select.args["order"]`` is admitted only
  when the branch's FROM clause holds at least one ``unnest`` (frames still
  never sort). VERIFIED: an ``exp.Ordered`` carries ``desc`` and
  ``nulls_first`` as plain bools and sqlglot fills BOTH from the Postgres
  defaults — ``ASC`` gives ``nulls_first=False`` (NULLS LAST), ``DESC`` gives
  ``nulls_first=True`` (NULLS FIRST) — so honoring the flags verbatim IS
  Postgres semantics, explicit ``NULLS FIRST``/``LAST`` included.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, SqlglotError

from sqlmpeg.errors import ErrorCode, SqlmpegError
from sqlmpeg.types import (
    CHAPTERS_COLUMN,
    DISPOSITION_COLUMN,
    DISPOSITION_KEYS,
    INPUT_COLUMNS,
    INPUT_DURATION_COLUMN,
    MAP_ELEMENTS,
    ROW_SCHEMAS,
    STREAM_ARRAY_COLUMNS,
    STREAM_TAG_COLUMNS,
    TAGS_COLUMN,
    UNNEST_COLUMNS,
    is_array,
)

__all__ = [
    "DISPOSITION_COLUMN",
    "DISPOSITION_KEYS",
    "FILTER_NAMESPACE",
    "INPUT_DURATION_COLUMN",
    "MACRO_NAMESPACE",
    "MAP_COLUMNS",
    "ROW_SCHEMAS",
    "ROW_STREAM",
    "TAGS_COLUMN",
    "RawInputOption",
    "RawSink",
    "RawSinkOption",
    "RawSource",
    "RawRowJoin",
    "RawSourceOption",
    "RawTrackRows",
    "RawValuesTable",
    "Resolved",
    "column_label",
    "disposition_key",
    "flag_error",
    "flag_hint",
    "from_entries",
    "from_items",
    "group_keys",
    "is_grouped",
    "is_value_expr",
    "kwarg_name",
    "map_example",
    "map_noun",
    "map_path",
    "map_ref",
    "parse",
    "resolve",
    "star_qualifier",
    "subscript_index",
    "subscript_metadata_shape",
    "tag_key",
    "tag_path",
    "union_branches",
]

# The reserved qualifier of the raw-filter namespace, `ffmpeg.<filter>(...)`.
# Not an alias, never resolved against the FROM clause, so no alias or CTE may
# claim the name (`_Resolver._reserve`).
FILTER_NAMESPACE = "ffmpeg"

# The reserved qualifier of the macro namespace, `sqlmpeg.<name>(...)`. A
# second reserved alias/CTE name; the macro table (`sqlmpeg.macros.MACROS`) is
# lower's business, not this pass's.
MACRO_NAMESPACE = "sqlmpeg"

# The row IS the stream, so the stream has no column name to write. A bare row
# alias is rewritten to a column of that alias under this internal name
# (`_normalize_row_aliases`), and every later pass reads a stream column
# exactly as it always did. The angle brackets keep it out of the surface: no
# unquoted identifier spells it, and the quoted spelling is turned away where
# row columns are checked.
ROW_STREAM = "<stream>"

# A map is read by PATH -- `f.tags.title`, `a.disposition.forced` -- which
# parses as a dotted column, one written part more than any other read. The two
# parts are folded into one internal column name here (`_normalize_map_paths`)
# so every later pass looks up an ordinary column; the angle brackets keep it
# off the surface, exactly as ROW_STREAM's do. `column_label` writes it back the
# way it was typed wherever a rejection names it.
MAP_COLUMNS = (TAGS_COLUMN, DISPOSITION_COLUMN)

# The key each map's hints quote, so an example is never invented here.
_MAP_EXAMPLES = {
    TAGS_COLUMN: STREAM_TAG_COLUMNS[0],
    DISPOSITION_COLUMN: DISPOSITION_KEYS[0],
}

# The spellings that left the language. Each keeps a rejection of its own
# naming its replacement, so an old query says what to write instead.
_REMOVED_ROW_STREAM = "track"
_REMOVED_FRAME = "frame"
# The per-stream tag fields, now entries of `tags`.
_REMOVED_STREAM_TAGS = ("language", "title")
# The named container tag columns, now entries of the container's `tags`.
_REMOVED_INPUT_TAGS = frozenset(
    {
        "title",
        "artist",
        "album",
        "album_artist",
        "date",
        "genre",
        "comment",
        "composer",
        "track",
        "copyright",
        "encoder",
        "description",
    }
)

# A top-level (or CTE-level) query: a plain SELECT, or a UNION ALL of them.
QueryExpr = exp.Select | exp.Union

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_DEFAULT_POS: tuple[int, int] = (1, 1)

# Select/Union arg keys that map to a hard "no streaming equivalent" rejection.
_STREAMING_CLAUSES: dict[str, str] = {
    "group": "GROUP BY",
    "having": "HAVING",
    "order": "ORDER BY",
    "sort": "SORT BY",
    "cluster": "CLUSTER BY",
    "distribute": "DISTRIBUTE BY",
    "limit": "LIMIT",
    "offset": "OFFSET",
    "distinct": "DISTINCT",
    "qualify": "QUALIFY",
    "windows": "WINDOW",
    "connect": "CONNECT BY",
}

_SELECT_ALLOWED = frozenset({"with_", "expressions", "from_", "joins", "where"})
_UNION_ALLOWED = frozenset({"with_", "this", "expression", "distinct"})
_SUBQUERY_ALLOWED = frozenset({"this"})
_BRACKET_ALLOWED = frozenset({"this", "expressions"})
# Every arg sqlglot 30.17 puts on an exp.Copy. `kind` and `credentials` are
# whitelisted here only so the generic check does not fire on them — each has
# its own explicit rejection below.
_COPY_ALLOWED = frozenset({"this", "kind", "credentials", "files", "params"})

# What a hint says about tags wherever one is worth naming.
_TAGS_HINT = f"container tags are read by path, e.g. <alias>.{TAGS_COLUMN}.title"

# sqlglot's Postgres dialect INDEX_OFFSET. Parsing rebases a subscript by
# -INDEX_OFFSET and generating adds it back; see the module docstring.
_INDEX_OFFSET = 1

_DIGITS_RE = re.compile(r"[0-9]+\Z")

_WHERE_HINT = (
    "the only supported WHERE forms are <alias>.t BETWEEN <start> AND <end>, "
    "<alias>.t >= <start>, or <alias>.t <= <end> (either operand order), "
    "optionally joined with AND"
)
_STRICT_HINT = (
    "use >= / <=: seeks are time-based, a strict bound has no frame-level meaning"
)
_ALIAS_HINT = "add an alias, e.g. FROM input('clip.mp4') a"
_SOURCE_ALIAS_HINT = (
    f"add an alias, e.g. FROM {FILTER_NAMESPACE}.testsrc(duration => 2) t"
)
_SOURCE_CALL_HINT = (
    f"a generated source is a CALL: write {FILTER_NAMESPACE}.<source>() alias, "
    f"e.g. FROM {FILTER_NAMESPACE}.anullsrc(duration => 30) s"
)
_SUBSCRIPT_HINT = (
    "stream subscripts are 1-based integer literals: a.video[1] is the first "
    "video stream"
)
_STAR_HINT = (
    "a star is a whole SELECT column: write `SELECT *` or `SELECT <alias>.*`; "
    "it cannot be aliased, subscripted, or passed to a function"
)
_SINK_HINT = "the only sink form is COPY (<query>) TO '<path>' WITH (<options>)"
_SCRIPT_HINT = (
    "a script is CREATE VIEW ... ; statements followed by one or more "
    "COPY (<query>) TO '<path>'; statements"
)
_VIEW_HINT = "write CREATE VIEW <name> AS <query>"
_OPTION_HINT = "sink options are name/value pairs, e.g. crf 20 or video_codec 'libx264'"
_KWARG_HINT = (
    "named arguments are written <name> => <value> and come last, "
    "e.g. gblur(a.video[1], sigma => 5)"
)
_UNNEST_HINT = (
    "unnest takes one bare array column of an input alias and needs a name for "
    "its rows, e.g. FROM input('film.mkv') f, unnest(f.audio) t"
)
_ROW_WHERE_HINT = (
    "a track-row predicate compares one row column against a literal: "
    "=, !=, <, <=, >, >=, BETWEEN, IS [NOT] NULL, joined with AND/OR/NOT"
)
_JOIN_HINT = (
    "JOIN matches the ROWS of two unnest tables: FROM input('a.mkv') f, "
    "input('b.mkv') g, unnest(f.audio) a JOIN unnest(g.audio) b ON "
    "a.tags.language = b.tags.language (INNER, LEFT [OUTER] and FULL [OUTER] only); "
    "at input level, FROM stays a comma cross-join"
)
_JOIN_ON_HINT = (
    "an ON predicate compares track-row columns against each other or against "
    "a literal: =, !=, <, <=, >, >=, BETWEEN, IS [NOT] NULL, joined with "
    "AND/OR/NOT"
)
_VALUE_HINT = (
    "a compile-time value is a literal, NULL, a track-row column, "
    "<input>.duration, or CASE / '||' / arithmetic / ::text over those"
)
_ROW_ORDER_HINT = (
    "ORDER BY sorts track rows by their metadata columns, "
    "e.g. ORDER BY t.tags.language, t.channels DESC"
)
_SUBSCRIPT_WHERE_HINT = (
    "a subscript metadata predicate compares <alias>.<type>[k].<column> against "
    "a literal: =, !=, <, <=, >, >=, BETWEEN, IS [NOT] NULL, joined with "
    "AND/OR/NOT"
)
_AGG_HINT = (
    "array_agg(<track expression>) gathers a branch's track rows into one "
    "file, e.g. SELECT array_agg(t) FROM input('f.mkv') f, "
    "unnest(f.audio) t"
)
_ARRAY_AGG_PLACE_HINT = (
    "array_agg(...) is a whole SELECT column: write array_agg(volume(t, "
    "0.5)), not volume(array_agg(t), 0.5)"
)
_GROUP_STREAM_HINT = (
    "a grouped query aggregates its streams: wrap it in array_agg(...), or "
    "make it the group's key"
)
_GROUP_VALUE_HINT = (
    "add it to the GROUP BY to make it the group's key, or tag the tracks "
    "inside a CTE and aggregate the CTE's streams outside it"
)
_CHAPTER_NOT_STREAM_HINT = (
    "a chapter is not a track, so a chapter row has no stream to select; read "
    "its metadata columns instead, e.g. {alias}.title"
)
_GROUP_FANOUT_HINT = (
    "one group is one file, so the destination has to name the group, "
    "e.g. TO (t.tags.language || '.mka')"
)


# position helpers


def _node_pos(node: exp.Expr) -> tuple[int, int] | None:
    """Token position of `node` itself, or None if sqlglot recorded none.

    sqlglot stores the position of the token's LAST character in
    ``meta["col"]``; ``start``/``end`` are absolute character offsets, so
    ``col - (end - start)`` recovers the 1-based starting column.
    """
    meta = node.meta
    line = meta.get("line")
    col = meta.get("col")
    if not isinstance(line, int) or not isinstance(col, int):
        return None
    start = meta.get("start")
    end = meta.get("end")
    if isinstance(start, int) and isinstance(end, int) and end >= start:
        col -= end - start
    return (line, max(col, 1))


def _pos(*nodes: exp.Expr | None) -> tuple[int, int]:
    """Earliest token position found in the first node that has any.

    Container nodes (Select, Where, Between, Column, ...) carry no position of
    their own in sqlglot — only leaf-ish tokens do — so we take the minimum over
    the subtree. Falls back to line 1, col 1.
    """
    for node in nodes:
        if node is None:
            continue
        best: tuple[int, int] | None = None
        for sub in node.walk():
            found = _node_pos(sub)
            if found is not None and (best is None or found < best):
                best = found
        if best is not None:
            return best
    return _DEFAULT_POS


def _error(
    code: ErrorCode,
    message: str,
    node: exp.Expr | None = None,
    *,
    fallback: exp.Expr | None = None,
    hint: str | None = None,
) -> SqlmpegError:
    line, col = _pos(node, fallback)
    return SqlmpegError(code, message, line=line, col=col, hint=hint)


def _frame_error(node: exp.Expr, alias: str, fallback: exp.Expr | None) -> SqlmpegError:
    """``<alias>.frame`` left the language; it was always ``<alias>.video[1]``."""
    return _error(
        ErrorCode.UNSUPPORTED_SQL,
        f"'{alias}.{_REMOVED_FRAME}' is not a column",
        node,
        fallback=fallback,
        hint=f"the first video stream is '{alias}.video[1]'",
    )


def _removed_tag_error(
    label: str, name: str, node: exp.Expr, fallback: exp.Expr | None
) -> SqlmpegError:
    """A tag that used to be a field of its own; it is an entry of ``tags`` now."""
    return _error(
        ErrorCode.UNSUPPORTED_SQL,
        f"'{label}.{name}' is not a column: a tag is read by path",
        node,
        fallback=fallback,
        hint=f"read the tag: '{label}.{TAGS_COLUMN}.{name}'",
    )


def _ident_name(node: exp.Expr | None) -> str:
    """Postgres identifier folding: unquoted -> lowercase, quoted -> verbatim."""
    if node is None:
        return ""
    if isinstance(node, exp.Identifier):
        return node.name if node.args.get("quoted") else node.name.lower()
    return str(node.name).lower()


# map paths


def map_path(column: str, key: str) -> str:
    """The internal column name ``<alias>.<column>.<key>`` folds into."""
    return f"<{column}>{key}"


def map_ref(name: str) -> tuple[str, str] | None:
    """The ``(map column, key)`` a folded column name carries, else None."""
    for column in MAP_COLUMNS:
        prefix = f"<{column}>"
        if name.startswith(prefix):
            return column, name[len(prefix) :]
    return None


def tag_path(key: str) -> str:
    """The internal column name ``<alias>.tags.<key>`` folds into."""
    return map_path(TAGS_COLUMN, key)


def tag_key(name: str) -> str | None:
    """The tag key a folded column name carries, or None for any other name."""
    ref = map_ref(name)
    return ref[1] if ref is not None and ref[0] == TAGS_COLUMN else None


def disposition_key(name: str) -> str | None:
    """The disposition key a folded column name carries, else None."""
    ref = map_ref(name)
    return ref[1] if ref is not None and ref[0] == DISPOSITION_COLUMN else None


def column_label(name: str) -> str:
    """A column name as it was TYPED: a folded map path spelled back out."""
    ref = map_ref(name)
    return name if ref is None else f"{ref[0]}.{ref[1]}"


def map_noun(column: str) -> str:
    """The record a map column holds: `tags` holds tags, `disposition` flags."""
    return MAP_ELEMENTS[column]


def map_example(column: str) -> str:
    """The key a hint quotes for one map column."""
    return _MAP_EXAMPLES[column]


def flag_hint(key: str) -> str:
    """What to write instead of a key outside the closed disposition set."""
    matches = difflib.get_close_matches(key, list(DISPOSITION_KEYS), n=1, cutoff=0.6)
    if matches:
        return f"did you mean '{matches[0]}'?"
    return f"the disposition flags are {', '.join(DISPOSITION_KEYS)}"


def flag_error(
    label: str, key: str, node: exp.Expr | None, fallback: exp.Expr | None
) -> SqlmpegError:
    """A disposition key outside the closed set: the flag set is ffmpeg's own."""
    return _error(
        ErrorCode.UNSUPPORTED_SQL,
        f"'{label}' is not a disposition flag",
        node,
        fallback=fallback,
        hint=flag_hint(key),
    )


def _map_column_type(
    ref: tuple[str, str], label: str, node: exp.Expr | None, fallback: exp.Expr | None
) -> str:
    """The type one folded map path reads: a tag is text, a flag is boolean."""
    column, key = ref
    if column == TAGS_COLUMN:
        return "text"
    if key not in DISPOSITION_KEYS:
        raise flag_error(f"{label}.{column}.{key}", key, node, fallback)
    return "boolean"


# compile-time value shapes

# How a literal and the type a comparison wanted are named back to the writer.
_LITERAL_NAMES = {
    "text": "a string",
    "number": "a number",
    "boolean": "true or false",
}
_WANTED_LITERALS = {
    "text": "a quoted string",
    "number": "a number",
    "boolean": "the bare word true or false",
}


def _literal_type(node: exp.Expr | None) -> str | None:
    """The type a written literal has, or None if `node` is not one."""
    if isinstance(node, exp.Boolean):
        return "boolean"
    if isinstance(node, exp.Literal):
        return "text" if node.is_string else "number"
    return None


# The operator nodes sqlglot builds for `+ - * /`, already nested in written
# precedence (`a + b * c` puts the Mul under the Add). Unary `-x` is exp.Neg,
# which the value grammar handles alongside them.
_ARITHMETIC = exp.Add | exp.Sub | exp.Mul | exp.Div

# How each operator is named back to the writer in a rejection.
_ARITHMETIC_NAMES: dict[type[exp.Expr], str] = {
    exp.Add: "'+'",
    exp.Sub: "'-'",
    exp.Mul: "'*'",
    exp.Div: "'/'",
}


def is_value_expr(node: exp.Expr | None) -> bool:
    """True for a shape that is a compile-time VALUE and can never be a stream.

    The dispatch test every context shares: a projection deciding whether it
    is a tag column, a comparison deciding whether both sides go through the
    value grammar, a call argument deciding whether it is computed per row.
    Bare literals and columns are left out on purpose -- they are already
    handled where they appear, and a bare column may well be a stream.
    """
    return isinstance(node, exp.Case | exp.DPipe | exp.Cast | _ARITHMETIC)


def _is_input_column(name: str) -> bool:
    """True for a column name an INPUT alias exposes, structural or tag path."""
    return name in INPUT_COLUMNS or tag_key(name) is not None


def _input_disposition_error(
    alias: str, node: exp.Expr | None, fallback: exp.Expr | None
) -> SqlmpegError:
    """``<input>.disposition``: a container has none, only its streams do."""
    return _error(
        ErrorCode.UNSUPPORTED_SQL,
        f"'{alias}.{DISPOSITION_COLUMN}' is a stream field, not a container one",
        node,
        fallback=fallback,
        hint=f"read it off a track row, e.g. unnest({alias}.audio) t ... "
        f"t.{DISPOSITION_COLUMN}.{DISPOSITION_KEYS[0]}",
    )


def _is_input_disposition(name: str) -> bool:
    """True for a folded ``<input>.disposition.<key>`` or the bare column."""
    return name == DISPOSITION_COLUMN or disposition_key(name) is not None


def _is_input_duration(node: exp.Expr | None, scope: dict[str, str]) -> bool:
    """True for ``<input alias>.duration``, the one numeric scalar an input exposes."""
    return _input_value_name(node, scope) == INPUT_DURATION_COLUMN


def _is_input_tag(node: exp.Expr | None, scope: dict[str, str]) -> bool:
    """True for ``<input alias>.tags.<key>``, a text scalar."""
    name = _input_value_name(node, scope)
    return name is not None and tag_key(name) is not None


def _input_value_name(node: exp.Expr | None, scope: dict[str, str]) -> str | None:
    """The column name `node` reads off an INPUT alias, else None."""
    if not isinstance(node, exp.Column):
        return None
    table_node = node.args.get("table")
    if table_node is None or scope.get(_ident_name(table_node)) != "input":
        return None
    return _ident_name(node.this)


def _is_row_column(node: exp.Expr | None, scope: dict[str, str]) -> bool:
    """True for ``<track-row alias>.<column>``."""
    if not isinstance(node, exp.Column):
        return False
    table_node = node.args.get("table")
    return table_node is not None and scope.get(_ident_name(table_node)) == "row"


def references_row_alias(node: exp.Expr, row_aliases: set[str]) -> bool:
    """True if `node` reads any column of a track-row table in `row_aliases`.

    The fan-out dispatch: a ``TO`` expression that reads one is one file per
    row, and one that reads none is an ordinary constant path.
    """
    for sub in node.walk():
        if not isinstance(sub, exp.Column):
            continue
        table_node = sub.args.get("table")
        if table_node is not None and _ident_name(table_node) in row_aliases:
            return True
    return False


def _is_row_bounded_window(conjunct: exp.Expr, scope: dict[str, str]) -> bool:
    """True for a time window on a non-row alias with row columns as bounds."""
    parsed = _time_bounds(conjunct)
    if parsed is None:
        return False
    column = parsed[0]
    table_node = column.args.get("table")
    if table_node is None or scope.get(_ident_name(table_node)) == "row":
        return False
    return _ident_name(column.this) == "t"


# subscripts


def subscript_index(bracket: exp.Bracket) -> int | None:
    """The 1-based stream index the user wrote, or None if it is not a literal.

    sqlglot parses ``a.video[1]`` under ``read="postgres"`` into
    ``Bracket(this=Column, expressions=[Literal(0)])`` — it subtracts the
    dialect's ``INDEX_OFFSET`` from any subscript it annotates as an integer,
    and its generator adds it back. This undoes that rebase, so a query written
    ``a.video[2]`` returns ``2``.

    Returns None for everything the parser rejects anyway: a missing or multiple
    subscript, a non-integer literal (string, float, NULL, boolean), a
    non-literal expression, and 0 or negative indices — sqlglot represents those
    as ``exp.Neg`` after the rebase, never as a bare literal.
    """
    expressions = bracket.expressions
    if len(expressions) != 1:
        return None
    index = expressions[0]
    if not isinstance(index, exp.Literal) or index.is_string:
        return None
    text = str(index.this)
    if not _DIGITS_RE.match(text):
        return None
    return int(text) + _INDEX_OFFSET


# subscript metadata accessors: <alias>.<type>[k].<column>
def subscript_metadata_shape(node: exp.Expr) -> tuple[exp.Bracket, str] | None:
    """Recognize ``<alias>.<type>[k].<column>``; return ``(bracket, name)``.

    VERIFIED under sqlglot 30.17 ``read="postgres"``: both
    ``f.audio[1].codec`` and the strictly-Postgres cast spelling
    ``(f.audio[1]).codec`` arrive as ``exp.Dot(this=Bracket(...) |
    Paren(Bracket(...)), expression=Identifier(<name>))`` — identical
    semantics, told apart only by an extra ``Paren`` this unwraps. Returns
    ``None`` for anything else, ``f.audio.codec`` (no subscript, a plain
    3-part ``exp.Column``) included — that shape never reaches here at all.
    A map path is one Dot deeper, and `_normalize_map_paths` folds it into
    this same shape before any of this runs.
    """
    if not isinstance(node, exp.Dot):
        return None
    inner = node.this
    if isinstance(inner, exp.Paren):
        inner = inner.this
    if not isinstance(inner, exp.Bracket):
        return None
    ident = node.args.get("expression")
    if not isinstance(ident, exp.Identifier):
        return None
    return inner, _ident_name(ident)


def _subscript_label(bracket: exp.Bracket) -> str:
    """``<alias>.<type>[k]``, written the way a user would paste it."""
    inner = bracket.this
    alias = "?"
    array_column = "?"
    if isinstance(inner, exp.Column):
        alias = _ident_name(inner.args.get("table"))
        array_column = _ident_name(inner.this)
    index = subscript_index(bracket)
    return f"{alias}.{array_column}[{index}]"


def _accessor_label(bracket: exp.Bracket, name: str) -> str:
    """``<alias>.<type>[k].<name>``, written the way a user would paste it."""
    return f"{_subscript_label(bracket)}.{column_label(name)}"


# stars


def star_qualifier(node: exp.Expr) -> str | None:
    """The alias a star projection expands, or None if `node` is not a star one.

    VERIFIED shapes (sqlglot 30.17, ``read="postgres"``):

    * ``SELECT *``   -> ``exp.Star()`` sits directly in ``Select.expressions``.
    * ``SELECT a.*`` -> ``exp.Column(this=Star(), table=Identifier(a))``.

    Returns ``""`` for the unqualified form (which stands for every FROM alias)
    and the folded alias name for the qualified one. Everything else — a star
    under an ``Alias`` (``* AS x``), a ``Bracket`` (``a.*[1]``), or a call
    (``scale(a.*, 0.5)``, ``count(*)``) — is not a star PROJECTION and comes
    back None, so the generic star rejection still fires on it.

    A ``Column(this=Star())`` with no ``table`` is not something sqlglot
    produces for any query we accept; it is treated as the bare form rather
    than crashing.
    """
    if isinstance(node, exp.Star):
        return ""
    if isinstance(node, exp.Column) and isinstance(node.this, exp.Star):
        table = node.args.get("table")
        return _ident_name(table) if isinstance(table, exp.Expr) else ""
    return None


# named arguments


def kwarg_name(kwarg: exp.Kwarg) -> str:
    """The option name of a ``name => value`` argument, VERBATIM.

    Deliberately NOT folded the Postgres way: the name is not an identifier in
    any table, it is an ffmpeg AVOption name, and those are case-sensitive
    (``gblur``'s ``sigmaV``, ``loudnorm``'s ``I``). Empty string if sqlglot put
    something nameless on the left of the ``=>``.
    """
    left = kwarg.this
    if not isinstance(left, exp.Expr):
        return ""
    return str(left.name)


# time predicates: WHERE <alias>.t ...
def _time_bounds(
    conjunct: exp.Expr,
) -> tuple[exp.Column, exp.Expr | None, exp.Expr | None, bool] | None:
    """Parse one WHERE conjunct as a time-range predicate, or None if it isn't one.

    Returns ``(column, low, high, strict)``:

    * ``exp.Between`` (non-symmetric, column on the left) gives both bounds.
    * ``exp.GTE`` / ``exp.LTE`` give ONE bound each, in EITHER operand order:
      ``a.t >= 120`` and ``120 <= a.t`` are the exact same predicate (lower
      bound 120), not an approximation of each other. VERIFIED under sqlglot
      30.17 ``read="postgres"``: sqlglot does NOT normalize operand order at
      parse time -- ``120 <= a.t`` parses to
      ``LTE(this=Literal(120), expression=Column(a.t))`` verbatim, so both
      shapes are handled explicitly here rather than relying on a canonical
      form. The mapping: for ``LTE``/``LT`` (`this OP expression`, `this` no
      greater than `expression`), a `Column` on the left is an upper bound
      and a `Column` on the right is a mirrored lower bound; for
      ``GTE``/``GT`` it is the reverse.
    * ``exp.GT`` / ``exp.LT`` match the same shapes but come back with
      `strict` True, so the caller can reject them with a dedicated hint
      instead of the generic "unsupported predicate" one (guardrail #3:
      reject a strict bound, never approximate it as its closed neighbor).

    None for anything else a caller should fall back to a generic rejection
    for: a symmetric BETWEEN, a non-column BETWEEN subject (e.g. ``* BETWEEN
    1 AND 2``), an inequality with no column operand, ``=``, etc.
    """
    if isinstance(conjunct, exp.Between):
        if conjunct.args.get("symmetric"):
            return None
        column = conjunct.this
        if not isinstance(column, exp.Column):
            return None
        low = conjunct.args.get("low")
        high = conjunct.args.get("high")
        return (
            column,
            low if isinstance(low, exp.Expr) else None,
            high if isinstance(high, exp.Expr) else None,
            False,
        )
    if isinstance(conjunct, exp.GTE | exp.GT | exp.LTE | exp.LT):
        strict = isinstance(conjunct, exp.GT | exp.LT)
        is_lte = isinstance(conjunct, exp.LTE | exp.LT)
        this = conjunct.this
        expression = conjunct.args.get("expression")
        if isinstance(this, exp.Column) and isinstance(expression, exp.Expr):
            # <alias>.t <= X (upper) / <alias>.t >= X (lower), strict variants
            return (this, None, expression, strict) if is_lte else (this, expression, None, strict)
        if isinstance(expression, exp.Column) and isinstance(this, exp.Expr):
            # X <= <alias>.t (lower, mirrored) / X >= <alias>.t (upper, mirrored)
            return (expression, this, None, strict) if is_lte else (expression, None, this, strict)
        return None
    return None


def _literal_seconds(node: exp.Literal) -> float | None:
    """Best-effort python float of a time-bound literal, or None if malformed.

    None (never raises) on purpose: an unparseable literal that still slips
    past sqlglot's own tokenizer (e.g. ``1e``) is a real rejection, but not
    this function's -- ``lower._number`` raises the typed one when it
    converts the same literal, so this only skips the (optional) empty-window
    check rather than pre-empting that error with a worse message.
    """
    try:
        return float(str(node.this))
    except (TypeError, ValueError):
        return None


# parse


def _parse_error_position(err: Exception) -> tuple[int, int]:
    errors = getattr(err, "errors", None)
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            line = first.get("line")
            col = first.get("col")
            if isinstance(line, int) and isinstance(col, int):
                return (max(line, 1), max(col, 1))
    return _DEFAULT_POS


def _parse_error_message(err: Exception) -> str:
    errors = getattr(err, "errors", None)
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            description = first.get("description")
            if isinstance(description, str) and description:
                return _ANSI_RE.sub("", description).strip()
    text = _ANSI_RE.sub("", str(err)).strip()
    return text.splitlines()[0] if text else err.__class__.__name__


def parse(text: str) -> exp.Expression:
    """Parse SQL text into a sqlglot AST using the Postgres dialect.

    ONE statement comes back as itself; a SCRIPT comes back as an
    ``exp.Block`` whose ``expressions`` are the statements. :func:`_statements`
    is the only thing that should look at that distinction.

    ``sqlglot.parse_one`` is deliberately kept over the plural
    ``sqlglot.parse``. VERIFIED (sqlglot 30.17, ``read="postgres"``) they agree
    on every script we accept — ``parse`` returns the same nodes as a flat list
    that ``parse_one`` wraps in a ``Block`` — and differ only on degenerate
    input: ``parse`` yields ``None`` list entries for empty statements
    (``;``, a leading ``;``, ``a;;``) where ``parse_one`` either absorbs them
    (a single trailing ``;``) or raises ``ParseError`` ("No expression was
    parsed"). Keeping ``parse_one`` keeps that PARSE_ERROR, and every other
    single-statement behavior, exactly as it was.

    Raises ``SqlmpegError(PARSE_ERROR)`` — and nothing else — on any failure.
    """
    if not text.strip():
        raise SqlmpegError(
            ErrorCode.PARSE_ERROR,
            "empty query",
            line=1,
            col=1,
            hint="write a SELECT statement",
        )
    try:
        tree = sqlglot.parse_one(text, read="postgres")
    except ParseError as err:
        line, col = _parse_error_position(err)
        raise SqlmpegError(
            ErrorCode.PARSE_ERROR, _parse_error_message(err), line=line, col=col
        ) from err
    except SqlglotError as err:
        raise SqlmpegError(
            ErrorCode.PARSE_ERROR, _parse_error_message(err), line=1, col=1
        ) from err
    except RecursionError as err:
        raise SqlmpegError(
            ErrorCode.PARSE_ERROR,
            "query nests too deeply to parse",
            line=1,
            col=1,
            hint="flatten the expression: fewer nested parentheses or calls",
        ) from err
    except Exception as err:  # sqlglot bug / anything at all
        raise SqlmpegError(
            ErrorCode.PARSE_ERROR,
            f"could not parse SQL ({err.__class__.__name__})",
            line=1,
            col=1,
        ) from err
    if not isinstance(tree, exp.Expression):
        raise SqlmpegError(ErrorCode.PARSE_ERROR, "no statement found", line=1, col=1)
    return tree


def _statements(tree: exp.Expr) -> list[exp.Expr]:
    """The statement list `tree` stands for, in script order.

    VERIFIED (sqlglot 30.17, ``read="postgres"``): ``parse_one`` returns the
    statement itself for one-statement text — a single trailing ``;`` and any
    trailing whitespace are absorbed — and an ``exp.Block`` with one
    ``expressions`` entry per statement for anything with an INTERNAL
    separator. An EMPTY statement (the second ``;`` of ``SELECT ...;;``) lands
    in that list as a literal ``None``, which ``Block.expressions``' own type
    annotation does not admit, so it is filtered out here: an extra semicolon
    is not a statement.
    """
    if not isinstance(tree, exp.Block):
        return [tree]
    return [statement for statement in tree.expressions if isinstance(statement, exp.Expr)]


# resolve


@dataclass(frozen=True)
class RawSinkOption:
    """One ``WITH (name value)`` pair, still as sqlglot nodes.

    `name` is folded lowercase the Postgres way. `value` is the raw sqlglot
    node — lower turns it into a python str/int/bool and checks it against
    ``sqlmpeg.sink.SINK_OPTIONS``; the parser deliberately knows nothing about
    which options exist.

    `name_node` is the ``exp.Var`` the name came from. VERIFIED (sqlglot
    30.17): sqlglot records NO token position on it, nor on a ``Boolean`` /
    ``Var`` / ``Null`` value, so it is kept only as a future-proof anchor —
    what actually carries a line/col today is a ``Literal`` `value`, with
    ``RawSink.path_node`` as the fallback.
    """

    name: str
    value: exp.Expr
    name_node: exp.Expr


@dataclass(frozen=True)
class RawSink:
    """``COPY (query) TO 'path' WITH (...)`` as the parser saw it.

    Shape only: the path is known to be a single string literal (or, for a
    csv sink, possibly absent — ``TO STDOUT``) and the option names
    to be unique, but nothing here has been checked against the option table
    yet. ``sqlmpeg.lower`` turns this into ``sqlmpeg.ir.SinkUnit`` (a media
    COPY) or a table/CSV result.

    A sink carries the query it wraps: a script has one COPY per
    OUTPUT GROUP, and each group is a whole query of its own. ``query`` and
    ``branches`` are the same pair ``Resolved.select``/``Resolved.branches``
    are for the single-sink case, fully validated — for a one-COPY statement
    they ARE that pair.

    ``is_csv`` is True exactly when ``WITH (...)`` names
    ``format`` with value ``'csv'`` — Postgres's own rule for what makes a
    COPY a table sink rather than a media one. It is decided HERE, from the
    raw option shape alone, because it changes what the wrapped query is even
    allowed to select (metadata columns become legal SELECT outputs) and
    whether ``TO STDOUT`` is a legal target — both decided before the option
    VALUES are otherwise interpreted, which stays lower's job as always.
    ``path`` is None for a csv sink's ``TO STDOUT`` and for a parenthesized
    ``TO (<expression>)``, whose text lower computes; a plain media sink's path
    is always a real string.

    ``path_expr`` is that parenthesized expression, or None for a quoted path.
    It fans the COPY out into one command per surviving row when it references
    a track-row column, and is an ordinary constant path when it does not.
    """

    path: str | None
    path_node: exp.Expr
    query: QueryExpr
    branches: tuple[exp.Select, ...]
    options: tuple[RawSinkOption, ...] = ()
    is_csv: bool = False
    path_expr: exp.Expr | None = None


@dataclass(frozen=True)
class RawInputOption:
    """One ``input('path', name => value)`` trailing named argument.

    Shape only, mirroring ``RawSinkOption``: `lower` turns `value` into a
    python scalar and checks it against ``sqlmpeg.inputs.INPUT_OPTIONS``; the
    parser deliberately knows nothing about which options exist.

    `name` is kept VERBATIM (see :func:`kwarg_name`) -- input options reuse the
    ``name => value`` named-argument syntax every call takes, NOT COPY's folded
    ``WITH (name value)`` one, so case matters exactly as it does for a dynamic
    filter option's name.

    `name_node` is the ``exp.Kwarg``'s ``Var`` (or the ``Kwarg`` itself, if
    that shape ever changes) -- it carries no token position, same gap a sink
    option name has. `path_node` is the input()'s own path string literal,
    the nearest node that reliably carries one, kept as the last-resort
    anchor exactly like ``RawSink.path_node``.
    """

    name: str
    value: exp.Expr
    name_node: exp.Expr
    path_node: exp.Expr


@dataclass(frozen=True)
class RawSourceOption:
    """One ``ffmpeg.<source>(name => value)`` option.

    Shape only, exactly like :class:`RawInputOption` -- but validated against
    the INSTALLED ffmpeg's option table for that source filter (the same
    ``Registry.options`` path a tier-2 call's named arguments take), not
    against a curated table, so `name` is kept VERBATIM: ffmpeg AVOption
    names are case-sensitive.

    `name_node` is the ``exp.Kwarg``'s ``Var``, which carries no token
    position; `call_node` is the ``exp.Anonymous`` of the source call, which
    does (it sits on the source NAME) and is the last-resort anchor.
    """

    name: str
    value: exp.Expr
    name_node: exp.Expr
    call_node: exp.Expr


@dataclass(frozen=True)
class RawSource:
    """``FROM ffmpeg.<source>(<named options>) alias``.

    A generated source has NO ffmpeg input index -- it lowers to a zero-input
    filter node, and there is no ``-i`` for it -- so it appears in
    ``Resolved.source_filters`` and in NEITHER ``Resolved.input_paths`` nor
    ``Resolved.sources``.

    `name` is the source filter's name, folded lowercase (function names are
    case-insensitive in this dialect; ffmpeg's own filter names are lowercase).
    Whether such a source exists, and what options it takes, is the installed
    ffmpeg's business -- lower asks ``Registry.get_source`` / ``options``.
    """

    alias: str
    name: str
    options: tuple[RawSourceOption, ...]
    call_node: exp.Expr


@dataclass(frozen=True)
class RawRowJoin:
    """How one FROM item attaches to the ones before it.

    `kind` is ``"cross"`` (a comma source — and, between two row tables, the
    bounded compile-time cross join), ``"inner"``, ``"left"``, ``"full"``, or
    ``"right"`` (which exists only to be rejected by name). `on` is the JOIN's
    predicate, already shape-checked by resolve and evaluated by lower; `node`
    is the ``exp.Join`` itself, the anchor for anything either pass rejects
    about the join rather than about one of its operands.
    """

    kind: str
    on: exp.Expr | None
    node: exp.Join


@dataclass(frozen=True)
class RawTrackRows:
    """``unnest(<source>.<column>) <alias>`` in FROM.

    A track-row TABLE: one row per stream of that array — the row IS the
    stream, and each piece of probed metadata is in a
    column of its own (``ROW_SCHEMAS[column]``). Shape only, as everywhere
    else in this pass — how many rows there are, and what is in them, is a
    property of the FILE, so lower (the pass with the probes) builds them.

    `source` is the INPUT alias whose array was unnested, already known to be
    comma-visible at this point in the FROM clause; `column` is one of
    ``video``/``audio``/``subtitle``/``data``/``chapters`` and decides the row
    schema. `node` is the ``exp.Unnest`` itself, the anchor for anything lower
    rejects about the table rather than about a column of it.

    ``chapters`` is the one non-stream array: its rows are not streams, so
    they carry chapter metadata and nothing selectable.
    """

    alias: str
    source: str
    column: str
    node: exp.Expr


@dataclass(frozen=True)
class RawValuesTable:
    """``WITH <alias>(<columns>) AS (VALUES (...), ...)`` -- a literal row table.

    Reachable only as a sink option's value (``chapters <alias>``) in v1;
    selecting FROM one directly stays rejected (see ``_add_table``). `columns`
    is the alias's column list, in written order; `rows` is one tuple of
    literal expressions (or ``NULL``) per VALUES row, each the same length as
    `columns`. Types are whatever each cell's own literal is -- there is no
    declared schema, just literals read at face value.
    """

    alias: str
    columns: tuple[str, ...]
    rows: tuple[tuple[exp.Literal | exp.Null, ...], ...]
    node: exp.Expr


@dataclass
class Resolved:
    """Output of the resolve pass — the validated query plus its input table."""

    select: QueryExpr
    """The query being compiled, CTEs still attached; ``exp.Union`` for UNION ALL.

    For anything with a COPY this MIRRORS ``sinks[0].query``; lower reads it
    only for the bare-SELECT case (no COPY at all), and walks ``sinks``
    otherwise. Retiring the field is parser cleanup, not this wave's.
    """

    input_paths: list[str]
    """``-i`` order; the list index is the ffmpeg input index. May repeat paths."""

    sources: dict[str, int]
    """Input alias -> index into ``input_paths``. One entry per distinct alias."""

    ctes: dict[str, QueryExpr] = field(default_factory=dict)
    """Named query binding -> its query, in DEFINITION order.

    Both kinds of binding live here, because a view IS a CTE to everything
    downstream: a script's ``CREATE VIEW``s, the CTEs of their
    bodies, and the CTEs of each COPY's own ``WITH``, in the order they were
    written. Lower walks this dict once and binds each entry by name, which is
    why the order matters and why views need no machinery of their own.
    """

    branches: list[exp.Select] = field(default_factory=list)
    """``select`` flattened into UNION ALL branches; a single element if not a union."""

    sinks: list[RawSink] = field(default_factory=list)
    """One entry per ``COPY``, in script order; EMPTY for a bare SELECT.

    Resolve accepts any number and lower turns each into one ``ir.SinkUnit``
    -- one output FILE of the single ffmpeg command the script compiles to.
    """

    views: dict[str, QueryExpr] = field(default_factory=dict)
    """``CREATE VIEW`` name -> its body, in definition order.

    A strict subset of ``ctes`` (same objects): this is the introspection
    view, ``ctes`` is the one lower walks.
    """

    input_options: dict[str, tuple[RawInputOption, ...]] = field(default_factory=dict)
    """Input alias -> its trailing named options. Only aliases that
    wrote at least one option get an entry -- absent means none."""

    source_filters: dict[str, RawSource] = field(default_factory=dict)
    """``FROM ffmpeg.<source>(...) alias`` records, keyed by alias, in FROM
    order across the whole query. Disjoint from ``sources``: a
    generated source has no ``-i`` and therefore no input index."""

    track_rows: dict[str, RawTrackRows] = field(default_factory=dict)
    """``unnest(<input>.<type>) alias`` records, keyed by ROW alias, in FROM
    order across the whole script. Disjoint from every other name
    table: a row alias takes no ``-i`` of its own — its streams belong to the
    input alias named in ``RawTrackRows.source`` — and shares the one flat
    namespace views, CTEs and aliases live in."""

    values_ctes: dict[str, RawValuesTable] = field(default_factory=dict)
    """``WITH <alias>(<cols>) AS (VALUES ...)`` records, keyed by alias.
    Disjoint from ``ctes``: a VALUES CTE is never FROM-selectable, only usable
    as a sink option's value (``chapters <alias>``)."""


def _listed_columns(names: Iterable[str]) -> str:
    return ", ".join(sorted(names))


def chapters_unnest_hint(alias: str) -> str:
    """What to write instead of reaching into ``<alias>.chapters`` directly."""
    return (
        f"unnest it into rows: unnest({alias}.{CHAPTERS_COLUMN}) c, then read "
        "c.index, c.title, c.start_t and c.end_t"
    )


def _unwrap_paren(node: exp.Expr) -> exp.Expr:
    """Strip redundant parentheses around an EXPRESSION (never around a query)."""
    while isinstance(node, exp.Paren) and isinstance(node.this, exp.Expr):
        node = node.this
    return node


def _bare_array_error(sub: exp.Column, fallback: exp.Expr) -> SqlmpegError | None:
    """``<alias>.<type>.<name>`` (no subscript) is a 3-part ``exp.Column``.

    Probed metadata belongs to ONE track; an array of them has none of its
    own, so this shape is always wrong rather than merely a
    qualified name the generic rejection would also catch -- a dedicated
    check gives the specific fix (subscript one track, or unnest the array)
    instead of the generic "qualified column names" message.
    """
    db_node = sub.args.get("db")
    table_node = sub.args.get("table")
    if (
        isinstance(db_node, exp.Expr)
        and not sub.args.get("catalog")
        and isinstance(table_node, exp.Expr)
        and _ident_name(table_node) in UNNEST_COLUMNS
    ):
        alias = _ident_name(db_node)
        array_column = _ident_name(table_node)
        if array_column == CHAPTERS_COLUMN:
            message = (
                f"'{alias}.{CHAPTERS_COLUMN}.{sub.name}' needs a row: an array "
                "has no metadata of its own"
            )
            hint = (
                f"unnest the array (unnest({alias}.{CHAPTERS_COLUMN}) c) and read "
                f"c.{sub.name}"
            )
        else:
            message = (
                f"'{alias}.{array_column}.{sub.name}' needs a subscript: an "
                "array has no metadata of its own"
            )
            hint = (
                f"subscript one track ({alias}.{array_column}[1].{sub.name}) or "
                f"unnest the whole array (unnest({alias}.{array_column}) t)"
            )
        return _error(
            ErrorCode.UNSUPPORTED_SQL, message, sub, fallback=fallback, hint=hint
        )
    return None


def _referenced_aliases(node: exp.Expr) -> set[str]:
    """Every table qualifier a column of `node` names, folded the Postgres way."""
    aliases: set[str] = set()
    for sub in node.walk():
        if isinstance(sub, exp.Column):
            table_node = sub.args.get("table")
            if table_node is not None:
                aliases.add(_ident_name(table_node))
    return aliases


def _describe_unnest_arg(node: object) -> str:
    """What an unusable ``unnest(...)`` argument is, for its rejection message."""
    if isinstance(node, exp.Bracket):
        return "one subscripted stream"
    if isinstance(node, exp.Column) and isinstance(node.this, exp.Star):
        return "a star"
    if isinstance(node, exp.Literal):
        return "a literal"
    if isinstance(node, exp.Expr):
        return f"{node.__class__.__name__.lower()}(...)"
    return "that"


def _join_spec(join: exp.Join) -> RawRowJoin:
    """How one ``exp.Join`` entry attaches its item, from its own args.

    Pure shape reading, no validation (resolve does that): the ``side``/``kind``
    matrix in the module docstring, folded into the four kinds lower evaluates
    plus ``"right"``, which exists only so resolve can reject it by name. A
    comma source carries none of the args and is ``"cross"`` — the bounded
    compile-time cross join between two row tables.
    """
    side = str(join.args.get("side") or "").upper()
    kind = str(join.args.get("kind") or "").upper()
    on = join.args.get("on")
    if side == "LEFT":
        name = "left"
    elif side == "FULL":
        name = "full"  # `FULL JOIN` and `FULL OUTER JOIN` are the same join
    elif side == "RIGHT":
        name = "right"
    elif kind == "CROSS":
        name = "cross"
    elif kind == "INNER" or on is not None:
        name = "inner"
    else:
        name = "cross"
    return RawRowJoin(
        kind=name, on=on if isinstance(on, exp.Expr) else None, node=join
    )


def from_entries(select: exp.Select) -> list[tuple[exp.Expr, RawRowJoin | None]]:
    """The FROM items of one branch, each with the join that attached it.

    VERIFIED under sqlglot 30.17 ``read="postgres"``: the FIRST item is
    ``From.this`` (join ``None`` — nothing attaches it) and every later one —
    comma source or explicit JOIN alike — is the ``this`` of an ``exp.Join`` in
    ``Select.args["joins"]``, whose own args say which kind it is
    (:func:`_join_spec`).
    """
    entries: list[tuple[exp.Expr, RawRowJoin | None]] = []
    from_ = select.args.get("from_")
    if isinstance(from_, exp.From) and isinstance(from_.this, exp.Expr):
        entries.append((from_.this, None))
    for join in select.args.get("joins") or []:
        if isinstance(join, exp.Join) and isinstance(join.this, exp.Expr):
            entries.append((join.this, _join_spec(join)))
    return entries


def from_items(select: exp.Select) -> list[exp.Expr]:
    """The FROM items of one branch, in written order (join specs dropped)."""
    return [item for item, _ in from_entries(select)]


def _unnest_aliases(select: exp.Select) -> set[str]:
    """The row-table names this branch's FROM clause binds with ``unnest``."""
    names: set[str] = set()
    for item in from_items(select):
        if not isinstance(item, exp.Unnest):
            continue
        alias_node = item.args.get("alias")
        if isinstance(alias_node, exp.TableAlias) and isinstance(alias_node.this, exp.Expr):
            names.add(_ident_name(alias_node.this))
    return names


def _name_the_stream(part: exp.Expr, names: set[str]) -> None:
    """Rewrite the bare row aliases inside one expression, in place."""
    for sub in part.walk():
        if not isinstance(sub, exp.Column) or sub.args.get("table") is not None:
            continue
        identifier = sub.this
        if not isinstance(identifier, exp.Identifier):
            continue
        if _ident_name(identifier) not in names:
            continue
        sub.set("this", exp.to_identifier(ROW_STREAM, quoted=False))
        sub.set("table", identifier)


def _normalize_row_aliases(select: exp.Select, path_expr: exp.Expr | None = None) -> None:
    """Rewrite every bare row alias into that row's stream column, in place.

    A row record used where a stream is expected IS that stream, so ``SELECT
    a``, ``array_agg(a)``, ``scale(a, ...)``, ``COALESCE(a, ...)`` and ``GROUP
    BY a`` all name the row's stream. The alias standing alone becomes
    ``a.<ROW_STREAM>`` here, once, and every pass after this one reads the
    stream column it has always read. The written identifier moves to the
    qualifier, so an error still anchors on the line and column the user
    typed.

    The FROM clause and any CTE bodies are left alone: an alias is only bare
    where a value goes. A fan-out ``TO`` expression is included, so a stream
    written there is rejected as a stream rather than as an unknown name.
    """
    names = _unnest_aliases(select)
    if not names:
        return
    for part in _value_parts(select, path_expr):
        _name_the_stream(part, names)


def _value_parts(select: exp.Select, path_expr: exp.Expr | None) -> list[exp.Expr]:
    """The parts of one branch a value may be written in.

    Everything but the FROM clause and any CTE bodies -- each of those is
    normalized as the branch it belongs to. A fan-out ``TO`` expression is
    included when the caller has one.
    """
    parts: list[exp.Expr] = [p for p in select.expressions if isinstance(p, exp.Expr)]
    if path_expr is not None:
        parts.append(path_expr)
    for key in ("where", "group", "order"):
        part = select.args.get(key)
        if isinstance(part, exp.Expr):
            parts.append(part)
    for join in select.args.get("joins") or []:
        on = join.args.get("on") if isinstance(join, exp.Join) else None
        if isinstance(on, exp.Expr):
            parts.append(on)
    return parts


def _fold_map_path(sub: exp.Expr) -> None:
    """Fold one written ``... .<map>.<key>`` into a single column name, in place."""
    if isinstance(sub, exp.Column):
        # `<alias>.<map>.<key>` -- a 3-part column, qualifier and all.
        db_node = sub.args.get("db")
        table_node = sub.args.get("table")
        map_column = _ident_name(table_node)
        if (
            not sub.args.get("catalog")
            and isinstance(db_node, exp.Expr)
            and map_column in MAP_COLUMNS
        ):
            key = _ident_name(sub.this)
            sub.set("this", exp.to_identifier(map_path(map_column, key), quoted=False))
            sub.set("table", db_node)
            sub.set("db", None)
        return
    # `<alias>.<array>[k].<map>.<key>` -- a Dot over the subscript's own Dot.
    if not isinstance(sub, exp.Dot) or not isinstance(sub.this, exp.Dot):
        return
    inner = sub.this
    ident = sub.args.get("expression")
    map_column = _ident_name(inner.args.get("expression"))
    if map_column not in MAP_COLUMNS:
        return
    if not isinstance(ident, exp.Identifier) or subscript_metadata_shape(inner) is None:
        return
    sub.set("this", inner.this)
    sub.set(
        "expression",
        exp.to_identifier(map_path(map_column, _ident_name(ident)), quoted=False),
    )


def _normalize_map_paths(select: exp.Select, path_expr: exp.Expr | None = None) -> None:
    """Fold every ``.tags.<key>`` and ``.disposition.<key>`` path into one
    internal column name, in place.

    Reading a map is the one read with two written parts after the qualifier.
    Folding them here means no later pass has to know the shape: an entry is a
    column with an unspellable name, looked up like any other.
    """
    for part in _value_parts(select, path_expr):
        for sub in list(part.walk()):
            _fold_map_path(sub)


def _has_row_source(select: exp.Select, visible: set[str]) -> bool:
    """True if this branch's FROM clause holds rows: ``unnest(...)`` or a
    reference to a CTE or view -- each of which contributes rows to group
    over, and admits the ORDER BY carve-out."""
    for item in from_items(select):
        if isinstance(item, exp.Unnest):
            return True
        if (
            isinstance(item, exp.Table)
            and isinstance(item.this, exp.Identifier)
            and _ident_name(item.this) in visible
        ):
            return True
    return False


def _projection_expr(projection: exp.Expr) -> exp.Expr:
    """A SELECT column with its ``AS`` alias and parentheses peeled off."""
    inner = projection.this if isinstance(projection, exp.Alias) else projection
    return _unwrap_paren(inner) if isinstance(inner, exp.Expr) else projection


def group_keys(select: exp.Select) -> list[exp.Expr]:
    """The GROUP BY key expressions of a branch, empty when it has none."""
    group = select.args.get("group")
    if not isinstance(group, exp.Group):
        return []
    return [key for key in group.expressions if isinstance(key, exp.Expr)]


def _array_aggs(select: exp.Select) -> list[exp.ArrayAgg]:
    """Every ``array_agg(...)`` in the SELECT list, in written order."""
    found: list[exp.ArrayAgg] = []
    for projection in select.expressions:
        if not isinstance(projection, exp.Expr):
            continue
        found += [sub for sub in projection.walk() if isinstance(sub, exp.ArrayAgg)]
    return found


def is_grouped(select: exp.Select) -> bool:
    """True for a branch that aggregates: a GROUP BY, an array_agg, or both."""
    return bool(group_keys(select)) or bool(_array_aggs(select))


def _references_row(node: exp.Expr, scope: dict[str, str]) -> bool:
    """True if `node` reads a column of a track-row alias bound in `scope`."""
    for sub in node.walk():
        if not isinstance(sub, exp.Column):
            continue
        table_node = sub.args.get("table")
        if table_node is not None and scope.get(_ident_name(table_node)) == "row":
            return True
    return False


def _unwrap(node: exp.Expr) -> exp.Expr:
    """Strip redundant parentheses around a query."""
    while isinstance(node, exp.Subquery | exp.Paren):
        inner = node.this
        if not isinstance(inner, exp.Select | exp.Union):
            break
        if isinstance(node, exp.Subquery):
            if node.args.get("alias") is not None:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "aliased subqueries are not supported",
                    node,
                    hint="use a WITH ... AS (...) CTE instead",
                )
            _check_query_args(node, _SUBQUERY_ALLOWED, "subquery")
        node = inner
    return node


def _first_expression(value: object) -> exp.Expr | None:
    if isinstance(value, exp.Expr):
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, exp.Expr):
                return item
    return None


def _check_query_args(node: exp.Expr, allowed: frozenset[str], what: str) -> None:
    """Whitelist the arg keys a query node may carry (reject, never approximate)."""
    for key, value in node.args.items():
        if key in allowed or value is None or value is False:
            continue
        if isinstance(value, list) and not value:
            continue
        anchor = _first_expression(value)
        display = _STREAMING_CLAUSES.get(key)
        if display is not None:
            raise _error(
                ErrorCode.NO_STREAMING_EQUIVALENT,
                f"{display} has no streaming equivalent",
                anchor,
                fallback=node,
                hint=f"remove the {display} clause",
            )
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"unsupported {what} clause: {key}",
            anchor,
            fallback=node,
        )


def _collect_branches(
    node: exp.Expr, root: exp.Expr, out: list[exp.Select]
) -> None:
    node = _unwrap(node)
    if node is not root and node.args.get("with_") is not None:
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            "nested WITH clauses are not supported",
            node.args.get("with_"),
            fallback=node,
            hint="hoist the CTE to the top-level WITH",
        )
    if isinstance(node, exp.Select):
        out.append(node)
        return
    if isinstance(node, exp.Union):
        if node.args.get("distinct"):
            raise _error(
                ErrorCode.NO_STREAMING_EQUIVALENT,
                "UNION requires deduplication, which has no streaming equivalent",
                node.args.get("expression"),
                fallback=node,
                hint="use UNION ALL",
            )
        _check_query_args(node, _UNION_ALLOWED, "UNION")
        _collect_branches(node.this, root, out)
        expression = node.args.get("expression")
        if not isinstance(expression, exp.Expr):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL, "malformed UNION ALL", fallback=node
            )
        _collect_branches(expression, root, out)
        return
    raise _error(
        ErrorCode.UNSUPPORTED_SQL,
        f"unsupported statement: {node.__class__.__name__.upper()}",
        node,
        hint="sqlmpeg accepts a single SELECT, optionally a UNION ALL of SELECTs",
    )


def union_branches(query: exp.Expr) -> list[exp.Select]:
    """Flatten a query into its UNION ALL branch selects, left to right.

    Also usable on a CTE body (``Resolved.ctes[name]``), which may itself be a
    UNION ALL. A plain SELECT yields a single-element list.
    """
    out: list[exp.Select] = []
    _collect_branches(query, query, out)
    return out


# COPY ... TO ... WITH (...)  — the sink wrapper


def _is_csv_format(options: tuple[RawSinkOption, ...]) -> bool:
    """True if ``WITH (...)`` names ``format`` with value ``csv``.

    Postgres's own rule: ``FORMAT csv`` is what makes a COPY a table sink,
    decided from the option SHAPE alone (its value need not even be a
    correctly-typed one for the discriminator to work — a malformed `format`
    value just falls through to the normal media interpretation and fails
    there instead, same as today). ``_ident_name`` folds both spellings
    (``format csv`` and ``format 'csv'`` — a bare ``Var`` or a string
    ``Literal``, VERIFIED under sqlglot 30.17) the Postgres way.
    """
    for option in options:
        if option.name == "format":
            return _ident_name(option.value) == "csv"
    return False


def _sink(
    copy: exp.Copy,
) -> tuple[str | None, exp.Expr | None, exp.Expr, tuple[RawSinkOption, ...], exp.Expr, bool]:
    """Validate a top-level COPY into
    ``(path, path expression, path node, options, wrapped query, is_csv)``.

    The pieces rather than a :class:`RawSink`: a sink also carries its
    VALIDATED query, and that validation is the resolver's job, so
    the record is assembled there once the query has been through it.

    Shape only — the option table is lower's business. VERIFIED shapes under
    sqlglot 30.17 ``read="postgres"``:

    * ``kind`` is a plain bool: True for ``COPY ... FROM`` (loading), False for
      ``COPY ... TO``. It is NOT an expression, so it has no position.
    * ``files`` is a list — ``TO 'a', 'b'`` gives two entries, and ``TO STDOUT``
      / ``TO x`` / ``TO PROGRAM 'cat'`` give an ``exp.Identifier`` rather than a
      ``Literal``. ``TO (<expression>)`` gives an ``exp.Paren``, the media
      COPY's computed destination. A media COPY rejects every other shape; a
      CSV COPY additionally accepts ``TO STDOUT`` — the
      one-word ``exp.Identifier`` case — since a table sink prints, it does
      not always write a file, and rejects the ``exp.Paren`` form, which has
      no row set to fan a printed table out over.
    * ``credentials`` is always an EMPTY ``exp.Credentials()`` — writing an
      actual ``CREDENTIALS (...)`` clause makes sqlglot fall back to
      ``exp.Command`` for the whole statement, which resolve rejects anyway.
    """
    if copy.args.get("kind"):
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            "COPY FROM loads data and has no ffmpeg equivalent",
            fallback=copy,
            hint=_SINK_HINT,
        )
    _check_query_args(copy, _COPY_ALLOWED, "COPY")

    credentials = copy.args.get("credentials")
    if isinstance(credentials, exp.Expr) and credentials.args:
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            "COPY CREDENTIALS are not supported",
            credentials,
            fallback=copy,
            hint=_SINK_HINT,
        )

    files = copy.args.get("files") or []
    if len(files) != 1:
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            "COPY writes exactly one file",
            _first_expression(files[1:]),
            fallback=copy,
            hint=_SINK_HINT,
        )
    target = files[0]
    if not isinstance(target, exp.Literal | exp.Identifier | exp.Paren):
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            "COPY target must be a single-quoted file path",
            target if isinstance(target, exp.Expr) else None,
            fallback=copy,
            hint=_SINK_HINT,
        )
    options = _sink_options(copy, target)
    is_csv = _is_csv_format(options)

    path: str | None
    path_expr: exp.Expr | None = None
    if isinstance(target, exp.Paren) and isinstance(target.this, exp.Expr):
        if is_csv:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "a csv COPY takes a quoted path or STDOUT, not a TO expression",
                target,
                fallback=copy,
                hint="a TO expression writes one MEDIA file per track row; a "
                "csv sink prints one table",
            )
        path, path_expr = None, target.this
    elif isinstance(target, exp.Literal) and target.is_string:
        path = str(target.this)
    elif is_csv and isinstance(target, exp.Identifier) and _ident_name(target) == "stdout":
        path = None
    else:
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            "COPY target must be a single-quoted file path"
            + (", or STDOUT for a csv sink" if is_csv else ""),
            target,
            fallback=copy,
            hint=_SINK_HINT,
        )

    query = copy.this
    if not isinstance(query, exp.Subquery | exp.Select | exp.Union):
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            "COPY source must be a parenthesized query",
            query if isinstance(query, exp.Expr) else None,
            fallback=copy,
            hint=_SINK_HINT,
        )

    return path, path_expr, target, options, query, is_csv


def _sink_options(copy: exp.Copy, target: exp.Expr) -> tuple[RawSinkOption, ...]:
    """The ``WITH (...)`` pairs, names folded and deduplicated.

    Each is an ``exp.CopyParameter(this=Var(name), expression=<value>)``; a
    value-less entry (``WITH (freeze)``, and both halves of ``WITH (faststart
    on)``, which sqlglot splits into TWO bare parameters) has no ``expression``
    at all and is rejected here.

    Nothing in a ``CopyParameter`` but a ``Literal`` value carries a token
    position, so `target` — the path literal, which sits on the ``TO`` line
    just above the ``WITH`` block — is the fallback anchor.
    """
    options: list[RawSinkOption] = []
    seen: set[str] = set()
    for param in copy.args.get("params") or []:
        if not isinstance(param, exp.CopyParameter):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "malformed sink option",
                param if isinstance(param, exp.Expr) else None,
                fallback=target,
                hint=_OPTION_HINT,
            )
        name_node = param.this
        name = _ident_name(name_node) if isinstance(name_node, exp.Expr) else ""
        if not name or not isinstance(name_node, exp.Expr):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "malformed sink option",
                param,
                fallback=target,
                hint=_OPTION_HINT,
            )
        value = param.args.get("expression")
        if not isinstance(value, exp.Expr):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"sink option '{name}' has no value",
                param,
                fallback=target,
                hint=_OPTION_HINT,
            )
        if name in seen:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"duplicate sink option '{name}'",
                value,
                fallback=target,
                hint="each sink option may be set at most once",
            )
        seen.add(name)
        options.append(RawSinkOption(name=name, value=value, name_node=name_node))
    return tuple(options)


def _named_only_arguments(
    rest: list[exp.Expr], fallback: exp.Expr, *, positional_message: str
) -> list[tuple[str, exp.Expr, exp.Expr]]:
    """Shape-check a table function's ``name => value`` arguments.

    Shared by ``input('path', ...)`` and ``ffmpeg.<source>(...)``: both take
    named arguments ONLY past a fixed positional prefix (one path literal for
    ``input``, none for a source), so a bare positional among `rest` is
    rejected outright rather than by the softer "positional arguments must come
    before named arguments" rule. `positional_message` is what that rejection
    says, and the only difference between the two callers.

    Returns ``(name, value, name_node)`` triples in written order; which
    option names actually exist is the caller's business (a curated table for
    input options, the installed ffmpeg for a source's).
    """
    out: list[tuple[str, exp.Expr, exp.Expr]] = []
    seen: set[str] = set()
    for arg in rest:
        if not isinstance(arg, exp.Kwarg):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                positional_message,
                arg,
                fallback=fallback,
                hint=_KWARG_HINT,
            )
        name = kwarg_name(arg)
        value = arg.args.get("expression")
        anchor = value if isinstance(value, exp.Expr) else arg
        if not name:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "malformed named argument",
                anchor,
                fallback=fallback,
                hint=_KWARG_HINT,
            )
        if not isinstance(value, exp.Expr):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"named argument '{name}' has no value",
                arg,
                fallback=fallback,
                hint=_KWARG_HINT,
            )
        if name in seen:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"duplicate named argument '{name}'",
                anchor,
                fallback=fallback,
                hint="each named argument may be given at most once",
            )
        seen.add(name)
        out.append((name, value, arg.this if isinstance(arg.this, exp.Expr) else arg))
    return out


def _input_options(rest: list[exp.Expr], path_node: exp.Expr) -> tuple[RawInputOption, ...]:
    """``input('path', name => value, ...)``'s trailing arguments, shape-checked.

    The path (already consumed by the caller) is the sole positional
    argument; everything after it must be an ``exp.Kwarg``. Which option
    names actually exist is ``sqlmpeg.inputs.INPUT_OPTIONS``' business, not
    the parser's (mirrors ``RawSinkOption`` / ``_sink_options``).
    """
    return tuple(
        RawInputOption(name=name, value=value, name_node=name_node, path_node=path_node)
        for name, value, name_node in _named_only_arguments(
            rest,
            path_node,
            positional_message="input() takes one positional path; every argument "
            "after it must be a named option",
        )
    )


def _source_options(
    args: list[exp.Expr], call_node: exp.Expr, name: str
) -> tuple[RawSourceOption, ...]:
    """``ffmpeg.<source>(name => value, ...)``'s arguments, shape-checked.

    A generated source has NO input pads at all -- that is what makes it a
    source -- so it takes no positional arguments whatsoever, and a bare one
    is rejected here with a message that says exactly that rather than the
    generic named-argument-ordering one.
    """
    return tuple(
        RawSourceOption(
            name=option_name, value=value, name_node=name_node, call_node=call_node
        )
        for option_name, value, name_node in _named_only_arguments(
            args,
            call_node,
            positional_message=f"{FILTER_NAMESPACE}.{name}() is a generated source: "
            "it has no stream inputs, and its options are named",
        )
    )


class _Resolver:
    def __init__(self) -> None:
        self.input_paths: list[str] = []
        self.sources: dict[str, int] = {}
        self.ctes: dict[str, QueryExpr] = {}
        self.views: dict[str, QueryExpr] = {}
        self.view_nodes: dict[str, exp.Expr] = {}
        self.used: set[str] = set()
        self.input_options: dict[str, tuple[RawInputOption, ...]] = {}
        self.source_filters: dict[str, RawSource] = {}
        self.track_rows: dict[str, RawTrackRows] = {}
        self.values_ctes: dict[str, RawValuesTable] = {}

    # -- entry point ------------------------------------------------------

    def run(self, tree: exp.Expr) -> Resolved:
        """Resolve one statement, or a whole script.

        Script rules, all of them typed rejections:

        * every ``CREATE VIEW`` precedes every ``COPY`` (forward references are
          already banned, so this only costs an ordering a Postgres script is
          free to write either way, and buys a single left-to-right pass);
        * a script writes its outputs with ``COPY`` — at least one, and a bare
          ``SELECT`` among several statements has nowhere to go;
        * a view nobody reads is a typo, not a no-op.

        A SINGLE statement keeps its pre-script behavior exactly: a bare SELECT
        (no sink) or one COPY.
        """
        statements = _statements(tree)
        script = len(statements) > 1

        sinks: list[RawSink] = []
        select: QueryExpr | None = None
        branches: list[exp.Select] = []

        for statement in statements:
            if isinstance(statement, exp.Create):
                if sinks:
                    raise _error(
                        ErrorCode.UNSUPPORTED_SQL,
                        "a CREATE VIEW may not follow a COPY",
                        statement,
                        hint="define every view before the first COPY",
                    )
                self._view(statement)
                continue
            if isinstance(statement, exp.Copy):
                # Peel the COPY wrapper off; what it wraps is validated exactly
                # like a bare SELECT from here on -- except a csv sink is table
                # mode, where metadata columns are legal SELECT outputs.
                path, path_expr, path_node, options, wrapped, is_csv = _sink(statement)
                query, query_branches = self._resolve_query(
                    wrapped, table_mode=is_csv, path_expr=path_expr
                )
                sinks.append(
                    RawSink(
                        path=path,
                        path_node=path_node,
                        query=query,
                        branches=tuple(query_branches),
                        options=options,
                        is_csv=is_csv,
                        path_expr=path_expr,
                    )
                )
                if select is None:
                    select, branches = query, query_branches
                continue
            if script:
                if isinstance(statement, exp.Select | exp.Union | exp.Subquery | exp.Paren):
                    # Only COPY carries a destination, so a bare SELECT among
                    # several statements has nowhere to send its streams.
                    raise _error(
                        ErrorCode.UNSUPPORTED_SQL,
                        "only COPY may appear alongside other statements",
                        statement,
                        hint="wrap it: COPY (<query>) TO '<path>'",
                    )
                # DROP / ALTER / anything else sqlglot recognized, including the
                # exp.Command it falls back to for syntax it cannot parse.
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"unsupported statement: {statement.__class__.__name__.upper()}",
                    statement,
                    hint=_SCRIPT_HINT,
                )
            # A bare SELECT has no media destination, so it is always at least
            # table-capable: metadata columns are legal SELECT outputs here,
            # even if the CLI goes on to compile it as a media command. This
            # never weakens a MEDIA query -- the streaming lowerer
            # independently enforces "streams are the only output"
            # (`sqlmpeg.lower._row_value`).
            select, branches = self._resolve_query(statement, table_mode=True)

        if select is None:
            # Reached only by a view-only script: nothing named a destination.
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "a script must write its output with COPY",
                next(iter(self.view_nodes.values()), None),
                fallback=tree,
                hint=_SCRIPT_HINT,
            )
        self._check_views_are_used()
        self._check_fanout_is_alone(sinks)

        return Resolved(
            select=select,
            input_paths=self.input_paths,
            sources=self.sources,
            ctes=self.ctes,
            branches=branches,
            sinks=sinks,
            views=self.views,
            input_options=self.input_options,
            source_filters=self.source_filters,
            track_rows=self.track_rows,
            values_ctes=self.values_ctes,
        )

    def _check_fanout_is_alone(self, sinks: list[RawSink]) -> None:
        """A fan-out COPY is the only statement of its script, v1."""
        if len(sinks) <= 1:
            return
        row_aliases = set(self.track_rows)
        for sink in sinks:
            if sink.path_expr is None or not references_row_alias(sink.path_expr, row_aliases):
                continue
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "a TO expression writes one file per row, so it cannot share a "
                f"script with another COPY ({len(sinks)} here)",
                sink.path_expr,
                fallback=sink.path_node,
                hint="split the fan-out COPY into its own query",
            )

    def _resolve_query(
        self,
        node: exp.Expr,
        *,
        table_mode: bool = False,
        path_expr: exp.Expr | None = None,
        context: str | None = None,
    ) -> tuple[QueryExpr, list[exp.Select]]:
        """Validate one whole query — a view body, a COPY's, or a bare SELECT.

        Its own ``WITH`` is resolved into the shared, ordered binding table
        FIRST, so the names it defines are visible to it and to everything
        written after it, and nothing else.

        ``table_mode`` is True for a bare SELECT and a
        csv COPY — the two contexts a metadata SELECT output is legal in —
        and False everywhere else (a media COPY, and any CTE/view body,
        left conservative since neither is exercised by a table query
        today).
        """
        query = _unwrap(node)
        if not isinstance(query, exp.Select | exp.Union):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"unsupported statement: {query.__class__.__name__.upper()}",
                query,
                hint="sqlmpeg accepts a single SELECT statement",
            )
        self._resolve_ctes(query)
        branches = union_branches(query)
        if path_expr is not None and len(branches) > 1:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "a TO expression writes one file per row, and UNION ALL has one "
                "row set per branch",
                path_expr,
                fallback=query,
                hint="give the concatenated query a quoted TO path",
            )
        # Where aggregation is NOT available, and what to call the place. A
        # UNION ALL branch aggregates like any other: it is one concat segment,
        # and a segment with several rows has to gather them the same way.
        no_aggregate = context
        visible = set(self.ctes)
        for branch in branches:
            self._validate_select(
                branch,
                visible,
                table_mode=table_mode,
                path_expr=path_expr,
                no_aggregate=no_aggregate,
            )
        return query, branches

    # -- CREATE VIEW --------------------------------------------

    def _view(self, create: exp.Create) -> None:
        """Validate one ``CREATE VIEW name AS <query>`` and bind it.

        Shapes VERIFIED under sqlglot 30.17 ``read="postgres"`` — an
        ``exp.Create`` with ``kind="VIEW"``, the name in ``this`` and the body
        in ``expression``; ``replace``/``exists``/``refresh``/``unique``/
        ``concurrently`` are plain bools that are present-and-False on the
        plain form (which is why :func:`_check_query_args` skips them):

        ======================================== ==================================
        written                                  how it lands here
        ======================================== ==================================
        ``CREATE VIEW v AS <q>``                 ``this=Table(Identifier(v))``,
                                                 ``expression=Select`` (``Union``
                                                 for a ``UNION ALL`` body; a body
                                                 ``WITH`` rides on the ``Select``)
        ``CREATE OR REPLACE VIEW v AS <q>``      ``replace=True``
        ``CREATE TEMP|TEMPORARY VIEW v AS <q>``  ``properties=[TemporaryProperty]``
        ``CREATE MATERIALIZED VIEW v AS <q>``    ``properties=[MaterializedProperty]``
        ``CREATE VIEW v WITH (k=v) AS <q>``      ``properties=[Property]``
        ``CREATE VIEW IF NOT EXISTS v AS <q>``   ``exists=True``
        ``CREATE VIEW v (c1, c2) AS <q>``        ``this=Schema(Table(v), [c1, c2])``
        ``CREATE VIEW s.v AS <q>``               ``this=Table(v, db=s)``
        ``CREATE TABLE t AS <q>``                ``kind="TABLE"``
        ``CREATE RECURSIVE VIEW v (c) AS <q>``   sqlglot falls back to
                                                 ``exp.Command`` -> never reaches
                                                 here, rejected as a statement
        ======================================== ==================================

        ANCHORING: an ``exp.Create`` carries no token position of its own and
        neither does the ``Table`` wrapper, but the view-NAME ``Identifier``
        does, and it is the earliest positioned token in the subtree — so
        ``_pos(create)`` already lands on the name. The name node is passed
        explicitly anyway, so a rejection cannot drift onto the body.
        """
        kind = create.args.get("kind")
        if not isinstance(kind, str) or kind.upper() != "VIEW":
            written = kind.upper() if isinstance(kind, str) and kind else "?"
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"unsupported statement: CREATE {written}",
                create,
                hint=_VIEW_HINT,
            )
        if create.args.get("replace"):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "CREATE OR REPLACE VIEW is not supported",
                create,
                hint="a view exists only for the length of one script; "
                "there is nothing to replace",
            )
        if create.args.get("exists"):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "CREATE VIEW IF NOT EXISTS is not supported",
                create,
                hint="a view exists only for the length of one script; "
                "it never exists already",
            )
        self._check_view_properties(create)
        _check_query_args(
            create, frozenset({"this", "kind", "expression"}), "CREATE VIEW"
        )

        name_node = create.this
        if isinstance(name_node, exp.Schema):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "view column lists are not supported",
                name_node,
                fallback=create,
                hint="name the view's columns with AS inside its SELECT",
            )
        if not isinstance(name_node, exp.Table):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL, "view is missing a name", create, hint=_VIEW_HINT
            )
        if name_node.args.get("db") or name_node.args.get("catalog"):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "qualified view names are not supported",
                name_node,
                fallback=create,
                hint="a view lives in one script, not in a schema",
            )
        identifier = name_node.this
        if not isinstance(identifier, exp.Identifier):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL, "view is missing a name", create, hint=_VIEW_HINT
            )
        name = _ident_name(identifier)
        self._reserve(name, identifier)

        body = create.args.get("expression")
        if not isinstance(body, exp.Expr):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"view '{name}' has no query",
                identifier,
                fallback=create,
                hint=_VIEW_HINT,
            )
        # A view BODY is a full query, so unlike a CTE body it may carry its
        # own WITH: the nested-WITH rejection is CTE-body-only
        # (`_resolve_ctes`) and branch-level (`_collect_branches`), and neither
        # fires on a statement's own top-level one.
        query, _ = self._resolve_query(body, context="a view body")
        # Reserved a second time on purpose: the body's own WITH may have
        # claimed the name in between, and that collision has to be caught
        # before the binding below overwrites it.
        self._reserve(name, identifier)
        self.ctes[name] = query
        self.views[name] = query
        self.view_nodes[name] = identifier

    def _check_view_properties(self, create: exp.Create) -> None:
        """TEMP / MATERIALIZED / ``WITH (...)`` all land in ``properties``."""
        properties = create.args.get("properties")
        if not isinstance(properties, exp.Properties):
            return
        for prop in properties.expressions:
            if isinstance(prop, exp.TemporaryProperty):
                message = "TEMPORARY views are not supported"
            elif isinstance(prop, exp.MaterializedProperty):
                message = "MATERIALIZED views are not supported"
            else:
                message = "view options are not supported"
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                message,
                prop if isinstance(prop, exp.Expr) else None,
                fallback=create,
                hint="a view exists only for the length of one script and is "
                "always inlined; write CREATE VIEW <name> AS <query>",
            )

    def _check_views_are_used(self) -> None:
        """A view nobody reads is a typo, anchored on its CREATE.

        Deliberately views only: an unused CTE has always been legal, and a
        script's whole point is that its views feed the COPYs, so one that
        feeds nothing is a misspelled reference somewhere else.
        """
        for name, node in self.view_nodes.items():
            if name in self.used:
                continue
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"view '{name}' is never used",
                node,
                hint="every view must be read by a later view or COPY; "
                "check the spelling of the name in its FROM clauses",
            )

    # -- CTEs -------------------------------------------------------------

    def _resolve_ctes(self, query: QueryExpr) -> None:
        with_ = query.args.get("with_")
        if with_ is None:
            return
        if not isinstance(with_, exp.With):
            raise _error(ErrorCode.UNSUPPORTED_SQL, "malformed WITH clause", fallback=query)
        if with_.args.get("recursive"):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "WITH RECURSIVE is not supported",
                with_,
                hint="a filtergraph is acyclic; drop RECURSIVE",
            )
        _check_query_args(with_, frozenset({"expressions"}), "WITH")

        for cte in with_.expressions:
            if not isinstance(cte, exp.CTE):
                raise _error(ErrorCode.UNSUPPORTED_SQL, "malformed CTE", cte)
            _check_query_args(cte, frozenset({"this", "alias"}), "CTE")
            alias = cte.args.get("alias")
            if not isinstance(alias, exp.TableAlias) or alias.this is None:
                raise _error(ErrorCode.UNSUPPORTED_SQL, "CTE is missing a name", cte)
            name = _ident_name(alias.this)

            # A column list is CTE syntax stock Postgres uses for two things:
            # naming a VALUES CTE's columns (there is nothing else to name
            # them from), or renaming an ordinary SELECT's. Only the first is
            # supported -- sqlglot's own shape tells them apart, since a
            # `(VALUES ...)` body always parses as `Select(expressions=[Star],
            # from_=From(this=Values(...)))`.
            if alias.args.get("columns"):
                self._reserve(name, alias.this)
                self._add_values_cte(name, alias, cte)
                continue

            self._reserve(name, alias.this)

            body = _unwrap(cte.this)
            if not isinstance(body, exp.Select | exp.Union):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"CTE '{name}' must be a SELECT",
                    cte.this,
                    fallback=cte,
                )
            if body.args.get("with_") is not None:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "nested WITH clauses are not supported",
                    body.args.get("with_"),
                    fallback=body,
                    hint="hoist the CTE to the top-level WITH",
                )
            # A CTE only sees the CTEs defined before it (no forward refs).
            visible = set(self.ctes)
            for branch in union_branches(body):
                self._validate_select(branch, visible, no_aggregate="a CTE body")
            self.ctes[name] = body

    def _add_values_cte(self, name: str, alias: exp.TableAlias, cte: exp.CTE) -> None:
        """``WITH <name>(<cols>) AS (VALUES ...)`` -- a compile-time row table.

        Shape only: sqlglot always wraps a parenthesized ``VALUES`` in
        ``Select(expressions=[Star()], from_=From(this=Values(...)))``, so
        that exact shape is what tells a real VALUES CTE apart from a column-
        renamed SELECT (which is not supported: ``AS`` inside the SELECT is
        the one way to name a SELECT CTE's columns).
        """
        columns_list = alias.args.get("columns") or []
        column_names = [_ident_name(c) for c in columns_list]
        if len(set(column_names)) != len(column_names):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"duplicate column name in '{name}({', '.join(column_names)})'",
                alias,
                hint="every VALUES column needs its own name",
            )

        body = _unwrap(cte.this)
        if not isinstance(body, exp.Select):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"CTE '{name}' has a column list, so it must be VALUES (...)",
                cte.this,
                fallback=cte,
                hint="name a SELECT CTE's columns with AS inside the SELECT instead",
            )
        _check_query_args(body, frozenset({"expressions", "from_"}), f"VALUES CTE '{name}'")
        exprs = body.expressions
        from_ = body.args.get("from_")
        if len(exprs) != 1 or not isinstance(exprs[0], exp.Star) or not isinstance(
            from_, exp.From
        ):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"CTE '{name}' has a column list, so it must be VALUES (...)",
                cte.this,
                fallback=cte,
                hint="name a SELECT CTE's columns with AS inside the SELECT instead",
            )
        values = from_.this
        if not isinstance(values, exp.Values):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"CTE '{name}' has a column list, so it must be VALUES (...)",
                cte.this,
                fallback=cte,
                hint="name a SELECT CTE's columns with AS inside the SELECT instead",
            )
        _check_query_args(from_, frozenset({"this"}), f"VALUES CTE '{name}' FROM")
        _check_query_args(values, frozenset({"expressions", "alias"}), "VALUES")

        rows: list[tuple[exp.Literal | exp.Null, ...]] = []
        for tup in values.expressions:
            if not isinstance(tup, exp.Tuple):
                raise _error(ErrorCode.UNSUPPORTED_SQL, "malformed VALUES row", tup, fallback=cte)
            cells = tup.expressions
            if len(cells) != len(column_names):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"VALUES row has {len(cells)} values, but "
                    f"'{name}({', '.join(column_names)})' names {len(column_names)}",
                    tup,
                    fallback=cte,
                )
            checked: list[exp.Literal | exp.Null] = []
            for cell in cells:
                unwrapped = _unwrap(cell) if isinstance(cell, exp.Expr) else cell
                if not isinstance(unwrapped, exp.Literal | exp.Null):
                    raise _error(
                        ErrorCode.UNSUPPORTED_SQL,
                        "a VALUES cell must be a literal",
                        cell if isinstance(cell, exp.Expr) else tup,
                        fallback=cte,
                        hint="VALUES rows are compile-time literals: numbers, "
                        "quoted strings, or NULL -- no expressions",
                    )
                checked.append(unwrapped)
            rows.append(tuple(checked))

        self.values_ctes[name] = RawValuesTable(
            alias=name, columns=tuple(column_names), rows=tuple(rows), node=cte
        )

    def _reserve(self, name: str, node: exp.Expr | None) -> None:
        if not name:
            raise _error(ErrorCode.UNSUPPORTED_SQL, "empty name", node)
        if name == FILTER_NAMESPACE:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{FILTER_NAMESPACE}' is reserved for the filter namespace",
                node,
                hint=f"{FILTER_NAMESPACE}.<filter>(...) calls an ffmpeg filter "
                "directly; pick another alias or CTE name",
            )
        if name == MACRO_NAMESPACE:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{MACRO_NAMESPACE}' is reserved for the macro namespace",
                node,
                hint=f"{MACRO_NAMESPACE}.<name>(...) calls a sqlmpeg macro "
                "directly; pick another alias or CTE name",
            )
        if (
            name in self.ctes
            or name in self.sources
            or name in self.source_filters
            or name in self.track_rows
            or name in self.values_ctes
        ):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"duplicate name '{name}'",
                node,
                hint="view, CTE and alias names share one flat namespace and "
                "must be unique across the whole script",
            )

    # -- selects ----------------------------------------------------------

    def _validate_select(
        self,
        select: exp.Select,
        visible: set[str],
        *,
        table_mode: bool = False,
        path_expr: exp.Expr | None = None,
        no_aggregate: str | None = None,
    ) -> None:
        # `ORDER BY` and `GROUP BY` are admitted for ROW-SOURCE queries and
        # nowhere else. The carve-out is decided from the FROM clause alone,
        # before any of it is validated, so a branch with no rows keeps the
        # NO_STREAMING_EQUIVALENT rejection byte for byte.
        _normalize_map_paths(select, path_expr)
        _normalize_row_aliases(select, path_expr)
        rows = _has_row_source(select, visible)
        if rows:
            self._check_aggregate_context(select, no_aggregate)
        allowed = _SELECT_ALLOWED | {"order", "group"} if rows else _SELECT_ALLOWED
        _check_query_args(select, allowed, "SELECT")

        # The SELECT list IS the output stream list, so any number of
        # projections is legal. Only an empty list is not.
        projections = select.expressions
        if not projections:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL, "SELECT has no output column", fallback=select
            )
        aggregating = rows and no_aggregate is None
        for projection in projections:
            # A star projection carries nothing but the star and its qualifier,
            # so there is no expression to check inside it -- and running the
            # generic walk would hit the star rejection it is exempt from.
            if star_qualifier(projection) is None:
                self._check_expression(
                    projection,
                    select,
                    array_agg=_projection_expr(projection) if aggregating else None,
                )

        where = select.args.get("where")
        if isinstance(where, exp.Where):
            self._check_expression(where, select)

        scope = self._collect_scope(select, visible)
        for projection in projections:
            self._check_columns(projection, scope, select, table_mode=table_mode)
            self._check_select_value(projection, scope, select)
        if isinstance(where, exp.Where):
            self._check_where(where, scope, select, fanout=path_expr is not None)
        order = select.args.get("order")
        if isinstance(order, exp.Order):
            self._check_order(order, scope, select)
        if path_expr is not None:
            self._check_path_expr(path_expr, scope, select)
        if is_grouped(select):
            self._check_grouping(select, scope, path_expr, table_mode=table_mode)

    def _check_aggregate_context(self, select: exp.Select, where: str | None) -> None:
        """Aggregation belongs to a query's own SELECT, never a CTE body.

        Fires only for a branch that HAS rows: without them the generic
        ``GROUP BY has no streaming equivalent`` / ``aggregate function ...``
        rejections already say the right thing.
        """
        if where is None:
            return
        keys = group_keys(select)
        if keys:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"GROUP BY is not supported in {where}",
                keys[0],
                fallback=select,
                hint="aggregation belongs to a media COPY's own SELECT",
            )
        aggregates = _array_aggs(select)
        if aggregates:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"array_agg() is not supported in {where}",
                aggregates[0],
                fallback=select,
                hint="aggregation belongs to a media COPY's own SELECT",
            )

    # -- grouping validity ------------------------------------------------

    def _check_grouping(
        self,
        select: exp.Select,
        scope: dict[str, str],
        path_expr: exp.Expr | None,
        *,
        table_mode: bool = False,
    ) -> None:
        """Postgres's rule: every column is aggregated, constant, or a key.

        A grouped branch is one with a GROUP BY, an ``array_agg``, or both, and
        what makes it decidable here is that only a TRACK-ROW column varies
        within a group -- an input alias's streams and scalars are the same for
        every row of the branch. So a row-referencing expression outside an
        aggregate must match a GROUP BY key verbatim (sqlglot ``.sql()``
        equality), and everything else passes.

        Keys that read a row column partition a MEDIA branch into one file per
        group, which only a fan-out ``TO (<expression>)`` can write -- a table
        query needs no destination, every group is just a printed row, so
        `table_mode` skips that requirement entirely.
        """
        keys = group_keys(select)
        key_texts = {key.sql() for key in keys}
        for key in keys:
            self._check_expression(key, select)
            self._check_columns(key, scope, select)
        for projection in select.expressions:
            if not isinstance(projection, exp.Expr):
                continue
            qualifier = star_qualifier(projection)
            if qualifier is None:
                self._check_grouped_expr(
                    _projection_expr(projection), scope, select, key_texts
                )
            elif scope.get(qualifier) == "row":
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"'{qualifier}.*' is neither aggregated nor a GROUP BY key",
                    projection,
                    fallback=select,
                    hint=_GROUP_STREAM_HINT,
                )
        if path_expr is not None:
            self._check_grouped_expr(path_expr, scope, select, key_texts)
        row_keys = [key for key in keys if _references_row(key, scope)]
        if table_mode or not row_keys:
            return
        if path_expr is None or not _references_row(path_expr, scope):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "GROUP BY over a track-row column writes one file per group, "
                "and this COPY names one destination",
                row_keys[0],
                fallback=select,
                hint=_GROUP_FANOUT_HINT,
            )

    def _check_grouped_expr(
        self,
        node: exp.Expr,
        scope: dict[str, str],
        select: exp.Select,
        key_texts: set[str],
    ) -> None:
        """One expression of a grouped branch, recursively."""
        if node.sql() in key_texts or isinstance(node, exp.ArrayAgg):
            return
        if isinstance(node, exp.Column) and not isinstance(node.this, exp.Star):
            table_node = node.args.get("table")
            alias = _ident_name(table_node) if table_node is not None else ""
            if scope.get(alias) != "row":
                return
            name = _ident_name(node.this)
            stream = name == ROW_STREAM
            label = alias if stream else f"{alias}.{column_label(name)}"
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{label}' is neither aggregated nor a GROUP BY key",
                node,
                fallback=select,
                hint=_GROUP_STREAM_HINT if stream else _GROUP_VALUE_HINT,
            )
        for value in node.args.values():
            items = value if isinstance(value, list) else [value]
            for item in items:
                if isinstance(item, exp.Expr):
                    self._check_grouped_expr(item, scope, select, key_texts)

    def _check_path_expr(
        self, node: exp.Expr, scope: dict[str, str], select: exp.Select
    ) -> None:
        """``TO (<expression>)``: one text value over this query's row columns."""
        kind = self._check_value_expr(node, scope, select)
        if kind == "text":
            return
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            "a TO expression must be text, got " + ("NULL" if kind is None else kind),
            node,
            fallback=select,
            hint="build the name from text: 'ch' || c.index::text || '.mkv'",
        )

    def _check_select_value(
        self, projection: exp.Expr, scope: dict[str, str], select: exp.Select
    ) -> None:
        """Type-check a SELECT column written as CASE or ``||``.

        Neither shape can ever be a stream, so this runs whatever the query is:
        lower decides whether the column is a metadata TAG (a media query over
        track rows) or a rejection, and a tag's value has to type-check either
        way.
        """
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        value = _unwrap_paren(inner) if isinstance(inner, exp.Expr) else None
        if is_value_expr(value):
            self._check_value_expr(value, scope, select)

    def _check_expression(
        self, node: exp.Expr, select: exp.Select, *, array_agg: exp.Expr | None = None
    ) -> None:
        """Reject constructs no streaming filtergraph can express.

        ``array_agg`` names the one node exempt from the aggregate rejection: the
        whole SELECT column of a track-row branch, which lower collapses into
        the same stream list the bare splat produces. Every other aggregate,
        and every array_agg written anywhere else, still has no equivalent.
        """
        for sub in node.walk():
            if isinstance(sub, exp.ArrayAgg):
                if sub is not array_agg:
                    raise _error(
                        ErrorCode.UNSUPPORTED_SQL,
                        "array_agg() is only supported as a whole SELECT column",
                        sub,
                        fallback=select,
                        hint=_ARRAY_AGG_PLACE_HINT,
                    )
                if isinstance(sub.this, exp.Order):
                    raise _error(
                        ErrorCode.UNSUPPORTED_SQL,
                        "ORDER BY inside array_agg() is not supported",
                        sub.this,
                        fallback=select,
                        hint="the aggregate keeps the branch's row order; sort "
                        "the rows themselves with the query's own ORDER BY",
                    )
            elif isinstance(sub, exp.AggFunc):
                raise _error(
                    ErrorCode.NO_STREAMING_EQUIVALENT,
                    f"aggregate function {sub.sql_name().lower()}() has no "
                    "streaming equivalent",
                    sub,
                    fallback=select,
                    hint=_AGG_HINT,
                )
            if isinstance(sub, exp.Window):
                raise _error(
                    ErrorCode.NO_STREAMING_EQUIVALENT,
                    "window functions have no streaming equivalent",
                    sub,
                    fallback=select,
                )
            if isinstance(sub, exp.SubqueryPredicate) or (
                isinstance(sub, exp.In) and sub.args.get("query") is not None
            ):
                raise _error(
                    ErrorCode.NO_STREAMING_EQUIVALENT,
                    "subquery predicates have no streaming equivalent",
                    sub,
                    fallback=select,
                )
            if isinstance(sub, exp.Select | exp.Union | exp.Subquery):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "subqueries are not supported here",
                    sub,
                    fallback=select,
                    hint="use a WITH ... AS (...) CTE instead",
                )
            if isinstance(sub, exp.Star):
                # A star is legal only as a whole projection, which
                # `_validate_select` peels off before calling this; anything
                # reaching here has one nested inside an expression, where
                # "every stream of that alias" has no meaning.
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "* is only supported as a whole SELECT column",
                    sub,
                    fallback=select,
                    hint=_STAR_HINT,
                )
            if isinstance(sub, exp.Bracket):
                self._check_subscript(sub, select)
            if isinstance(sub, exp.Func):
                self._check_named_arguments(sub, select)

    def _check_named_arguments(self, call: exp.Func, select: exp.Select) -> None:
        """``name => value`` arguments must TRAIL the positional ones, once each.

        Shape only: whether the option exists, and what type it takes,
        depends on the ffmpeg the query is compiled against, so lower's registry
        owns those two checks (`UNKNOWN_FILTER_OPTION` / `FILTER_OPTION_TYPE`).

        Anchoring: an ``exp.Kwarg``'s ``Var`` name carries no token position (the
        same gap sink option names have), so a rejection anchors on the offending
        VALUE where it is a literal and falls back to the call itself.
        """
        seen: set[str] = set()
        named = False
        for arg in call.expressions:
            if not isinstance(arg, exp.Expr):
                continue
            if not isinstance(arg, exp.Kwarg):
                if named:
                    raise _error(
                        ErrorCode.UNSUPPORTED_SQL,
                        "positional arguments must come before named arguments",
                        arg,
                        fallback=select,
                        hint=_KWARG_HINT,
                    )
                continue
            named = True
            name = kwarg_name(arg)
            value = arg.args.get("expression")
            anchor = value if isinstance(value, exp.Expr) else arg
            if not name:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "malformed named argument",
                    anchor,
                    fallback=select,
                    hint=_KWARG_HINT,
                )
            if not isinstance(value, exp.Expr):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"named argument '{name}' has no value",
                    arg,
                    fallback=select,
                    hint=_KWARG_HINT,
                )
            if name in seen:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"duplicate named argument '{name}'",
                    anchor,
                    fallback=select,
                    hint="each named argument may be given at most once",
                )
            seen.add(name)

    def _check_subscript(self, bracket: exp.Bracket, select: exp.Select) -> None:
        """A subscript selects exactly one stream: ``<alias>.<column>[<int>]``."""
        _check_query_args(bracket, _BRACKET_ALLOWED, "subscript")
        inner = bracket.this
        if isinstance(inner, exp.Bracket):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "chained stream subscripts are not supported",
                bracket,
                fallback=select,
                hint="one subscript selects one stream, e.g. a.video[1]",
            )
        if not isinstance(inner, exp.Column):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "only stream columns can be subscripted",
                bracket,
                fallback=select,
                hint="subscript a stream column, e.g. a.video[1]",
            )
        if subscript_index(bracket) is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "stream subscript must be a positive integer literal",
                bracket,
                fallback=select,
                hint=_SUBSCRIPT_HINT,
            )

    # -- FROM / aliases ---------------------------------------------------

    def _collect_scope(self, select: exp.Select, visible: set[str]) -> dict[str, str]:
        from_ = select.args.get("from_")
        if not isinstance(from_, exp.From):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "SELECT requires a FROM clause",
                fallback=select,
                hint="add FROM input('clip.mp4') a",
            )

        scope: dict[str, str] = {}
        self._add_from_item(from_.this, scope, visible)

        joins = select.args.get("joins") or []
        for join in joins:
            if not isinstance(join, exp.Join):
                raise _error(ErrorCode.UNSUPPORTED_SQL, "malformed FROM clause", fallback=select)
            spec = _join_spec(join)
            explicit = spec.kind != "cross" or any(
                join.args.get(key)
                for key in ("on", "using", "side", "kind", "method", "match_condition")
            )
            if explicit:
                self._check_join(join, spec, scope, select)
            self._add_from_item(join.this, scope, visible)
            if spec.on is not None:
                # AFTER the right side is bound: an ON names both operands.
                self._check_join_predicate(spec.on, scope, join)
        return scope

    # -- JOIN between track-row tables -----------------

    def _check_join(
        self,
        join: exp.Join,
        spec: RawRowJoin,
        scope: dict[str, str],
        select: exp.Select,
    ) -> None:
        """Admit one explicit JOIN, which only track-row tables may use.

        JOIN syntax is admitted between unnest tables ONLY; input-level FROM
        stays comma-cross-join. Everything a track-row join cannot be (a
        stream-level operand, RIGHT, CROSS, NATURAL, USING) is rejected, with
        the hint saying which spelling to reach for instead.
        """
        for key in ("using", "method", "match_condition"):
            value = join.args.get(key)
            if value:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"{'USING' if key == 'using' else 'this JOIN form'} is not "
                    "supported",
                    _first_expression(value),
                    fallback=join,
                    hint=_JOIN_HINT,
                )
        if not isinstance(join.this, exp.Unnest) or "row" not in scope.values():
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "explicit JOIN syntax is supported between unnest(...) track-row "
                "tables only",
                _first_expression(
                    join.args.get("on")
                    or join.args.get("side")
                    or join.args.get("kind")
                ),
                fallback=join,
                hint=_JOIN_HINT,
            )
        if spec.kind == "right":
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "RIGHT JOIN is not supported",
                fallback=join,
                hint="swap the two unnest tables and write LEFT JOIN, or use "
                "FULL OUTER JOIN — row order follows the LEFT side",
            )
        if spec.kind == "cross":
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "CROSS JOIN is not supported",
                fallback=join,
                hint="a comma between two unnest tables IS the cross join: "
                "FROM ..., unnest(f.audio) a, unnest(g.audio) b",
            )
        if spec.on is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "a track-row JOIN requires an ON predicate",
                fallback=join,
                hint="match the rows on their metadata, e.g. "
                "ON a.tags.language = b.tags.language",
            )
        self._check_expression(spec.on, select)

    def _check_join_predicate(
        self, node: exp.Expr, scope: dict[str, str], join: exp.Join
    ) -> None:
        """One ON predicate: 061's row grammar, plus column-to-column comparison.

        The addition over :meth:`_check_row_predicate` is the whole reason JOIN
        exists — ``a.tags.language = b.tags.language`` compares two row COLUMNS — so the
        two operands may now both be columns, and then their static types must
        match (a text column never equals a numeric one, whatever the files
        turned out to contain). Everything else is the same closed grammar:
        AND/OR/NOT over comparisons, BETWEEN and IS [NOT] NULL, with only
        track-row columns and literals as operands.
        """
        node = _unwrap_paren(node)
        if isinstance(node, exp.And | exp.Or):
            self._check_join_predicate(node.this, scope, join)
            expression = node.args.get("expression")
            if not isinstance(expression, exp.Expr):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "malformed ON predicate",
                    node,
                    fallback=join,
                    hint=_JOIN_ON_HINT,
                )
            self._check_join_predicate(expression, scope, join)
            return
        if isinstance(node, exp.Not):
            if not isinstance(node.this, exp.Expr):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "malformed ON predicate",
                    node,
                    fallback=join,
                    hint=_JOIN_ON_HINT,
                )
            self._check_join_predicate(node.this, scope, join)
            return
        if isinstance(node, exp.Is):
            column = _unwrap_paren(node.this) if isinstance(node.this, exp.Expr) else None
            if not isinstance(node.args.get("expression"), exp.Null) or not isinstance(
                column, exp.Column
            ):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "only 'IS NULL' and 'IS NOT NULL' are supported",
                    node,
                    fallback=join,
                    hint=_JOIN_ON_HINT,
                )
            self._row_operand(column, scope, join)
            return
        if isinstance(node, exp.Between):
            if node.args.get("symmetric"):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "BETWEEN SYMMETRIC is not supported",
                    node,
                    fallback=join,
                    hint=_JOIN_ON_HINT,
                )
            column = _unwrap_paren(node.this) if isinstance(node.this, exp.Expr) else None
            if not isinstance(column, exp.Column):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "BETWEEN needs a track-row column on its left",
                    node,
                    fallback=join,
                    hint=_JOIN_ON_HINT,
                )
            column_type = self._row_operand(column, scope, join)
            for bound in (node.args.get("low"), node.args.get("high")):
                self._check_join_operand(bound, column, column_type, scope, join)
            return
        if isinstance(node, exp.EQ | exp.NEQ | exp.GT | exp.GTE | exp.LT | exp.LTE):
            left = _unwrap_paren(node.this) if isinstance(node.this, exp.Expr) else None
            right = node.args.get("expression")
            right = _unwrap_paren(right) if isinstance(right, exp.Expr) else None
            if is_value_expr(left) or is_value_expr(right):
                self._check_value_pair(left, right, scope, join, _JOIN_ON_HINT)
                return
            column, other = (left, right) if isinstance(left, exp.Column) else (right, left)
            if not isinstance(column, exp.Column):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "an ON predicate compares track-row columns",
                    node,
                    fallback=join,
                    hint=_JOIN_ON_HINT,
                )
            column_type = self._row_operand(column, scope, join)
            self._check_join_operand(other, column, column_type, scope, join)
            return
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            "unsupported ON predicate",
            node,
            fallback=join,
            hint=_JOIN_ON_HINT,
        )

    def _check_join_operand(
        self,
        node: exp.Expr | None,
        column: exp.Column,
        column_type: str,
        scope: dict[str, str],
        join: exp.Join,
    ) -> None:
        """An ON comparison's other operand: another row column, or a literal."""
        other = _unwrap_paren(node) if isinstance(node, exp.Expr) else None
        if isinstance(other, exp.Column):
            other_type = self._row_operand(other, scope, join)
            if other_type != column_type:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"'{_ident_name(column.args.get('table'))}."
                    f"{_ident_name(column.this)}' is {column_type} and "
                    f"'{_ident_name(other.args.get('table'))}."
                    f"{_ident_name(other.this)}' is {other_type}, so they can "
                    "never match",
                    other,
                    fallback=join,
                    hint="join columns of the same kind, e.g. "
                    "ON a.tags.language = b.tags.language",
                )
            return
        self._check_row_literal(node, column, column_type, join)

    def _add_from_item(
        self, item: exp.Expr | None, scope: dict[str, str], visible: set[str]
    ) -> None:
        """One FROM item: a track-row ``unnest``, or an ordinary table."""
        if isinstance(item, exp.Unnest):
            self._add_track_rows(item, scope)
            return
        self._add_table(item, scope, visible)

    def _add_table(
        self, table: exp.Expr | None, scope: dict[str, str], visible: set[str]
    ) -> None:
        if not isinstance(table, exp.Table):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "only input('path') and CTE names are allowed in FROM",
                table,
                hint="use a WITH ... AS (...) CTE instead of a subquery",
            )
        # `FROM ffmpeg.<source>(...) alias` is the ONE qualified
        # table name there is: the namespace lands in `db`. A three-part name
        # (`x.ffmpeg.testsrc(...)`) also fills `catalog`, and is not it.
        db = table.args.get("db")
        namespaced = (
            not table.args.get("catalog")
            and isinstance(db, exp.Expr)
            and _ident_name(db) == FILTER_NAMESPACE
        )
        if (db or table.args.get("catalog")) and not namespaced:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "qualified table names are not supported",
                table,
            )
        for key, value in table.args.items():
            if key in ("this", "alias", "db") or not value:
                continue
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"unsupported table modifier: {key}",
                _first_expression(value),
                fallback=table,
            )

        inner = table.this
        alias_node = table.args.get("alias")
        if isinstance(alias_node, exp.TableAlias) and alias_node.args.get("columns"):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "table column aliases are not supported",
                alias_node,
            )

        if namespaced:
            self._add_source(table, inner, alias_node, scope)
            return
        if isinstance(inner, exp.Anonymous):
            self._add_input(table, inner, alias_node, scope)
            return
        if isinstance(inner, exp.Identifier):
            name = _ident_name(inner)
            if name in self.values_ctes:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"'{name}' is a VALUES CTE, and cannot be selected from directly",
                    inner,
                    fallback=table,
                    hint=f"pass it to a sink option instead: WITH (chapters {name})",
                )
            if name not in visible:
                raise _error(
                    ErrorCode.UNKNOWN_ALIAS,
                    f"unknown table '{name}'",
                    inner,
                    fallback=table,
                    hint=self._known_hint(visible),
                )
            # Feeds the unused-VIEW check. CTE names land here too;
            # only views are required to be read.
            self.used.add(name)
            local = self._local_alias(name, alias_node, table)
            if local in scope:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"duplicate name '{local}'",
                    alias_node if alias_node is not None else inner,
                    fallback=table,
                    hint="a name can appear only once per FROM clause; to consume "
                    "it twice, reference <name>.video[1] twice — reuse is automatic",
                )
            scope[local] = "cte"
            return
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            "only input('path') and CTE names are allowed in FROM",
            table,
        )

    # -- FROM unnest(<input>.<type>) alias ------------

    def _add_track_rows(self, unnest: exp.Unnest, scope: dict[str, str]) -> None:
        """Bind one track-row table. Shape only — the rows are lower's business.

        Four rules, each a typed rejection (see the module docstring's shape
        table for the sqlglot node each one keys off):

        1. ONE argument, and it is a BARE array column of an alias — no
           subscript (``unnest(f.audio[1])``: that is one stream, not a set),
           no nesting, no expression. ``exp.Unnest.expressions`` is the arg
           list, so the arity check is on it directly.
        2. That alias is COMMA-VISIBLE at this point in the FROM clause and is
           an ``input()``. Postgres scopes an implicit-LATERAL function call to
           the items written BEFORE it, so ``FROM unnest(f.audio) t,
           input('f') f`` genuinely does not see ``f`` — the same rejection a
           misspelled alias gets. A CTE or a generated source has no probed
           metadata to put in the columns, so neither is a track source.
        3. The row table needs a NAME, and it is a plain one: a row alias is a
           table alias, and a column list on it (``t(x)``) would rename the
           row schema, which is fixed by the stream type.
        4. ``WITH ORDINALITY`` is rejected: the row number it adds is
           ``index``, which every row already carries, 1-based.
        """
        _check_query_args(
            unnest, frozenset({"expressions", "alias", "offset"}), "unnest"
        )
        if unnest.args.get("offset"):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "unnest ... WITH ORDINALITY is not supported",
                unnest,
                hint="every track row already carries its 1-based position as "
                "<alias>.index",
            )

        arguments = unnest.expressions
        if len(arguments) != 1:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "unnest takes exactly one array column",
                _first_expression(arguments[1:]) or unnest,
                fallback=unnest,
                hint=_UNNEST_HINT,
            )
        argument = arguments[0]
        if not isinstance(argument, exp.Column) or isinstance(argument.this, exp.Star):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "unnest takes a bare array column, not "
                f"{_describe_unnest_arg(argument)}",
                argument if isinstance(argument, exp.Expr) else unnest,
                fallback=unnest,
                hint=_UNNEST_HINT,
            )
        if argument.args.get("db") or argument.args.get("catalog"):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "qualified column names are not supported",
                argument,
                fallback=unnest,
            )
        table_node = argument.args.get("table")
        if table_node is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"unqualified column '{argument.name}' in unnest",
                argument,
                fallback=unnest,
                hint=_UNNEST_HINT,
            )
        source = _ident_name(table_node)
        column = _ident_name(argument.this)
        kind = scope.get(source)
        if kind is None:
            raise _error(
                ErrorCode.UNKNOWN_ALIAS,
                f"unknown alias '{source}'",
                table_node,
                fallback=unnest,
                hint=self._known_hint(scope),
            )
        if kind != "input":
            what = "a track-row table" if kind == "row" else f"a {kind}"
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{source}' is {what}, and only an input's array column can "
                "be unnested",
                table_node,
                fallback=unnest,
                hint="a track row's columns are PROBED metadata, so its tracks "
                "must come from a file: unnest an input('path') alias",
            )
        if column not in UNNEST_COLUMNS:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{source}.{column}' is not an array column",
                argument,
                fallback=unnest,
                hint=f"unnest one of {_listed_columns(UNNEST_COLUMNS)}, "
                f"e.g. unnest({source}.audio) t",
            )

        alias_node = unnest.args.get("alias")
        if not isinstance(alias_node, exp.TableAlias) or alias_node.this is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"unnest({source}.{column}) requires an alias",
                unnest,
                hint=f"name the rows, e.g. unnest({source}.{column}) t",
            )
        if alias_node.args.get("columns"):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "table column aliases are not supported",
                alias_node,
                fallback=unnest,
                hint="a track row's columns are fixed by the stream type: "
                f"{_listed_columns(ROW_SCHEMAS[column])}",
            )
        alias = _ident_name(alias_node.this)
        self._reserve(alias, alias_node.this)
        if alias in scope:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL, f"duplicate name '{alias}'", alias_node.this
            )
        self.track_rows[alias] = RawTrackRows(
            alias=alias, source=source, column=column, node=unnest
        )
        scope[alias] = "row"

    def _local_alias(
        self, name: str, alias_node: exp.Expr | None, table: exp.Table
    ) -> str:
        """The name a view/CTE is read under in THIS branch.

        ``FROM master`` reads it under its own name; ``FROM master m`` binds
        ``m``, and that binding is BRANCH-LOCAL — nothing else about a branch
        escapes it either, so two branches (or two COPYs of one script) may
        both spell it ``m``, and the alias is not recorded in the flat
        namespace. What it may NOT do is SHADOW that namespace: an alias that
        collides with a view, a CTE, an input alias, a generated source or
        the ``ffmpeg`` namespace would make one name mean two things inside a
        single FROM clause. :meth:`_reserve` is exactly that check, and it
        records nothing, so it is the whole rule here.
        """
        if alias_node is None:
            return name
        if not isinstance(alias_node, exp.TableAlias) or alias_node.this is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"malformed alias for '{name}'",
                alias_node,
                fallback=table,
            )
        local = _ident_name(alias_node.this)
        self._reserve(local, alias_node.this)
        return local

    def _add_input(
        self,
        table: exp.Table,
        func: exp.Anonymous,
        alias_node: exp.Expr | None,
        scope: dict[str, str],
    ) -> None:
        func_name = str(func.this).lower()
        if func_name != "input":
            hint = "the only table function is input('path')"
            if func_name == CHAPTERS_COLUMN:
                argument = func.expressions[0] if func.expressions else None
                source = (
                    _ident_name(argument.this)
                    if isinstance(argument, exp.Column)
                    and isinstance(argument.this, exp.Identifier)
                    else "f"
                )
                hint = (
                    f"chapters is an array column of the input: write "
                    f"unnest({source}.{CHAPTERS_COLUMN}) c"
                )
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"unsupported table function {func_name}()",
                func,
                fallback=table,
                hint=hint,
            )
        args = func.expressions
        if not args or not (isinstance(args[0], exp.Literal) and args[0].is_string):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "input() takes a string literal path, optionally followed by "
                "named options",
                func,
                fallback=table,
                hint="use input('clip.mp4') or input('logo.png', loop => true)",
            )
        path_node = args[0]
        path = str(path_node.this)
        options = _input_options(args[1:], path_node)
        if not isinstance(alias_node, exp.TableAlias) or alias_node.this is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "input() requires an alias",
                func,
                fallback=table,
                hint=_ALIAS_HINT,
            )
        alias = _ident_name(alias_node.this)
        self._reserve(alias, alias_node.this)
        if alias in scope:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL, f"duplicate name '{alias}'", alias_node.this
            )
        # Dedup key is the ALIAS, not the path: the same file under two aliases
        # is two -i entries (see the README PiP example).
        self.sources[alias] = len(self.input_paths)
        self.input_paths.append(path)
        if options:
            self.input_options[alias] = options
        scope[alias] = "input"

    def _add_source(
        self,
        table: exp.Table,
        inner: exp.Expr | None,
        alias_node: exp.Expr | None,
        scope: dict[str, str],
    ) -> None:
        """``FROM ffmpeg.<source>(<named options>) alias``.

        Shapes VERIFIED under sqlglot 30.17 ``read="postgres"`` (every one of
        them a plain ``exp.Table`` with ``db=Identifier(ffmpeg)`` -- an
        ``exp.Dot`` never appears in FROM position, unlike the same namespace
        in CALL position):

        ==================================== ==========================================
        written                              ``Table.this`` / how it lands here
        ==================================== ==========================================
        ``ffmpeg.testsrc(duration => 2) t``  ``Anonymous(testsrc, [Kwarg(duration, 2)])``
        ``ffmpeg.testsrc() t``               ``Anonymous(testsrc)``, no ``expressions``
        ``ffmpeg.testsrc t``                 ``Identifier(testsrc)`` -> rejected, hint
        ``ffmpeg.testsrc(duration => 2)``    no ``alias`` -> rejected (alias mandatory)
        ``ffmpeg.testsrc(2) t``              ``Anonymous`` with a bare ``Literal`` arg
        ``ffmpeg.testsrc(2, d => 1) t``      ``Literal`` then ``Kwarg``, same rejection
        ``FFMPEG.TestSrc(...) t``            name kept VERBATIM (``'TestSrc'``), folded
                                             here; ``db`` folds the Postgres way
        ``x.ffmpeg.testsrc(...) t``          also fills ``catalog`` -> not the namespace
        ==================================== ==========================================

        A CTE body and a ``UNION ALL`` branch produce the identical Table
        shape (they are ordinary SELECTs), and so does a comma cross-join
        alongside ``input('...')`` -- the source lands in ``Join.this``, which
        :meth:`_collect_scope` feeds through this same path.

        Options are collected raw and are NOT looked at here: which options a
        source has is a property of the installed ffmpeg, and only lower (with
        its registry) knows that.
        """
        if isinstance(inner, exp.Identifier):
            # `FROM ffmpeg.testsrc t` -- a bare qualified NAME, not a call.
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{FILTER_NAMESPACE}.{_ident_name(inner)}' is not a table",
                inner,
                fallback=table,
                hint=_SOURCE_CALL_HINT,
            )
        if not isinstance(inner, exp.Anonymous):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"malformed {FILTER_NAMESPACE}.<source>() in FROM",
                inner if isinstance(inner, exp.Expr) else None,
                fallback=table,
                hint=_SOURCE_CALL_HINT,
            )
        # Function names are case-insensitive in this dialect (lower folds a
        # call's name the same way); ffmpeg's own filter names are lowercase.
        name = str(inner.this).lower()
        options = _source_options(inner.expressions, inner, name)
        if not isinstance(alias_node, exp.TableAlias) or alias_node.this is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"{FILTER_NAMESPACE}.{name}() requires an alias",
                inner,
                fallback=table,
                hint=_SOURCE_ALIAS_HINT,
            )
        alias = _ident_name(alias_node.this)
        self._reserve(alias, alias_node.this)
        if alias in scope:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL, f"duplicate name '{alias}'", alias_node.this
            )
        # NO input index is assigned: a source is a zero-input filter node,
        # not an `-i`. `input_paths`/`sources` stay untouched.
        self.source_filters[alias] = RawSource(
            alias=alias, name=name, options=options, call_node=inner
        )
        scope[alias] = "source"

    def _known_hint(self, names: set[str] | dict[str, str]) -> str:
        known = ", ".join(sorted(names))
        return f"known names: {known}" if known else "no aliases are in scope"

    # -- columns / WHERE --------------------------------------------------

    def _check_columns(
        self,
        node: exp.Expr,
        scope: dict[str, str],
        select: exp.Select,
        *,
        table_mode: bool = False,
    ) -> None:
        for sub in node.walk():
            if isinstance(sub, exp.Dot):
                # `<alias>.<type>[k].<column>` reads metadata, which is no
                # SELECT output in a MEDIA query: streams are the only outputs
                # one has. A table/csv query prints the metadata columns.
                shape = subscript_metadata_shape(sub)
                if shape is not None:
                    self._check_output_accessor(sub, shape, scope, select, table_mode=table_mode)
                continue
            if not isinstance(sub, exp.Column):
                continue
            bare_array = _bare_array_error(sub, select)
            if bare_array is not None:
                raise bare_array
            if sub.args.get("db") or sub.args.get("catalog"):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "qualified column names are not supported",
                    sub,
                    fallback=select,
                )
            table_node = sub.args.get("table")
            if table_node is None:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"unqualified column '{sub.name}'",
                    sub,
                    fallback=select,
                    hint="qualify the column with its alias, e.g. a.video[1]",
                )
            name = _ident_name(table_node)
            kind = scope.get(name)
            if kind is None:
                # `ffmpeg.<x>` with no parentheses is a COLUMN, not a namespaced
                # call: the namespace only exists in call position, so say so
                # instead of listing aliases the user never meant.
                if name == FILTER_NAMESPACE:
                    hint = (
                        f"{FILTER_NAMESPACE}.<filter>(...) is a call, not a column; "
                        f"write {FILTER_NAMESPACE}.{sub.name}(<stream>, "
                        "<option> => <value>) or select a real alias's stream"
                    )
                elif name == MACRO_NAMESPACE:
                    hint = (
                        f"{MACRO_NAMESPACE}.<name>(...) is a call, not a column; "
                        f"write {MACRO_NAMESPACE}.{sub.name}(<stream>, ...) or "
                        "select a real alias's stream"
                    )
                else:
                    hint = self._known_hint(scope)
                raise _error(
                    ErrorCode.UNKNOWN_ALIAS,
                    f"unknown alias '{name}'",
                    table_node,
                    fallback=select,
                    hint=hint,
                )
            # `<alias>.*` is an exp.Column whose `this` is a Star: the alias
            # still has to exist (checked just above), but there is no column
            # NAME to whitelist -- the star names all of them.
            if isinstance(sub.this, exp.Star):
                continue
            # An input exposes a fixed set of pseudo-columns. A CTE exposes
            # whatever its body named with AS, and a generated source exposes
            # exactly one stream of a type only the registry knows, so only
            # lower can check either of those.
            if kind == "input" and _ident_name(sub.this) == _REMOVED_FRAME:
                raise _frame_error(sub, name, select)
            if kind == "input" and _ident_name(sub.this) in _REMOVED_INPUT_TAGS:
                raise _removed_tag_error(name, _ident_name(sub.this), sub, select)
            if kind == "input" and _is_input_disposition(_ident_name(sub.this)):
                raise _input_disposition_error(name, sub, select)
            if kind == "input" and not _is_input_column(_ident_name(sub.this)):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"unknown column '{name}.{sub.name}'",
                    sub,
                    fallback=select,
                    hint=f"an input exposes {', '.join(sorted(INPUT_COLUMNS))}; "
                    f"{_TAGS_HINT}",
                )
            # A track-row table's schema is fixed by the stream type it
            # unnested, so it is checkable HERE -- unlike
            # a CTE's, which only lower knows.
            if kind == "row":
                self._check_row_column(sub, name, select)

    def _check_row_column(self, column: exp.Column, alias: str, select: exp.Expr) -> str:
        """Whitelist one ``<row alias>.<column>`` and return its column type."""
        array_column = self.track_rows[alias].column
        schema = ROW_SCHEMAS[array_column]
        name = _ident_name(column.this)
        if name == ROW_STREAM and not column.this.args.get("quoted"):
            if array_column == CHAPTERS_COLUMN:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"'{alias}' is a chapter row, not a stream",
                    column,
                    fallback=select,
                    hint=_CHAPTER_NOT_STREAM_HINT.format(alias=alias),
                )
            return "stream"
        if name == _REMOVED_ROW_STREAM and array_column != CHAPTERS_COLUMN:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{alias}.{_REMOVED_ROW_STREAM}' is not a column",
                column,
                fallback=select,
                hint=f"the row is the stream: use '{alias}'",
            )
        ref = map_ref(name)
        if ref is not None:
            if array_column == CHAPTERS_COLUMN:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"'{alias}' is a chapter row, and a chapter carries no "
                    f"{ref[0]}",
                    column,
                    fallback=select,
                    hint=f"a chapter row exposes {_listed_columns(schema)}",
                )
            return _map_column_type(ref, alias, column, select)
        if name in _REMOVED_STREAM_TAGS and name not in schema:
            raise _removed_tag_error(alias, name, column, select)
        column_type = schema.get(name)
        if column_type is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"unknown column '{alias}.{column.name}'",
                column,
                fallback=select,
                hint=f"{self.track_rows[alias].column} track rows expose "
                f"{_listed_columns(schema)}",
            )
        return column_type

    # -- subscript metadata accessors: <alias>.<type>[k].<column> ----------

    def _check_accessor(
        self,
        bracket: exp.Bracket,
        name: str,
        anchor: exp.Expr,
        scope: dict[str, str],
        fallback: exp.Expr,
    ) -> str:
        """Validate one ``<alias>.<type>[k].<name>`` accessor; return its type.

        Returns one of :data:`ROW_SCHEMAS`'s column types. Shared by every
        context an accessor can appear in -- SELECT (where no metadata
        accessor survives, since a SELECT column is an output stream) and
        WHERE (the row grammar, reused rather than reinvented for a plain
        input alias's subscript) -- so the alias/array/subscript/column checks
        are written exactly once.
        """
        inner = bracket.this
        if not isinstance(inner, exp.Column):  # defensive: _check_subscript checked it
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "a subscript metadata accessor needs a stream column",
                bracket,
                fallback=fallback,
                hint=_SUBSCRIPT_HINT,
            )
        table_node = inner.args.get("table")
        if table_node is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"unqualified column '{inner.name}'",
                inner,
                fallback=fallback,
                hint=_ALIAS_HINT,
            )
        alias = _ident_name(table_node)
        kind = scope.get(alias)
        array_column = _ident_name(inner.this)
        if kind != "input":
            raise self._accessor_alias_error(
                alias, kind, array_column, name, table_node, scope, fallback
            )
        if array_column == CHAPTERS_COLUMN:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{alias}.{CHAPTERS_COLUMN}' cannot be subscripted: a chapter "
                "is not a stream",
                inner,
                fallback=fallback,
                hint=chapters_unnest_hint(alias),
            )
        if array_column not in STREAM_ARRAY_COLUMNS:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{alias}.{array_column}' has no per-track metadata",
                inner,
                fallback=fallback,
                hint=f"metadata accessors need an array column: "
                f"{_listed_columns(STREAM_ARRAY_COLUMNS)}",
            )
        if subscript_index(bracket) is None:  # defensive: _check_subscript checked it
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "stream subscript must be a positive integer literal",
                bracket,
                fallback=fallback,
                hint=_SUBSCRIPT_HINT,
            )
        if name == _REMOVED_ROW_STREAM:
            label = _subscript_label(bracket)
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{label}.{_REMOVED_ROW_STREAM}' is not a column",
                anchor,
                fallback=fallback,
                hint=f"the subscript is already the stream: use '{label}'",
            )
        schema = ROW_SCHEMAS[array_column]
        ref = map_ref(name)
        if ref is not None:
            return _map_column_type(ref, _subscript_label(bracket), anchor, fallback)
        if name in _REMOVED_STREAM_TAGS and name not in schema:
            raise _removed_tag_error(_subscript_label(bracket), name, anchor, fallback)
        if name in MAP_COLUMNS:
            label = _subscript_label(bracket)
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{label}.{name}' is the whole {map_noun(name)} map, not a value",
                anchor,
                fallback=fallback,
                hint=f"name the key: '{label}.{name}.{map_example(name)}', or "
                f"unnest the array (unnest({alias}.{array_column}) t) and read "
                f"t.{name}",
            )
        column_type = schema.get(name)
        if column_type is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"unknown column '{alias}.{array_column}[...].{name}'",
                anchor,
                fallback=fallback,
                hint=f"{array_column} track rows expose {_listed_columns(schema)}",
            )
        return column_type

    def _accessor_alias_error(
        self,
        alias: str,
        kind: str | None,
        array_column: str,
        name: str,
        table_node: exp.Expr,
        scope: dict[str, str],
        fallback: exp.Expr,
    ) -> SqlmpegError:
        """Why ``alias`` cannot carry a subscript metadata accessor.

        A CTE or a generated source has no probed metadata (the same reason
        neither can be ``unnest``ed); a row alias already
        IS the metadata table and wants its own columns directly, not another
        subscript layer on top. Empirically decided: none of the
        three has anything a subscript accessor could read, so each gets its
        own clear rejection rather than a half-working fallback.
        """
        if kind == "cte":
            return _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{alias}.{array_column}[...].{name}' has no metadata: "
                f"'{alias}' is a CTE, and a CTE's columns carry no probed metadata",
                table_node,
                fallback=fallback,
                hint="subscript metadata comes from the probed file; reference "
                "the input directly instead of through a CTE built over it",
            )
        if kind == "source":
            return _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{alias}.{array_column}[...].{name}' has no metadata: "
                f"'{alias}' is a generated source, and nothing was probed for it",
                table_node,
                fallback=fallback,
                hint="a generated source's shape comes from its own options, "
                "not from probed metadata",
            )
        if kind == "row":
            return _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{alias}' is already a track-row table; use its metadata "
                f"columns directly ({alias}.{name}), not a subscript",
                table_node,
                fallback=fallback,
                hint=f"a row table's columns ARE the per-track metadata; write "
                f"{alias}.{name} instead of {alias}.{array_column}[...].{name}",
            )
        return _error(
            ErrorCode.UNKNOWN_ALIAS,
            f"unknown alias '{alias}'",
            table_node,
            fallback=fallback,
            hint=self._known_hint(scope),
        )

    def _check_output_accessor(
        self,
        dot: exp.Dot,
        shape: tuple[exp.Bracket, str],
        scope: dict[str, str],
        select: exp.Select,
        *,
        table_mode: bool = False,
    ) -> None:
        """A ``<alias>.<type>[k].<column>`` in SELECT position: never a stream.

        Streams are the only SELECT output a MEDIA query has, and every
        accessor names a piece of metadata -- a string or a number -- which
        has nowhere to go on an ffmpeg command line. A table/csv query is the
        one exception: its whole point is printing that metadata, so
        ``table_mode`` skips this rejection there.
        """
        bracket, name = shape
        self._check_accessor(bracket, name, dot, scope, select)
        if not table_mode:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'.{name}' is track metadata, not a stream, and a SELECT "
                "column is an output stream",
                dot,
                fallback=select,
                hint="streams are the only output: drop the accessor, the "
                "subscript is already the stream, and filter on metadata in "
                "WHERE instead",
            )

    def _subscript_operand(
        self,
        bracket: exp.Bracket,
        name: str,
        anchor: exp.Expr,
        scope: dict[str, str],
        where: exp.Where,
    ) -> str:
        """Check one WHERE accessor operand and return its type."""
        return self._check_accessor(bracket, name, anchor, scope, where)

    def _check_subscript_conjunct(
        self, conjunct: exp.Expr, scope: dict[str, str], where: exp.Where
    ) -> bool:
        """Shape-check `conjunct` if it holds a subscript metadata accessor.

        ``<alias>.<type>[k].<column>`` compares like a row column -- same
        grammar, same static literal typing -- but the alias is an ordinary
        INPUT alias, not an unnest row, so it is told apart by SHAPE (a ``Dot``
        over a ``Bracket``) rather than by which kind of name it is. A loose
        column reference elsewhere in the same conjunct -- typically the time
        window, ``<alias>.t`` -- means the conjunct mixes the two languages,
        the same rejection :meth:`_check_row_conjunct` gives a row/non-row mix.
        """
        shapes = [
            subscript_metadata_shape(sub)
            for sub in conjunct.walk()
            if isinstance(sub, exp.Dot)
        ]
        accessors = [shape for shape in shapes if shape is not None]
        if not accessors:
            return False
        covered = {id(bracket.this) for bracket, _ in accessors}
        for sub in conjunct.walk():
            if isinstance(sub, exp.Column) and id(sub) not in covered:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "a WHERE predicate cannot mix subscript metadata accessors "
                    "with the time window",
                    conjunct,
                    fallback=where,
                    hint="subscript metadata is checked at compile time and a "
                    "time window is a seek on the input; write them as "
                    "separate AND conjuncts",
                )
        self._check_subscript_predicate(conjunct, scope, where)
        return True

    def _check_subscript_predicate(
        self, node: exp.Expr, scope: dict[str, str], where: exp.Where
    ) -> None:
        """One compile-time subscript metadata predicate, recursively.

        The same closed grammar :meth:`_check_row_predicate` checks --
        AND/OR/NOT over comparisons of one accessor against one literal, plus
        BETWEEN and IS [NOT] NULL -- with an accessor (a ``Dot``-over-
        ``Bracket`` shape) standing where a row column stood there.
        """
        node = _unwrap_paren(node)
        if isinstance(node, exp.And | exp.Or):
            self._check_subscript_predicate(node.this, scope, where)
            expression = node.args.get("expression")
            if not isinstance(expression, exp.Expr):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "malformed WHERE predicate",
                    node,
                    fallback=where,
                    hint=_SUBSCRIPT_WHERE_HINT,
                )
            self._check_subscript_predicate(expression, scope, where)
            return
        if isinstance(node, exp.Not):
            if not isinstance(node.this, exp.Expr):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "malformed WHERE predicate",
                    node,
                    fallback=where,
                    hint=_SUBSCRIPT_WHERE_HINT,
                )
            self._check_subscript_predicate(node.this, scope, where)
            return
        if isinstance(node, exp.Is):
            operand = _unwrap_paren(node.this) if isinstance(node.this, exp.Expr) else None
            shape = subscript_metadata_shape(operand) if isinstance(operand, exp.Expr) else None
            if not isinstance(node.args.get("expression"), exp.Null) or shape is None:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "only 'IS NULL' and 'IS NOT NULL' are supported",
                    node,
                    fallback=where,
                    hint=_SUBSCRIPT_WHERE_HINT,
                )
            assert operand is not None
            self._subscript_operand(shape[0], shape[1], operand, scope, where)
            return
        if isinstance(node, exp.Between):
            if node.args.get("symmetric"):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "BETWEEN SYMMETRIC is not supported",
                    node,
                    fallback=where,
                    hint=_SUBSCRIPT_WHERE_HINT,
                )
            operand = _unwrap_paren(node.this) if isinstance(node.this, exp.Expr) else None
            shape = subscript_metadata_shape(operand) if isinstance(operand, exp.Expr) else None
            if shape is None:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "BETWEEN needs a subscript metadata accessor on its left",
                    node,
                    fallback=where,
                    hint=_SUBSCRIPT_WHERE_HINT,
                )
            assert operand is not None
            column_type = self._subscript_operand(shape[0], shape[1], operand, scope, where)
            label = _accessor_label(shape[0], shape[1])
            for bound in (node.args.get("low"), node.args.get("high")):
                self._check_literal_type(
                    bound, column_type, label, where, hint=_SUBSCRIPT_WHERE_HINT
                )
            return
        if isinstance(node, exp.EQ | exp.NEQ | exp.GT | exp.GTE | exp.LT | exp.LTE):
            left = _unwrap_paren(node.this) if isinstance(node.this, exp.Expr) else None
            right = node.args.get("expression")
            right = _unwrap_paren(right) if isinstance(right, exp.Expr) else None
            left_shape = subscript_metadata_shape(left) if isinstance(left, exp.Expr) else None
            right_shape = (
                subscript_metadata_shape(right) if isinstance(right, exp.Expr) else None
            )
            if left_shape is not None:
                shape, operand, literal = left_shape, left, right
            else:
                shape, operand, literal = right_shape, right, left
            if shape is None or operand is None:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "a subscript metadata comparison needs an accessor on one side",
                    node,
                    fallback=where,
                    hint=_SUBSCRIPT_WHERE_HINT,
                )
            column_type = self._subscript_operand(shape[0], shape[1], operand, scope, where)
            label = _accessor_label(shape[0], shape[1])
            self._check_literal_type(
                literal, column_type, label, where, hint=_SUBSCRIPT_WHERE_HINT
            )
            return
        shape = subscript_metadata_shape(node)
        if shape is not None:
            # A boolean accessor stands alone, as a boolean row column does.
            column_type = self._subscript_operand(shape[0], shape[1], node, scope, where)
            if column_type == "boolean":
                return
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{_accessor_label(shape[0], shape[1])}' is {column_type}, "
                "not a condition",
                node,
                fallback=where,
                hint=_SUBSCRIPT_WHERE_HINT,
            )
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            "unsupported WHERE predicate",
            node,
            fallback=where,
            hint=_SUBSCRIPT_WHERE_HINT,
        )

    def _check_where(
        self,
        where: exp.Where,
        scope: dict[str, str],
        select: exp.Select,
        *,
        fanout: bool = False,
    ) -> None:
        conjuncts: list[exp.Expr] = []
        self._flatten_and(where.this, conjuncts, select)
        for conjunct in conjuncts:
            for sub in conjunct.walk():
                if isinstance(sub, exp.Column):
                    bare_array = _bare_array_error(sub, where)
                    if bare_array is not None:
                        raise bare_array
                    alias = _ident_name(sub.args.get("table"))
                    if scope.get(alias) == "input" and _is_input_disposition(
                        _ident_name(sub.this)
                    ):
                        raise _input_disposition_error(alias, sub, where)
        conjuncts = [
            conjunct
            for conjunct in conjuncts
            if not self._check_row_conjunct(conjunct, scope, where, fanout=fanout)
            and not self._check_subscript_conjunct(conjunct, scope, where)
        ]

        # alias -> {"low": <bound>, "high": <bound>}, accumulated across
        # every conjunct so a repeated bound of one kind is rejected however it
        # is spelled (twice in one BETWEEN, two inequalities, or a mix), and so
        # a closed pair can be checked for an empty window once both are known.
        bounds: dict[str, dict[str, exp.Expr]] = {}
        for conjunct in conjuncts:
            parsed = _time_bounds(conjunct)
            if parsed is None:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "unsupported WHERE predicate",
                    conjunct,
                    fallback=where,
                    hint=_WHERE_HINT,
                )
            column, low, high, strict = parsed
            if strict:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "strict inequalities are not supported",
                    conjunct,
                    fallback=where,
                    hint=_STRICT_HINT,
                )
            table_node = column.args.get("table")
            if table_node is None:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"unqualified column '{column.name}' in WHERE",
                    column,
                    fallback=where,
                    hint=_WHERE_HINT,
                )
            alias = _ident_name(table_node)
            if alias not in scope:
                raise _error(
                    ErrorCode.UNKNOWN_ALIAS,
                    f"unknown alias '{alias}'",
                    table_node,
                    fallback=where,
                    hint=self._known_hint(scope),
                )
            if column.name.lower() != "t":
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"only the time column '{alias}.t' can be filtered, "
                    f"got '{alias}.{column.name}'",
                    column,
                    fallback=where,
                    hint=_WHERE_HINT,
                )
            alias_bounds = bounds.setdefault(alias, {})
            for kind, bound in (("low", low), ("high", high)):
                if bound is None:
                    continue
                self._check_time_bound(bound, scope, where, fanout=fanout)
                if kind in alias_bounds:
                    bound_name = "lower" if kind == "low" else "upper"
                    raise _error(
                        ErrorCode.UNSUPPORTED_SQL,
                        f"more than one {bound_name} bound for alias '{alias}'",
                        column,
                        fallback=where,
                        hint="each alias may set at most one lower and one "
                        "upper time bound",
                    )
                alias_bounds[kind] = bound

        for alias, alias_bounds in bounds.items():
            low_literal = alias_bounds.get("low")
            high_literal = alias_bounds.get("high")
            # Only a pair of literals is orderable HERE; a computed bound is
            # a number lower knows and checks the same window against.
            if not isinstance(low_literal, exp.Literal) or not isinstance(
                high_literal, exp.Literal
            ):
                continue
            start = _literal_seconds(low_literal)
            end = _literal_seconds(high_literal)
            if start is None or end is None or start < end:
                continue
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"empty time window for alias '{alias}': start "
                f"({low_literal.this}) is not before end ({high_literal.this})",
                high_literal,
                fallback=where,
                hint="the start bound must be strictly before the end bound",
            )

    def _check_time_bound(
        self,
        bound: exp.Expr,
        scope: dict[str, str],
        where: exp.Where,
        *,
        fanout: bool = False,
    ) -> None:
        """One trim bound: a number of seconds, literal or computed.

        A bound is still a SEEK on the input, so it has to be a number by the
        time lower reads it -- but which number may be arithmetic over probed
        scalars (``f.duration - 0.5``), so the value grammar types it here.

        Under a fan-out ``TO`` a bare row column (``c.start_t``) is a bound
        too: each command binds its own row, so the window is that row's.
        """
        if isinstance(bound, exp.Literal) and not bound.is_string:
            return
        if (
            (is_value_expr(bound) or _is_input_duration(bound, scope))
            or (fanout and _is_row_column(bound, scope))
        ) and (self._check_value_expr(bound, scope, where) == "number"):
            return
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            "time bounds must be numeric literals (seconds)",
            bound,
            fallback=where,
            hint=_WHERE_HINT,
        )

    # -- WHERE over track-row columns ---------------------------

    def _check_row_conjunct(
        self,
        conjunct: exp.Expr,
        scope: dict[str, str],
        where: exp.Where,
        *,
        fanout: bool = False,
    ) -> bool:
        """Shape-check `conjunct` if it is a ROW predicate; say whether it was one.

        The WHERE clause of a track-row query mixes two unrelated languages:
        the time window that becomes ``-ss``/``-to`` on an input, and the
        compile-time predicate that decides which rows survive. They are told
        apart by what a conjunct REFERENCES, which is unambiguous — a row alias
        is an alias like any other, and one name cannot be both. A conjunct
        that touches both is rejected rather than split: ``AND`` between them
        is legal Postgres, but the two halves run in different worlds (one on
        the ffmpeg command line, one in this compiler), and quietly cutting the
        expression in half is the kind of approximation guardrail #3 bans.

        A fan-out ``TO`` reopens exactly one mixed shape: a time window on a
        non-row alias whose BOUNDS are row columns (``WHERE f.t BETWEEN
        c.start_t AND c.end_t``). Both halves then run in one world -- the
        command being built for that row -- so there is nothing to cut in half.

        Returns True when the conjunct was a row predicate (and is now
        validated), False when it belongs to the time-window path.
        """
        aliases = _referenced_aliases(conjunct)
        rows = {alias for alias in aliases if scope.get(alias) == "row"}
        if not rows:
            return False
        others = aliases - rows
        if others:
            if fanout and _is_row_bounded_window(conjunct, scope):
                return False
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "a WHERE predicate cannot mix track-row columns "
                f"({_listed_columns(rows)}) with other aliases "
                f"({_listed_columns(others)})",
                conjunct,
                fallback=where,
                hint="track rows are filtered at compile time and a time window "
                "is a seek on the input; write them as separate AND conjuncts",
            )
        if len(rows) > 1:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "a WHERE predicate may reference only one track-row table, got "
                f"{_listed_columns(rows)}",
                conjunct,
                fallback=where,
                hint="filter each unnest separately; matching rows of two "
                "tables against each other is a JOIN",
            )
        self._check_row_predicate(conjunct, scope, where)
        return True

    def _check_row_predicate(
        self, node: exp.Expr, scope: dict[str, str], where: exp.Expr
    ) -> None:
        """One compile-time row predicate, recursively.

        The grammar is small and closed: ``AND`` / ``OR`` / ``NOT`` over
        comparisons of ONE row column against ONE literal (or against a CASE /
        ``||`` value), plus ``BETWEEN`` and ``IS [NOT] NULL``. Evaluation is
        lower's (it has the probes); this
        checks the shape and the TYPES, which are static — a row column's type
        comes from :data:`ROW_SCHEMAS`, not from whatever the file happened to
        contain, so ``t.channels = 'stereo'`` is rejected even for a track
        whose channel count was never probed.
        """
        node = _unwrap_paren(node)
        if isinstance(node, exp.And | exp.Or):
            self._check_row_predicate(node.this, scope, where)
            expression = node.args.get("expression")
            if not isinstance(expression, exp.Expr):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "malformed WHERE predicate",
                    node,
                    fallback=where,
                    hint=_ROW_WHERE_HINT,
                )
            self._check_row_predicate(expression, scope, where)
            return
        if isinstance(node, exp.Not):
            if not isinstance(node.this, exp.Expr):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "malformed WHERE predicate",
                    node,
                    fallback=where,
                    hint=_ROW_WHERE_HINT,
                )
            self._check_row_predicate(node.this, scope, where)
            return
        if isinstance(node, exp.Is):
            # `IS NULL` / `IS NOT NULL` (the latter is the same node with
            # `negate=True`, VERIFIED under sqlglot 30.17).
            column = _unwrap_paren(node.this) if isinstance(node.this, exp.Expr) else None
            if not isinstance(node.args.get("expression"), exp.Null) or not isinstance(
                column, exp.Column
            ):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "only 'IS NULL' and 'IS NOT NULL' are supported",
                    node,
                    fallback=where,
                    hint=_ROW_WHERE_HINT,
                )
            self._row_operand(column, scope, where)
            return
        if isinstance(node, exp.Between):
            if node.args.get("symmetric"):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "BETWEEN SYMMETRIC is not supported",
                    node,
                    fallback=where,
                    hint=_ROW_WHERE_HINT,
                )
            column = _unwrap_paren(node.this) if isinstance(node.this, exp.Expr) else None
            if is_value_expr(column):
                # A computed subject types against each bound the way any
                # comparison's two sides do; there is no COLUMN to name.
                for bound in (node.args.get("low"), node.args.get("high")):
                    self._check_value_pair(column, bound, scope, where, _ROW_WHERE_HINT)
                return
            if not isinstance(column, exp.Column):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "BETWEEN needs a track-row column on its left",
                    node,
                    fallback=where,
                    hint=_ROW_WHERE_HINT,
                )
            column_type = self._row_operand(column, scope, where)
            for bound in (node.args.get("low"), node.args.get("high")):
                if is_value_expr(_unwrap_paren(bound) if isinstance(bound, exp.Expr) else None):
                    self._check_value_pair(column, bound, scope, where, _ROW_WHERE_HINT)
                    continue
                self._check_row_literal(bound, column, column_type, where)
            return
        if isinstance(node, exp.EQ | exp.NEQ | exp.GT | exp.GTE | exp.LT | exp.LTE):
            left = _unwrap_paren(node.this) if isinstance(node.this, exp.Expr) else None
            right = node.args.get("expression")
            right = _unwrap_paren(right) if isinstance(right, exp.Expr) else None
            if is_value_expr(left) or is_value_expr(right):
                self._check_value_pair(left, right, scope, where, _ROW_WHERE_HINT)
                return
            column, literal = (left, right) if isinstance(left, exp.Column) else (right, left)
            if not isinstance(column, exp.Column):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "a track-row comparison needs a row column on one side",
                    node,
                    fallback=where,
                    hint=_ROW_WHERE_HINT,
                )
            if isinstance(literal, exp.Column):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "a track-row comparison compares one column against a literal",
                    node,
                    fallback=where,
                    hint="matching two columns against each other is a JOIN",
                )
            column_type = self._row_operand(column, scope, where)
            self._check_row_literal(literal, column, column_type, where)
            return
        if isinstance(node, exp.Boolean):
            return
        if isinstance(node, exp.Column):
            # A boolean column stands alone, as it does in Postgres; anything
            # else needs an operator to become a condition.
            column_type = self._row_operand(node, scope, where)
            if column_type == "boolean":
                return
            alias = _ident_name(node.args.get("table"))
            label = f"{alias}.{column_label(_ident_name(node.this))}"
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{label}' is {column_type}, not a condition",
                node,
                fallback=where,
                hint=_ROW_WHERE_HINT,
            )
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            "unsupported WHERE predicate",
            node,
            fallback=where,
            hint=_ROW_WHERE_HINT,
        )

    def _row_operand(
        self, column: exp.Column, scope: dict[str, str], where: exp.Expr
    ) -> str:
        """Check one value-expression column operand and return its type.

        A track-row column, or an input alias's ``duration`` / container tag;
        the row itself is a stream and is rejected as one.
        """
        table_node = column.args.get("table")
        if table_node is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"unqualified column '{column.name}' in WHERE",
                column,
                fallback=where,
                hint=_ROW_WHERE_HINT,
            )
        alias = _ident_name(table_node)
        if scope.get(alias) != "row":
            # `<input>.duration` is the one non-row column the value grammar
            # reads: a probed container scalar, so it types as a number here
            # and lower rejects it on an input it could not probe.
            if _is_input_duration(column, scope):
                return "number"
            # A container tag reads off the same probe, one line down: text,
            # NULL when the file carries no such key.
            if _is_input_tag(column, scope):
                return "text"
            if alias not in scope:
                raise _error(
                    ErrorCode.UNKNOWN_ALIAS,
                    f"unknown alias '{alias}'",
                    table_node,
                    fallback=where,
                    hint=self._known_hint(scope),
                )
            if scope[alias] == "input" and _ident_name(column.this) == _REMOVED_FRAME:
                raise _frame_error(column, alias, where)
            if scope[alias] == "input" and _ident_name(column.this) in _REMOVED_INPUT_TAGS:
                raise _removed_tag_error(alias, _ident_name(column.this), column, where)
            if scope[alias] == "input" and _is_input_disposition(_ident_name(column.this)):
                raise _input_disposition_error(alias, column, where)
            if scope[alias] == "input" and _ident_name(column.this) == TAGS_COLUMN:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"'{alias}.{TAGS_COLUMN}' is the whole tag map, not a "
                    "single value",
                    column,
                    fallback=where,
                    hint=f"name the key: '{alias}.{TAGS_COLUMN}.title'",
                )
            if scope[alias] == "input" and _ident_name(column.this) == CHAPTERS_COLUMN:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"'{alias}.{CHAPTERS_COLUMN}' is an array of chapter "
                    "records, not a single value",
                    column,
                    fallback=where,
                    hint=chapters_unnest_hint(alias),
                )
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"unknown column '{alias}.{column.name}'",
                column,
                fallback=where,
                hint=f"'{alias}' is not a track-row table; the values an input "
                f"alias carries are '{alias}.{INPUT_DURATION_COLUMN}' and its "
                f"container tags ({alias}.{TAGS_COLUMN}.title, ...)",
            )
        column_type = self._check_row_column(column, alias, where)
        if column_type == "stream":
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{alias}' is a stream, not a value to compare",
                column,
                fallback=where,
                hint="filter on the metadata columns, e.g. "
                f"WHERE {alias}.{TAGS_COLUMN}.language = 'eng'",
            )
        if is_array(column_type):
            name = _ident_name(column.this)
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{alias}.{name}' is the whole {map_noun(name)} map, "
                "not a single value",
                column,
                fallback=where,
                hint=f"name the key: '{alias}.{name}.{map_example(name)}'",
            )
        return column_type

    # -- compile-time value expressions: literals, row columns, CASE, ||,
    #    arithmetic, ::text ---------------------------------------------------

    def _check_value_expr(
        self, node: exp.Expr | None, scope: dict[str, str], fallback: exp.Expr
    ) -> str | None:
        """Static type of one compile-time value expression.

        ``"text"``, ``"number"``, ``"boolean"``, or None where the type is
        open — a bare NULL,
        or a CASE whose every result is NULL. Static because both sources are:
        a row column's type comes from :data:`ROW_SCHEMAS` and a literal's from
        how it was written, so nothing here depends on what a file contained.
        """
        value = _unwrap_paren(node) if isinstance(node, exp.Expr) else None
        if value is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "malformed value expression",
                fallback=fallback,
                hint=_VALUE_HINT,
            )
        if isinstance(value, exp.Null):
            return None
        if isinstance(value, exp.Column):
            return self._row_operand(value, scope, fallback)
        if isinstance(value, exp.Neg):
            return self._check_numeric(value.this, "negation", value, scope, fallback)
        literal_type = _literal_type(value)
        if literal_type is not None:
            return literal_type
        if isinstance(value, exp.Case):
            return self._check_case(value, scope, fallback)
        if isinstance(value, exp.DPipe):
            return self._check_concat(value, scope, fallback)
        if isinstance(value, _ARITHMETIC):
            return self._check_arithmetic(value, scope, fallback)
        if isinstance(value, exp.Cast):
            return self._check_cast(value, scope, fallback)
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"unsupported value expression: {value.__class__.__name__.upper()}",
            value,
            fallback=fallback,
            hint=_VALUE_HINT,
        )

    def _check_case(
        self, node: exp.Case, scope: dict[str, str], fallback: exp.Expr
    ) -> str | None:
        """One CASE, searched or simple; its result type.

        Searched (``CASE WHEN <predicate>``): each condition is an ordinary row
        predicate, checked by the same grammar WHERE uses. Simple (``CASE <x>
        WHEN <value>``): each WHEN is compared with the operand, so the two
        must share a type. Either way the results must agree on ONE type, with
        NULL fitting any of them — that agreement is what types the CASE.
        """
        operand = node.this if isinstance(node.this, exp.Expr) else None
        results: list[exp.Expr | None] = []
        for branch in node.args.get("ifs") or []:
            if not isinstance(branch, exp.If) or not isinstance(branch.this, exp.Expr):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL, "malformed CASE", node,
                    fallback=fallback, hint=_VALUE_HINT,
                )
            if operand is None:
                self._check_row_predicate(branch.this, scope, fallback)
            else:
                self._check_value_pair(operand, branch.this, scope, fallback, _VALUE_HINT)
            results.append(branch.args.get("true"))
        default = node.args.get("default")
        if isinstance(default, exp.Expr):
            results.append(default)
        found: str | None = None
        for result in results:
            result_type = self._check_value_expr(result, scope, fallback)
            if result_type is None:
                continue
            if found is None:
                found = result_type
            elif found != result_type:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"CASE results must share one type, got {found} and "
                    f"{result_type}",
                    result,
                    fallback=fallback,
                    hint=_VALUE_HINT,
                )
        return found

    def _check_concat(
        self, node: exp.DPipe, scope: dict[str, str], fallback: exp.Expr
    ) -> str:
        """``||`` joins TEXT; its result is text.

        A numeric operand is rejected rather than coerced. Postgres itself
        wants the cast, and an implicit one would have this compiler decide how
        to spell a number nobody asked it to format.
        """
        for side in (node.this, node.args.get("expression")):
            side_type = self._check_value_expr(side, scope, fallback)
            if side_type == "number":
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "'||' joins text, but one side is a number",
                    side if isinstance(side, exp.Expr) else None,
                    fallback=fallback,
                    hint="cast the number with ::text, quote it, or join text "
                    "columns: 'Audio (' || t.tags.language || ')'",
                )
            if side_type == "boolean":
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "'||' joins text, but one side is a boolean",
                    side if isinstance(side, exp.Expr) else None,
                    fallback=fallback,
                    hint="cast it with ::text, or fold it into a CASE: "
                    f"CASE WHEN t.{DISPOSITION_COLUMN}.{DISPOSITION_KEYS[0]} "
                    "THEN 'main' ELSE 'alt' END",
                )
        return "text"

    def _check_arithmetic(
        self, node: exp.Expr, scope: dict[str, str], fallback: exp.Expr
    ) -> str | None:
        """``+ - * /`` over two numbers; the result is a number.

        Precedence and grouping are sqlglot's -- `a + b * c` already arrives
        with the Mul nested under the Add -- so there is nothing to decide
        here but the operand types. NULL keeps the type open, exactly as it
        does in a CASE.
        """
        operator = _ARITHMETIC_NAMES[type(node)]
        left = self._check_numeric(node.this, operator, node, scope, fallback)
        right = self._check_numeric(
            node.args.get("expression"), operator, node, scope, fallback
        )
        return None if left is None or right is None else "number"

    def _check_numeric(
        self,
        operand: exp.Expr | None,
        operator: str,
        node: exp.Expr,
        scope: dict[str, str],
        fallback: exp.Expr,
    ) -> str | None:
        """One arithmetic operand: a number, or NULL (which keeps it open)."""
        operand_type = self._check_value_expr(operand, scope, fallback)
        if operand_type is None or operand_type == "number":
            return operand_type
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"{operator} needs numbers, but one side is {operand_type}",
            operand if isinstance(operand, exp.Expr) else node,
            fallback=fallback,
            hint=_VALUE_HINT,
        )

    def _check_cast(
        self, node: exp.Cast, scope: dict[str, str], fallback: exp.Expr
    ) -> str | None:
        """``x::text`` / ``CAST(x AS text)``: the bridge from a number into ``||``.

        Text is the only target v1 casts to. The other direction is already
        covered without a cast -- a bare ``:name`` variable parses as a number
        wherever a number is wanted -- so ``::int``/``::float`` would only add
        a second way to spell what already works.
        """
        to = node.args.get("to")
        target = to.this if isinstance(to, exp.DataType) else None
        if target != exp.DataType.Type.TEXT:
            spelled = str(getattr(target, "value", target or "")).lower()
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"cast to {spelled or 'that type'} is not supported",
                node,
                fallback=fallback,
                hint="::text is the only cast sqlmpeg has; it spells a number "
                "for '||', e.g. 'w=' || t.width::text",
            )
        value_type = self._check_value_expr(node.this, scope, fallback)
        return None if value_type is None else "text"

    def _check_value_pair(
        self,
        left: exp.Expr | None,
        right: exp.Expr | None,
        scope: dict[str, str],
        fallback: exp.Expr,
        hint: str,
    ) -> None:
        """Two compile-time operands of one comparison: their types must match."""
        left_type = self._check_value_expr(left, scope, fallback)
        right_type = self._check_value_expr(right, scope, fallback)
        if left_type is None or right_type is None or left_type == right_type:
            return
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"a comparison needs one type, got {left_type} and {right_type}",
            right if isinstance(right, exp.Expr) else None,
            fallback=fallback,
            hint=hint,
        )

    def _check_row_literal(
        self,
        node: exp.Expr | None,
        column: exp.Column,
        column_type: str,
        where: exp.Expr,
    ) -> None:
        """A row predicate's other operand: a literal of the column's own type."""
        alias = _ident_name(column.args.get("table"))
        name = _ident_name(column.this)
        self._check_literal_type(node, column_type, f"{alias}.{column_label(name)}", where)

    def _check_literal_type(
        self,
        node: exp.Expr | None,
        column_type: str,
        label: str,
        where: exp.Expr,
        *,
        hint: str = _ROW_WHERE_HINT,
    ) -> None:
        """A compile-time predicate's other operand: a literal of ``label``'s type.

        Shared by row-column predicates and subscript
        metadata predicates -- the whole point of both being
        static typing, not a probed-value coincidence: an absent metadata
        field makes the VALUE null, never the column untyped, so comparing a
        text column to a number is a mistake whatever the file turned out to
        contain. A negative number arrives as ``exp.Neg`` wrapping the
        literal, exactly as it does for a positional filter argument.
        """
        node = _unwrap_paren(node) if isinstance(node, exp.Expr) else None
        if isinstance(node, exp.Neg) and isinstance(node.this, exp.Expr):
            node = _unwrap_paren(node.this)
        got = _literal_type(node)
        if got is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{label}' can only be compared against a literal",
                node,
                fallback=where,
                hint=hint,
            )
        if got == column_type:
            return
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"'{label}' is {column_type}, but the comparison value is "
            f"{_LITERAL_NAMES[got]}",
            node,
            fallback=where,
            hint=f"compare '{label}' against {_WANTED_LITERALS[column_type]}",
        )

    # -- ORDER BY over track-row columns -------------------------

    def _check_order(
        self, order: exp.Order, scope: dict[str, str], select: exp.Select
    ) -> None:
        """Every sort key is a track-row metadata column, and nothing else.

        Reaching here at all means the branch has a row source
        (:func:`_has_row_source` decides the arg whitelist), so this is the
        second half of the carve-out: the carve-out is for track-row METADATA
        columns, not for the query that happens to contain them. Sorting
        frames, a CTE's streams, or a time column is still
        NO_STREAMING_EQUIVALENT.
        """
        _check_query_args(order, frozenset({"expressions"}), "ORDER BY")
        expressions = order.expressions
        if not expressions:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "ORDER BY has no sort key",
                fallback=order,
                hint=_ROW_ORDER_HINT,
            )
        for ordered in expressions:
            if not isinstance(ordered, exp.Ordered):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "malformed ORDER BY",
                    ordered if isinstance(ordered, exp.Expr) else None,
                    fallback=order,
                    hint=_ROW_ORDER_HINT,
                )
            _check_query_args(
                ordered, frozenset({"this", "desc", "nulls_first"}), "ORDER BY"
            )
            key = ordered.this
            key = _unwrap_paren(key) if isinstance(key, exp.Expr) else None
            if not isinstance(key, exp.Column) or isinstance(key.this, exp.Star):
                raise _error(
                    ErrorCode.NO_STREAMING_EQUIVALENT,
                    "ORDER BY has no streaming equivalent",
                    ordered,
                    fallback=order,
                    hint="only track-row metadata columns can be sorted; "
                    + _ROW_ORDER_HINT,
                )
            table_node = key.args.get("table")
            alias = _ident_name(table_node) if table_node is not None else ""
            if scope.get(alias) != "row":
                raise _error(
                    ErrorCode.NO_STREAMING_EQUIVALENT,
                    "ORDER BY has no streaming equivalent",
                    key,
                    fallback=order,
                    hint="only track-row metadata columns can be sorted; "
                    + _ROW_ORDER_HINT,
                )
            if self._check_row_column(key, alias, order) == "stream":
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"'{alias}' is a stream, and streams have no order to "
                    "sort by",
                    key,
                    fallback=order,
                    hint=_ROW_ORDER_HINT,
                )

    def _flatten_and(
        self, node: exp.Expr | None, out: list[exp.Expr], select: exp.Select
    ) -> None:
        if node is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL, "empty WHERE clause", fallback=select, hint=_WHERE_HINT
            )
        while isinstance(node, exp.Paren):
            inner = node.this
            if not isinstance(inner, exp.Expr):
                break
            node = inner
        if isinstance(node, exp.And):
            self._flatten_and(node.this, out, select)
            self._flatten_and(node.args.get("expression"), out, select)
            return
        out.append(node)


def resolve(tree: exp.Expression) -> Resolved:
    """Validate the AST against the v0 dialect and build the input table.

    Raises ``SqlmpegError`` — and nothing else — on every rejection.
    """
    try:
        return _Resolver().run(tree)
    except SqlmpegError:
        raise
    except Exception as err:  # backstop: guardrail #7, no panics on user input
        raise SqlmpegError(
            ErrorCode.INTERNAL,
            f"internal error while resolving ({err.__class__.__name__}: {err})",
            line=1,
            col=1,
            hint="please report this query as a bug",
        ) from err
