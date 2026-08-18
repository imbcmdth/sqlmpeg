"""Emit pass: IR ``Graph`` -> ffmpeg ``-filter_complex`` string and argv.

This is pass 4 of the compiler (see "Architecture" in sqlmpeg-project.md).
It assumes the graph has already been through the split pass, so every pad
has exactly one consumer.

FrameRef grammar consumed here (authoritative source: ``ir.py``)::

    "<node-id>"          -> output pad 0 of that node
    "<node-id>:<p>"      -> output pad p of that node
    "src:<alias>:v:<k>"  -> the k-th (0-based) video stream of the input bound
                            to <alias>; rendered as "[<index>:v:<k>]"
    "src:<alias>:a:<k>"  -> same for audio; rendered as "[<index>:a:<k>]"
    "src:<alias>:s:<k>"  -> same for subtitle; rendered as "<index>:s:<k>"
    "src:<alias>:d:<k>"  -> same for data; rendered as "<index>:d:<k>"

Subtitle and data refs (RFC-004) are passthrough-only: they only ever appear
as sink-unit outputs and are only ever rendered as bare ``-map`` targets,
never as filtergraph labels. Repeating one is legal ffmpeg, so they are exempt
from the consume-once check (:func:`_check_fanout`).

Output groups (RFC-006)
-----------------------
``Graph.sinks`` is one :class:`~sqlmpeg.ir.SinkUnit` per output FILE, and
:attr:`Emitted.groups` mirrors it one-for-one. The whole command is a single
ffmpeg invocation: the inputs are rendered once, the ``-filter_complex`` once,
and then each group contributes its own ``-map`` block, per-stream options,
sink options and path, in that order — ffmpeg's own native multi-output form.

Two scopes matter here and they are NOT the same:

* output STREAM indices restart at 0 in every output file, so ``-c:<i>`` and
  ``-metadata:s:<i>`` are indexed within a group;
* filtergraph LABELS are graph-scoped — one filter_complex serves the whole
  command — so ``out<i>`` is indexed over ``Graph.outputs``, the union of
  every group's outputs, and stays unique across the command.

Each ``Output`` becomes an :class:`OutputMap`:

* **passthrough** -- ``Output.ref`` is a source ref with zero node consumers.
  The stream never enters the filtergraph: the map target is the bare ffmpeg
  stream spec (``"0:a:1"``) and ``copy`` is True, so ``build_ffmpeg_args``
  adds ``-c:<i> copy``. (v0 hung a ``null`` filter on such refs; that hack is
  gone.)
* **filtered** -- ``Output.ref`` names a node pad. That pad's label is
  ``out<i>``, where ``i`` is the output's index in ``Graph.outputs``, and the
  map target is ``"[out<i>]"``. v0's single ``[out]`` label is gone: labels
  are always indexed, even for a one-column SELECT.

A graph whose outputs are all passthrough has NO nodes and therefore an empty
``filter_complex``; ``build_ffmpeg_args`` omits ``-filter_complex`` entirely
in that case.

Input seeking (RFC-004 amendment)
---------------------------------
``Graph.input_trims`` maps an alias to a ``(start, end)`` window, either bound
possibly ``None`` (plan 039: an open-ended ``WHERE <alias>.t >= x`` or
``<alias>.t <= y``). Since an alias owns exactly one ``-i`` slot, emit resolves
it against ``Graph.sources`` into :attr:`Emitted.input_trims`, a list parallel
to :attr:`Emitted.inputs`, and ``build_ffmpeg_args`` renders ``-ss <start>``
and/or ``-to <end>`` immediately BEFORE the owning ``-i`` — an input option, so
the demuxer seeks and every stream of that input is cut coherently, subtitle
and data streams included — omitting whichever half is ``None`` (a tail-only
window has no ``-to`` at all, so ffmpeg reads to EOF). Both bounds are on the
input's own timeline. A seeked input can still be stream-copied (a window no
longer forces a filter node into the graph), and then the cut snaps back to
the preceding keyframe; a decoded stream is frame-accurate.

Input options (RFC-005 SS4, plan 041)
--------------------------------------
``Graph.input_options`` maps an alias to its validated
``input('path', name => value, ...)`` options (``sqlmpeg.inputs.INPUT_OPTIONS``
-- ``loop``, ``stream_loop``, ``framerate``, ``itsoffset``, ``hwaccel``),
resolved the same way ``input_trims`` is, into :attr:`Emitted.input_options`, a
list parallel to :attr:`Emitted.inputs`. ``build_ffmpeg_args`` renders them
immediately before that input's ``-i``, and BEFORE any ``-ss``/``-to`` from
``input_trims`` -- verified empirically against a real ffmpeg 7.1
(``-loop 1 -framerate 15 -to 2 -i frame.png`` runs correctly; ffmpeg input
options are order-INSENSITIVE among themselves, but this fixed order --
options, then seek flags, then ``-i`` -- is what sqlmpeg always emits). A bool
value of ``False`` (e.g. ``loop => false``) is never rendered, same as a sink
option's False bool.

An option name is resolved through ``sqlmpeg.inputs.option_spec``, not the
user-facing table alone: lower mints inputs of its own (RFC-009's
``sqlmpeg.empty_captions()``, whose ``data:`` URI needs ``-f webvtt``) and
carries their flags as INTERNAL input options, which render exactly like any
other but are not nameable from SQL.

Pad label scheme
----------------
Every node output pad gets exactly one label, derived from the node id
(sanitized: any character outside ``[A-Za-z0-9_]`` becomes ``_``):

* single-output node  -> ``[<id>]``            e.g. ``[n2]``
* multi-output node   -> ``[<id><p>]``         e.g. a ``split`` node whose
  id is ``n1_split`` and whose ``outputs`` list has two entries produces
  ``[n1_split0][n1_split1]``, so a full chain reads
  ``[n1]split=2[n1_split0][n1_split1]``
* a pad named by ``Graph.outputs[i]`` -> ``[out<i>]``, whichever node/pad it is

The out-pad count of a node is ``len(node.outputs)`` -- no filter is special
cased. ``out0..out<n-1>`` are reserved up front, and any label collision is
broken by appending ``_`` until the label is unique. Labels of pads that are
consumed inside a merged comma-chain are never rendered (that is what chain
merging means).

Chain merging
-------------
Nodes are walked in graph (topological) order. A node extends the chain built
so far when it is the *sole* consumer of the immediately preceding node's only
output pad and has no other inputs; such runs are joined with ``,`` and only
the head's input labels and the tail's output labels are rendered. Chains are
joined with ``;`` (no whitespace). The README example therefore emits::

    [0:v:0]crop=w=600:h=200:x=1200:y=50,scale=w=iw*0.5:h=-2[n2];[0:v:0][n2]overlay=x=20:y=20[out0]

(both source refs read ``[0:v:0]``: the README's two aliases name the same
untrimmed ``game.mp4``, so input dedup, below, folds them onto one ``-i``.)

Input dedup (RFC-009 "Carried from RFC-008", plan 060)
--------------------------------------------------------
Two ``INPUT`` aliases over the SAME path, with the SAME (validated,
normalized) ``input_options`` and NO ``input_trims`` window on either one,
share a single ``-i``: :func:`_dedup_inputs` runs first, before anything
else in :func:`emit`, and folds such aliases' ``Graph.sources`` entries onto
one ``input_paths`` slot (first occurrence wins the slot's position).  A
trimmed alias is NEVER merged, even against an identical trim window on
another alias -- the concat splice (two aliases, two DIFFERENT windows, same
file; cookbook recipe 17) needs two distinct ``-i``'s to seek independently,
and this pass does not try to tell "two windows that happen to match" apart
from "two windows that must stay apart", so it conservatively never merges a
trimmed input at all.

This is a POST-LOWER normalization, not a lowering decision: trims are only
known once `sqlmpeg.lower` has resolved every ``WHERE <alias>.t`` window
into ``Graph.input_trims`` (RFC-004), so a merge test that has to check "no
trim window" cannot run any earlier than emit's own entry point without
either duplicating that resolution or coupling this pass to lower's internal
state. Doing it here also means the pass never touches `sqlmpeg.parser` or
`sqlmpeg.lower` (both owned by a concurrently-developed RFC-009 wave): it
operates purely on the finished :class:`~sqlmpeg.ir.Graph`, the same object
:func:`emit` already takes as input, by re-keying ``sources``/``input_paths``
-- exactly the resolution :func:`_src_spec` already does per source ref, so
no FrameRef, node, or label needs to change. The pre-dedup graph (e.g. from
``sqlmpeg ir``) still shows one input slot per alias -- only the rendered
``-i`` list and the indices baked into the filtergraph are deduped.

Zero-input nodes (RFC-005 §1, plan 042)
---------------------------------------
A generated source (``FROM ffmpeg.testsrc(duration => 2) t``) lowers to a
node with ``inputs=[]``. This pass needed no change for it, VERIFIED rather
than assumed: such a node renders as a chain head with no input labels
(``testsrc=duration=2[out0]``), :func:`_extends` refuses to comma-append any
node that does not take exactly one input, so a source always STARTS a chain
and can never be swallowed into the previous one, and what FOLLOWS it merges
normally (``sine=frequency=440,volume=volume=0.5[out0]``).
:func:`_verify_topological` and :func:`_check_fanout` both iterate
``node.inputs``, so an empty list simply contributes nothing. A graph made
only of sources has no ``-i`` at all: ``build_ffmpeg_args`` emits
``ffmpeg -filter_complex ... -map [out0] out.mp4``, which is a complete and
valid ffmpeg command.

Argument rendering
------------------
``args`` renders in dict insertion order as ``filter=k1=v1:k2=v2``. Special
cases: a filter with no args renders bare (``hflip``, no ``=``); the key
``"expr"`` renders value-only (``setpts=PTS-STARTPTS``); every arg of a
``split``/``asplit`` node renders value-only (``split=2``). ``concat`` needs
nothing special: ``{"n": 2, "v": 1, "a": 0}`` -> ``concat=n=2:v=1:a=0``.

Escaping
--------
All filter values go through :func:`_escape_value` -- the single place where
ffmpeg filtergraph escaping happens (drawtext ``text=`` included). Metadata
values do NOT: they are passed as argv words, not parsed as a filtergraph.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from .errors import ErrorCode, SqlmpegError
from .inputs import InputOptionSpec, option_spec
from .ir import FrameRef, Graph, Node, Output, SinkUnit, StreamType, is_src, src_parts
from .sink import SINK_OPTIONS, SinkOptionSpec

OUTPUT_LABEL_PREFIX = "out"
"""Filtered outputs are labelled ``out0``, ``out1``, ... (without brackets)."""

_SPLIT_FILTERS = frozenset({"split", "asplit"})
_VALUE_ONLY_KEYS = frozenset({"expr"})

_TYPE_MARKERS: dict[StreamType, str] = {
    "video": "v",
    "audio": "a",
    "subtitle": "s",
    "data": "d",
}

# Stream types no filtergraph can carry (RFC-004): they are only ever bare
# `-map`s, so they are exempt from the consume-once rule below.
_PASSTHROUGH_ONLY: frozenset[StreamType] = frozenset({"subtitle", "data"})

# Level 1 (filter-option) escaping: these characters would otherwise separate
# options / quote inside a single filter's argument list.
_LEVEL1_ESCAPES = {
    "\\": "\\\\",
    "'": "\\'",
    ":": "\\:",
    "=": "\\=",
}

# Level 2 (filtergraph) escaping: applied on top of level 1, so the backslashes
# introduced above are themselves escaped -- exactly the composition described
# in ffmpeg's "Notes on filtergraph escaping".
_LEVEL2_SPECIAL = re.compile(r"[\\'\[\],;#\s]")

_LABEL_UNSAFE = re.compile(r"[^A-Za-z0-9_]")


@dataclass
class OutputMap:
    """One ``-map`` argument: a filtered pad label or a raw stream spec."""

    target: str  # "[out0]" (filtered) or "0:a:1" (passthrough)
    type: StreamType
    copy: bool  # passthrough -> True -> -c:<i> copy
    metadata: dict[str, str]


@dataclass
class OutputGroup:
    """One output FILE of the command: its maps, its path, its options.

    Mirrors one :class:`~sqlmpeg.ir.SinkUnit` (RFC-006). `maps` is that
    file's ``-map`` list in SELECT order, and its INDEX is the ffmpeg output
    stream index — per file, because ffmpeg restarts output stream numbering
    at every output — so ``-c:<i>``/``-metadata:s:<i>`` are computed from it
    and from nothing wider. `path` is None only for a bare-SELECT graph,
    where the caller supplies the destination (`build_ffmpeg_args`'s
    ``out_path``). `options` are the validated ``WITH (...)`` sink options.
    """

    maps: list[OutputMap]
    path: str | None = None
    options: dict[str, object] = field(default_factory=dict)


@dataclass
class Emitted:
    inputs: list[str]  # file paths in -i order
    filter_complex: str  # "" when every output is a passthrough
    groups: list[OutputGroup]  # one per Graph.sinks entry, same order
    # RFC-004 input-seek amendment: entry `i` is the (start, end) window of the
    # `-i` at index `i`, or None if that input is not seeked. Plan 039: either
    # half of a present window may itself be None (open-ended). :func:`emit`
    # always fills this in parallel with `inputs` (all None when the graph has
    # no `input_trims`); the empty default is for hand-built Emitted objects and
    # means the same thing -- no input is seeked.
    input_trims: list[tuple[float | None, float | None] | None] = field(default_factory=list)
    # RFC-005 SS4 (plan 041): entry `i` is the validated options of the `-i`
    # at index `i` (insertion-ordered), or an empty dict if that input set
    # none. Parallel to `inputs`, same convention as `input_trims`.
    input_options: list[dict[str, object]] = field(default_factory=list)

    @property
    def maps(self) -> list[OutputMap]:
        """Every group's maps concatenated, in command order (read-only).

        A convenience view of the whole command's ``-map`` list. Its index is
        an ffmpeg output STREAM index only when there is exactly one group:
        ffmpeg numbers output streams per FILE, which is why
        ``build_ffmpeg_args`` walks ``groups`` and never this.
        """
        return [mapping for group in self.groups for mapping in group.maps]


_MergeKey = tuple[str, tuple[tuple[str, object], ...]]


def _merge_key(g: Graph, index: int, aliases: list[str], trimmed: set[str]) -> _MergeKey | None:
    """This input slot's dedup key, or None if it can never merge (RFC-009).

    None for any slot carrying a trimmed alias -- a trim is a property of
    that alias's own ``-i`` (RFC-004), so two windows are never folded
    together even when they happen to be equal (see the module docstring's
    "Input dedup" section: telling "coincidentally equal" apart from "must
    stay apart" is not attempted). Otherwise the key is the path plus the
    resolved options every alias on this slot set, sorted by option name --
    two slots merge only when both match exactly.
    """
    if any(alias in trimmed for alias in aliases):
        return None
    options: dict[str, object] = {}
    for alias in aliases:
        options.update(g.input_options.get(alias, {}))
    return (g.input_paths[index], tuple(sorted(options.items())))


def _dedup_inputs(g: Graph) -> Graph:
    """Fold untrimmed same-path same-options aliases onto one ``-i`` (RFC-009).

    See the module docstring's "Input dedup" section for why this runs here,
    at the top of :func:`emit`, rather than in `sqlmpeg.lower`. Aliases keep
    their names; only ``Graph.sources`` (alias -> input index) and
    ``Graph.input_paths`` are rewritten, in first-occurrence order, so every
    other pass (labels, chains, fanout) needs no change -- they already
    resolve a source ref's input index through ``g.sources`` at render time.
    Returns `g` unchanged (same object) when nothing merges.
    """
    aliases_by_index: dict[int, list[str]] = {}
    for alias, index in g.sources.items():
        aliases_by_index.setdefault(index, []).append(alias)

    trimmed = set(g.input_trims.keys())

    key_to_new_index: dict[_MergeKey, int] = {}
    old_to_new_index: dict[int, int] = {}
    new_input_paths: list[str] = []
    for index, path in enumerate(g.input_paths):
        key = _merge_key(g, index, aliases_by_index.get(index, []), trimmed)
        if key is not None and key in key_to_new_index:
            old_to_new_index[index] = key_to_new_index[key]
            continue
        new_index = len(new_input_paths)
        new_input_paths.append(path)
        old_to_new_index[index] = new_index
        if key is not None:
            key_to_new_index[key] = new_index

    if len(new_input_paths) == len(g.input_paths):
        return g

    new_sources = {alias: old_to_new_index[index] for alias, index in g.sources.items()}
    return replace(g, input_paths=new_input_paths, sources=new_sources)


def emit(g: Graph) -> Emitted:
    """Render `g` as an ffmpeg filtergraph plus its output map list.

    Raises ``SqlmpegError(INTERNAL)`` if the graph is malformed: a cycle or
    non-topological node ordering, a dangling FrameRef, no outputs, or a pad
    with more than one consumer (which means the split pass did not run --
    an ``Output`` counts as a consumer, in whichever group it sits, so a pad
    feeding both a node and an output, or two outputs naming the same pad, is
    a split-pass bug).

    Runs :func:`_dedup_inputs` first (RFC-009): untrimmed aliases that share
    a path and input options collapse onto one ``-i`` before anything else
    below reads ``g.sources``/``g.input_paths``.
    """
    g = _dedup_inputs(g)
    _verify_topological(g)

    nodes = list(g.nodes.values())
    pads = {node.id: _out_pad_count(node) for node in nodes}
    _check_fanout(nodes, g)
    labels = _assign_labels(nodes, pads, g.outputs)

    chains = _build_chains(nodes, pads)
    filter_complex = ";".join(_render_chain(chain, g, pads, labels) for chain in chains)

    groups = [_output_group(g, unit, labels) for unit in g.sinks]

    return Emitted(
        inputs=list(g.input_paths),
        filter_complex=filter_complex,
        groups=groups,
        input_trims=_input_windows(g),
        input_options=_input_option_list(g),
    )


def _output_group(g: Graph, unit: SinkUnit, labels: dict[str, str]) -> OutputGroup:
    return OutputGroup(
        maps=[_output_map(g, output, labels) for output in unit.outputs],
        path=unit.path,
        options=dict(unit.options),
    )


def _input_windows(g: Graph) -> list[tuple[float | None, float | None] | None]:
    """``g.input_trims`` (alias-keyed) resolved into a per-``-i`` list.

    Each alias's window applies to whichever input slot ``g.sources`` sends
    it to; the result is parallel to ``g.input_paths``: entry `i` is that
    input's ``(start, end)``, or None. Ordinarily one alias owns a slot, but
    plan 060's ``_dedup_inputs`` never merges a trimmed alias's slot with
    another's, so two aliases sharing a slot here (a hand-built ``Graph``,
    not something dedup produces) are only tolerated when their windows
    agree exactly -- a genuine mismatch is still the lower/split bug this
    guards against.
    """
    windows: list[tuple[float | None, float | None] | None] = [None] * len(g.input_paths)
    for alias, window in g.input_trims.items():
        index = g.sources.get(alias)
        if index is None:
            raise _internal(f"input trim names unknown source alias {alias!r}")
        if not 0 <= index < len(g.input_paths):
            raise _internal(
                f"input trim on alias {alias!r} maps to out-of-range input index {index}"
            )
        previous = windows[index]
        if previous is not None and previous != window:
            raise _internal(
                f"input {index} carries two different trim windows, {previous} and "
                f"{window}; each alias must own its own -i (lower/split bug)"
            )
        windows[index] = window
    return windows


def _input_option_list(g: Graph) -> list[dict[str, object]]:
    """``g.input_options`` (alias-keyed) resolved into a per-``-i`` list.

    Same resolution ``_input_windows`` does for trims -- entry `i` is that
    input's options dict (insertion-ordered), empty for an input that set
    none. Two aliases sharing a slot (plan 060's dedup merges untrimmed,
    same-path, same-options aliases onto one) is fine exactly when their
    option sets agree; a real mismatch on one slot is still a lower/split
    bug, same as `_input_windows`.
    """
    options: list[dict[str, object]] = [{} for _ in g.input_paths]
    seen: list[bool] = [False for _ in g.input_paths]
    for alias, alias_options in g.input_options.items():
        index = g.sources.get(alias)
        if index is None:
            raise _internal(f"input options name unknown source alias {alias!r}")
        if not 0 <= index < len(g.input_paths):
            raise _internal(
                f"input options on alias {alias!r} map to out-of-range input index {index}"
            )
        candidate = dict(alias_options)
        if seen[index] and options[index] != candidate:
            raise _internal(
                f"input {index} carries two different option sets; each alias "
                "must own its own -i (lower/split bug)"
            )
        options[index] = candidate
        seen[index] = True
    return options


def build_ffmpeg_args(e: Emitted, out_path: str | None = None) -> list[str]:
    """Full ffmpeg argv for `e`: one invocation, one output file per group.

    Path precedence per group: `out_path` if given, else that group's own
    path, else ``ValueError("no output path given and the query has no
    sink")``. `out_path` OVERRIDES, so it is legal only when `e` has exactly
    one group — overriding several files with one path would silently write
    them on top of each other — and a multi-group `e` with `out_path` set is
    ``ValueError``. This is a CLI/programmer contract, not something user SQL
    can trigger: the CLI rejects ``-o`` against a multi-sink script itself
    (usage error, exit 2) before calling this.

    The SELECT list is authoritative: exactly one ``-map`` per
    :class:`OutputMap`, in order, with ``-c:<i> copy`` for passthrough streams
    and ``-metadata:s:<i> k=v`` (keys sorted) for provenance metadata. Those
    ``<i>`` are PER GROUP — ffmpeg restarts output stream numbering at each
    output file — so group 2's first map is ``-c:0``, not ``-c:<n>``. v0's
    implicit ``-map 0:a? -c:a copy`` tail is gone. ``-filter_complex`` is
    rendered once for the whole command, and omitted when the graph is pure
    passthrough.

    Per-``-i`` flags come first, in a fixed order (verified against real
    ffmpeg -- see the module docstring's "Input options" section): an input
    carrying entries in `e.input_options` gets its ``sqlmpeg.inputs.INPUT_OPTIONS``
    flags (e.g. ``-loop 1``, ``-framerate 15``) rendered FIRST, then an input
    carrying a window in `e.input_trims` gets ``-ss <start>`` and/or
    ``-to <end>`` (RFC-004's input seek; plan 039 lets either half be absent
    for an open-ended window), and only then its own ``-i``. Short or empty
    `input_options`/`input_trims` lists simply leave the remaining inputs
    plain, so a hand-built :class:`Emitted` needs neither at all.

    Sink option rendering (RFC-002, plan 027): after each group's
    -map/-metadata block, that group's own options render in insertion order
    purely from ``sqlmpeg.sink.SINK_OPTIONS`` table data -- no option-specific
    logic here. Per-stream specs (``per_stream=True``) render ``f"{flag}:{i}"``
    for every output index `i` of THAT GROUP whose type matches the spec's
    scope; container specs (``per_stream=False``) render ``flag`` once,
    regardless of output types. A bool value of False is never rendered (e.g.
    ``faststart false`` emits nothing).

    Copy suppression: if a group's options set a codec option (``flag ==
    "-c"``) scoped to video, that group's video passthrough outputs drop their
    ``-c:<i> copy`` (the explicit codec re-encodes them instead); same for
    audio and — since the rule reads the option table's ``scope`` rather than
    naming types — for ``subtitle_codec`` (RFC-004: webvtt -> mov_text on a
    passthrough subtitle output). Suppression is per group, so one file may
    re-encode a stream another file copies.
    """
    if not e.groups:
        raise ValueError("no output path given and the query has no sink")
    if out_path is not None and len(e.groups) > 1:
        raise ValueError(
            f"out_path overrides a single output file, but this command writes "
            f"{len(e.groups)}"
        )

    args = ["ffmpeg"]
    for index, input_path in enumerate(e.inputs):
        options = e.input_options[index] if index < len(e.input_options) else {}
        args += _render_input_options(options)
        window = e.input_trims[index] if index < len(e.input_trims) else None
        if window is not None:
            start, end = window
            if start is not None:
                args += ["-ss", _render_number(start)]
            if end is not None:
                args += ["-to", _render_number(end)]
        args += ["-i", input_path]
    if e.filter_complex:
        args += ["-filter_complex", e.filter_complex]
    for group in e.groups:
        path = out_path if out_path is not None else group.path
        if path is None:
            raise ValueError("no output path given and the query has no sink")
        copy_suppressed_scopes = _copy_suppressed_scopes(group.options)
        # Output stream indices restart at 0 in every output file.
        for index, mapping in enumerate(group.maps):
            args += ["-map", mapping.target]
            if mapping.copy and mapping.type not in copy_suppressed_scopes:
                args += [f"-c:{index}", "copy"]
            for key in sorted(mapping.metadata):
                args += [f"-metadata:s:{index}", f"{key}={mapping.metadata[key]}"]
        args += _render_sink_options(group)
        args.append(path)
    return args


# ---------------------------------------------------------------------------
# input option rendering (RFC-005 SS4, plan 041)
# ---------------------------------------------------------------------------


def _render_input_option_value(spec: InputOptionSpec, value: object) -> str | None:
    """Render one input option's value per its spec, or None to omit entirely.

    A bool value of False is always omitted (e.g. plain ``loop => false``);
    ``num``/``int`` go through :func:`_render_number` (so a negative
    ``itsoffset`` renders as ``-1``, not ``-1.0``); a bool True renders
    ``"1"`` (ffmpeg's own spelling for e.g. ``-loop 1``); ``str`` renders
    as-is.
    """
    if spec.type == "bool":
        return "1" if value is True else None
    if spec.type in ("int", "num") and isinstance(value, int | float):
        return _render_number(value)
    return str(value)


def _render_input_options(options: dict[str, object]) -> list[str]:
    args: list[str] = []
    for name, value in options.items():
        spec = option_spec(name)
        if spec is None:  # defensive: lower validated every name it wrote
            raise _internal(f"unknown input option {name!r} reached emit")
        rendered = _render_input_option_value(spec, value)
        if rendered is None:
            continue
        args += [spec.flag, rendered]
    return args


# ---------------------------------------------------------------------------
# sink option rendering (RFC-002, plan 027)
# ---------------------------------------------------------------------------


def _copy_suppressed_scopes(options: dict[str, object]) -> set[str]:
    """Stream scopes ("video"/"audio") for which a group sets an explicit codec."""
    return {SINK_OPTIONS[name].scope for name in options if SINK_OPTIONS[name].flag == "-c"}


def _render_option_value(spec: SinkOptionSpec, value: object) -> str | None:
    """Render one option's value per its spec, or None to omit it entirely.

    A bool value of False is always omitted (e.g. plain ``faststart false``);
    every other value renders via ``spec.value_template.format(v=value)``.
    """
    if spec.type == "bool" and value is not True:
        return None
    return spec.value_template.format(v=value)


def _render_sink_options(group: OutputGroup) -> list[str]:
    args: list[str] = []
    for name, value in group.options.items():
        spec = SINK_OPTIONS[name]
        rendered = _render_option_value(spec, value)
        if rendered is None:
            continue
        if spec.per_stream:
            # Per-FILE stream indices, same as the -c/-metadata block above.
            for index, mapping in enumerate(group.maps):
                if mapping.type == spec.scope:
                    args += [f"{spec.flag}:{index}", rendered]
        else:
            args += [spec.flag, rendered]
    return args


# ---------------------------------------------------------------------------
# escaping
# ---------------------------------------------------------------------------


def _escape_value(s: str) -> str:
    """Escape `s` for use as a filter option value inside a filtergraph.

    A filtergraph description is unescaped twice: once when the graph is
    split into filters (``[ ] , ;`` separate, ``\\`` escapes, ``'`` quotes),
    then again when a filter's option string is split into ``k=v`` pairs
    (``:`` separates, ``\\`` escapes, ``'`` quotes). This applies both levels
    in that order, matching the worked example in ffmpeg's "Notes on
    filtergraph escaping": ``:`` becomes ``\\\\:``, ``'`` becomes ``\\\\\\'``,
    ``,`` becomes ``\\,``.

    Whitespace and ``#`` are backslash-escaped too: ffmpeg trims unquoted
    leading/trailing whitespace from tokens, and ``#`` can start a comment.
    Values made only of ordinary characters (alphanumerics, ``. - + * / % @``,
    expression punctuation like ``( ) < >``) contain nothing special at either
    level and therefore pass through unchanged and unquoted.
    """
    if not s:
        return "''"
    level1 = "".join(_LEVEL1_ESCAPES.get(char, char) for char in s)
    return _LEVEL2_SPECIAL.sub(lambda m: "\\" + m.group(0), level1)


# ---------------------------------------------------------------------------
# refs, labels, validation
# ---------------------------------------------------------------------------


def _internal(message: str) -> SqlmpegError:
    return SqlmpegError(
        ErrorCode.INTERNAL,
        message,
        hint="this is a compiler bug; please report the query that produced it",
    )


def _parse_node_ref(ref: FrameRef) -> tuple[str, int]:
    """Split a non-source FrameRef into ``(node_id, pad)``."""
    node_id, sep, pad_text = ref.partition(":")
    if not sep:
        return node_id, 0
    try:
        pad = int(pad_text)
    except ValueError:
        raise _internal(f"malformed frame ref {ref!r}: pad index is not an integer") from None
    if pad < 0:
        raise _internal(f"malformed frame ref {ref!r}: negative pad index")
    return node_id, pad


def _parse_src_ref(ref: FrameRef) -> tuple[str, StreamType, int]:
    try:
        alias, stream_type, index = src_parts(ref)
    except ValueError as exc:
        raise _internal(f"malformed source ref {ref!r}: {exc}") from None
    if index < 0:
        raise _internal(f"malformed source ref {ref!r}: negative stream index")
    return alias, stream_type, index


def _slot(ref: FrameRef) -> str:
    """Canonical key for the pad a FrameRef points at.

    ``"n1"`` and ``"n1:0"`` are the same pad, so both map to ``"n1:0"``;
    source refs are already canonical and keep their ``"src:<alias>:v:<k>"``
    form.
    """
    if is_src(ref):
        return ref
    node_id, pad = _parse_node_ref(ref)
    return f"{node_id}:{pad}"


def _out_pad_count(node: Node) -> int:
    count = len(node.outputs)
    if count < 1:
        raise _internal(f"node {node.id!r} declares no output pads")
    return count


def _pad_type(g: Graph, ref: FrameRef) -> StreamType:
    """Stream type of the pad `ref` points at (src marker or node.outputs[pad])."""
    if is_src(ref):
        return _parse_src_ref(ref)[1]
    node_id, pad = _parse_node_ref(ref)
    node = g.nodes.get(node_id)
    if node is None:
        raise _internal(f"ref {ref!r} names unknown node {node_id!r}")
    return node.outputs[pad]


def _src_spec(g: Graph, ref: FrameRef) -> str:
    """Render a source ref as an ffmpeg stream spec, e.g. ``0:a:1``."""
    alias, stream_type, index = _parse_src_ref(ref)
    if alias not in g.sources:
        raise _internal(f"source ref {ref!r} names unknown source alias {alias!r}")
    return f"{g.sources[alias]}:{_TYPE_MARKERS[stream_type]}:{index}"


def _verify_topological(g: Graph) -> None:
    """Check every ref resolves and points backwards; a cycle cannot pass."""
    defined: set[str] = set()
    for node_id, node in g.nodes.items():
        if node.id != node_id:
            raise _internal(f"node key {node_id!r} does not match node id {node.id!r}")
        for ref in node.inputs:
            _check_ref(g, ref, defined, f"node {node.id!r}")
        defined.add(node_id)
    if not g.outputs:
        raise _internal("graph has no outputs")
    for index, output in enumerate(g.outputs):
        if not output.ref:
            raise _internal(f"graph output {index} has an empty ref")
        _check_ref(g, output.ref, defined, f"graph output {index}")


def _check_ref(g: Graph, ref: FrameRef, defined: set[str], where: str) -> None:
    if is_src(ref):
        alias, _stream_type, _index = _parse_src_ref(ref)
        if alias not in g.sources:
            raise _internal(f"{where} references unknown source alias {alias!r}")
        input_index = g.sources[alias]
        if not 0 <= input_index < len(g.input_paths):
            raise _internal(
                f"source alias {alias!r} maps to out-of-range input index {input_index}"
            )
        return
    node_id, pad = _parse_node_ref(ref)
    if node_id not in g.nodes:
        raise _internal(f"{where} references unknown node {node_id!r}")
    if node_id not in defined:
        raise _internal(
            f"{where} references {node_id!r}, which is not defined earlier: "
            "the node graph is cyclic or not in topological order"
        )
    if pad >= _out_pad_count(g.nodes[node_id]):
        raise _internal(f"{where} references pad {pad} of {node_id!r}, which has fewer outputs")


def _check_fanout(nodes: list[Node], g: Graph) -> None:
    """Every pad has at most one consumer, except the ones that are not pads.

    Two exemptions, both about refs that are bare ``-map``s rather than
    filtergraph pads — and both mirrored in the split pass, which is what
    leaves them fanned out on purpose:

    * RFC-004: a subtitle/data source ref never enters the filtergraph, and
      repeating one is legal ffmpeg (``SELECT f.subtitle[1], f.subtitle[1]``
      writes the same track twice). There is no split filter for such a
      stream at all.
    * RFC-006: a video/audio SOURCE ref that no node consumes and that each
      output FILE maps at most once. Two output groups bare-mapping the same
      input stream is a repeated ``-map``, which is equally legal, and it is
      what lets both files stream-copy that track. A repeat within ONE group,
      or any pad a filter also consumes, is still a split-pass bug.

    Real filtergraph pads (``"<node-id>[:<pad>]"``) are consume-once in every
    direction, including across groups: two sinks reading one view's pad must
    have been split.
    """
    counts = _count_consumers(nodes, g.outputs)
    exempt = _exempt_refs(g)
    for slot, count in counts.items():
        if count > 1 and slot not in exempt:
            raise _internal(
                f"pad {slot!r} has {count} consumers; ffmpeg pads are consume-once "
                "(the split pass must run before emit)"
            )


def _is_passthrough_only(ref: FrameRef) -> bool:
    """True for a subtitle/data source ref (a bare -map, never a pad)."""
    if not is_src(ref):
        return False
    try:
        return src_parts(ref)[1] in _PASSTHROUGH_ONLY
    except ValueError:  # malformed: _parse_src_ref reports it properly later
        return False


def _exempt_refs(g: Graph) -> set[FrameRef]:
    """Source refs allowed to keep more than one consumer (see `_check_fanout`).

    Deliberately the same rule ``sqlmpeg.split._exempt_refs`` applies, stated
    independently: emit never imports the split pass, it CHECKS its output.
    """
    exempt: set[FrameRef] = set()
    filtered: set[FrameRef] = {
        ref for node in g.nodes.values() for ref in node.inputs if is_src(ref)
    }
    for unit in g.sinks:
        seen: dict[FrameRef, int] = {}
        for output in unit.outputs:
            if is_src(output.ref):
                seen[output.ref] = seen.get(output.ref, 0) + 1
        for ref, count in seen.items():
            if _is_passthrough_only(ref):
                exempt.add(ref)
            elif count == 1 and ref not in filtered:
                exempt.add(ref)
            else:
                exempt.discard(ref)
                filtered.add(ref)
    return exempt


def _count_consumers(nodes: list[Node], outputs: list[Output]) -> dict[str, int]:
    """Count consumers per pad. Every sink unit's Output is a consumer too."""
    counts: dict[str, int] = {}
    for node in nodes:
        for ref in node.inputs:
            slot = _slot(ref)
            counts[slot] = counts.get(slot, 0) + 1
    for output in outputs:
        slot = _slot(output.ref)
        counts[slot] = counts.get(slot, 0) + 1
    return counts


def _sanitize_label(node_id: str) -> str:
    label = _LABEL_UNSAFE.sub("_", node_id)
    return label or "pad"


def _assign_labels(
    nodes: list[Node], pads: dict[str, int], outputs: list[Output]
) -> dict[str, str]:
    """Map every node pad slot to its rendered label (without brackets)."""
    output_labels: dict[str, str] = {}
    for index, output in enumerate(outputs):
        if is_src(output.ref):
            continue  # passthrough: never enters the filtergraph
        output_labels[_slot(output.ref)] = f"{OUTPUT_LABEL_PREFIX}{index}"

    # Reserve every out<i> up front, passthrough indices included, so a node
    # id that happens to be "out1" can never steal an output label.
    used = {f"{OUTPUT_LABEL_PREFIX}{index}" for index in range(len(outputs))}
    labels: dict[str, str] = {}
    for node in nodes:
        count = pads[node.id]
        for pad in range(count):
            slot = f"{node.id}:{pad}"
            output_label = output_labels.get(slot)
            if output_label is not None:
                labels[slot] = output_label
                continue
            base = _sanitize_label(node.id)
            if count > 1:
                base = f"{base}{pad}"
            while base in used:
                base += "_"
            used.add(base)
            labels[slot] = base
    return labels


def _output_map(g: Graph, output: Output, labels: dict[str, str]) -> OutputMap:
    produced = _pad_type(g, output.ref)
    if produced != output.type:
        raise _internal(
            f"output {output.ref!r} is declared {output.type} but the producing "
            f"pad is {produced} (lower/split bug)"
        )
    if is_src(output.ref):
        # Passthrough: zero node consumers is guaranteed by _check_fanout,
        # which counts this Output itself as the pad's single consumer.
        return OutputMap(
            target=_src_spec(g, output.ref),
            type=output.type,
            copy=True,
            metadata=dict(output.metadata),
        )
    slot = _slot(output.ref)
    label = labels.get(slot)
    if label is None:
        raise _internal(f"no pad label was assigned for {slot!r}")
    return OutputMap(
        target=f"[{label}]",
        type=output.type,
        copy=False,
        metadata=dict(output.metadata),
    )


# ---------------------------------------------------------------------------
# chain building and rendering
# ---------------------------------------------------------------------------


def _build_chains(nodes: list[Node], pads: dict[str, int]) -> list[list[Node]]:
    chains: list[list[Node]] = []
    for node in nodes:
        if chains and _extends(chains[-1][-1], node, pads):
            chains[-1].append(node)
        else:
            chains.append([node])
    return chains


def _extends(prev: Node, node: Node, pads: dict[str, int]) -> bool:
    """True if `node` can be comma-appended after `prev` in one chain.

    `node` must directly follow `prev` (guaranteed by the caller), take
    exactly one input, and that input must be `prev`'s only output pad. Pads
    are consume-once (checked in :func:`_check_fanout`), so `node` being a
    consumer of that pad already makes it the sole consumer, and rules out the
    pad also being named by a ``Graph.outputs`` entry.
    """
    if pads[prev.id] != 1:
        return False
    if len(node.inputs) != 1:
        return False
    ref = node.inputs[0]
    if is_src(ref):
        return False
    return _slot(ref) == f"{prev.id}:0"


def _render_chain(
    chain: list[Node],
    g: Graph,
    pads: dict[str, int],
    labels: dict[str, str],
) -> str:
    head, tail = chain[0], chain[-1]
    prefix = "".join(f"[{_input_label(g, ref, labels)}]" for ref in head.inputs)
    body = ",".join(_render_filter(node) for node in chain)
    suffix = "".join(f"[{labels[f'{tail.id}:{pad}']}]" for pad in range(pads[tail.id]))
    return f"{prefix}{body}{suffix}"


def _input_label(g: Graph, ref: FrameRef, labels: dict[str, str]) -> str:
    if is_src(ref):
        return _src_spec(g, ref)
    slot = _slot(ref)
    label = labels.get(slot)
    if label is None:
        raise _internal(f"no pad label was assigned for {slot!r}")
    return label


def _render_filter(node: Node) -> str:
    if not node.args:
        return node.filter
    value_only_filter = node.filter in _SPLIT_FILTERS
    parts: list[str] = []
    for key, value in node.args.items():
        rendered = _escape_value(_render_scalar(node, key, value))
        if value_only_filter or key in _VALUE_ONLY_KEYS:
            parts.append(rendered)
        else:
            parts.append(f"{key}={rendered}")
    return f"{node.filter}={':'.join(parts)}"


def _render_number(value: int | float) -> str:
    """Render a numeric scalar: ``12.5`` -> ``"12.5"``, ``5`` -> ``"5"``.

    The one place numbers become argv/filtergraph text, so a filter argument
    and an ``-ss``/``-to`` seek time render by the same rule.
    """
    return str(value)


def _render_scalar(node: Node, key: str, value: object) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int | float):
        return _render_number(value)
    if isinstance(value, str):
        return value
    raise _internal(
        f"node {node.id!r} arg {key!r} has unrenderable type {type(value).__name__}"
    )
