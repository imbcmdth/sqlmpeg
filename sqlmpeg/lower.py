"""Lower pass: a resolved query becomes an IR :class:`~sqlmpeg.ir.Graph`.

This is pass 2 of the compiler (see "Architecture" in sqlmpeg-project.md). It
assumes :func:`sqlmpeg.parser.resolve` already accepted the query, so every
rejection raised here is either a check resolve deliberately left to lowering
(column names, function names, argument types) or a defensive re-check.

What lowering does, in order:

* CTE bodies lower first, in definition order, into the *same* graph; the
  FrameRef each one ends on is what ``FROM <cte>`` resolves to later.
* Inside a branch, ``FROM`` builds an environment mapping alias -> FrameRef
  (``"src:<alias>"`` for ``input()``, the recorded ref for a CTE).
* ``WHERE a.t BETWEEN x AND y`` splices ``trim`` + ``setpts`` in front of that
  alias's ref and rebinds the alias, so every consumer of ``a`` in that branch
  sees the trimmed, PTS-rebased stream.
* The single projection expression lowers bottom-up. ``<alias>.frame`` is a
  FrameRef; a stdlib call type-checks its arguments against
  ``stdlib.FUNCTIONS[name].variants`` and then delegates node creation to the
  spec's ``expand`` (guardrail #4: no per-function lowering logic lives here).
* A UNION ALL (top level or inside a CTE) lowers each branch and joins them
  with one ``concat`` node.

Node ids are ``n1, n2, ...`` in creation order across the whole graph, minted
by :class:`_NodeFactory`, the :class:`sqlmpeg.stdlib.ExpandCtx` implementation.

sqlglot notes that matter here
------------------------------
* Postgres has a builtin ``OVERLAY``, so ``overlay(a, b, x, y)`` parses to
  :class:`sqlglot.exp.Overlay` with *named* args (``this``, ``expression``,
  ``from_``, ``for_``) rather than to ``exp.Anonymous``. :func:`_call_parts`
  normalizes it back to four positional arguments. The other eleven stdlib
  names have no builtin collision and arrive as ``exp.Anonymous``.
* ``exp.Literal.to_py()`` returns ``decimal.Decimal`` for non-integer numbers,
  which neither ``emit`` nor JSON can render, so numeric literals are coerced
  to ``int``/``float`` here. ``-1.5`` parses as ``exp.Neg(Literal)``.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable

from sqlglot import exp

from sqlmpeg.errors import ErrorCode, SqlmpegError
from sqlmpeg.ir import FrameRef, Graph, Node
from sqlmpeg.parser import Resolved, _pos, union_branches
from sqlmpeg.parser import _ident_name as _fold
from sqlmpeg.stdlib import FUNCTIONS, ExpandCtx, Param, signatures

__all__ = ["lower"]

_FRAME_COLUMN = "frame"
_TIME_COLUMN = "t"

# Kind label used in UDF_ARG_TYPE "got" lists for anything that is neither a
# literal nor a frame-typed subexpression (e.g. `1 + 2`, NULL, TRUE).
_EXPR_KIND = "expr"

_COLUMN_HINT = "the only column a frame source exposes is <alias>.frame"
_TIME_HINT = "<alias>.t is only usable as WHERE <alias>.t BETWEEN <start> AND <end>"
_ARG_HINT = "arguments are frame expressions or literals, in the order shown"


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
    """Short human name for an expression that cannot produce a frame."""
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
# ExpandCtx
# ---------------------------------------------------------------------------


class _NodeFactory:
    """:class:`sqlmpeg.stdlib.ExpandCtx` impl: mints ``n1, n2, ...`` into a graph."""

    def __init__(self, graph: Graph) -> None:
        self._graph = graph
        self._counter = 0

    def node(
        self, filter: str, args: dict[str, object], inputs: list[FrameRef]
    ) -> FrameRef:
        self._counter += 1
        node_id = f"n{self._counter}"
        self._graph.nodes[node_id] = Node(
            id=node_id, filter=filter, args=dict(args), inputs=list(inputs)
        )
        return node_id


# ---------------------------------------------------------------------------
# the lowering walk
# ---------------------------------------------------------------------------


class _Lowerer:
    def __init__(self, res: Resolved) -> None:
        self.res = res
        self.graph = Graph(
            input_paths=list(res.input_paths), sources=dict(res.sources)
        )
        # Annotated as the protocol so mypy checks the structural match.
        self.ctx: ExpandCtx = _NodeFactory(self.graph)
        self.cte_refs: dict[str, FrameRef] = {}

    # -- entry point ------------------------------------------------------

    def run(self) -> Graph:
        for name, body in self.res.ctes.items():
            self.cte_refs[name] = self._lower_query(union_branches(body), body)
        self.graph.output = self._lower_query(self.res.branches, self.res.select)
        return self.graph

    def _lower_query(self, branches: list[exp.Select], anchor: exp.Expr) -> FrameRef:
        if not branches:
            raise _error(ErrorCode.UNSUPPORTED_SQL, "query has no SELECT", anchor)
        refs = [self._lower_branch(branch) for branch in branches]
        if len(refs) == 1:
            return refs[0]
        return self.ctx.node("concat", {"n": len(refs), "v": 1, "a": 0}, refs)

    # -- one SELECT branch ------------------------------------------------

    def _lower_branch(self, select: exp.Select) -> FrameRef:
        env = self._scope(select)
        self._apply_where(select, env)

        projections = select.expressions
        if len(projections) != 1:
            raise _error(
                ErrorCode.SINGLE_OUTPUT_ONLY,
                f"SELECT must produce exactly one frame column, got {len(projections)}",
                fallback=select,
            )
        return self._lower_expr(projections[0], env, select)

    # -- FROM -------------------------------------------------------------

    def _scope(self, select: exp.Select) -> dict[str, FrameRef]:
        env: dict[str, FrameRef] = {}
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

    def _add_table(
        self, table: exp.Expr | None, env: dict[str, FrameRef], select: exp.Select
    ) -> None:
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
            env[alias] = f"src:{alias}"
            return
        if isinstance(inner, exp.Identifier):
            name = _fold(inner)
            ref = self.cte_refs.get(name)
            if ref is None:
                raise _error(
                    ErrorCode.UNKNOWN_ALIAS,
                    f"unknown table '{name}'",
                    inner,
                    fallback=table,
                    hint=self._known_hint(),
                )
            env[name] = ref
            return
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            "only input('path') and CTE names are allowed in FROM",
            table,
            fallback=select,
        )

    def _known_hint(self) -> str:
        known = sorted(set(self.cte_refs) | set(self.graph.sources))
        return f"known names: {', '.join(known)}" if known else "no aliases are in scope"

    # -- WHERE ------------------------------------------------------------

    def _apply_where(self, select: exp.Select, env: dict[str, FrameRef]) -> None:
        """Rebind each aliased time range to a trim + setpts chain."""
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
            if alias not in env:
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
            start = _number(low, ErrorCode.UNSUPPORTED_SQL)
            end = _number(high, ErrorCode.UNSUPPORTED_SQL)
            trimmed = self.ctx.node("trim", {"start": start, "end": end}, [env[alias]])
            env[alias] = self.ctx.node("setpts", {"expr": "PTS-STARTPTS"}, [trimmed])

    # -- expressions ------------------------------------------------------

    def _lower_expr(
        self, node: exp.Expr, env: dict[str, FrameRef], select: exp.Select
    ) -> FrameRef:
        node = _unwrap(node)
        if isinstance(node, exp.Column):
            return self._lower_column(node, env, select)
        if isinstance(node, exp.Cast):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "casts are not supported",
                node,
                fallback=select,
                hint="frames have exactly one type",
            )
        parts = _call_parts(node)
        if parts is not None:
            return self._lower_call(node, parts, env, select)
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"expected a frame expression, got {_describe(node)}",
            node,
            fallback=select,
            hint="a SELECT must produce a frame, e.g. scale(a.frame, 0.5)",
        )

    def _lower_column(
        self, node: exp.Column, env: dict[str, FrameRef], select: exp.Select
    ) -> FrameRef:
        table_node = node.args.get("table")
        if table_node is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"unqualified column '{node.name}'",
                node,
                fallback=select,
                hint="qualify the column with its alias, e.g. a.frame",
            )
        alias = _fold(table_node)
        column = _fold(node.this)
        if column == _TIME_COLUMN:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{alias}.t' is a time column, not a frame",
                node,
                fallback=select,
                hint=_TIME_HINT,
            )
        if column != _FRAME_COLUMN:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"unknown column '{alias}.{node.name}'",
                node,
                fallback=select,
                hint=_COLUMN_HINT,
            )
        ref = env.get(alias)
        if ref is None:
            raise _error(
                ErrorCode.UNKNOWN_ALIAS,
                f"unknown alias '{alias}'",
                table_node,
                fallback=select,
                hint=self._known_hint(),
            )
        return ref

    def _lower_call(
        self,
        node: exp.Expr,
        parts: tuple[str, list[exp.Expr]],
        env: dict[str, FrameRef],
        select: exp.Select,
    ) -> FrameRef:
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

        kinds = [self._classify(arg, select) for arg in arg_nodes]
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
            if param.kind == "frame":
                values.append(self._lower_expr(arg, env, select))
            elif param.kind == "num":
                values.append(_number(arg))
            else:
                values.append(_string(arg))
        return spec.expand(self.ctx, values)

    def _classify(self, node: exp.Expr, select: exp.Select) -> str:
        """Kind label for one call argument, matched against ``Param.kind``.

        Nested calls to unknown functions are reported here rather than being
        labelled ``frame`` and swallowed by an outer arity error.
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
        if isinstance(node, exp.Column):
            return "frame"
        if isinstance(node, exp.Cast):
            return _EXPR_KIND
        parts = _call_parts(node)
        if parts is not None:
            name = parts[0].lower()
            if name not in FUNCTIONS:
                raise _error(
                    ErrorCode.UNKNOWN_FUNCTION,
                    f"unknown function {parts[0]}()",
                    node,
                    fallback=select,
                    hint=_unknown_function_hint(name),
                )
            return "frame"
        return _EXPR_KIND


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


def lower(res: Resolved) -> Graph:
    """Lower a resolved query into an IR graph.

    Raises ``SqlmpegError`` — and nothing else — on every rejection.
    """
    try:
        return _Lowerer(res).run()
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
