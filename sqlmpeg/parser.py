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
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, SqlglotError

from sqlmpeg.errors import ErrorCode, SqlmpegError

__all__ = ["Resolved", "parse", "resolve", "union_branches"]

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

_WHERE_HINT = (
    "the only supported WHERE form is <alias>.t BETWEEN <start> AND <end>, "
    "optionally joined with AND"
)
_ALIAS_HINT = "add an alias, e.g. FROM input('clip.mp4') a"


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


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


@dataclass
class Resolved:
    """Output of the resolve pass — the validated query plus its input table."""

    select: QueryExpr
    """Top-level query, CTEs still attached. An ``exp.Union`` for UNION ALL."""

    input_paths: list[str]
    """``-i`` order; the list index is the ffmpeg input index. May repeat paths."""

    sources: dict[str, int]
    """Input alias -> index into ``input_paths``. One entry per distinct alias."""

    ctes: dict[str, QueryExpr] = field(default_factory=dict)
    """CTE name -> its query, in definition order."""

    branches: list[exp.Select] = field(default_factory=list)
    """``select`` flattened into UNION ALL branches; a single element if not a union."""


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


class _Resolver:
    def __init__(self) -> None:
        self.input_paths: list[str] = []
        self.sources: dict[str, int] = {}
        self.ctes: dict[str, QueryExpr] = {}

    # -- entry point ------------------------------------------------------

    def run(self, tree: exp.Expr) -> Resolved:
        if isinstance(tree, exp.Block):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "only one statement per query is supported",
                tree,
                hint="remove the trailing statements",
            )
        query = _unwrap(tree)
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

        return Resolved(
            select=query,
            input_paths=self.input_paths,
            sources=self.sources,
            ctes=self.ctes,
            branches=branches,
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
                    hint="a CTE produces exactly one frame column",
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
        if name in self.ctes or name in self.sources:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"duplicate name '{name}'",
                node,
                hint="alias and CTE names must be unique across the whole query",
            )

    # -- selects ----------------------------------------------------------

    def _validate_select(self, select: exp.Select, visible: set[str]) -> None:
        _check_query_args(select, _SELECT_ALLOWED, "SELECT")

        projections = select.expressions
        if len(projections) > 1:
            raise _error(
                ErrorCode.SINGLE_OUTPUT_ONLY,
                f"SELECT must produce exactly one frame column, got {len(projections)}",
                projections[1],
                fallback=select,
                hint="split the extra columns into separate queries",
            )
        if not projections:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL, "SELECT has no output column", fallback=select
            )
        projection = projections[0]
        self._check_expression(projection, select)

        where = select.args.get("where")
        if isinstance(where, exp.Where):
            self._check_expression(where, select)

        scope = self._collect_scope(select, visible)
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
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "SELECT * is not supported",
                    sub,
                    fallback=select,
                    hint="select a single frame expression",
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
        if table.args.get("db") or table.args.get("catalog"):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "qualified table names are not supported",
                table,
            )
        for key, value in table.args.items():
            if key in ("this", "alias") or not value:
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
                    hint="reference a CTE once per query; reuse is automatic",
                )
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
        if len(args) != 1 or not (isinstance(args[0], exp.Literal) and args[0].is_string):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "input() takes exactly one string literal path",
                func,
                fallback=table,
                hint="use input('clip.mp4')",
            )
        path = str(args[0].this)
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
        scope[alias] = "input"

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
            if name not in scope:
                raise _error(
                    ErrorCode.UNKNOWN_ALIAS,
                    f"unknown alias '{name}'",
                    table_node,
                    fallback=select,
                    hint=self._known_hint(scope),
                )

    def _check_where(
        self, where: exp.Where, scope: dict[str, str], select: exp.Select
    ) -> None:
        conjuncts: list[exp.Expr] = []
        self._flatten_and(where.this, conjuncts, select)

        seen: set[str] = set()
        for conjunct in conjuncts:
            if not isinstance(conjunct, exp.Between) or conjunct.args.get("symmetric"):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "unsupported WHERE predicate",
                    conjunct,
                    fallback=where,
                    hint=_WHERE_HINT,
                )
            column = conjunct.this
            if not isinstance(column, exp.Column):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "unsupported WHERE predicate",
                    conjunct,
                    fallback=where,
                    hint=_WHERE_HINT,
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
            for key in ("low", "high"):
                bound = conjunct.args.get(key)
                if not (isinstance(bound, exp.Literal) and not bound.is_string):
                    raise _error(
                        ErrorCode.UNSUPPORTED_SQL,
                        "BETWEEN bounds must be numeric literals (seconds)",
                        bound if isinstance(bound, exp.Expr) else conjunct,
                        fallback=where,
                        hint=_WHERE_HINT,
                    )
            if alias in seen:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"more than one time range for alias '{alias}'",
                    column,
                    fallback=where,
                    hint="use a single BETWEEN per alias",
                )
            seen.add(alias)

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
