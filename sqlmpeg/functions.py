"""User-defined SQL functions: ``CREATE FUNCTION``, and what a call to one becomes.

A function is a parameterized query fragment, which is what a view cannot be::

    CREATE FUNCTION normalize_lang(raw text) RETURNS text AS $$
      SELECT CASE WHEN raw = 'en' THEN 'eng' ELSE raw END
    $$ LANGUAGE sql;

There is no runtime concept and nothing new in the IR: every call site INLINES
the body with its arguments bound, the definitions are lifted out of the
script, and the ordinary compiler runs on what is left. :func:`expanded` is the
whole surface -- ``sqlmpeg.parser.resolve`` wraps its own work in it, and
nothing else in the package knows functions exist.

Three things make that inlining honest:

**Hygiene.** Names live in ONE flat script-wide namespace, so a body that binds
``g`` breaks the moment the function is called twice. Every alias the body
binds is renamed per call site (``g`` -> ``first_track_1_g``), against a set of
names already taken by the script, so two calls never collide -- and two calls
to a body that declares its own ``input()`` mint two ``-i`` entries, since
input identity is the alias. A body may not reference an alias of the calling
query at all; it sees its parameters and its own FROM items, and nothing else.

**The body's own type is what compiles.** Inlining means the expansion IS the
query the user could have written by hand: a body selecting a bare array splats
in the SELECT list because a bare array column does, not because ``RETURNS
audio_stream[]`` said so. ``RETURNS`` is checked against the declared type
vocabulary (:mod:`sqlmpeg.types`) and decides table-returning from
value-returning; it is not re-checked against the body.

**Diagnostics through two layers.** A body's nodes carry positions in the
BODY's coordinates, which mean nothing in a script the reader is looking at.
Each expansion re-stamps them into a private high line range that encodes which
expansion they came from and which body line they were on (:data:`_BODY_LINE_BASE`,
the same trick as a ``#line`` directive). A rejection landing on one is
rewritten to anchor on the CALL SITE, its message saying which body line it
came from; on a successful resolve the range is flattened to the call site
outright, so no later pass can report a line the reader cannot find. A
rejection whose blame includes a written ARGUMENT anchors on the argument
instead, since ``sqlmpeg.parser._pos`` takes the earliest position in the
subtree and the caller's own text always sorts first.

**A table-returning function is a row source, so it becomes a CTE.**
``RETURNS TABLE(col type, ...)`` is called in FROM and nowhere else. Its body
is not spliced into the calling query -- a body with an ``unnest`` produces
MANY rows, and splicing would hand the host the body's FROM items without the
body ever being a relation of its own. Each call becomes one generated CTE
holding the whole body, its projections aliased to the declared column names,
and the FROM item becomes a reference to that CTE under the writer's alias.
Everything about cardinality is then the CTE row source's, already defined:
comma is a cross join with real multiplicity, ``WHERE`` narrows the product,
``array_agg``/``GROUP BY`` gather it, and several rows into one path is
``ROW_COUNT_MISMATCH``. A call in the SELECT list is a typed rejection: SQL
reads ``(f(x)).a`` once per FIELD, and input identity here is the alias, so
each read would mint its own ``-i`` for one file.

**A definition need not be written in the script.** A call qualified by a
namespace a package claims (``me.pick(...)``, :mod:`sqlmpeg.project`) resolves
to a definition read out of that package's sources and inlines through this
same expander -- same hygiene, same source map, same arity and type checks.
Nothing is spliced into the script and the flat script namespace is untouched,
because a package's definitions never enter it.

One rule differs by where a definition was written. A package source is a
LIBRARY: it exports more than any one query calls, so an uncalled definition
in one is the point of it. A definition in the user's own script is a script's,
and one nothing calls stays an error. :meth:`_Expander._uncalled` is where that
asymmetry is spelled out.
"""

from __future__ import annotations

import copy
import difflib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import UnionType

from sqlglot import exp

from .errors import ErrorCode, SqlmpegError
from .parser import (
    _ARITHMETIC,
    FILTER_NAMESPACE,
    MACRO_NAMESPACE,
    _check_query_args,
    _error,
    _ident_name,
    _pos,
    _statements,
    from_entries,
    parse,
)
from .project import RESERVED_NAMESPACES, Package, PackageSet
from .types import TYPES, element_type, is_array

__all__ = ["NAMEABLE_TYPES", "expanded"]

# The FROM item that mints an `-i`. Never an argument: it is a table, and a
# table reference in a value position is not SQL.
_INPUT = "input"

# Names a definition may not claim: the dialect's own FROM item, the two
# reserved namespaces, and every name sqlglot parses as a builtin (a call to
# one comes back as its own node type, never as the anonymous call expansion
# looks for, so redefining it would silently do nothing).
_RESERVED = frozenset({_INPUT, FILTER_NAMESPACE, MACRO_NAMESPACE})

# The types a signature may name: the scalars, the four stream records, and
# the non-stream records the compiler surfaces. Handles, maps and `container`
# have no spelling a query can write; `attachment` and `cue` are declared but
# not wired up.
NAMEABLE_TYPES: tuple[str, ...] = tuple(
    sorted(
        name
        for name, declared in TYPES.items()
        if declared.kind in {"scalar", "stream", "record"}
        and (not declared.fields or any(f.exposed for f in declared.fields))
    )
)

_TYPE_HINT = (
    "a signature names " + ", ".join(NAMEABLE_TYPES) + ", or an array of one "
    "(e.g. audio_stream[])"
)
_FUNCTION_HINT = (
    "write CREATE FUNCTION <name>(<param> <type>, ...) RETURNS <type> "
    "AS $$ <query> $$ LANGUAGE sql"
)
_TABLE_HINT = (
    "write RETURNS TABLE(<column> <type>, ...), one name per column the body selects"
)
_VALUE_BODY_HINT = "a value-returning function's body is one SELECT of one column"
_TABLE_BODY_HINT = (
    "a table-returning function's body is one SELECT, one column per RETURNS TABLE column"
)
_ARG_HINT = (
    "a function that needs a file takes its path as text and calls input() in "
    "its own FROM"
)
_BODY_SCOPE_HINT = (
    "a function body sees its parameters and its own FROM items; pass what it "
    "needs as an argument"
)

# The sqlglot data types the three scalars land as. Everything else is an
# unknown type name -- including the other spellings of these (`varchar`,
# `int`), which the dialect does not have.
_SCALAR_TYPES = {
    exp.DataType.Type.TEXT: "text",
    exp.DataType.Type.DECIMAL: "number",
    exp.DataType.Type.BOOLEAN: "boolean",
}

_ArgumentShape = type[exp.Expr] | tuple[type[exp.Expr], ...] | UnionType

# What a written argument's SHAPE says its type is, where the shape says
# anything at all. A column, a subscript, an accessor or a CASE says nothing
# here -- their types come from the probe or from their branches, and resolve
# checks them after expansion.
_ARGUMENT_KINDS: list[tuple[_ArgumentShape, str]] = [
    (exp.Boolean, "boolean"),
    (exp.DPipe, "text"),
    (exp.Cast, "text"),
    (_ARITHMETIC, "number"),
    (exp.Neg, "number"),
    (
        (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between, exp.In,
         exp.Is, exp.And, exp.Or, exp.Not),
        "boolean",
    ),
]

# How each kind is named back to the writer in a rejection.
_KIND_NAMES = {
    "text": "a string",
    "number": "a number",
    "boolean": "true or false",
    "stream": "a stream",
}

# Body positions live in this line range, one SPAN per expansion, so a
# rejection landing on body text can be traced back to the call it came from.
# Well above any hand-written script's line count; :meth:`_Expander.settle`
# clears the range before any later pass can see it.
_BODY_LINE_BASE = 1_000_000
_BODY_LINE_SPAN = 10_000

# How many inlinings one script may do. Recursion is caught by name, so this
# only bounds a legal but absurd nesting.
_EXPANSION_BUDGET = 200


@dataclass(frozen=True)
class _Param:
    """One position of a signature: a name and a declared type."""

    name: str
    type: str


@dataclass
class _Function:
    """One ``CREATE FUNCTION``, validated, with its body already parsed.

    `node` is the name identifier -- the earliest positioned token of the
    statement, so a definition-level rejection anchors on the name.
    `aliases` is what the body binds and expansion has to rename.
    `columns` is the ``RETURNS TABLE`` list, and None for a value.
    `namespace` is the package this came from, and "" for the script's own.
    """

    name: str
    params: tuple[_Param, ...]
    returns: str
    body: exp.Select
    node: exp.Expr
    aliases: frozenset[str]
    position: int
    columns: tuple[_Param, ...] | None = None
    used: bool = False
    namespace: str = ""

    @property
    def returns_rows(self) -> bool:
        return self.columns is not None

    @property
    def library(self) -> bool:
        """True for a definition read out of a package SOURCE, not the script."""
        return bool(self.namespace)

    @property
    def qualified(self) -> str:
        """The name as a call site writes it: ``ns.fn`` for a package's, ``fn`` for a script's."""
        return f"{self.namespace}.{self.name}" if self.namespace else self.name

    @property
    def signature(self) -> str:
        written = ", ".join(f"{p.name} {p.type}" for p in self.params)
        return f"{self.qualified}({written}) RETURNS {self.returns}"


@dataclass(frozen=True)
class _Expansion:
    """One inlining: which function, and where it was written."""

    name: str
    line: int
    col: int


@contextmanager
def expanded(tree: exp.Expression, *, packages: PackageSet | None = None) -> Iterator[exp.Expr]:
    """Yield `tree` with every function definition lifted out and every call inlined.

    `packages` is where a namespaced call resolves; None means the caller named
    no project, and a qualified call is then whatever it always was.

    A rejection raised while the block runs is re-anchored: one landing inside
    an expanded body comes back pointing at the call site, saying which body
    line it came from. On a clean exit the body line range is flattened to the
    call site, so nothing downstream can report a position the reader cannot
    find.

    A script with no ``CREATE FUNCTION`` and no packages to call into yields
    the tree untouched.
    """
    expander = _Expander(packages=packages)
    try:
        # Expansion's own rejections need translating too: a call written
        # inside a body was already stamped by the expansion around it.
        script = expander.run(tree)
    except SqlmpegError as err:
        raise expander.translate(err) from err
    try:
        yield script
    except SqlmpegError as err:
        raise expander.translate(err) from err
    expander.settle(script)


# -- reading a definition -------------------------------------------------


def _create_kind(create: exp.Create) -> str:
    kind = create.args.get("kind")
    return kind.upper() if isinstance(kind, str) else ""


def _written(node: exp.Expr | None) -> str:
    """What a node says when printed back, for a message. Never raises."""
    if node is None:
        return "?"
    try:
        return node.sql(dialect="postgres")
    except Exception:  # a node sqlglot cannot render is still a rejection
        return node.__class__.__name__.upper()


def _listed(columns: tuple[_Param, ...] | None) -> str:
    """The columns an alias would expose, written off an example alias."""
    names = ", ".join(f"t.{column.name}" for column in columns or ())
    return names or "its columns"


def _type_name(node: exp.Expr | None) -> str | None:
    """The dialect type `node` spells, or None if it spells none."""
    if not isinstance(node, exp.DataType):
        return None
    kind = node.this
    if kind is exp.DataType.Type.ARRAY:
        inner = node.expressions
        if len(inner) != 1:
            return None
        element = _type_name(inner[0])
        return None if element is None or is_array(element) else f"{element}[]"
    if kind is exp.DataType.Type.USERDEFINED:
        return _ident_name(node.args.get("kind"))
    if node.expressions:  # a parameterized spelling, e.g. decimal(5, 2)
        return None
    return _SCALAR_TYPES.get(kind)


def _checked_type(node: exp.Expr | None, name: str, anchor: exp.Expr) -> str:
    """The type `node` declares, rejected by name if the dialect has no such type."""
    declared = _type_name(node)
    if declared is None or element_type(declared) not in NAMEABLE_TYPES:
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"function '{name}' declares an unknown type '{_written(node).lower()}'",
            anchor,
            hint=_TYPE_HINT,
        )
    return declared


def _function_name(create: exp.Create) -> tuple[exp.Expr, exp.Identifier, list[exp.Expr]]:
    """The signature node, the name identifier, and the parameter list."""
    signature = create.this
    if isinstance(signature, exp.UserDefinedFunction):
        table, params = signature.this, list(signature.expressions)
    else:
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            "a function is missing its parameter list",
            signature if isinstance(signature, exp.Expr) else None,
            fallback=create,
            hint=_FUNCTION_HINT,
        )
    if not isinstance(table, exp.Table):
        raise _error(
            ErrorCode.UNSUPPORTED_SQL, "a function is missing its name", create, hint=_FUNCTION_HINT
        )
    if table.args.get("db") or table.args.get("catalog"):
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            "qualified function names are not supported",
            table,
            fallback=create,
            hint="a function lives in one script, not in a schema",
        )
    identifier = table.this
    if not isinstance(identifier, exp.Identifier):
        raise _error(
            ErrorCode.UNSUPPORTED_SQL, "a function is missing its name", create, hint=_FUNCTION_HINT
        )
    return signature, identifier, params


def _properties(
    create: exp.Create, name: str, anchor: exp.Expr
) -> tuple[exp.ReturnsProperty, str]:
    """The ``RETURNS`` property and the declared language; anything else is rejected."""
    returns: exp.ReturnsProperty | None = None
    language = ""
    properties = create.args.get("properties")
    if isinstance(properties, exp.Properties):
        for prop in properties.expressions:
            if isinstance(prop, exp.ReturnsProperty):
                returns = prop
                continue
            if isinstance(prop, exp.LanguageProperty):
                language = _ident_name(prop.this) if isinstance(prop.this, exp.Expr) else ""
                continue
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"unsupported CREATE FUNCTION option: {_written(prop)}",
                anchor,
                hint="a function carries a signature, a body and LANGUAGE sql, "
                "and nothing else",
            )
    if returns is None:
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"function '{name}' declares no RETURNS type",
            anchor,
            hint=_FUNCTION_HINT,
        )
    return returns, language


def _body_text(create: exp.Create, name: str, anchor: exp.Expr) -> str:
    """The body as written, from either quoting Postgres allows."""
    body = create.args.get("expression")
    if isinstance(body, exp.Heredoc) and isinstance(body.this, str):
        return body.this
    if isinstance(body, exp.Literal) and body.is_string:
        return str(body.this)
    raise _error(
        ErrorCode.UNSUPPORTED_SQL,
        f"function '{name}' has no body",
        anchor,
        fallback=create,
        hint=_FUNCTION_HINT,
    )


def _reanchor(err: SqlmpegError, name: str, anchor: exp.Expr) -> SqlmpegError:
    """A body-static rejection, said in the script's own coordinates."""
    line, col = _pos(anchor)
    at = f" at body line {err.line}" if err.line is not None else ""
    return SqlmpegError(
        err.code,
        f"the body of {name}(){at}: {err.message}",
        line=line,
        col=col,
        hint=err.hint,
    )


def _body_select(
    text: str, name: str, anchor: exp.Expr, columns: tuple[_Param, ...] | None
) -> exp.Select:
    """Parse and shape-check one body: a single SELECT of the declared width.

    A value's body is one column; a table's is one per ``RETURNS TABLE`` name.
    """
    wanted = 1 if columns is None else len(columns)
    shape = _VALUE_BODY_HINT if columns is None else _TABLE_BODY_HINT
    try:
        parsed = parse(text)
    except SqlmpegError as err:
        raise _reanchor(err, name, anchor) from err
    if not isinstance(parsed, exp.Select):
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"the body of {name}() is not one SELECT",
            anchor,
            hint=shape,
        )
    if parsed.args.get("with_") is not None:
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"the body of {name}() may not have its own WITH",
            anchor,
            hint="a function body is inlined into the query that calls it; "
            "put the CTE there",
        )
    try:
        _check_query_args(
            parsed, frozenset({"expressions", "from_", "joins", "where"}), "function body"
        )
    except SqlmpegError as err:
        raise _reanchor(err, name, anchor) from err
    written = len(parsed.expressions)
    if written != wanted:
        plural = "" if written == 1 else "s"
        said = (
            "and a value is one column"
            if columns is None
            else f"but its RETURNS TABLE declares {wanted}"
        )
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"the body of {name}() selects {written} column{plural}, {said}",
            anchor,
            hint=shape,
        )
    return parsed


def _body_aliases(
    body: exp.Select, name: str, params: Sequence[_Param], anchor: exp.Expr
) -> frozenset[str]:
    """The names the body binds in its own FROM, checked against the signature."""
    aliases: set[str] = set()
    for item, _ in from_entries(body):
        alias = item.args.get("alias")
        if isinstance(alias, exp.TableAlias) and isinstance(alias.this, exp.Identifier):
            aliases.add(_ident_name(alias.this))
    shadowed = aliases.intersection(param.name for param in params)
    if shadowed:
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"the body of {name}() shadows the parameter "
            f"'{sorted(shadowed)[0]}' with a FROM alias",
            anchor,
            hint="rename the alias; a parameter and a FROM item cannot share a name",
        )
    return frozenset(aliases)


def _check_body_scope(
    body: exp.Select, name: str, params: Sequence[_Param], aliases: frozenset[str], anchor: exp.Expr
) -> None:
    """Every name the body reads is one of its parameters or one of its own aliases."""
    known = aliases.union(param.name for param in params)
    for column in body.find_all(exp.Column):
        key = _leftmost(column)
        if key is None:
            continue
        read = _ident_name(column.args.get(key))
        if read in known:
            continue
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"the body of {name}() references '{read}', which is neither a "
            "parameter nor one of its own FROM items",
            anchor,
            hint=_BODY_SCOPE_HINT,
        )


@dataclass(frozen=True)
class _Declared:
    """What a ``<name> <type>`` list is called, and how each rejection advises."""

    noun: str
    shape: str
    plain: str
    once: str


_PARAMETER = _Declared(
    noun="parameter",
    shape=_FUNCTION_HINT,
    plain="a parameter is a name and a type: no defaults, no OUT, no VARIADIC",
    once="one name, one position",
)
_TABLE_COLUMN = _Declared(
    noun="RETURNS TABLE column",
    shape=_TABLE_HINT,
    plain="a RETURNS TABLE column is a name and a type, and nothing else",
    once="one name, one column",
)


def _column_defs(
    nodes: Sequence[exp.Expr], name: str, anchor: exp.Expr, create: exp.Create, kind: _Declared
) -> tuple[_Param, ...]:
    """One ``<name> <type>`` list -- a signature's, or a ``RETURNS TABLE``'s."""
    declared: list[_Param] = []
    for node in nodes:
        if not isinstance(node, exp.ColumnDef) or not isinstance(node.this, exp.Identifier):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"function '{name}' has a {kind.noun} with no name",
                anchor,
                fallback=create,
                hint=kind.shape,
            )
        written = _ident_name(node.this)
        # DEFAULT, OUT/INOUT, VARIADIC and COLLATE all land here.
        constraints = node.args.get("constraints")
        if constraints:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"function '{name}' writes the {kind.noun} '{written}' with "
                f"{_written(constraints[0])}, which is not supported",
                anchor,
                fallback=create,
                hint=kind.plain,
            )
        if any(seen.name == written for seen in declared):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"function '{name}' declares the {kind.noun} '{written}' twice",
                anchor,
                fallback=create,
                hint=kind.once,
            )
        declared.append(_Param(written, _checked_type(node.args.get("kind"), name, anchor)))
    return tuple(declared)


def _table_columns(
    prop: exp.ReturnsProperty, name: str, anchor: exp.Expr, create: exp.Create
) -> tuple[_Param, ...]:
    """The ``RETURNS TABLE(...)`` column list: the shape the call site's alias exposes."""
    schema = prop.this
    nodes = list(schema.expressions) if isinstance(schema, exp.Schema) else []
    if not nodes:
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"function '{name}' declares RETURNS TABLE with no columns",
            anchor,
            fallback=create,
            hint=_TABLE_HINT,
        )
    return _column_defs(nodes, name, anchor, create, _TABLE_COLUMN)


def _define(create: exp.Create) -> _Function:
    """One validated ``CREATE FUNCTION``, body parsed and shape-checked."""
    if create.args.get("replace"):
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            "CREATE OR REPLACE FUNCTION is not supported",
            create,
            hint="a function exists only for the length of one script; "
            "there is nothing to replace",
        )
    if create.args.get("exists"):
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            "CREATE FUNCTION IF NOT EXISTS is not supported",
            create,
            hint="a function exists only for the length of one script; "
            "it never exists already",
        )
    signature, identifier, param_nodes = _function_name(create)
    _check_query_args(
        create, frozenset({"this", "kind", "expression", "properties", "begin"}), "CREATE FUNCTION"
    )
    name = _ident_name(identifier)
    if name in _RESERVED or name.upper() in exp.FUNCTION_BY_NAME:
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"'{name}' is a reserved function name",
            identifier,
            fallback=signature,
            hint="pick a name the dialect does not already use",
        )

    params = _column_defs(param_nodes, name, identifier, create, _PARAMETER)

    returns_prop, language = _properties(create, name, identifier)
    if not language:
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"function '{name}' declares no LANGUAGE sql",
            identifier,
            fallback=create,
            hint="add LANGUAGE sql; a body is a query, and there is no other language",
        )
    if language != "sql":
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"function '{name}' is written in {language}, not sql",
            identifier,
            fallback=create,
            hint="only a LANGUAGE sql body is a query the compiler can inline",
        )
    columns: tuple[_Param, ...] | None = None
    if returns_prop.args.get("is_table"):
        columns = _table_columns(returns_prop, name, identifier, create)
        returns = "TABLE(" + ", ".join(f"{c.name} {c.type}" for c in columns) + ")"
    else:
        node = returns_prop.this if isinstance(returns_prop.this, exp.Expr) else None
        returns = _checked_type(node, name, identifier)

    body = _body_select(_body_text(create, name, identifier), name, identifier, columns)
    aliases = _body_aliases(body, name, params, identifier)
    _check_body_scope(body, name, params, aliases, identifier)
    return _Function(
        name=name,
        params=params,
        returns=returns,
        body=body,
        node=identifier,
        aliases=aliases,
        position=0,
        columns=columns,
    )


def _in_source(err: SqlmpegError, namespace: str, path: Path, anchor: exp.Expr) -> SqlmpegError:
    """A rejection from a package source, said at the call site that reached for it.

    A source file's line numbers mean nothing in the query the reader is
    looking at, so the anchor moves to the call and the message carries the
    file and the line it really came from.
    """
    line, col = _pos(anchor)
    at = f" line {err.line}" if err.line is not None else ""
    return SqlmpegError(
        err.code,
        f"package '{namespace}' source {path}{at}: {err.message}",
        line=line,
        col=col,
        hint=err.hint,
    )


def _source_definitions(package: Package, path: Path, anchor: exp.Expr) -> list[_Function]:
    """Every ``CREATE FUNCTION`` one package source holds, validated.

    A source is a LIBRARY, so it holds definitions and nothing else: a SELECT
    or a COPY in one is a script, and is rejected as one. Every definition it
    yields carries the package's namespace, which is what makes it a library's
    rather than the script's.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"package '{package.namespace}': could not read source {path}: "
            f"{err.strerror or err}",
            anchor,
            hint=f"{package.manifest} lists it as a source",
        ) from err
    try:
        statements = _statements(parse(text))
    except SqlmpegError as err:
        raise _in_source(err, package.namespace, path, anchor) from err

    definitions: list[_Function] = []
    for statement in statements:
        if not (isinstance(statement, exp.Create) and _create_kind(statement) == "FUNCTION"):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"package '{package.namespace}' source {path} holds a statement that "
                "is not a CREATE FUNCTION",
                anchor,
                hint="a package source is a library: it defines functions, and a "
                "query of its own belongs in a script",
            )
        try:
            function = _define(statement)
        except SqlmpegError as err:
            raise _in_source(err, package.namespace, path, anchor) from err
        function.namespace = package.namespace
        # Ahead of every statement of the calling script: a library is already
        # defined when the query that calls it is written.
        function.position = -1
        definitions.append(function)
    return definitions


# -- reading and rewriting nodes ------------------------------------------

# A column's qualifiers, outermost first: the arg holding the LEFTMOST written
# identifier is the alias (or parameter) the column reads.
_QUALIFIERS = ("catalog", "db", "table", "this")


def _leftmost(column: exp.Column) -> str | None:
    """The arg key holding the name `column` reads, or None for a star."""
    for key in _QUALIFIERS:
        node = column.args.get(key)
        if isinstance(node, exp.Identifier):
            return key
    return None


def _path_after(column: exp.Column, key: str) -> list[exp.Identifier]:
    """The identifiers written to the right of `key`, in written order."""
    rest = _QUALIFIERS[_QUALIFIERS.index(key) + 1 :]
    return [
        node for name in rest if isinstance(node := column.args.get(name), exp.Identifier)
    ]


def _preorder(node: exp.Expr, *, stop: type[exp.Expr] | None = None) -> Iterator[exp.Expr]:
    """`node` and its subtree, parents first, not descending into `stop` nodes."""
    yield node
    for child in node.iter_expressions():
        if stop is not None and isinstance(child, stop):
            continue
        yield from _preorder(child, stop=stop)


def _call_name(node: object) -> str:
    """The bare name an anonymous call writes; "" for anything else.

    A namespaced call (``ffmpeg.sine()``) is an ``exp.Dot``, never this, so a
    function can never shadow one.
    """
    return str(node.name).lower() if isinstance(node, exp.Anonymous) else ""


def _rename(body: exp.Expr, mapping: dict[str, str]) -> None:
    """Rewrite every alias the body binds, and every reference to one."""
    for node in body.walk():
        if isinstance(node, exp.TableAlias) and isinstance(node.this, exp.Identifier):
            replacement = mapping.get(_ident_name(node.this))
            if replacement is not None:
                node.this.set("this", replacement)
        elif isinstance(node, exp.Column):
            key = _leftmost(node)
            if key is None:
                continue
            identifier = node.args.get(key)
            if not isinstance(identifier, exp.Identifier):
                continue
            replacement = mapping.get(_ident_name(identifier))
            if replacement is not None:
                identifier.set("this", replacement)


def _accessor(argument: exp.Expr, path: list[exp.Identifier]) -> exp.Expr:
    """`path` read off `argument`, in the shape the same query written by hand takes.

    Over a plain column the dialect spells that as one dotted column
    (``t.tags.language``); over anything else -- a subscript, a filter call --
    as the parenthesized accessor (``(f.audio[1]).codec``).
    """
    if isinstance(argument, exp.Column):
        written = [
            part
            for key in _QUALIFIERS
            if isinstance(part := argument.args.get(key), exp.Identifier)
        ]
        joined = [*written, *path]
        if len(joined) <= len(_QUALIFIERS):
            keys = _QUALIFIERS[len(_QUALIFIERS) - len(joined) :]
            return exp.Column(**{key: part.copy() for key, part in zip(keys, joined)})
    read: exp.Expr = exp.Paren(this=copy.deepcopy(argument))
    for part in path:
        read = exp.Dot(this=read, expression=part.copy())
    return read


def _substitute(body: exp.Select, bindings: dict[str, exp.Expr]) -> None:
    """Replace every parameter reference with the argument bound to it.

    A bare reference becomes the argument itself; a reference with a path off
    it (``track.tags.language``) becomes the accessor form over the argument,
    which is what the same query written by hand parses to.
    """
    for column in list(body.find_all(exp.Column)):
        key = _leftmost(column)
        if key is None:
            continue
        argument = bindings.get(_ident_name(column.args.get(key)))
        if argument is None:
            continue
        if key == "this":
            column.replace(copy.deepcopy(argument))
            continue
        column.replace(_accessor(argument, _path_after(column, key)))


def _and_into(host: exp.Select, predicate: exp.Expr) -> None:
    """Add one conjunct to the host query's WHERE."""
    conjunct = exp.Paren(this=predicate) if isinstance(predicate, exp.Or) else predicate
    where = host.args.get("where")
    if isinstance(where, exp.Where) and isinstance(where.this, exp.Expr):
        where.set("this", exp.And(this=where.this, expression=conjunct))
        return
    host.set("where", exp.Where(this=conjunct))


def _splice(host: exp.Select, body: exp.Select) -> None:
    """Move the body's FROM items and WHERE into the query being compiled."""
    added: list[exp.Join] = []
    from_ = body.args.get("from_")
    if isinstance(from_, exp.From) and isinstance(from_.this, exp.Expr):
        if host.args.get("from_") is None:
            host.set("from_", exp.From(this=from_.this))
        else:
            added.append(exp.Join(this=from_.this))
    added.extend(join for join in body.args.get("joins") or [] if isinstance(join, exp.Join))
    if added:
        host.set("joins", [*(host.args.get("joins") or []), *added])
    where = body.args.get("where")
    if isinstance(where, exp.Where) and isinstance(where.this, exp.Expr):
        _and_into(host, where.this)


def _name_columns(body: exp.Select, columns: tuple[_Param, ...]) -> None:
    """Alias each projection to the column ``RETURNS TABLE`` named for it, in order.

    Naming the projections is what makes the generated CTE expose the declared
    columns: a CTE exposes what its body wrote ``AS``, and nothing else.
    """
    named: list[exp.Expr] = []
    for column, projection in zip(columns, body.expressions):
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        named.append(
            exp.Alias(this=inner, alias=exp.Identifier(this=column.name, quoted=False))
        )
    body.set("expressions", named)


def _query_node(statement: exp.Expr) -> exp.Expr | None:
    """The node whose ``WITH`` holds a statement's CTEs, read as the resolver reads it."""
    node: object = statement
    if isinstance(statement, exp.Copy):
        node = statement.this
    elif isinstance(statement, exp.Create):
        node = statement.args.get("expression")
    while isinstance(node, exp.Subquery | exp.Paren) and isinstance(
        node.this, exp.Select | exp.Union
    ):
        node = node.this
    return node if isinstance(node, exp.Select | exp.Union) else None


def _containing_cte(node: exp.Expr) -> exp.Expr | None:
    """The CTE `node` is written inside, if any."""
    parent = node.parent
    while parent is not None:
        if isinstance(parent, exp.CTE):
            return parent
        parent = parent.parent
    return None


@dataclass(frozen=True)
class _Site:
    """Where a generated CTE goes: which query's ``WITH``, and before which entry.

    A CTE only sees the CTEs written before it, so a call inside a CTE body
    puts its own CTE ahead of that one; a call in the main query appends.
    """

    query: exp.Expr
    before: exp.Expr | None


@dataclass(frozen=True)
class _CallSite:
    """One call to expand: which definition, its arguments, and what it replaces.

    `node` is the node expansion swaps out, which is not always the call: a
    namespaced value call is the ``exp.Dot`` around it, and a row source is
    the whole ``exp.Table`` FROM item.
    """

    function: _Function
    call: exp.Anonymous
    node: exp.Expr

    @property
    def row_source(self) -> bool:
        return isinstance(self.node, exp.Table)


# -- argument types --------------------------------------------------------


def _argument_kind(node: exp.Expr) -> str | None:
    """What the argument's shape says its type is, or None if it says nothing."""
    if isinstance(node, exp.Paren) and isinstance(node.this, exp.Expr):
        return _argument_kind(node.this)
    if isinstance(node, exp.Literal):
        return "text" if node.is_string else "number"
    for shapes, kind in _ARGUMENT_KINDS:
        if isinstance(node, shapes):
            return kind
    # A filter call, bare or namespaced: its output is a stream. Deliberately
    # not every exp.Func -- CASE and CAST are Funcs too, and are values.
    if isinstance(node, exp.Anonymous):
        return "stream"
    if isinstance(node, exp.Dot) and isinstance(node.args.get("expression"), exp.Func):
        return "stream"
    return None


def _declared_kind(declared: str) -> str:
    """What an argument of the declared type has to look like."""
    return "stream" if TYPES[element_type(declared)].kind != "scalar" else declared


# -- the pass --------------------------------------------------------------


@dataclass
class _Expander:
    """One script's worth of definitions, call sites and inlinings."""

    functions: dict[str, _Function] = field(default_factory=dict)
    expansions: list[_Expansion] = field(default_factory=list)
    taken: set[str] = field(default_factory=set)
    budget: int = _EXPANSION_BUDGET
    # Where the statement being walked keeps its generated CTEs.
    site: _Site | None = None
    # The packages a namespaced call may resolve in, and what each one's
    # sources define once they have been read.
    packages: PackageSet | None = None
    exports: dict[str, dict[str, _Function]] = field(default_factory=dict)
    # Whose definitions a BARE call name sees; "" is the script's own.
    scope: str = ""

    # -- entry point ------------------------------------------------------

    def run(self, tree: exp.Expr) -> exp.Expr:
        """`tree` with the definitions lifted out and every call inlined."""
        statements = _statements(tree)
        rest = self._collect(statements)
        if not self.functions and not self._claimed:
            return tree
        self.taken = {
            _ident_name(node)
            for statement in rest
            for node in statement.walk()
            if isinstance(node, exp.Identifier)
        }
        for position, statement in enumerate(rest):
            self._expand_statement(statement, position)
        unused = self._uncalled()
        if unused is not None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"function '{unused.name}' is never called",
                unused.node,
                hint="every function must be called by a later view or COPY; "
                "check the spelling of the name at its call sites",
            )
        if len(rest) == 1:
            return rest[0]
        tree.set("expressions", rest)
        return tree

    def _uncalled(self) -> _Function | None:
        """The first definition that had to be called and was not.

        The script-versus-library asymmetry, in one place: a definition in a
        package SOURCE is a library's, and a library exports more than any one
        query calls, so only the script's own definitions carry the rule.
        """
        for function in (*self.functions.values(), *self._exported()):
            if function.library or function.used:
                continue
            return function
        return None

    def _exported(self) -> Iterator[_Function]:
        """Every definition read out of a package source so far."""
        for functions in self.exports.values():
            yield from functions.values()

    def _collect(self, statements: list[exp.Expr]) -> list[exp.Expr]:
        """Read every definition out of the script; return what is left to compile."""
        rest: list[exp.Expr] = []
        written = False
        for statement in statements:
            if isinstance(statement, exp.Create) and _create_kind(statement) == "FUNCTION":
                if written:
                    raise _error(
                        ErrorCode.UNSUPPORTED_SQL,
                        "a CREATE FUNCTION may not follow a COPY",
                        statement,
                        hint="define every function before the first COPY",
                    )
                function = _define(statement)
                if function.name in self.functions:
                    raise _error(
                        ErrorCode.UNSUPPORTED_SQL,
                        f"function '{function.name}' is defined twice",
                        function.node,
                        hint="one name, one signature; functions are not overloaded",
                    )
                function.position = len(rest)
                self.functions[function.name] = function
                continue
            written = written or isinstance(statement, exp.Copy)
            rest.append(statement)
        return rest

    # -- packages ---------------------------------------------------------

    @property
    def _claimed(self) -> bool:
        """True when some package claims a namespace this compile may resolve in."""
        return self.packages is not None and bool(self.packages.packages)

    @contextmanager
    def _scoped(self, namespace: str) -> Iterator[None]:
        """Whose definitions a bare call name sees while `namespace`'s body expands.

        A package source is its own flat namespace: a library body calling
        ``helper()`` means the package's own helper, never the script's, and a
        script body never sees a package's.
        """
        previous = self.scope
        self.scope = namespace
        try:
            yield
        finally:
            self.scope = previous

    def _visible(self, name: str) -> _Function | None:
        """The definition a BARE call name resolves to where it is written."""
        if self.scope:
            return self.exports.get(self.scope, {}).get(name)
        return self.functions.get(name)

    def _member(self, namespace: str, call: exp.Anonymous, anchor: exp.Expr) -> _Function | None:
        """The definition `namespace.<call>` names, or None if there is no project.

        None keeps a qualified call exactly what it was before packages
        existed: outside a project, ``me.pick(...)`` is not a package call and
        the rejection it earns downstream is the one it has always earned.
        """
        packages = self.packages
        if packages is None or not packages.packages:
            return None
        name = _call_name(call)
        package = packages.get(namespace)
        if package is None:
            known = packages.namespaces()
            near = difflib.get_close_matches(namespace, list(known), n=1, cutoff=0.6)
            raise _error(
                ErrorCode.UNKNOWN_FUNCTION,
                f"unknown namespace '{namespace}'",
                anchor,
                hint=f"did you mean {near[0]}.{name}()?"
                if near
                else f"namespaces this project can call: {', '.join(known)}",
            )
        exports = self._exports_of(package, anchor)
        function = exports.get(name)
        if function is None:
            near = difflib.get_close_matches(name, sorted(exports), n=1, cutoff=0.6)
            raise _error(
                ErrorCode.UNKNOWN_FUNCTION,
                f"package '{namespace}' has no function '{name}'",
                anchor,
                hint=f"did you mean {namespace}.{near[0]}()?"
                if near
                else f"{namespace} defines: {', '.join(sorted(exports)) or 'nothing'}",
            )
        return function

    def _exports_of(self, package: Package, anchor: exp.Expr) -> dict[str, _Function]:
        """Every definition `package`'s sources hold, read and parsed once per compile.

        Read on FIRST use of the namespace rather than up front: a package
        whose sources this query never calls into costs nothing, and a broken
        source only blocks the queries that reach for it.
        """
        cached = self.exports.get(package.namespace)
        if cached is not None:
            return cached
        exports: dict[str, _Function] = {}
        origin: dict[str, Path] = {}
        # Registered before the sources are read so a body calling a sibling
        # resolves against the same dict this loop is filling.
        self.exports[package.namespace] = exports
        for path in package.sources:
            for function in _source_definitions(package, path, anchor):
                if function.name in exports:
                    raise _error(
                        ErrorCode.UNSUPPORTED_SQL,
                        f"package '{package.namespace}' defines '{function.name}' twice: "
                        f"{origin[function.name]} and {path}",
                        anchor,
                        hint="one name, one definition, across all of a package's sources",
                    )
                exports[function.name] = function
                origin[function.name] = path
        return exports

    # -- finding calls ----------------------------------------------------

    def _expand_statement(self, statement: exp.Expr, position: int) -> None:
        """Inline every call the statement writes, each into the query around it."""
        query = _query_node(statement)
        selects = [node for node in _preorder(statement) if isinstance(node, exp.Select)]
        for select in selects:
            self.site = self._site_of(query, select)
            self._expand_within(select, select, position, ())
        if isinstance(statement, exp.Copy) and selects:
            # A fan-out destination is written over the query's rows, so its
            # calls expand into that query.
            self.site = self._site_of(query, selects[0])
            for destination in statement.args.get("files") or []:
                if isinstance(destination, exp.Expr):
                    self._expand_within(destination, selects[0], position, ())
        self.site = None

    def _site_of(self, query: exp.Expr | None, select: exp.Select) -> _Site:
        """Where a call written in `select` puts the CTE it becomes."""
        if query is None:
            # A statement shaped like nothing the resolver knows; it is about
            # to be rejected, and the WITH goes somewhere it can be seen.
            return _Site(select, None)
        return _Site(query, _containing_cte(select))

    def _expand_within(
        self, root: exp.Expr, host: exp.Select, position: int, stack: tuple[str, ...]
    ) -> exp.Expr:
        """Inline the calls `root` writes, outermost first, into `host`.

        Returns what `root` became: a root that IS a call is replaced outright,
        and the scan continues over what took its place.
        """
        while True:
            site = self._next_call(root, position)
            if site is None:
                return root
            if site.row_source:
                self._expand_row_source(site, host, position, stack)
                continue
            replacement = self._expand_call(site, host, position, stack)
            site.node.replace(replacement)
            if site.node is root:
                root = replacement

    def _next_call(self, root: exp.Expr, position: int) -> _CallSite | None:
        """The first call to a defined function in `root`'s own query, if any.

        A FROM item comes back as the ``exp.Table`` around the call, since that
        whole item is what a row source replaces, and a namespaced value call
        as the ``exp.Dot`` that qualifies it.
        """
        # A nested SELECT is its own query and gets its own pass, so the scan
        # stops at one -- but not at `root` itself, which is where it starts.
        stop = exp.Select if isinstance(root, exp.Select) else None
        for node in _preorder(root, stop=stop):
            if isinstance(node, exp.Table):
                site = self._row_source_site(node)
            elif isinstance(node, exp.Dot):
                site = self._qualified_site(node)
            elif isinstance(node, exp.Anonymous):
                site = self._bare_site(node)
            else:
                continue
            if site is None:
                continue
            self._check_shape(site)
            self._check_defined(site.function, site.node, position)
            return site
        return None

    def _bare_site(self, call: exp.Anonymous) -> _CallSite | None:
        """An unqualified ``fn(...)`` value call, if something in scope defines `fn`."""
        # A qualifier owns the name under it: the Anonymous inside a Dot or a
        # FROM item was already offered to the branch that reads the qualifier,
        # and a definition may not shadow what `ffmpeg.` or `me.` resolves.
        if isinstance(call.parent, exp.Dot | exp.Table):
            return None
        function = self._visible(_call_name(call))
        return None if function is None else _CallSite(function, call, call)

    def _qualified_site(self, node: exp.Dot) -> _CallSite | None:
        """A ``ns.fn(...)`` value call, if `ns` is a namespace a package claims."""
        call, qualifier = node.expression, node.this
        if not isinstance(call, exp.Anonymous) or not isinstance(qualifier, exp.Identifier):
            return None
        namespace = _ident_name(qualifier)
        if namespace in RESERVED_NAMESPACES:  # a filter or a macro; lower resolves it
            return None
        function = self._member(namespace, call, node)
        return None if function is None else _CallSite(function, call, node)

    def _row_source_site(self, item: exp.Table) -> _CallSite | None:
        """A FROM-position call, bare or namespaced, if something defines it."""
        call = item.this
        if not isinstance(call, exp.Anonymous):
            return None
        if item.args.get("catalog"):  # `x.ns.fn()` names a schema, not a namespace
            return None
        db = item.args.get("db")
        if not isinstance(db, exp.Identifier):
            function = self._visible(_call_name(call))
        else:
            namespace = _ident_name(db)
            if namespace in RESERVED_NAMESPACES:  # `ffmpeg.<source>()`; lower resolves it
                return None
            function = self._member(namespace, call, item)
        return None if function is None else _CallSite(function, call, item)

    def _check_shape(self, site: _CallSite) -> None:
        """A call has to be written where what the function returns belongs."""
        function = site.function
        if site.row_source and not function.returns_rows:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"function '{function.qualified}' returns a value, not a table",
                site.node,
                hint="call it where its value belongs: a SELECT column, a "
                "WHERE predicate, a tag column",
            )
        if not site.row_source and function.returns_rows:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"function '{function.qualified}' returns a table, not a value",
                site.node,
                hint=f"call it in FROM: FROM {function.qualified}(...) AS t, then read "
                f"{_listed(function.columns)} off the alias; a field read off the "
                "call itself reads it once per field, which would mint one input "
                "per read",
            )

    def _check_defined(self, function: _Function, node: exp.Expr, position: int) -> None:
        """A call may only name a function an earlier statement defined.

        A library's definitions were written before the script was, so the
        ordering rule is the script's own alone.
        """
        if function.library or function.position <= position:
            return
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"function '{function.name}' is used before it is defined",
            node,
            hint="define every function before the statements that call it",
        )

    # -- inlining one call ------------------------------------------------

    def _enter(self, function: _Function, call: exp.Expr, stack: tuple[str, ...]) -> None:
        """What every inlining costs: the cycle check, the budget, the used mark."""
        key = function.qualified
        if key in stack:
            chain = " -> ".join([*stack[stack.index(key) :], key])
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"function '{key}' is recursive: {chain}",
                call,
                hint="a filtergraph is acyclic; a function may not call itself, "
                "directly or through another",
            )
        self.budget -= 1
        if self.budget < 0:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"this script inlines more than {_EXPANSION_BUDGET} function calls",
                call,
                hint="flatten the nesting: fewer functions calling functions",
            )
        function.used = True

    def _arguments(
        self, function: _Function, call: exp.Anonymous, host: exp.Select, position: int
    ) -> list[exp.Expr]:
        """The written arguments, expanded and checked against the signature.

        An argument is the CALLER's text, so its own calls expand in the
        caller's context -- f(f(x)) is nesting, never recursion.
        """
        raw = [node for node in call.expressions if isinstance(node, exp.Expr)]
        arguments = [self._expand_within(node, host, position, ()) for node in raw]
        self._check_arguments(function, call, arguments)
        return arguments

    def _instance(
        self, site: _CallSite, arguments: list[exp.Expr]
    ) -> tuple[exp.Select, int]:
        """One private copy of the body: aliases renamed, positions stamped, arguments bound."""
        function = site.function
        body = copy.deepcopy(function.body)
        index = len(self.expansions)
        line, col = _pos(site.node)
        self.expansions.append(_Expansion(function.qualified, line, col))
        _rename(body, self._fresh_aliases(function, index))
        self._stamp(body, index)
        _substitute(body, {p.name: a for p, a in zip(function.params, arguments)})
        return body, index

    def _expand_call(
        self, site: _CallSite, host: exp.Select, position: int, stack: tuple[str, ...]
    ) -> exp.Expr:
        """The expression the call stands for, with the body spliced into `host`."""
        function = site.function
        self._enter(function, site.node, stack)
        arguments = self._arguments(function, site.call, host, position)
        body, _ = self._instance(site, arguments)
        # The body is the package's or the script's text, so its own bare calls
        # resolve where it was written, not where it was called from.
        with self._scoped(function.namespace):
            self._expand_within(body, host, position, (*stack, function.qualified))
        _splice(host, body)
        projection: exp.Expr = body.expressions[0]
        inner = projection.this if isinstance(projection, exp.Alias) else None
        return inner if isinstance(inner, exp.Expr) else projection

    def _expand_row_source(
        self, site: _CallSite, host: exp.Select, position: int, stack: tuple[str, ...]
    ) -> None:
        """Turn one FROM-position call into a generated CTE, and read that instead.

        The body becomes a relation of its own, which is what makes the call
        site carry the body's ROW COUNT: splicing its FROM items into the host
        would hand the host a product it never wrote.
        """
        item = site.node
        if not isinstance(item, exp.Table):  # unreachable: only a Table is a row source
            return
        function = site.function
        # `db` is the namespace qualifier of a package call, and nothing else
        # reaches here with one: an unclaimed qualifier never resolves.
        _check_query_args(item, frozenset({"this", "alias", "db"}), "a table function call")
        self._enter(function, item, stack)
        arguments = self._arguments(function, site.call, host, position)
        body, index = self._instance(site, arguments)
        # The body is its own query now, so its own calls expand into it.
        with self._scoped(function.namespace):
            self._expand_within(body, body, position, (*stack, function.qualified))
        _name_columns(body, function.columns or ())
        name = self._fresh_name(f"{function.name}_{index + 1}")
        self._add_cte(name, body)

        identifier = exp.Identifier(this=name, quoted=False)
        line, col = _pos(item)
        identifier.meta.update({"line": line, "col": col, "start": 0, "end": 0})
        alias = item.args.get("alias")
        if alias is None:
            # Unwritten, the alias is the function's own name, as Postgres has it.
            alias = exp.TableAlias(this=exp.Identifier(this=function.name, quoted=False))
        item.replace(exp.Table(this=identifier, alias=alias))

    def _fresh_name(self, base: str) -> str:
        """A name for a generated CTE that nothing in the script has claimed."""
        name = base
        while name in self.taken:
            name += "_"
        self.taken.add(name)
        return name

    def _add_cte(self, name: str, body: exp.Select) -> None:
        """Bind `body` as one more CTE of the query the call was written in."""
        site = self.site
        if site is None:  # unreachable: every expansion runs under one statement
            raise _error(ErrorCode.UNSUPPORTED_SQL, "a table function has no query to join", body)
        with_ = site.query.args.get("with_")
        if not isinstance(with_, exp.With):
            with_ = exp.With(expressions=[])
            site.query.set("with_", with_)
        entries = list(with_.expressions)
        at = len(entries)
        if site.before is not None:
            for index, entry in enumerate(entries):
                if entry is site.before:
                    at = index
                    break
        alias = exp.TableAlias(this=exp.Identifier(this=name, quoted=False))
        entries.insert(at, exp.CTE(this=body, alias=alias))
        with_.set("expressions", entries)

    def _fresh_aliases(self, function: _Function, index: int) -> dict[str, str]:
        """A name per body alias that nothing in the script has already claimed."""
        mapping: dict[str, str] = {}
        for alias in sorted(function.aliases):
            fresh = f"{function.name}_{index + 1}_{alias}"
            while fresh in self.taken:
                fresh += "_"
            self.taken.add(fresh)
            mapping[alias] = fresh
        return mapping

    def _check_arguments(
        self, function: _Function, call: exp.Anonymous, arguments: list[exp.Expr]
    ) -> None:
        """Arity and what each argument's shape says, against the signature."""
        if len(arguments) != len(function.params):
            plural = "" if len(arguments) == 1 else "s"
            raise _error(
                ErrorCode.UDF_ARG_TYPE,
                f"{function.qualified}() got {len(arguments)} argument{plural}, but it "
                f"declares {len(function.params)}",
                call,
                hint=function.signature,
            )
        for param, argument in zip(function.params, arguments):
            if _call_name(argument) == _INPUT:
                raise _error(
                    ErrorCode.UDF_ARG_TYPE,
                    f"{function.qualified}() cannot take input() as its '{param.name}' "
                    "argument: input() mints a FROM item, not a value",
                    argument,
                    fallback=call,
                    hint=_ARG_HINT,
                )
            written = _argument_kind(argument)
            if written is None or written == _declared_kind(param.type):
                continue
            raise _error(
                ErrorCode.UDF_ARG_TYPE,
                f"{function.qualified}() takes {param.type} as its '{param.name}' "
                f"argument, got {_KIND_NAMES.get(written, written)}",
                argument,
                fallback=call,
                hint=function.signature,
            )

    # -- positions --------------------------------------------------------

    def _stamp(self, body: exp.Expr, index: int) -> None:
        """Move the body's own line numbers into this expansion's private range."""
        base = _BODY_LINE_BASE + index * _BODY_LINE_SPAN
        for node in body.walk():
            line = node.meta.get("line")
            if isinstance(line, int):
                node.meta["line"] = base + min(line, _BODY_LINE_SPAN - 1)

    def _source(self, line: int) -> tuple[_Expansion, int, int, int] | None:
        """The expansion `line` came from, its body line, and the call site.

        The call site of a nested expansion is itself body text, so the walk up
        continues until it reaches a line the script actually has.
        """
        index, body_line = divmod(line - _BODY_LINE_BASE, _BODY_LINE_SPAN)
        if not 0 <= index < len(self.expansions):
            return None
        expansion = self.expansions[index]
        site = expansion
        for _ in range(len(self.expansions)):
            if site.line < _BODY_LINE_BASE:
                return expansion, body_line, site.line, site.col
            outer = divmod(site.line - _BODY_LINE_BASE, _BODY_LINE_SPAN)[0]
            if not 0 <= outer < len(self.expansions):
                break
            site = self.expansions[outer]
        return expansion, body_line, 1, 1

    def translate(self, err: SqlmpegError) -> SqlmpegError:
        """A rejection that landed on body text, said at the call site."""
        if err.line is None or err.line < _BODY_LINE_BASE:
            return err
        found = self._source(err.line)
        if found is None:
            return SqlmpegError(err.code, err.message, line=1, col=1, hint=err.hint)
        expansion, body_line, line, col = found
        return SqlmpegError(
            err.code,
            f"in the body of {expansion.name}() at body line {body_line}: {err.message}",
            line=line,
            col=col,
            hint=err.hint,
        )

    def settle(self, script: exp.Expr) -> None:
        """Flatten every stamped position onto its call site.

        Resolve is the last pass that can tell body text apart, so after it
        succeeds the expansions are ordinary query nodes and must anchor
        somewhere the reader can see.
        """
        for node in script.walk():
            line = node.meta.get("line")
            if not isinstance(line, int) or line < _BODY_LINE_BASE:
                continue
            found = self._source(line)
            node.meta["line"] = 1 if found is None else found[2]
            node.meta["col"] = 1 if found is None else found[3]
            node.meta["start"] = 0
            node.meta["end"] = 0
