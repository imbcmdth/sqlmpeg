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
  ``FROM <cte>`` later exposes those columns by their ``AS`` names. A script's
  VIEWS are CTEs here (RFC-006): ``Resolved.ctes`` holds both, so the whole
  binding table is lowered exactly ONCE no matter how many COPYs read it.
* Then one :class:`~sqlmpeg.ir.SinkUnit` per ``COPY``, in script order, each
  from that COPY's own query (RFC-006) — or, for a bare SELECT, a single unit
  with ``path=None``. Every unit shares this graph's nodes, so a view read by
  three COPYs is decoded and filtered once and fanned out by the split pass.
* Inside a branch, ``FROM`` builds a typed environment: an ``input()`` alias
  exposes per-type stream access (``a.video[1]`` -> ``"src:a:v:0"``; SQL
  subscripts are 1-based, IR indices 0-based), a CTE alias exposes its
  recorded columns (under its own name, or under a branch-local alias:
  ``FROM master m``), and a ``ffmpeg.<source>(...)`` alias exposes exactly one
  statically-typed stream (see below).
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
  stream it carries (an array column splats into consecutive Outputs). A call
  type-checks its stream arguments against the filter's pad signature and its
  option arguments against that filter's introspected AVOptions (see "One
  calling convention" below).

Generated sources: ``FROM ffmpeg.<source>(...) a`` (RFC-005 §1)
--------------------------------------------------------------
A source alias is the third kind of binding (:class:`_SourceBinding`), and
it is the registry surface in TABLE position: the name resolves through
``Registry.get_source`` alone (never ``get``), and its options through the
same ``Registry.options`` path a call's named arguments take, with the same
two codes.

What makes it different from an ``input()`` alias is that there is no FILE:

* no ``-i``, so no input index — a source appears in neither
  ``Graph.input_paths`` nor ``Graph.sources``, and ``compile_sql`` never
  probes it (it probes ``Resolved.sources``, which a source alias is not in);
* it lowers to a ZERO-INPUT node, ``Node(filter=<source>, args=<options>,
  inputs=[], outputs=[<type>])``, minted lazily on first column access and
  memoized on the binding, so fan-out is the split pass's ordinary business
  and never a second generator;
* one output pad means one stream of one statically-known type, so every
  column rule is answered without a probe: ``a.frame``/``a.video[1]`` on a
  video source, ``a.audio[1]`` on an audio one, a bare ``a.video``/``a.audio``
  that is an array of LENGTH 1, ``a.*`` = that one column, and
  ``STREAM_NOT_FOUND`` (naming the source and what it produces) for the other
  type or any subscript but ``[1]``;
* ``WHERE a.t`` is rejected: nothing was read, so there is no timeline to
  seek — a source's length is its own ``duration =>`` option;
* provenance is always empty, for the same reason (nothing probed).

Everything else is ordinary. A source is legal in a CTE body and in a UNION
ALL branch — silent-audio-for-concat, ``SELECT t.video[1], s.audio[1] FROM
ffmpeg.testsrc2(...) t, ffmpeg.anullsrc(...) s`` as the second branch of a
concat, is the motivating case — and the node it builds is one split, emit
and the goldens cannot tell apart from any other.

One calling convention (RFC-007, plan 051)
------------------------------------------
Every call is an ffmpeg filter, spelled the way ffmpeg's own filtergraph
syntax spells it::

    <name>(<stream inputs...>, <positional options...>, <named options...>)

There is no curated stdlib and no tier system: a name resolves in the
``registry`` — the filter set of the ffmpeg on PATH — and nowhere else. What
compiles therefore depends on what that ffmpeg reports, and an empty registry
(no ffmpeg) means every call name is simply UNKNOWN, not an INTERNAL error.

* STREAM INPUTS come first, count and types straight from the pad signature
  (``gblur`` is ``V->V``, ``xfade`` is ``VV->V``). A count or type mismatch
  against that signature is ``UDF_ARG_TYPE`` — the code's whole remaining job.
* POSITIONAL OPTIONS follow, binding to the filter's options in REGISTRY
  ORDER, which is ffmpeg's AVOption declaration order and therefore exactly
  the order ``gblur=5:2`` binds in a filtergraph (see
  ``sqlmpeg/registry.py``'s dedup docstring for why the deduped list is that
  order, and what had to be fixed for it to be). ``crop(f, 100, 50, 10, 20)``
  is ``crop=out_w=100:out_h=50:x=10:y=20``; ``scale(f, 640, 480)`` is
  ``scale=width=640:height=480``. A positional binds AS the option it lands
  on and is validated as that option — same type/range/enum checks, same two
  codes — so option problems are uniformly ``UNKNOWN_FILTER_OPTION`` /
  ``FILTER_OPTION_TYPE`` whether the option was written positionally or by
  name. More positionals than the filter has options is ``UDF_ARG_TYPE``,
  naming that count.
* NAMED OPTIONS (``sigma => 5``) come last. Mixing rules: a positional after
  a named is ``UNSUPPORTED_SQL`` (:func:`_split_args`, resolve's rule), and a
  named that collides with an option already bound positionally is
  ``FILTER_OPTION_TYPE`` — a named argument never silently overrides one.
* ``enable`` stays NAMED-ONLY and framework-level: it is in no filter's option
  table, so it can never be reached positionally, and it is admitted by the
  ``T`` flag alone (RFC-005 §2).
* ``ffmpeg.<filter>(...)`` is the same call under a name no SQL grammar can
  claim (plan 038): identical semantics, but it bypasses Postgres's special
  forms, so ``ffmpeg.overlay(base, top, x => 20, eof_action => 'pass')``
  reaches the option set the ``OVERLAY..PLACING`` grammar hides, and
  ``ffmpeg.trim(...)`` / ``ffmpeg.format(...)`` arrive with their arguments
  intact. It is REQUIRED for the census's eleven collided names and optional
  everywhere else. The node it builds carries the FILTER's name, so nothing
  downstream knows the namespace exists.
* Three ``->N`` filters are callable through that namespace despite the pad
  fence, because their output COUNT is fixed by an option: ``channelsplit``,
  ``acrossover`` and ``extractplanes`` (RFC-006, :data:`ARRAY_RETURNING`).
  Each lowers to ONE node with N output pads and RETURNS an array — the first
  call that does — so its result splats into a SELECT list, subscripts out of
  a CTE column and broadcasts elementwise like any other array. The table is
  consulted before the registry's verdict, since the registry has no entry to
  give; every other fenced name keeps its ``UNKNOWN_FUNCTION``.
* Three ``N->1`` filters are re-admitted the mirror way (:data:`N_INPUT`,
  plan 051): ``amix``, ``hstack`` and ``vstack`` take a variable number of
  INPUT pads fixed by their ``inputs`` option, so the pad fence excludes them
  too, yet the count is statically knowable the moment that option is read.
  Their leading stream arguments ARE the input pads and their `inputs` option
  must agree with how many were supplied (``UDF_ARG_TYPE`` naming both
  numbers when it does not). Unlike the array trio these are reachable BARE
  as well as namespaced — no Postgres grammar claims their names.
* ``sqlmpeg.<name>(...)`` (RFC-007, plan 052) is a THIRD namespace, resolved
  against :data:`sqlmpeg.macros.MACROS` and NEVER the registry -- macros work
  offline, with no ffmpeg on PATH at all. A macro owns its own fixed
  positional signature (no named arguments, no option table) and expands to a
  small filter subgraph (:data:`sqlmpeg.macros.Macro.expand`); its one stream
  argument broadcasts elementwise through the same :meth:`_expand_call` every
  other call uses.
* Broadcasting and zipping run off the stream-argument POSITIONS, which are
  always the leading ones, so ``volume(a.audio, 0.5)`` and
  ``anlmdn(a.audio, s => 0.01)`` expand identically.
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
by :class:`_NodeFactory`.

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
  before lowering ever sees the call, so a BARE ``overlay`` can take its
  options positionally but never by name. Eleven registry names collide with a
  Postgres special form this way (see docs/dynamic-filters.md for the census);
  ``ffmpeg.<filter>(...)`` is the spelling that reaches every one of them,
  because the special-form grammars key on a BARE name and a qualified call
  parses as ``Dot(Identifier(ffmpeg), Anonymous(...))`` no matter what the
  filter is called. (All of this is surface-level sqlglot behavior, not a
  lowering rule.)
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
from sqlmpeg.inputs import validate_option as validate_input_option
from sqlmpeg.ir import FrameRef, Graph, Node, Output, SinkUnit, StreamType
from sqlmpeg.macros import MACROS
from sqlmpeg.parser import (
    FILTER_NAMESPACE,
    MACRO_NAMESPACE,
    RawSink,
    RawSource,
    Resolved,
    _pos,
    _time_bounds,
    kwarg_name,
    star_qualifier,
    subscript_index,
    union_branches,
)
from sqlmpeg.parser import _ident_name as _fold
from sqlmpeg.probe import ProbeResult, StreamMeta
from sqlmpeg.registry import DynamicFilter, FilterOption, Registry, SourceFilter
from sqlmpeg.sink import validate_option as validate_sink_option

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
# literal nor a stream-typed subexpression (e.g. `1 + 2`, NULL, TRUE). The
# angle brackets keep it from ever colliding with a StreamType name.
_UNSUPPORTED_KIND = "<expr>"

# Kind labels :meth:`_Lowerer._classify` gives a stream-valued argument -- the
# only kinds that may occupy a call's leading (stream input) positions.
_STREAM_KINDS: frozenset[str] = frozenset({"video", "audio", "subtitle", "data"})

# Provenance tags copied onto a passthrough Output. "und" is what mp4 muxers
# stamp on untagged streams; it carries no information, so it is not copied.
_PROVENANCE_KEYS = ("language", "title")
_UNDEFINED_LANGUAGE = "und"

_TIME_HINT = (
    "<alias>.t is only usable as WHERE <alias>.t BETWEEN <start> AND <end>, "
    "<alias>.t >= <start>, or <alias>.t <= <end>"
)
_STREAM_HINT = "a SELECT column must be a stream, e.g. a.video[1] or scale(a.frame, 640, -2)"
_SUBSCRIPT_HINT = "stream subscripts are 1-based: a.video[1] is the first video stream"
_ZIP_HINT = (
    "broadcast arrays zip elementwise, one output per element; "
    "subscript one of them to pair a single stream with the other, e.g. a.audio[1]"
)
_NO_REGISTRY_HINT = (
    "sqlmpeg's function surface IS your installed ffmpeg's filter set; install "
    "ffmpeg, or put it on PATH"
)
_PASSTHROUGH_HINT = (
    "subtitle and data streams can only be selected (and copied), never filtered; "
    "drop them from the call and select them as their own column"
)
_SOURCE_DURATION_HINT = (
    "a generated source has no timeline to seek into; give it a length with "
    "its own option instead, e.g. ffmpeg.anullsrc(duration => 30) s"
)
_CAPTION_TRIM_HINT = (
    "trim the video/audio without selecting the subtitle/data columns, or select "
    "them in a query without a WHERE time range; to caption a trimmed clip, join "
    "an external subtitle file whose cues are timed for the cut"
)

# -- timeline `enable` (RFC-005 SS2) ---------------------------------------
#
# `enable` is FRAMEWORK-level: ffmpeg implements it in the filter framework,
# not in any filter, so it never appears in a filter's `-help` AVOptions and
# no options table can ever contain it. Which filters honour it is the `T`
# column of `ffmpeg -filters`, captured as DynamicFilter.timeline (plan 040),
# and that flag is what admits the name here.
_ENABLE = "enable"
_ENABLE_HINT = (
    "enable takes a single-quoted ffmpeg timeline expression over t (seconds), "
    "n (frame number) or pos, e.g. enable => 'between(t,2,5)'"
)
_NO_TIMELINE_HINT = (
    "enable is only accepted by filters your ffmpeg flags with timeline support "
    "(the T column of `ffmpeg -filters`: gblur has it, scale does not); drop it, "
    "or express the timing with a WHERE window over the input"
)

# Longest option/constant list a hint or message renders before it stops
# counting (xfade's `transition` alone has 59 constants).
_MAX_LISTED = 12


# ---------------------------------------------------------------------------
# array-RETURNING filters (RFC-006, plan 047)
# ---------------------------------------------------------------------------
#
# Three ffmpeg filters take ONE input pad and produce a number of output pads
# that is fixed, statically, by one of their options. The registry's v1 pad
# fence excludes all three (their `-filters` spec is `A->N` / `V->N`, and `N`
# is excluded wholesale), so `Registry.get` says None for them and they are
# not callable as tier-2 filters. This table is what re-admits exactly those
# three, and it lives here rather than in the registry because the count rule
# is a property of the OPTION SEMANTICS, which nothing ffmpeg prints exposes:
# the registry keeps saying `A->N`, and lowering keeps the arithmetic.
#
# Re-admitted through the `ffmpeg.<filter>(...)` namespace only (RFC-006:
# "Remains namespace-only"). A bare `channelsplit(...)` stays UNKNOWN_FUNCTION,
# exactly as every other fenced name does -- the namespace is where a call
# that needs a special shape is spelled.
#
# The result is an ARRAY value: `Node(outputs=[element]*N)` plus one `_Stream`
# per pad, `is_array=True` even when N == 1 (a one-element array still splats,
# subscripts through a CTE column, and broadcasts). Its pads are ordinary pads
# -- consume-once, so a pad read by two sinks gets an `asplit` from the split
# pass like any other.


@dataclass(frozen=True)
class _BadCount:
    """A count rule's rejection: which option said what, and what was expected."""

    option: str
    value: str
    expected: str
    hint: str


@dataclass(frozen=True)
class _ArrayFilter:
    """One array-returning filter: its pads, and how an option fixes its count."""

    name: str
    input: StreamType  # its single input pad
    element: StreamType  # what every one of its output pads carries
    count: Callable[[dict[str, object]], int | _BadCount]


# `ffmpeg -layouts` (7.1), "Standard channel layouts": name -> how many
# channels its decomposition lists. Data, verbatim -- the whole table ffmpeg
# printed, not a curated subset of it, so the only layouts a query can be
# rejected for are the ones this ffmpeg would reject too.
_CHANNEL_LAYOUTS: dict[str, int] = {
    "mono": 1,
    "stereo": 2,
    "2.1": 3,
    "3.0": 3,
    "3.0(back)": 3,
    "4.0": 4,
    "quad": 4,
    "quad(side)": 4,
    "3.1": 4,
    "5.0": 5,
    "5.0(side)": 5,
    "4.1": 5,
    "5.1": 6,
    "5.1(side)": 6,
    "6.0": 6,
    "6.0(front)": 6,
    "3.1.2": 6,
    "hexagonal": 6,
    "6.1": 7,
    "6.1(back)": 7,
    "6.1(front)": 7,
    "7.0": 7,
    "7.0(front)": 7,
    "7.1": 8,
    "7.1(wide)": 8,
    "7.1(wide-side)": 8,
    "5.1.2": 8,
    "octagonal": 8,
    "cube": 8,
    "5.1.4": 10,
    "7.1.2": 10,
    "7.1.4": 12,
    "7.2.3": 12,
    "9.1.4": 14,
    "hexadecagonal": 16,
    "downmix": 2,
    "22.2": 24,
}

# `ffmpeg -layouts` (7.1), "Individual channels": the names a custom layout is
# composed of with `+` (`FL+FR`, `FC+LFE`), which ffmpeg accepts anywhere a
# standard layout name is accepted.
_CHANNEL_NAMES: frozenset[str] = frozenset(
    {
        "FL", "FR", "FC", "LFE", "BL", "BR", "FLC", "FRC", "BC", "SL", "SR",
        "TC", "TFL", "TFC", "TFR", "TBL", "TBC", "TBR", "DL", "DR", "WL", "WR",
        "SDL", "SDR", "LFE2", "TSL", "TSR", "BFC", "BFL", "BFR", "SSL", "SSR",
        "TTL", "TTR",
    }
)

_LAYOUT_HINT = (
    "a channel layout is one of ffmpeg's standard names (see `ffmpeg -layouts`) "
    "or a '+'-joined list of channel names, e.g. 'stereo', '5.1', 'FL+FR'"
)
_SPLIT_HINT = (
    "acrossover splits at a list of positive frequencies separated by spaces or "
    "'|', e.g. split => '500' (2 bands) or split => '500|3000' (3 bands)"
)
_PLANES_HINT = (
    "planes names the planes to extract, e.g. planes => 'y'; your ffmpeg types "
    "it as an enum, so only ONE plane per call is accepted here"
)


def _option_text(value: object) -> str:
    """A validated option value as the text ffmpeg will be handed."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _channel_count(text: str) -> int | None:
    """How many channels a layout spelling describes, or None if unrecognized."""
    standard = _CHANNEL_LAYOUTS.get(text)
    if standard is not None:
        return standard
    parts = text.split("+")
    if parts and all(part in _CHANNEL_NAMES for part in parts):
        return len(parts)
    return None


def _channelsplit_count(args: dict[str, object]) -> int | _BadCount:
    """One output pad per channel channelsplit is asked to extract.

    `channels` (default "all") wins when it is set to anything else: it is
    itself a layout spelling naming the SUBSET to split out, so
    `channels => 'FL'` is one pad however wide `channel_layout` is. Verified
    against ffmpeg 7.1 -- a graph that labels more pads than the filter has is
    a hard "More output link labels specified ... than it has outputs" error,
    so the count has to follow both options, not just the documented one.
    """
    channels = _option_text(args.get("channels", "all"))
    if channels != "all":
        count = _channel_count(channels)
        if count is None:
            return _BadCount("channels", channels, "a channel layout", _LAYOUT_HINT)
        return count
    layout = _option_text(args.get("channel_layout", "stereo"))
    count = _channel_count(layout)
    if count is None:
        return _BadCount(
            "channel_layout",
            layout,
            f"one of {_listed(_CHANNEL_LAYOUTS)}",
            _LAYOUT_HINT,
        )
    return count


def _acrossover_count(args: dict[str, object]) -> int | _BadCount:
    """One band per split frequency, plus the band below the lowest one."""
    split = _option_text(args.get("split", "500"))
    parts = split.replace("|", " ").split()
    ok = bool(parts)
    for part in parts:
        try:
            frequency = float(part)
        except ValueError:
            ok = False
            break
        if not frequency > 0:
            ok = False
            break
    if not ok:
        return _BadCount("split", split, "a list of positive frequencies", _SPLIT_HINT)
    return len(parts) + 1


def _extractplanes_count(args: dict[str, object]) -> int | _BadCount:
    """One output pad per requested plane.

    ffmpeg's own option is a `flags` set (`y+u+v`), but the registry types an
    option that lists constants as an enum, so `_option_value` accepts exactly
    one of them and a `+`-joined value is FILTER_OPTION_TYPE before this rule
    ever runs. The `+` arithmetic is written out anyway: it is what the option
    means, and it is what a later plan widening flags handling will need.
    """
    planes = _option_text(args.get("planes", "r"))
    parts = planes.split("+")
    if not parts or not all(parts):
        return _BadCount("planes", planes, "one or more plane names", _PLANES_HINT)
    return len(parts)


ARRAY_RETURNING: dict[str, _ArrayFilter] = {
    "channelsplit": _ArrayFilter(
        name="channelsplit",
        input="audio",
        element="audio",
        count=_channelsplit_count,
    ),
    "acrossover": _ArrayFilter(
        name="acrossover",
        input="audio",
        element="audio",
        count=_acrossover_count,
    ),
    "extractplanes": _ArrayFilter(
        name="extractplanes",
        input="video",
        element="video",
        count=_extractplanes_count,
    ),
}

_ARRAY_INPUT_HINT = (
    "an array-returning filter takes exactly one stream, because its own result "
    "is the array; subscript the argument, e.g. a.audio[1]"
)


# ---------------------------------------------------------------------------
# fixed-count N-INPUT filters (plan 051)
# ---------------------------------------------------------------------------
#
# The mirror image of ARRAY_RETURNING. Three ffmpeg filters take a number of
# INPUT pads that is fixed, statically, by one of their options and produce
# exactly one output pad. Their `-filters` spec is `N->A` / `N->V`, so the v1
# pad fence excludes all three and `Registry.get` says None for them -- yet
# the count is knowable the moment the option is read, which is the same
# argument that re-admits the array-returning trio.
#
# Re-admitted under BOTH spellings, bare and namespaced: unlike the array
# trio, none of these three names collides with a Postgres special form, and
# `amix(a, b)` is the spelling everybody already writes.
#
# The mechanism generalizes to any `N->1` filter whose input count is one
# option (`concat`'s `n`, `mix`, `interleave`, ...); it is deliberately
# scoped to these three for now, since the general variadic/array-CONSUMING
# story (passing one array where N streams are wanted) is queued separately.


@dataclass(frozen=True)
class _NInputFilter:
    """One fixed-count N-input filter: its pads, and the option fixing the count."""

    name: str
    stream: StreamType  # what every one of its INPUT pads carries
    output: StreamType  # its single output pad
    option: str  # the option whose value IS the input-pad count
    fallback: int  # count when the option is neither written nor introspectable


N_INPUT: dict[str, _NInputFilter] = {
    "amix": _NInputFilter(name="amix", stream="audio", output="audio", option="inputs", fallback=2),
    "hstack": _NInputFilter(
        name="hstack", stream="video", output="video", option="inputs", fallback=2
    ),
    "vstack": _NInputFilter(
        name="vstack", stream="video", output="video", option="inputs", fallback=2
    ),
}

_N_INPUT_HINT = (
    "the number of streams you pass IS the filter's input count; either pass "
    "that many streams, or set the count explicitly, e.g. amix(a, b, c, inputs => 3)"
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
    """A function call as lowering sees it: a name, positional args, named args.

    `namespaced` marks the ``ffmpeg.<filter>(...)`` spelling (plan 038), which
    resolves in the registry under a name no Postgres grammar can claim.
    `is_macro` marks the ``sqlmpeg.<name>(...)`` spelling (plan 052), which
    resolves against :data:`MACROS` and never touches the registry. The two
    are mutually exclusive (different Dot qualifiers).
    """

    name: str
    args: list[exp.Expr]
    named: list[_NamedArg]
    namespaced: bool = False
    is_macro: bool = False

    @property
    def display(self) -> str:
        """The call as the user spelled it, for error messages."""
        if self.namespaced:
            return f"{FILTER_NAMESPACE}.{self.name}"
        if self.is_macro:
            return f"{MACRO_NAMESPACE}.{self.name}"
        return self.name


def _namespaced_call(node: exp.Expr) -> exp.Anonymous | None:
    """The ``exp.Anonymous`` inside ``ffmpeg.<filter>(...)``, else None.

    VERIFIED (sqlglot 30.17, ``read="postgres"``): a qualified call parses as
    ``exp.Dot(this=Identifier(ffmpeg), expression=exp.Anonymous(...))`` for
    EVERY filter name, with its positional arguments and its ``=>`` kwargs
    intact inside the ``Anonymous``. Postgres's special-form grammars —
    ``OVERLAY(x PLACING y ...)``, ``TRIM``, ``FORMAT``, ``MEDIAN``, ... — key
    on a BARE name, so qualifying the call bypasses all of them at once. That
    is the whole point of the namespace: it is the one spelling of a filter
    name that no SQL grammar has an opinion about.
    """
    if not isinstance(node, exp.Dot):
        return None
    qualifier = node.this
    if not isinstance(qualifier, exp.Identifier) or _fold(qualifier) != FILTER_NAMESPACE:
        return None
    inner = node.args.get("expression")
    return inner if isinstance(inner, exp.Anonymous) else None


def _macro_call(node: exp.Expr) -> exp.Anonymous | None:
    """The ``exp.Anonymous`` inside ``sqlmpeg.<name>(...)``, else None.

    Mirrors :func:`_namespaced_call` exactly, and VERIFIED (plan 052) to parse
    to the identical shape under sqlglot 30.17 ``read="postgres"`` for all
    three macro names: ``exp.Dot(this=Identifier(sqlmpeg),
    expression=exp.Anonymous(this=<macro>, expressions=[...]))``, symmetric
    with plan 038's ffmpeg-namespace findings.
    """
    if not isinstance(node, exp.Dot):
        return None
    qualifier = node.this
    if not isinstance(qualifier, exp.Identifier) or _fold(qualifier) != MACRO_NAMESPACE:
        return None
    inner = node.args.get("expression")
    return inner if isinstance(inner, exp.Anonymous) else None


def _call_parts(node: exp.Expr) -> _Call | None:
    """The call `node` is, else None.

    ``exp.Overlay`` is normalized back to the four positional arguments the
    SQL surface uses; sqlglot parks them under named keys because Postgres
    spells the builtin ``OVERLAY(x PLACING y FROM n FOR m)``. (That builtin
    grammar also means a BARE ``overlay(...)`` cannot take named arguments at
    all: sqlglot rejects ``=>`` inside it at PARSE time. Its options are still
    reachable positionally, and ``ffmpeg.overlay(base, top, x => 20, y => 20)``
    reaches every one of them by name.)

    Named arguments arrive as ``exp.Kwarg`` among the positional ones and are
    split out here. Their TRAILING position is enforced by resolve; the check
    is repeated defensively because a Kwarg among positional args would
    otherwise silently shift every parameter after it.
    """
    inner = _namespaced_call(node)
    if inner is not None:
        return _split_args(str(inner.this), inner, namespaced=True)
    macro_inner = _macro_call(node)
    if macro_inner is not None:
        return _split_args(str(macro_inner.this), macro_inner, is_macro=True)
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


def _split_args(
    name: str, call: exp.Expr, *, namespaced: bool = False, is_macro: bool = False
) -> _Call:
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
    return _Call(name, positional, named, namespaced, is_macro)


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


def _input_value(node: exp.Expr) -> object:
    """One `input('path', name => value)` value as a python scalar.

    Mirrors :func:`_sink_value`, with one addition: INPUT_OPTIONS has a
    ``"num"`` type (``framerate``, ``itsoffset``) whose value may carry a
    leading ``-`` -- ``itsoffset`` legitimately takes a negative offset.
    ``exp.Neg`` is unwrapped first, the same rule :func:`_number` applies to
    positional numeric literals. Never raises: an unusable shape comes back
    as an :class:`_Unrepresentable`, exactly like `_sink_value`, and the
    option table decides.
    """
    node = _unwrap(node)
    if not (isinstance(node, exp.Neg) and isinstance(node.this, exp.Expr)):
        return _sink_value(node)
    inner = _sink_value(_unwrap(node.this))
    if isinstance(inner, int | float) and not isinstance(inner, bool):
        return -inner
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


@dataclass
class _SourceBinding:
    """``FROM ffmpeg.<source>(...) a`` — exposes ONE statically-typed stream.

    RFC-005 §1. Everything about the stream is known before any projection
    lowers: the registry's :class:`~sqlmpeg.registry.SourceFilter` says which
    type the source's single output pad carries, so ``a.video[1]`` /
    ``a.frame`` (video sources), ``a.audio[1]`` (audio ones), the bare array
    ``a.video`` (length 1, statically), and ``a.*`` are all answered without
    a probe — there is no file to probe, and no ``-i``: the source is a
    ZERO-INPUT filter node.

    `options` is already validated against the source's introspected option
    table (the exact same ``Registry.options`` path a tier-2 call's named
    arguments take), because that happens when the FROM clause binds, not
    when a column is read.

    Mutable on purpose: `ref` memoizes the node, which is minted lazily on
    the FIRST column access and shared by every later one. Fan-out beyond
    that is the split pass's job, exactly as for any other node, so
    ``SELECT a.frame, hflip(a.frame) FROM ffmpeg.testsrc(...) a`` is one
    ``testsrc`` plus a ``split``, never two generators.
    """

    alias: str
    name: str  # the ffmpeg source filter's name, e.g. "testsrc"
    output: StreamType
    options: dict[str, object]
    ref: FrameRef | None = None

    @property
    def display(self) -> str:
        """The source as the user spelled it, for error messages."""
        return f"{FILTER_NAMESPACE}.{self.name}"


_Binding = _InputBinding | _CteBinding | _SourceBinding


@dataclass
class _Env:
    """Everything one SELECT branch resolves names against."""

    bindings: dict[str, _Binding] = field(default_factory=dict)
    # CTE name -> its WHERE window. CTE-ONLY: an INPUT alias's window is a
    # property of its `-i`, not of this branch, so `_collect_trims` records it
    # in `Graph.input_trims` instead and no filter trim is ever spliced for it.
    # Plan 039: either half may be None (open-ended window).
    trims: dict[str, tuple[int | float | None, int | float | None]] = field(
        default_factory=dict
    )
    # base stream ref -> its trimmed ref, so one filter trim is shared by every
    # consumer of that stream inside this branch (CTE-only, as above).
    trimmed: dict[FrameRef, FrameRef] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# ExpandCtx
# ---------------------------------------------------------------------------


class _NodeFactory:
    """Mints ``n1, n2, ...`` node ids into a graph, in creation order."""

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
    ) -> None:
        self.res = res
        self.probes = probes
        self.registry = registry
        self.graph = Graph(input_paths=list(res.input_paths), sources=dict(res.sources))
        self.ctx = _NodeFactory(self.graph)
        self.cte_columns: dict[str, tuple[_Column, ...]] = {}

    # -- entry point ------------------------------------------------------

    def run(self) -> Graph:
        """Lower every CTE/view once, then one :class:`SinkUnit` per COPY.

        The bindings come first and are shared: ``res.ctes`` holds a script's
        views AND every COPY's own ``WITH``, in written order (RFC-006), and
        each is lowered into THIS graph exactly once. A view read by three
        COPYs therefore mints its nodes once and hands the same refs to all
        three — the fan-out is the split pass's ordinary business, which is
        the whole point of the ABR ladder compiling to one ffmpeg command.

        ``res.select`` / ``res.branches`` are read for the BARE-SELECT case
        only (a query with no COPY at all, which is the one unit whose path
        is None). When there are sinks they are just a mirror of ``sinks[0]``
        and walking them again would lower the first group twice.
        """
        for name, body in self.res.ctes.items():
            self.cte_columns[name] = tuple(
                self._lower_query(union_branches(body), body)
            )
        if self.res.sinks:
            self.graph.sinks = [self._lower_sink(raw) for raw in self.res.sinks]
        else:
            columns = self._lower_query(self.res.branches, self.res.select)
            self.graph.sinks = [SinkUnit(outputs=_outputs(columns))]
        self.graph.input_options = self._lower_input_options()
        return self.graph

    # -- the COPY sink (RFC-002, RFC-006) ----------------------------------

    def _lower_sink(self, raw: RawSink) -> SinkUnit:
        """One COPY: its own query lowered, its options validated.

        Each COPY carries a whole query of its own (``RawSink.query`` /
        ``.branches``, already validated by resolve), so a sink unit is that
        query's SELECT list plus the destination it names.

        Anchoring, VERIFIED against sqlglot 30.17: the option NAME (an
        ``exp.Var``) carries no token position, and neither does a ``Boolean``
        / ``Var`` / ``Null`` value, so the anchor falls back through the name
        node to the value node to the path literal — which at least keeps
        every rejection on (or just above) the ``WITH`` block.
        """
        columns = self._lower_query(list(raw.branches), raw.query)
        options: dict[str, object] = {}
        for option in raw.options:
            line, col = _pos(option.name_node, option.value, raw.path_node)
            options[option.name] = validate_sink_option(
                option.name, _sink_value(option.value), line=line, col=col
            )
        return SinkUnit(outputs=_outputs(columns), path=raw.path, options=options)

    # -- input() named options (RFC-005 SS4, plan 041) ---------------------

    def _lower_input_options(self) -> dict[str, dict[str, object]]:
        """Validate every `input('path', name => value, ...)`'s trailing options.

        Mirrors `_lower_sink`: anchor falls back through the name node to the
        value node to the input()'s own path literal, since neither a
        Kwarg's `Var` name nor a `Boolean`/`Var`/`Null` value carries a token
        position (same gap sink option names have).
        """
        result: dict[str, dict[str, object]] = {}
        for alias, raw_options in self.res.input_options.items():
            options: dict[str, object] = {}
            for option in raw_options:
                line, col = _pos(option.name_node, option.value, option.path_node)
                options[option.name] = validate_input_option(
                    option.name, _input_value(option.value), line=line, col=col
                )
            if options:
                result[alias] = options
        return result

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
            elif isinstance(binding, _SourceBinding):
                # A source has exactly one stream, so its star is that one
                # column -- statically, like everything else about it.
                columns.append(
                    _Column(name=None, value=_scalar(self._source_stream_of(binding)))
                )
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
        db = table.args.get("db")
        if isinstance(db, exp.Expr) and _fold(db) == FILTER_NAMESPACE:
            # `FROM ffmpeg.<source>(...) alias` (RFC-005 §1): resolve already
            # shape-checked it and parked the record in `res.source_filters`.
            self._add_source(table, alias_node, env, select)
            return
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
            # RFC-006: `FROM master m` binds the view/CTE under a BRANCH-LOCAL
            # name (resolve checked it shadows nothing in the flat namespace).
            # The binding records the local name, so `m.v` resolves and every
            # message about it reads back as the user wrote it; the columns —
            # and therefore the graph refs — are the same objects either way,
            # which is what makes the shared subgraph shared.
            local = name
            if isinstance(alias_node, exp.TableAlias) and alias_node.this is not None:
                local = _fold(alias_node.this)
            env.bindings[local] = _CteBinding(name=local, columns=columns)
            return
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            "only input('path') and CTE names are allowed in FROM",
            table,
            fallback=select,
        )

    def _known_hint(self) -> str:
        known = sorted(
            set(self.cte_columns) | set(self.graph.sources) | set(self.res.source_filters)
        )
        return f"known names: {', '.join(known)}" if known else "no aliases are in scope"

    # -- FROM ffmpeg.<source>(...) (RFC-005 §1, plan 042) ------------------

    def _add_source(
        self,
        table: exp.Table,
        alias_node: exp.Expr | None,
        env: _Env,
        select: exp.Select,
    ) -> None:
        """Bind one generated-source alias, options validated, no node yet.

        Resolution and option validation happen HERE, when the FROM clause
        binds, rather than at first column access: a source's options are
        checked against the installed ffmpeg exactly like a tier-2 call's
        named arguments, and that check is a property of the query, not of
        how many times a column of it is read. The NODE is what is deferred
        (:meth:`_source_stream_of`) — an alias no projection ever mentions
        contributes no filter, which is the one respect in which a source
        alias differs from an ``input()`` one (that always gets its ``-i``).
        """
        if not isinstance(alias_node, exp.TableAlias) or alias_node.this is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"{FILTER_NAMESPACE}.<source>() requires an alias",
                table,
                fallback=select,
                hint=f"add an alias, e.g. FROM {FILTER_NAMESPACE}.testsrc"
                "(duration => 2) t",
            )
        alias = _fold(alias_node.this)
        raw = self.res.source_filters.get(alias)
        if raw is None:  # defensive: resolve records every source alias
            raise _error(
                ErrorCode.UNKNOWN_ALIAS,
                f"unknown alias '{alias}'",
                alias_node,
                fallback=table,
                hint=self._known_hint(),
            )
        source = self._source_filter(raw, select)
        named = [_NamedArg(name=option.name, value=option.value) for option in raw.options]
        options = (
            self._filter_options(raw.name, raw.call_node, select) if named else {}
        )
        # No `timeline=`: SourceFilter has no such field, because a generator
        # is never timeline-capable -- there is no upstream frame to switch
        # on/off. `enable => ...` on a source rejects unconditionally.
        args = self._check_named_args(
            raw.name,
            options,
            named,
            raw.call_node,
            owner=f"{FILTER_NAMESPACE}.{raw.name}",
            occupied=set(),
        )
        env.bindings[alias] = _SourceBinding(
            alias=alias, name=raw.name, output=source.output, options=args
        )

    def _source_filter(self, raw: RawSource, select: exp.Select) -> SourceFilter:
        """The registry's entry for ``ffmpeg.<name>`` in FROM position, or a rejection.

        Three ways this fails, in the order they are told apart:

        * the name is a REGULAR filter of this ffmpeg (``ffmpeg.gblur``) — it
          has input pads, so it is a call, not a table: UNSUPPORTED_SQL saying
          so, the one fenced case that is positively identifiable;
        * there is no registry at all (no ffmpeg) — the standard
          unavailability wording, same as a namespaced CALL's;
        * the name is unknown to both tables — UNKNOWN_FUNCTION with a
          did-you-mean over ``source_names()``. Sources the v1 scope fence
          excluded (``avsynctest``'s ``|->AV``, ``movie``/``amovie``'s
          ``|->N``) are NOT retained by the registry at all, so they are
          indistinguishable from a typo here and land on the same rejection —
          which is why its fallback hint states the fence explicitly rather
          than only listing near-misses.
        """
        registry = self.registry
        source = registry.get_source(raw.name) if registry is not None else None
        if source is not None:
            return source
        if registry is not None and registry.get(raw.name) is not None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"{FILTER_NAMESPACE}.{raw.name} is an ffmpeg filter, not a source: "
                "it takes stream inputs, so it cannot stand in FROM",
                raw.call_node,
                fallback=select,
                hint=f"call it over a stream instead, e.g. SELECT "
                f"{FILTER_NAMESPACE}.{raw.name}(a.frame) FROM input('clip.mp4') a",
            )
        raise _error(
            ErrorCode.UNKNOWN_FUNCTION,
            f"unknown generated source {FILTER_NAMESPACE}.{raw.name}()",
            raw.call_node,
            fallback=select,
            hint=self._unknown_source_hint(raw.name),
        )

    def _unknown_source_hint(self, name: str) -> str:
        """Did-you-mean over ``source_names()``, then why the set might be missing.

        Mirrors :meth:`_namespaced_function_hint` branch for branch — the
        namespace is the same one, and a source is unavailable for exactly the
        same reasons a namespaced call is — but suggests only SOURCES, since
        a regular filter would not be usable in FROM either way.
        """
        registry = self.registry
        if registry is not None and registry.available():
            matches = difflib.get_close_matches(
                name, sorted(registry.source_names()), n=1, cutoff=0.6
            )
            if matches:
                return f"did you mean {FILTER_NAMESPACE}.{matches[0]}()?"
            return (
                f"FROM {FILTER_NAMESPACE}.<source>(...) takes a zero-input filter of "
                "your installed ffmpeg, and this is not one of them; sources with "
                "more than one output pad (avsynctest) or a variable pad count "
                "(movie, amovie) are not usable"
            )
        return (
            f"FROM {FILTER_NAMESPACE}.<source>(...) generates a stream with your "
            "installed ffmpeg; ffmpeg was not found on PATH"
        )

    def _source_stream_of(self, binding: _SourceBinding) -> _Stream:
        """The source's one stream, minting its node on first use only.

        The node is ``Node(filter=<source>, args=<validated options>,
        inputs=[], outputs=[<type>])`` — a chain head with no input labels
        (emit renders it as ``testsrc=duration=2[out0]``). Provenance is
        always empty: nothing was probed, because nothing was read.
        """
        if binding.ref is None:
            binding.ref = self.ctx.node(
                binding.name, dict(binding.options), [], [binding.output]
            )
        return _Stream(ref=binding.ref, type=binding.output, source=None)

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

        Plan 039 (open-ended windows): a conjunct may supply only a lower
        bound (``<alias>.t >= x``) or only an upper one (``<alias>.t <= y``),
        via :func:`sqlmpeg.parser._time_bounds`, which also normalizes the
        mirrored operand order (``x <= <alias>.t`` etc.) and flags a strict
        ``>``/``<`` so it is rejected here too. Two conjuncts for the same
        alias MERGE into one window (``t >= 1 AND t <= 2`` behaves exactly
        like ``t BETWEEN 1 AND 2``) — resolve already rejected a second bound
        of the same kind, so this only ever fills in the other half. Every
        check below duplicates one resolve already made (defensive re-check,
        as elsewhere in this pass).
        """
        where = select.args.get("where")
        if not isinstance(where, exp.Where):
            return
        windows: dict[str, tuple[int | float | None, int | float | None]] = {}
        for conjunct in _flatten_and(where.this):
            parsed = _time_bounds(conjunct)
            if parsed is None:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "unsupported WHERE predicate",
                    conjunct,
                    fallback=where,
                    hint=_TIME_HINT,
                )
            column, low, high, strict = parsed
            if strict:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "strict inequalities are not supported",
                    conjunct,
                    fallback=where,
                    hint=_TIME_HINT,
                )
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
            binding = env.bindings[alias]
            if isinstance(binding, _SourceBinding):
                # A generated source has no input file to seek and no
                # timeline to trim: it is a filter that MAKES a stream, and
                # how long a stream it makes is one of its own options.
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"'{alias}' is a generated source, so 'WHERE {alias}.t' has "
                    "nothing to seek",
                    conjunct,
                    fallback=where,
                    hint=_SOURCE_DURATION_HINT,
                )
            start, end = windows.get(alias, (None, None))
            if low is not None:
                start = _number(low, ErrorCode.UNSUPPORTED_SQL)
            if high is not None:
                end = _number(high, ErrorCode.UNSUPPORTED_SQL)
            windows[alias] = (start, end)

        for alias, window in windows.items():
            start, end = window
            if start is not None and end is not None and start >= end:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"empty time window for alias '{alias}': start ({start}) "
                    f"is not before end ({end})",
                    fallback=select,
                    hint="the start bound must be strictly before the end bound",
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
            if value.type in _PASSTHROUGH_ONLY and alias in self.graph.input_trims:
                # Measured, not theoretical: ffmpeg does not retime subtitle/data
                # packets under an input -ss (copy OR transcode; cue times stay
                # near-original while video rebases to zero), so a seeked caption
                # track plays out of sync by the seek amount. Reject rather than
                # ship silent desync. (2026-08-15 investigation; see RFC-004.)
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"'WHERE {alias}.t' cannot trim a selected {value.type} stream: "
                    "ffmpeg does not retime caption packets under an input seek, so "
                    "they would play out of sync with the trimmed video",
                    anchor,
                    fallback=select,
                    hint=_CAPTION_TRIM_HINT,
                )
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
        self,
        env: _Env,
        window: tuple[int | float | None, int | float | None],
        stream: _Stream,
    ) -> _Stream:
        """The trimmed counterpart of one stream; a trim is spliced once per stream.

        Plan 039: `window` may have either half absent (open-ended), so the
        ``trim``/``atrim`` node only gets the args it actually has --
        ``start=X``, ``end=Y``, or both, same as today.
        """
        cached = env.trimmed.get(stream.ref)
        if cached is not None:
            return _Stream(ref=cached, type=stream.type, source=stream.source)
        start, end = window
        args: dict[str, object] = {}
        if start is not None:
            args["start"] = start
        if end is not None:
            args["end"] = end
        if stream.type == "video":
            trimmed = self.ctx.node("trim", args, [stream.ref], ["video"])
            rebased = self.ctx.node(
                "setpts", {"expr": "PTS-STARTPTS"}, [trimmed], ["video"]
            )
        else:
            trimmed = self.ctx.node("atrim", args, [stream.ref], ["audio"])
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
        if isinstance(binding, _SourceBinding):
            return alias, self._source_value(binding, name, index, anchor, select)
        return alias, self._cte_value(binding, name, index, anchor, select)

    def _source_value(
        self,
        binding: _SourceBinding,
        name: str,
        index: int | None,
        anchor: exp.Expr,
        select: exp.Select,
    ) -> _Value:
        """One column of a generated-source alias — all of it statically known.

        A source has exactly ONE output pad, of exactly one type, so the whole
        column surface is decided by ``binding.output`` with no probe
        anywhere:

        * ``a.frame`` — sugar for ``a.video[1]``, and therefore VIDEO sources
          only; on an audio source it is a wrong-type column like any other.
        * ``a.video[1]`` / ``a.audio[1]`` — the stream, when the type matches.
        * bare ``a.video`` / ``a.audio`` — an ARRAY of length 1, so it splats
          into one Output and broadcasts a call exactly once. (Not a scalar:
          a length-1 array is still an array, the same distinction a
          single-track file's ``a.audio`` has.)
        * a subscript other than ``[1]``, or a column of the other type
          (``subtitle``/``data`` included) — STREAM_NOT_FOUND stating what the
          source does produce.
        * anything else — an unknown column.
        """
        produces = f"{binding.display} produces 1 {binding.output} stream"
        if name == _TIME_COLUMN:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{binding.alias}.t' is a time column, not a stream",
                anchor,
                fallback=select,
                hint=_SOURCE_DURATION_HINT,
            )
        if name == _FRAME_COLUMN:
            if binding.output != "video":
                raise _error(
                    ErrorCode.STREAM_NOT_FOUND,
                    f"'{binding.alias}.frame' does not exist: {produces}",
                    anchor,
                    fallback=select,
                    hint=f"'{binding.alias}.frame' is sugar for "
                    f"'{binding.alias}.video[1]'; write "
                    f"'{binding.alias}.{binding.output}[1]'",
                )
            if index is not None:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"'{binding.alias}.frame' is a single stream and cannot be "
                    "subscripted",
                    anchor,
                    fallback=select,
                    hint=f"'{binding.alias}.frame' is sugar for "
                    f"'{binding.alias}.video[1]'",
                )
            return _scalar(self._source_stream_of(binding))
        array_type = _ARRAY_COLUMNS.get(name)
        if array_type is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"unknown column '{binding.alias}.{name}'",
                anchor,
                fallback=select,
                hint=self._source_columns_hint(binding),
            )
        if array_type != binding.output:
            raise _error(
                ErrorCode.STREAM_NOT_FOUND,
                f"'{binding.alias}.{name}' does not exist: {produces}",
                anchor,
                fallback=select,
                hint=self._source_columns_hint(binding),
            )
        if index is None:
            return _array(binding.output, (self._source_stream_of(binding),))
        if index != 1:
            raise _error(
                ErrorCode.STREAM_NOT_FOUND,
                f"'{binding.alias}.{name}[{index}]' does not exist: {produces}",
                anchor,
                fallback=select,
                hint=_SUBSCRIPT_HINT,
            )
        return _scalar(self._source_stream_of(binding))

    def _source_columns_hint(self, binding: _SourceBinding) -> str:
        columns = [f"{binding.alias}.{binding.output}"]
        if binding.output == "video":
            columns.append(f"{binding.alias}.frame")
        return f"'{binding.display}' exposes {' and '.join(columns)}"

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
        """Resolve a call in the registry, and nowhere else (RFC-007).

        One convention, three shapes of filter, tried in the order that makes
        each reachable at all:

        * :data:`ARRAY_RETURNING` (namespaced spelling ONLY, RFC-006) and
          :data:`N_INPUT` (either spelling) come first, because both tables
          exist precisely for names the v1 pad fence keeps OUT of the registry
          — asking ``get`` about them first would answer "unknown" and the
          tables would never be reached;
        * then the registry proper, whose pad signature is the call's stream
          signature.

        ``ffmpeg.<filter>(...)`` differs from the bare spelling only in what a
        message calls the function (``call.display``) and in skipping the
        Postgres special forms at PARSE time.
        """
        name = call.name.lower()
        if call.is_macro:
            return self._lower_macro_call(node, name, call, env, select)
        if call.namespaced:
            options = self._array_options(name)
            if options is not None:
                return self._lower_array_call(
                    node, ARRAY_RETURNING[name], options, call, env, select
                )
        n_input = self._n_input_options(name)
        if n_input is not None:
            return self._lower_n_input_call(node, N_INPUT[name], n_input, call, env, select)
        dynamic = self.registry.get(name) if self.registry is not None else None
        if dynamic is None:
            raise _error(
                ErrorCode.UNKNOWN_FUNCTION,
                f"unknown function {call.display}()",
                node,
                fallback=select,
                hint=self._namespaced_function_hint(name)
                if call.namespaced
                else self._unknown_function_hint(name),
            )
        return self._lower_dynamic_call(node, name, dynamic, call, env, select)

    # -- the sqlmpeg macro namespace (plan 052) -----------------------------

    def _lower_macro_call(
        self, node: exp.Expr, name: str, call: _Call, env: _Env, select: exp.Select
    ) -> _Value:
        """Resolve ``sqlmpeg.<name>(...)`` against :data:`MACROS`, and nowhere
        else -- the registry is never consulted, so a macro compiles OFFLINE
        (``which() -> None``) exactly as well as it does against a live ffmpeg.

        A macro owns its OWN positional signature: there is no option table to
        bind against, so named arguments are rejected outright (UNSUPPORTED_SQL,
        the same shape-violation code resolve's own named-only/positional-only
        argument rules use) and arity/kind mismatches are UDF_ARG_TYPE naming
        the macro's signature -- mirroring the registry call's stream-signature
        message, but there is exactly one stream position (always index 0) to
        check, so no `_bind_options` machinery is involved.

        Broadcasting reuses :meth:`_expand_call` unchanged: it is type-driven
        off `positions`/`streams`, so a macro's single stream argument
        broadcasts elementwise exactly like any registry call's would.
        """
        macro = MACROS.get(name)
        if macro is None:
            raise _error(
                ErrorCode.UNKNOWN_FUNCTION,
                f"unknown function {call.display}()",
                node,
                fallback=select,
                hint=self._macro_function_hint(name),
            )
        if call.named:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"{call.display}() is a sqlmpeg macro: its arguments are "
                "positional only, in the documented order",
                call.named[0].value,
                fallback=node,
                hint=f"its signature is {macro.signature}",
            )
        if len(call.args) != len(macro.params):
            raise _error(
                ErrorCode.UDF_ARG_TYPE,
                f"{call.display}() takes {len(macro.params)} argument"
                f"{'' if len(macro.params) == 1 else 's'}, got {len(call.args)}",
                node,
                fallback=select,
                hint=f"its signature is {macro.signature}",
            )
        stream_pos = macro.stream_positions[0]
        stream_param = macro.params[stream_pos]
        kind = self._classify(call.args[stream_pos], env, select)
        self._reject_passthrough_args(call.display, [kind], call, call.args[stream_pos])
        if kind != stream_param.stream_type:
            hint = macro.kind_hints.get(
                kind,
                f"stream inputs come first, then options in the macro's own "
                f"order: {macro.signature}",
            )
            raise _error(
                ErrorCode.UDF_ARG_TYPE,
                f"{call.display}() takes a {stream_param.stream_type} stream as "
                f"its '{stream_param.name}' argument, got {kind}",
                call.args[stream_pos],
                fallback=node,
                hint=hint,
            )
        literals: dict[int, object] = {}
        for position, param in enumerate(macro.params):
            if param.kind != "num":
                continue
            arg = call.args[position]
            try:
                literals[position] = _number(arg)
            except SqlmpegError as exc:
                raise _error(
                    exc.code,
                    f"{call.display}()'s '{param.name}' argument must be a "
                    "numeric literal",
                    arg,
                    fallback=node,
                    hint=f"its signature is {macro.signature}",
                ) from None
        streams = {stream_pos: self._lower_expr(call.args[stream_pos], env, select)}

        def build(values: list[object]) -> FrameRef:
            return macro.expand(values, self.ctx.node)

        return self._expand_call(
            call.display,
            node,
            call.args,
            select,
            streams=streams,
            literals=literals,
            arity=len(macro.params),
            positions=[stream_pos],
            returns=macro.output,
            build=build,
        )

    def _macro_function_hint(self, name: str) -> str:
        """Did-you-mean over :data:`MACROS`, RFC-007's small-by-design trio."""
        matches = difflib.get_close_matches(name, sorted(MACROS), n=1, cutoff=0.6)
        if matches:
            return f"did you mean {MACRO_NAMESPACE}.{matches[0]}()?"
        return (
            f"{MACRO_NAMESPACE}.<name> is one of sqlmpeg's own macros -- "
            f"{', '.join(sorted(MACROS))} -- not an ffmpeg filter; filters live "
            f"bare or under {FILTER_NAMESPACE}.<filter>(...)"
        )

    # -- the ordinary case: any filter the installed ffmpeg reports --------

    def _lower_dynamic_call(
        self,
        node: exp.Expr,
        name: str,
        dynamic: DynamicFilter,
        call: _Call,
        env: _Env,
        select: exp.Select,
    ) -> _Value:
        """A call resolved from the registry: streams, then options.

        The pad signature IS the stream signature: ``gblur`` (``V->V``) takes
        exactly one video argument, ``xfade`` (``VV->V``) exactly two. Every
        positional argument after those binds to one of the filter's OPTIONS,
        in ffmpeg's own declared order (:meth:`_bind_options`).

        Reached by both spellings — a bare filter name and
        ``ffmpeg.<filter>(...)`` — which differ only in ``call.display``. The
        NODE always carries the filter's own name, so the IR, split and emit
        never learn that the namespace exists.
        """
        expected = list(dynamic.inputs)
        kinds = self._stream_kinds(call, env, select, len(expected))
        if kinds != expected:
            raise self._bad_streams(call, node, select, expected, kinds)
        args = self._bind_options(
            name,
            call,
            node,
            select,
            env,
            options=self._options_for(name, call, len(expected), node, select),
            extras=call.args[len(expected) :],
            timeline=dynamic.timeline,
        )
        streams = {
            position: self._lower_expr(arg, env, select)
            for position, arg in enumerate(call.args[: len(expected)])
        }
        output = dynamic.output

        def build(values: list[object]) -> FrameRef:
            return self.ctx.node(
                name, dict(args), [_as_ref(value) for value in values], [output]
            )

        return self._expand_call(
            call.display,
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

    # -- fixed-count N-input filters (plan 051) ----------------------------

    def _n_input_options(self, name: str) -> dict[str, FilterOption] | None:
        """`name`'s option table if it is a callable fixed-count N-input filter.

        Mirrors :meth:`_array_options` exactly: in the table, a registry to ask,
        and an ffmpeg that actually HAS the filter. The last one is why options
        are fetched even for a call that passes none — a fenced name is in no
        registry table, so its option block is the only evidence this build has
        it (see ``Registry.fenced_options``).
        """
        if name not in N_INPUT or self.registry is None:
            return None
        return self.registry.fenced_options(name)

    def _lower_n_input_call(
        self,
        node: exp.Expr,
        spec: _NInputFilter,
        options: dict[str, FilterOption],
        call: _Call,
        env: _Env,
        select: exp.Select,
    ) -> _Value:
        """One node with N input pads, N being what the count option says.

        The stream/option split cannot come from a pad signature here (there
        is none — the registry fenced the filter out for exactly that reason),
        so it comes from the arguments themselves: the LEADING RUN of
        stream-valued arguments are the input pads, and everything after them
        is an option. That is unambiguous because an option value is always a
        literal and a pad is never one.

        The count option is then read back and must AGREE with how many
        streams were supplied — `amix(a, b)` (2 streams, `inputs` defaulted to
        2) and `amix(a, b, c, inputs => 3)` are both consistent;
        `amix(a, b, c)` is not, and says so with both numbers.
        """
        kinds = [self._classify(arg, env, select) for arg in call.args]
        self._reject_passthrough_args(call.display, kinds, call, node)
        count = 0
        for kind in kinds:
            if kind not in _STREAM_KINDS:
                break
            count += 1
        supplied = kinds[:count]
        if not supplied or any(kind != spec.stream for kind in supplied):
            raise _error(
                ErrorCode.UDF_ARG_TYPE,
                f"{call.display}() is an ffmpeg filter: its stream inputs are all "
                f"{spec.stream}, got ({', '.join(supplied) or 'no streams'})",
                node,
                fallback=select,
                hint=_N_INPUT_HINT,
            )
        args = self._bind_options(
            spec.name,
            call,
            node,
            select,
            env,
            options=options,
            extras=call.args[count:],
            timeline=False,
        )
        declared = self._n_input_count(spec, args, options)
        if declared != count:
            anchor = next(
                (arg.value for arg in call.named if arg.name == spec.option), node
            )
            raise _error(
                ErrorCode.UDF_ARG_TYPE,
                f"{call.display}() was given {_stream_count(count)} but its "
                f"'{spec.option}' option says {declared}",
                anchor,
                fallback=select,
                hint=_N_INPUT_HINT,
            )
        # The count is always written onto the node, defaulted or not: ffmpeg
        # needs `inputs=N` in the filtergraph to grow the pads, and emit only
        # renders args this dict carries.
        args[spec.option] = count
        streams = {
            position: self._lower_expr(arg, env, select)
            for position, arg in enumerate(call.args[:count])
        }

        def build(values: list[object]) -> FrameRef:
            return self.ctx.node(
                spec.name, dict(args), [_as_ref(value) for value in values], [spec.output]
            )

        return self._expand_call(
            call.display,
            node,
            call.args,
            select,
            streams=streams,
            literals={},
            arity=count,
            positions=list(range(count)),
            returns=spec.output,
            build=build,
        )

    def _n_input_count(
        self,
        spec: _NInputFilter,
        args: dict[str, object],
        options: dict[str, FilterOption],
    ) -> int:
        """What the count option says, written or introspected-default or fallback.

        `args` has already been validated against the option table, so a
        written value is a number in range; only the DEFAULT needs care, since
        `FilterOption.default` is verbatim ffmpeg text that is documented as
        never re-typed (it can be a constant name, or absent entirely).
        """
        written = args.get(spec.option)
        if isinstance(written, (int, float)) and not isinstance(written, bool):
            return int(written)
        option = options.get(spec.option)
        if option is not None and option.default is not None:
            try:
                return int(float(option.default))
            except ValueError:
                pass
        return spec.fallback

    # -- array-returning filters (RFC-006, plan 047) -----------------------

    def _array_options(self, name: str) -> dict[str, FilterOption] | None:
        """`name`'s option table if it is a callable array-returning filter.

        Three questions, one answer, because they have the same shape: is the
        name in :data:`ARRAY_RETURNING`, is there a registry at all, and does
        THIS ffmpeg actually have the filter. The last one is why the
        options are fetched even for a call with no named arguments: a fenced
        name is in no registry table, so its option block is the only evidence
        this build has it (see ``Registry.fenced_options``). None means "not
        callable", and the caller falls through to the ordinary namespaced
        rejection, hint and all.
        """
        if name not in ARRAY_RETURNING or self.registry is None:
            return None
        return self.registry.fenced_options(name)

    def _lower_array_call(
        self,
        node: exp.Expr,
        spec: _ArrayFilter,
        options: dict[str, FilterOption],
        call: _Call,
        env: _Env,
        select: exp.Select,
    ) -> _Value:
        """One node with N output pads, returned as an N-element array value.

        The pad COUNT comes from the table's count rule, run over the validated
        named arguments — so the option's own type, range and constant checks
        have already happened, and a value that is well-typed but not a count
        this filter could produce (``channel_layout => 'nonsense'``) is the
        rule's own ``FILTER_OPTION_TYPE``, anchored on that argument.

        Provenance is a 1:N fan: the single input stream's source is threaded
        to every element (not ``_agreed_source``, which answers the opposite
        question), so splitting a ``language=eng`` track gives N eng channels.
        """
        expected = [spec.input]
        kinds = self._stream_kinds(call, env, select, 1)
        if kinds != expected:
            raise self._bad_streams(call, node, select, expected, kinds)
        args = self._bind_options(
            spec.name,
            call,
            node,
            select,
            env,
            options=options,
            extras=call.args[1:],
            timeline=False,
        )
        count = spec.count(args)
        if isinstance(count, _BadCount):
            raise self._bad_count(spec, count, call, node, select)

        value = self._lower_expr(call.args[0], env, select)
        if value.is_array:
            raise _error(
                ErrorCode.UDF_ARG_TYPE,
                f"{call.display}() returns an array, so it cannot also broadcast "
                f"over one: {_sql_text(call.args[0])} is "
                f"{_stream_count(len(value.streams))}",
                call.args[0],
                fallback=node,
                hint=_ARRAY_INPUT_HINT,
            )
        stream = value.streams[0]
        node_id = self.ctx.node(
            spec.name, dict(args), [stream.ref], [spec.element] * count
        )
        return _array(
            spec.element,
            [
                _Stream(ref=f"{node_id}:{pad}", type=spec.element, source=stream.source)
                for pad in range(count)
            ],
        )

    def _bad_count(
        self,
        spec: _ArrayFilter,
        bad: _BadCount,
        call: _Call,
        node: exp.Expr,
        select: exp.Select,
    ) -> SqlmpegError:
        """A count rule's rejection, anchored on the argument that caused it.

        The offending option is normally one the query wrote, and that is the
        token worth pointing at; a rule can only reject a DEFAULT if the table
        itself is wrong, so falling back to the call keeps that case anchored
        rather than unanchored.
        """
        written = next((arg for arg in call.named if arg.name == bad.option), None)
        anchor = written.value if written is not None else node
        return _error(
            ErrorCode.FILTER_OPTION_TYPE,
            f"option '{bad.option}' of filter '{spec.name}' decides how many "
            f"streams the call returns, so it must be {bad.expected}, "
            f"got {bad.value!r}",
            anchor,
            fallback=select,
            hint=bad.hint,
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

    def _stream_kinds(
        self, call: _Call, env: _Env, select: exp.Select, arity: int
    ) -> list[str]:
        """Kind labels for the call's LEADING `arity` arguments, checked for captions.

        Only the leading run is classified: everything after it is an option
        value, which is a literal that the OPTION table judges, not the
        classifier. A short call classifies what it has, so the caller's
        comparison against the pad signature reports the missing argument.
        """
        kinds = [self._classify(arg, env, select) for arg in call.args[:arity]]
        if kinds:
            self._reject_passthrough_args(call.display, kinds, call, call.args[0])
        return kinds

    def _bad_streams(
        self,
        call: _Call,
        node: exp.Expr,
        select: exp.Select,
        expected: list[StreamType],
        got: list[str],
    ) -> SqlmpegError:
        """The stream-signature rejection — UDF_ARG_TYPE's remaining job (RFC-007).

        Option problems never reach here: they are ``UNKNOWN_FILTER_OPTION`` /
        ``FILTER_OPTION_TYPE`` uniformly, positional or named.
        """
        shown = call.display
        return _error(
            ErrorCode.UDF_ARG_TYPE,
            f"{shown}() is an ffmpeg filter: it takes {', '.join(expected)} as its "
            f"stream input{'' if len(expected) == 1 else 's'}, "
            f"got ({', '.join(got) or 'nothing'})",
            node,
            fallback=select,
            hint=f"stream inputs come first, then options in the filter's own order, "
            f"then named options: {shown}({', '.join(expected)}, <option>, "
            f"<option> => <value>)",
        )

    def _options_for(
        self,
        filter_name: str,
        call: _Call,
        stream_arity: int,
        node: exp.Expr,
        select: exp.Select,
    ) -> dict[str, FilterOption]:
        """The filter's option table, fetched only when the call actually needs it.

        ``-help filter=X`` is a subprocess, and a call that passes no options
        at all (``hflip(a.frame)``) has nothing to validate — so the table stays
        unfetched, exactly as it did before positional options existed.
        """
        if len(call.args) <= stream_arity and not call.named:
            return {}
        return self._filter_options(filter_name, node, select)

    def _reject_stream_option(
        self,
        filter_name: str,
        option: FilterOption,
        arg: exp.Expr,
        node: exp.Expr,
        env: _Env,
        select: exp.Select,
    ) -> None:
        """A stream where an option value belongs, said plainly.

        Classifying first is also what keeps a TYPO in a nested call readable:
        `gblur(a.frame, nope(a.frame))` is UNKNOWN_FUNCTION for `nope`, raised
        by the classifier, rather than a puzzled complaint about `sigma`'s type.
        Only stream-SHAPED arguments are classified -- a literal is the option
        validator's business and is left to it.
        """
        inner = _unwrap(arg)
        if not isinstance(inner, exp.Bracket | exp.Column) and _call_parts(inner) is None:
            return
        kind = self._classify(arg, env, select)
        if kind not in _STREAM_KINDS:
            return
        raise _error(
            ErrorCode.FILTER_OPTION_TYPE,
            f"option '{option.name}' of filter '{filter_name}' takes a value, "
            f"got a {kind} stream",
            arg,
            fallback=node,
            hint="stream inputs come first and are counted by the filter's pad "
            "signature; everything after them is an option value",
        )

    def _bind_options(
        self,
        filter_name: str,
        call: _Call,
        node: exp.Expr,
        select: exp.Select,
        env: _Env,
        *,
        options: dict[str, FilterOption],
        extras: list[exp.Expr],
        timeline: bool,
    ) -> dict[str, object]:
        """Positional options first, then named ones — one merged arg dict.

        `extras` is every positional argument past the stream inputs. Each
        binds to the option at its own index in ``options``, whose insertion
        order IS ffmpeg's AVOption declaration order and therefore its own
        positional binding order (verified against ffmpeg 7.1 for the whole
        registry; see ``sqlmpeg/registry.py``). Having landed on an option, a
        positional is validated AS that option by the very same
        :func:`_option_value` a named argument goes through, which is what
        makes option errors uniform across the two spellings.

        Named arguments are then checked with the positionally-bound names
        marked `occupied`, so a named argument never silently overrides one the
        call already set.
        """
        order = list(options)
        if len(extras) > len(order):
            raise _error(
                ErrorCode.UDF_ARG_TYPE,
                f"{call.display}() got {len(extras)} positional option"
                f"{'' if len(extras) == 1 else 's'}, but the '{filter_name}' filter "
                f"has {len(order)}",
                extras[len(order)] if len(order) < len(extras) else node,
                fallback=select,
                hint="its options, in the order they bind: " + _listed(order)
                if order
                else f"the '{filter_name}' filter has no options sqlmpeg can set",
            )
        bound: dict[str, object] = {}
        for index, arg in enumerate(extras):
            option = options[order[index]]
            self._reject_stream_option(filter_name, option, arg, node, env, select)
            bound[option.name] = _option_value(
                filter_name, option, _NamedArg(name=option.name, value=arg), node
            )
        bound.update(
            self._check_named_args(
                filter_name,
                options,
                call.named,
                node,
                owner=call.display,
                occupied=set(bound),
                timeline=timeline,
            )
        )
        return bound

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
        arguments are (always the LEADING positions, from the pad signature or
        from an N-input call's own count) and `build` is what turns one
        element's argument values into a subgraph.
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

        One rule: options ARE the installed ffmpeg. Without a registry there is
        nothing to validate them against, and guessing is exactly what this
        compiler does not do. (A CALL cannot reach this with a None registry —
        its name would already be UNKNOWN_FUNCTION — but a generated source in
        FROM position can, so the branch stays.)

        ``Registry.options`` returns None only for a filter this ffmpeg does not
        have (or that the v1 scope fence excluded); an empty dict is a real
        answer (a filter with no options) and is passed through as one.
        """
        if self.registry is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "options are validated against your installed ffmpeg; "
                "ffmpeg was not found",
                anchor,
                fallback=fallback,
                hint=_NO_REGISTRY_HINT,
            )
        options = self.registry.options(filter_name)
        if options is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"options are validated against the ffmpeg filter "
                f"'{filter_name}', which your ffmpeg does not provide",
                anchor,
                fallback=fallback,
                hint="drop the options, or install an ffmpeg that has "
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
        timeline: bool = False,
    ) -> dict[str, object]:
        """Validate every named argument against `options`, in written order.

        `occupied` holds the option names this call already bound
        POSITIONALLY, so ``crop(f, 100, 50, 10, 20, out_w => 5)`` reads as the
        conflict it is rather than silently overriding what the call itself
        said. A collision is ``FILTER_OPTION_TYPE`` — an option problem, like
        every other one under RFC-007 — and the fix is to drop one of the two
        spellings.

        The collision check comes FIRST so the message names the conflict
        rather than whatever the registry would say about the name.

        `timeline` is the target's ``DynamicFilter.timeline`` flag, and it is a
        PARAMETER because this method cannot look filters up: every caller
        already holds the registry entry (or, for a generated source, knows
        there is no such field to hold — a source is never timeline-capable, so
        the default rejects). It admits ``enable`` BEFORE `options` is consulted
        (RFC-005 SS2): ffmpeg implements ``enable`` in the filter framework, so
        it is in no filter's option table and a registry lookup would always
        call it unknown.
        """
        checked: dict[str, object] = {}
        for arg in named:
            if arg.name in occupied:
                raise _error(
                    ErrorCode.FILTER_OPTION_TYPE,
                    f"option '{arg.name}' of filter '{filter_name}' is already set "
                    f"positionally by {owner}()",
                    arg.value,
                    fallback=call,
                    hint="a named argument never overrides what the call itself "
                    "set; drop one of the two spellings",
                )
            if arg.name == _ENABLE:
                checked[_ENABLE] = _enable_value(filter_name, arg, call, timeline)
                continue
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
        """Did-you-mean over the registry (RFC-007: there is nothing else)."""
        registry = self.registry
        if registry is not None and registry.available():
            if registry.get_source(name) is not None:
                return (
                    f"{name} is a generated source, not a function: put it in FROM, "
                    f"e.g. FROM {FILTER_NAMESPACE}.{name}(duration => 2) s"
                )
            # `name` itself can be in the candidate set -- N_INPUT lists three
            # names unconditionally, and this ffmpeg may simply not have one --
            # and "did you mean amix()?" for `amix()` helps nobody.
            candidates = sorted((set(registry.names()) | set(N_INPUT)) - {name})
            matches = difflib.get_close_matches(name, candidates, n=1, cutoff=0.6)
            if matches:
                return f"did you mean {matches[0]}()?"
            return (
                "every function is a filter of your installed ffmpeg, and this is "
                "not one of them; filters with a variable pad count, more than one "
                "output, or no input at all are not callable"
            )
        return _NO_REGISTRY_HINT

    def _namespaced_function_hint(self, name: str) -> str:
        """Did-you-mean for ``ffmpeg.<filter>()``, keeping the namespace spelling.

        Suggestions keep the ``ffmpeg.`` prefix, which is the one spelling that
        works for every filter name whatever Postgres thinks of it.
        """
        registry = self.registry
        if registry is not None and registry.available():
            if registry.get_source(name) is not None:
                # A generated source IS usable -- in FROM, where it belongs
                # (RFC-005 §1). Say where rather than "unknown".
                return (
                    f"{FILTER_NAMESPACE}.{name} is a generated source, not a "
                    f"function: put it in FROM, e.g. FROM {FILTER_NAMESPACE}."
                    f"{name}(duration => 2) s"
                )
            candidates = sorted(
                (set(registry.names()) | set(ARRAY_RETURNING) | set(N_INPUT)) - {name}
            )
            matches = difflib.get_close_matches(name, candidates, n=1, cutoff=0.6)
            if matches:
                return f"did you mean {FILTER_NAMESPACE}.{matches[0]}()?"
            return (
                f"{FILTER_NAMESPACE}.<filter> is a filter of your installed ffmpeg, "
                "and this is not one of them; filters with a variable pad count, "
                "more than one output, or no input at all are not callable"
            )
        return (
            f"the {FILTER_NAMESPACE}.<filter> namespace is your installed ffmpeg's "
            "filter set; ffmpeg was not found on PATH"
        )

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
        """Kind label for one call argument: a stream type, ``num``/``str``, or
        :data:`_UNSUPPORTED_KIND`.

        Stream arguments resolve to ``video``/``audio`` without creating any
        node, so a mismatch is reported before the graph grows. Nested calls
        to unknown functions are reported here rather than being labelled a
        stream and swallowed by an outer arity error, and a nested call
        resolves exactly the way a top-level one does, so
        ``scale(gblur(a.frame, 2), 640, 480)`` sees the inner call's output
        pad type.
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
            return _UNSUPPORTED_KIND
        call = _call_parts(node)
        if call is not None:
            name = call.name.lower()
            if call.is_macro:
                macro = MACROS.get(name)
                if macro is None:
                    raise _error(
                        ErrorCode.UNKNOWN_FUNCTION,
                        f"unknown function {call.display}()",
                        node,
                        fallback=select,
                        hint=self._macro_function_hint(name),
                    )
                return macro.output
            # An array-returning call is classified by its ELEMENT type, which
            # is what makes it a legal argument: `volume(ffmpeg.channelsplit(
            # a.audio[1]), 0.5)` broadcasts over the channels.
            if call.namespaced and self._array_options(name) is not None:
                return ARRAY_RETURNING[name].element
            if self._n_input_options(name) is not None:
                return N_INPUT[name].output
            dynamic = self.registry.get(name) if self.registry is not None else None
            if dynamic is None:
                raise _error(
                    ErrorCode.UNKNOWN_FUNCTION,
                    f"unknown function {call.display}()",
                    node,
                    fallback=select,
                    hint=self._namespaced_function_hint(name)
                    if call.namespaced
                    else self._unknown_function_hint(name),
                )
            return dynamic.output
        return _UNSUPPORTED_KIND

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


def _outputs(columns: list[_Column]) -> list[Output]:
    """One :class:`~sqlmpeg.ir.Output` per stream a SELECT list carries.

    The SELECT list IS the output stream list, and an array column is several
    streams, so it splats into consecutive Outputs. Every element of an
    aliased array column keeps that alias VERBATIM (no ordinal suffix): the
    alias names the column, not the stream, and ffmpeg metadata naming is
    plan 022's business.
    """
    return [
        Output(
            ref=stream.ref,
            type=stream.type,
            name=column.name,
            metadata=_provenance(stream),
        )
        for column in columns
        for stream in column.value.streams
    ]


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

    Deliberately separate from :func:`_number`: that raises its own message,
    and an option's expected type is only known after the registry has been
    consulted, so reading the value and judging it are two steps here.
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
    inner = _unwrap(node)
    if isinstance(inner, exp.Literal):
        # A literal `_literal_value` could not read (sqlglot tokenizes `1e` as a
        # number but `to_py()` raises): echo what was written, not its shape.
        return repr(str(inner.this))
    return _describe(inner)


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


def _enable_value(
    filter_name: str, arg: _NamedArg, call: exp.Expr, timeline: bool
) -> str:
    """The timeline ``enable`` expression, or the rejection for this filter.

    Two ways it fails. The filter has no timeline support at all, which is a
    property of the FILTER and so reads as an unknown option on it — flavoured
    with the reason, because "gblur has no option 'enable'" would be a lie
    about gblur. Or the value is not a string: an ffmpeg timeline expression is
    text, and a bare number would silently mean "always on"/"never on" rather
    than the window the writer had in mind.

    The expression's CONTENT is deliberately unchecked (RFC-005 non-goals): the
    variable vocabulary is per-filter and is not introspectable, so it is
    ffmpeg's to validate at run time.
    """
    if not timeline:
        raise _error(
            ErrorCode.UNKNOWN_FILTER_OPTION,
            f"filter '{filter_name}' has no option 'enable': your ffmpeg does "
            f"not flag '{filter_name}' as supporting timeline editing",
            arg.value,
            fallback=call,
            hint=_NO_TIMELINE_HINT,
        )
    value = _literal_value(arg.value)
    if not isinstance(value, str):
        raise _error(
            ErrorCode.FILTER_OPTION_TYPE,
            f"option 'enable' of filter '{filter_name}' expects an ffmpeg "
            f"timeline expression, got {_option_got(arg.value, value)}",
            arg.value,
            fallback=call,
            hint=_ENABLE_HINT,
        )
    return value


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


def _sql_text(node: exp.Expr) -> str:
    """The argument as the user wrote it, for a BROADCAST_MISMATCH message.

    ``dialect="postgres"`` matters: it re-adds the ``INDEX_OFFSET`` sqlglot
    subtracted at parse time, so ``a.audio[2]`` renders as ``a.audio[2]``.
    """
    return str(node.sql(dialect="postgres"))


def _stream_count(count: int) -> str:
    return f"{count} stream" + ("" if count == 1 else "s")


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------


def lower(
    res: Resolved,
    probes: dict[str, ProbeResult | None],
    *,
    registry: Registry | None = None,
) -> Graph:
    """Lower a resolved query into an IR graph.

    `probes` is keyed by input ALIAS (``compiler.compile_sql`` builds it, one
    ``probe()`` per distinct path); a missing or ``None`` entry means that
    input could not be read, and lowering stays symbolic for it.

    `registry` IS the function surface (RFC-007): the filter set of the ffmpeg
    on PATH, introspected lazily. It is a PARAMETER rather than a module lookup
    so that a caller — ``compile_sql``, or a test — decides which ffmpeg (or
    which captured snapshot) this compile resolves against. None, or an empty
    one, means every call name is UNKNOWN_FUNCTION.

    Raises ``SqlmpegError`` — and nothing else — on every rejection.
    """
    try:
        return _Lowerer(res, probes, registry).run()
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
