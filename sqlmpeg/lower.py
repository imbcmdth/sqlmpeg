"""Lower pass: a resolved query becomes an IR :class:`~sqlmpeg.ir.Graph`.

This is pass 2 of the compiler (see "Architecture" in sqlmpeg-project.md). It
assumes :func:`sqlmpeg.parser.resolve` already accepted the query, so every
rejection raised here is either a check resolve deliberately left to lowering
(CTE column names, function names, argument types, probed stream bounds) or a
defensive re-check.

RFC-001 (stream-aware) shapes this pass: the top-level SELECT list IS the
output stream list, and every value flowing through lowering is a *typed*
stream (``video`` or ``audio``), never an untyped "frame".

What lowering does, in order:

* CTE bodies lower first, in definition order, into the *same* graph. A CTE
  records a list of ``(name, type, ref)`` columns — its SELECT list — and
  ``FROM <cte>`` later exposes those columns by their ``AS`` names.
* Inside a branch, ``FROM`` builds a typed environment: an ``input()`` alias
  exposes per-type stream access (``a.video[1]`` -> ``"src:a:v:0"``; SQL
  subscripts are 1-based, IR indices 0-based), a CTE alias exposes its
  recorded columns.
* ``<alias>.frame`` is sugar for ``<alias>.video[1]`` (v0 compat). A single
  unnamed video column of a CTE is likewise reachable as ``<cte>.frame``.
* ``WHERE a.t BETWEEN x AND y`` records a per-alias time range. The trim is
  spliced lazily, the first time a stream of that alias is consumed, and
  memoized per stream, so every consumer of the same stream shares one
  ``trim``+``setpts`` (video) / ``atrim``+``asetpts`` (audio) pair.
* Each projection lowers bottom-up to one :class:`~sqlmpeg.ir.Output`. A
  stdlib call type-checks its arguments against
  ``stdlib.FUNCTIONS[name].variants`` (kinds are ``video``/``audio``/``num``/
  ``str``) and then delegates node creation to the spec's ``expand``
  (guardrail #4: no per-function lowering logic lives here).
* A UNION ALL (top level or inside a CTE) lowers each branch and joins them
  with one ``concat`` node. Branch column counts, types and order must match
  exactly (``CONCAT_MISMATCH``); concat inputs interleave per ffmpeg's segment
  contract — all of segment 1's videos, then its audios, then segment 2's, ...
  — and its output pads are ``["video"]*v + ["audio"]*a``, mapped back to the
  branch's own column order.

Probing (``probes``, keyed by alias) only ever ADDS validation: an explicit
subscript lowers to the same ref whether or not the input could be probed, but
a probed input bounds-checks it (``STREAM_NOT_FOUND``). An output whose ref is
a raw source stream of a probed input also carries that stream's language/title
tags into ``Output.metadata`` (an ffmpeg-stamped ``language=und`` carries no
information and is dropped). Provenance through filter chains is plan 020's.

Not yet supported here (plan 020, broadcasting): bare ``a.video`` / ``a.audio``
arrays — splatted into a SELECT list, passed to a function, or produced by a
CTE column — and subscripting a computed (CTE) column.

Node ids are ``n1, n2, ...`` in creation order across the whole graph, minted
by :class:`_NodeFactory`, the :class:`sqlmpeg.stdlib.ExpandCtx` implementation.

sqlglot notes that matter here
------------------------------
* Postgres has a builtin ``OVERLAY``, so ``overlay(a, b, x, y)`` parses to
  :class:`sqlglot.exp.Overlay` with *named* args (``this``, ``expression``,
  ``from_``, ``for_``) rather than to ``exp.Anonymous``. :func:`_call_parts`
  normalizes it back to four positional arguments.
* A subscript arrives as ``exp.Bracket`` wrapping the ``exp.Column``, and
  sqlglot REBASES the index at parse time (postgres ``INDEX_OFFSET = 1``), so
  ``a.video[1]`` holds ``Literal(0)``. Never read ``Bracket.expressions``
  here: :func:`sqlmpeg.parser.subscript_index` undoes the rebase and returns
  the 1-based number the user wrote.
* Neither ``Bracket`` nor ``Column`` carries a token position of its own;
  ``_pos`` walks the subtree and anchors on the qualifier identifier, which is
  the best line/col a stream error can get.
* ``exp.Literal.to_py()`` returns ``decimal.Decimal`` for non-integer numbers,
  which neither ``emit`` nor JSON can render, so numeric literals are coerced
  to ``int``/``float`` here. ``-1.5`` parses as ``exp.Neg(Literal)``.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlglot import exp

from sqlmpeg.errors import ErrorCode, SqlmpegError
from sqlmpeg.ir import FrameRef, Graph, Node, Output, StreamType, is_src, src_parts
from sqlmpeg.parser import Resolved, _pos, subscript_index, union_branches
from sqlmpeg.parser import _ident_name as _fold
from sqlmpeg.probe import ProbeResult
from sqlmpeg.stdlib import FUNCTIONS, ExpandCtx, Param, signatures

__all__ = ["lower"]

_FRAME_COLUMN = "frame"
_TIME_COLUMN = "t"

# The two array-typed pseudo-columns an input exposes, and their element type.
_ARRAY_COLUMNS: dict[str, StreamType] = {"video": "video", "audio": "audio"}

_TYPE_MARKERS: dict[StreamType, str] = {"video": "v", "audio": "a"}

# Kind label used in UDF_ARG_TYPE "got" lists for anything that is neither a
# literal nor a stream-typed subexpression (e.g. `1 + 2`, NULL, TRUE).
_EXPR_KIND = "expr"

# Provenance tags copied onto a passthrough Output. "und" is what mp4 muxers
# stamp on untagged streams; it carries no information, so it is not copied.
_PROVENANCE_KEYS = ("language", "title")
_UNDEFINED_LANGUAGE = "und"

_TIME_HINT = "<alias>.t is only usable as WHERE <alias>.t BETWEEN <start> AND <end>"
_ARG_HINT = "arguments are stream expressions or literals, in the order shown"
_STREAM_HINT = "a SELECT column must be a stream, e.g. a.video[1] or scale(a.frame, 0.5)"
_SUBSCRIPT_HINT = "stream subscripts are 1-based: a.video[1] is the first video stream"
_BROADCAST_HINT = (
    "broadcasting over a whole stream array is coming (plan 020); "
    "for now subscript a single stream, e.g. a.audio[1]"
)


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


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


def _describe(node: exp.Expr) -> str:
    """Short human name for an expression that cannot produce a stream."""
    if isinstance(node, exp.Literal):
        return "a string literal" if node.is_string else "a numeric literal"
    if isinstance(node, exp.Neg):
        return "a numeric literal"
    if isinstance(node, exp.Null):
        return "NULL"
    if isinstance(node, exp.Boolean):
        return "a boolean literal"
    return f"a {node.__class__.__name__.upper()} expression"


# ---------------------------------------------------------------------------
# small AST helpers
# ---------------------------------------------------------------------------


def _unwrap(node: exp.Expr) -> exp.Expr:
    """Strip projection aliases and redundant parentheses."""
    while True:
        if isinstance(node, exp.Alias | exp.Paren):
            inner = node.this
            if isinstance(inner, exp.Expr):
                node = inner
                continue
        return node


def _projection_name(node: exp.Expr) -> str | None:
    """The ``AS`` name of a projection, folded Postgres-style, else None."""
    if not isinstance(node, exp.Alias):
        return None
    alias = node.args.get("alias")
    if not isinstance(alias, exp.Expr):
        return None
    name = _fold(alias)
    return name or None


def _flatten_and(node: exp.Expr | None) -> list[exp.Expr]:
    """Flatten an AND tree into its conjuncts, left to right."""
    out: list[exp.Expr] = []
    stack: list[exp.Expr | None] = [node]
    while stack:
        current = stack.pop(0)
        if current is None:
            continue
        while isinstance(current, exp.Paren) and isinstance(current.this, exp.Expr):
            current = current.this
        if isinstance(current, exp.And):
            expression = current.args.get("expression")
            stack.insert(0, expression if isinstance(expression, exp.Expr) else None)
            stack.insert(0, current.this if isinstance(current.this, exp.Expr) else None)
            continue
        out.append(current)
    return out


def _call_parts(node: exp.Expr) -> tuple[str, list[exp.Expr]] | None:
    """``(name, positional args)`` if `node` is a function call, else None.

    ``exp.Overlay`` is normalized back to the four positional arguments the
    SQL surface uses; sqlglot parks them under named keys because Postgres
    spells the builtin ``OVERLAY(x PLACING y FROM n FOR m)``.
    """
    if isinstance(node, exp.Overlay):
        named = [
            node.this,
            node.args.get("expression"),
            node.args.get("from_"),
            node.args.get("for_"),
        ]
        return "overlay", [arg for arg in named if isinstance(arg, exp.Expr)]
    if isinstance(node, exp.Anonymous):
        return str(node.this), [arg for arg in node.expressions if isinstance(arg, exp.Expr)]
    if isinstance(node, exp.Func):
        name = node.sql_name().lower()
        args = [arg for arg in node.expressions if isinstance(arg, exp.Expr)]
        return name, args
    return None


def _unknown_function_hint(name: str) -> str:
    matches = difflib.get_close_matches(name, sorted(FUNCTIONS), n=1, cutoff=0.6)
    if matches:
        return f"did you mean {matches[0]}()?"
    return "known functions: " + ", ".join(sorted(FUNCTIONS))


# ---------------------------------------------------------------------------
# literal coercion
# ---------------------------------------------------------------------------


def _number(node: exp.Expr, code: ErrorCode = ErrorCode.UDF_ARG_TYPE) -> int | float:
    """Python value of a numeric literal, negation included.

    ``to_py()`` hands back ``decimal.Decimal`` for non-integers (the IR only
    carries JSON/ffmpeg-renderable scalars, so that is narrowed to float here)
    and raises ``ValueError`` on malformed literals sqlglot still tokenized as
    numbers, e.g. ``1e`` — which must surface as a typed rejection, not a panic.
    """
    node = _unwrap(node)
    sign = 1
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Expr):
        sign = -1
        node = node.this
    if not isinstance(node, exp.Literal) or node.is_string:
        raise _error(code, "expected a numeric literal", node)
    try:
        value = node.to_py()
        if isinstance(value, bool):
            raise ValueError(value)
        return sign * value if isinstance(value, int) else sign * float(value)
    except (ArithmeticError, TypeError, ValueError):
        raise _error(code, f"could not read {str(node.this)!r} as a number", node) from None


def _string(node: exp.Expr) -> str:
    node = _unwrap(node)
    if not isinstance(node, exp.Literal) or not node.is_string:
        raise _error(ErrorCode.UDF_ARG_TYPE, "expected a string literal", node)
    return str(node.this)


# ---------------------------------------------------------------------------
# typed values, bindings, per-branch environment
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Stream:
    """One typed stream value: an IR ref plus the pad type it carries."""

    ref: FrameRef
    type: StreamType


@dataclass(frozen=True)
class _Column:
    """One SELECT column of a branch (or of a CTE body): its name and stream."""

    name: str | None
    stream: _Stream


@dataclass(frozen=True)
class _InputBinding:
    """``FROM input('x.mp4') a`` — exposes ``a.video[k]`` / ``a.audio[k]``."""

    alias: str


@dataclass(frozen=True)
class _CteBinding:
    """``FROM <cte>`` — exposes the columns the CTE's SELECT list named."""

    name: str
    columns: tuple[_Column, ...]


_Binding = _InputBinding | _CteBinding


@dataclass
class _Env:
    """Everything one SELECT branch resolves names against."""

    bindings: dict[str, _Binding] = field(default_factory=dict)
    trims: dict[str, tuple[int | float, int | float]] = field(default_factory=dict)
    # base stream ref -> its trimmed ref, so one trim is shared by every
    # consumer of that stream inside this branch.
    trimmed: dict[FrameRef, FrameRef] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# ExpandCtx
# ---------------------------------------------------------------------------


class _NodeFactory:
    """:class:`sqlmpeg.stdlib.ExpandCtx` impl: mints ``n1, n2, ...`` into a graph."""

    def __init__(self, graph: Graph) -> None:
        self._graph = graph
        self._counter = 0

    def node(
        self,
        filter: str,
        args: dict[str, object],
        inputs: list[FrameRef],
        outputs: list[StreamType],
    ) -> FrameRef:
        self._counter += 1
        node_id = f"n{self._counter}"
        self._graph.nodes[node_id] = Node(
            id=node_id,
            filter=filter,
            args=dict(args),
            inputs=list(inputs),
            outputs=list(outputs),
        )
        return node_id


# ---------------------------------------------------------------------------
# the lowering walk
# ---------------------------------------------------------------------------


class _Lowerer:
    def __init__(self, res: Resolved, probes: dict[str, ProbeResult | None]) -> None:
        self.res = res
        self.probes = probes
        self.graph = Graph(input_paths=list(res.input_paths), sources=dict(res.sources))
        # Annotated as the protocol so mypy checks the structural match.
        self.ctx: ExpandCtx = _NodeFactory(self.graph)
        self.cte_columns: dict[str, tuple[_Column, ...]] = {}

    # -- entry point ------------------------------------------------------

    def run(self) -> Graph:
        for name, body in self.res.ctes.items():
            self.cte_columns[name] = tuple(
                self._lower_query(union_branches(body), body)
            )
        columns = self._lower_query(self.res.branches, self.res.select)
        self.graph.outputs = [
            Output(
                ref=column.stream.ref,
                type=column.stream.type,
                name=column.name,
                metadata=self._provenance(column.stream.ref),
            )
            for column in columns
        ]
        return self.graph

    # -- a query (one SELECT, or a UNION ALL of them) ----------------------

    def _lower_query(
        self, branches: list[exp.Select], anchor: exp.Expr
    ) -> list[_Column]:
        if not branches:
            raise _error(ErrorCode.UNSUPPORTED_SQL, "query has no SELECT", anchor)
        lowered = [self._lower_branch(branch) for branch in branches]
        if len(lowered) == 1:
            return lowered[0]
        self._check_concat_signature(branches, lowered)
        return self._concat(lowered)

    def _check_concat_signature(
        self, branches: list[exp.Select], lowered: list[list[_Column]]
    ) -> None:
        """Every UNION ALL branch must agree on column count, types and order."""
        expected = [column.stream.type for column in lowered[0]]
        for index in range(1, len(lowered)):
            got = [column.stream.type for column in lowered[index]]
            if got == expected:
                continue
            raise _error(
                ErrorCode.CONCAT_MISMATCH,
                "UNION ALL branches must select the same stream types in the same "
                f"order: branch 1 selects ({', '.join(expected) or 'nothing'}), "
                f"branch {index + 1} selects ({', '.join(got) or 'nothing'})",
                branches[index],
                hint="ffmpeg concat needs identical segments; reorder or add columns",
            )

    def _concat(self, lowered: list[list[_Column]]) -> list[_Column]:
        """Join branches with one ``concat`` node, interleaved as ffmpeg wants.

        ffmpeg's concat filter takes its inputs per SEGMENT — for ``v=1:a=1``
        that is ``[seg1 v][seg1 a][seg2 v][seg2 a]`` — and produces ``v``
        video pads followed by ``a`` audio pads. The SELECT list of branch 1
        defines the output COLUMN order, which may interleave types
        differently, so the pads are mapped back onto it here.
        """
        first = lowered[0]
        video_positions = [i for i, column in enumerate(first) if column.stream.type == "video"]
        audio_positions = [i for i, column in enumerate(first) if column.stream.type == "audio"]
        video_count, audio_count = len(video_positions), len(audio_positions)

        inputs: list[FrameRef] = []
        for columns in lowered:
            inputs += [columns[i].stream.ref for i in video_positions]
            inputs += [columns[i].stream.ref for i in audio_positions]

        video_pads: list[StreamType] = ["video"] * video_count
        audio_pads: list[StreamType] = ["audio"] * audio_count
        node_id = self.ctx.node(
            "concat",
            {"n": len(lowered), "v": video_count, "a": audio_count},
            inputs,
            video_pads + audio_pads,
        )

        pad_of: dict[int, int] = {}
        for pad, position in enumerate(video_positions):
            pad_of[position] = pad
        for pad, position in enumerate(audio_positions):
            pad_of[position] = video_count + pad

        total = video_count + audio_count
        return [
            _Column(
                name=column.name,
                stream=_Stream(
                    ref=node_id if total == 1 else f"{node_id}:{pad_of[position]}",
                    type=column.stream.type,
                ),
            )
            for position, column in enumerate(first)
        ]

    # -- one SELECT branch ------------------------------------------------

    def _lower_branch(self, select: exp.Select) -> list[_Column]:
        env = self._scope(select)
        self._collect_trims(select, env)

        projections = select.expressions
        if not projections:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL, "SELECT has no output column", fallback=select
            )
        return [
            _Column(
                name=_projection_name(projection),
                stream=self._lower_expr(projection, env, select),
            )
            for projection in projections
        ]

    # -- FROM -------------------------------------------------------------

    def _scope(self, select: exp.Select) -> _Env:
        env = _Env()
        from_ = select.args.get("from_")
        if not isinstance(from_, exp.From):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "SELECT requires a FROM clause",
                fallback=select,
                hint="add FROM input('clip.mp4') a",
            )
        self._add_table(from_.this, env, select)
        joins = select.args.get("joins") or []
        for join in joins:
            if not isinstance(join, exp.Join):
                raise _error(ErrorCode.UNSUPPORTED_SQL, "malformed FROM clause", fallback=select)
            self._add_table(join.this, env, select)
        return env

    def _add_table(self, table: exp.Expr | None, env: _Env, select: exp.Select) -> None:
        if not isinstance(table, exp.Table):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "only input('path') and CTE names are allowed in FROM",
                table,
                fallback=select,
            )
        inner = table.this
        alias_node = table.args.get("alias")
        if isinstance(inner, exp.Anonymous):
            if not isinstance(alias_node, exp.TableAlias) or alias_node.this is None:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "input() requires an alias",
                    table,
                    fallback=select,
                    hint="add an alias, e.g. FROM input('clip.mp4') a",
                )
            alias = _fold(alias_node.this)
            if alias not in self.graph.sources:
                raise _error(
                    ErrorCode.UNKNOWN_ALIAS, f"unknown alias '{alias}'", alias_node, fallback=table
                )
            env.bindings[alias] = _InputBinding(alias=alias)
            return
        if isinstance(inner, exp.Identifier):
            name = _fold(inner)
            columns = self.cte_columns.get(name)
            if columns is None:
                raise _error(
                    ErrorCode.UNKNOWN_ALIAS,
                    f"unknown table '{name}'",
                    inner,
                    fallback=table,
                    hint=self._known_hint(),
                )
            env.bindings[name] = _CteBinding(name=name, columns=columns)
            return
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            "only input('path') and CTE names are allowed in FROM",
            table,
            fallback=select,
        )

    def _known_hint(self) -> str:
        known = sorted(set(self.cte_columns) | set(self.graph.sources))
        return f"known names: {', '.join(known)}" if known else "no aliases are in scope"

    # -- WHERE ------------------------------------------------------------

    def _collect_trims(self, select: exp.Select, env: _Env) -> None:
        """Record each aliased time range; the trim itself is spliced lazily."""
        where = select.args.get("where")
        if not isinstance(where, exp.Where):
            return
        for conjunct in _flatten_and(where.this):
            if not isinstance(conjunct, exp.Between) or not isinstance(
                conjunct.this, exp.Column
            ):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "unsupported WHERE predicate",
                    conjunct,
                    fallback=where,
                    hint=_TIME_HINT,
                )
            column = conjunct.this
            table_node = column.args.get("table")
            if table_node is None:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"unqualified column '{column.name}' in WHERE",
                    column,
                    fallback=where,
                    hint=_TIME_HINT,
                )
            alias = _fold(table_node)
            if _fold(column.this) != _TIME_COLUMN:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"only the time column '{alias}.t' can be filtered, "
                    f"got '{alias}.{column.name}'",
                    column,
                    fallback=where,
                    hint=_TIME_HINT,
                )
            if alias not in env.bindings:
                raise _error(
                    ErrorCode.UNKNOWN_ALIAS,
                    f"unknown alias '{alias}'",
                    table_node,
                    fallback=where,
                    hint=self._known_hint(),
                )
            low = conjunct.args.get("low")
            high = conjunct.args.get("high")
            if not isinstance(low, exp.Expr) or not isinstance(high, exp.Expr):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "BETWEEN needs both bounds",
                    conjunct,
                    fallback=where,
                    hint=_TIME_HINT,
                )
            env.trims[alias] = (
                _number(low, ErrorCode.UNSUPPORTED_SQL),
                _number(high, ErrorCode.UNSUPPORTED_SQL),
            )

    def _access(self, env: _Env, alias: str, stream: _Stream) -> _Stream:
        """Apply `alias`'s WHERE trim to `stream`, once per distinct stream."""
        window = env.trims.get(alias)
        if window is None:
            return stream
        cached = env.trimmed.get(stream.ref)
        if cached is not None:
            return _Stream(ref=cached, type=stream.type)
        start, end = window
        if stream.type == "video":
            trimmed = self.ctx.node(
                "trim", {"start": start, "end": end}, [stream.ref], ["video"]
            )
            rebased = self.ctx.node(
                "setpts", {"expr": "PTS-STARTPTS"}, [trimmed], ["video"]
            )
        else:
            trimmed = self.ctx.node(
                "atrim", {"start": start, "end": end}, [stream.ref], ["audio"]
            )
            rebased = self.ctx.node(
                "asetpts", {"expr": "PTS-STARTPTS"}, [trimmed], ["audio"]
            )
        env.trimmed[stream.ref] = rebased
        return _Stream(ref=rebased, type=stream.type)

    # -- expressions ------------------------------------------------------

    def _lower_expr(self, node: exp.Expr, env: _Env, select: exp.Select) -> _Stream:
        node = _unwrap(node)
        if isinstance(node, exp.Bracket | exp.Column):
            alias, stream = self._base_stream(node, env, select)
            return self._access(env, alias, stream)
        if isinstance(node, exp.Cast):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "casts are not supported",
                node,
                fallback=select,
                hint="a stream has exactly one type",
            )
        parts = _call_parts(node)
        if parts is not None:
            return self._lower_call(node, parts, env, select)
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            "every SELECT column must be a stream expression, got "
            f"{_describe(node)}",
            node,
            fallback=select,
            hint=_STREAM_HINT,
        )

    # -- stream references -------------------------------------------------

    def _base_stream(
        self, node: exp.Expr, env: _Env, select: exp.Select
    ) -> tuple[str, _Stream]:
        """Resolve a column / subscript to ``(alias, untrimmed stream)``.

        Pure: creates no nodes, so the type checker (:meth:`_classify`) can
        call it on an argument before deciding whether to lower it.
        """
        if isinstance(node, exp.Bracket):
            inner = node.this
            if not isinstance(inner, exp.Column):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "only stream columns can be subscripted",
                    node,
                    fallback=select,
                    hint=_SUBSCRIPT_HINT,
                )
            index = subscript_index(node)
            if index is None:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "stream subscript must be a positive integer literal",
                    node,
                    fallback=select,
                    hint=_SUBSCRIPT_HINT,
                )
            return self._resolve_column(inner, index, node, env, select)
        if isinstance(node, exp.Column):
            return self._resolve_column(node, None, node, env, select)
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"expected a stream expression, got {_describe(node)}",
            node,
            fallback=select,
            hint=_STREAM_HINT,
        )

    def _resolve_column(
        self,
        column: exp.Column,
        index: int | None,
        anchor: exp.Expr,
        env: _Env,
        select: exp.Select,
    ) -> tuple[str, _Stream]:
        table_node = column.args.get("table")
        if table_node is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"unqualified column '{column.name}'",
                anchor,
                fallback=select,
                hint="qualify the column with its alias, e.g. a.video[1]",
            )
        alias = _fold(table_node)
        name = _fold(column.this)
        binding = env.bindings.get(alias)
        if binding is None:
            raise _error(
                ErrorCode.UNKNOWN_ALIAS,
                f"unknown alias '{alias}'",
                table_node,
                fallback=select,
                hint=self._known_hint(),
            )
        if isinstance(binding, _InputBinding):
            return alias, self._input_stream(alias, name, index, anchor, select)
        return alias, self._cte_stream(binding, name, index, anchor, select)

    def _input_stream(
        self,
        alias: str,
        name: str,
        index: int | None,
        anchor: exp.Expr,
        select: exp.Select,
    ) -> _Stream:
        if name == _TIME_COLUMN:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{alias}.t' is a time column, not a stream",
                anchor,
                fallback=select,
                hint=_TIME_HINT,
            )
        if name == _FRAME_COLUMN:
            if index is not None:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"'{alias}.frame' is a single stream and cannot be subscripted",
                    anchor,
                    fallback=select,
                    hint=f"'{alias}.frame' is sugar for '{alias}.video[1]'",
                )
            stream_type: StreamType = "video"
            zero_based = 0
        else:
            array_type = _ARRAY_COLUMNS.get(name)
            if array_type is None:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"unknown column '{alias}.{name}'",
                    anchor,
                    fallback=select,
                    hint=f"an input exposes {alias}.frame, {alias}.video, "
                    f"{alias}.audio and {alias}.t",
                )
            if index is None:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"'{alias}.{name}' is the whole array of {array_type} streams, "
                    "which is not supported yet",
                    anchor,
                    fallback=select,
                    hint=_BROADCAST_HINT,
                )
            stream_type = array_type
            zero_based = index - 1

        self._check_bounds(alias, stream_type, zero_based, anchor, select)
        marker = _TYPE_MARKERS[stream_type]
        return _Stream(ref=f"src:{alias}:{marker}:{zero_based}", type=stream_type)

    def _cte_stream(
        self,
        binding: _CteBinding,
        name: str,
        index: int | None,
        anchor: exp.Expr,
        select: exp.Select,
    ) -> _Stream:
        column = self._cte_column(binding, name)
        if column is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"unknown column '{binding.name}.{name}'",
                anchor,
                fallback=select,
                hint=self._cte_columns_hint(binding),
            )
        if index is not None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{binding.name}.{name}' is a single stream and cannot be subscripted",
                anchor,
                fallback=select,
                hint=_BROADCAST_HINT,
            )
        return column.stream

    def _cte_column(self, binding: _CteBinding, name: str) -> _Column | None:
        for column in binding.columns:
            if column.name == name:
                return column
        # v0 compat: a CTE that selects exactly one video column is reachable
        # as `<cte>.frame` whatever (if anything) its AS named it.
        if (
            name == _FRAME_COLUMN
            and len(binding.columns) == 1
            and binding.columns[0].stream.type == "video"
        ):
            return binding.columns[0]
        return None

    def _cte_columns_hint(self, binding: _CteBinding) -> str:
        names = {column.name for column in binding.columns if column.name is not None}
        if len(binding.columns) == 1 and binding.columns[0].stream.type == "video":
            names.add(_FRAME_COLUMN)
        if not names:
            return (
                f"'{binding.name}' has no named columns; name them with AS "
                "inside its SELECT"
            )
        return f"'{binding.name}' exposes: {', '.join(sorted(names))}"

    def _check_bounds(
        self,
        alias: str,
        stream_type: StreamType,
        zero_based: int,
        anchor: exp.Expr,
        select: exp.Select,
    ) -> None:
        """Bounds-check a subscript — only possible when the input was probed."""
        result = self.probes.get(alias)
        if result is None:
            return
        available = len(result.by_type(stream_type))
        if zero_based < available:
            return
        path = self.res.input_paths[self.graph.sources[alias]]
        have = f"{available} {stream_type} stream" + ("" if available == 1 else "s")
        raise _error(
            ErrorCode.STREAM_NOT_FOUND,
            f"'{alias}.{stream_type}[{zero_based + 1}]' does not exist: "
            f"'{path}' has {have}",
            anchor,
            fallback=select,
            hint=_SUBSCRIPT_HINT,
        )

    # -- calls -------------------------------------------------------------

    def _lower_call(
        self,
        node: exp.Expr,
        parts: tuple[str, list[exp.Expr]],
        env: _Env,
        select: exp.Select,
    ) -> _Stream:
        raw_name, arg_nodes = parts
        name = raw_name.lower()
        spec = FUNCTIONS.get(name)
        if spec is None:
            raise _error(
                ErrorCode.UNKNOWN_FUNCTION,
                f"unknown function {raw_name}()",
                node,
                fallback=select,
                hint=_unknown_function_hint(name),
            )

        kinds = [self._classify(arg, env, select) for arg in arg_nodes]
        variant = _match_variant(spec.variants, kinds)
        if variant is None:
            got = ", ".join(kinds)
            raise _error(
                ErrorCode.UDF_ARG_TYPE,
                f"{name}() expects {signatures(name)}, got {name}({got})",
                node,
                fallback=select,
                hint=_ARG_HINT,
            )

        values: list[object] = []
        for param, arg in zip(variant, arg_nodes):
            if param.kind in ("video", "audio"):
                values.append(self._lower_expr(arg, env, select).ref)
            elif param.kind == "num":
                values.append(_number(arg))
            else:
                values.append(_string(arg))
        return _Stream(ref=spec.expand(self.ctx, values), type=spec.returns)

    def _classify(self, node: exp.Expr, env: _Env, select: exp.Select) -> str:
        """Kind label for one call argument, matched against ``Param.kind``.

        Stream arguments resolve to ``video``/``audio`` without creating any
        node, so a mismatch is reported before the graph grows. Nested calls
        to unknown functions are reported here rather than being labelled a
        stream and swallowed by an outer arity error.
        """
        node = _unwrap(node)
        if isinstance(node, exp.Literal):
            return "str" if node.is_string else "num"
        if (
            isinstance(node, exp.Neg)
            and isinstance(node.this, exp.Literal)
            and not node.this.is_string
        ):
            return "num"
        if isinstance(node, exp.Bracket | exp.Column):
            return self._base_stream(node, env, select)[1].type
        if isinstance(node, exp.Cast):
            return _EXPR_KIND
        parts = _call_parts(node)
        if parts is not None:
            name = parts[0].lower()
            spec = FUNCTIONS.get(name)
            if spec is None:
                raise _error(
                    ErrorCode.UNKNOWN_FUNCTION,
                    f"unknown function {parts[0]}()",
                    node,
                    fallback=select,
                    hint=_unknown_function_hint(name),
                )
            return spec.returns
        return _EXPR_KIND

    # -- provenance --------------------------------------------------------

    def _provenance(self, ref: FrameRef) -> dict[str, str]:
        """Language/title tags of a passthrough output's source stream.

        Only a DIRECT source ref qualifies in this plan: threading 1:1
        provenance through a filter chain is plan 020's job. ``language=und``
        is what an mp4 muxer stamps on an untagged stream, so it is dropped.
        """
        if not is_src(ref):
            return {}
        try:
            alias, stream_type, index = src_parts(ref)
        except ValueError:  # pragma: no cover - refs are built above
            return {}
        result = self.probes.get(alias)
        if result is None:
            return {}
        streams = result.by_type(stream_type)
        if not 0 <= index < len(streams):
            return {}
        source = streams[index]
        metadata: dict[str, str] = {}
        for key in _PROVENANCE_KEYS:
            value = source.metadata.get(key)
            if value is None:
                continue
            if key == "language" and value == _UNDEFINED_LANGUAGE:
                continue
            metadata[key] = value
        return metadata


def _match_variant(
    variants: Iterable[tuple[Param, ...]], kinds: list[str]
) -> tuple[Param, ...] | None:
    for variant in variants:
        if len(variant) != len(kinds):
            continue
        if all(param.kind == kind for param, kind in zip(variant, kinds)):
            return variant
    return None


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------


def lower(res: Resolved, probes: dict[str, ProbeResult | None]) -> Graph:
    """Lower a resolved query into an IR graph.

    `probes` is keyed by input ALIAS (``compiler.compile_sql`` builds it, one
    ``probe()`` per distinct path); a missing or ``None`` entry means that
    input could not be read, and lowering stays symbolic for it.

    Raises ``SqlmpegError`` — and nothing else — on every rejection.
    """
    try:
        return _Lowerer(res, probes).run()
    except SqlmpegError:
        raise
    except Exception as err:  # backstop: guardrail #7, no panics on user input
        raise SqlmpegError(
            ErrorCode.INTERNAL,
            f"internal error while lowering ({err.__class__.__name__}: {err})",
            line=1,
            col=1,
            hint="please report this query as a bug",
        ) from err
