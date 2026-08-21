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

Not here yet: ``RETURNS TABLE(...)``, which is a row source and belongs in
FROM. It parses far enough to be told apart and rejected by name.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
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
    """

    name: str
    params: tuple[_Param, ...]
    returns: str
    body: exp.Select
    node: exp.Expr
    aliases: frozenset[str]
    position: int
    used: bool = False

    @property
    def signature(self) -> str:
        written = ", ".join(f"{p.name} {p.type}" for p in self.params)
        return f"{self.name}({written}) RETURNS {self.returns}"


@dataclass(frozen=True)
class _Expansion:
    """One inlining: which function, and where it was written."""

    name: str
    line: int
    col: int


@contextmanager
def expanded(tree: exp.Expression) -> Iterator[exp.Expr]:
    """Yield `tree` with every function definition lifted out and every call inlined.

    A rejection raised while the block runs is re-anchored: one landing inside
    an expanded body comes back pointing at the call site, saying which body
    line it came from. On a clean exit the body line range is flattened to the
    call site, so nothing downstream can report a position the reader cannot
    find.

    A script with no ``CREATE FUNCTION`` in it yields the tree untouched.
    """
    expander = _Expander()
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


def _properties(create: exp.Create, name: str, anchor: exp.Expr) -> tuple[exp.Expr | None, str]:
    """The ``RETURNS`` node and the declared language; anything else is rejected."""
    returns: exp.Expr | None = None
    language = ""
    properties = create.args.get("properties")
    if isinstance(properties, exp.Properties):
        for prop in properties.expressions:
            if isinstance(prop, exp.ReturnsProperty):
                if prop.args.get("is_table"):
                    raise _error(
                        ErrorCode.UNSUPPORTED_SQL,
                        f"function '{name}' returns a table, which is not supported yet",
                        anchor,
                        hint="a value-returning function is called where its value "
                        "belongs; RETURNS TABLE(...) is a row source and has no "
                        "FROM form yet",
                    )
                returns = prop.this if isinstance(prop.this, exp.Expr) else None
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


def _body_select(text: str, name: str, anchor: exp.Expr) -> exp.Select:
    """Parse and shape-check one body: a single SELECT of a single column."""
    try:
        parsed = parse(text)
    except SqlmpegError as err:
        raise _reanchor(err, name, anchor) from err
    if not isinstance(parsed, exp.Select):
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"the body of {name}() is not one SELECT",
            anchor,
            hint="a value-returning function's body is one SELECT of one column",
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
    if len(parsed.expressions) != 1:
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"the body of {name}() selects {len(parsed.expressions)} columns, "
            "and a value is one column",
            anchor,
            hint="a value-returning function's body is one SELECT of one column",
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

    params: list[_Param] = []
    for node in param_nodes:
        if not isinstance(node, exp.ColumnDef) or not isinstance(node.this, exp.Identifier):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"function '{name}' has a parameter with no name",
                identifier,
                fallback=create,
                hint=_FUNCTION_HINT,
            )
        param_name = _ident_name(node.this)
        # DEFAULT, OUT/INOUT, VARIADIC and COLLATE all land here.
        constraints = node.args.get("constraints")
        if constraints:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"function '{name}' writes the parameter '{param_name}' with "
                f"{_written(constraints[0])}, which is not supported",
                identifier,
                fallback=create,
                hint="a parameter is a name and a type: no defaults, no OUT, no VARIADIC",
            )
        if any(param.name == param_name for param in params):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"function '{name}' declares the parameter '{param_name}' twice",
                identifier,
                fallback=create,
                hint="one name, one position",
            )
        params.append(_Param(param_name, _checked_type(node.args.get("kind"), name, identifier)))

    returns_node, language = _properties(create, name, identifier)
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
    returns = _checked_type(returns_node, name, identifier)

    body = _body_select(_body_text(create, name, identifier), name, identifier)
    aliases = _body_aliases(body, name, params, identifier)
    _check_body_scope(body, name, params, aliases, identifier)
    return _Function(
        name=name,
        params=tuple(params),
        returns=returns,
        body=body,
        node=identifier,
        aliases=aliases,
        position=0,
    )


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

    # -- entry point ------------------------------------------------------

    def run(self, tree: exp.Expr) -> exp.Expr:
        """`tree` with the definitions lifted out and every call inlined."""
        statements = _statements(tree)
        rest = self._collect(statements)
        if not self.functions:
            return tree
        self.taken = {
            _ident_name(node)
            for statement in rest
            for node in statement.walk()
            if isinstance(node, exp.Identifier)
        }
        for position, statement in enumerate(rest):
            self._expand_statement(statement, position)
        for function in self.functions.values():
            if function.used:
                continue
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"function '{function.name}' is never called",
                function.node,
                hint="every function must be called by a later view or COPY; "
                "check the spelling of the name at its call sites",
            )
        if len(rest) == 1:
            return rest[0]
        tree.set("expressions", rest)
        return tree

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

    # -- finding calls ----------------------------------------------------

    def _expand_statement(self, statement: exp.Expr, position: int) -> None:
        """Inline every call the statement writes, each into the query around it."""
        selects = [node for node in _preorder(statement) if isinstance(node, exp.Select)]
        for select in selects:
            self._expand_within(select, select, position, ())
        if isinstance(statement, exp.Copy) and selects:
            # A fan-out destination is written over the query's rows, so its
            # calls expand into that query.
            for destination in statement.args.get("files") or []:
                if isinstance(destination, exp.Expr):
                    self._expand_within(destination, selects[0], position, ())

    def _expand_within(
        self, root: exp.Expr, host: exp.Select, position: int, stack: tuple[str, ...]
    ) -> exp.Expr:
        """Inline the calls `root` writes, outermost first, into `host`.

        Returns what `root` became: a root that IS a call is replaced outright,
        and the scan continues over what took its place.
        """
        while True:
            call = self._next_call(root, position)
            if call is None:
                return root
            replacement = self._expand_call(call, host, position, stack)
            call.replace(replacement)
            if call is root:
                root = replacement

    def _next_call(self, root: exp.Expr, position: int) -> exp.Anonymous | None:
        """The first call to a defined function in `root`'s own query, if any."""
        # A nested SELECT is its own query and gets its own pass, so the scan
        # stops at one -- but not at `root` itself, which is where it starts.
        stop = exp.Select if isinstance(root, exp.Select) else None
        for node in _preorder(root, stop=stop):
            if isinstance(node, exp.Table):
                function = self.functions.get(_call_name(node.this))
                if function is not None:
                    raise _error(
                        ErrorCode.UNSUPPORTED_SQL,
                        f"function '{function.name}' returns a value, not a table",
                        node,
                        hint="call it where its value belongs: a SELECT column, a "
                        "WHERE predicate, a tag column",
                    )
                continue
            if not isinstance(node, exp.Anonymous):
                continue
            function = self.functions.get(_call_name(node))
            if function is None:
                continue
            if function.position > position:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"function '{function.name}' is used before it is defined",
                    node,
                    hint="define every function before the statements that call it",
                )
            return node
        return None

    # -- inlining one call ------------------------------------------------

    def _expand_call(
        self, call: exp.Anonymous, host: exp.Select, position: int, stack: tuple[str, ...]
    ) -> exp.Expr:
        """The expression `call` stands for, with the body spliced into `host`."""
        function = self.functions[_call_name(call)]
        if function.name in stack:
            chain = " -> ".join([*stack[stack.index(function.name) :], function.name])
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"function '{function.name}' is recursive: {chain}",
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

        # An argument is the CALLER's text, so its own calls expand in the
        # caller's context -- f(f(x)) is nesting, never recursion.
        raw = [node for node in call.expressions if isinstance(node, exp.Expr)]
        arguments = [self._expand_within(node, host, position, ()) for node in raw]
        self._check_arguments(function, call, arguments)

        body = copy.deepcopy(function.body)
        index = len(self.expansions)
        line, col = _pos(call)
        self.expansions.append(_Expansion(function.name, line, col))
        _rename(body, self._fresh_aliases(function, index))
        self._stamp(body, index)
        _substitute(body, {p.name: a for p, a in zip(function.params, arguments)})
        self._expand_within(body, host, position, (*stack, function.name))
        _splice(host, body)
        projection: exp.Expr = body.expressions[0]
        inner = projection.this if isinstance(projection, exp.Alias) else None
        return inner if isinstance(inner, exp.Expr) else projection

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
                f"{function.name}() got {len(arguments)} argument{plural}, but it "
                f"declares {len(function.params)}",
                call,
                hint=function.signature,
            )
        for param, argument in zip(function.params, arguments):
            if _call_name(argument) == _INPUT:
                raise _error(
                    ErrorCode.UDF_ARG_TYPE,
                    f"{function.name}() cannot take input() as its '{param.name}' "
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
                f"{function.name}() takes {param.type} as its '{param.name}' "
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
