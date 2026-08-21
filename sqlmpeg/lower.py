"""Lower pass: a resolved query becomes an IR :class:`~sqlmpeg.ir.Graph`.

This is pass 2 of the compiler (see "Architecture" in sqlmpeg-project.md). It
assumes :func:`sqlmpeg.parser.resolve` already accepted the query, so every
rejection raised here is either a check resolve deliberately left to lowering
(CTE column names, function names, argument types, probed stream bounds) or a
defensive re-check.

The top-level SELECT list IS the output stream list, and every value flowing
through lowering is a *typed* stream (``video``, ``audio``, ``subtitle`` or
``data``), never an untyped "frame".

Passthrough-only stream types
-----------------------------
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

``SELECT *`` and ``<alias>.*``
------------------------------
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
  VIEWS are CTEs here: ``Resolved.ctes`` holds both, so the whole
  binding table is lowered exactly ONCE no matter how many COPYs read it.
* Then one :class:`~sqlmpeg.ir.SinkUnit` per ``COPY``, in script order, each
  from that COPY's own query — or, for a bare SELECT, a single unit
  with ``path=None``. Every unit shares this graph's nodes, so a view read by
  three COPYs is decoded and filtered once and fanned out by the split pass.
* Inside a branch, ``FROM`` builds a typed environment: an ``input()`` alias
  exposes per-type stream access (``a.video[1]`` -> ``"src:a:v:0"``; SQL
  subscripts are 1-based, IR indices 0-based), a CTE alias exposes its
  recorded columns (under its own name, or under a branch-local alias:
  ``FROM master m``), and a ``ffmpeg.<source>(...)`` alias exposes exactly one
  statically-typed stream (see below).
* ``WHERE <alias>.t BETWEEN x AND y`` records a per-alias time range; where
  that window lands depends on what the alias is:

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
  - under a fan-out ``TO (<expression>)`` an input alias's window is per-FILE
    rather than per-``-i``: the rows name different windows over one input, so
    each lands on its own ``SinkUnit.window`` and emit seeks that OUTPUT. The
    exception is a fan-out that stream-copies everything it maps, where an
    output seek would write a corrupt file: that one goes back to one graph
    (one command) per file, each with its own ``Graph.input_trims``.
* Each projection lowers bottom-up to one :class:`~sqlmpeg.ir.Output` per
  stream it carries (an array column splats into consecutive Outputs). A call
  type-checks its stream arguments against the filter's pad signature and its
  option arguments against that filter's introspected AVOptions (see "One
  calling convention" below).

Generated sources: ``FROM ffmpeg.<source>(...) a``
--------------------------------------------------
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
  column rule is answered without a probe: ``a.video[1]`` on a
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

One calling convention
----------------------
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
  ``sqlmpeg/registry.py``'s docstring for why the deduped list is that order).
  ``crop(f, 100, 50, 10, 20)``
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
  ``T`` flag alone.
* ``ffmpeg.<filter>(...)`` is the same call under a name no SQL grammar can
  claim: identical semantics, but it bypasses Postgres's special
  forms, so ``ffmpeg.overlay(base, top, x => 20, eof_action => 'pass')``
  reaches the option set the ``OVERLAY..PLACING`` grammar hides, and
  ``ffmpeg.trim(...)`` / ``ffmpeg.format(...)`` arrive with their arguments
  intact. It is REQUIRED for the census's eleven collided names and optional
  everywhere else. The node it builds carries the FILTER's name, so nothing
  downstream knows the namespace exists.
* Three ``->N`` filters are callable through that namespace despite the pad
  scope check, because their output COUNT is fixed by an option: ``channelsplit``,
  ``acrossover`` and ``extractplanes`` (:data:`ARRAY_RETURNING`). Each lowers
  to ONE node with N output pads and RETURNS an array, so its result splats
  into a SELECT list, subscripts out of a CTE column and broadcasts
  elementwise like any other array. The table is
  consulted before the registry's verdict, since the registry has no entry to
  give; every other excluded name keeps its ``UNKNOWN_FUNCTION``.
* Several ``N->1`` filters are re-admitted the mirror way (:data:`N_INPUT`):
  ``amix``, ``hstack``, ``vstack``, ``amerge``, ``join``, ``interleave`` and
  ``ainterleave`` take a variable number of INPUT pads fixed by one option
  (``inputs`` for most, ``nb_inputs`` for interleave/ainterleave), so the pad
  scope check excludes them too, yet the count is statically knowable the moment
  that option is read. Their leading stream arguments ARE the input pads and
  the count option must agree with how many were supplied (``UDF_ARG_TYPE``
  naming both numbers when it does not). Unlike the array trio these are
  reachable BARE as well as namespaced — no Postgres grammar claims their
  names. ``ladspa`` (``N->A``) joins the same table but with no count option
  at all — its pad count is whatever the loaded LADSPA plugin's own ports
  say, so the streams supplied ARE the count, nothing to cross-check and
  nothing to write back.
* ``sqlmpeg.<name>(...)`` is a THIRD namespace, resolved against
  :data:`sqlmpeg.macros.MACROS` and NEVER the registry -- macros work offline,
  with no ffmpeg on PATH at all. A macro owns its own fixed
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

Broadcasting makes a bare ``a.video`` / ``a.audio`` the WHOLE array of that
input's streams, in probe order. Splatted into a SELECT list it becomes
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
* Postgres has a builtin ``OVERLAY(x PLACING y FROM n FOR m)``, so
  ``overlay(a, b, x, y)`` parses to :class:`sqlglot.exp.Overlay` with *named*
  args (``this``, ``expression``, ``from_``, ``for_``) rather than to
  ``exp.Anonymous``; :func:`_call_parts` normalizes it back to four
  positionals. A ``=>`` inside that grammar is a PARSE_ERROR before lowering
  sees the call, so a BARE ``overlay`` can take its options positionally but
  never by name. Eleven registry names collide with a Postgres special form
  this way (census in docs/dynamic-filters.md); ``ffmpeg.<filter>(...)``
  reaches every one of them, because the special-form grammars key on a BARE
  name and a qualified call parses as ``Dot(Identifier(ffmpeg),
  Anonymous(...))`` whatever the filter is called.
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
* A COPY option value (``WITH (crf 20)``) is NOT always a ``Literal``: ``true``
  / ``false`` arrive as ``exp.Boolean``, a bare word as ``exp.Var``, a
  double-quoted word as ``exp.Identifier``, ``NULL`` as ``exp.Null``.
  :func:`_sink_value` normalizes the first three shapes to python values and
  hands everything else to the option table as an unrepresentable value, so
  the SINK_OPTION_TYPE message and hint still come from the table.
"""

from __future__ import annotations

import base64
import difflib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal

from sqlglot import exp

from sqlmpeg import binaries, loudnorm
from sqlmpeg.errors import ErrorCode, SqlmpegError
from sqlmpeg.inputs import validate_option as validate_input_option
from sqlmpeg.ir import (
    NO_CHAPTERS,
    FrameRef,
    Graph,
    Node,
    Output,
    SinkUnit,
    StreamType,
    dedup_inputs,
    is_src,
    src_parts,
)
from sqlmpeg.macros import INPUT_MACROS, MACROS, InputMacro, Macro, macro_names
from sqlmpeg.parser import (
    _ARITHMETIC,
    _ARITHMETIC_NAMES,
    _REMOVED_FRAME,
    FILTER_NAMESPACE,
    MACRO_NAMESPACE,
    MAP_COLUMNS,
    ROW_STREAM,
    RawRowJoin,
    RawSink,
    RawSinkOption,
    RawSource,
    RawTrackRows,
    RawValuesTable,
    Resolved,
    _pos,
    _projection_expr,
    _time_bounds,
    chapters_unnest_hint,
    column_label,
    flag_error,
    from_entries,
    group_keys,
    is_grouped,
    is_value_expr,
    kwarg_name,
    map_example,
    map_noun,
    map_path,
    map_ref,
    record_cast_type,
    references_row_alias,
    star_qualifier,
    subscript_index,
    subscript_metadata_shape,
    tag_key,
    tag_path,
    union_branches,
)
from sqlmpeg.parser import _ident_name as _fold
from sqlmpeg.probe import ProbeResult, StreamMeta
from sqlmpeg.registry import DynamicFilter, FilterOption, Registry, SourceFilter
from sqlmpeg.sink import (
    CODEC_PARAMS_FLAGS,
    TWO_PASS_CODECS,
    copy_suppressed_scopes,
    validate_csv_option,
)
from sqlmpeg.sink import validate_option as validate_sink_option
from sqlmpeg.table import (
    ArrayCell,
    CellValue,
    RecordCell,
    StreamCell,
    TableResult,
    TableSink,
)
from sqlmpeg.types import (
    CHAPTER_TYPE,
    CHAPTERS_COLUMN,
    CONTAINER_READONLY_FIELDS,
    DISPOSITION_COLUMN,
    DISPOSITION_KEYS,
    INPUT_DURATION_COLUMN,
    RECORD_FIELDS,
    ROW_READONLY_FIELDS,
    ROW_SCHEMAS,
    ROW_STAR_COLUMNS,
    STAR_COLUMNS,
    STREAM_ARRAY_COLUMNS,
    STREAM_TAG_COLUMNS,
    TAGS_COLUMN,
    TIME_COLUMN,
    RowColumnType,
)

__all__ = ["lower", "lower_table"]

# The array-typed pseudo-columns an input exposes, and their element type.
# subtitle/data have the identical array/subscript/splat surface but are
# passthrough-only (see `_PASSTHROUGH_ONLY` below).
_ARRAY_COLUMNS: dict[str, StreamType] = {
    "video": "video",
    "audio": "audio",
    "subtitle": "subtitle",
    "data": "data",
}

# The container array columns a MEDIA query's `SELECT *` expands: the stream
# ones, in declaration order. `chapters` is an array column too, but a chapter
# is not a stream, so it takes no output position.
_STREAM_STAR_COLUMNS: tuple[str, ...] = tuple(
    name for name in STAR_COLUMNS if name in STREAM_ARRAY_COLUMNS
)

_TYPE_MARKERS: dict[StreamType, str] = {
    "video": "v",
    "audio": "a",
    "subtitle": "s",
    "data": "d",
}

# Stream types an ffmpeg filtergraph cannot carry: they may only become an
# Output (a bare `-map`), never a filter argument and never a WHERE trim's
# input.
_PASSTHROUGH_ONLY: frozenset[StreamType] = frozenset({"subtitle", "data"})

# Kind label used in UDF_ARG_TYPE "got" lists for anything that is neither a
# literal nor a stream-typed subexpression (e.g. `1 + 2`, NULL, TRUE). The
# angle brackets keep it from ever colliding with a StreamType name.
_UNSUPPORTED_KIND = "<expr>"

# Kind labels :meth:`_Lowerer._classify` gives a stream-valued argument -- the
# only kinds that may occupy a call's leading (stream input) positions.
_STREAM_KINDS: frozenset[str] = frozenset({"video", "audio", "subtitle", "data"})

# What an mp4 muxer stamps on an untagged stream: no information, so it is
# never copied onto a passthrough Output.
_UNDEFINED_LANGUAGE = "und"

_TIME_HINT = (
    "<alias>.t is only usable as WHERE <alias>.t BETWEEN <start> AND <end>, "
    "<alias>.t >= <start>, or <alias>.t <= <end>"
)
_STREAM_HINT = (
    "a SELECT column must be a stream, e.g. a.video[1] or scale(a.video[1], 640, -2)"
)
_SUBSCRIPT_HINT = "stream subscripts are 1-based: a.video[1] is the first video stream"
_ZIP_HINT = (
    "broadcast arrays zip elementwise, one output per element; "
    "subscript one of them to pair a single stream with the other, e.g. a.audio[1]"
)
_NO_REGISTRY_HINT = (
    f"sqlmpeg's function surface IS your installed ffmpeg's filter set; {binaries.INSTALL_HINT}"
)
_PASSTHROUGH_HINT = (
    "subtitle and data streams can only be selected (and copied), never filtered; "
    "drop them from the call and select them as their own column"
)
_SOURCE_DURATION_HINT = (
    "a generated source has no timeline to seek into; give it a length with "
    "its own option instead, e.g. ffmpeg.anullsrc(duration => 30) s"
)
_ROW_METADATA_HINT = (
    "a track row's metadata columns are what you FILTER, JOIN and SORT rows by; "
    "the only column that is a stream — and therefore the only one that can be "
    "an output — is the row itself, <alias>. Give the column an alias to write "
    "it back as a TAG instead, e.g. SELECT t, t.tags.language AS language"
)
_ARRAY_AGG_HINT = (
    "array_agg takes one track-row stream expression, e.g. array_agg(t) "
    "over FROM input('f.mkv') f, unnest(f.audio) t"
)
_ONE_FILE_PER_ROW_HINT = (
    "gather the rows into that one file with array_agg(...), adding GROUP BY "
    "the column they share when they share one; or give each row a file of its "
    "own with a TO expression, e.g. TO (t.tags.language || '.mka')"
)
_ONE_FILE_PER_GROUP_HINT = (
    "one group is one file, so the destination has to name the group, e.g. "
    "TO (t.tags.language || '.mka'); group by a column every row agrees on to write "
    "a single file instead"
)
_GROUPED_CTE_HINT = (
    "a CTE with several rows varies inside the group: wrap the column in "
    "array_agg(...), or add it to the GROUP BY to make it the group's key"
)
_CHAPTER_ROW_HINT = (
    "a chapter row has no stream column at all — a chapter is not a track — "
    "so it can only be read as a metadata query, e.g. no COPY, or COPY ... "
    "WITH (FORMAT csv)"
)
_CHAPTER_LITERAL = f"ROW(title, start_t, end_t)::{CHAPTER_TYPE}"
_CHAPTER_EXAMPLE = f"ROW('Intro', 0, 60)::{CHAPTER_TYPE}"
_CHAPTERS_COLUMN_HINT = (
    f"a {CHAPTERS_COLUMN} column is an array of chapter records, e.g. "
    f"ARRAY[{_CHAPTER_EXAMPLE}] AS {CHAPTERS_COLUMN}, or "
    f"array_agg(ROW(c.title, c.start_t, c.end_t)::{CHAPTER_TYPE}) AS "
    f"{CHAPTERS_COLUMN} over rows"
)
_WRITTEN_ROW_HINT = (
    "a written row carries values, never a stream: filter, group and aggregate "
    "by its columns, e.g. array_agg(ROW(m.title, m.start_t, m.end_t)::chapter) "
    "AS chapters"
)
_CAPTION_TRIM_HINT = (
    "trim the video/audio without selecting the subtitle/data columns, or select "
    "them in a query without a WHERE time range; to caption a trimmed clip, join "
    "an external subtitle file whose cues are timed for the cut"
)

# `enable` is FRAMEWORK-level: ffmpeg implements it in the filter framework,
# not in any filter, so it never appears in a filter's `-help` AVOptions and no
# options table can contain it. Which filters honour it is the `T` column of
# `ffmpeg -filters`, captured as DynamicFilter.timeline; that flag admits the
# name here.
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


# array-RETURNING filters.
#
# Three ffmpeg filters take ONE input pad and produce a number of output pads
# fixed statically by one of their options. Their `-filters` spec is `A->N` /
# `V->N`, so the pad scope check excludes all three and `Registry.get` says None.
# This table re-admits exactly those three. It lives here, not in the registry,
# because the count rule is a property of the OPTION SEMANTICS, which nothing
# ffmpeg prints exposes: the registry keeps saying `A->N`, lowering keeps the
# arithmetic.
#
# Re-admitted through the `ffmpeg.<filter>(...)` namespace ONLY. A bare
# `channelsplit(...)` stays UNKNOWN_FUNCTION like every other excluded name.
#
# The result is an ARRAY value: `Node(outputs=[element]*N)` plus one `_Stream`
# per pad, `is_array=True` even when N == 1 (a one-element array still splats,
# subscripts through a CTE column, and broadcasts). Its pads are ordinary
# consume-once pads, so a pad read by two sinks gets an `asplit` like any other.


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


# fixed-count N-INPUT filters, the mirror image of ARRAY_RETURNING.
#
# Several ffmpeg filters take a number of INPUT pads fixed statically by one
# of their options and produce exactly one output pad. Their `-filters` spec
# is `N->A` / `N->V`, so the pad scope check excludes them all -- yet the count is
# knowable the moment the option is read, the same argument that re-admits the
# array-returning trio.
#
# Re-admitted under BOTH spellings, bare and namespaced: none of these names
# collides with a Postgres special form.
#
# The mechanism generalizes to any `N->1` filter whose input count is one
# option (`concat`'s `n`, `mix`, ...), but stays scoped to the entries below
# until array-CONSUMING calls exist.


@dataclass(frozen=True)
class _NInputFilter:
    """One fixed-count N-input filter: its pads, and the option fixing the count."""

    name: str
    stream: StreamType  # what every one of its INPUT pads carries
    output: StreamType  # its single output pad
    option: str | None  # the option whose value IS the input-pad count; None
    # when there is no such option (ladspa: the plugin's own ports decide) --
    # then the supplied stream count is never checked against anything and
    # never written back.
    fallback: int  # count when the option is neither written nor introspectable
    # Write the count onto the node even when it equals the fallback. True for
    # the filters that are N-input on EVERY ffmpeg (amix: pins carry
    # `inputs=2`); False for ones that grew the option in a later ffmpeg
    # (acrossfade, N->A since ffmpeg 9) -- omitting the defaulted count keeps
    # the compiled command valid on builds whose acrossfade has no such
    # option at all.
    emit_default: bool = True


N_INPUT: dict[str, _NInputFilter] = {
    "amix": _NInputFilter(name="amix", stream="audio", output="audio", option="inputs", fallback=2),
    "hstack": _NInputFilter(
        name="hstack", stream="video", output="video", option="inputs", fallback=2
    ),
    "vstack": _NInputFilter(
        name="vstack", stream="video", output="video", option="inputs", fallback=2
    ),
    "acrossfade": _NInputFilter(
        name="acrossfade",
        stream="audio",
        output="audio",
        option="inputs",
        fallback=2,
        emit_default=False,
    ),
    "amerge": _NInputFilter(
        name="amerge", stream="audio", output="audio", option="inputs", fallback=2
    ),
    "join": _NInputFilter(
        name="join", stream="audio", output="audio", option="inputs", fallback=2
    ),
    # interleave/ainterleave's count option is `nb_inputs`, not the shorter
    # `n` alias: VERIFIED via `Registry.excluded_options` (ffmpeg 9.0.1) -- `n`
    # is `nb_inputs`'s adjacent alias, and the dedup rule keeps the longer
    # name (see registry.py's docstring).
    "interleave": _NInputFilter(
        name="interleave", stream="video", output="video", option="nb_inputs", fallback=2
    ),
    "ainterleave": _NInputFilter(
        name="ainterleave", stream="audio", output="audio", option="nb_inputs", fallback=2
    ),
    "ladspa": _NInputFilter(
        name="ladspa", stream="audio", output="audio", option=None, fallback=0, emit_default=False
    ),
}

_N_INPUT_HINT = (
    "the number of streams you pass IS the filter's input count; either pass "
    "that many streams, or set the count explicitly, e.g. amix(a, b, c, inputs => 3)"
)


# errors


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


def _computed_segments(expression: exp.Expr, row_aliases: set[str]) -> list[exp.Expr]:
    """The pieces of a path expression whose text comes from row metadata.

    A ``||`` chain is split at its operands, so the literal directory in
    ``'out/' || t.tags.language`` stays a literal and only ``t.tags.language`` is
    checked. Anything else is one segment, computed if it reads a row at all.
    """
    node = _unwrap(expression)
    if isinstance(node, exp.DPipe):
        expression_node = node.args.get("expression")
        sides = [node.this, expression_node if isinstance(expression_node, exp.Expr) else None]
        return [
            segment
            for side in sides
            if isinstance(side, exp.Expr)
            for segment in _computed_segments(side, row_aliases)
        ]
    return [node] if references_row_alias(node, row_aliases) else []


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
    if isinstance(node, exp.Case):
        return "a CASE expression"
    if isinstance(node, exp.DPipe):
        return "a '||' expression"
    return f"a {node.__class__.__name__.upper()} expression"


# small AST helpers


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


def _table_column_name(node: exp.Expr) -> str:
    """A table/csv column's header: the ``AS`` alias, else its natural name.

    The SELECT alias when given, else the column expression's natural name
    (``language``, ``codec``, ...). A bare row/input column names itself, and
    a bare row alias names the alias, as Postgres does for a whole-row
    reference; a subscript metadata accessor names the metadata field it
    reads (``f.audio[1].codec`` -> ``codec``, matching a row table's
    own column of the same name); anything else (a filter call, COALESCE,
    ...) has no single name to fall back to. A tag path names its KEY
    (``a.tags.language`` -> ``language``): the last part of the path, the way
    Postgres names any field reference.
    """
    alias = _projection_name(node)
    if alias is not None:
        return alias
    inner = _unwrap(node)
    if isinstance(inner, exp.Column):
        name = _fold(inner.this)
        if name == ROW_STREAM:
            return _fold(inner.args.get("table"))
        return _map_key(name)
    if isinstance(inner, exp.ArrayAgg):
        return "array_agg"  # Postgres's own convention for the unaliased column
    shape = subscript_metadata_shape(inner)
    if shape is not None:
        return _map_key(shape[1])
    return "column"


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
    """One ``name => value`` call argument.

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

    `namespaced` marks the ``ffmpeg.<filter>(...)`` spelling, which
    resolves in the registry under a name no Postgres grammar can claim.
    `is_macro` marks the ``sqlmpeg.<name>(...)`` spelling, which
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

    Mirrors :func:`_namespaced_call` exactly, and VERIFIED to parse
    to the identical shape under sqlglot 30.17 ``read="postgres"`` for all
    three macro names: ``exp.Dot(this=Identifier(sqlmpeg),
    expression=exp.Anonymous(this=<macro>, expressions=[...]))``, symmetric
    with the ffmpeg namespace's.
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


# literal coercion


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


def _bare_name(node: exp.Expr) -> str | None:
    """`node` as a bare, unqualified identifier name, else None.

    What ``metadata_from <alias>`` takes: an ``exp.Var`` (VERIFIED under
    sqlglot 30.17 -- a sink option's bare-word value always parses as one),
    never a quoted string or a qualified name.
    """
    if isinstance(node, exp.Var) and not node.args.get("table"):
        return _fold(node)
    return None


# Characters ffmetadata's own escaping would need (`\`, `=`, `;`, `#`, a
# newline) -- rejected outright rather than silently writing a file ffmpeg
# cannot parse back.
_UNSAFE_CHAPTER_TITLE = frozenset("\\=;#\n\r")


def _record_args(node: exp.Expr) -> list[exp.Expr] | None:
    """The values a ``ROW(...)`` record constructor lists, else None.

    ``ROW(a, b, c)`` parses as a plain call and the bare ``(a, b, c)`` form as
    a tuple; both are the same constructor, so both are read here.
    """
    inner = _unwrap(node.this) if isinstance(node.this, exp.Expr) else None
    if isinstance(inner, exp.Tuple):
        return [item for item in inner.expressions if isinstance(item, exp.Expr)]
    if isinstance(inner, exp.Anonymous) and str(inner.this).lower() == "row":
        return [item for item in inner.expressions if isinstance(item, exp.Expr)]
    return None


@dataclass(frozen=True)
class _Chapter:
    """One written chapter: its span, its title, and where they were written.

    `start_node` / `end_node` are the expressions the bounds came from, so a
    span rejection anchors on the number the query typed.
    """

    start: int | float
    end: int | float
    title: str | None
    start_node: exp.Expr
    end_node: exp.Expr


def _chapters_ffmetadata(chapters: Sequence[_Chapter]) -> str:
    """One evaluated ``chapter[]`` as an ffmetadata document's text.

    ``;FFMETADATA1`` plus one ``[CHAPTER]`` block per record, in written order,
    ``TIMEBASE=1/1`` so ``START``/``END`` are plain seconds -- exactly what
    the cookbook's pinned recipe expects, byte for byte. A number written as
    an integer renders as one. `title` is nullable, and a NULL one omits the
    line entirely.
    """
    lines = [";FFMETADATA1"]
    previous: tuple[int | float, int | float] | None = None
    for position, chapter in enumerate(chapters, start=1):
        _check_chapter_span(
            CHAPTERS_COLUMN,
            position,
            chapter.start,
            chapter.end,
            previous,
            chapter.start_node,
            chapter.end_node,
        )
        previous = (chapter.start, chapter.end)
        lines.append("[CHAPTER]")
        lines.append("TIMEBASE=1/1")
        lines.append(f"START={chapter.start}")
        lines.append(f"END={chapter.end}")
        if chapter.title is not None:
            lines.append(f"title={chapter.title}")
    return "\n".join(lines) + "\n"


def _check_chapter_span(
    alias: str,
    position: int,
    start: int | float,
    end: int | float,
    previous: tuple[int | float, int | float] | None,
    start_cell: exp.Expr,
    end_cell: exp.Expr,
) -> None:
    """One written chapter against the three rules a chapter list obeys.

    A chapter runs forward, the list runs forward, and two chapters never cover
    the same second: a player reads them in written order and has no way to
    show a span that goes backwards or sits inside its neighbour.
    """
    if start >= end:
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"'{alias}' chapter {position} ends at {end}, which is not after "
            f"its start {start}",
            end_cell,
            hint="a chapter runs from start_t to end_t: end_t must be larger",
        )
    if previous is None:
        return
    previous_start, previous_end = previous
    if start < previous_start:
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"'{alias}' chapter {position} starts at {start}, before chapter "
            f"{position - 1} at {previous_start}",
            start_cell,
            hint="chapters are written in ascending order; reorder the rows",
        )
    if start < previous_end:
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"'{alias}' chapter {position} starts at {start}, inside chapter "
            f"{position - 1} which ends at {previous_end}",
            start_cell,
            hint=f"chapters may not overlap: start this one at or after {previous_end}",
        )


def _chapter_number(value: RowValue, column: str, node: exp.Expr) -> int | float:
    """One evaluated ``start_t``/``end_t`` as the number it must be, never NULL."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        got = "NULL" if value is None else repr(value)
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"'{CHAPTERS_COLUMN}.{column}' must be a number, got {got}",
            node,
            hint=f"{column} is a number of seconds, e.g. {_CHAPTER_EXAMPLE}",
        )
    return value


def _chapter_title(value: RowValue, node: exp.Expr) -> str | None:
    """One evaluated ``title`` as text, or None for NULL (ffmetadata omits it)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"'{CHAPTERS_COLUMN}.title' must be a string or NULL, got {value!r}",
            node,
            hint=f"title is text or NULL, e.g. {_CHAPTER_EXAMPLE}",
        )
    if any(char in _UNSAFE_CHAPTER_TITLE for char in value):
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"'{CHAPTERS_COLUMN}.title' {value!r} contains a character "
            "ffmetadata cannot represent unescaped",
            node,
            hint=r"avoid \ = ; # and newlines in a chapter title",
        )
    return value


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


def _check_sink_option_conflicts(
    options: dict[str, object],
    option_nodes: dict[str, exp.Expr],
    path_node: exp.Expr,
) -> None:
    """Reject two sink options that cannot both hold, once all are validated.

    ``faststart``/``movflags`` both set: -movflags either way, so one would
    silently win over the other's spelling. ``strip_metadata``/
    ``metadata_from`` both set: -map_metadata either way, same problem.
    ``codec_params`` with no matching ``video_codec``: its rendered flag (see
    ``sqlmpeg.sink.CODEC_PARAMS_FLAGS``) is derived FROM ``video_codec``, so
    it has nothing to derive from.
    """
    if "faststart" in options and "movflags" in options:
        raise _error(
            ErrorCode.SINK_OPTION_TYPE,
            "'faststart' and 'movflags' both set -movflags",
            option_nodes["movflags"],
            fallback=path_node,
            hint="use 'faststart true' for the common case, or 'movflags' "
            "directly for anything else -- not both",
        )
    if "strip_metadata" in options and "metadata_from" in options:
        raise _error(
            ErrorCode.SINK_OPTION_TYPE,
            "'strip_metadata' and 'metadata_from' both set -map_metadata",
            option_nodes["metadata_from"],
            fallback=path_node,
            hint="'metadata_from' copies an input's global tags through; "
            "'strip_metadata' drops them -- not both",
        )
    if "codec_params" in options:
        codec = options.get("video_codec")
        if not isinstance(codec, str) or codec not in CODEC_PARAMS_FLAGS:
            raise _error(
                ErrorCode.SINK_OPTION_TYPE,
                f"'codec_params' needs a matching video_codec, got {codec!r}",
                option_nodes["codec_params"],
                fallback=path_node,
                hint="set video_codec to one of: "
                + ", ".join(sorted(CODEC_PARAMS_FLAGS)),
            )
    if options.get("two_pass") is True:
        _check_two_pass(options, option_nodes, path_node)


def _check_two_pass(
    options: dict[str, object],
    option_nodes: dict[str, exp.Expr],
    path_node: exp.Expr,
) -> None:
    """The rate-control rules a ``two_pass true`` sink must satisfy.

    Two-pass exists to hit a target bitrate with a codec that has a ``-pass``
    mode, so it needs both and cannot coexist with ``crf``, which is the other
    rate control entirely.
    """
    anchor = option_nodes.get("two_pass")
    if "crf" in options:
        raise _error(
            ErrorCode.SINK_OPTION_TYPE,
            "'crf' and 'two_pass' are two different rate controls",
            option_nodes.get("crf", anchor),
            fallback=path_node,
            hint="two-pass targets a bitrate (video_bitrate); crf targets a "
            "quality level -- pick one",
        )
    codec = options.get("video_codec")
    if not isinstance(codec, str) or codec not in TWO_PASS_CODECS:
        raise _error(
            ErrorCode.SINK_OPTION_TYPE,
            f"'two_pass' needs a video_codec with a -pass mode, got {codec!r}",
            option_nodes.get("video_codec", anchor),
            fallback=path_node,
            hint="set video_codec to one of: " + ", ".join(sorted(TWO_PASS_CODECS)),
        )
    if "video_bitrate" not in options:
        raise _error(
            ErrorCode.SINK_OPTION_TYPE,
            "'two_pass' needs a video_bitrate to target",
            anchor,
            fallback=path_node,
            hint="two-pass exists to hit a bitrate, e.g. video_bitrate '2500k'",
        )


def _check_two_pass_outputs(
    options: dict[str, object], outputs: list[Output], path_node: exp.Expr
) -> None:
    """A ``two_pass`` sink must have a video output for pass 1 to analyse."""
    if options.get("two_pass") is not True:
        return
    if not any(output.type == "video" for output in outputs):
        raise _error(
            ErrorCode.SINK_OPTION_TYPE,
            "'two_pass' analyses a video stream, but this COPY selects none",
            path_node,
            hint="select a video column, or drop two_pass",
        )


def _check_two_pass_is_single_sink(sinks: list[SinkUnit], raws: list[RawSink]) -> None:
    """``two_pass`` is one COPY per script: nothing sequences per-COPY passes."""
    if len(sinks) <= 1:
        return
    for unit, raw in zip(sinks, raws):
        if unit.options.get("two_pass") is True:
            raise _error(
                ErrorCode.SINK_OPTION_TYPE,
                f"'two_pass' is not supported in a {len(sinks)}-COPY script",
                raw.path_node,
                hint="a script's COPYs share one ffmpeg command; two_pass "
                "splits it in two -- write the two-pass COPY on its own",
            )


# typed values, bindings, per-branch environment


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


# Table mode only: the sentinel `_Stream.ref` for an
# outer join's NULL row, read back by `_value_to_cells`. Never a real
# FrameRef -- every well-formed one is non-empty (a node id or a "src:..."
# ref) -- so there is no ambiguity with an actual stream.
_NULL_STREAM_REF: FrameRef = ""


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

    `splat` matters only when `value.is_array`: True means the array IS a row
    set (a row alias's stream column, or a call over one) that a table query
    prints one row per element, like :meth:`_Lowerer._value_to_cells` already
    does outside a CTE. False means the array is a single unit -- an
    ``array_agg`` or a bare input array column -- that a table query
    broadcasts as ONE cell instead (see :meth:`_Lowerer._array_cell_broadcast`).
    Ignored for a scalar column.
    """

    name: str | None
    value: _Value
    splat: bool = True


@dataclass(frozen=True)
class _InputBinding:
    """``FROM input('x.mp4') a`` — exposes ``a.video[k]`` / ``a.audio[k]``."""

    alias: str


@dataclass(frozen=True)
class _CteBinding:
    """``FROM <cte>`` — a TABLE of the rows its body produced.

    `columns` is what the body's SELECT list named, each column holding every
    stream it carries. `rows` is the body's ROW count, which a splat array
    column carries one element per; `relation` is the branch's joined row set,
    so a column of that width reads back one element per result row and a
    cross join with a second source repeats it honestly.
    """

    name: str
    columns: tuple[_Column, ...]
    rows: int = 1
    relation: _RowRelation | None = None


@dataclass
class _SourceBinding:
    """``FROM ffmpeg.<source>(...) a`` — exposes ONE statically-typed stream.

    Everything about the stream is known before any projection lowers: the
    registry's :class:`~sqlmpeg.registry.SourceFilter` says which
    type the source's single output pad carries, so ``a.video[1]``
    (video sources), ``a.audio[1]`` (audio ones), the bare array
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
    ``SELECT a.video[1], hflip(a.video[1]) FROM ffmpeg.testsrc(...) a`` is one
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


# A track-row metadata value: NULL (unprobed input, or a field this file does
# not carry) or the probed scalar. A disposition flag is the boolean case.
# Never a stream — the row IS that.
RowValue = str | int | float | bool | None

# `_TrackRow.stream` for a row that carries no track -- a chapter row, or a
# written VALUES row. Never a real stream (neither exposes a stream column at
# all), only a dataclass filler. Its ref deliberately fails `is_src()` (no
# "src:" prefix) and is not a node id either, so anything that somehow did try
# to render it fails fast with "unknown node" rather than silently wiring up
# the wrong stream.
_STREAMLESS_ROW = _Stream(ref="rows:no-stream", type="data", source=None)


@dataclass(frozen=True)
class _TrackRow:
    """One row of an ``unnest`` table: the track, plus its metadata columns.

    `stream` IS the row's stream, and its ``_Stream.source`` is the very
    ``StreamMeta`` `columns` was read from — a row's provenance and its columns
    are the same probed fact, seen twice.
    """

    stream: _Stream
    columns: dict[str, RowValue]


@dataclass(frozen=True)
class _CteRow:
    """One row of a CTE source: which row of the body's row set it is.

    A CTE row has no metadata columns of its own — the body named what it
    named, and those columns are streams — so the position is all a result
    tuple needs to read the row's value back out of each column's array.
    """

    position: int


# What one result row holds per FROM alias: a track (or a gap, where an outer
# join found no counterpart) for a row table, a position for a CTE source.
_RowTuple = dict[str, "_TrackRow | _CteRow | None"]


@dataclass
class _RowRelation:
    """One branch's joined row set: every row source, aligned.

    `tuples` is the relation itself — one dict per result ROW, mapping each row
    alias to that row's track, or to ``None`` where an outer join left a gap,
    and each CTE alias to the body row it took. All of a branch's row sources
    share this one object, which is what keeps
    ``a`` and ``b`` aligned: element `i` of each is the pair the
    join made, so the existing zip/broadcast machinery wires the right streams
    together without learning that joins exist.

    Row order is the join's, never sorted implicitly: the
    LEFT side's order, then — for a FULL join only — the unmatched right rows
    in their own order. `keys` remembers which columns each side was matched
    on, so a NULL track can say what it failed to match.
    """

    aliases: list[str] = field(default_factory=list)
    tuples: list[_RowTuple] = field(default_factory=list)
    keys: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class _RowBinding:
    """``FROM ..., unnest(<input>.<type>) t`` — a compile-time TABLE.

    `rows` is this alias's column of the branch's joined relation, in ROW
    ORDER: the surviving row set, one entry per result row, ``None`` where an
    outer join found no counterpart. It is what the WHERE predicate and the
    ORDER BY rewrite (both act on the shared :class:`_RowRelation`, so every
    alias stays aligned), and both happen once per branch before any projection
    lowers. Selecting ``t`` over N surviving rows is an N-element array in
    that order, which is the same array value a bare ``f.audio`` produces — the
    row model and the array model are one mechanism.

    `source` is the INPUT alias the tracks belong to. Everything downstream
    (the ``-i``, its WHERE window, provenance) keys off THAT alias, not the row
    one: a row table takes no input slot of its own. `values` is set instead
    for a WRITTEN row source (a VALUES CTE in FROM), whose rows come from the
    query rather than from a probe; it has no input alias and no streams.
    """

    alias: str
    source: str
    column: str  # the array that was unnested: video/audio/subtitle/data
    type: StreamType
    relation: _RowRelation
    values: RawValuesTable | None = None

    @property
    def rows(self) -> tuple[_TrackRow | None, ...]:
        return tuple(_track_of(row, self.alias) for row in self.relation.tuples)

    @property
    def streamless(self) -> bool:
        """True for rows that carry no track: chapters, and written rows."""
        return self.values is not None or self.column == CHAPTERS_COLUMN

    @property
    def schema(self) -> dict[str, RowColumnType]:
        """The columns these rows expose, in declaration (or written) order."""
        return (
            self.values.schema() if self.values is not None else ROW_SCHEMAS[self.column]
        )

    @property
    def star(self) -> tuple[str, ...]:
        """What ``<alias>.*`` expands to: the scalar columns, in order."""
        if self.values is not None:
            return self.values.columns
        return ROW_STAR_COLUMNS[self.column]

    @property
    def readonly(self) -> frozenset[str]:
        """The columns a query may not assert. A written row has none."""
        if self.values is not None:
            return frozenset()
        return ROW_READONLY_FIELDS[self.column]

    @property
    def exposes(self) -> str:
        """How a rejection names this row source's column list."""
        listed = ", ".join(sorted(self.schema))
        if self.values is not None:
            return f"'{self.alias}' exposes {listed}"
        return f"{self.column} track rows expose {listed}"


_Binding = _InputBinding | _CteBinding | _SourceBinding | _RowBinding

# Metadata tag overrides for one query: probed StreamMeta identity -> the keys
# its output streams set, with None for a key the query clears.
_TagOverrides = dict[int, dict[str, str | None]]

# What a query being lowered can tag. "sink" is a query that writes a file:
# per-stream tags where it has track rows, container tags where it has none.
# "rows" is a CTE body -- per-stream tags only, since a CTE has no container.
_TagScope = Literal["sink", "rows"]

# One query's per-track disposition writes: probed StreamMeta identity -> the
# flags that stream's output sets, in declared order. An empty tuple is the
# written `'0'`: every flag off.
_DispositionOverrides = dict[int, tuple[str, ...]]


def _track_of(row: _RowTuple, alias: str) -> _TrackRow | None:
    """One result row's track for a row alias; None for a gap or a CTE row."""
    entry = row.get(alias)
    return entry if isinstance(entry, _TrackRow) else None


def _cte_row_count(columns: Iterable[_Column]) -> int:
    """How many rows a CTE body produced: the width of its row-set columns.

    A splat array column carries one stream per body row, so its length IS the
    body's row count; a body with none (a single input row, a UNION ALL's
    concat, a broadcast array) is one row.
    """
    widths = [
        len(column.value.streams)
        for column in columns
        if column.splat and column.value.is_array
    ]
    return max(widths) if widths else 1


def _map_key(name: str) -> str:
    """The key a folded map path names, else the column name itself."""
    ref = map_ref(name)
    return ref[1] if ref is not None else name


def _tags_to_cell(tags: dict[str, str]) -> ArrayCell:
    """One tag map as an array cell of ``(key,value)`` records, in key order."""
    return ArrayCell(
        elements=tuple(RecordCell(fields=(key, tags[key])) for key in sorted(tags))
    )


def _tag_cell(row: _TrackRow | None) -> CellValue:
    """One row's whole tag map as a cell; NULL for an outer join's gap."""
    if row is None:
        return None
    source = row.stream.source
    return _tags_to_cell({} if source is None else source.metadata)


def _flags_to_cell(flags: dict[str, bool]) -> ArrayCell:
    """One disposition as an array cell of ``(key,set)`` records, in flag order.

    The key set is CLOSED, so every declared flag is an entry; one this ffmpeg
    did not report reads NULL, the way an absent tag does.
    """
    return ArrayCell(
        elements=tuple(
            RecordCell(fields=(key, flags.get(key))) for key in DISPOSITION_KEYS
        )
    )


def _disposition_cell(row: _TrackRow | None) -> CellValue:
    """One row's whole flag map as a cell; NULL where nothing was probed."""
    if row is None or row.stream.source is None:
        return None
    return _flags_to_cell(row.stream.source.disposition)


def _row_star_error(
    binding: _RowBinding, anchor: exp.Expr, select: exp.Select
) -> SqlmpegError:
    """``*`` over rows in a MEDIA query: fields are not output streams.

    A star over a row table expands the record's fields, the same as it does in
    a table query. Fields have nowhere to go on an ffmpeg command line; for a
    track row the stream the query means is the bare alias, and a chapter row
    has no stream at all.
    """
    printed = "a bare SELECT prints the fields as a table"
    hint = (
        f"these rows carry no stream; {printed}"
        if binding.streamless
        else f"the row is the stream: select {binding.alias}; {printed}"
    )
    return _error(
        ErrorCode.UNSUPPORTED_SQL,
        f"'*' over the rows of '{binding.alias}' expands their fields, and a "
        "SELECT column is an output stream",
        anchor,
        fallback=select,
        hint=hint,
    )


def _row_columns(meta: StreamMeta, column: str) -> dict[str, RowValue]:
    """One probed stream's row columns.

    Three sources, one table: the stream's own tags (``StreamMeta.metadata``)
    and its disposition flags each become one column per key under a folded
    path name, everything else comes from a field of the StreamMeta itself. An
    absent field is NULL, and so is a key the file does not carry — which is
    the whole NULL story, there is no other way for a row column to be null.

    ``index`` is +1'd: ``StreamMeta.index`` is the 0-based per-type index the
    IR ref uses, and the SQL surface is 1-based everywhere (``f.audio[1]``), so
    ``WHERE t.index = 1`` and ``f.audio[1]`` name the same track.

    The enriched fields (``codec``, ``channels``, ``channel_layout``,
    ``bitrate``, ``duration``, ``color_transfer``) are read through
    :func:`getattr` deliberately: a StreamMeta built without them yields NULL
    columns rather than an AttributeError -- exactly what an unprobed field
    yields anyway.
    """
    schema = ROW_SCHEMAS[column]
    values: dict[str, RowValue] = {
        "index": meta.index + 1,
        "width": meta.width,
        "height": meta.height,
        "fps": meta.fps,
        "sample_rate": meta.sample_rate,
    }
    for name in ("codec", "channels", "channel_layout", "bitrate", "duration",
                 "color_transfer"):
        probed = getattr(meta, name, None)
        values[name] = probed if isinstance(probed, str | int | float) else None
    columns = {name: values.get(name) for name in schema if name not in MAP_COLUMNS}
    columns.update({tag_path(key): value for key, value in meta.metadata.items()})
    columns.update(
        {
            map_path(DISPOSITION_COLUMN, key): meta.disposition[key]
            for key in DISPOSITION_KEYS
            if key in meta.disposition
        }
    )
    return columns


def _join_keys(on: exp.Expr) -> dict[str, list[str]]:
    """Which columns each row alias was matched on, from a JOIN's ON predicate.

    Bookkeeping for one message only: a NULL track says what it failed to
    match (``no 'b' row matched a.tags.language='fra'``), and that needs the key
    columns of the side that DID match. Order is written order, deduplicated.
    """
    keys: dict[str, list[str]] = {}
    for sub in on.walk():
        if not isinstance(sub, exp.Column):
            continue
        table_node = sub.args.get("table")
        if table_node is None:
            continue
        names = keys.setdefault(_fold(table_node), [])
        name = _fold(sub.this)
        if name not in names:
            names.append(name)
    return keys


# The fill each track type takes when an outer join leaves a gap. Quoted
# verbatim in the NULL-track hint, so it is spelled the way a user would paste
# it. `data` is absent deliberately: nothing generates a data track, so there
# is no fill to suggest.
_FILL_SPELLINGS: dict[StreamType, str] = {
    "audio": f"{FILTER_NAMESPACE}.anullsrc()",
    "video": f"{FILTER_NAMESPACE}.color()",
    "subtitle": f"{MACRO_NAMESPACE}.empty_captions()",
}

_COALESCE_HINT = (
    "COALESCE fills an outer join's gaps: COALESCE(b, "
    f"{FILTER_NAMESPACE}.anullsrc(duration => 2)) for audio, "
    f"{FILTER_NAMESPACE}.color() for video, "
    f"{MACRO_NAMESPACE}.empty_captions() for captions"
)


# The compile-time row predicate evaluator.
#
# Every column of a track row is PROBED metadata, so a predicate over rows is
# decidable here, at compile time, and never reaches ffmpeg -- the way a
# `WHERE t BETWEEN` vanishes into `-ss`/`-to`. Standard SQL three-valued logic
# throughout: a comparison against NULL is UNKNOWN (python `None`), AND/OR/NOT
# are Kleene, and WHERE keeps a row only when its predicate came back TRUE, so
# "NULL matches nothing" falls out rather than being a rule of ours.
#
# `resolve` already shape- and type-checked everything below; the rejections
# here are defensive re-checks raising the same SqlmpegError resolve would.


def _kleene_and(left: bool | None, right: bool | None) -> bool | None:
    if left is False or right is False:
        return False
    if left is None or right is None:
        return None
    return True


def _kleene_or(left: bool | None, right: bool | None) -> bool | None:
    if left is True or right is True:
        return True
    if left is None or right is None:
        return None
    return False


# `<literal> OP <column>` is the same predicate as `<column> OP' <literal>`
# with the ordering operators inverted; the two equality ones are their own
# mirror. sqlglot does NOT normalize operand order at parse time (the same
# thing `_time_bounds` handles for time bounds), so the mirror is explicit.
_MIRRORED_COMPARISONS: dict[type[exp.Expr], type[exp.Expr]] = {
    exp.EQ: exp.EQ,
    exp.NEQ: exp.NEQ,
    exp.GT: exp.LT,
    exp.GTE: exp.LTE,
    exp.LT: exp.GT,
    exp.LTE: exp.GTE,
}


def _sort_key(value: RowValue) -> tuple[int, str, float]:
    """A total, type-stable sort key for one non-NULL row-column value.

    A column's type is static, so the two branches never actually compete
    within one sort — the tuple shape is what keeps the comparison total
    anyway, rather than letting a surprising value raise a TypeError deep
    inside ``list.sort``.
    """
    if isinstance(value, str):
        return (0, value, 0.0)
    return (1, "", float(value if value is not None else 0))


def _compare(node: exp.Expr, left: RowValue, right: RowValue) -> bool | None:
    """One comparison under SQL NULL semantics; None is UNKNOWN, never False."""
    if left is None or right is None:
        return None
    if isinstance(node, exp.EQ):
        return left == right
    if isinstance(node, exp.NEQ):
        return left != right
    if isinstance(left, str) != isinstance(right, str):
        # Unreachable via resolve (a column's type is static and the literal
        # was checked against it), and an ordering comparison across the two
        # would be a python TypeError rather than an answer.
        return None
    if isinstance(node, exp.GT):
        return left > right  # type: ignore[operator]
    if isinstance(node, exp.GTE):
        return left >= right  # type: ignore[operator]
    if isinstance(node, exp.LT):
        return left < right  # type: ignore[operator]
    return left <= right  # type: ignore[operator]


@dataclass
class _Env:
    """Everything one SELECT branch resolves names against."""

    bindings: dict[str, _Binding] = field(default_factory=dict)
    # CTE name -> its WHERE window. CTE-ONLY: an INPUT alias's window is a
    # property of its `-i`, not of this branch, so `_collect_trims` records it
    # in `Graph.input_trims` instead and no filter trim is ever spliced for it.
    # Either half may be None (an open-ended window).
    trims: dict[str, tuple[int | float | None, int | float | None]] = field(
        default_factory=dict
    )
    # base stream ref -> its trimmed ref, so one filter trim is shared by every
    # consumer of that stream inside this branch (CTE-only, as above).
    trimmed: dict[FrameRef, FrameRef] = field(default_factory=dict)
    # The branch's joined row set, or None until its first
    # `unnest` binds. There is at most ONE: every row table of a branch joins
    # into it, comma sources included (the comma between two unnests is the
    # bounded cross join), so all row aliases stay aligned by construction.
    relation: _RowRelation | None = None
    # True for a branch that aggregates -- a GROUP BY, an `array_agg`, or both.
    # Its scalar columns are group-constants (resolve's grouping check proves
    # that), so they tag the CONTAINER rather than the tracks.
    grouped: bool = False
    # The GROUP BY keys that read a track-row column: the ones that actually
    # partition the relation. An input-level or constant key has the same value
    # for every tuple and leaves one group.
    group_keys: tuple[exp.Expr, ...] = ()


# ExpandCtx


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


# the lowering walk


class _Lowerer:
    def __init__(
        self,
        res: Resolved,
        probes: dict[str, ProbeResult | None],
        registry: Registry | None,
        fanout_index: int = 0,
        *,
        fanout_sinks: bool = False,
    ) -> None:
        self.res = res
        self.probes = probes
        self.registry = registry
        self.graph = Graph(input_paths=list(res.input_paths), sources=dict(res.sources))
        self.ctx = _NodeFactory(self.graph)
        self.cte_columns: dict[str, tuple[_Column, ...]] = {}
        # Inputs this pass minted itself (`sqlmpeg.empty_captions()`),
        # alias -> its INTERNAL input options. Merged into `Graph.input_options`
        # by `_lower_input_options`, which is the only writer of that field.
        self.minted_input_options: dict[str, dict[str, object]] = {}
        # The tag columns of the query being lowered; reset per query, since
        # two COPYs may tag the same track differently.
        self.tags: _TagOverrides = {}
        # The tag columns of every CTE body, harvested as each one lowers and
        # kept for the whole pass: a CTE's streams carry their tags into
        # whichever sink maps them, under that sink's own tags.
        self.cte_tags: _TagOverrides = {}
        # The disposition columns of the query being lowered, and of every CTE
        # body, on the same two-scope plan the tags follow.
        self.dispositions: _DispositionOverrides = {}
        self.cte_dispositions: _DispositionOverrides = {}
        # The same for the CONTAINER tags of the file being written, key ->
        # value, None meaning "clear this key".
        self.container_tags: dict[str, str | None] = {}
        # The chapter list of the file being written: the ffmpeg input index
        # its chapters come from, `ir.NO_CHAPTERS` for a written NULL, and None
        # while no `chapters` column has been read. Reset per COPY.
        self.chapters: int | None = None
        # Output fan-out: which row of the sink's relation THIS run binds, the
        # sink's TO expression once it is known to reference a row column, and
        # the pinned row / its branch environment once `_pin_fanout_row` runs.
        # `fanout_count` is the relation's surviving row count, i.e. how many
        # FILES the query writes; None until a pin happens, which is what tells
        # `lower_commands` this was not a fan-out query at all.
        self.fanout_index = fanout_index
        # True -> every fan-out row becomes a SinkUnit of THIS graph (one
        # command, several output files) and its time window rides that unit
        # instead of the shared `-i`. False -> `fanout_index` alone binds, one
        # graph per row, which is the `&&` chain a stream-copy trim needs.
        self.fanout_sinks = fanout_sinks
        # The input windows the row being lowered named, alias -> (start, end).
        # Reset per row; harvested into that row's `SinkUnit.window`.
        self.fanout_windows: dict[str, tuple[float | None, float | None]] = {}
        # True once a row named two windows at once: no single output seek
        # says that, so the fan-out falls back to the chain.
        self.fanout_window_conflict = False
        self.fanout_expr: exp.Expr | None = None
        # Sticky across sinks, unlike `fanout_expr`: the loudnorm2 limits ask
        # whether ANY COPY of the script fanned out.
        self.fanout_seen = False
        self.fanout_row: _RowTuple = {}
        self.fanout_env: _Env | None = None
        self.fanout_count: int | None = None
        # True when the pin partitioned by a GROUP BY key rather than by row,
        # so a collision message names groups.
        self.fanout_grouped = False
        # The COPY whose query is lowering: the node its row-count rejection
        # anchors on, and the path it names. Both None for a bare SELECT,
        # which names no destination at all.
        self.sink_anchor: exp.Expr | None = None
        self.sink_path: str | None = None
        # True for the whole duration of `run_table()`; `run()` never sets it.
        # Table mode changes exactly one thing about the stream machinery it
        # otherwise reuses verbatim: an outer join's NULL row is an empty cell
        # rather than a rejection (see `_row_stream`).
        self.table_mode = False

    # -- entry point ------------------------------------------------------

    def run(self) -> Graph:
        """Lower every CTE/view once, then one :class:`SinkUnit` per COPY.

        The bindings come first and are shared: ``res.ctes`` holds a script's
        views AND every COPY's own ``WITH``, in written order, and
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
                self._lower_query(union_branches(body), body, tags="rows")
            )
            self._harvest_cte_tags(body)
            self._harvest_cte_dispositions(body)
        if self.res.sinks:
            self.graph.sinks = self._lower_sinks()
            if self.fanout_count is None:
                _check_two_pass_is_single_sink(self.graph.sinks, self.res.sinks)
        else:
            columns = self._lower_query(self.res.branches, self.res.select, tags="sink")
            self.graph.sinks = [
                SinkUnit(
                    outputs=_outputs(
                        columns, self._layered_tags(), self._layered_dispositions()
                    ),
                    tags=dict(self.container_tags),
                    chapters=self.chapters,
                )
            ]
        self._check_loudnorm2()
        self.graph.input_options = self._lower_input_options()
        return self.graph

    def _check_loudnorm2(self) -> None:
        """The v1 limits on ``sqlmpeg.loudnorm2``.

        It is not one filter among others: its presence turns the whole
        compile into a two-command sequence with a shell handoff in the
        middle. Everything that would need a SECOND sequencing rule on top of
        that -- a second loudnorm2, a ``two_pass`` sink, a fan-out TO -- is
        closed rather than guessed at. Counted over NODES, so a call
        broadcast across an audio array is caught as the several it is.

        The fan-out rejection comes FIRST: a fan-out mints the call once per
        file it writes, so the count would otherwise report a multiplicity the
        query text does not show.
        """
        anchors = [(raw.path_expr, raw.path_node) for raw in self.res.sinks]
        anchor, fallback = anchors[0] if anchors else (self.res.select, self.res.select)
        count = sum(1 for n in self.graph.nodes.values() if n.filter == loudnorm.FILTER)
        if count == 0:
            return
        if self.fanout_seen:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "sqlmpeg.loudnorm2() and a fan-out TO cannot both be set",
                anchor,
                fallback=fallback,
                hint="a TO expression writes one file per row, each needing its "
                "own measuring pass; write a quoted TO path",
            )
        if count > 1:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"one sqlmpeg.loudnorm2() per query, got {count}",
                anchor,
                fallback=fallback,
                hint="each one needs its own measuring pass; write one query per "
                "stream you are normalizing",
            )
        for unit, raw in zip(self.graph.sinks, self.res.sinks):
            if unit.options.get("two_pass") is True:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "'two_pass' and sqlmpeg.loudnorm2() cannot both be set",
                    raw.path_node,
                    hint="both compile to a command sequence of their own; "
                    "normalize the audio in a separate COPY",
                )

    # -- the COPY sink ----------------------------------

    def _lower_sinks(self) -> list[SinkUnit]:
        """One :class:`SinkUnit` per COPY — or per fan-out ROW/GROUP.

        A fan-out COPY is alone in its script (the parser sees to that) and
        writes one file per surviving row, so under `fanout_sinks` it lowers
        once per row into THIS graph: shared streams are minted once and the
        split pass fans them out across the units, exactly as it does for a
        view several COPYs read. The row COUNT is a property of the probed
        relation, so it comes back from the first pass rather than being known
        up front.
        """
        units: list[SinkUnit] = []
        for raw in self.res.sinks:
            units.append(self._lower_sink(raw))
            count = self.fanout_count
            if not self.fanout_sinks or count is None:
                continue
            for index in range(1, count):
                self.fanout_index = index
                units.append(self._lower_sink(raw))
            self.fanout_index = 0
        return units

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

        ``metadata_from`` is pulled out of the ordinary name/value loop and
        resolved separately: its value is a bare identifier (an input alias),
        never a literal, so ``SINK_OPTIONS``' str/int/bool/num machinery does
        not apply to it.

        A ``TO (<expression>)`` reaching here is a fan-out sink exactly when it
        reads a track-row column; that decision is made FIRST, since it changes
        how the wrapped query lowers (one pinned row, per-row seek bounds).
        """
        self.fanout_expr = (
            raw.path_expr
            if raw.path_expr is not None
            and references_row_alias(raw.path_expr, set(self.res.track_rows))
            else None
        )
        self.fanout_seen = self.fanout_seen or self.fanout_expr is not None
        self.sink_anchor = raw.path_expr if raw.path_expr is not None else raw.path_node
        self.sink_path = raw.path
        self.fanout_windows = {}
        self.chapters = None
        columns = self._lower_query(list(raw.branches), raw.query, tags="sink")
        options: dict[str, object] = {}
        option_nodes: dict[str, exp.Expr] = {}
        metadata_from_opt: RawSinkOption | None = None
        for option in raw.options:
            if option.name == "metadata_from":
                metadata_from_opt = option
                continue
            line, col = _pos(option.name_node, option.value, raw.path_node)
            options[option.name] = validate_sink_option(
                option.name, _sink_value(option.value), line=line, col=col
            )
            option_nodes[option.name] = option.value
        if metadata_from_opt is not None:
            options["metadata_from"] = self._lower_metadata_from(
                metadata_from_opt, raw.path_node
            )
            option_nodes["metadata_from"] = metadata_from_opt.value
        _check_sink_option_conflicts(options, option_nodes, raw.path_node)
        outputs = _outputs(columns, self._layered_tags(), self._layered_dispositions())
        _check_two_pass_outputs(options, outputs, raw.path_node)
        path = raw.path
        if raw.path_expr is not None:
            self._check_fanout_options(options, raw)
            path = self._sink_path(raw)
        return SinkUnit(
            outputs=outputs,
            path=path,
            options=options,
            tags=dict(self.container_tags),
            window=self._fanout_window(),
            chapters=self.chapters,
        )

    # -- the fan-out TO expression ------------------------------------

    def _fanout_window(self) -> tuple[float | None, float | None] | None:
        """This row's OUTPUT window: the one input window its WHERE named.

        An output seek is a property of the FILE, so every alias the row
        trimmed has to agree on it. Two disagreeing windows are not a
        rejection — they are recorded and send the whole fan-out back to one
        command per row, where each alias seeks its own ``-i`` again.
        """
        windows = set(self.fanout_windows.values())
        if not windows:
            return None
        if len(windows) > 1:
            self.fanout_window_conflict = True
            return None
        return windows.pop()

    def _check_fanout_options(self, options: dict[str, object], raw: RawSink) -> None:
        """The sink options a fan-out COPY does not take, v1.

        ``two_pass`` already compiles to a command SEQUENCE of its own, and
        ``metadata_from`` names one input per file; both are small matrices
        left closed rather than guessed at.
        """
        if self.fanout_expr is None:
            return
        # `metadata_from` carries an input INDEX, so 0 is a set value; only
        # `two_pass false` is a set option that asks for nothing.
        for name in ("two_pass", "metadata_from"):
            if name not in options or options[name] is False:
                continue
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{name}' and a fan-out TO cannot both be set",
                raw.path_expr,
                fallback=raw.path_node,
                hint=f"a TO expression writes one file per row; drop {name}, or "
                "write a quoted TO path",
            )

    def _sink_path(self, raw: RawSink) -> str:
        """``TO (<expression>)`` evaluated: this command's destination.

        A constant expression is an ordinary path. A fan-out one is the pinned
        row's, and is checked for the two things a name built from file
        metadata must not smuggle in: a NULL (an unprobed column, named), and a
        path separator or ``..`` inside a COMPUTED segment.
        """
        expression = raw.path_expr
        if expression is None:  # defensive: the caller checked it
            raise _error(ErrorCode.INTERNAL, "sink path expression is missing")
        env = self.fanout_env if self.fanout_env is not None else _Env()
        # The wrapped query's first branch is the anchor every rejection below
        # falls back to; `raw.query` may be a Union, which `_eval_value` is not
        # typed for.
        anchor = raw.branches[0] if raw.branches else exp.Select()
        if self.fanout_expr is not None and self.fanout_env is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "a TO expression reads a track-row table this COPY's FROM does "
                "not bind",
                expression,
                fallback=raw.path_node,
                hint="unnest the rows in the COPY's own FROM, e.g. FROM "
                "input(:'src') f, unnest(f.audio) t",
            )
        for segment in _computed_segments(expression, set(self.res.track_rows)):
            self._check_path_segment(segment, env, raw, anchor)
        value = self._eval_value(expression, env, self.fanout_row, anchor)
        if value is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "the TO expression is NULL for this row: "
                + self._null_field(expression, env, anchor),
                expression,
                fallback=raw.path_node,
                hint="COALESCE the column, or filter the rows that lack it",
            )
        return _tag_text(value)

    def _check_path_segment(
        self, segment: exp.Expr, env: _Env, raw: RawSink, anchor: exp.Select
    ) -> None:
        """One computed piece of a path: no separator, no ``..``."""
        value = self._eval_value(segment, env, self.fanout_row, anchor)
        if not isinstance(value, str):
            return
        found = next((bad for bad in ("/", "\\", "..") if bad in value), None)
        if found is None:
            return
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"a computed path segment may not contain {found!r}, got {value!r}",
            segment,
            fallback=raw.path_node,
            hint="write the directory as a literal: 'out/' || t.tags.language "
            "|| '.m4a'; metadata never chooses the directory",
        )

    def _null_field(self, expression: exp.Expr, env: _Env, anchor: exp.Select) -> str:
        """Which column of the path expression read NULL, for the message."""
        for sub in expression.walk():
            if not isinstance(sub, exp.Column):
                continue
            if self._eval_value(sub, env, self.fanout_row, anchor) is None:
                table_node = sub.args.get("table")
                prefix = f"{_fold(table_node)}." if table_node is not None else ""
                return f"'{prefix}{column_label(_fold(sub.this))}' was never probed"
        return "no column of it has a value"

    # -- the chapters output column ------------------------------------

    def _collect_chapters(
        self, projection: exp.Expr, env: _Env, select: exp.Select, *, scope: _TagScope
    ) -> None:
        """``... AS chapters``: the file's chapter list, from one of three sources.

        A literal ``ARRAY[ROW(...)::chapter, ...]`` and an ``array_agg`` over
        rows both become one self-contained ffmetadata ``data:`` input;
        ``<input>.chapters`` names that input's own list; NULL writes none.
        The value is the FILE's, not a row's, so it is read once per COPY and
        two branches of a UNION ALL have to agree on it.
        """
        if scope != "sink":
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{CHAPTERS_COLUMN}' is the file's chapter list, and a CTE "
                "body writes no file",
                projection,
                fallback=select,
                hint="build the chapter list in the outer SELECT, e.g. "
                "array_agg(ROW(c.title, c.start_t, c.end_t)::chapter) AS chapters",
            )
        if self.fanout_expr is not None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{CHAPTERS_COLUMN}' and a fan-out TO cannot both be set",
                projection,
                fallback=select,
                hint="a TO expression writes one file per row; drop the "
                "chapters column, or write a quoted TO path",
            )
        index = self._chapters_input(_unwrap(projection), env, select)
        if self.chapters is not None and self.chapters != index:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{CHAPTERS_COLUMN}' takes two different chapter lists",
                projection,
                fallback=select,
                hint="a file has one chapter list, so write the column once; "
                "the branches of a UNION ALL write one file between them",
            )
        self.chapters = index

    def _chapters_input(self, value: exp.Expr, env: _Env, select: exp.Select) -> int:
        """The ffmpeg input index a ``chapters`` column resolves to."""
        if isinstance(value, exp.Null):
            return NO_CHAPTERS
        copied = self._copied_chapters(value, env)
        if copied is not None:
            return copied
        records = self._chapter_records(value, env, select)
        if not records:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{CHAPTERS_COLUMN}' is an empty list",
                value,
                fallback=select,
                hint=f"write at least one chapter, or NULL AS {CHAPTERS_COLUMN} "
                "for a file with none",
            )
        text = _chapters_ffmetadata(records)
        uri = "data:text/plain;base64," + base64.b64encode(text.encode()).decode()
        return self._mint_chapters_input(uri)

    def _copied_chapters(self, value: exp.Expr, env: _Env) -> int | None:
        """The input index behind ``<input>.chapters``, else None."""
        if not isinstance(value, exp.Column) or isinstance(value.this, exp.Star):
            return None
        table_node = value.args.get("table")
        if table_node is None or _fold(value.this) != CHAPTERS_COLUMN:
            return None
        binding = env.bindings.get(_fold(table_node))
        if not isinstance(binding, _InputBinding):
            return None
        return self.graph.sources.get(binding.alias)

    def _chapter_records(
        self, value: exp.Expr, env: _Env, select: exp.Select
    ) -> list[_Chapter]:
        """The chapter records a ``chapters`` column lists, in written order.

        A literal array is evaluated ONCE, over the branch's first row -- the
        list belongs to the file, not to a row -- so it may read an input's
        ``duration`` or a variable. ``array_agg`` is the per-row form: one
        record per surviving row, in row order.
        """
        if isinstance(value, exp.ArrayAgg):
            inner = value.this
            relation = env.relation
            if not isinstance(inner, exp.Expr) or relation is None:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "array_agg() aggregates rows, and this query has none",
                    value,
                    fallback=select,
                    hint=_CHAPTERS_COLUMN_HINT,
                )
            return [
                self._chapter_record(inner, env, row, select) for row in relation.tuples
            ]
        if isinstance(value, exp.Array):
            row = _group_row(env)
            return [
                self._chapter_record(element, env, row, select)
                for element in value.expressions
                if isinstance(element, exp.Expr)
            ]
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"'{CHAPTERS_COLUMN}' takes an array of chapter records, got "
            f"{_describe(value)}",
            value,
            fallback=select,
            hint=_CHAPTERS_COLUMN_HINT,
        )

    def _chapter_record(
        self, node: exp.Expr, env: _Env, row: _RowTuple, select: exp.Select
    ) -> _Chapter:
        """One ``ROW(title, start_t, end_t)::chapter``, evaluated and checked.

        The positional signature is the declared one
        (:data:`~sqlmpeg.types.RECORD_FIELDS`): a query supplies the writable
        fields, in declaration order, and never the probed ``index``. Each
        value takes the ordinary compile-time value grammar.
        """
        node = _unwrap(node)
        record = record_cast_type(node)
        fields = RECORD_FIELDS[CHAPTER_TYPE]
        written = _record_args(node) if record == CHAPTER_TYPE else None
        if written is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"a chapter is written as {_CHAPTER_LITERAL}, got "
                f"{_describe(node)}",
                node,
                fallback=select,
                hint=_CHAPTERS_COLUMN_HINT,
            )
        if len(written) != len(fields):
            named = ", ".join(field.name for field in fields)
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"a chapter takes {len(fields)} values ({named}), got "
                f"{len(written)}",
                node,
                fallback=select,
                hint=_CHAPTERS_COLUMN_HINT,
            )
        cells = dict(zip((field.name for field in fields), written, strict=True))
        values = {
            name: self._eval_value(cell, env, row, select)
            for name, cell in cells.items()
        }
        return _Chapter(
            start=_chapter_number(values["start_t"], "start_t", cells["start_t"]),
            end=_chapter_number(values["end_t"], "end_t", cells["end_t"]),
            title=_chapter_title(values["title"], cells["title"]),
            start_node=cells["start_t"],
            end_node=cells["end_t"],
        )

    def _lower_metadata_from(self, option: RawSinkOption, path_node: exp.Expr) -> int:
        """``metadata_from <alias>``: copy an existing input's own global tags."""
        node = option.value
        name = _bare_name(node)
        index = self.graph.sources.get(name) if name is not None else None
        if index is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'metadata_from' names an input() alias, got {_sink_describe(node)}",
                node,
                fallback=path_node,
                hint="metadata_from copies an input's global tags through, e.g. "
                "metadata_from f for FROM input(:'source') f",
            )
        return index

    def _mint_chapters_input(self, uri: str) -> int:
        """Add one ffmetadata ``data:`` URI as an extra ``-i``; return its index.

        Mirrors :meth:`_mint_input` (the ``empty_captions`` mechanism): the
        alias is spelled so no query can ever collide with it, and it exists
        only to carry the slot in the graph's alias-keyed tables. Unlike
        ``_mint_input`` this returns the plain ffmpeg input INDEX, not a
        stream ref -- ``-map_chapters`` names an input, not a stream.
        """
        index = len(self.graph.input_paths)
        alias = f"{MACRO_NAMESPACE}.chapters#{index + 1}"
        self.graph.input_paths.append(uri)
        self.graph.sources[alias] = index
        self.minted_input_options[alias] = {"format": "ffmetadata"}
        return index

    # -- input() named options ---------------------

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
        # Compiler-minted inputs last: their options are INTERNAL (`-f webvtt`
        # for an `empty_captions` data: URI), already validated by construction,
        # and their aliases cannot collide with a user one.
        result.update(self.minted_input_options)
        return result

    # -- a query (one SELECT, or a UNION ALL of them) ----------------------

    def _lower_query(
        self, branches: list[exp.Select], anchor: exp.Expr, *, tags: _TagScope
    ) -> list[_Column]:
        if not branches:
            raise _error(ErrorCode.UNSUPPORTED_SQL, "query has no SELECT", anchor)
        self.tags = {}
        self.dispositions = {}
        self.container_tags = {}
        lowered = [self._lower_branch(branch, tags=tags) for branch in branches]
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
        """No UNION ALL branch may carry a subtitle/data column.

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

    def _lower_branch(self, select: exp.Select, *, tags: _TagScope) -> list[_Column]:
        env = self._scope(select)
        env.grouped = is_grouped(select)
        env.group_keys = _partition_keys(select, env)
        self._check_grouped_cte_columns(select, env)
        # One WHERE clause, three languages. A conjunct over track-row columns
        # is decided HERE and never reaches ffmpeg; a subscript metadata
        # conjunct is a compile-time ASSERTION (nothing to filter -- the SELECT
        # list already names the exact stream the subscript picked); a time
        # window is a seek on an input. Resolve already rejected a conjunct
        # mixing any two, so the split is total -- except for the one mix a
        # fan-out TO admits, a time window bounded by row columns.
        time_conjuncts, row_conjuncts, assertion_conjuncts = self._split_where(select, env)
        fanout = self.fanout_expr is not None
        # Under fan-out the trims wait for the pin: a bound may name that row.
        if not fanout:
            self._collect_trims(select, env, time_conjuncts)
        self._filter_rows(row_conjuncts, env, select)
        self._check_assertions(assertion_conjuncts, select)
        self._order_rows(select, env)
        self._pin_fanout_row(env, select)
        if fanout:
            self._collect_trims(select, env, time_conjuncts)

        projections = select.expressions
        if not projections:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL, "SELECT has no output column", fallback=select
            )
        columns: list[_Column] = []
        for projection in projections:
            # A star is not an expression, it is a column GENERATOR: it
            # contributes as many columns as the aliases it names have
            # streams, so it expands here rather than in `_lower_expr`.
            qualifier = star_qualifier(projection)
            if qualifier is not None:
                columns += self._expand_star(qualifier, projection, env, select)
                continue
            # The chapter list is a column of the FILE, not a stream and not a
            # tag: one array of chapter records, whatever the branch's rows.
            if _projection_name(projection) == CHAPTERS_COLUMN:
                self._collect_chapters(projection, env, select, scope=tags)
                continue
            # A tag column produces no stream, so it never becomes an output.
            # With track rows the tag is per-stream, without them it is a
            # container tag on the file being written -- which a CTE body
            # ("rows" scope) has no way to name. A GROUPED branch has rows but
            # no per-row scope: its scalars are group-constants, so they tag
            # the group's container.
            if _is_tag_column(projection, env):
                self._check_settable_key(projection, env, select)
                if _projection_name(projection) == DISPOSITION_COLUMN:
                    self._collect_disposition(projection, env, select, scope=tags)
                elif _has_track_rows(env) and not env.grouped:
                    self._collect_tag(projection, env, select)
                elif tags == "sink":
                    self._collect_container_tag(projection, env, select)
                else:
                    raise _error(
                        ErrorCode.UNSUPPORTED_SQL,
                        f"tag column '{_projection_name(projection)}' in a CTE "
                        "body has no track row to tag",
                        projection,
                        fallback=select,
                        hint="a CTE tags the rows it selects, e.g. FROM "
                        "input('f.mkv') f, unnest(f.audio) t; a container tag "
                        "belongs in the outer SELECT",
                    )
                continue
            columns.append(
                _Column(
                    name=_projection_name(projection),
                    value=self._branch_value(projection, env, select),
                    splat=self._is_splat_projection(projection, env),
                )
            )
        if not columns:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "every SELECT column is a metadata tag, so the query selects no "
                "stream",
                fallback=select,
                hint="a tag rides on the file the query writes; select its "
                "tracks too, e.g. SELECT t, ... AS title",
            )
        if tags == "sink":
            self._check_one_row_per_file(select, env)
        return columns

    # -- one row, one file -------------------------------------------------

    def _check_one_row_per_file(self, select: exp.Select, env: _Env) -> None:
        """One row is one file, so a single destination needs a single row.

        The count is the RESOLVED one -- the relation as the WHERE clause and
        the joins left it, partitioned into groups where the branch groups --
        so a row table a predicate narrows to one track writes its one file,
        and rows are combined only where the query says to combine them:
        ``array_agg`` (with ``GROUP BY`` when they share a key), or a fan-out
        ``TO (<expression>)`` that gives each row a destination of its own.

        A fan-out has already been pinned to the one group this command
        writes, so it never reaches the count.
        """
        if self.fanout_expr is not None:
            return
        if env.grouped:
            count = len(self._grouped_partitions(env, select))
            what = "group" if count == 1 else "groups"
            hint = _ONE_FILE_PER_GROUP_HINT
        else:
            count = len(env.relation.tuples) if env.relation is not None else 1
            what = "row" if count == 1 else "rows"
            hint = _ONE_FILE_PER_ROW_HINT
        if count <= 1:
            return
        destination = (
            f"'{self.sink_path}' is one file" if self.sink_path else "it writes one file"
        )
        raise _error(
            ErrorCode.ROW_COUNT_MISMATCH,
            f"this query has {count} {what}, and {destination}",
            self.sink_anchor,
            fallback=select,
            hint=hint,
        )

    def _check_grouped_cte_columns(self, select: exp.Select, env: _Env) -> None:
        """Postgres's grouping rule for the columns only lowering can judge.

        Resolve enforces the rule wherever the SQL text settles it -- a track
        row's columns vary within a group, an input alias's do not. A CTE
        column is neither until its body has been lowered: it varies exactly
        when the body produced more than one row and the column carries one
        stream per row. So the same rejection is raised here, with the same
        wording, for the shape resolve could not see.
        """
        if not env.grouped:
            return
        key_texts = {key.sql() for key in group_keys(select)}
        for projection in select.expressions:
            if isinstance(projection, exp.Expr) and star_qualifier(projection) is None:
                self._check_grouped_cte_expr(
                    _projection_expr(projection), env, select, key_texts
                )

    def _check_grouped_cte_expr(
        self, node: exp.Expr, env: _Env, select: exp.Select, key_texts: set[str]
    ) -> None:
        """One expression of a grouped branch, recursively."""
        if node.sql() in key_texts or isinstance(node, exp.ArrayAgg):
            return
        if isinstance(node, exp.Column) and not isinstance(node.this, exp.Star):
            table_node = node.args.get("table")
            binding = (
                env.bindings.get(_fold(table_node)) if table_node is not None else None
            )
            name = _fold(node.this)
            if isinstance(binding, _CteBinding) and self._varies_per_row(binding, name):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"'{binding.name}.{name}' is neither aggregated nor a GROUP "
                    "BY key",
                    node,
                    fallback=select,
                    hint=_GROUPED_CTE_HINT,
                )
            return
        for value in node.args.values():
            items = value if isinstance(value, list) else [value]
            for item in items:
                if isinstance(item, exp.Expr):
                    self._check_grouped_cte_expr(item, env, select, key_texts)

    def _varies_per_row(self, binding: _CteBinding, name: str) -> bool:
        """True when a CTE column carries a stream per body row, and there is
        more than one of them -- the shape that differs tuple by tuple."""
        if binding.rows <= 1:
            return False
        column = self._cte_column(binding, name)
        return column is not None and column.splat and column.value.is_array

    def _branch_value(
        self, projection: exp.Expr, env: _Env, select: exp.Select
    ) -> _Value:
        """One SELECT column's streams, group by group where that matters.

        A grouped branch gathers each group in turn: an aggregate sees its
        whole group, every other column the group's first tuple -- which is
        what makes ``SELECT vid, array_agg(aud) ... GROUP BY vid`` map the
        video once and every audio of its group after it.
        With no partitioning key there is a single group, and the same split
        holds inside it: a group-constant column is mapped ONCE however many
        tuples the relation carries (:meth:`_lower_grouped_table_branch` reads
        it the same way, which is what keeps a table preview and its COPY
        agreeing). Under a fan-out ``TO`` the pin already cut the relation to
        one group. An ungrouped branch lowers over the relation as it stands.
        """
        if not env.grouped:
            return self._lower_expr(projection, env, select)
        relation = env.relation
        if relation is None:  # a query with no rows has nothing to partition
            return self._lower_expr(projection, env, select)
        groups = self._grouped_partitions(env, select)
        if not groups:
            # No row survived: lower the column as it stands, which is where
            # the empty-row-set rejection lives.
            return self._lower_expr(projection, env, select)
        aggregate = isinstance(_unwrap(projection), exp.ArrayAgg)
        original = relation.tuples
        gathered: list[_Stream] = []
        stream_type: StreamType = "video"  # every pass overwrites it
        try:
            for group in groups:
                relation.tuples = list(group) if aggregate else group[:1]
                value = self._lower_expr(projection, env, select)
                gathered += value.streams
                stream_type = value.type
        finally:
            relation.tuples = original
        return _array(stream_type, gathered)

    def _is_splat_projection(self, projection: exp.Expr, env: _Env) -> bool:
        """True when this stream column's array value (if it turns out to be
        one) is a row set rather than a single broadcast unit.

        Computed here (at the projection's OWN scope, CTE body or bare SELECT)
        because that is the only place its AST shape is still visible -- an
        outer table query sees just ``<cte>.<name>`` and has to trust what got
        recorded.

        A column is a row set exactly when it READS one: a row alias's stream
        column, a call over one, or another CTE's row-set column (which it
        inherits). A bare input/source array (``f.audio``) and anything
        broadcast over one is a single row carrying an array VALUE, and an
        ``array_agg`` is one unit by definition.
        """
        expr = _unwrap(projection)
        if isinstance(expr, exp.ArrayAgg):
            return False
        return self._reads_row_set(expr, env)

    def _reads_row_set(self, node: exp.Expr, env: _Env) -> bool:
        """True when `node` reads a row alias's column, or a CTE column that
        is itself a row set."""
        for sub in node.walk():
            if not isinstance(sub, exp.Column):
                continue
            table_node = sub.args.get("table")
            if table_node is None:
                continue
            binding = env.bindings.get(_fold(table_node))
            if isinstance(binding, _RowBinding):
                return True
            if isinstance(binding, _CteBinding):
                column = self._cte_column(binding, _fold(sub.this))
                if column is not None and column.splat and column.value.is_array:
                    return True
        return False

    # -- metadata tag columns ---------------------------------------------

    def _harvest_cte_tags(self, body: exp.Expr) -> None:
        """Move the tags one CTE body just recorded into the carry-over dict.

        ``_lower_query`` clears ``self.tags`` at entry, so a CTE's tags would be
        gone by the time a sink's ``_outputs`` reads them. The clearing itself
        is right -- two COPYs may tag one track differently -- so what the CTE
        recorded moves somewhere that outlives the reset instead.

        The CTE bodies of one script all pour into the SAME dict, though, so
        unlike two COPYs they cannot disagree: whatever any of them says about a
        track is what every sink reading that track sees.
        """
        for source_id, overrides in self.tags.items():
            carried = self.cte_tags.setdefault(source_id, {})
            for key, value in overrides.items():
                if key in carried and carried[key] != value:
                    raise _error(
                        ErrorCode.UNSUPPORTED_SQL,
                        f"tag '{key}' takes two different values on the same track",
                        body,
                        hint="two CTE bodies tag one track's '"
                        f"{key}' differently; give it a single value, or set it "
                        "in the outer SELECT, which overrides them both",
                    )
                carried[key] = value

    def _harvest_cte_dispositions(self, body: exp.Expr) -> None:
        """Move the dispositions one CTE body just recorded into the carry-over
        dict, exactly as `_harvest_cte_tags` does for its tags."""
        for source_id, flags in self.dispositions.items():
            carried = self.cte_dispositions.get(source_id)
            if carried is not None and carried != flags:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "the disposition takes two different values on the same track",
                    body,
                    hint="two CTE bodies flag one track differently; give it a "
                    "single value, or set it in the outer SELECT, which "
                    "overrides them both",
                )
            self.cte_dispositions[source_id] = flags

    def _layered_dispositions(self) -> _DispositionOverrides:
        """The CTE bodies' dispositions with this sink's laid over them."""
        return {**self.cte_dispositions, **self.dispositions}

    def _layered_tags(self) -> _TagOverrides:
        """The CTE bodies' tags with this sink's laid over them, per track.

        Two scopes, written inner to outer, so on a key both set the sink wins.
        That is layering, not the disagreement ``_record_tag`` rejects: that
        check stays inside one query.
        """
        merged: _TagOverrides = {
            source_id: dict(overrides) for source_id, overrides in self.cte_tags.items()
        }
        for source_id, overrides in self.tags.items():
            merged.setdefault(source_id, {}).update(overrides)
        return merged

    def _collect_container_tag(
        self, projection: exp.Expr, env: _Env, select: exp.Select
    ) -> None:
        """One tag column in a branch with no per-row scope: a CONTAINER tag.

        The alias is the key, free-form, and the value is evaluated once. NULL
        CLEARS the key rather than leaving it alone: ffmpeg copies an input's
        global tags by default, so the clear has to be written out.

        A GROUPED branch evaluates it over the group's first tuple: resolve's
        grouping check proved the column is a GROUP BY key, so every tuple of
        the group reads the same value and the first one stands for all.
        """
        key = _projection_name(projection)
        if key is None:  # defensive: `_is_tag_column` checked
            raise _error(
                ErrorCode.UNSUPPORTED_SQL, "malformed tag column", projection,
                fallback=select,
            )
        value = self._eval_value(_unwrap(projection), env, _group_row(env), select)
        text = None if value is None else _tag_text(value)
        if key in self.container_tags and self.container_tags[key] != text:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"container tag '{key}' takes two different values",
                projection,
                fallback=select,
                hint="one value per key; a file has one set of container tags",
            )
        self.container_tags[key] = text

    def _check_settable_key(
        self, projection: exp.Expr, env: _Env, select: exp.Select
    ) -> None:
        """A tag column's alias names a tag KEY, never a read-only field.

        Construction by alias and a free-form tag key share one spelling, so
        the record's own READ-ONLY field names are the reserved set: `'eng' AS
        language` writes a tag entry, `'h264' AS codec` claims a probed fact
        and is rejected. A writable field (`disposition`) keeps its meaning --
        it is the assertion it looks like.
        """
        key = _projection_name(projection)
        if key is None:
            return
        if _has_track_rows(env) and not env.grouped:
            what = "track row"
            reserved = frozenset(
                name
                for binding in env.bindings.values()
                if isinstance(binding, _RowBinding)
                for name in binding.readonly
            )
        else:
            what = "container"
            reserved = CONTAINER_READONLY_FIELDS
        if key not in reserved:
            return
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"'{key}' is a probed field of the {what}, not something a query "
            "can set",
            projection,
            fallback=select,
            hint=f"the file reports {key}; a tag column's alias is a free-form "
            "key, e.g. 'eng' AS language",
        )

    def _collect_tag(
        self, projection: exp.Expr, env: _Env, select: exp.Select
    ) -> None:
        """One tag column, evaluated once per result row.

        ROW-SCOPED: the value a result row computes is written onto every track
        that row carries, so a row holding a video and an audio track tags both,
        and a joined row can tag one side's track with the other side's column.
        """
        key = _projection_name(projection)
        relation = env.relation
        if key is None or relation is None:  # defensive: `_is_tag_column` checked
            raise _error(
                ErrorCode.UNSUPPORTED_SQL, "malformed tag column", projection,
                fallback=select,
            )
        value_node = _unwrap(projection)
        for row in relation.tuples:
            value = self._eval_value(value_node, env, row, select)
            text = None if value is None else _tag_text(value)
            for track in row.values():
                # A CTE row carries no track of its own: its streams were
                # tagged by the body that named them.
                if isinstance(track, _TrackRow):
                    self._record_tag(track.stream.source, key, text, projection, select)

    def _collect_disposition(
        self, projection: exp.Expr, env: _Env, select: exp.Select, *, scope: _TagScope
    ) -> None:
        """``... AS disposition``: ffmpeg's own flag spec, per result row.

        The value is the spec ffmpeg takes on the command line -- flag names
        joined by ``+``, or ``'0'`` for none -- and it is ABSOLUTE: it says what
        the output stream's whole flag map is, so every flag it does not name
        is off. NULL says the same as ``'0'``, the way a NULL tag clears its
        key. A container has no disposition, so a branch with no track row to
        flag is a rejection rather than a container write.
        """
        relation = env.relation
        if not _has_track_rows(env) or env.grouped or relation is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{DISPOSITION_COLUMN}' is a stream field, not a container one",
                projection,
                fallback=select,
                hint="a disposition rides on a track row, e.g. SELECT t, "
                f"'{DISPOSITION_KEYS[0]}' AS {DISPOSITION_COLUMN} FROM "
                "input('f.mkv') f, unnest(f.audio) t"
                if scope == "sink"
                else "flag the rows a CTE body selects, then gather them outside it",
            )
        value_node = _unwrap(projection)
        for row in relation.tuples:
            flags = self._flag_spec(
                self._eval_value(value_node, env, row, select), projection, select
            )
            for track in row.values():
                if isinstance(track, _TrackRow):
                    self._record_disposition(track.stream.source, flags, projection, select)

    def _flag_spec(
        self, value: RowValue, anchor: exp.Expr, select: exp.Select
    ) -> tuple[str, ...]:
        """One written disposition value as the flags it sets, in declared order.

        ``'default+forced'`` sets those two, ``'0'`` and NULL set none, and a
        name outside the closed set is a rejection naming the ones that are in
        it. Order is the type's, not the writer's, so one flag map has one
        spelling however it was typed.
        """
        if value is None:
            return ()
        if not isinstance(value, str):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{DISPOSITION_COLUMN}' takes ffmpeg's flag spec, not a number",
                anchor,
                fallback=select,
                hint=f"quote the flags, e.g. '{DISPOSITION_KEYS[0]}' or '0' to "
                "clear them",
            )
        if value == "0":
            return ()
        named = set()
        for part in value.split("+"):
            key = part.strip().lower()
            if not key or key.startswith(("+", "-")):
                # ffmpeg's own `+flag`/`-flag` adjusts what the source carries;
                # this column says what the whole map is, so there is nothing
                # for a relative spec to adjust.
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"'{value}' is not a flag list",
                    anchor,
                    fallback=select,
                    hint="name every flag the track should have, joined with "
                    f"'+', e.g. '{DISPOSITION_KEYS[0]}+{DISPOSITION_KEYS[6]}'; "
                    "'0' clears them all",
                )
            if key not in DISPOSITION_KEYS:
                raise flag_error(part.strip(), key, anchor, select)
            named.add(key)
        return tuple(key for key in DISPOSITION_KEYS if key in named)

    def _record_disposition(
        self,
        source: StreamMeta | None,
        flags: tuple[str, ...],
        anchor: exp.Expr,
        select: exp.Select,
    ) -> None:
        """Note one track's disposition; disagreement is a rejection.

        Keyed like `_record_tag`, by the identity of the probed StreamMeta, so
        the flags find their track through any chain of filters.
        """
        if source is None:
            return
        recorded = self.dispositions.get(id(source))
        if recorded is not None and recorded != flags:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "the disposition takes two different values on the same track",
                anchor,
                fallback=select,
                hint="a disposition is row-scoped, so a track selected by "
                "several result rows must get the same flags in each",
            )
        self.dispositions[id(source)] = flags

    def _record_tag(
        self,
        source: StreamMeta | None,
        key: str,
        value: str | None,
        anchor: exp.Expr,
        select: exp.Select,
    ) -> None:
        """Note one track's override for one key; disagreement is a rejection.

        Keyed by the identity of the probed :class:`StreamMeta`, which is the
        same thing :func:`_provenance` reads off an output stream — so an
        override finds its track through any chain of filters that threads
        provenance, not just a passthrough. The probes hold every StreamMeta for
        the whole lowering, so the ids stay valid.
        """
        if source is None:
            return
        overrides = self.tags.setdefault(id(source), {})
        if key in overrides and overrides[key] != value:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"tag '{key}' takes two different values on the same track",
                anchor,
                fallback=select,
                hint="a tag is row-scoped, so a track selected by several result "
                "rows must get the same value in each",
            )
        overrides[key] = value

    # -- SELECT * / <alias>.* ------------------------------------

    def _expand_star(
        self, qualifier: str, anchor: exp.Expr, env: _Env, select: exp.Select
    ) -> list[_Column]:
        """Every stream a star stands for, as passthrough columns.

        A bare ``*`` takes every alias of the FROM clause in FROM order
        (``_Env.bindings`` is insertion-ordered and built by `_scope` in exactly
        that order); ``<alias>.*`` takes one. Within an alias: the container's
        stream array columns in v/a/s/d order for an input, COLUMN order for a
        CTE, with array columns splatting.

        The WHERE window of each alias still applies: for an input alias it is
        already on the ``-i`` (so ``SELECT *`` under a WHERE seeks every stream
        of the file, captions included), for a CTE it is the filter trim
        `_access` splices — which is also where a trimmed CTE caption column is
        rejected.
        """
        columns: list[_Column] = []
        for binding in self._star_bindings(qualifier, anchor, env, select):
            if isinstance(binding, _RowBinding):
                raise _row_star_error(binding, anchor, select)
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

    def _star_probe(self, alias: str, anchor: exp.Expr, select: exp.Select) -> ProbeResult:
        """The probe a star over an input alias needs, or INPUT_NOT_FOUND.

        Splat tier, same policy as a bare ``a.audio``: how many streams a
        file has, and of which types, is a property of the file, so an input
        that could not be probed is a rejection rather than a guess.
        """
        result = self.probes.get(alias)
        if result is None:
            path = self.res.input_paths[self.graph.sources[alias]]
            raise _error(
                ErrorCode.INPUT_NOT_FOUND,
                f"cannot expand '*' over '{path}': file not found or unreadable",
                anchor,
                fallback=select,
                hint="'*' is every stream of the input, and only a readable input "
                f"can list them; name the streams instead, e.g. {alias}.video[1]",
            )
        return result

    def _star_input(
        self, alias: str, anchor: exp.Expr, env: _Env, select: exp.Select
    ) -> list[_Column]:
        """Every stream of one input alias: the stream arrays, in v/a/s/d order.

        The container's array columns are what a star stands for, and a media
        SELECT column is an output stream, so the four stream arrays expand and
        `chapters` does not -- a chapter is not a stream, and ffmpeg's own
        default already carries an input's chapters through a remux.
        """
        result = self._star_probe(alias, anchor, select)
        path = self.res.input_paths[self.graph.sources[alias]]
        streams = [
            meta
            for column in _STREAM_STAR_COLUMNS
            for meta in result.by_type(_ARRAY_COLUMNS[column])
        ]
        if not streams:
            raise _error(
                ErrorCode.STREAM_NOT_FOUND,
                f"'*' over '{path}' selects nothing: it has no video, audio, "
                "subtitle or data streams",
                anchor,
                fallback=select,
                hint="an empty expansion would select nothing; drop the star",
            )
        for meta in streams:
            self._reject_codecless(
                meta,
                f"'{alias}.*' includes '{alias}.{meta.type}[{meta.index + 1}]', which",
                anchor,
                select,
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
            for meta in streams
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
            for stream in self._cte_column_value(
                binding, column, anchor, select
            ).streams
        ]

    def _star_bindings(
        self, qualifier: str, anchor: exp.Expr, env: _Env, select: exp.Select
    ) -> list[_Binding]:
        """What a star stands for: one named alias, or every FROM alias."""
        if not qualifier:
            return list(env.bindings.values())
        binding = env.bindings.get(qualifier)
        if binding is None:
            raise _error(
                ErrorCode.UNKNOWN_ALIAS,
                f"unknown alias '{qualifier}'",
                anchor,
                fallback=select,
                hint=self._known_hint(),
            )
        return [binding]

    def _star_names(
        self, qualifier: str, anchor: exp.Expr, env: _Env, select: exp.Select
    ) -> list[str]:
        """A table star's column headers. Static: no probe is consulted.

        A container names its array columns, a row table its record's scalar
        fields, a CTE the columns its body named, and a generated source the
        one array column its output type fills. `_star_cells` walks the very
        same lists in the same order.
        """
        names: list[str] = []
        for binding in self._star_bindings(qualifier, anchor, env, select):
            if isinstance(binding, _RowBinding):
                names += binding.star
            elif isinstance(binding, _InputBinding):
                names += STAR_COLUMNS
            elif isinstance(binding, _SourceBinding):
                names.append(binding.output)
            else:
                names += [column.name or "column" for column in binding.columns]
        return names

    def _star_cells(
        self,
        qualifier: str,
        anchor: exp.Expr,
        env: _Env,
        select: exp.Select,
        cardinality: int,
    ) -> list[list[CellValue]]:
        """A table star's columns, each already one cell per printed row."""
        columns: list[list[CellValue]] = []
        for binding in self._star_bindings(qualifier, anchor, env, select):
            if isinstance(binding, _RowBinding):
                columns += [
                    self._row_metadata_cells(binding, name, anchor, select)
                    for name in binding.star
                ]
            elif isinstance(binding, _InputBinding):
                columns += [
                    self._input_array_cells(
                        binding.alias, name, anchor, env, select, cardinality
                    )
                    for name in STAR_COLUMNS
                ]
            elif isinstance(binding, _SourceBinding):
                cell = ArrayCell(
                    elements=(self._stream_to_cell(self._source_stream_of(binding)),)
                )
                columns.append([cell] * cardinality)
            else:
                columns += [
                    self._value_to_cells(
                        self._access(
                            env,
                            binding.name,
                            self._cte_column_value(binding, column, anchor, select),
                            anchor,
                            select,
                        ),
                        cardinality,
                        splat=column.splat,
                    )
                    for column in binding.columns
                ]
        return columns

    def _input_array_cells(
        self,
        alias: str,
        column: str,
        anchor: exp.Expr,
        env: _Env,
        select: exp.Select,
        cardinality: int,
    ) -> list[CellValue]:
        """One container array column as ONE array cell, broadcast to each row.

        The same cell a bare ``f.audio`` / ``f.chapters`` prints on its own: an
        array column is a value inside the input's single row, not a row set.
        """
        if column == CHAPTERS_COLUMN:
            return self._chapters_cells(alias, anchor, select, cardinality)
        result = self._star_probe(alias, anchor, select)
        stream_type = _ARRAY_COLUMNS[column]
        streams = [
            self._source_stream(alias, stream_type, meta.index)
            for meta in result.by_type(stream_type)
        ]
        if streams:
            streams = list(
                self._access(env, alias, _array(stream_type, streams), anchor, select).streams
            )
        cell = ArrayCell(elements=tuple(self._stream_to_cell(stream) for stream in streams))
        return [cell] * cardinality

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
        for item, join in from_entries(select):
            if isinstance(item, exp.Unnest):
                self._add_track_rows(item, join, env, select)
            else:
                self._add_table(item, env, select)
        return env

    # -- FROM unnest(<input>.<type>) alias -------------

    def _add_track_rows(
        self,
        unnest: exp.Unnest,
        join: RawRowJoin | None,
        env: _Env,
        select: exp.Select,
    ) -> None:
        """Bind one track-row table: every track of the array becomes a row.

        This is the one binding that MUST probe. A row's columns are probed
        metadata and its row COUNT is a property of the file, so an input that
        could not be read cannot be unnested at all -- the same policy, and the
        same code, a bare ``f.audio`` has: the streams of a file that cannot be
        read cannot be enumerated.

        No node is minted and no ``-i`` is taken: the rows' streams are the
        INPUT alias's streams, already probed and already mapped, so a row
        table is pure bookkeeping until ``t`` is actually selected. That
        is what makes the consume-once rule fall out of ordinary column
        selection -- an unmatched row's stream is simply never read.
        """
        alias_node = unnest.args.get("alias")
        alias = (
            _fold(alias_node.this)
            if isinstance(alias_node, exp.TableAlias) and alias_node.this is not None
            else ""
        )
        raw = self.res.track_rows.get(alias)
        if raw is None:  # defensive: resolve records every row alias
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "malformed unnest in FROM",
                unnest,
                fallback=select,
                hint="unnest one input's stream array, e.g. unnest(f.audio) t",
            )
        if raw.column == CHAPTERS_COLUMN:
            stream_type: StreamType = "data"  # filler: a chapter row has no track
            rows = self._chapter_rows(raw, unnest, select)
        else:
            stream_type = _ARRAY_COLUMNS[raw.column]
            result = self.probes.get(raw.source)
            if result is None:
                raise _error(
                    ErrorCode.INPUT_NOT_FOUND,
                    f"cannot unnest '{raw.source}.{raw.column}' of "
                    f"'{self._path_of(raw.source)}': file not found or unreadable",
                    unnest,
                    fallback=select,
                    hint=f"unnest lists the tracks of a file and reads their "
                    f"metadata, and only a readable input has either; subscript "
                    f"one stream instead, e.g. {raw.source}.{raw.column}[1]",
                )
            rows = [
                _TrackRow(
                    stream=self._source_stream(raw.source, stream_type, position),
                    columns=_row_columns(meta, raw.column),
                )
                for position, meta in enumerate(result.by_type(stream_type))
            ]
        if env.relation is None:
            env.relation = _RowRelation()
        env.bindings[alias] = _RowBinding(
            alias=alias,
            source=raw.source,
            column=raw.column,
            type=stream_type,
            relation=env.relation,
        )
        self._join_rows(env.relation, alias, rows, join, env, select)

    # -- joining two row tables ------------------------

    def _join_rows(
        self,
        relation: _RowRelation,
        alias: str,
        rows: Sequence[_TrackRow | _CteRow],
        join: RawRowJoin | None,
        env: _Env,
        select: exp.Select,
    ) -> None:
        """Fold one freshly bound row source into the branch's relation.

        Ordinary SQL join semantics, evaluated here because every column is
        probed metadata ("the joins never reach ffmpeg"):

        * the FIRST row table simply becomes the relation;
        * a comma between two row tables is the bounded CROSS join;
        * ``ON`` is 061's three-valued evaluator, and a pair is kept only when
          it comes back TRUE — so a NULL key matches nothing, without that
          being a rule of ours;
        * multiplicity is real: a left row matching two right rows pairs with
          BOTH (two result rows, hence two output streams). The fix, when that
          is not wanted, is a wider key, not an error;
        * LEFT keeps an unmatched left row with a NULL right side, FULL also
          appends the unmatched RIGHT rows, in their own order, after every
          left row -- which is the whole of the row-order rule.
        """
        kind = join.kind if join is not None else "cross"
        if not relation.aliases:
            relation.aliases.append(alias)
            relation.tuples = [{alias: row} for row in rows]
            return
        if join is not None and join.on is not None:
            for key_alias, names in _join_keys(join.on).items():
                for name in names:
                    if name not in relation.keys.setdefault(key_alias, []):
                        relation.keys[key_alias].append(name)

        combined: list[_RowTuple] = []
        matched: set[int] = set()
        for left in relation.tuples:
            paired = False
            for position, row in enumerate(rows):
                candidate: _RowTuple = {**left, alias: row}
                if kind != "cross" and (
                    join is None
                    or join.on is None
                    or self._eval_row(join.on, env, candidate, select) is not True
                ):
                    continue
                combined.append(candidate)
                matched.add(position)
                paired = True
            if not paired and kind in ("left", "full"):
                combined.append({**left, alias: None})
        if kind == "full":
            empty: _RowTuple = {name: None for name in relation.aliases}
            combined += [
                {**empty, alias: row}
                for position, row in enumerate(rows)
                if position not in matched
            ]
        relation.aliases.append(alias)
        relation.tuples = combined

    def _path_of(self, alias: str) -> str:
        """The path behind an input alias, for a message about its file."""
        index = self.graph.sources.get(alias)
        if index is None or not 0 <= index < len(self.res.input_paths):
            return alias
        return self.res.input_paths[index]

    def _chapter_rows(
        self, raw: RawTrackRows, unnest: exp.Expr, select: exp.Select
    ) -> list[_TrackRow]:
        """The rows of ``unnest(<input>.chapters)``: one per probed chapter.

        The one array column whose elements are not streams, so every row
        carries `_STREAMLESS_ROW` in place of a track and only the
        `ROW_SCHEMAS["chapters"]` metadata columns are ever read.
        """
        result = self.probes.get(raw.source)
        if result is None:
            raise _error(
                ErrorCode.INPUT_NOT_FOUND,
                f"cannot read chapters of '{self._path_of(raw.source)}': file "
                "not found or unreadable",
                unnest,
                fallback=select,
                hint=f"unnest({raw.source}.{CHAPTERS_COLUMN}) lists a file's "
                "chapters, and only a readable input has any",
            )
        return [
            _TrackRow(
                stream=_STREAMLESS_ROW,
                columns={
                    "index": chapter.index,
                    "title": chapter.title,
                    "start_t": chapter.start_t,
                    "end_t": chapter.end_t,
                },
            )
            for chapter in result.chapters
        ]

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
            # `FROM ffmpeg.<source>(...) alias`: resolve already
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
            values = self.res.values_ctes.get(name)
            if values is not None:
                local = name
                if isinstance(alias_node, exp.TableAlias) and alias_node.this is not None:
                    local = _fold(alias_node.this)
                self._add_values_rows(local, values, env, select)
                return
            columns = self.cte_columns.get(name)
            if columns is None:
                raise _error(
                    ErrorCode.UNKNOWN_ALIAS,
                    f"unknown table '{name}'",
                    inner,
                    fallback=table,
                    hint=self._known_hint(),
                )
            # `FROM master m` binds the view/CTE under a BRANCH-LOCAL name
            # (resolve checked it shadows nothing in the flat namespace). The
            # binding records the local name, so `m.v` resolves and messages
            # read back as written; the columns -- and therefore the graph
            # refs -- are the same objects either way, which is what makes the
            # shared subgraph shared.
            local = name
            if isinstance(alias_node, exp.TableAlias) and alias_node.this is not None:
                local = _fold(alias_node.this)
            self._add_cte_rows(local, columns, env, select)
            return
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            "only input('path') and CTE names are allowed in FROM",
            table,
            fallback=select,
        )

    def _add_values_rows(
        self, local: str, values: RawValuesTable, env: _Env, select: exp.Select
    ) -> None:
        """Bind one VALUES table: its written rows join the branch's relation.

        The same join :meth:`_add_track_rows` builds, with the rows read off
        the query instead of a probe -- so a comma between a VALUES table and
        anything else is the ordinary cross join, and ``array_agg`` over it
        aggregates the same way. No stream and no ``-i``: the rows are values.
        """
        if env.relation is None:
            env.relation = _RowRelation()
        rows = [
            _TrackRow(
                stream=_STREAMLESS_ROW,
                columns={
                    name: None
                    if isinstance(cell, exp.Null)
                    else self._literal_of(cell, select)
                    for name, cell in zip(values.columns, row, strict=True)
                },
            )
            for row in values.rows
        ]
        env.bindings[local] = _RowBinding(
            alias=local,
            source="",
            column=local,
            type="data",  # filler: a written row has no track
            relation=env.relation,
            values=values,
        )
        self._join_rows(env.relation, local, rows, None, env, select)

    def _add_cte_rows(
        self, local: str, columns: tuple[_Column, ...], env: _Env, select: exp.Select
    ) -> None:
        """Bind one CTE reference: its body's rows join the branch's relation.

        One body row is one outer row, so a comma between two CTEs (or between
        a CTE and an unnest table) is the ordinary cross join
        :meth:`_join_rows` already builds, multiplicity and all. A single-row
        body is a shape no-op, which is what keeps the one-input CTE shapes
        compiling exactly as they did.
        """
        if env.relation is None:
            env.relation = _RowRelation()
        rows = _cte_row_count(columns)
        env.bindings[local] = _CteBinding(
            name=local, columns=columns, rows=rows, relation=env.relation
        )
        self._join_rows(
            env.relation,
            local,
            [_CteRow(position=position) for position in range(rows)],
            None,
            env,
            select,
        )

    def _known_hint(self) -> str:
        known = sorted(
            set(self.cte_columns)
            | set(self.graph.sources)
            | set(self.res.source_filters)
            | set(self.res.track_rows)
        )
        return f"known names: {', '.join(known)}" if known else "no aliases are in scope"

    # -- FROM ffmpeg.<source>(...) ------------------

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
          so, the one excluded case that is positively identifiable;
        * there is no registry at all (no ffmpeg) — the standard
          unavailability wording, same as a namespaced CALL's;
        * the name is unknown to both tables — UNKNOWN_FUNCTION with a
          did-you-mean over ``source_names()``. Sources the v1 scope check
          excluded (``avsynctest``'s ``|->AV``, ``movie``/``amovie``'s
          ``|->N``) are NOT retained by the registry at all, so they are
          indistinguishable from a typo here and land on the same rejection —
          which is why its fallback hint states the exclusion explicitly rather
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
                f"{FILTER_NAMESPACE}.{raw.name}(a.video[1]) FROM input('clip.mp4') a",
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
            "installed ffmpeg; the provisioner failed to supply one"
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

    # -- WHERE, split into its three halves ------------

    def _split_where(
        self, select: exp.Select, env: _Env
    ) -> tuple[list[exp.Expr], list[exp.Expr], list[exp.Expr]]:
        """This branch's WHERE conjuncts, as ``(time windows, row predicates,
        subscript metadata assertions)``.

        A conjunct is a ROW predicate exactly when it mentions a track-row
        alias, which is unambiguous: a row alias is an alias, and one name
        cannot be two things. A subscript metadata accessor (``Dot`` over
        ``Bracket``) is told apart by SHAPE instead, since its alias
        is an ordinary input one -- checked first, so a conjunct never falls
        through to the row/time split. Resolve rejected every mixed case, so
        nothing here has to decide what a half-and-half conjunct would mean.
        """
        where = select.args.get("where")
        if not isinstance(where, exp.Where):
            return [], [], []
        time_conjuncts: list[exp.Expr] = []
        row_conjuncts: list[exp.Expr] = []
        assertion_conjuncts: list[exp.Expr] = []
        for conjunct in _flatten_and(where.this):
            if any(
                isinstance(sub, exp.Dot) and subscript_metadata_shape(sub) is not None
                for sub in conjunct.walk()
            ):
                assertion_conjuncts.append(conjunct)
                continue
            aliases = {
                _fold(sub.args["table"])
                for sub in conjunct.walk()
                if isinstance(sub, exp.Column) and sub.args.get("table") is not None
            }
            rows = {
                alias
                for alias in aliases
                if isinstance(env.bindings.get(alias), _RowBinding)
            }
            if not rows:
                time_conjuncts.append(conjunct)
                continue
            if aliases - rows and len(rows) == 1 and self._is_row_window(conjunct, env):
                # A time window whose BOUNDS are row columns: one seek per row,
                # so it needs the row a fan-out TO pins.
                if self.fanout_expr is None:
                    raise _error(
                        ErrorCode.UNSUPPORTED_SQL,
                        "a trim bound may reference track-row columns only under "
                        "a fan-out TO",
                        conjunct,
                        fallback=where,
                        hint="write TO ('ch' || c.index::text || '.mkv') to get "
                        "one command per row, each with its own window",
                    )
                time_conjuncts.append(conjunct)
                continue
            if aliases - rows or len(rows) > 1:  # defensive: resolve rejected both
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "a WHERE predicate may reference only one track-row table",
                    conjunct,
                    fallback=where,
                    hint="filter each unnest separately",
                )
            row_conjuncts.append(conjunct)
        return time_conjuncts, row_conjuncts, assertion_conjuncts

    def _is_row_window(self, conjunct: exp.Expr, env: _Env) -> bool:
        """True for a time window on a non-row alias bounded by row columns."""
        parsed = _time_bounds(conjunct)
        if parsed is None:
            return False
        table_node = parsed[0].args.get("table")
        if table_node is None or _fold(parsed[0].this) != TIME_COLUMN:
            return False
        return not isinstance(env.bindings.get(_fold(table_node)), _RowBinding)

    # -- compile-time row filtering / ordering -------------------

    def _filter_rows(
        self, conjuncts: list[exp.Expr], env: _Env, select: exp.Select
    ) -> None:
        """Keep the rows whose predicate is TRUE; drop UNKNOWN and FALSE alike.

        Standard SQL: WHERE admits TRUE only, so a row whose metadata field was
        never probed simply does not match — no new rule, and no silent guess.
        The surviving set is written back onto the branch's relation, so every
        later ``t`` sees it and an unselected row's stream is never
        touched. Filtering happens AFTER the joins, which is where
        SQL puts it: dropping a row of an outer join's nullable side before the
        join would silently turn it into an inner one.
        """
        for conjunct in conjuncts:
            relation = self._row_binding_of(conjunct, env, select).relation
            relation.tuples = [
                row
                for row in relation.tuples
                if self._eval_row(conjunct, env, row, select) is True
            ]

    def _pin_fanout_row(self, env: _Env, select: exp.Select) -> None:
        """Cut the branch's relation down to the ONE group this command writes.

        Ungrouped, a group is a single row and this is the per-row pin it has
        always been. Under a GROUP BY over row columns the relation partitions
        into one group per distinct key, and the pinned group keeps ALL its
        tuples: everything downstream then works unchanged, since ``t``
        over the surviving tuples is exactly the array ``array_agg`` asked for,
        and the trim bounds and the path expression read `fanout_row` -- the
        group's first tuple, which stands for the whole group because the key
        is what every tuple in it agrees on.

        `fanout_count` is recorded so :func:`lower_commands` knows how many
        more runs to make.
        """
        if self.fanout_expr is None or env.relation is None:
            return
        groups = self._fanout_groups(env, select)
        if not groups:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "a TO expression writes one file per row, and no row survives "
                "the WHERE clause",
                self.fanout_expr,
                fallback=select,
                hint="loosen the filter, or write a quoted TO path",
            )
        if not 0 <= self.fanout_index < len(groups):
            raise _error(
                ErrorCode.INTERNAL,
                f"fan-out index {self.fanout_index} is outside the "
                f"{len(groups)} files this query writes",
                fallback=select,
                hint="please report this query as a bug",
            )
        group = groups[self.fanout_index]
        self.fanout_count = len(groups)
        self.fanout_grouped = bool(env.group_keys)
        self.fanout_row = group[0]
        self.fanout_env = env
        env.relation.tuples = list(group)

    def _fanout_groups(
        self, env: _Env, select: exp.Select
    ) -> list[list[_RowTuple]]:
        """The relation's tuples partitioned into the files they write.

        One group per distinct GROUP BY key, in FIRST-APPEARANCE order (the
        dict's own insertion order), so the command sequence follows the row
        order the query built. With no row-level key every tuple is its own
        group, which is the ungrouped fan-out unchanged.
        """
        relation = env.relation
        tuples = relation.tuples if relation is not None else []
        if not env.group_keys:
            return [[row] for row in tuples]
        groups: dict[tuple[RowValue, ...], list[_RowTuple]] = {}
        for row in tuples:
            key = tuple(self._key_value(node, env, row, select) for node in env.group_keys)
            groups.setdefault(key, []).append(row)
        return list(groups.values())

    def _key_value(
        self, node: exp.Expr, env: _Env, row: _RowTuple, select: exp.Select
    ) -> RowValue:
        """One GROUP BY key, read out of one result tuple.

        A stream column -- a CTE's, or a row table's ``track`` -- has no
        metadata value to compare, so what identifies the group is the stream
        itself: its ref, which two tuples share exactly when they carry the
        same stream.
        """
        stream = self._key_stream(node, env, row)
        if stream is not None:
            return stream.ref
        return self._eval_value(node, env, row, select)

    def _key_stream(self, node: exp.Expr, env: _Env, row: _RowTuple) -> _Stream | None:
        """The stream a GROUP BY key names in this tuple, else None."""
        column_node = _unwrap(node)
        if not isinstance(column_node, exp.Column):
            return None
        table_node = column_node.args.get("table")
        if table_node is None:
            return None
        binding = env.bindings.get(_fold(table_node))
        name = _fold(column_node.this)
        if isinstance(binding, _RowBinding):
            if name != ROW_STREAM:
                return None
            track = _track_of(row, binding.alias)
            return track.stream if track is not None else None
        if not isinstance(binding, _CteBinding):
            return None
        column = self._cte_column(binding, name)
        if column is None or not column.value.streams:
            return None
        entry = row.get(binding.name)
        if (
            isinstance(entry, _CteRow)
            and column.splat
            and len(column.value.streams) == binding.rows
        ):
            return column.value.streams[entry.position]
        # A broadcast column is one unit: every tuple reads the same stream.
        return column.value.streams[0]

    def _row_binding_of(
        self, node: exp.Expr, env: _Env, select: exp.Select
    ) -> _RowBinding:
        """The single row table `node`'s columns belong to (checked upstream)."""
        for sub in node.walk():
            if not isinstance(sub, exp.Column):
                continue
            table_node = sub.args.get("table")
            if table_node is None:
                continue
            binding = env.bindings.get(_fold(table_node))
            if isinstance(binding, _RowBinding):
                return binding
        raise _error(  # defensive: the caller only passes row expressions
            ErrorCode.UNSUPPORTED_SQL,
            "unsupported track-row expression",
            node,
            fallback=select,
        )

    def _eval_row(
        self,
        node: exp.Expr,
        env: _Env,
        rows: _RowTuple,
        select: exp.Select,
    ) -> bool | None:
        """One predicate against one result row: TRUE, FALSE, UNKNOWN (``None``).

        `rows` maps every row alias in scope to that result row's track, or to
        None where an outer join left a gap — one evaluator for WHERE (which
        sees a single alias) and for a JOIN's ON (which sees both sides), plan
        062 generalizing 061's single binding.

        Kleene three-valued logic, which is what makes the NULL story a
        non-story: a comparison with a NULL operand is UNKNOWN, UNKNOWN
        propagates through AND/OR/NOT the SQL way, and both callers keep TRUE
        only. A gap row reads NULL in every column, so "NULL matches nothing"
        covers the gaps too, for free.
        """
        node = _unwrap(node)
        if isinstance(node, exp.And | exp.Or):
            left = self._eval_row(node.this, env, rows, select)
            expression = node.args.get("expression")
            if not isinstance(expression, exp.Expr):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL, "malformed row predicate", node,
                    fallback=select,
                )
            right = self._eval_row(expression, env, rows, select)
            return (
                _kleene_and(left, right)
                if isinstance(node, exp.And)
                else _kleene_or(left, right)
            )
        if isinstance(node, exp.Not) and isinstance(node.this, exp.Expr):
            inner = self._eval_row(node.this, env, rows, select)
            return None if inner is None else not inner
        if isinstance(node, exp.Is):
            value = self._row_value_of(node.this, env, rows, select)
            is_null = value is None
            return not is_null if node.args.get("negate") else is_null
        if isinstance(node, exp.Between):
            value = self._eval_value(node.this, env, rows, select)
            low = self._eval_value(node.args.get("low"), env, rows, select)
            high = self._eval_value(node.args.get("high"), env, rows, select)
            return _kleene_and(
                _compare(exp.GTE(), value, low), _compare(exp.LTE(), value, high)
            )
        if isinstance(node, exp.EQ | exp.NEQ | exp.GT | exp.GTE | exp.LT | exp.LTE):
            # Both sides go through one value evaluator, so the operands stay in
            # written order and `'eng' = t.tags.language` needs no mirroring.
            return _compare(
                node,
                self._eval_value(node.this, env, rows, select),
                self._eval_value(node.args.get("expression"), env, rows, select),
            )
        if isinstance(node, exp.Boolean | exp.Column):
            # A boolean value IS the condition, as it is in Postgres; resolve
            # already turned away a column of any other type.
            value = self._eval_value(node, env, rows, select)
            return None if value is None else bool(value)
        raise _error(  # defensive: resolve accepted only the shapes above
            ErrorCode.UNSUPPORTED_SQL,
            "unsupported row predicate",
            node,
            fallback=select,
        )

    def _row_value_of(
        self,
        node: exp.Expr | None,
        env: _Env,
        rows: _RowTuple,
        select: exp.Select,
    ) -> RowValue:
        """One ``<row alias>.<column>`` reference, read out of this result row.

        A gap (the alias maps to None, because an outer join found no
        counterpart) reads NULL in every column — the one thing an absent row
        can honestly say about itself.

        ``<input alias>.duration`` and the container tags come from no row at
        all: they are probed off the input itself.
        """
        column = _unwrap(node) if isinstance(node, exp.Expr) else None
        if not isinstance(column, exp.Column):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "a track-row predicate compares a row column against a literal "
                "or another row column",
                column,
                fallback=select,
            )
        table_node = column.args.get("table")
        binding = env.bindings.get(_fold(table_node)) if table_node is not None else None
        if isinstance(binding, _InputBinding):
            name = _fold(column.this)
            if name == INPUT_DURATION_COLUMN:
                return self._input_duration(binding.alias, column, select)
            key = tag_key(name)
            if key is not None:
                return self._input_tag(binding.alias, key, column, select)
        if not isinstance(binding, _RowBinding):  # defensive: resolve checked it
            raise _error(
                ErrorCode.UNKNOWN_ALIAS,
                f"unknown track-row alias '{_fold(table_node)}'",
                column,
                fallback=select,
                hint=self._known_hint(),
            )
        name = _fold(column.this)
        if name in MAP_COLUMNS:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{binding.alias}.{name}' is the whole {map_noun(name)} map, "
                "not a single value",
                column,
                fallback=select,
                hint=f"name the key: '{binding.alias}.{name}.{map_example(name)}'",
            )
        if name not in binding.schema and map_ref(name) is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"unknown column '{binding.alias}.{column.name}'",
                column,
                fallback=select,
                hint=binding.exposes,
            )
        row = _track_of(rows, binding.alias)
        return None if row is None else row.columns.get(name)

    def _eval_value(
        self,
        node: exp.Expr | None,
        env: _Env,
        rows: _RowTuple,
        select: exp.Select,
    ) -> RowValue:
        """One compile-time value over a result row.

        The whole value grammar: a literal, NULL, a row's metadata column, an
        input's probed ``duration``, ``CASE``, ``||``, arithmetic and
        ``::text``. Shared by the predicate evaluator (a comparison's operands,
        a BETWEEN bound), by tag columns, by trim bounds and by computed call
        arguments, so every one of them speaks the same language.
        """
        value = _unwrap(node) if isinstance(node, exp.Expr) else None
        if isinstance(value, exp.Null):
            return None
        if isinstance(value, exp.Boolean):
            return bool(value.this)
        if isinstance(value, exp.Column):
            return self._row_value_of(value, env, rows, select)
        if isinstance(value, exp.Case):
            return self._eval_case(value, env, rows, select)
        if isinstance(value, exp.DPipe):
            return self._eval_concat(value, env, rows, select)
        if isinstance(value, _ARITHMETIC):
            return self._eval_arithmetic(value, env, rows, select)
        if isinstance(value, exp.Cast):
            return self._eval_cast(value, env, rows, select)
        if isinstance(value, exp.Neg) and not isinstance(_unwrap(value.this), exp.Literal):
            operand = self._eval_number(value.this, "'-'", value, env, rows, select)
            return None if operand is None else -operand
        return self._literal_of(value, select)

    def _eval_arithmetic(
        self,
        node: exp.Expr,
        env: _Env,
        rows: _RowTuple,
        select: exp.Select,
    ) -> RowValue:
        """``+ - * /`` with Postgres' own typing, at compile time.

        int op int stays an int and ``/`` TRUNCATES toward zero, any float
        operand makes the result a float, and NULL on either side propagates.
        Dividing by a zero is a typed rejection: the value is knowable here, so
        shipping an ffmpeg command built on it is not an option.
        """
        operator = _ARITHMETIC_NAMES[type(node)]
        left = self._eval_number(node.this, operator, node, env, rows, select)
        right = self._eval_number(node.args.get("expression"), operator, node, env, rows, select)
        if left is None or right is None:
            return None
        if isinstance(node, exp.Add):
            return left + right
        if isinstance(node, exp.Sub):
            return left - right
        if isinstance(node, exp.Mul):
            return left * right
        if right == 0:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "division by zero",
                node,
                fallback=select,
                hint="the divisor is known at compile time, and it is zero",
            )
        if isinstance(left, int) and isinstance(right, int):
            quotient = abs(left) // abs(right)
            return -quotient if (left < 0) != (right < 0) else quotient
        return left / right

    def _eval_number(
        self,
        node: exp.Expr | None,
        operator: str,
        anchor: exp.Expr,
        env: _Env,
        rows: _RowTuple,
        select: exp.Select,
    ) -> int | float | None:
        """One arithmetic operand's value; text is a typed rejection."""
        value = self._eval_value(node, env, rows, select)
        if value is None or isinstance(value, int | float):
            return value
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"{operator} needs numbers, but one side is text",
            node if isinstance(node, exp.Expr) else anchor,
            fallback=select,
        )

    def _eval_cast(
        self,
        node: exp.Cast,
        env: _Env,
        rows: _RowTuple,
        select: exp.Select,
    ) -> RowValue:
        """``x::text``: the number spelled out, NULL left NULL.

        One spelling rule, shared with the filtergraph and the seek times --
        an int prints without a point, a float in python's shortest form that
        reads back as the same float.
        """
        value = self._eval_value(node.this, env, rows, select)
        return None if value is None else _tag_text(value)

    def _eval_case(
        self,
        node: exp.Case,
        env: _Env,
        rows: _RowTuple,
        select: exp.Select,
    ) -> RowValue:
        """CASE, searched and simple: the first TRUE branch, else ELSE, else NULL.

        A searched branch's condition is an ordinary row predicate, so its
        three-valued logic carries straight over: only TRUE takes a branch, and
        UNKNOWN falls through exactly as FALSE does. The simple form compares
        the operand with ``=``, which makes a NULL operand match no WHEN — SQL's
        rule, and the same 3VL again.
        """
        operand_node = node.this if isinstance(node.this, exp.Expr) else None
        operand = (
            self._eval_value(operand_node, env, rows, select)
            if operand_node is not None
            else None
        )
        for branch in node.args.get("ifs") or []:
            if not isinstance(branch, exp.If) or not isinstance(branch.this, exp.Expr):
                raise _error(  # defensive: resolve checked the shape
                    ErrorCode.UNSUPPORTED_SQL, "malformed CASE", node, fallback=select
                )
            matched = (
                self._eval_row(branch.this, env, rows, select)
                if operand_node is None
                else _compare(
                    exp.EQ(),
                    operand,
                    self._eval_value(branch.this, env, rows, select),
                )
            )
            if matched is True:
                return self._eval_value(branch.args.get("true"), env, rows, select)
        default = node.args.get("default")
        if not isinstance(default, exp.Expr):
            return None
        return self._eval_value(default, env, rows, select)

    def _eval_concat(
        self,
        node: exp.DPipe,
        env: _Env,
        rows: _RowTuple,
        select: exp.Select,
    ) -> RowValue:
        """``a || b``: NULL when either side is NULL, else the two texts joined."""
        left = self._eval_value(node.this, env, rows, select)
        right = self._eval_value(node.args.get("expression"), env, rows, select)
        if left is None or right is None:
            return None
        return f"{left}{right}"

    def _literal_of(self, node: exp.Expr | None, select: exp.Select) -> RowValue:
        """A row predicate's literal operand as a python scalar."""
        value = _unwrap(node) if isinstance(node, exp.Expr) else None
        if isinstance(value, exp.Neg) and isinstance(value.this, exp.Expr):
            return -_number(_unwrap(value.this), ErrorCode.UNSUPPORTED_SQL)
        if isinstance(value, exp.Literal):
            if value.is_string:
                return str(value.this)
            return _number(value, ErrorCode.UNSUPPORTED_SQL)
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            "a track-row predicate compares a row column against a literal",
            value,
            fallback=select,
        )

    # -- subscript metadata WHERE assertions --
    #
    # `<alias>.<type>[k].<column>` names ONE probed track deterministically
    # (the subscript is bounds-checked, not filtered), so a WHERE conjunct over
    # it has nothing to DROP the way a row predicate drops rows. It is an
    # ASSERTION, checked once at compile time against the probed file: TRUE
    # proceeds unchanged, FALSE or UNKNOWN (3VL -- a field that was never
    # probed) is a typed rejection, because an ffmpeg command line cannot
    # encode "select nothing" (recipe 29 of docs/examples.md).
    #
    # The boolean algebra is the row evaluator's, reused wholesale; the only
    # new piece is where a leaf's VALUE comes from (`_accessor_value`, probed
    # off the input through the same `_row_columns` a track-row table uses).

    def _check_assertions(self, conjuncts: list[exp.Expr], select: exp.Select) -> None:
        for conjunct in conjuncts:
            if self._eval_assertion(conjunct, select) is not True:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    "WHERE assertion failed at compile time: "
                    f"{conjunct.sql(dialect='postgres')}",
                    conjunct,
                    fallback=select,
                    hint="a subscript metadata predicate is checked once, "
                    "against the probed file, and a false or unprobed ('NULL') "
                    "result refuses to compile rather than silently shipping "
                    "the wrong track; fix the query or the input",
                )

    def _eval_assertion(self, node: exp.Expr, select: exp.Select) -> bool | None:
        """One subscript metadata predicate, Kleene three-valued, like `_eval_row`."""
        node = _unwrap(node)
        if isinstance(node, exp.And | exp.Or):
            left = self._eval_assertion(node.this, select)
            expression = node.args.get("expression")
            if not isinstance(expression, exp.Expr):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL, "malformed WHERE predicate", node,
                    fallback=select,
                )
            right = self._eval_assertion(expression, select)
            return (
                _kleene_and(left, right)
                if isinstance(node, exp.And)
                else _kleene_or(left, right)
            )
        if isinstance(node, exp.Not) and isinstance(node.this, exp.Expr):
            inner = self._eval_assertion(node.this, select)
            return None if inner is None else not inner
        if isinstance(node, exp.Is):
            value = self._accessor_value(node.this, select)
            is_null = value is None
            return not is_null if node.args.get("negate") else is_null
        if isinstance(node, exp.Between):
            value = self._accessor_value(node.this, select)
            low = self._literal_of(node.args.get("low"), select)
            high = self._literal_of(node.args.get("high"), select)
            return _kleene_and(
                _compare(exp.GTE(), value, low), _compare(exp.LTE(), value, high)
            )
        if isinstance(node, exp.EQ | exp.NEQ | exp.GT | exp.GTE | exp.LT | exp.LTE):
            left_node = node.this
            right_node = node.args.get("expression")
            left_shape = (
                subscript_metadata_shape(_unwrap(left_node))
                if isinstance(left_node, exp.Expr)
                else None
            )
            if left_shape is not None:
                return _compare(
                    node,
                    self._accessor_value(left_node, select),
                    self._literal_of(right_node, select),
                )
            mirrored = _MIRRORED_COMPARISONS[type(node)]()
            return _compare(
                mirrored,
                self._accessor_value(right_node, select),
                self._literal_of(left_node, select),
            )
        raise _error(
            ErrorCode.UNSUPPORTED_SQL, "unsupported WHERE predicate", node,
            fallback=select,
        )

    def _input_duration(
        self, alias: str, anchor: exp.Expr, select: exp.Select
    ) -> int | float:
        """``<input>.duration``: the probed container length, in seconds.

        Probed-only, and a rejection when it is not there — an unreadable file
        has no length, and neither does a container that declares none, so
        there is nothing to guess an expression's value from.
        """
        result = self.probes.get(alias)
        duration = None if result is None else result.duration
        if duration is None:
            raise _error(
                ErrorCode.INPUT_NOT_FOUND,
                f"'{alias}.{INPUT_DURATION_COLUMN}' is unknown: "
                f"'{self._path_of(alias)}' reports no container duration",
                anchor,
                fallback=select,
                hint="the duration is probed from the file; only a readable "
                "input that declares one has it",
            )
        return duration

    def _input_tag(
        self, alias: str, key: str, anchor: exp.Expr, select: exp.Select
    ) -> str | None:
        """``<input>.<tag>``: one probed container tag, NULL when absent.

        An absent key is NULL — that is what lets a CASE fill it — but an input
        this compile could not probe is a rejection, the same rule
        ``duration`` follows: a file nobody read says nothing about its tags.
        """
        result = self.probes.get(alias)
        if result is None:
            raise _error(
                ErrorCode.INPUT_NOT_FOUND,
                f"'{alias}.{TAGS_COLUMN}.{key}' is unknown: "
                f"'{self._path_of(alias)}' could not be probed",
                anchor,
                fallback=select,
                hint="container tags are read from the file; only a readable "
                "input has them",
            )
        return result.tags.get(key)

    def _accessor_value(self, node: exp.Expr | None, select: exp.Select) -> RowValue:
        """The probed value one ``<alias>.<type>[k].<column>`` accessor names.

        Resolve already confined this shape to an ordinary INPUT alias (never
        a row or CTE one), so this reads the SAME probed ``StreamMeta`` a bare
        ``<alias>.<type>[k]`` would select, through the SAME `_row_columns` a
        track-row table's columns come from -- one metadata table,
        two ways to name a row of it.
        """
        shape = (
            subscript_metadata_shape(_unwrap(node)) if isinstance(node, exp.Expr) else None
        )
        if shape is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "a subscript metadata predicate compares an accessor against "
                "a literal",
                node if isinstance(node, exp.Expr) else None,
                fallback=select,
            )
        bracket, name = shape
        inner = bracket.this
        if not isinstance(inner, exp.Column):  # defensive: resolve checked the shape
            raise _error(
                ErrorCode.UNSUPPORTED_SQL, "malformed subscript metadata accessor",
                bracket, fallback=select,
            )
        table_node = inner.args.get("table")
        alias = _fold(table_node) if table_node is not None else ""
        array_column = _fold(inner.this)
        stream_type = _ARRAY_COLUMNS.get(array_column)
        if stream_type is None:  # defensive: resolve checked the array column
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{alias}.{array_column}' has no per-track metadata",
                inner,
                fallback=select,
            )
        index = subscript_index(bracket)
        if index is None:  # defensive: resolve checked the subscript
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "stream subscript must be a positive integer literal",
                bracket,
                fallback=select,
                hint=_SUBSCRIPT_HINT,
            )
        result = self.probes.get(alias)
        if result is None:
            path = self._path_of(alias)
            raise _error(
                ErrorCode.INPUT_NOT_FOUND,
                f"cannot check '{alias}.{array_column}[{index}].{name}' of "
                f"'{path}': file not found or unreadable",
                bracket,
                fallback=select,
                hint="subscript metadata is probed from the file, and only a "
                "readable input has any; the WHERE assertion cannot be checked",
            )
        streams = result.by_type(stream_type)
        if not 1 <= index <= len(streams):
            have = f"{len(streams)} {stream_type} stream" + ("" if len(streams) == 1 else "s")
            raise _error(
                ErrorCode.STREAM_NOT_FOUND,
                f"'{alias}.{array_column}[{index}]' does not exist: "
                f"'{self._path_of(alias)}' has {have}",
                bracket,
                fallback=select,
                hint=_SUBSCRIPT_HINT,
            )
        meta = streams[index - 1]
        columns = _row_columns(meta, array_column)
        return columns.get(name)

    def _order_rows(self, select: exp.Select, env: _Env) -> None:
        """Re-sort a row table explicitly -- the ORDER BY carve-out.

        Row order is deterministic WITHOUT this — it is the file's track order,
        which is player-visible surface nothing resorts implicitly — so an
        ORDER BY is the user saying otherwise, and it applies at compile time
        to the row list, never to frames.

        Multi-key sorting is done one key at a time from LAST to FIRST over
        python's stable sort, which is exactly SQL's key precedence. NULLs are
        partitioned out rather than sorted, because they have no order: their
        position is ``nulls_first``, which sqlglot fills in from the Postgres
        defaults (ASC -> NULLS LAST, DESC -> NULLS FIRST) whether or not the
        query spelled it.
        """
        order = select.args.get("order")
        if not isinstance(order, exp.Order):
            return
        for ordered in reversed(order.expressions):
            if not isinstance(ordered, exp.Ordered):
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL, "malformed ORDER BY", fallback=order
                )
            binding = self._row_binding_of(ordered, env, select)
            key = _unwrap(ordered.this)
            if not isinstance(key, exp.Column):  # defensive: resolve checked it
                raise _error(
                    ErrorCode.NO_STREAMING_EQUIVALENT,
                    "ORDER BY has no streaming equivalent",
                    ordered,
                    fallback=order,
                )
            name = _fold(key.this)
            relation = binding.relation

            def value_of(
                row: _RowTuple,
                alias: str = binding.alias,
                name: str = name,
            ) -> RowValue:
                track = _track_of(row, alias)
                return None if track is None else track.columns.get(name)

            nulls = [row for row in relation.tuples if value_of(row) is None]
            rest = [row for row in relation.tuples if value_of(row) is not None]
            rest.sort(
                key=lambda row: _sort_key(value_of(row)),
                reverse=bool(ordered.args.get("desc")),
            )
            relation.tuples = (
                nulls + rest if ordered.args.get("nulls_first") else rest + nulls
            )

    def _collect_trims(
        self, select: exp.Select, env: _Env, conjuncts: list[exp.Expr]
    ) -> None:
        """Record each aliased time range, on the input or on the branch.

        The binding decides where the window goes. An INPUT alias owns its own
        ``-i`` and is globally unique, so at most one window can ever apply to
        it: it is recorded on the GRAPH
        (``Graph.input_trims``) and becomes ``-ss``/``-to``, seeking every
        stream of that input coherently — captions and unselected streams
        included. A CTE name is a filtergraph pad, so its window is recorded on
        the BRANCH (``_Env.trims``) and the ``trim``/``atrim`` pair is spliced
        lazily by :meth:`_access`, the first time a stream of that CTE is
        consumed.

        A conjunct may supply only a lower bound (``<alias>.t >= x``) or only
        an upper one (``<alias>.t <= y``),
        via :func:`sqlmpeg.parser._time_bounds`, which also normalizes the
        mirrored operand order (``x <= <alias>.t`` etc.) and flags a strict
        ``>``/``<`` so it is rejected here too. Two conjuncts for the same
        alias MERGE into one window (``t >= 1 AND t <= 2`` behaves exactly
        like ``t BETWEEN 1 AND 2``) — resolve already rejected a second bound
        of the same kind, so this only ever fills in the other half. Every
        check below duplicates one resolve already made (defensive re-check,
        as elsewhere in this pass).

        `conjuncts` is the TIME half of the WHERE clause
        (:meth:`_split_where`), not the whole of it: row predicates share the
        clause and are decided on rows, not on the timeline.
        """
        where = select.args.get("where")
        if not isinstance(where, exp.Where) or not conjuncts:
            return
        windows: dict[str, tuple[int | float | None, int | float | None]] = {}
        for conjunct in conjuncts:
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
            if _fold(column.this) != TIME_COLUMN:
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
                start = self._time_bound(low, env, select)
            if high is not None:
                end = self._time_bound(high, env, select)
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
                if any(
                    opt.name == "seek_end"
                    for opt in self.res.input_options.get(alias, ())
                ):
                    raise _error(
                        ErrorCode.UNSUPPORTED_SQL,
                        f"'{alias}' sets seek_end and is also seeked by "
                        f"'WHERE {alias}.t' -- one input, two seek origins",
                        fallback=select,
                        hint=f"drop seek_end from {alias}'s input(), or drop "
                        f"the WHERE window on '{alias}'",
                    )
                if self.fanout_sinks and self.fanout_expr is not None:
                    # A fan-out row's window belongs to the FILE that row
                    # writes, not to the `-i` every one of them reads.
                    self.fanout_windows[alias] = window
                else:
                    self.graph.input_trims[alias] = window
            else:
                env.trims[alias] = window

    def _time_bound(
        self, bound: exp.Expr, env: _Env, select: exp.Select
    ) -> int | float:
        """One trim bound in seconds: a literal, or the value grammar's answer.

        A computed bound is still a SEEK, so it must come out a number. The
        one way it could come out NULL — an input whose duration was never
        probed — is already a rejection naming that field
        (:meth:`_input_duration`), so the raise below is the defensive floor.

        A fan-out command evaluates the bound against ITS pinned row, which is
        what makes ``WHERE f.t BETWEEN c.start_t AND c.end_t`` a per-row seek.
        """
        value = self._eval_value(bound, env, self.fanout_row, select)
        if isinstance(value, int | float):
            return value
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"time bound '{bound.sql(dialect='postgres')}' is "
            + ("NULL" if value is None else "text"),
            bound,
            fallback=select,
            hint="a trim bound is a number of seconds",
        )

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

        This is also where a trimmed caption is rejected: the WHERE
        window is collected before any projection lowers, so "is this CTE's
        subtitle/data actually CONSUMED under a trim" is only knowable here, at
        the point the trim would be applied. A CTE's trim is a filtergraph
        ``trim``/``atrim`` pair, which cannot carry subtitle or data streams at
        all, so for a CTE the rejection is permanent; on an input
        alias it does not arise, because there is no filter node to feed.
        """
        window = env.trims.get(alias)
        if window is None:
            trimmed = alias in self.graph.input_trims or alias in self.fanout_windows
            if value.type in _PASSTHROUGH_ONLY and trimmed:
                # MEASURED 2026-08-15, not theoretical: ffmpeg does not retime
                # subtitle/data packets under an input -ss (copy OR transcode;
                # cue times stay near-original while video rebases to zero), so
                # a seeked caption track plays out of sync by the seek amount.
                # Reject rather than ship silent desync.
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

        `window` may have either half absent (open-ended), so the
        ``trim``/``atrim`` node gets only the args it has: ``start=X``,
        ``end=Y``, or both.
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
        if isinstance(node, exp.ArrayAgg):
            return self._lower_array_agg(node, env, select)
        if isinstance(node, exp.Cast):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "casts are not supported",
                node,
                fallback=select,
                hint="a stream has exactly one type",
            )
        if isinstance(node, exp.Array):
            # The one array LITERAL the language has is a chapter list, and
            # that is a column of the file rather than a stream.
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "an array literal is not a stream expression",
                node,
                fallback=select,
                hint=_CHAPTERS_COLUMN_HINT,
            )
        if isinstance(node, exp.Coalesce):
            # Not a call: COALESCE resolves against the ROW model, not the
            # registry -- it is how a nullable track column is spelled.
            return self._lower_coalesce(node, env, select)
        if is_value_expr(node):
            # A value expression, never a stream. Reaching here means it is not
            # a tag column either: unaliased, or inside a CTE body.
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"every SELECT column must be a stream expression, got "
                f"{_describe(node)}",
                node,
                fallback=select,
                hint="a value expression names a metadata TAG: give it an alias "
                "for the tag key",
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

    def _lower_array_agg(
        self, node: exp.ArrayAgg, env: _Env, select: exp.Select
    ) -> _Value:
        """``array_agg(<stream expression>)``: the explicit splat.

        The argument lowers over the branch's surviving tuples exactly as it
        would on its own -- ``t`` is already the N-element array of the
        rows in row order (:meth:`_row_value`), and a filter call over it
        already broadcasts elementwise -- so the aggregate is the identity on
        the value, and the sugar and the spelled-out form emit the same bytes
        by construction rather than by agreement.
        """
        inner = node.this
        if not isinstance(inner, exp.Expr):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "array_agg() takes one stream expression",
                node,
                fallback=select,
                hint=_ARRAY_AGG_HINT,
            )
        if env.relation is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "array_agg() aggregates track rows, and this query has none",
                node,
                fallback=select,
                hint=_ARRAY_AGG_HINT,
            )
        return self._lower_expr(inner, env, select)

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
        if isinstance(binding, _RowBinding):
            # Under the INPUT alias, not the row one: a row table has no window
            # of its own, and every rule about the streams (`-i`, `-ss`, the
            # caption-seek rejection) is a property of the file they came from.
            return binding.source, self._row_value(binding, name, index, anchor, select)
        return alias, self._cte_value(binding, name, index, anchor, select)

    def _row_value(
        self,
        binding: _RowBinding,
        name: str,
        index: int | None,
        anchor: exp.Expr,
        select: exp.Select,
    ) -> _Value:
        """One column of a track-row table — and only the row itself is a stream.

        ``t`` over N surviving rows is an N-element ARRAY in row order, exactly
        what a bare ``f.audio`` is, so
        every existing array rule (splat, broadcast, subscript, zip) applies to
        it unchanged and the downstream passes learn nothing new.

        A metadata column is not an output: streams are the only outputs there
        are, and ``SELECT t.tags.language`` names a string. That is a typed
        rejection rather than a stringly-typed output, and its hint says what
        metadata columns ARE for.
        """
        schema = binding.schema
        if name != ROW_STREAM:
            if name not in schema and map_ref(name) is None:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"unknown column '{binding.alias}.{column_label(name)}'",
                    anchor,
                    fallback=select,
                    hint=binding.exposes,
                )
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{binding.alias}.{column_label(name)}' is track metadata, not "
                "a stream, and a SELECT column is an output stream",
                anchor,
                fallback=select,
                hint=_WRITTEN_ROW_HINT
                if binding.values is not None
                else _CHAPTER_ROW_HINT
                if binding.streamless
                else _ROW_METADATA_HINT,
            )
        if binding.values is not None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{binding.alias}' is a written row, not a stream",
                anchor,
                fallback=select,
                hint=_WRITTEN_ROW_HINT,
            )
        if binding.column == CHAPTERS_COLUMN:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{binding.alias}' is a chapter row, not a stream",
                anchor,
                fallback=select,
                hint=_CHAPTER_ROW_HINT,
            )
        if not binding.rows:
            raise _error(
                ErrorCode.STREAM_NOT_FOUND,
                f"'{binding.alias}' selects nothing: no "
                f"{binding.column} track of '{self._path_of(binding.source)}' "
                "survived",
                anchor,
                fallback=select,
                hint="an empty row set would select no streams; widen the WHERE, "
                "or check that the file has the tracks you expect",
            )
        streams = [
            self._row_stream(binding, row, position, anchor, select)
            for position, row in enumerate(binding.rows)
        ]
        if index is None:
            return _array(binding.type, streams)
        if not 1 <= index <= len(streams):
            have = f"{len(streams)} row" + ("" if len(streams) == 1 else "s")
            raise _error(
                ErrorCode.STREAM_NOT_FOUND,
                f"'{binding.alias}[{index}]' does not exist: "
                f"'{binding.alias}' has {have}",
                anchor,
                fallback=select,
                hint=_SUBSCRIPT_HINT,
            )
        return _scalar(streams[index - 1])

    def _row_stream(
        self,
        binding: _RowBinding,
        row: _TrackRow | None,
        position: int,
        anchor: exp.Expr,
        select: exp.Select,
    ) -> _Stream:
        """One result row's track — and the NULL rejection when there isn't one.

        Selecting a nullable track column (outer join) without COALESCE is a
        typed rejection naming the row that was NULL, never a silently missing
        output. An outer join is the user saying the
        counterpart may be absent, so what to put there instead is a decision
        only they can make; ``COALESCE(<column>, <fill>)`` is where they make
        it, and the hint says so with the fill this column's type takes.

        In table mode there is no ffmpeg command to be
        missing an input for — the NULL row is exactly what an outer join's
        gap IS, and it prints as an empty cell, psql-style, same as any other
        NULL. ``_NULL_STREAM`` is the sentinel :meth:`_value_to_cells` reads
        back into that empty cell; its empty ref can never collide with a
        real one (every real ref is non-empty).
        """
        if row is not None:
            self._reject_codecless(
                row.stream.source,
                f"'{binding.alias}' (row {position + 1})",
                anchor,
                select,
            )
            return row.stream
        if self.table_mode:
            return _Stream(ref=_NULL_STREAM_REF, type=binding.type, source=None)
        fill = _FILL_SPELLINGS.get(binding.type)
        hint = (
            f"an outer join leaves gaps; fill them with "
            f"COALESCE({binding.alias}, {fill})"
            if fill is not None
            # data rows have no fill spelling at all: nothing can stand in
            # for a missing data track, so the join itself must not leave
            # the gap.
            else "data tracks have no fill; use an INNER or LEFT join so "
            "every selected row has one"
        )
        raise _error(
            ErrorCode.STREAM_NOT_FOUND,
            f"'{binding.alias}' is NULL in row {position + 1}: "
            f"{self._unmatched_text(binding, position)}",
            anchor,
            fallback=select,
            hint=hint,
        )

    def _unmatched_text(self, binding: _RowBinding, position: int) -> str:
        """What the missing row failed to match, named from its paired row."""
        relation = binding.relation
        row = relation.tuples[position]
        paired_alias, paired = self._paired_row(relation, row, binding.alias)
        keys = relation.keys.get(paired_alias or "", [])
        if paired is None or not keys:
            return f"the join found no {binding.column} row of '{binding.alias}'"
        described = ", ".join(
            f"{paired_alias}.{column_label(key)}={paired.columns.get(key)!r}"
            for key in keys
        )
        return f"no '{binding.alias}' row matched {described}"

    def _paired_row(
        self,
        relation: _RowRelation,
        row: _RowTuple,
        alias: str,
    ) -> tuple[str | None, _TrackRow | None]:
        """The counterpart of a gap: the first row table that DID match here.

        A fill's provenance is the paired (non-NULL counterpart) row's metadata,
        and its inherited options come from that same row, so a silence-filled
        French mix stays French.
        Relation order (FROM order) breaks the tie when three tables joined.
        """
        for other in relation.aliases:
            if other == alias:
                continue
            track = _track_of(row, other)
            if track is not None:
                return other, track
        return None, None

    # -- COALESCE(<row>, <fill>) -----------------

    def _lower_coalesce(self, node: exp.Expr, env: _Env, select: exp.Select) -> _Value:
        """The accepted spelling for a nullable track column: fill its gaps.

        The result is the same N-element array ``<alias>`` is, in the
        same row order — every gap replaced by a generated stand-in. Only the
        gaps mint anything: a join with no unmatched rows compiles to exactly
        the command the bare column would -- consume-once here means "generate
        nothing nobody needed".
        """
        binding, fill = self._coalesce_parts(node, env, select)
        relation = binding.relation
        if not relation.tuples:
            raise _error(
                ErrorCode.STREAM_NOT_FOUND,
                f"'{binding.alias}' selects nothing: no "
                f"{binding.column} track of '{self._path_of(binding.source)}' "
                "survived",
                node,
                fallback=select,
                hint="an empty row set would select no streams; widen the WHERE, "
                "or check that the file has the tracks you expect",
            )
        streams: list[_Stream] = []
        for row in relation.tuples:
            track = _track_of(row, binding.alias)
            if track is not None:
                # The real track goes through `_access` exactly as a bare
                # a bare `<alias>` would, so the input's WHERE window (and the
                # caption-seek rejection) still applies to it.
                streams.append(
                    self._access(
                        env, binding.source, _scalar(track.stream), node, select
                    ).streams[0]
                )
                continue
            _, paired = self._paired_row(relation, row, binding.alias)
            streams.append(self._lower_fill(fill, binding, paired, node, select))
        return _array(binding.type, streams)

    def _coalesce_parts(
        self, node: exp.Expr, env: _Env, select: exp.Select
    ) -> tuple[_RowBinding, exp.Expr]:
        """``(the track column's row table, the fill expression)``, or a rejection.

        Deliberately narrow: COALESCE exists in this dialect for exactly one
        job — standing something in for an outer join's missing track — so it
        takes a track-row stream column and one fill, and nothing else. It
        creates no nodes, which is what lets :meth:`_classify` call it on an
        argument before deciding whether to lower it.
        """
        arguments = [
            argument
            for argument in [node.this, *node.expressions]
            if isinstance(argument, exp.Expr)
        ]
        if len(arguments) != 2:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"COALESCE takes a track column and one fill, got "
                f"{len(arguments)} argument{'' if len(arguments) == 1 else 's'}",
                node,
                fallback=select,
                hint=_COALESCE_HINT,
            )
        column = _unwrap(arguments[0])
        binding: _Binding | None = None
        if isinstance(column, exp.Column):
            table_node = column.args.get("table")
            if table_node is not None:
                binding = env.bindings.get(_fold(table_node))
        if not isinstance(binding, _RowBinding) or _fold(column.this) != ROW_STREAM:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "COALESCE's first argument is a track-row stream column, got "
                f"{_describe(arguments[0])}",
                arguments[0],
                fallback=select,
                hint=_COALESCE_HINT,
            )
        return binding, arguments[1]

    def _lower_fill(
        self,
        node: exp.Expr,
        binding: _RowBinding,
        paired: _TrackRow | None,
        anchor: exp.Expr,
        select: exp.Select,
    ) -> _Stream:
        """Mint the stand-in for one missing track, per the per-type table.

        Two mechanisms, one rule. ``ffmpeg.<source>()`` is a zero-input filter
        node (``anullsrc`` for audio, ``color`` for video), option-checked
        against the installed ffmpeg exactly like a source in FROM;
        ``sqlmpeg.empty_captions()`` is an INPUT, because a filtergraph carries
        no subtitle pads to generate one on. Either way the fill inherits from
        the PAIRED row — the counterpart that did match — both its options
        (:meth:`_inherited_fill_options`) and its provenance, so a
        silence-filled French mix is still tagged French.
        """
        call = _call_parts(node) if isinstance(node, exp.Expr) else None
        if call is None or call.args or not (call.namespaced or call.is_macro):
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"a COALESCE fill is a generated stand-in, got {_describe(node)}",
                node,
                fallback=select,
                hint=self._fill_hint(binding),
            )
        source_meta = paired.stream.source if paired is not None else None
        name = call.name.lower()
        if call.is_macro:
            return self._lower_macro_fill(node, name, call, binding, source_meta, select)
        source = self._source_filter(
            RawSource(alias="", name=name, options=(), call_node=node), select
        )
        self._check_fill_type(source.output, call.display, binding, node, select)
        options = self._filter_options(name, node, select)
        args = self._check_named_args(
            name,
            options,
            call.named,
            node,
            owner=f"{FILTER_NAMESPACE}.{name}",
            occupied=set(),
        )
        for option, value in self._inherited_fill_options(binding.type, paired).items():
            if value is None or option in args or option not in options:
                continue
            args[option] = value
        if "duration" in options and "duration" not in args:
            # A generator with no duration runs forever, and "forever" is not
            # what a missing 2-second track means. Inheriting it is the only
            # correct default, so when the paired row was never
            # probed for one, the query has to say it.
            raise _error(
                ErrorCode.UDF_ARG_TYPE,
                f"{call.display}() has no duration to stand in for: the paired "
                f"track's duration was never probed",
                node,
                fallback=select,
                hint=f"give the fill one, e.g. {call.display}(duration => 2)",
            )
        return _Stream(
            ref=self.ctx.node(name, args, [], [source.output]),
            type=source.output,
            source=source_meta,
        )

    def _lower_macro_fill(
        self,
        node: exp.Expr,
        name: str,
        call: _Call,
        binding: _RowBinding,
        source_meta: StreamMeta | None,
        select: exp.Select,
    ) -> _Stream:
        """``sqlmpeg.empty_captions()`` as a fill: an input, with the pair's tags."""
        macro = INPUT_MACROS.get(name)
        if macro is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL
                if name in MACROS
                else ErrorCode.UNKNOWN_FUNCTION,
                f"{call.display}() cannot stand in for a missing track"
                if name in MACROS
                else f"unknown function {call.display}()",
                node,
                fallback=select,
                hint=self._fill_hint(binding),
            )
        if call.named:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"{call.display}() takes no arguments: an empty caption track "
                "has nothing to configure",
                call.named[0].value,
                fallback=node,
                hint=f"write {call.display}()",
            )
        self._check_fill_type(macro.output, call.display, binding, node, select)
        return _Stream(
            ref=self._mint_input(macro), type=macro.output, source=source_meta
        )

    def _check_fill_type(
        self,
        output: StreamType,
        display: str,
        binding: _RowBinding,
        node: exp.Expr,
        select: exp.Select,
    ) -> None:
        """A fill stands in for a track, so it has to BE one of the same type."""
        if output == binding.type:
            return
        raise _error(
            ErrorCode.UDF_ARG_TYPE,
            f"{display}() generates a {output} stream, but "
            f"'{binding.alias}' is {binding.type}",
            node,
            fallback=select,
            hint=self._fill_hint(binding),
        )

    def _fill_hint(self, binding: _RowBinding) -> str:
        spelling = _FILL_SPELLINGS.get(binding.type)
        if spelling is None:
            return (
                f"nothing generates a {binding.type} track, so there is no fill "
                f"for '{binding.alias}'; select it from a "
                "join that always matches"
            )
        return (
            f"the fill for a {binding.type} track is {spelling}; its options "
            "inherit from the paired row unless you give them"
        )

    def _inherited_fill_options(
        self, stream_type: StreamType, paired: _TrackRow | None
    ) -> dict[str, object]:
        """What the fill copies from the row it stands beside.

        Audio inherits DURATION only in v1 — a silent track's sample rate and
        layout are ffmpeg's own defaults, and amix resamples anyway, so
        inventing them would put options in the command nobody wrote. Video
        inherits size, rate and duration, because a black frame of the wrong
        size or rate is not a stand-in for the picture that is missing.
        An option the query set explicitly always wins (the caller only fills
        the ones it did not).
        """
        if paired is None:
            return {}
        columns = paired.columns
        if stream_type == "audio":
            return {"duration": columns.get("duration")}
        if stream_type == "video":
            width = columns.get("width")
            height = columns.get("height")
            size = (
                f"{int(width)}x{int(height)}"
                if isinstance(width, int | float) and isinstance(height, int | float)
                else None
            )
            return {
                "size": size,
                "rate": columns.get("fps"),
                "duration": columns.get("duration"),
            }
        return {}

    def _mint_input(self, macro: InputMacro) -> FrameRef:
        """Add the macro's own ``-i`` to the graph and ref its single stream.

        The alias is spelled so no query can ever collide with it (a dot AND a
        ``#``, neither legal in an unquoted identifier), because it is not a
        name anything resolves — it exists only so the graph's alias-keyed
        input tables (``sources``, ``input_options``) can carry the slot. The
        internal ``format`` option is what puts ``-f webvtt`` before the
        ``data:`` URI; see ``sqlmpeg.inputs.option_spec``.
        """
        index = len(self.graph.input_paths)
        alias = f"{MACRO_NAMESPACE}.{macro.name}#{index + 1}"
        self.graph.input_paths.append(macro.path)
        self.graph.sources[alias] = index
        self.minted_input_options[alias] = {"format": macro.format}
        return f"src:{alias}:{_TYPE_MARKERS[macro.output]}:0"

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
        if name == TIME_COLUMN:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{binding.alias}.t' is a time column, not a stream",
                anchor,
                fallback=select,
                hint=_SOURCE_DURATION_HINT,
            )
        if name == _REMOVED_FRAME:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{binding.alias}.{_REMOVED_FRAME}' is not a column",
                anchor,
                fallback=select,
                hint=f"'{binding.display}' produces one {binding.output} "
                f"stream: use '{binding.alias}.{binding.output}[1]'",
            )
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
        return f"'{binding.display}' exposes {binding.alias}.{binding.output}"

    def _input_value(
        self,
        alias: str,
        name: str,
        index: int | None,
        anchor: exp.Expr,
        select: exp.Select,
    ) -> _Value:
        if name == TIME_COLUMN:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{alias}.t' is a time column, not a stream",
                anchor,
                fallback=select,
                hint=_TIME_HINT,
            )
        if name == INPUT_DURATION_COLUMN:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{alias}.{INPUT_DURATION_COLUMN}' is a number of seconds, "
                "not a stream",
                anchor,
                fallback=select,
                hint=f"'{alias}.{INPUT_DURATION_COLUMN}' belongs in an "
                f"expression, e.g. WHERE {alias}.t <= {alias}."
                f"{INPUT_DURATION_COLUMN} - 60",
            )
        key = tag_key(name)
        if key is not None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{alias}.{column_label(name)}' is a text tag, not a stream",
                anchor,
                fallback=select,
                hint=f"give it an alias to write it back, e.g. SELECT "
                f"{alias}.video[1], {alias}.{column_label(name)} AS {key}",
            )
        if name == TAGS_COLUMN:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{alias}.{TAGS_COLUMN}' carries no streams: it is the "
                "container's tag map, and a SELECT column of a media query is "
                "an output stream",
                anchor,
                fallback=select,
                hint=f"read one key as a value, e.g. {alias}.{TAGS_COLUMN}.title",
            )
        if name == CHAPTERS_COLUMN:
            if index is not None:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"'{alias}.{CHAPTERS_COLUMN}' cannot be subscripted: a "
                    "chapter is not a stream",
                    anchor,
                    fallback=select,
                    hint=chapters_unnest_hint(alias),
                )
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"'{alias}.{CHAPTERS_COLUMN}' carries no streams: it is an "
                "array of chapter records, and a SELECT column of a media "
                "query is an output stream",
                anchor,
                fallback=select,
                hint=chapters_unnest_hint(alias),
            )
        array_type = _ARRAY_COLUMNS.get(name)
        if array_type is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"unknown column '{alias}.{name}'",
                anchor,
                fallback=select,
                hint=f"an input exposes the streams {alias}.video, "
                f"{alias}.audio, {alias}.subtitle and {alias}.data, plus the "
                f"values {alias}.t, {alias}.{INPUT_DURATION_COLUMN} and its "
                f"container tags ({alias}.{TAGS_COLUMN}.title, ...)",
            )
        if index is None:
            return self._enumerate(alias, array_type, anchor, select)
        stream_type: StreamType = array_type
        zero_based = index - 1

        self._check_bounds(alias, stream_type, zero_based, anchor, select)
        stream = self._source_stream(alias, stream_type, zero_based)
        self._reject_codecless(
            stream.source, f"'{alias}.{stream_type}[{zero_based + 1}]'", anchor, select
        )
        return _scalar(stream)

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

    def _reject_codecless(
        self,
        meta: StreamMeta | None,
        display: str,
        anchor: exp.Expr,
        select: exp.Select,
    ) -> None:
        """A probed stream ffmpeg could not IDENTIFY cannot reach a media sink.

        ffprobe reporting no codec at all (e.g. a DASH manifest's WebVTT
        AdaptationSets, which ffmpeg's demuxer sees but cannot name) means
        ffmpeg can neither copy the stream (no tag to write) nor transcode it
        (no decoder to invoke) -- the run is GUARANTEED to die at header-write
        with "Could not find tag for codec none". We know at compile time, so
        we say so at compile time. Table queries are exempt on purpose: rows
        with a NULL codec column are how you DISCOVER these tracks. An
        unprobed input (meta None) is exempt too -- nothing is known, so
        nothing is knowably broken.
        """
        if self.table_mode or meta is None or meta.codec is not None:
            return
        raise _error(
            ErrorCode.UNSUPPORTED_SQL,
            f"{display} has no identifiable codec: ffmpeg's demuxer reports "
            f"none, so the stream can be neither copied nor transcoded and no "
            f"container can carry it",
            anchor,
            fallback=select,
            hint="drop it from the SELECT (a query with no COPY can still "
            "inspect it as a table row, codec column NULL); if it is a "
            "subtitle track, extract it with a tool that can read it and mux "
            "the resulting file as its own input() instead",
        )

    def _enumerate(
        self, alias: str, stream_type: StreamType, anchor: exp.Expr, select: exp.Select
    ) -> _Value:
        """The whole array of `alias`'s `stream_type` streams, in file order.

        The one thing lowering cannot do symbolically: an array's LENGTH is a
        property of the file, so an input that could not be probed fails here
        -- the streams of a file that cannot be read cannot be enumerated, a
        natural error rather than a policy one.
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
        streams = [self._source_stream(alias, stream_type, k) for k in range(count)]
        for k, stream in enumerate(streams):
            self._reject_codecless(
                stream.source, f"'{alias}.{stream_type}[{k + 1}]'", anchor, select
            )
        return _array(stream_type, streams)

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
        if index is None:
            return self._cte_column_value(binding, column, anchor, select)
        # A subscript names one element of the BODY's array, whatever the
        # branch's relation did with the rows around it.
        value = column.value
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

    def _cte_column_value(
        self,
        binding: _CteBinding,
        column: _Column,
        anchor: exp.Expr,
        select: exp.Select,
    ) -> _Value:
        """One CTE column as this branch's relation reads it.

        A row-set column carries one stream per body row, so the value is that
        column re-read through the result tuples: a cross join repeats the
        stream once per partner row, and a filtered relation drops the rows it
        dropped. Everything else -- a scalar, an ``array_agg``, a bare input
        array re-exposed -- is one unit that broadcasts, exactly as before.
        """
        relation = binding.relation
        if (
            relation is None
            or not (column.splat and column.value.is_array)
            or len(column.value.streams) != binding.rows
        ):
            return column.value
        streams = [
            column.value.streams[row.position]
            for row in (tuple_.get(binding.name) for tuple_ in relation.tuples)
            if isinstance(row, _CteRow)
        ]
        if not streams:
            raise _error(
                ErrorCode.STREAM_NOT_FOUND,
                f"'{binding.name}.{column.name}' selects nothing: no row of "
                f"'{binding.name}' survived",
                anchor,
                fallback=select,
                hint="an empty row set would select no streams; widen the WHERE",
            )
        return _array(column.value.type, streams)

    def _cte_column(self, binding: _CteBinding, name: str) -> _Column | None:
        for column in binding.columns:
            if column.name == name:
                return column
        return None

    def _cte_columns_hint(self, binding: _CteBinding) -> str:
        names = {column.name for column in binding.columns if column.name is not None}
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
        """Resolve a call in the registry, and nowhere else.

        One convention, three shapes of filter, tried in the order that makes
        each reachable at all:

        * :data:`ARRAY_RETURNING` (namespaced spelling ONLY) and
          :data:`N_INPUT` (either spelling) come first, because both tables
          exist precisely for names the v1 pad scope check keeps OUT of the registry
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

    # -- the sqlmpeg macro namespace -----------------------------

    def _lower_macro_call(
        self, node: exp.Expr, name: str, call: _Call, env: _Env, select: exp.Select
    ) -> _Value:
        """Resolve ``sqlmpeg.<name>(...)`` against :data:`MACROS`, and nowhere
        else -- the registry is never consulted, so a macro compiles OFFLINE
        (``which() -> None``) exactly as well as it does against a live ffmpeg.

        A macro owns its OWN positional signature: there is no option table to
        bind against, so named arguments are rejected outright (UNSUPPORTED_SQL,
        the same shape-violation code resolve's own named-only/positional-only
        argument rules use) unless the macro declares its own closed option
        list, and arity/kind mismatches are UDF_ARG_TYPE naming the macro's
        signature -- mirroring the registry call's stream-signature message,
        but there is exactly one stream position (always index 0) to check, so
        no `_bind_options` machinery is involved.

        Broadcasting reuses :meth:`_expand_call` unchanged: it is type-driven
        off `positions`/`streams`, so a macro's single stream argument
        broadcasts elementwise exactly like any registry call's would.
        """
        input_macro = INPUT_MACROS.get(name)
        if input_macro is not None:
            # An input-minting macro: no filter node, no arguments,
            # one passthrough stream of the type it mints.
            if call.args or call.named:
                raise _error(
                    ErrorCode.UNSUPPORTED_SQL,
                    f"{call.display}() takes no arguments",
                    node,
                    fallback=select,
                    hint=f"write {call.display}()",
                )
            return _scalar(
                _Stream(
                    ref=self._mint_input(input_macro), type=input_macro.output
                )
            )
        macro = MACROS.get(name)
        if macro is None:
            raise _error(
                ErrorCode.UNKNOWN_FUNCTION,
                f"unknown function {call.display}()",
                node,
                fallback=select,
                hint=self._macro_function_hint(name),
            )
        if call.named and not macro.options:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"{call.display}() is a sqlmpeg macro: its arguments are "
                "positional only, in the documented order",
                call.named[0].value,
                fallback=node,
                hint=f"its signature is {macro.signature}",
            )
        if macro.name == loudnorm.FILTER and self.table_mode:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"{call.display}() is a filter, and a table query filters nothing",
                node,
                fallback=select,
                hint="print the tracks with a table query, normalize them with "
                "a COPY that writes a file",
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
        options = self._macro_options(macro, call, node)
        streams = {stream_pos: self._lower_expr(call.args[stream_pos], env, select)}

        def build(values: list[object], _element: int) -> FrameRef:
            return macro.expand(values, self.ctx.node, options)

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

    def _macro_options(
        self, macro: Macro, call: _Call, node: exp.Expr
    ) -> dict[str, object]:
        """A macro's named-only options: every one optional, none repeated.

        Returned in the MACRO's declared order, not the order they were
        written, so the rendered filter is the same whichever way round the
        query spells them. An omitted option is left out entirely -- the
        expansion renders only what was written, and ffmpeg's own default
        covers the rest. Repeats need no check here: resolve rejects a
        duplicate `name =>` on any call before lowering starts.
        """
        written: dict[str, object] = {}
        for argument in call.named:
            if argument.name not in macro.options:
                raise _error(
                    ErrorCode.UDF_ARG_TYPE,
                    f"{call.display}() has no '{argument.name}' option",
                    argument.value,
                    fallback=node,
                    hint=f"its signature is {macro.signature}",
                )
            try:
                written[argument.name] = _number(argument.value)
            except SqlmpegError as exc:
                raise _error(
                    exc.code,
                    f"{call.display}()'s '{argument.name}' option must be a "
                    "numeric literal",
                    argument.value,
                    fallback=node,
                    hint=f"its signature is {macro.signature}",
                ) from None
        return {name: written[name] for name in macro.options if name in written}

    def _macro_function_hint(self, name: str) -> str:
        """Did-you-mean over :data:`MACROS`, the small-by-design macro set."""
        matches = difflib.get_close_matches(name, macro_names(), n=1, cutoff=0.6)
        if matches:
            return f"did you mean {MACRO_NAMESPACE}.{matches[0]}()?"
        return (
            f"{MACRO_NAMESPACE}.<name> is one of sqlmpeg's own macros -- "
            f"{', '.join(macro_names())} -- not an ffmpeg filter; filters live "
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
        args_at = self._option_binder(
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

        def build(values: list[object], element: int) -> FrameRef:
            return self.ctx.node(
                name, dict(args_at(element)), [_as_ref(value) for value in values], [output]
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

    # -- fixed-count N-input filters ----------------------------

    def _n_input_options(self, name: str) -> dict[str, FilterOption] | None:
        """`name`'s option table if it is a callable fixed-count N-input filter.

        Mirrors :meth:`_array_options` exactly: in the table, a registry to ask,
        and an ffmpeg that actually HAS the filter. The last one is why options
        are fetched even for a call that passes none — an excluded name is in no
        registry table, so its option block is the only evidence this build has
        it (see ``Registry.excluded_options``).
        """
        if name not in N_INPUT or self.registry is None:
            return None
        if self.registry.get(name) is not None:
            # THIS ffmpeg has the filter in-scope (acrossfade was an ordinary
            # AA->A filter before ffmpeg 9 made it variadic): the registry's
            # own pad signature is the truth here, and the N_INPUT rescue is
            # only for builds whose pad scope check excluded the name.
            return None
        return self.registry.excluded_options(name)

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
        is none — the registry excluded the filter for exactly that reason),
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
        # A filter with no count option (ladspa) has nothing to cross-check the
        # supplied stream count against and nothing to write back -- the
        # streams themselves ARE the count, decided by the loaded plugin.
        option_name = spec.option
        if option_name is not None:
            declared = self._n_input_count(spec, option_name, args, options)
            if declared != count:
                anchor = next(
                    (arg.value for arg in call.named if arg.name == option_name), node
                )
                raise _error(
                    ErrorCode.UDF_ARG_TYPE,
                    f"{call.display}() was given {_stream_count(count)} but its "
                    f"'{option_name}' option says {declared}",
                    anchor,
                    fallback=select,
                    hint=_N_INPUT_HINT,
                )
            # Write the count onto the node unless this spec omits a defaulted
            # one (`emit_default`): ffmpeg only NEEDS `inputs=N` to grow pads
            # beyond the option's default of 2, and for a filter that is
            # variadic only on newer builds the omitted default is what keeps
            # the command portable.
            if spec.emit_default or option_name in args or count != spec.fallback:
                args[option_name] = count
        streams = {
            position: self._lower_expr(arg, env, select)
            for position, arg in enumerate(call.args[:count])
        }

        def build(values: list[object], _element: int) -> FrameRef:
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
        option_name: str,
        args: dict[str, object],
        options: dict[str, FilterOption],
    ) -> int:
        """What the count option says, written or introspected-default or fallback.

        `args` has already been validated against the option table, so a
        written value is a number in range; only the DEFAULT needs care, since
        `FilterOption.default` is verbatim ffmpeg text that is documented as
        never re-typed (it can be a constant name, or absent entirely). Called
        only when `spec.option` is not None; `option_name` is that narrowed
        value, passed separately so mypy sees a plain `str`.
        """
        written = args.get(option_name)
        if isinstance(written, (int, float)) and not isinstance(written, bool):
            return int(written)
        option = options.get(option_name)
        if option is not None and option.default is not None:
            try:
                return int(float(option.default))
            except ValueError:
                pass
        return spec.fallback

    # -- array-returning filters -----------------------

    def _array_options(self, name: str) -> dict[str, FilterOption] | None:
        """`name`'s option table if it is a callable array-returning filter.

        Three questions, one answer, because they have the same shape: is the
        name in :data:`ARRAY_RETURNING`, is there a registry at all, and does
        THIS ffmpeg actually have the filter. The last one is why the
        options are fetched even for a call with no named arguments: an excluded
        name is in no registry table, so its option block is the only evidence
        this build has it (see ``Registry.excluded_options``). None means "not
        callable", and the caller falls through to the ordinary namespaced
        rejection, hint and all.
        """
        if name not in ARRAY_RETURNING or self.registry is None:
            return None
        return self.registry.excluded_options(name)

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
        """No function takes a subtitle or data stream.

        An ffmpeg filtergraph carries video and audio only, so a caption or
        timed-metadata stream can never be a filter INPUT — in either tier.
        Tier 1 would otherwise report it as a generic signature mismatch and
        tier 2 as "expects gblur(video)"; both are true but neither says the
        thing that actually matters, which is that no signature could ever
        accept it. ``ParamKind`` and ``DynamicFilter.inputs`` are deliberately
        left alone ("ParamKind is UNCHANGED"), so this is the one
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
        """The stream-signature rejection — UDF_ARG_TYPE's remaining job.

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
        at all (``hflip(a.video[1])``) has nothing to validate — so the table stays
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
        `gblur(a.video[1], nope(a.video[1]))` is UNKNOWN_FUNCTION for `nope`, raised
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

    def _option_binder(
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
    ) -> Callable[[int], dict[str, object]]:
        """This call's option dict, as a function of the broadcast element.

        An option written as a compile-time expression
        (``scale(t, t.width / 2, -2)``) is evaluated against the row that
        element came from and REPLACED BY THE LITERAL it computes to, so a
        per-row option and a written one bind through the same
        :meth:`_bind_options` and are validated by the same option table.

        A call with no computed option binds exactly once and hands the same
        dict to every element -- which is every call that existed before
        arithmetic did.
        """
        if not any(is_value_expr(arg) for arg in self._option_args(call, extras)):
            args = self._bind_options(
                filter_name, call, node, select, env,
                options=options, extras=extras, timeline=timeline,
            )
            return lambda _element: args
        tuples = env.relation.tuples if env.relation is not None else []
        cache: dict[int, dict[str, object]] = {}

        def bound(element: int) -> dict[str, object]:
            if element not in cache:
                row = tuples[element] if element < len(tuples) else {}
                cache[element] = self._bind_options(
                    filter_name,
                    replace(
                        call,
                        named=[
                            _NamedArg(arg.name, self._computed_arg(arg.value, env, row, select))
                            for arg in call.named
                        ],
                    ),
                    node,
                    select,
                    env,
                    options=options,
                    extras=[self._computed_arg(arg, env, row, select) for arg in extras],
                    timeline=timeline,
                )
            return cache[element]

        return bound

    @staticmethod
    def _option_args(call: _Call, extras: list[exp.Expr]) -> list[exp.Expr]:
        """Every value node this call binds to an option, positional and named."""
        return [*extras, *(arg.value for arg in call.named)]

    def _computed_arg(
        self,
        node: exp.Expr,
        env: _Env,
        row: _RowTuple,
        select: exp.Select,
    ) -> exp.Expr:
        """One option argument as `row` makes it; anything else, untouched."""
        if not is_value_expr(node):
            return node
        return _literal_node(self._eval_value(node, env, row, select), node)

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
        build: Callable[[list[object], int], FrameRef],
    ) -> _Value:
        """Broadcast `build` over the array arguments, if there are any.

        Type-driven and tier-agnostic: `positions` is where the stream
        arguments are (always the LEADING positions, from the pad signature or
        from an N-input call's own count) and `build` is what turns one
        element's argument values into a subgraph. `build` also gets the
        ELEMENT INDEX, which is what lets a filter option computed per row
        pick out the row that element came from.
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
            # that input's provenance unconditionally. A call over two or more
            # streams (amix, overlay, xfade) is a join like concat's: it
            # threads provenance only when every input agrees
            # (`_agreed_source`).
            if len(positions) == 1:
                source = streams[positions[0]].at(element).source
            elif len(positions) >= 2:
                source = _agreed_source([streams[p].at(element) for p in positions])
            else:
                source = None
            expanded.append(_Stream(ref=build(values, element), type=returns, source=source))
        if length is None:
            return _scalar(expanded[0])
        return _array(returns, expanded)

    # -- named argument validation --

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
        have (or that the v1 scope check excluded); an empty dict is a real
        answer (a filter with no options) and is passed through as one.
        """
        if self.registry is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "options are validated against your installed ffmpeg; "
                "the provisioner failed to supply one",
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
        said. A collision is ``FILTER_OPTION_TYPE``, an option problem like any
        other, and the fix is to drop one of the two spellings.

        The collision check comes FIRST so the message names the conflict
        rather than whatever the registry would say about the name.

        `timeline` is the target's ``DynamicFilter.timeline`` flag, and it is a
        PARAMETER because this method cannot look filters up: every caller
        already holds the registry entry (or, for a generated source, knows
        there is no such field to hold — a source is never timeline-capable, so
        the default rejects). It admits ``enable`` BEFORE `options` is consulted
: ffmpeg implements ``enable`` in the filter framework, so
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
        """Did-you-mean over the registry (there is nothing else)."""
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
                #. Say where rather than "unknown".
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
            "filter set; the provisioner failed to supply one"
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
        ``scale(gblur(a.video[1], 2), 640, 480)`` sees the inner call's output
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
        if isinstance(node, exp.Coalesce):
            # A filled track column is a stream of the row table's own type,
            # which is knowable without lowering anything (no fill is minted).
            return self._coalesce_parts(node, env, select)[0].type
        call = _call_parts(node)
        if call is not None:
            name = call.name.lower()
            if call.is_macro:
                if name in INPUT_MACROS:
                    return INPUT_MACROS[name].output
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

    # -- table/csv queries --
    #
    # A table query never reaches ffmpeg -- the row model holds every cell at
    # compile time -- so this is a second top-level entry point (`run_table`,
    # parallel to `run`), not a mode bolted onto the streaming one. It reuses
    # the streaming machinery for anything STREAM-shaped (a row alias, a filtered
    # stream, COALESCE's fill) by calling into `_lower_expr` with
    # `self.table_mode` set; the one behavior that changes under it is
    # `_row_stream`'s NULL-row rejection, which becomes an empty cell. Metadata
    # columns have no streaming representation, so those shapes are intercepted
    # before `_lower_expr` sees them.

    def run_table(self) -> list[TableSink]:
        """One :class:`~sqlmpeg.table.TableSink` per COPY, or one bare-select."""
        for name, body in self.res.ctes.items():
            self.cte_columns[name] = tuple(
                self._lower_query(union_branches(body), body, tags="rows")
            )
            self._harvest_cte_tags(body)
            self._harvest_cte_dispositions(body)
        self.table_mode = True
        sinks: list[TableSink] = []
        if self.res.sinks:
            for raw in self.res.sinks:
                sinks.append(self._lower_table_sink(raw))
        else:
            result = self._lower_table_query(self.res.branches, self.res.select)
            sinks.append(TableSink(result=result, path=None, csv=False, header=False))
        self.graph.input_options = self._lower_input_options()
        return self._render_specs(sinks)

    def _lower_table_sink(self, raw: RawSink) -> TableSink:
        """One csv COPY: its query lowered, ``FORMAT``/``HEADER`` validated.

        Against ``sqlmpeg.sink.CSV_OPTIONS``, a separate table from
        ``SINK_OPTIONS``, so a media option like ``video_codec`` here is
        UNKNOWN rather than silently accepted.
        """
        result = self._lower_table_query(list(raw.branches), raw.query)
        header = False
        for option in raw.options:
            line, col = _pos(option.name_node, option.value, raw.path_node)
            value = validate_csv_option(option.name, _sink_value(option.value), line=line, col=col)
            if option.name == "header":
                assert isinstance(value, bool)
                header = value
        return TableSink(result=result, path=raw.path, csv=True, header=header)

    def _lower_table_query(self, branches: list[exp.Select], anchor: exp.Expr) -> TableResult:
        if not branches:
            raise _error(ErrorCode.UNSUPPORTED_SQL, "query has no SELECT", anchor)
        if len(branches) > 1:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                "a table/csv query does not support UNION ALL",
                branches[1],
                fallback=anchor,
                hint="run each branch as its own query",
            )
        return self._lower_table_branch(branches[0])

    def _lower_table_branch(self, select: exp.Select) -> TableResult:
        """One table/csv branch: row cardinality, then every column, per row.

        Cardinality is the branch's shared row relation -- every row source
        stays aligned to it, joins and CTE references included -- and 1 for a
        branch with no rows at all (a plain metadata/stream SELECT has exactly
        one row, the same way a bare scalar broadcasts). A GROUPED branch (a
        GROUP BY, an ``array_agg``, or both) prints one row per group instead
        -- see :meth:`_lower_grouped_table_branch`.
        """
        env = self._scope(select)
        env.grouped = is_grouped(select)
        env.group_keys = _partition_keys(select, env)
        self._check_grouped_cte_columns(select, env)
        time_conjuncts, row_conjuncts, assertion_conjuncts = self._split_where(select, env)
        self._collect_trims(select, env, time_conjuncts)
        self._filter_rows(row_conjuncts, env, select)
        self._check_assertions(assertion_conjuncts, select)
        self._order_rows(select, env)

        projections = select.expressions
        if not projections:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL, "SELECT has no output column", fallback=select
            )
        names: list[str] = []
        for projection in projections:
            qualifier = star_qualifier(projection)
            if qualifier is not None:
                names += self._star_names(qualifier, projection, env, select)
            else:
                names.append(_table_column_name(projection))

        if env.grouped:
            return self._lower_grouped_table_branch(select, env, projections, names)

        cardinality = len(env.relation.tuples) if env.relation is not None else 1
        per_column = self._table_columns(projections, env, select, cardinality)
        rows = [[per_column[c][r] for c in range(len(names))] for r in range(cardinality)]
        return TableResult(columns=names, rows=rows)

    def _table_columns(
        self,
        projections: list[exp.Expr],
        env: _Env,
        select: exp.Select,
        cardinality: int,
    ) -> list[list[CellValue]]:
        """Every printed column of a branch, in SELECT order, stars expanded."""
        columns: list[list[CellValue]] = []
        for projection in projections:
            qualifier = star_qualifier(projection)
            if qualifier is not None:
                columns += self._star_cells(
                    qualifier, projection, env, select, cardinality
                )
            else:
                columns.append(
                    self._table_projection(projection, env, select, cardinality)
                )
        return columns

    def _lower_grouped_table_branch(
        self,
        select: exp.Select,
        env: _Env,
        projections: list[exp.Expr],
        names: list[str],
    ) -> TableResult:
        """One printed row per GROUP BY group; no fan-out sink involved.

        Reuses the exact per-row machinery: for each group, the relation's
        tuples are pinned to that group (the array_agg column sees every tuple,
        so it collects the whole group; every other column -- a key or a
        constant, the only shapes grouping validity admits -- sees just the
        first, since it is the same value for the whole group by construction)
        and every projection lowers as one ordinary, single-row column.
        """
        relation = env.relation
        assert relation is not None  # is_grouped implies rows; resolve enforced it
        groups = self._grouped_partitions(env, select)
        original = relation.tuples
        rows: list[list[CellValue]] = []
        try:
            for group in groups:
                row: list[CellValue] = []
                for projection in projections:
                    aggregate = isinstance(_projection_expr(projection), exp.ArrayAgg)
                    relation.tuples = list(group) if aggregate else group[:1]
                    row += [
                        cells[0]
                        for cells in self._table_columns([projection], env, select, 1)
                    ]
                rows.append(row)
        finally:
            relation.tuples = original
        return TableResult(columns=names, rows=rows)

    def _grouped_partitions(
        self, env: _Env, select: exp.Select
    ) -> list[list[_RowTuple]]:
        """The relation's tuples partitioned into the groups a table query
        prints, one row each.

        A row-referencing GROUP BY key partitions in FIRST-APPEARANCE order,
        the same partition a media fan-out builds. With no such key the whole
        relation is ONE group -- Postgres's own rule for an aggregate with
        nothing to partition by (unlike a media fan-out's ungrouped case,
        where every row writes its own file).

        An EMPTY relation partitions into NO groups either way: a table query
        prints the same zero rows an ungrouped branch does, and a media query
        falls through to the empty-row-set rejection.
        """
        relation = env.relation
        tuples = relation.tuples if relation is not None else []
        if not tuples:
            return []
        if not env.group_keys:
            return [list(tuples)]
        groups: dict[tuple[RowValue, ...], list[_RowTuple]] = {}
        for row in tuples:
            key = tuple(self._key_value(node, env, row, select) for node in env.group_keys)
            groups.setdefault(key, []).append(row)
        return list(groups.values())

    def _table_projection(
        self, projection: exp.Expr, env: _Env, select: exp.Select, cardinality: int
    ) -> list[CellValue]:
        """One SELECT column, per row: a metadata value, or a stream cell."""
        expr = _unwrap(projection)
        if isinstance(expr, exp.ArrayAgg):
            return self._array_cell_broadcast(expr, env, select, cardinality)
        if isinstance(expr, exp.Column):
            table_node = expr.args.get("table")
            if table_node is not None:
                binding = env.bindings.get(_fold(table_node))
                if isinstance(binding, _RowBinding):
                    name = _fold(expr.this)
                    if name != ROW_STREAM:
                        return self._row_metadata_cells(binding, name, expr, select)
                elif (
                    isinstance(binding, _InputBinding)
                    and _fold(expr.this) == CHAPTERS_COLUMN
                ):
                    return self._chapters_cells(binding.alias, expr, select, cardinality)
                elif (
                    isinstance(binding, _InputBinding)
                    and _fold(expr.this) == TAGS_COLUMN
                ):
                    return self._container_tag_cells(
                        binding.alias, expr, select, cardinality
                    )
                elif (
                    isinstance(binding, _InputBinding | _SourceBinding)
                    and _fold(expr.this) in _ARRAY_COLUMNS
                ):
                    return self._array_cell_broadcast(expr, env, select, cardinality)
                elif isinstance(binding, _CteBinding):
                    column = self._cte_column(binding, _fold(expr.this))
                    # A splat column falls through to `_value_to_cells` below,
                    # which is where its per-row cardinality is already
                    # honored; a non-splat one (array_agg / a bare input
                    # array, re-exposed through the CTE) stays ONE cell.
                    if column is not None and column.value.is_array and not column.splat:
                        return self._array_cell_broadcast(expr, env, select, cardinality)
        if is_value_expr(expr) or _is_input_value_column(expr, env):
            return self._value_cells(expr, env, select, cardinality)
        shape = subscript_metadata_shape(expr)
        if shape is not None:
            metadata_value = self._accessor_value(expr, select)
            return [metadata_value] * cardinality
        stream_value = self._lower_expr(projection, env, select)
        splat = self._is_splat_projection(projection, env)
        return self._value_to_cells(stream_value, cardinality, splat=splat)

    def _value_cells(
        self, node: exp.Expr, env: _Env, select: exp.Select, cardinality: int
    ) -> list[CellValue]:
        """A CASE / ``||`` column, evaluated once per row.

        The same expression a media query writes back as a tag, PRINTED
        instead: a table query is how you check what the tag would say before
        writing it.
        """
        relation = env.relation
        if relation is None:
            return [self._eval_value(node, env, {}, select)] * cardinality
        return [self._eval_value(node, env, row, select) for row in relation.tuples]

    def _row_metadata_cells(
        self, binding: _RowBinding, name: str, anchor: exp.Expr, select: exp.Select
    ) -> list[CellValue]:
        """A row alias's metadata column, one value per row (NULL for a gap)."""
        schema = binding.schema
        if name == TAGS_COLUMN:
            return [_tag_cell(row) for row in binding.rows]
        if name == DISPOSITION_COLUMN and name in schema:
            return [_disposition_cell(row) for row in binding.rows]
        if name not in schema and map_ref(name) is None:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"unknown column '{binding.alias}.{column_label(name)}'",
                anchor,
                fallback=select,
                hint=binding.exposes,
            )
        return [None if row is None else row.columns.get(name) for row in binding.rows]

    def _container_tag_cells(
        self, alias: str, anchor: exp.Expr, select: exp.Select, cardinality: int
    ) -> list[CellValue]:
        """A bare ``<input>.tags`` as ONE array cell, broadcast to every row.

        The map's entries print as key/value records in key order:
        ``{(artist,Nobody),(title,Clip)}``. Name a key to read one of them.
        """
        result = self.probes.get(alias)
        if result is None:
            raise _error(
                ErrorCode.INPUT_NOT_FOUND,
                f"cannot read tags of '{self._path_of(alias)}': file not "
                "found or unreadable",
                anchor,
                fallback=select,
                hint=f"'{alias}.{TAGS_COLUMN}' is the container's own tag map, "
                "and only a readable input has one",
            )
        return [_tags_to_cell(result.tags)] * cardinality

    def _chapters_cells(
        self, alias: str, anchor: exp.Expr, select: exp.Select, cardinality: int
    ) -> list[CellValue]:
        """A bare ``<input>.chapters`` as ONE array cell, broadcast to every row.

        The array's records print in schema order (index, title, start_t,
        end_t): ``{(1,Intro,0.0,1.0),(2,Chapter 1,1.0,2.0)}``. Unnest it to
        read the fields as columns.
        """
        result = self.probes.get(alias)
        if result is None:
            raise _error(
                ErrorCode.INPUT_NOT_FOUND,
                f"cannot read chapters of '{self._path_of(alias)}': file not "
                "found or unreadable",
                anchor,
                fallback=select,
                hint=f"'{alias}.{CHAPTERS_COLUMN}' is the container's own "
                "chapter list, and only a readable input has one",
            )
        cell = ArrayCell(
            elements=tuple(
                RecordCell(
                    fields=(chapter.index, chapter.title, chapter.start_t, chapter.end_t)
                )
                for chapter in result.chapters
            )
        )
        return [cell] * cardinality

    def _array_cell_broadcast(
        self, node: exp.Expr, env: _Env, select: exp.Select, cardinality: int
    ) -> list[CellValue]:
        """A bare input array column (``f.video``/``f.audio``/...) or a whole
        ``array_agg(...)`` column: every element as ONE array cell,
        broadcasting to every row -- the value does not depend on which row
        (or, grouped, which group) is printing it."""
        value = self._lower_expr(node, env, select)
        cell = ArrayCell(
            elements=tuple(self._stream_to_cell(stream) for stream in value.streams)
        )
        return [cell] * cardinality

    def _value_to_cells(
        self, value: _Value, cardinality: int, splat: bool = True
    ) -> list[CellValue]:
        """A lowered stream `_Value` as one cell per row: a scalar broadcasts,
        and a row column's array (``t`` over N surviving rows) splats
        one stream cell per row -- the array IS the row set, not one cell.

        `splat` False marks an array that is NOT a row set -- a call broadcast
        over a bare input array, whose length is the file's track count and
        has nothing to do with the row count. That one prints as a single
        array cell per row, exactly as the bare array column does.
        """
        if value.is_array and splat:
            return [self._stream_to_cell(stream) for stream in value.streams]
        if value.is_array:
            array_cell = ArrayCell(
                elements=tuple(self._stream_to_cell(stream) for stream in value.streams)
            )
            return [array_cell] * cardinality
        cell = self._stream_to_cell(value.streams[0])
        return [cell] * cardinality

    def _stream_to_cell(self, stream: _Stream) -> CellValue:
        """One stream as a cell, carrying its REF until `_render_specs` runs."""
        if stream.ref == _NULL_STREAM_REF:
            return None
        return StreamCell(type=stream.type, spec=stream.ref)

    def _render_specs(self, sinks: list[TableSink]) -> list[TableSink]:
        """Turn every cell's stream ref into the spec the command will name.

        Which ``-i`` an alias reads is settled only once every input option
        and trim window is known, and two aliases over one untrimmed path
        share a slot. A table previews the command, so it names the same
        input: the refs wait here for the final input list.
        """
        self.graph = dedup_inputs(self.graph)
        return [
            replace(
                sink,
                result=TableResult(
                    columns=sink.result.columns,
                    rows=[[self._render_cell(cell) for cell in row] for row in sink.result.rows],
                ),
            )
            for sink in sinks
        ]

    def _render_cell(self, cell: CellValue) -> CellValue:
        if isinstance(cell, StreamCell):
            return StreamCell(type=cell.type, spec=self._stream_spec(cell.spec))
        if isinstance(cell, ArrayCell):
            return ArrayCell(
                elements=tuple(self._render_cell(element) for element in cell.elements)
            )
        return cell

    def _stream_spec(self, ref: FrameRef) -> str:
        """The ffmpeg stream spec (``"0:a:0"``) for a source ref, else the
        filtergraph node id verbatim (``"n2"``) for a filtered one."""
        if is_src(ref):
            alias, stream_type, index = src_parts(ref)
            return f"{self.graph.sources[alias]}:{_TYPE_MARKERS[stream_type]}:{index}"
        return ref

# provenance & small value helpers


def _provenance(stream: _Stream) -> dict[str, str]:
    """Language/title tags of the source stream an output is derived 1:1 from.

    `_Stream.source` is what threads them: it survives a passthrough, the WHERE
    trim, and any chain of single-stream-input calls unconditionally; a call
    over two or more streams (``amix``, ``overlay``) and a concat pad thread it
    only when every stream feeding them agrees (:func:`_agreed_source`).
    ``language=und`` is what an mp4 muxer stamps on an untagged stream, so it
    carries no information and is not copied.

    Only STREAM_TAG_COLUMNS ride, not every key the source carries: a file's
    ``encoder`` or ``handler_name`` tag riding through a filter would emit
    ``-metadata`` ffmpeg does not emit today.
    """
    source = stream.source
    if source is None:
        return {}
    metadata: dict[str, str] = {}
    for key in STREAM_TAG_COLUMNS:
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


def _outputs(
    columns: list[_Column],
    tags: _TagOverrides,
    dispositions: _DispositionOverrides,
) -> list[Output]:
    """One :class:`~sqlmpeg.ir.Output` per stream a SELECT list carries.

    The SELECT list IS the output stream list, and an array column is several
    streams, so it splats into consecutive Outputs. Every element of an
    aliased array column keeps that alias VERBATIM (no ordinal suffix): the
    alias names the column, not the stream.
    """
    return [
        Output(
            ref=stream.ref,
            type=stream.type,
            name=column.name,
            metadata=_metadata(stream, tags),
            disposition=_disposition(stream, dispositions),
        )
        for column in columns
        for stream in column.value.streams
    ]


def _metadata(stream: _Stream, tags: _TagOverrides) -> dict[str, str]:
    """One output's tags: its provenance, with this query's overrides applied.

    An override REPLACES the provenance value for its key, a NULL one removes
    the key, and a key nothing overrode passes through untouched.
    """
    metadata = _provenance(stream)
    if stream.source is None:
        return metadata
    for key, value in tags.get(id(stream.source), {}).items():
        if value is None:
            metadata.pop(key, None)
        else:
            metadata[key] = value
    return metadata


def _disposition(
    stream: _Stream, dispositions: _DispositionOverrides
) -> tuple[str, ...] | None:
    """The flags one output asserts, or None where the query asserted none.

    Nothing rides through from the source: ffmpeg copies a stream's own
    disposition already, so only a written column puts `-disposition:<i>` on
    the command line.
    """
    if stream.source is None:
        return None
    return dispositions.get(id(stream.source))


def _tag_text(value: str | int | float | bool) -> str:
    """A tag value as the text ffmpeg receives; a boolean spells itself out."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value if isinstance(value, str) else str(value)


def _partition_keys(select: exp.Select, env: _Env) -> tuple[exp.Expr, ...]:
    """The GROUP BY keys that actually partition the branch's relation.

    A key reading a row source -- a track row, a chapter row, a CTE row --
    varies from tuple to tuple. An input-level or constant key has the same
    value everywhere and leaves one group.
    """
    return tuple(key for key in group_keys(select) if _reads_row_source(key, env))


def _reads_row_source(node: exp.Expr, env: _Env) -> bool:
    for sub in node.walk():
        if not isinstance(sub, exp.Column):
            continue
        table_node = sub.args.get("table")
        if table_node is None:
            continue
        if isinstance(env.bindings.get(_fold(table_node)), _RowBinding | _CteBinding):
            return True
    return False


def _has_track_rows(env: _Env) -> bool:
    """True when the branch has rows carrying a track to tag per stream.

    Chapter rows and written rows carry none, so a branch holding only those
    tags the CONTAINER, exactly as one with no rows at all does.
    """
    return any(
        isinstance(binding, _RowBinding) and not binding.streamless
        for binding in env.bindings.values()
    )


def _group_row(env: _Env) -> _RowTuple:
    """The one tuple a FILE-level value reads, or no row at all.

    A container tag and a chapter list belong to the file, not to a row, so
    they are evaluated over a single representative tuple: the group's first
    where the branch groups, the relation's first otherwise (an ungrouped
    branch that survives the one-row rule has exactly one).
    """
    relation = env.relation
    if relation is None or not relation.tuples:
        return {}
    return relation.tuples[0]


def _is_tag_column(projection: exp.Expr, env: _Env) -> bool:
    """True for a SELECT column that sets a metadata TAG rather than a stream.

    A tag column is aliased — the alias IS the tag key — and its value is a
    compile-time expression over the row: a literal, NULL, a row's metadata
    column, an input's ``duration`` or container tag, CASE, ``||``, arithmetic
    or ``::text``. Everything else is a stream expression and lowers as one.

    The branch decides what it tags: one with track rows tags THOSE (per
    stream), one without tags the CONTAINER.
    """
    if _projection_name(projection) is None:
        return False
    value = _unwrap(projection)
    if isinstance(value, exp.Null | exp.Literal | exp.Neg) or is_value_expr(value):
        return True
    if _is_input_value_column(value, env):
        return True
    return _row_metadata_column(value, env) is not None


def _is_input_value_column(node: exp.Expr, env: _Env) -> bool:
    """True for an input alias's scalar column — ``duration`` or a container
    tag — a value, never a stream."""
    if not isinstance(node, exp.Column):
        return False
    table_node = node.args.get("table")
    if table_node is None:
        return False
    if not isinstance(env.bindings.get(_fold(table_node)), _InputBinding):
        return False
    name = _fold(node.this)
    return name == INPUT_DURATION_COLUMN or tag_key(name) is not None


def _row_metadata_column(node: exp.Expr, env: _Env) -> str | None:
    """The metadata column `node` reads off a row alias, else None (``track``
    is a stream, not metadata; a chapters row has no stream to tag AT ALL, so
    it is never a tag column either -- it falls through to `_row_value`'s
    ordinary "not an output" rejection instead)."""
    if not isinstance(node, exp.Column):
        return None
    table_node = node.args.get("table")
    if table_node is None:
        return None
    binding = env.bindings.get(_fold(table_node))
    if not isinstance(binding, _RowBinding) or binding.streamless:
        return None
    name = _fold(node.this)
    return None if name == ROW_STREAM else name


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


def _literal_node(value: RowValue, source: exp.Expr) -> exp.Expr:
    """A computed value back as the literal node the option binder reads.

    The synthesized node inherits `source`'s position, so an option that
    rejects what a row computed still points at the expression that wrote it.
    """
    node: exp.Expr
    if value is None:
        node = exp.Null()
    elif isinstance(value, str):
        node = exp.Literal.string(value)
    elif value < 0:
        node = exp.Neg(this=exp.Literal.number(str(-value)))
    else:
        node = exp.Literal.number(str(value))
    line, col = _pos(source)
    node.meta.update({"line": line, "col": col})
    return node


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

    The expression's CONTENT is deliberately unchecked: the variable
    vocabulary is per-filter and not introspectable, so it is ffmpeg's to
    validate at run time.
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


# public entry point


def lower(
    res: Resolved,
    probes: dict[str, ProbeResult | None],
    *,
    registry: Registry | None = None,
) -> Graph:
    """Lower a resolved query into an IR graph -- its FIRST command's.

    The whole query except for the one fan-out shape that compiles to a
    command sequence (see :func:`lower_commands`, which returns them all).

    `probes` is keyed by input ALIAS (``compiler.compile_sql`` builds it, one
    ``probe()`` per distinct path); a missing or ``None`` entry means that
    input could not be read, and lowering stays symbolic for it.

    `registry` IS the function surface: the filter set of the ffmpeg
    on PATH, introspected lazily. It is a PARAMETER rather than a module lookup
    so that a caller — ``compile_sql``, or a test — decides which ffmpeg (or
    which captured snapshot) this compile resolves against. None, or an empty
    one, means every call name is UNKNOWN_FUNCTION.

    Raises ``SqlmpegError`` — and nothing else — on every rejection.
    """
    return lower_commands(res, probes, registry=registry)[0]


def lower_commands(
    res: Resolved,
    probes: dict[str, ProbeResult | None],
    *,
    registry: Registry | None = None,
) -> list[Graph]:
    """Lower a resolved query into one IR graph per ffmpeg COMMAND.

    Usually ONE graph, a fan-out COPY included: ffmpeg takes several output
    files per invocation, so a ``TO (<expression>)`` lowers each surviving
    row into a :class:`SinkUnit` of a single graph, sharing one decode of the
    inputs. The row COUNT is a property of the probed file, so it comes back
    from the lowering rather than being known up front.

    The exception is a fan-out that TRIMS and stream-copies every stream it
    maps (:func:`_fanout_keeps_chain`): that one lowers again, one graph per
    row, and the caller chains the commands.

    Same probing/registry contract as :func:`lower`; raises ``SqlmpegError``
    -- and nothing else -- on every rejection.
    """
    try:
        shared = _Lowerer(res, probes, registry, fanout_sinks=True)
        graph = shared.run()
        count = shared.fanout_count
        if count is None:
            return [graph]
        _check_distinct_paths(
            [unit.path for unit in graph.sinks], res, grouped=shared.fanout_grouped
        )
        if not _fanout_keeps_chain(graph, conflict=shared.fanout_window_conflict):
            return [graph]
        return [
            _Lowerer(res, probes, registry, fanout_index=index).run()
            for index in range(count)
        ]
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


def _fanout_keeps_chain(graph: Graph, *, conflict: bool) -> bool:
    """True when this fan-out has to stay one ffmpeg command per file.

    An output-side seek re-encodes, and ffmpeg writes a corrupt file when one
    meets a stream copy, so a windowed fan-out whose every mapped stream is a
    copy keeps the ``&&`` chain and seeks its inputs instead. Anything that
    re-encodes -- a filtered stream, a codec the sink names -- takes the
    single invocation, and the streams that would have been copies re-encode
    along with it. `conflict` is the other way back to the chain: one file
    wanting two different windows, which only an ``-i`` seek can say.
    """
    if conflict:
        return True
    if all(unit.window is None for unit in graph.sinks):
        return False
    return all(
        is_src(output.ref) and output.type not in copy_suppressed_scopes(unit.options)
        for unit in graph.sinks
        for output in unit.outputs
    )


def _check_distinct_paths(
    paths: list[str | None], res: Resolved, *, grouped: bool = False
) -> None:
    """No two fan-out files may share a destination.

    Rows sharing a destination is the typo guard; GROUP BY is how a query ASKS
    for them to share one, so the hint says so. Two distinct GROUPS colliding
    is still a rejection -- the key told them apart, the name did not.
    """
    what = "groups" if grouped else "rows"
    hint = (
        "add a column that tells the groups apart to the TO expression"
        if grouped
        else "add a column that tells the rows apart, e.g. t.index::text, to "
        "the TO expression, or GROUP BY the column they share to write one "
        "file per group"
    )
    seen: dict[str, int] = {}
    anchor = res.sinks[0].path_expr if res.sinks else None
    fallback = res.sinks[0].path_node if res.sinks else None
    for index, path in enumerate(paths):
        if path is None:
            continue
        if path in seen:
            raise _error(
                ErrorCode.UNSUPPORTED_SQL,
                f"{what} {seen[path] + 1} and {index + 1} both name '{path}'",
                anchor,
                fallback=fallback,
                hint=hint,
            )
        seen[path] = index


def lower_table(
    res: Resolved,
    probes: dict[str, ProbeResult | None],
    *,
    registry: Registry | None = None,
) -> list[TableSink]:
    """Lower a resolved TABLE query into its printable result set(s).

    The sibling of :func:`lower` for a query with no media destination -- a
    bare SELECT, or every COPY a ``FORMAT csv`` one. Never
    executes ffmpeg, never inserts splits (there is no filtergraph fan-out to
    consume-once here, only cells). Same probing/registry contract as
    :func:`lower`; raises ``SqlmpegError`` -- and nothing else -- on every
    rejection.
    """
    try:
        return _Lowerer(res, probes, registry).run_table()
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
