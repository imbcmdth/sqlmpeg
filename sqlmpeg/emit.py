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

Outputs and passthrough
-----------------------
``Graph.outputs`` is the output stream list: one ``Output`` per top-level
SELECT column, in ``-map`` order. Each one becomes an :class:`OutputMap`:

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

    [1:v:0]crop=w=600:h=200:x=1200:y=50,scale=w=iw*0.5:h=-2[n2];[0:v:0][n2]overlay=x=20:y=20[out0]

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
from dataclasses import dataclass

from .errors import ErrorCode, SqlmpegError
from .ir import FrameRef, Graph, Node, Output, StreamType, is_src, src_parts

OUTPUT_LABEL_PREFIX = "out"
"""Filtered outputs are labelled ``out0``, ``out1``, ... (without brackets)."""

_SPLIT_FILTERS = frozenset({"split", "asplit"})
_VALUE_ONLY_KEYS = frozenset({"expr"})

_TYPE_MARKERS: dict[StreamType, str] = {"video": "v", "audio": "a"}

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
class Emitted:
    inputs: list[str]  # file paths in -i order
    filter_complex: str  # "" when every output is a passthrough
    maps: list[OutputMap]  # one per Graph.outputs entry, same order


def emit(g: Graph) -> Emitted:
    """Render `g` as an ffmpeg filtergraph plus its output map list.

    Raises ``SqlmpegError(INTERNAL)`` if the graph is malformed: a cycle or
    non-topological node ordering, a dangling FrameRef, no outputs, or a pad
    with more than one consumer (which means the split pass did not run --
    an ``Output`` counts as a consumer, so a pad feeding both a node and an
    output, or two outputs naming the same pad, is a split-pass bug).
    """
    _verify_topological(g)

    nodes = list(g.nodes.values())
    pads = {node.id: _out_pad_count(node) for node in nodes}
    _check_fanout(nodes, g.outputs)
    labels = _assign_labels(nodes, pads, g.outputs)

    chains = _build_chains(nodes, pads)
    filter_complex = ";".join(_render_chain(chain, g, pads, labels) for chain in chains)

    maps = [_output_map(g, output, labels) for output in g.outputs]

    return Emitted(
        inputs=list(g.input_paths),
        filter_complex=filter_complex,
        maps=maps,
    )


def build_ffmpeg_args(e: Emitted, out_path: str) -> list[str]:
    """Full ffmpeg argv for `e`, writing to `out_path`.

    The SELECT list is authoritative: exactly one ``-map`` per
    :class:`OutputMap`, in order, with ``-c:<i> copy`` for passthrough streams
    and ``-metadata:s:<i> k=v`` (keys sorted) for provenance metadata. v0's
    implicit ``-map 0:a? -c:a copy`` tail is gone. ``-filter_complex`` is
    omitted when the graph is pure passthrough.
    """
    args = ["ffmpeg"]
    for path in e.inputs:
        args += ["-i", path]
    if e.filter_complex:
        args += ["-filter_complex", e.filter_complex]
    for index, mapping in enumerate(e.maps):
        args += ["-map", mapping.target]
        if mapping.copy:
            args += [f"-c:{index}", "copy"]
        for key in sorted(mapping.metadata):
            args += [f"-metadata:s:{index}", f"{key}={mapping.metadata[key]}"]
    args.append(out_path)
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


def _check_fanout(nodes: list[Node], outputs: list[Output]) -> None:
    counts = _count_consumers(nodes, outputs)
    for slot, count in counts.items():
        if count > 1:
            raise _internal(
                f"pad {slot!r} has {count} consumers; ffmpeg pads are consume-once "
                "(the split pass must run before emit)"
            )


def _count_consumers(nodes: list[Node], outputs: list[Output]) -> dict[str, int]:
    """Count consumers per pad. A ``Graph.outputs`` entry is a consumer too."""
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


def _render_scalar(node: Node, key: str, value: object) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str):
        return value
    raise _internal(
        f"node {node.id!r} arg {key!r} has unrenderable type {type(value).__name__}"
    )
