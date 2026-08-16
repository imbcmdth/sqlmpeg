"""Parse + resolve passes for sqlmpeg.

``parse`` turns SQL text into a sqlglot AST (always ``read="postgres"``,
guardrail #2). ``resolve`` validates that the AST stays inside the v0 dialect
surface, builds the alias/CTE table, and assigns ffmpeg input indices.

Neither function ever lets a sqlglot (or any other) exception escape: every
rejection is a :class:`sqlmpeg.errors.SqlmpegError` with a typed code and, where
sqlglot gives us token positions, a line/col anchor.

Notes for downstream passes (lower):

* Input indices are keyed by ALIAS, not by path. Two aliases over the same file
  produce two ``-i`` entries (the README PiP example is exactly this), so
  ``input_paths`` may contain duplicates.
* ``Resolved.select`` is the top-level query and may be an ``exp.Union`` when the
  query is a ``UNION ALL``. Use ``Resolved.branches`` (or :func:`union_branches`
  for CTE bodies) to get the flat list of branch selects.
* Identifier names are normalized the Postgres way: unquoted identifiers are
  lowercased, quoted ones are kept verbatim. ``sources`` keys are normalized.
* The SELECT list may hold MORE THAN ONE projection (RFC-001: one column = one
  output stream). ``SINGLE_OUTPUT_ONLY`` is retired; the parser only rejects an
  empty projection list.
* A statement may be wrapped in ``COPY (<query>) TO '<path>' WITH (<options>)``
  (RFC-002). The COPY is peeled off into a ``Resolved.sinks`` entry and the
  query it wraps goes through the EXACT same validation a bare SELECT does; a
  bare SELECT leaves ``sinks`` empty. Only the shape is checked here — option
  NAMES and VALUES are validated against ``sqlmpeg.sink.SINK_OPTIONS`` by lower.

* Input text may be a SCRIPT: ``CREATE VIEW <name> AS <query>;``* followed by
  ``COPY ...;``+ (RFC-006). See :func:`_statements` and :meth:`_Resolver._view`
  for the VERIFIED sqlglot shapes. A view is to STATEMENTS what a CTE is to
  branches, so it is stored as one: ``Resolved.ctes`` is the flat, ordered
  binding table (views AND CTEs, in definition order) that lower walks, and
  ``Resolved.views`` names the subset that came from a ``CREATE VIEW``.
* Named function arguments (``gblur(a.frame, sigma => 5)``, RFC-003) are native
  Postgres syntax: sqlglot parses each into an ``exp.Kwarg(this=Var(name),
  expression=<value>)`` inside the call's ``expressions`` list. The resolver only
  checks their SHAPE — named args must TRAIL the positional ones and may not
  repeat — because which options exist is a property of the installed ffmpeg,
  which only lower (and its registry) knows. Names are kept VERBATIM, not folded:
  ffmpeg option names are case-sensitive (``gblur``'s ``sigmaV``).

* ``SELECT *`` / ``SELECT <alias>.*`` (RFC-004) are accepted in PROJECTION
  position only. VERIFIED shapes under sqlglot 30.17 ``read="postgres"``:
  ``SELECT *`` puts a bare ``exp.Star()`` in the projection list, while
  ``SELECT a.*`` puts an ``exp.Column(this=Star(), table=Identifier(a))`` —
  two different shapes, both recognized by :func:`star_qualifier`. Everything
  else a star can appear in (``scale(a.*, 0.5)``, ``a.*[1]``, ``* AS x``,
  ``count(*)``, a star in WHERE) is still rejected: which streams a star
  stands for is only knowable after probing, so it can only ever BE a column,
  never feed one. The resolver checks the qualifier is a known alias; lower
  does the expansion (it is the pass that has the probes).

* The ``ffmpeg.<filter>(...)`` namespace (plan 038) needs nothing from this
  pass but a reserved name. VERIFIED under sqlglot 30.17 ``read="postgres"``:
  a qualified call parses as ``exp.Dot(this=Identifier(ffmpeg),
  expression=exp.Anonymous(this=<filter>, expressions=[...]))`` for EVERY
  filter name, collision victims included — the builtin special-form grammars
  (``OVERLAY ... PLACING``, ``TRIM``, ``FORMAT``, ...) key on a bare name, so
  qualifying it bypasses them completely, and ``=>`` arguments land in the
  ``Anonymous`` as ordinary ``exp.Kwarg``s. The resolver therefore only
  RESERVES the name ``ffmpeg`` (:meth:`_Resolver._reserve`) so no alias or CTE
  can shadow the namespace; lower does the resolution.

* ``FROM ffmpeg.<source>(<named options>) alias`` (RFC-005 §1, plan 042) is the
  SAME namespace in TABLE position, and it is a DIFFERENT sqlglot shape from
  the call one above — a ``Dot`` never appears. VERIFIED under sqlglot 30.17
  ``read="postgres"``, see the table in :meth:`_Resolver._add_source`:
  ``FROM ffmpeg.testsrc(duration => 2) t`` parses as
  ``exp.Table(this=Anonymous(testsrc, [Kwarg...]), db=Identifier(ffmpeg),
  alias=TableAlias(t))`` — the qualifier lands in the Table's ``db`` slot
  (``catalog`` too, for a three-part name), NOT wrapped around the call. The
  parenthesis-less ``FROM ffmpeg.testsrc t`` is the same Table with an
  ``Identifier`` rather than an ``Anonymous`` ``this``, which is how that form
  is told apart and rejected with a hint. A generated source takes NO ffmpeg
  input index — it is a zero-input filter node, there is no ``-i`` — so it
  never enters ``input_paths``/``sources``; its record goes into
  ``Resolved.source_filters`` instead. Which source names exist, and which
  options they take, is the installed ffmpeg's business: this pass checks the
  SHAPE only (alias mandatory, arguments named-only) and lower does the rest.

* Stream subscripts (``a.video[1]``) arrive as ``exp.Bracket`` wrapping the
  ``exp.Column``. **sqlglot rebases the index at parse time**: under
  ``read="postgres"`` (``INDEX_OFFSET = 1``) it rewrites the single subscript
  expression to ``expr - 1`` whenever it annotates it as an integer type, so
  ``a.video[1]`` holds ``Literal(0)`` and ``a.video[0]`` holds ``Neg(Literal(1))``.
  Never read ``Bracket.expressions[0]`` directly — use :func:`subscript_index`,
  which undoes the rebase and hands back the 1-based number the user wrote.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, SqlglotError

from sqlmpeg.errors import ErrorCode, SqlmpegError

__all__ = [
    "FILTER_NAMESPACE",
    "RawInputOption",
    "RawSink",
    "RawSinkOption",
    "RawSource",
    "RawSourceOption",
    "Resolved",
    "kwarg_name",
    "parse",
    "resolve",
    "star_qualifier",
    "subscript_index",
    "union_branches",
]

# The reserved qualifier of the raw-filter namespace: `ffmpeg.<filter>(...)`
# (plan 038). It is not an alias and never resolves against the FROM clause, so
# no alias or CTE may claim the name (`_Resolver._reserve`).
FILTER_NAMESPACE = "ffmpeg"

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

# The only column names an INPUT alias exposes. A CTE alias exposes whatever its
# body named with AS, so the whitelist does not apply there (lower checks those).
# RFC-004 added `subtitle` and `data`: same array/subscript/splat surface as
# video/audio, passthrough-only downstream (lower enforces that half).
_INPUT_COLUMNS = frozenset({"frame", "video", "audio", "subtitle", "data", "t"})

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
    "e.g. gblur(a.frame, sigma => 5)"
)


# ---------------------------------------------------------------------------
# position helpers
# ---------------------------------------------------------------------------


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


def _ident_name(node: exp.Expr | None) -> str:
    """Postgres identifier folding: unquoted -> lowercase, quoted -> verbatim."""
    if node is None:
        return ""
    if isinstance(node, exp.Identifier):
        return node.name if node.args.get("quoted") else node.name.lower()
    return str(node.name).lower()


# ---------------------------------------------------------------------------
# subscripts
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# stars (RFC-004)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# named arguments (RFC-003)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# time predicates: WHERE <alias>.t ... (plan 039, open-ended windows)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------


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

    ONE statement comes back as itself; a SCRIPT (RFC-006) comes back as an
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
    except Exception as err:  # sqlglot bug / recursion / anything at all
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


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RawSinkOption:
    """One ``WITH (name value)`` pair, still as sqlglot nodes (RFC-002).

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
    """``COPY (query) TO 'path' WITH (...)`` as the parser saw it (RFC-002).

    Shape only: the path is known to be a single string literal and the option
    names to be unique, but nothing here has been checked against the option
    table yet. ``sqlmpeg.lower`` turns this into ``sqlmpeg.ir.Sink``.

    A sink carries the query it wraps (RFC-006): a script has one COPY per
    OUTPUT GROUP, and each group is a whole query of its own. ``query`` and
    ``branches`` are the same pair ``Resolved.select``/``Resolved.branches``
    are for the single-sink case, fully validated — for a one-COPY statement
    they ARE that pair.
    """

    path: str
    path_node: exp.Expr
    query: QueryExpr
    branches: tuple[exp.Select, ...]
    options: tuple[RawSinkOption, ...] = ()


@dataclass(frozen=True)
class RawInputOption:
    """One ``input('path', name => value)`` trailing named argument (RFC-005, plan 041).

    Shape only, mirroring ``RawSinkOption``: `lower` turns `value` into a
    python scalar and checks it against ``sqlmpeg.inputs.INPUT_OPTIONS``; the
    parser deliberately knows nothing about which options exist.

    `name` is kept VERBATIM (see :func:`kwarg_name`) -- input options reuse
    the ``name => value`` named-argument syntax RFC-003 gives every call,
    NOT COPY's folded ``WITH (name value)`` one, so case matters exactly like
    a dynamic filter option's name does.

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
    """One ``ffmpeg.<source>(name => value)`` option (RFC-005 §1, plan 042).

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
    """``FROM ffmpeg.<source>(<named options>) alias`` (RFC-005 §1, plan 042).

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


@dataclass
class Resolved:
    """Output of the resolve pass — the validated query plus its input table."""

    select: QueryExpr
    """The query being compiled, CTEs still attached; ``exp.Union`` for UNION ALL.

    For a script this is ``sinks[0].query``: lowering is single-sink until
    plan 046, which retires this field in favour of walking ``sinks``.
    """

    input_paths: list[str]
    """``-i`` order; the list index is the ffmpeg input index. May repeat paths."""

    sources: dict[str, int]
    """Input alias -> index into ``input_paths``. One entry per distinct alias."""

    ctes: dict[str, QueryExpr] = field(default_factory=dict)
    """Named query binding -> its query, in DEFINITION order.

    Both kinds of binding live here, because a view IS a CTE to everything
    downstream (RFC-006): a script's ``CREATE VIEW``s, the CTEs of their
    bodies, and the CTEs of each COPY's own ``WITH``, in the order they were
    written. Lower walks this dict once and binds each entry by name, which is
    why the order matters and why views need no machinery of their own.
    """

    branches: list[exp.Select] = field(default_factory=list)
    """``select`` flattened into UNION ALL branches; a single element if not a union."""

    sinks: list[RawSink] = field(default_factory=list)
    """One entry per ``COPY``, in script order; EMPTY for a bare SELECT.

    RFC-006 replaced the single ``sink`` field with this list so the parser
    contract is already multi-sink. Resolve accepts any number; ``lower``
    TEMPORARILY rejects more than one until plan 046 builds the multi-sink
    core.
    """

    views: dict[str, QueryExpr] = field(default_factory=dict)
    """``CREATE VIEW`` name -> its body, in definition order.

    A strict subset of ``ctes`` (same objects): this is the introspection
    view, ``ctes`` is the one lower walks.
    """

    input_options: dict[str, tuple[RawInputOption, ...]] = field(default_factory=dict)
    """Input alias -> its trailing named options (RFC-005). Only aliases that
    wrote at least one option get an entry -- absent means none."""

    source_filters: dict[str, RawSource] = field(default_factory=dict)
    """``FROM ffmpeg.<source>(...) alias`` records, keyed by alias, in FROM
    order across the whole query (RFC-005 §1). Disjoint from ``sources``: a
    generated source has no ``-i`` and therefore no input index."""


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


# ---------------------------------------------------------------------------
# COPY ... TO ... WITH (...)  — the sink wrapper (RFC-002)
# ---------------------------------------------------------------------------


def _sink(copy: exp.Copy) -> tuple[str, exp.Expr, tuple[RawSinkOption, ...], exp.Expr]:
    """Validate a top-level COPY into ``(path, path node, options, wrapped query)``.

    The pieces rather than a :class:`RawSink`: a sink also carries its
    VALIDATED query (RFC-006), and that validation is the resolver's job, so
    the record is assembled there once the query has been through it.

    Shape only — the option table is lower's business. VERIFIED shapes under
    sqlglot 30.17 ``read="postgres"``:

    * ``kind`` is a plain bool: True for ``COPY ... FROM`` (loading), False for
      ``COPY ... TO``. It is NOT an expression, so it has no position.
    * ``files`` is a list — ``TO 'a', 'b'`` gives two entries, and ``TO STDOUT``
      / ``TO x`` / ``TO PROGRAM 'cat'`` give an ``exp.Identifier`` rather than a
      ``Literal``. All of those are rejected here.
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
    if not (isinstance(target, exp.Literal) and target.is_string):
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            "COPY target must be a single-quoted file path",
            target if isinstance(target, exp.Expr) else None,
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

    return str(target.this), target, _sink_options(copy, target), query


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

    Shared by ``input('path', ...)`` (plan 041) and
    ``ffmpeg.<source>(...)`` (plan 042): both take named arguments ONLY past
    a fixed positional prefix (one path literal for ``input``, none at all for
    a source), so a bare positional among `rest` is rejected outright rather
    than by RFC-003's softer "positional arguments must come before named
    arguments" rule. `positional_message` is what that rejection says, and it
    is the only difference between the two callers.

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

    # -- entry point ------------------------------------------------------

    def run(self, tree: exp.Expr) -> Resolved:
        """Resolve one statement, or a whole script (RFC-006).

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
                # RFC-002: peel the COPY wrapper off; what it wraps is
                # validated exactly like a bare SELECT from here on.
                path, path_node, options, wrapped = _sink(statement)
                query, query_branches = self._resolve_query(wrapped)
                sinks.append(
                    RawSink(
                        path=path,
                        path_node=path_node,
                        query=query,
                        branches=tuple(query_branches),
                        options=options,
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
            select, branches = self._resolve_query(statement)

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
        )

    def _resolve_query(self, node: exp.Expr) -> tuple[QueryExpr, list[exp.Select]]:
        """Validate one whole query — a view body, a COPY's, or a bare SELECT.

        Its own ``WITH`` is resolved into the shared, ordered binding table
        FIRST, so the names it defines are visible to it and to everything
        written after it, and nothing else.
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
        visible = set(self.ctes)
        for branch in branches:
            self._validate_select(branch, visible)
        return query, branches

    # -- CREATE VIEW (RFC-006) --------------------------------------------

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
        # A view BODY is a full query (RFC-006), so unlike a CTE body it may
        # carry its own WITH: the nested-WITH rejection is CTE-body-only
        # (`_resolve_ctes`) and branch-level (`_collect_branches`), and neither
        # fires on a statement's own top-level one.
        query, _ = self._resolve_query(body)
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
        """A view nobody reads is a typo (RFC-006), anchored on its CREATE.

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
            if alias.args.get("columns"):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "CTE column lists are not supported",
                    alias,
                    hint="name the CTE's columns with AS inside its SELECT",
                )
            name = _ident_name(alias.this)
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
                self._validate_select(branch, visible)
            self.ctes[name] = body

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
        if name in self.ctes or name in self.sources or name in self.source_filters:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"duplicate name '{name}'",
                node,
                hint="view, CTE and alias names share one flat namespace and "
                "must be unique across the whole script",
            )

    # -- selects ----------------------------------------------------------

    def _validate_select(self, select: exp.Select, visible: set[str]) -> None:
        _check_query_args(select, _SELECT_ALLOWED, "SELECT")

        # RFC-001: the SELECT list IS the output stream list, so any number of
        # projections is legal. Only an empty list is not.
        projections = select.expressions
        if not projections:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL, "SELECT has no output column", fallback=select
            )
        for projection in projections:
            # A star projection carries nothing but the star and its qualifier,
            # so there is no expression to check inside it -- and running the
            # generic walk would hit the star rejection it is exempt from.
            if star_qualifier(projection) is None:
                self._check_expression(projection, select)

        where = select.args.get("where")
        if isinstance(where, exp.Where):
            self._check_expression(where, select)

        scope = self._collect_scope(select, visible)
        for projection in projections:
            self._check_columns(projection, scope, select)
        if isinstance(where, exp.Where):
            self._check_where(where, scope, select)

    def _check_expression(self, node: exp.Expr, select: exp.Select) -> None:
        """Reject constructs no streaming filtergraph can express."""
        for sub in node.walk():
            if isinstance(sub, exp.AggFunc):
                raise _error(
                    ErrorCode.NO_STREAMING_EQUIVALENT,
                    f"aggregate function {sub.sql_name().lower()}() has no "
                    "streaming equivalent",
                    sub,
                    fallback=select,
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
                # RFC-004 accepts a star only as a whole projection, which
                # `_validate_select` peels off before calling this; anything
                # that reaches here has one nested inside an expression, where
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

        Shape only (RFC-003): whether the option exists, and what type it takes,
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
        self._add_table(from_.this, scope, visible)

        joins = select.args.get("joins") or []
        for join in joins:
            if not isinstance(join, exp.Join):
                raise _error(ErrorCode.UNSUPPORTED_SQL, "malformed FROM clause", fallback=select)
            for key in ("on", "using", "side", "kind", "method", "match_condition"):
                value = join.args.get(key)
                if value:
                    raise _error(
                        ErrorCode.UNSUPPORTED_SQL,
                        "explicit JOIN syntax is not supported",
                        _first_expression(value),
                        fallback=join,
                        hint="use a comma cross-join: FROM a, b",
                    )
            self._add_table(join.this, scope, visible)
        return scope

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
        # `FROM ffmpeg.<source>(...) alias` (plan 042) is the ONE qualified
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
            if alias_node is not None:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"aliasing CTE '{name}' is not supported",
                    alias_node,
                    fallback=table,
                    hint="reference the CTE by its own name",
                )
            if name not in visible:
                raise _error(
                    ErrorCode.UNKNOWN_ALIAS,
                    f"unknown table '{name}'",
                    inner,
                    fallback=table,
                    hint=self._known_hint(visible),
                )
            if name in scope:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"duplicate name '{name}'",
                    inner,
                    fallback=table,
                    hint="a name can appear only once per FROM clause; to consume "
                    "it twice, reference <name>.frame twice — reuse is automatic",
                )
            # Feeds the unused-VIEW check (RFC-006). CTE names land here too;
            # only views are required to be read.
            self.used.add(name)
            scope[name] = "cte"
            return
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            "only input('path') and CTE names are allowed in FROM",
            table,
        )

    def _add_input(
        self,
        table: exp.Table,
        func: exp.Anonymous,
        alias_node: exp.Expr | None,
        scope: dict[str, str],
    ) -> None:
        func_name = str(func.this).lower()
        if func_name != "input":
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"unsupported table function {func_name}()",
                func,
                fallback=table,
                hint="the only table function is input('path')",
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
        """``FROM ffmpeg.<source>(<named options>) alias`` (RFC-005 §1, plan 042).

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
        # not an `-i` (RFC-005 §1). `input_paths`/`sources` stay untouched.
        self.source_filters[alias] = RawSource(
            alias=alias, name=name, options=options, call_node=inner
        )
        scope[alias] = "source"

    def _known_hint(self, names: set[str] | dict[str, str]) -> str:
        known = ", ".join(sorted(names))
        return f"known names: {known}" if known else "no aliases are in scope"

    # -- columns / WHERE --------------------------------------------------

    def _check_columns(
        self, node: exp.Expr, scope: dict[str, str], select: exp.Select
    ) -> None:
        for sub in node.walk():
            if not isinstance(sub, exp.Column):
                continue
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
                    hint="qualify the column with its alias, e.g. a.frame",
                )
            name = _ident_name(table_node)
            kind = scope.get(name)
            if kind is None:
                # `ffmpeg.<x>` with no parentheses is a COLUMN, not a namespaced
                # call: the namespace only exists in call position, so say so
                # instead of listing aliases the user never meant.
                hint = (
                    f"{FILTER_NAMESPACE}.<filter>(...) is a call, not a column; "
                    f"write {FILTER_NAMESPACE}.{sub.name}(<stream>, "
                    "<option> => <value>) or select a real alias's stream"
                    if name == FILTER_NAMESPACE
                    else self._known_hint(scope)
                )
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
            if kind == "input" and _ident_name(sub.this) not in _INPUT_COLUMNS:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"unknown column '{name}.{sub.name}'",
                    sub,
                    fallback=select,
                    hint=f"an input exposes {', '.join(sorted(_INPUT_COLUMNS))}",
                )

    def _check_where(
        self, where: exp.Where, scope: dict[str, str], select: exp.Select
    ) -> None:
        conjuncts: list[exp.Expr] = []
        self._flatten_and(where.this, conjuncts, select)

        # alias -> {"low": <literal>, "high": <literal>}, accumulated across
        # every conjunct so a second bound of the same kind for one alias is
        # rejected (mirrors the old one-BETWEEN rule) whether it repeats
        # within one BETWEEN, across two inequalities, or a BETWEEN plus an
        # inequality -- and so the closed pair, once both are known, can be
        # checked for an empty window.
        bounds: dict[str, dict[str, exp.Literal]] = {}
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
                if not (isinstance(bound, exp.Literal) and not bound.is_string):
                    raise _error(
                        ErrorCode.UNSUPPORTED_SQL,
                        "time bounds must be numeric literals (seconds)",
                        bound,
                        fallback=where,
                        hint=_WHERE_HINT,
                    )
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
            if low_literal is None or high_literal is None:
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
