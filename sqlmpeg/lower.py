"""Lower pass: a resolved query becomes an IR :class:`~sqlmpeg.ir.Graph`.

This is pass 2 of the compiler (see "Architecture" in sqlmpeg-project.md). It
assumes :func:`sqlmpeg.parser.resolve` already accepted the query, so every
rejection raised here is either a check resolve deliberately left to lowering
(CTE column names, function names, argument types, probed stream bounds) or a
defensive re-check.

RFC-001 (stream-aware) shapes this pass: the top-level SELECT list IS the
output stream list, and every value flowing through lowering is a *typed*
stream (``video``, ``audio``, ``subtitle`` or ``data``), never an untyped
"frame".

Passthrough-only stream types (RFC-004)
---------------------------------------
``subtitle`` and ``data`` streams get the exact same surface as video/audio —
``a.subtitle[1]``, the bare array ``a.data``, a CTE column, a star expansion —
but an ffmpeg filtergraph carries video and audio only, so they may never be a
filter input. Three rejections enforce that, all keyed off ``_PASSTHROUGH_ONLY``:

* as a function argument, in EITHER tier -> ``UDF_ARG_TYPE``
  (:meth:`_Lowerer._reject_passthrough_args`);
* under a **CTE's** WHERE time range that is actually consumed in that branch ->
  ``UNSUPPORTED_SQL`` (:meth:`_Lowerer._access`). An INPUT alias's WHERE is not
  a filtergraph trim at all any more (see the WHERE bullet below), so it carries
  captions perfectly well; a CTE's window IS a filtergraph trim, so for it the
  rejection is permanent;
* as a UNION ALL branch column -> ``UNSUPPORTED_SQL``
  (:meth:`_Lowerer._check_concat_columns`; ``concat`` has ``v``/``a`` pads only).

Everything else about them is ordinary: they lower to ``"src:<alias>:s:<k>"`` /
``"src:<alias>:d:<k>"`` refs, carry provenance (a caption track's ``language``
tag rides the same passthrough metadata path an audio track's does), and become
``Output`` rows that split and emit treat as bare ``-map``s.

``SELECT *`` and ``<alias>.*`` (RFC-004)
----------------------------------------
A star is a column GENERATOR, not an expression: :meth:`_Lowerer._expand_star`
turns it into one passthrough column per stream. A bare ``*`` covers every FROM
alias in FROM order; ``<alias>.*`` covers one. Within an INPUT alias the order
is FILE order (probe order, all four stream types interleaved as the container
has them) and the expansion is splat tier — it needs a probe, so an unreadable
input is ``INPUT_NOT_FOUND``, the same policy a bare ``a.audio`` has. Within a
CTE it is column order, array columns splatting, and no probe is consulted at
all: the CTE's shape was fixed when its body lowered.

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
* ``WHERE <alias>.t BETWEEN x AND y`` records a per-alias time range, and where
  that window lands depends on what the alias is (RFC-004's input-seek
  amendment):

  - an INPUT alias owns its own ``-i`` slot and has at most one window in the
    whole query, so the window is recorded as ``Graph.input_trims[alias]`` and
    emit renders it as ``-ss <x> -to <y>`` in front of that ``-i``. NO filter
    node is spliced: the stream refs come out of lowering untouched, so a
    trimmed column that nothing else filters stays a PASSTHROUGH and is
    stream-copied. The seek applies to the WHOLE input — every stream of that
    alias, including subtitle/data streams and streams the SELECT list never
    mentions (harmless: an unselected stream is never ``-map``ped) — which is
    exactly what makes a trimmed caption track possible. Accuracy: decoded
    (filtered/re-encoded) streams are frame-accurate; stream-copied ones snap
    back to the preceding keyframe and may start up to a GOP early.
  - a CTE alias names a filtergraph pad, not an input, so its window still
    lowers to a filter trim: spliced lazily, the first time a stream of that
    CTE is consumed, and memoized per stream, so every consumer of the same
    stream shares one ``trim``+``setpts`` (video) / ``atrim``+``asetpts``
    (audio) pair. Being a filtergraph trim, it cannot carry captions.
* Each projection lowers bottom-up to one :class:`~sqlmpeg.ir.Output` per
  stream it carries (an array column splats into consecutive Outputs). A
  stdlib call type-checks its arguments against
  ``stdlib.FUNCTIONS[name].variants`` (kinds are ``video``/``audio``/``num``/
  ``str``) and then delegates node creation to the spec's ``expand``
  (guardrail #4: no per-function lowering logic lives here).

Two function tiers (RFC-003)
----------------------------
A call name resolves against the stdlib FIRST and against the ``registry`` —
the filter set of the ffmpeg on PATH — only if the stdlib does not have it, so
tier 1 wins every collision (``scale``, ``crop`` and ``overlay`` are both
stdlib functions and real ffmpeg filters, and the stdlib's argument order is
the documented one). Tier 2 is deliberately machine-dependent: what compiles
depends on what that ffmpeg reports, and a None registry (no ffmpeg, or
``--portable``) means the whole tier is simply absent — an unknown name, not
an INTERNAL error.

* A tier-2 call takes its STREAM INPUTS positionally, count and types straight
  from the pad signature (``gblur`` is ``V->V``, ``xfade`` is ``VV->V``), and
  every option by name (``sigma => 5``). Its node is an ordinary
  :class:`~sqlmpeg.ir.Node`, so split, emit and the goldens neither know nor
  care that it came from introspection.
* A tier-1 call may ALSO carry trailing named arguments, validated against
  ``FuncSpec.named_target``'s options (``blur`` -> ``gblur``) and merged into
  the single node its expansion produced, after the positionally-mapped args
  and in written order. A macro spec (``named_target`` None: ``blur_regions``)
  has no single target and rejects them.
* Both use the same two codes — ``UNKNOWN_FILTER_OPTION`` (did-you-mean over
  that filter's REAL options) and ``FILTER_OPTION_TYPE`` (the introspected
  type, plus the range or the constant list) — and the same rule: named
  arguments are your installed ffmpeg, so without one they are rejected rather
  than guessed at.
* Broadcasting and zipping are type-driven and tier-agnostic: they run off the
  stream-argument POSITIONS, which come from a stdlib variant or from a pad
  signature, so ``volume(a.audio, 0.5)`` and ``anlmdn(a.audio, s => 0.01)``
  expand identically.
* A UNION ALL (top level or inside a CTE) lowers each branch and joins them
  with one ``concat`` node. Branch column counts, types and order must match
  exactly (``CONCAT_MISMATCH``); concat inputs interleave per ffmpeg's segment
  contract — all of segment 1's videos, then its audios, then segment 2's, ...
  — and its output pads are ``["video"]*v + ["audio"]*a``, mapped back to the
  branch's own column order.

Broadcasting (plan 020) makes a bare ``a.video`` / ``a.audio`` the WHOLE array
of that input's streams, in probe order. Splatted into a SELECT list it becomes
one Output per element; handed to a function it expands the call elementwise
(a fresh subgraph per element); stored in a CTE column it keeps its length, so
``<cte>.<name>`` splats or broadcasts again and ``<cte>.<name>[k]`` picks one
element (1-based, bounds-checked statically — no probe needed at that point).
Arrays are purely a lowering concept: the spread happens here, so the IR, the
split pass and emit only ever see scalar streams.

Probing (``probes``, keyed by alias) only ever ADDS validation: an explicit
subscript lowers to the same ref whether or not the input could be probed, but
a probed input bounds-checks it (``STREAM_NOT_FOUND``). Enumerating an array is
the one thing that cannot be done symbolically — a bare array over an input
that could not be probed is ``INPUT_NOT_FOUND``. Two arrays in one call zip and
must agree on length (``BROADCAST_MISMATCH``); scalar arguments repeat.

Provenance: a stream derived 1:1 from one probed source stream — a passthrough,
or a chain of single-stream-input calls, WHERE trims included — carries that
stream's language/title tags into ``Output.metadata`` (an ffmpeg-stamped
``language=und`` carries no information and is dropped), so a broadcast
``reverb(a.audio, 0.3)`` keeps every track's language tag. A call over two or
more streams (``amix``, ``overlay``) and a ``concat`` pad (fed by one stream
per UNION ALL segment) are the other kind of join: each threads the tag only
when EVERY stream feeding it carries the same non-empty one, so mixing two
English tracks keeps ``language=eng``, but mixing English with French, or with
an untagged stream, keeps neither. Same rule, one function: ``_agreed_source``.

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
* A named argument is an ``exp.Kwarg(this=Var(name), expression=value)``. The
  ``Var`` carries NO token position (the same gap sink option names have), so
  every rejection about one anchors on the VALUE — a literal, which does have a
  position — and falls back to the call itself for a ``Boolean`` value, which
  does not.
* Postgres has a builtin ``OVERLAY(x PLACING y FROM n FOR m)``, and sqlglot
  parses ``overlay(...)`` with that grammar: a ``=>`` inside it is a PARSE_ERROR
  before lowering ever sees the call, so ``overlay`` is the one stdlib function
  whose named extras are unreachable. Its underlying filter is reachable as a
  tier-2 ``overlay`` call only if the stdlib entry is not shadowing it — which
  it is. (Both facts are surface-level sqlglot behavior, not a lowering rule.)
* A COPY option value (``WITH (crf 20)``) is NOT always a ``Literal``: ``true``
  / ``false`` arrive as ``exp.Boolean``, a bare word as ``exp.Var``, a
  double-quoted word as ``exp.Identifier``, ``NULL`` as ``exp.Null``.
  :func:`_sink_value` normalizes the first three shapes to python values and
  hands everything else to the option table as an unrepresentable value, so
  the SINK_OPTION_TYPE message and hint still come from the table.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from sqlglot import exp

from sqlmpeg.errors import ErrorCode, SqlmpegError
from sqlmpeg.ir import FrameRef, Graph, Node, Output, Sink, StreamType
from sqlmpeg.parser import (
    RawSink,
    Resolved,
    _pos,
    kwarg_name,
    star_qualifier,
    subscript_index,
    union_branches,
)
from sqlmpeg.parser import _ident_name as _fold
from sqlmpeg.probe import ProbeResult, StreamMeta
from sqlmpeg.registry import DynamicFilter, FilterOption, Registry
from sqlmpeg.sink import validate_option
from sqlmpeg.stdlib import FUNCTIONS, ExpandCtx, FuncSpec, Param, signatures

__all__ = ["lower"]

_FRAME_COLUMN = "frame"
_TIME_COLUMN = "t"

# The array-typed pseudo-columns an input exposes, and their element type.
# RFC-004 added subtitle/data: identical array/subscript/splat surface, but
# passthrough-only (see `_PASSTHROUGH_ONLY` below).
_ARRAY_COLUMNS: dict[str, StreamType] = {
    "video": "video",
    "audio": "audio",
    "subtitle": "subtitle",
    "data": "data",
}

_TYPE_MARKERS: dict[StreamType, str] = {
    "video": "v",
    "audio": "a",
    "subtitle": "s",
    "data": "d",
}

# Stream types an ffmpeg filtergraph cannot carry (RFC-004, "Passthrough-only"):
# they may only ever become an Output (a bare `-map`), never a filter argument
# and never the input of a WHERE trim.
_PASSTHROUGH_ONLY: frozenset[StreamType] = frozenset({"subtitle", "data"})

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
_ZIP_HINT = (
    "broadcast arrays zip elementwise, one output per element; "
    "subscript one of them to pair a single stream with the other, e.g. a.audio[1]"
)
_MACRO_HINT = (
    "call the underlying ffmpeg filters directly instead; each of those takes "
    "named options"
)
_NO_REGISTRY_HINT = (
    "install ffmpeg (or put it on PATH) to use named arguments; a stdlib call "
    "with positional arguments only compiles anywhere"
)
_PORTABLE_HINT = (
    "--portable keeps a query machine-independent: drop the named arguments, or "
    "compile without --portable"
)
_PASSTHROUGH_HINT = (
    "subtitle and data streams can only be selected (and copied), never filtered; "
    "drop them from the call and select them as their own column"
)
_CAPTION_TRIM_HINT = (
    "put the WHERE on the INPUT alias instead (an input's time range becomes a "
    "seek, which trims every stream type), or select the subtitle/data columns "
    "of the CTE without a WHERE time range"
)

# Longest option/constant list a hint or message renders before it stops
# counting (xfade's `transition` alone has 59 constants).
_MAX_LISTED = 12


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


@dataclass(frozen=True)
class _NamedArg:
    """One ``name => value`` call argument (RFC-003).

    `name` is verbatim (ffmpeg AVOption names are case-sensitive) and `value` is
    the raw sqlglot node — the option table this is checked against comes from
    the installed ffmpeg, so nothing is interpreted before the registry says
    what the option's type is.
    """

    name: str
    value: exp.Expr


@dataclass(frozen=True)
class _Call:
    """A function call as lowering sees it: a name, positional args, named args."""

    name: str
    args: list[exp.Expr]
    named: list[_NamedArg]


def _call_parts(node: exp.Expr) -> _Call | None:
    """The call `node` is, else None.

    ``exp.Overlay`` is normalized back to the four positional arguments the
    SQL surface uses; sqlglot parks them under named keys because Postgres
    spells the builtin ``OVERLAY(x PLACING y FROM n FOR m)``. (That builtin
    grammar also means ``overlay(...)`` is the one stdlib call that cannot
    take named arguments: sqlglot rejects ``=>`` inside it at PARSE time.)

    Named arguments arrive as ``exp.Kwarg`` among the positional ones and are
    split out here. Their TRAILING position is enforced by resolve; the check
    is repeated defensively because a Kwarg among positional args would
    otherwise silently shift every parameter after it.
    """
    if isinstance(node, exp.Overlay):
        parts = [
            node.this,
            node.args.get("expression"),
            node.args.get("from_"),
            node.args.get("for_"),
        ]
        return _Call("overlay", [arg for arg in parts if isinstance(arg, exp.Expr)], [])
    if isinstance(node, exp.Anonymous):
        return _split_args(str(node.this), node)
    if isinstance(node, exp.Func):
        return _split_args(node.sql_name().lower(), node)
    return None


def _split_args(name: str, call: exp.Expr) -> _Call:
    positional: list[exp.Expr] = []
    named: list[_NamedArg] = []
    for arg in call.expressions:
        if not isinstance(arg, exp.Expr):
            continue
        if isinstance(arg, exp.Kwarg):
            value = arg.args.get("expression")
            if not isinstance(value, exp.Expr):  # resolve already rejected this
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"named argument '{kwarg_name(arg)}' has no value",
                    arg,
                )
            named.append(_NamedArg(name=kwarg_name(arg), value=value))
            continue
        if named:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "positional arguments must come before named arguments",
                arg,
                fallback=call,
            )
        positional.append(arg)
    return _Call(name, positional, named)


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


@dataclass(frozen=True)
class _Unrepresentable:
    """A COPY option value that is no python scalar at all (``NULL``, a bare word).

    Handed to :func:`sqlmpeg.sink.validate_option` AS the value: it is never a
    ``str``/``int``/``bool``, so every declared option type rejects it and the
    SINK_OPTION_TYPE message plus its per-type hint still come from the option
    table — guardrail #4, no option knowledge is duplicated here. ``__repr__``
    is what the message interpolates, so it reads back as what the user wrote.
    """

    text: str

    def __repr__(self) -> str:
        return self.text


def _sink_describe(node: exp.Expr) -> str:
    if isinstance(node, exp.Var):
        return f"the bare word {node.name}"
    if isinstance(node, exp.Identifier):
        return f'the identifier "{node.name}"'
    return _describe(node)


def _sink_value(node: exp.Expr) -> object:
    """One ``COPY ... WITH (name value)`` value as a python scalar.

    Never raises and never validates: an unusable shape comes back as an
    :class:`_Unrepresentable`, and a well-formed value of the WRONG type (a
    float for ``crf``, a string for ``faststart``) comes back as itself. The
    option table decides in both cases.
    """
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if isinstance(node, exp.Literal):
        if node.is_string:
            return str(node.this)
        try:
            value = node.to_py()
        except (ArithmeticError, TypeError, ValueError):
            return _Unrepresentable(repr(str(node.this)))
        if isinstance(value, bool):  # sqlglot never does this; be explicit anyway
            return _Unrepresentable(repr(value))
        # Decimal renders neither to JSON nor to an ffmpeg arg; float does, and
        # a float is a type error for every v1 option anyway.
        return value if isinstance(value, int) else float(value)
    return _Unrepresentable(_sink_describe(node))


# ---------------------------------------------------------------------------
# typed values, bindings, per-branch environment
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Stream:
    """One typed stream: an IR ref, the pad type it carries, and its origin.

    `source` is the probed :class:`~sqlmpeg.probe.StreamMeta` this stream comes
    from 1:1 — directly (a passthrough subscript) or through a chain of
    single-stream-input filters, the WHERE trim included — and is threaded
    unconditionally. A call over two or more streams (``amix``, ``overlay``)
    and a ``concat`` pad are the other kind of join: each keeps `source` only
    when every stream feeding it agrees on what it says (:func:`_agreed_source`);
    otherwise it is None, same as an unprobed input. :func:`_provenance` turns
    it into ``Output.metadata``.
    """

    ref: FrameRef
    type: StreamType
    source: StreamMeta | None = None


@dataclass(frozen=True)
class _Value:
    """What every expression lowers to: one stream, or a whole array of them.

    `is_array` is deliberately not ``len(streams) != 1``: a one-element array
    is still an array — it splats, broadcasts and subscripts — and on a
    single-track file that is the ONLY thing separating ``a.audio`` from
    ``a.audio[1]``.
    """

    type: StreamType  # element type; every element of an array agrees on it
    streams: tuple[_Stream, ...]
    is_array: bool

    def at(self, index: int) -> _Stream:
        """Element `index` of an array; the one stream of a scalar (it repeats)."""
        return self.streams[index] if self.is_array else self.streams[0]


def _scalar(stream: _Stream) -> _Value:
    return _Value(type=stream.type, streams=(stream,), is_array=False)


def _array(stream_type: StreamType, streams: Iterable[_Stream]) -> _Value:
    return _Value(type=stream_type, streams=tuple(streams), is_array=True)


@dataclass(frozen=True)
class _Column:
    """One SELECT column of a branch (or of a CTE body): its name and value.

    An array column carries every one of its streams here, so a CTE records an
    array column's LENGTH statically and a later ``<cte>.<name>[k]`` is
    bounds-checked without re-probing anything.
    """

    name: str | None
    value: _Value


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
    # CTE name -> its WHERE window. CTE-ONLY: an INPUT alias's window is a
    # property of its `-i`, not of this branch, so `_collect_trims` records it
    # in `Graph.input_trims` instead and no filter trim is ever spliced for it.
    trims: dict[str, tuple[int | float, int | float]] = field(default_factory=dict)
    # base stream ref -> its trimmed ref, so one filter trim is shared by every
    # consumer of that stream inside this branch (CTE-only, as above).
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
    def __init__(
        self,
        res: Resolved,
        probes: dict[str, ProbeResult | None],
        registry: Registry | None,
        portable: bool,
    ) -> None:
        self.res = res
        self.probes = probes
        self.registry = registry
        self.portable = portable
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
        # The SELECT list IS the output stream list, and an array column is
        # several streams, so it splats into consecutive Outputs. Every element
        # of an aliased array column keeps that alias VERBATIM (no ordinal
        # suffix): the alias names the column, not the stream, and ffmpeg
        # metadata naming is plan 022's business.
        self.graph.outputs = [
            Output(
                ref=stream.ref,
                type=stream.type,
                name=column.name,
                metadata=_provenance(stream),
            )
            for column in columns
            for stream in column.value.streams
        ]
        if self.res.sink is not None:
            self.graph.sink = self._lower_sink(self.res.sink)
        return self.graph

    # -- the COPY sink (RFC-002) -------------------------------------------

    def _lower_sink(self, raw: RawSink) -> Sink:
        """Validate the COPY options against the table and normalize them.

        Anchoring, VERIFIED against sqlglot 30.17: the option NAME (an
        ``exp.Var``) carries no token position, and neither does a ``Boolean``
        / ``Var`` / ``Null`` value, so the anchor falls back through the name
        node to the value node to the path literal — which at least keeps
        every rejection on (or just above) the ``WITH`` block.
        """
        options: dict[str, object] = {}
        for option in raw.options:
            line, col = _pos(option.name_node, option.value, raw.path_node)
            options[option.name] = validate_option(
                option.name, _sink_value(option.value), line=line, col=col
            )
        return Sink(path=raw.path, options=options)

    # -- a query (one SELECT, or a UNION ALL of them) ----------------------

    def _lower_query(
        self, branches: list[exp.Select], anchor: exp.Expr
    ) -> list[_Column]:
        if not branches:
            raise _error(ErrorCode.UNSUPPORTED_SQL, "query has no SELECT", anchor)
        lowered = [self._lower_branch(branch) for branch in branches]
        if len(lowered) == 1:
            # A single branch keeps its arrays: a CTE body's array column stays
            # an array for `<cte>.<name>` to splat, broadcast over, or subscript.
            return lowered[0]
        # concat maps one input pad per column, so arrays are flattened to
        # one column per element BEFORE it sees them.
        flattened = [_flatten(columns) for columns in lowered]
        self._check_concat_columns(branches, flattened)
        self._check_concat_signature(branches, lowered, flattened)
        return self._concat(flattened)

    def _check_concat_columns(
        self, branches: list[exp.Select], flattened: list[list[_Column]]
    ) -> None:
        """No UNION ALL branch may carry a subtitle/data column (RFC-004).

        ``concat`` is a filtergraph filter and takes ``v`` video plus ``a``
        audio pads — there is no ``s``/``d`` half — so a caption column in a
        concatenated branch has nowhere to go. Checked before
        :meth:`_check_concat_signature` so the rejection names the real reason
        rather than a column-count mismatch.
        """
        for index, columns in enumerate(flattened):
            for column in columns:
                if column.value.type in _PASSTHROUGH_ONLY:
                    raise _error(
                        ErrorCode.UNSUPPORTED_SQL,
                        f"UNION ALL concatenates video and audio only: branch "
                        f"{index + 1} selects a {column.value.type} stream",
                        branches[index],
                        hint="select subtitle and data streams outside the UNION ALL "
                        "(they are copied, never concatenated)",
                    )

    def _check_concat_signature(
        self,
        branches: list[exp.Select],
        lowered: list[list[_Column]],
        flattened: list[list[_Column]],
    ) -> None:
        """Every UNION ALL branch must agree on column count, types and order.

        On the FLATTENED signature: an array column contributes one concat
        column per element, so branches must agree on element counts too. The
        message renders each column as written (``audio[2]`` for an array), so
        a pure length mismatch reads as one.
        """
        expected = [column.value.type for column in flattened[0]]
        for index in range(1, len(flattened)):
            got = [column.value.type for column in flattened[index]]
            if got == expected:
                continue
            raise _error(
                ErrorCode.CONCAT_MISMATCH,
                "UNION ALL branches must select the same stream types in the same "
                f"order: branch 1 selects ({_signature(lowered[0])}), "
                f"branch {index + 1} selects ({_signature(lowered[index])})",
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
        video_positions = [i for i, column in enumerate(first) if column.value.type == "video"]
        audio_positions = [i for i, column in enumerate(first) if column.value.type == "audio"]
        video_count, audio_count = len(video_positions), len(audio_positions)

        inputs: list[FrameRef] = []
        for columns in lowered:
            inputs += [columns[i].value.streams[0].ref for i in video_positions]
            inputs += [columns[i].value.streams[0].ref for i in audio_positions]

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
        # A concat pad is fed by one stream per segment; it inherits provenance
        # only where all of them say the same thing (see `_agreed_source`).
        return [
            _Column(
                name=column.name,
                value=_scalar(
                    _Stream(
                        ref=node_id if total == 1 else f"{node_id}:{pad_of[position]}",
                        type=column.value.type,
                        source=_agreed_source(
                            [columns[position].value.streams[0] for columns in lowered]
                        ),
                    )
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
        columns: list[_Column] = []
        for projection in projections:
            # RFC-004: a star is not an expression, it is a column GENERATOR --
            # it contributes as many columns as the aliases it names have
            # streams, so it is expanded here rather than in `_lower_expr`.
            qualifier = star_qualifier(projection)
            if qualifier is not None:
                columns += self._expand_star(qualifier, projection, env, select)
                continue
            columns.append(
                _Column(
                    name=_projection_name(projection),
                    value=self._lower_expr(projection, env, select),
                )
            )
        return columns

    # -- SELECT * / <alias>.* (RFC-004) ------------------------------------

    def _expand_star(
        self, qualifier: str, anchor: exp.Expr, env: _Env, select: exp.Select
    ) -> list[_Column]:
        """Every stream a star stands for, as passthrough columns.

        A bare ``*`` takes every alias of the FROM clause in FROM order
        (``_Env.bindings`` is insertion-ordered and built by `_scope` in exactly
        that order); ``<alias>.*`` takes one. Within an alias: FILE order for an
        input (probe order, all four stream types interleaved as the container
        has them), COLUMN order for a CTE, with array columns splatting.

        The WHERE window of each alias still applies: for an input alias it is
        already on the ``-i`` (so ``SELECT *`` under a WHERE seeks every stream
        of the file, captions included), for a CTE it is the filter trim
        `_access` splices — which is also where a trimmed CTE caption column is
        rejected.
        """
        if qualifier:
            binding = env.bindings.get(qualifier)
            if binding is None:
                raise _error(
                    ErrorCode.UNKNOWN_ALIAS,
                    f"unknown alias '{qualifier}'",
                    anchor,
                    fallback=select,
                    hint=self._known_hint(),
                )
            bindings = [binding]
        else:
            bindings = list(env.bindings.values())

        columns: list[_Column] = []
        for binding in bindings:
            if isinstance(binding, _InputBinding):
                columns += self._star_input(binding.alias, anchor, env, select)
            else:
                columns += self._star_cte(binding, anchor, env, select)
        return columns

    def _star_input(
        self, alias: str, anchor: exp.Expr, env: _Env, select: exp.Select
    ) -> list[_Column]:
        """Every stream of one input alias, in file order.

        Splat tier, same policy as a bare ``a.audio`` (RFC-001 "Probing
        policy"): how many streams a file has, and of which types, is a
        property of the file, so an input that could not be probed is
        INPUT_NOT_FOUND rather than a guess.
        """
        result = self.probes.get(alias)
        path = self.res.input_paths[self.graph.sources[alias]]
        if result is None:
            raise _error(
                ErrorCode.INPUT_NOT_FOUND,
                f"cannot expand '*' over '{path}': file not found or unreadable",
                anchor,
                fallback=select,
                hint="'*' is every stream of the input, and only a readable input "
                f"can list them; name the streams instead, e.g. {alias}.video[1]",
            )
        if not result.streams:
            raise _error(
                ErrorCode.STREAM_NOT_FOUND,
                f"'*' over '{path}' selects nothing: it has no video, audio, "
                "subtitle or data streams",
                anchor,
                fallback=select,
                hint="an empty expansion would select nothing; drop the star",
            )
        return [
            _Column(
                name=None,
                value=self._access(
                    env,
                    alias,
                    _scalar(self._source_stream(alias, meta.type, meta.index)),
                    anchor,
                    select,
                ),
            )
            for meta in result.streams
        ]

    def _star_cte(
        self, binding: _CteBinding, anchor: exp.Expr, env: _Env, select: exp.Select
    ) -> list[_Column]:
        """A CTE's columns, in order, arrays splatted. No probe is involved.

        A CTE's shape was fixed when its body lowered, so this is static — the
        same information `<cte>.<name>` already reads. Column names are kept:
        the star selects the columns the CTE named, not anonymous streams.
        """
        return [
            _Column(
                name=column.name,
                value=self._access(
                    env, binding.name, _scalar(stream), anchor, select
                ),
            )
            for column in binding.columns
            for stream in column.value.streams
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
        """Record each aliased time range, on the input or on the branch.

        The binding decides where the window goes (RFC-004's input-seek
        amendment). An INPUT alias owns its own ``-i`` and is globally unique,
        so at most one window can ever apply to it: it is recorded on the GRAPH
        (``Graph.input_trims``) and becomes ``-ss``/``-to``, seeking every
        stream of that input coherently — captions and unselected streams
        included. A CTE name is a filtergraph pad, so its window is recorded on
        the BRANCH (``_Env.trims``) and the ``trim``/``atrim`` pair is spliced
        lazily by :meth:`_access`, the first time a stream of that CTE is
        consumed.
        """
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
            window = (
                _number(low, ErrorCode.UNSUPPORTED_SQL),
                _number(high, ErrorCode.UNSUPPORTED_SQL),
            )
            if isinstance(env.bindings[alias], _InputBinding):
                self.graph.input_trims[alias] = window
            else:
                env.trims[alias] = window

    def _access(
        self,
        env: _Env,
        alias: str,
        value: _Value,
        anchor: exp.Expr,
        select: exp.Select,
    ) -> _Value:
        """Apply `alias`'s FILTER trim to every stream of `value`.

        ``_Env.trims`` is CTE-only (see :meth:`_collect_trims`), so this is a
        no-op for every input alias: an input's window is already on its ``-i``
        as ``-ss``/``-to``, and the stream refs pass through untouched — which
        is what lets a trimmed column stay a passthrough and be stream-copied.

        For a CTE window the trim is spliced elementwise over an array and
        memoized per stream, so each element of a broadcast array gets exactly
        one trim, shared by all its consumers.

        This is also where a trimmed caption is rejected (RFC-004): the WHERE
        window is collected before any projection lowers, so "is this CTE's
        subtitle/data actually CONSUMED under a trim" is only knowable here, at
        the point the trim would be applied. A CTE's trim is a filtergraph
        ``trim``/``atrim`` pair, which cannot carry subtitle or data streams at
        all, so for a CTE the rejection is permanent (RFC-004); on an input
        alias it does not arise, because there is no filter node to feed.
        """
        window = env.trims.get(alias)
        if window is None:
            return value
        if value.type in _PASSTHROUGH_ONLY:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"a CTE's captions cannot be trimmed: 'WHERE {alias}.t' would have "
                f"to trim a {value.type} stream, which no filtergraph can carry",
                anchor,
                fallback=select,
                hint=_CAPTION_TRIM_HINT,
            )
        return _Value(
            type=value.type,
            streams=tuple(self._trim(env, window, stream) for stream in value.streams),
            is_array=value.is_array,
        )

    def _trim(
        self, env: _Env, window: tuple[int | float, int | float], stream: _Stream
    ) -> _Stream:
        """The trimmed counterpart of one stream; a trim is spliced once per stream."""
        cached = env.trimmed.get(stream.ref)
        if cached is not None:
            return _Stream(ref=cached, type=stream.type, source=stream.source)
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
        # A trim is 1:1, so it threads provenance through unchanged.
        return _Stream(ref=rebased, type=stream.type, source=stream.source)

    # -- expressions ------------------------------------------------------

    def _lower_expr(self, node: exp.Expr, env: _Env, select: exp.Select) -> _Value:
        node = _unwrap(node)
        if isinstance(node, exp.Bracket | exp.Column):
            alias, value = self._base_stream(node, env, select)
            return self._access(env, alias, value, node, select)
        if isinstance(node, exp.Cast):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "casts are not supported",
                node,
                fallback=select,
                hint="a stream has exactly one type",
            )
        call = _call_parts(node)
        if call is not None:
            return self._lower_call(node, call, env, select)
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
    ) -> tuple[str, _Value]:
        """Resolve a column / subscript to ``(alias, untrimmed value)``.

        The value is an ARRAY for a bare ``a.video`` / ``a.audio`` (or a bare
        reference to an array-typed CTE column) and a scalar for anything
        subscripted. Pure: creates no nodes, so the type checker
        (:meth:`_classify`) can call it on an argument before deciding whether
        to lower it — which is also why enumerating an unprobeable input fails
        here, before the graph has grown.
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
    ) -> tuple[str, _Value]:
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
            return alias, self._input_value(alias, name, index, anchor, select)
        return alias, self._cte_value(binding, name, index, anchor, select)

    def _input_value(
        self,
        alias: str,
        name: str,
        index: int | None,
        anchor: exp.Expr,
        select: exp.Select,
    ) -> _Value:
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
                    f"{alias}.audio, {alias}.subtitle, {alias}.data and {alias}.t",
                )
            if index is None:
                return self._enumerate(alias, array_type, anchor, select)
            stream_type = array_type
            zero_based = index - 1

        self._check_bounds(alias, stream_type, zero_based, anchor, select)
        return _scalar(self._source_stream(alias, stream_type, zero_based))

    def _source_stream(self, alias: str, stream_type: StreamType, index: int) -> _Stream:
        """One raw input stream, tagged with its probed metadata when there is any."""
        marker = _TYPE_MARKERS[stream_type]
        return _Stream(
            ref=f"src:{alias}:{marker}:{index}",
            type=stream_type,
            source=self._stream_meta(alias, stream_type, index),
        )

    def _stream_meta(
        self, alias: str, stream_type: StreamType, index: int
    ) -> StreamMeta | None:
        result = self.probes.get(alias)
        if result is None:
            return None
        streams = result.by_type(stream_type)
        if not 0 <= index < len(streams):
            return None
        return streams[index]

    def _enumerate(
        self, alias: str, stream_type: StreamType, anchor: exp.Expr, select: exp.Select
    ) -> _Value:
        """The whole array of `alias`'s `stream_type` streams, in file order.

        The one thing lowering cannot do symbolically: an array's LENGTH is a
        property of the file, so an input that could not be probed fails here
        (RFC-001, "Probing policy" — "cannot enumerate the streams of a file I
        cannot read" is a natural error, not a policy error).
        """
        result = self.probes.get(alias)
        if result is None:
            path = self.res.input_paths[self.graph.sources[alias]]
            raise _error(
                ErrorCode.INPUT_NOT_FOUND,
                f"cannot enumerate the streams of '{path}': file not found or unreadable",
                anchor,
                fallback=select,
                hint=f"'{alias}.{stream_type}' is the whole stream array, and only a "
                f"readable input can size it; subscript one stream, "
                f"e.g. {alias}.{stream_type}[1]",
            )
        count = len(result.by_type(stream_type))
        if count == 0:
            path = self.res.input_paths[self.graph.sources[alias]]
            raise _error(
                ErrorCode.STREAM_NOT_FOUND,
                f"'{alias}.{stream_type}' is empty: '{path}' has no "
                f"{stream_type} streams",
                anchor,
                fallback=select,
                hint="an empty stream array would select nothing; drop the column",
            )
        return _array(
            stream_type,
            (self._source_stream(alias, stream_type, k) for k in range(count)),
        )

    def _cte_value(
        self,
        binding: _CteBinding,
        name: str,
        index: int | None,
        anchor: exp.Expr,
        select: exp.Select,
    ) -> _Value:
        column = self._cte_column(binding, name)
        if column is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"unknown column '{binding.name}.{name}'",
                anchor,
                fallback=select,
                hint=self._cte_columns_hint(binding),
            )
        value = column.value
        if index is None:
            return value
        if not value.is_array:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{binding.name}.{name}' is a single stream and cannot be subscripted",
                anchor,
                fallback=select,
                hint=f"drop the subscript: '{binding.name}.{name}' already names one stream",
            )
        # The length was recorded when the CTE body lowered, so this bound is
        # STATIC: no probe is consulted here, whatever produced the array.
        if not 1 <= index <= len(value.streams):
            have = f"{len(value.streams)} stream" + ("" if len(value.streams) == 1 else "s")
            raise _error(
                ErrorCode.STREAM_NOT_FOUND,
                f"'{binding.name}.{name}[{index}]' does not exist: "
                f"column '{binding.name}.{name}' has {have}",
                anchor,
                fallback=select,
                hint=_SUBSCRIPT_HINT,
            )
        return _scalar(value.streams[index - 1])

    def _cte_column(self, binding: _CteBinding, name: str) -> _Column | None:
        for column in binding.columns:
            if column.name == name:
                return column
        # v0 compat: a CTE that selects exactly one video column is reachable
        # as `<cte>.frame` whatever (if anything) its AS named it. `frame` is
        # singular sugar, so an array column does not answer to it.
        if name == _FRAME_COLUMN and _is_single_video_column(binding):
            return binding.columns[0]
        return None

    def _cte_columns_hint(self, binding: _CteBinding) -> str:
        names = {column.name for column in binding.columns if column.name is not None}
        if _is_single_video_column(binding):
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
        self, node: exp.Expr, call: _Call, env: _Env, select: exp.Select
    ) -> _Value:
        """Resolve a call against tier 1, then tier 2 (RFC-003).

        The stdlib ALWAYS wins a name collision (``scale``, ``crop``, ``overlay``
        are both stdlib functions and real ffmpeg filters): tier 1 is the
        portable, documented-forever surface, tier 2 is whatever the installed
        ffmpeg happens to provide.
        """
        name = call.name.lower()
        spec = FUNCTIONS.get(name)
        if spec is not None:
            return self._lower_stdlib_call(node, name, spec, call, env, select)
        dynamic = self.registry.get(name) if self.registry is not None else None
        if dynamic is None:
            raise _error(
                ErrorCode.UNKNOWN_FUNCTION,
                f"unknown function {call.name}()",
                node,
                fallback=select,
                hint=self._unknown_function_hint(name),
            )
        return self._lower_dynamic_call(node, name, dynamic, call, env, select)

    # -- tier 1: the curated stdlib, plus trailing named extras ------------

    def _lower_stdlib_call(
        self,
        node: exp.Expr,
        name: str,
        spec: FuncSpec,
        call: _Call,
        env: _Env,
        select: exp.Select,
    ) -> _Value:
        kinds = [self._classify(arg, env, select) for arg in call.args]
        self._reject_passthrough_args(name, kinds, call, node)
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
        target = self._named_extras_target(name, spec, call, node)
        streams, literals = self._lower_arguments(variant, call.args, env, select)

        # The extras are validated ONCE, against the arg keys the expansion
        # produced — which is only known after expanding — and then merged into
        # every element's node, so a broadcast call sets them on each one.
        checked: dict[str, object] = {}
        validated = False

        def build(values: list[object]) -> FrameRef:
            nonlocal checked, validated
            ref = spec.expand(self.ctx, values)
            if target is None:
                return ref
            filter_name, options = target
            produced = self._expanded_node(name, filter_name, ref, node, select)
            if not validated:
                checked = self._check_named_args(
                    filter_name,
                    options,
                    call.named,
                    node,
                    owner=name,
                    occupied=set(produced.args),
                )
                validated = True
            produced.args.update(checked)
            return ref

        return self._expand_call(
            name,
            node,
            call.args,
            select,
            streams=streams,
            literals=literals,
            arity=len(variant),
            positions=_stream_positions(variant),
            returns=spec.returns,
            build=build,
        )

    def _named_extras_target(
        self, name: str, spec: FuncSpec, call: _Call, node: exp.Expr
    ) -> tuple[str, dict[str, FilterOption]] | None:
        """``(filter, its options)`` the trailing named args of a stdlib call target.

        None when the call has no named args at all. A spec whose
        ``named_target`` is None is a MACRO over several filters (only
        ``blur_regions`` today), so there is no single option set to reach
        through to and the named args are rejected outright.
        """
        if not call.named:
            return None
        anchor = call.named[0].value
        if spec.named_target is None:
            raise _error(
                ErrorCode.UDF_ARG_TYPE,
                f"{name}() expands to more than one ffmpeg filter, so its named "
                f"argument '{call.named[0].name}' has no single filter to set it on",
                anchor,
                fallback=node,
                hint=_MACRO_HINT,
            )
        return spec.named_target, self._filter_options(spec.named_target, anchor, node)

    def _expanded_node(
        self, name: str, filter_name: str, ref: FrameRef, node: exp.Expr, select: exp.Select
    ) -> Node:
        """The single node a ``named_target`` spec's expansion produced.

        Invariant (stdlib.FuncSpec): a spec that names a ``named_target`` is
        single-filter by construction, so the ref its expand returned is that
        node's id and its filter is the target. A spec that breaks the invariant
        is a bug in the table, not in the query — hence INTERNAL.
        """
        produced = self.graph.nodes.get(ref)
        if produced is None or produced.filter != filter_name:
            raise _error(
                ErrorCode.INTERNAL,
                f"{name}() declares named_target '{filter_name}' but its expansion "
                "did not produce exactly that one filter",
                node,
                fallback=select,
                hint="please report this query as a bug",
            )
        return produced

    # -- tier 2: any filter the installed ffmpeg reports -------------------

    def _lower_dynamic_call(
        self,
        node: exp.Expr,
        name: str,
        dynamic: DynamicFilter,
        call: _Call,
        env: _Env,
        select: exp.Select,
    ) -> _Value:
        """A call resolved from the registry: streams positionally, options named.

        The pad signature IS the signature: ``gblur`` (``V->V``) takes exactly
        one video argument, ``xfade`` (``VV->V``) exactly two. Everything else
        about the call — every option — is named, because ffmpeg option order is
        not a thing users should have to know.
        """
        kinds = [self._classify(arg, env, select) for arg in call.args]
        self._reject_passthrough_args(name, kinds, call, node)
        expected = list(dynamic.inputs)
        if kinds != expected:
            raise _error(
                ErrorCode.UDF_ARG_TYPE,
                f"{name}() is an ffmpeg filter: it expects "
                f"{name}({', '.join(expected)}), got {name}({', '.join(kinds)})",
                node,
                fallback=select,
                hint=f"only stream inputs are positional for a dynamic filter; pass "
                f"options by name, e.g. {name}({', '.join(expected)}, <option> => <value>)",
            )
        options = self._filter_options(name, node, select) if call.named else {}
        args = self._check_named_args(
            name, options, call.named, node, owner=name, occupied=set()
        )
        streams = {
            position: self._lower_expr(arg, env, select)
            for position, arg in enumerate(call.args)
        }
        output = dynamic.output

        def build(values: list[object]) -> FrameRef:
            return self.ctx.node(
                name, dict(args), [_as_ref(value) for value in values], [output]
            )

        return self._expand_call(
            name,
            node,
            call.args,
            select,
            streams=streams,
            literals={},
            arity=len(expected),
            positions=list(range(len(expected))),
            returns=output,
            build=build,
        )

    # -- shared call machinery --------------------------------------------

    def _reject_passthrough_args(
        self,
        name: str,
        kinds: list[str],
        call: _Call,
        node: exp.Expr,
    ) -> None:
        """No function takes a subtitle or data stream (RFC-004).

        An ffmpeg filtergraph carries video and audio only, so a caption or
        timed-metadata stream can never be a filter INPUT — in either tier.
        Tier 1 would otherwise report it as a generic signature mismatch and
        tier 2 as "expects gblur(video)"; both are true but neither says the
        thing that actually matters, which is that no signature could ever
        accept it. ``ParamKind`` and ``DynamicFilter.inputs`` are deliberately
        left alone (RFC-004: "ParamKind is UNCHANGED"), so this is the one
        place that knows it.
        """
        for position, kind in enumerate(kinds):
            if kind not in _PASSTHROUGH_ONLY:
                continue
            raise _error(
                ErrorCode.UDF_ARG_TYPE,
                f"{name}() cannot take a {kind} stream: {kind} streams cannot be "
                "filtered, only selected",
                call.args[position],
                fallback=node,
                hint=_PASSTHROUGH_HINT,
            )

    def _lower_arguments(
        self,
        variant: tuple[Param, ...],
        arg_nodes: list[exp.Expr],
        env: _Env,
        select: exp.Select,
    ) -> tuple[dict[int, _Value], dict[int, object]]:
        """Lower each argument ONCE, into ``(streams, literals)`` by position.

        A stream argument used by several broadcast elements is one subgraph
        fanned out by the split pass, not a copy per element.
        """
        streams: dict[int, _Value] = {}
        literals: dict[int, object] = {}
        for position, (param, arg) in enumerate(zip(variant, arg_nodes)):
            if param.kind in ("video", "audio"):
                streams[position] = self._lower_expr(arg, env, select)
            elif param.kind == "num":
                literals[position] = _number(arg)
            else:
                literals[position] = _string(arg)
        return streams, literals

    def _expand_call(
        self,
        name: str,
        node: exp.Expr,
        arg_nodes: list[exp.Expr],
        select: exp.Select,
        *,
        streams: dict[int, _Value],
        literals: dict[int, object],
        arity: int,
        positions: list[int],
        returns: StreamType,
        build: Callable[[list[object]], FrameRef],
    ) -> _Value:
        """Broadcast `build` over the array arguments, if there are any.

        Type-driven and tier-agnostic: `positions` is where the stream
        arguments are (from a stdlib variant, or from a dynamic filter's pad
        signature) and `build` is what turns one element's argument values into
        a subgraph.
        """
        length = self._zip_length(name, node, arg_nodes, streams, select)
        expanded: list[_Stream] = []
        for element in range(1 if length is None else length):
            values: list[object] = [
                streams[position].at(element).ref
                if position in streams
                else literals[position]
                for position in range(arity)
            ]
            # A single-stream-input function is 1:1, so its result inherits
            # that one input's provenance unconditionally. A call over two or
            # more streams (amix, overlay, xfade) is a join like concat's: it
            # threads provenance only when every input stream agrees
            # (_agreed_source) -- mixing two English tracks yields an English
            # track.
            if len(positions) == 1:
                source = streams[positions[0]].at(element).source
            elif len(positions) >= 2:
                source = _agreed_source([streams[p].at(element) for p in positions])
            else:
                source = None
            expanded.append(_Stream(ref=build(values), type=returns, source=source))
        if length is None:
            return _scalar(expanded[0])
        return _array(returns, expanded)

    # -- named argument validation (RFC-003, both tiers) -------------------

    def _filter_options(
        self, filter_name: str, anchor: exp.Expr, fallback: exp.Expr
    ) -> dict[str, FilterOption]:
        """The introspected options of `filter_name`, or a typed rejection.

        One rule, stated the same way everywhere: named arguments ARE the
        installed ffmpeg. Without a registry — no ffmpeg, or ``--portable`` —
        there is nothing to validate them against, and guessing is exactly what
        this compiler does not do.

        ``Registry.options`` returns None only for a filter this ffmpeg does not
        have (or that the v1 scope fence excluded); an empty dict is a real
        answer (a filter with no options) and is passed through as one.
        """
        if self.registry is None:
            if self.portable:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "named arguments are validated against your installed ffmpeg, "
                    "and --portable turns that off",
                    anchor,
                    fallback=fallback,
                    hint=_PORTABLE_HINT,
                )
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "named arguments are validated against your installed ffmpeg; "
                "ffmpeg was not found",
                anchor,
                fallback=fallback,
                hint=_NO_REGISTRY_HINT,
            )
        options = self.registry.options(filter_name)
        if options is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"named arguments are validated against the ffmpeg filter "
                f"'{filter_name}', which your ffmpeg does not provide",
                anchor,
                fallback=fallback,
                hint="drop the named arguments, or install an ffmpeg that has "
                f"the '{filter_name}' filter",
            )
        return options

    def _check_named_args(
        self,
        filter_name: str,
        options: dict[str, FilterOption],
        named: list[_NamedArg],
        call: exp.Expr,
        *,
        owner: str,
        occupied: set[str],
    ) -> dict[str, object]:
        """Validate every named argument against `options`, in written order.

        `occupied` holds the arg keys a stdlib call's expansion already set —
        both the ones mapped from its positional arguments (``crop``'s ``w``)
        and the constants the spec hardcodes (``crossfade``'s default
        ``transition``, ``scale(f, 0.5)``'s ``h=-2``). A named argument that
        collides with one is rejected rather than silently overriding it
        (RFC-003), and the overload that takes it positionally, if there is one,
        is the way to set it.

        The collision check comes FIRST so that ``crop(f, 0, 0, 10, 10, w => 5)``
        reads as the conflict it is — ffmpeg's own name for that option is
        ``out_w``, so a registry check would otherwise call ``w`` unknown.
        """
        checked: dict[str, object] = {}
        for arg in named:
            if arg.name in occupied:
                raise _error(
                    ErrorCode.UDF_ARG_TYPE,
                    f"{owner}() already sets '{arg.name}' on the "
                    f"'{filter_name}' filter it expands to",
                    arg.value,
                    fallback=call,
                    hint="a named argument never overrides what the call itself "
                    "set; drop it, or use the overload that takes it positionally",
                )
            option = options.get(arg.name)
            if option is None:
                raise _error(
                    ErrorCode.UNKNOWN_FILTER_OPTION,
                    f"filter '{filter_name}' has no option '{arg.name}'",
                    arg.value,
                    fallback=call,
                    hint=_option_hint(arg.name, options),
                )
            checked[arg.name] = _option_value(filter_name, option, arg, call)
        return checked

    def _unknown_function_hint(self, name: str) -> str:
        """Did-you-mean across BOTH tiers, then why tier 2 might be missing."""
        candidates = set(FUNCTIONS)
        registry = self.registry
        dynamic = registry is not None and registry.available()
        if registry is not None and dynamic:
            candidates |= set(registry.names())
        matches = difflib.get_close_matches(name, sorted(candidates), n=1, cutoff=0.6)
        if matches:
            return f"did you mean {matches[0]}()?"
        known = "known functions: " + ", ".join(sorted(FUNCTIONS))
        if self.portable:
            return f"{known} (dynamic ffmpeg filters are disabled by --portable)"
        if not dynamic:
            return f"{known} (dynamic ffmpeg filters need ffmpeg on PATH)"
        return known

    def _zip_length(
        self,
        name: str,
        node: exp.Expr,
        arg_nodes: list[exp.Expr],
        streams: dict[int, _Value],
        select: exp.Select,
    ) -> int | None:
        """The element count this call expands to, or None if nothing is an array.

        Arrays zip (no cross products): they must all have the same length, and
        scalar arguments repeat into every element.
        """
        first: tuple[int, int] | None = None  # (argument position, length)
        for position, value in sorted(streams.items()):
            if not value.is_array:
                continue
            length = len(value.streams)
            if first is None:
                first = (position, length)
                continue
            if length == first[1]:
                continue
            raise _error(
                ErrorCode.BROADCAST_MISMATCH,
                f"{name}() cannot broadcast over arrays of different lengths: "
                f"{_sql_text(arg_nodes[first[0]])} has {_stream_count(first[1])}, "
                f"{_sql_text(arg_nodes[position])} has {_stream_count(length)}",
                node,
                fallback=select,
                hint=_ZIP_HINT,
            )
        return None if first is None else first[1]

    def _classify(self, node: exp.Expr, env: _Env, select: exp.Select) -> str:
        """Kind label for one call argument, matched against ``Param.kind``.

        Stream arguments resolve to ``video``/``audio`` without creating any
        node, so a mismatch is reported before the graph grows. Nested calls
        to unknown functions are reported here rather than being labelled a
        stream and swallowed by an outer arity error. A nested call resolves
        across BOTH tiers, so ``scale(gblur(a.frame, sigma => 2), 0.5)`` sees
        the inner call's output pad type.
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
        call = _call_parts(node)
        if call is not None:
            name = call.name.lower()
            spec = FUNCTIONS.get(name)
            if spec is not None:
                return spec.returns
            dynamic = self.registry.get(name) if self.registry is not None else None
            if dynamic is None:
                raise _error(
                    ErrorCode.UNKNOWN_FUNCTION,
                    f"unknown function {call.name}()",
                    node,
                    fallback=select,
                    hint=self._unknown_function_hint(name),
                )
            return dynamic.output
        return _EXPR_KIND


# ---------------------------------------------------------------------------
# provenance & small value helpers
# ---------------------------------------------------------------------------


def _provenance(stream: _Stream) -> dict[str, str]:
    """Language/title tags of the source stream an output is derived 1:1 from.

    `_Stream.source` is what threads them: it survives a passthrough, the WHERE
    trim, and any chain of single-stream-input calls unconditionally; a call
    over two or more streams (``amix``, ``overlay``) and a concat pad thread it
    only when every stream feeding them agrees (:func:`_agreed_source`).
    ``language=und`` is what an mp4 muxer stamps on an untagged stream, so it
    carries no information and is not copied.
    """
    source = stream.source
    if source is None:
        return {}
    metadata: dict[str, str] = {}
    for key in _PROVENANCE_KEYS:
        value = source.metadata.get(key)
        if value is None:
            continue
        if key == "language" and value == _UNDEFINED_LANGUAGE:
            continue
        metadata[key] = value
    return metadata


def _agreed_source(segments: list[_Stream]) -> StreamMeta | None:
    """The provenance an N:1 join inherits from the streams feeding it.

    Used by both kinds of join that take more than one input stream: a concat
    pad (`segments` is one stream per UNION ALL branch, in branch order) and a
    multi-stream call like ``amix``/``overlay`` (`segments` is its stream
    arguments, in argument order, one element already picked out of each). The
    result is only still "that stream" when every segment says the SAME thing
    about it: the comparison is on the FILTERED provenance dicts, not on the
    raw ``StreamMeta``, so two segments that differ in sample rate or index but
    agree on ``language=fra`` do agree, and two "und"-tagged segments both
    filter down to ``{}`` — nothing to say, so nothing survives. Any
    disagreement, or an empty dict, gives None.

    The first segment's ``StreamMeta`` is what gets threaded: it and the others
    render identically, and it keeps ``_Stream.source`` a real probed stream.
    """
    agreed = _provenance(segments[0])
    if not agreed:
        return None
    if any(_provenance(segment) != agreed for segment in segments[1:]):
        return None
    return segments[0].source


def _flatten(columns: list[_Column]) -> list[_Column]:
    """One column per stream: arrays are gone, every column is a scalar.

    An aliased array column hands its alias to each of its elements, exactly as
    the SELECT-list splat does.
    """
    return [
        _Column(name=column.name, value=_scalar(stream))
        for column in columns
        for stream in column.value.streams
    ]


def _signature(columns: list[_Column]) -> str:
    """Branch column types for a CONCAT_MISMATCH message, arrays as ``audio[2]``."""
    parts = [
        f"{column.value.type}[{len(column.value.streams)}]"
        if column.value.is_array
        else column.value.type
        for column in columns
    ]
    return ", ".join(parts) or "nothing"


def _is_single_video_column(binding: _CteBinding) -> bool:
    """True if `binding` exposes exactly one column and it is a single video stream."""
    if len(binding.columns) != 1:
        return False
    value = binding.columns[0].value
    return value.type == "video" and not value.is_array


def _as_ref(value: object) -> FrameRef:
    """A lowered argument value as a stream ref (dynamic calls take only those)."""
    if not isinstance(value, str):  # pragma: no cover -- structurally impossible
        raise SqlmpegError(
            ErrorCode.INTERNAL,
            "a dynamic filter argument lowered to something that is not a stream",
            line=1,
            col=1,
            hint="please report this query as a bug",
        )
    return value


def _listed(names: Iterable[str]) -> str:
    """Comma-list at most ``_MAX_LISTED`` names, then count the rest."""
    items = list(names)
    if len(items) <= _MAX_LISTED:
        return ", ".join(items)
    rest = len(items) - _MAX_LISTED
    return ", ".join(items[:_MAX_LISTED]) + f", ... ({rest} more)"


def _option_hint(name: str, options: dict[str, FilterOption]) -> str:
    """Did-you-mean over the filter's REAL option names, else list them."""
    matches = difflib.get_close_matches(name, sorted(options), n=1, cutoff=0.6)
    if matches:
        return f"did you mean {matches[0]} => ...?"
    if not options:
        return "this filter has no options sqlmpeg can set"
    return "its options: " + _listed(sorted(options))


def _number_text(value: float) -> str:
    """A range bound as ffmpeg meant it: ``1024`` rather than ``1024.0``."""
    if value == int(value):
        return str(int(value))
    return str(value)


def _range_text(option: FilterOption) -> str | None:
    if option.minimum is not None and option.maximum is not None:
        return f"from {_number_text(option.minimum)} to {_number_text(option.maximum)}"
    if option.minimum is not None:
        return f"at least {_number_text(option.minimum)}"
    if option.maximum is not None:
        return f"at most {_number_text(option.maximum)}"
    return None


def _literal_value(node: exp.Expr) -> object | None:
    """A named argument's value as a python scalar, or None if it is not a literal.

    Deliberately separate from :func:`_number` / :func:`_string`: those raise the
    stdlib's own message, and an option's expected type is only known after the
    registry has been consulted, so reading the value and judging it are two
    steps here.
    """
    node = _unwrap(node)
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    negated = False
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Expr):
        negated = True
        node = _unwrap(node.this)
    if not isinstance(node, exp.Literal):
        return None
    if node.is_string:
        return None if negated else str(node.this)
    try:
        value = node.to_py()
    except (ArithmeticError, TypeError, ValueError):
        return None
    if isinstance(value, bool):  # sqlglot never does this; be explicit anyway
        return None
    number = value if isinstance(value, int) else float(value)
    return -number if negated else number


def _option_got(node: exp.Expr, value: object) -> str:
    """How a rejected option value is echoed back in the message."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str):
        return repr(value)
    return _describe(_unwrap(node))


def _option_error(
    filter_name: str, option: FilterOption, arg: _NamedArg, call: exp.Expr, what: str, hint: str
) -> SqlmpegError:
    return _error(
        ErrorCode.FILTER_OPTION_TYPE,
        f"option '{option.name}' of filter '{filter_name}' {what}",
        arg.value,
        fallback=call,
        hint=hint,
    )


def _option_value(
    filter_name: str, option: FilterOption, arg: _NamedArg, call: exp.Expr
) -> object:
    """One named argument's value, checked against its introspected AVOption.

    The type map is the RFC's: numeric AVOptions take a bare number (range
    checked whenever ffmpeg printed a parseable one), booleans take ``true`` /
    ``false``, an enum takes one of its named constants, and everything else
    takes a string — or a bare number, since an ffmpeg option value is text on
    the command line either way and ``duration``/``video_rate``/expression
    options (``xfade``'s ``duration``, ``crop``'s ``x``) are routinely numeric.
    """
    value = _literal_value(arg.value)
    got = _option_got(arg.value, value)
    if option.unusable:
        raise _option_error(
            filter_name,
            option,
            arg,
            call,
            "has an ffmpeg type (binary/dictionary) sqlmpeg cannot set",
            "drop it; sqlmpeg sets numeric, string and boolean options only",
        )
    if option.type == "num":
        if not isinstance(value, int | float) or isinstance(value, bool):
            bounds = _range_text(option)
            raise _option_error(
                filter_name,
                option,
                arg,
                call,
                f"expects a number, got {got}",
                f"write a bare numeric literal ({bounds})" if bounds else
                "write a bare numeric literal, e.g. sigma => 5",
            )
        bounds = _range_text(option)
        below = option.minimum is not None and value < option.minimum
        above = option.maximum is not None and value > option.maximum
        if (below or above) and bounds is not None:
            raise _option_error(
                filter_name,
                option,
                arg,
                call,
                f"accepts a number {bounds}, got {got}",
                f"pick a value {bounds}",
            )
        return value
    if option.type == "bool":
        if not isinstance(value, bool):
            raise _option_error(
                filter_name,
                option,
                arg,
                call,
                f"expects true or false, got {got}",
                "write the bare word true or false, with no quotes",
            )
        return value
    if option.constants:
        if not isinstance(value, str) or value not in option.constants:
            constants = _listed(option.constants)
            matches = (
                difflib.get_close_matches(value, list(option.constants), n=1, cutoff=0.6)
                if isinstance(value, str)
                else []
            )
            raise _option_error(
                filter_name,
                option,
                arg,
                call,
                f"expects one of its named constants ({constants}), got {got}",
                f"did you mean '{matches[0]}'?"
                if matches
                else "the value is a single-quoted constant name, not a number",
            )
        return value
    if isinstance(value, str) or (isinstance(value, int | float) and not isinstance(value, bool)):
        return value
    raise _option_error(
        filter_name,
        option,
        arg,
        call,
        f"expects a string, got {got}",
        "write a single-quoted string literal, e.g. flags => 'lanczos'",
    )


def _stream_positions(variant: tuple[Param, ...]) -> list[int]:
    """Every parameter position of `variant` whose kind is a stream type, in order."""
    return [i for i, param in enumerate(variant) if param.kind in ("video", "audio")]


def _sql_text(node: exp.Expr) -> str:
    """The argument as the user wrote it, for a BROADCAST_MISMATCH message.

    ``dialect="postgres"`` matters: it re-adds the ``INDEX_OFFSET`` sqlglot
    subtracted at parse time, so ``a.audio[2]`` renders as ``a.audio[2]``.
    """
    return str(node.sql(dialect="postgres"))


def _stream_count(count: int) -> str:
    return f"{count} stream" + ("" if count == 1 else "s")


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


def lower(
    res: Resolved,
    probes: dict[str, ProbeResult | None],
    *,
    registry: Registry | None = None,
    portable: bool = False,
) -> Graph:
    """Lower a resolved query into an IR graph.

    `probes` is keyed by input ALIAS (``compiler.compile_sql`` builds it, one
    ``probe()`` per distinct path); a missing or ``None`` entry means that
    input could not be read, and lowering stays symbolic for it.

    `registry` is the tier-2 half of the function surface (RFC-003): the filter
    set of the ffmpeg on PATH, introspected lazily. It is a PARAMETER rather
    than a module lookup so that a caller — ``compile_sql``, or a test — decides
    whether this compile may consult the local ffmpeg at all. None means it may
    not: every name is then resolved against the stdlib alone.

    `portable` only changes what a rejection SAYS. None registry + portable
    means the caller turned tier 2 off deliberately (``--portable``); None
    registry without it means ffmpeg was not found. Both reject the same
    queries, so a query that compiles portably compiles everywhere.

    Raises ``SqlmpegError`` — and nothing else — on every rejection.
    """
    try:
        return _Lowerer(res, probes, registry, portable).run()
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
