"""Emit pass: IR ``Graph`` -> ffmpeg ``-filter_complex`` string and argv.

This is pass 4 of the compiler (see "Architecture" in sqlmpeg-project.md).
It assumes the graph has already been through the split pass, so every pad
has exactly one consumer.

FrameRef grammar consumed here (authoritative source: ``split.py``)::

    "<node-id>"      -> output pad 0 of that node
    "<node-id>:<k>"  -> output pad k of that node (only split nodes have k>0)
    "src:<alias>"    -> a raw input stream; rendered as "[<index>:v]"

Pad label scheme
----------------
Every node output pad gets exactly one label, derived from the node id
(sanitized: any character outside ``[A-Za-z0-9_]`` becomes ``_``):

* single-output node  -> ``[<id>]``            e.g. ``[n2]``
* multi-output node   -> ``[<id><k>]``         e.g. a ``split=2`` node whose
  id is ``n1_split`` produces ``[n1_split0][n1_split1]``, so a full chain
  reads ``[n1]split=2[n1_split0][n1_split1]``
* the pad named by ``Graph.output`` -> ``[out]``, whichever node/pad it is

``out`` is reserved up front, and any label collision is broken by appending
``_`` until the label is unique. Labels of pads that are consumed inside a
merged comma-chain are never rendered (that is what chain merging means).

Chain merging
-------------
Nodes are walked in graph (topological) order. A node extends the chain built
so far when it is the *sole* consumer of the immediately preceding node's only
output pad and has no other inputs; such runs are joined with ``,`` and only
the head's input labels and the tail's output labels are rendered. Chains are
joined with ``;`` (no whitespace). The README example therefore emits::

    [1:v]crop=w=600:h=200:x=1200:y=50,scale=w=iw*0.5:h=-2[n2];[0:v][n2]overlay=x=20:y=20[out]

Argument rendering
------------------
``args`` renders in dict insertion order as ``filter=k1=v1:k2=v2``. Special
cases: a filter with no args renders bare (``hflip``, no ``=``); the key
``"expr"`` renders value-only (``setpts=PTS-STARTPTS``); every arg of a
``split`` node renders value-only (``split=2``). ``concat`` needs nothing
special: ``{"n": 2, "v": 1, "a": 0}`` -> ``concat=n=2:v=1:a=0``.

Escaping
--------
All values go through :func:`_escape_value` -- the single place where ffmpeg
filtergraph escaping happens (drawtext ``text=`` included).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import ErrorCode, SqlmpegError
from .ir import FrameRef, Graph, Node, is_src, src_alias

OUTPUT_LABEL = "out"
"""Label given to the pad named by ``Graph.output`` (without brackets)."""

_PASSTHROUGH_FILTER = "null"
_SPLIT_FILTER = "split"
_VALUE_ONLY_KEYS = frozenset({"expr"})

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
class Emitted:
    inputs: list[str]  # file paths in -i order
    filter_complex: str
    output_label: str  # label WITHOUT brackets, e.g. "out"


def emit(g: Graph) -> Emitted:
    """Render `g` as an ffmpeg filtergraph.

    Raises ``SqlmpegError(INTERNAL)`` if the graph is malformed: a cycle or
    non-topological node ordering, a dangling FrameRef, or a pad with more
    than one consumer (which means the split pass did not run).
    """
    _verify_topological(g)

    nodes = list(g.nodes.values())
    output_ref = g.output
    if is_src(output_ref):
        # Pass-through query: ffmpeg still needs a filter to hang [out] on.
        passthrough = Node(
            id=_fresh_id("passthrough", g.nodes),
            filter=_PASSTHROUGH_FILTER,
            args={},
            inputs=[output_ref],
        )
        nodes.append(passthrough)
        output_ref = passthrough.id

    pads = {node.id: _out_pad_count(node) for node in nodes}
    _check_fanout(nodes, output_ref)
    labels = _assign_labels(nodes, pads, output_ref)

    chains = _build_chains(nodes, pads)
    filter_complex = ";".join(_render_chain(chain, g, pads, labels) for chain in chains)

    return Emitted(
        inputs=list(g.input_paths),
        filter_complex=filter_complex,
        output_label=OUTPUT_LABEL,
    )


def build_ffmpeg_args(e: Emitted, out_path: str) -> list[str]:
    """Full ffmpeg argv for `e`, writing to `out_path`.

    Audio is copied from the first input (spec: v0 SQL is video-only); the
    ``?`` in ``0:a?`` makes the mapping tolerate a silent input.
    """
    args = ["ffmpeg"]
    for path in e.inputs:
        args += ["-i", path]
    args += [
        "-filter_complex",
        e.filter_complex,
        "-map",
        f"[{e.output_label}]",
        "-map",
        "0:a?",
        "-c:a",
        "copy",
        out_path,
    ]
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


def _slot(ref: FrameRef) -> str:
    """Canonical key for the pad a FrameRef points at.

    ``"n1"`` and ``"n1:0"`` are the same pad, so both map to ``"n1:0"``;
    source refs keep their ``"src:<alias>"`` form.
    """
    if is_src(ref):
        return ref
    node_id, pad = _parse_node_ref(ref)
    return f"{node_id}:{pad}"


def _out_pad_count(node: Node) -> int:
    if node.filter != _SPLIT_FILTER:
        return 1
    n = node.args.get("n")
    if not isinstance(n, int) or isinstance(n, bool) or n < 2:
        raise _internal(f"split node {node.id!r} needs an integer arg n >= 2, got {n!r}")
    return n


def _verify_topological(g: Graph) -> None:
    """Check every ref resolves and points backwards; a cycle cannot pass."""
    defined: set[str] = set()
    for node_id, node in g.nodes.items():
        if node.id != node_id:
            raise _internal(f"node key {node_id!r} does not match node id {node.id!r}")
        for ref in node.inputs:
            _check_ref(g, ref, defined, f"node {node.id!r}")
        defined.add(node_id)
    if not g.output:
        raise _internal("graph has no output ref")
    _check_ref(g, g.output, defined, "graph output")


def _check_ref(g: Graph, ref: FrameRef, defined: set[str], where: str) -> None:
    if is_src(ref):
        alias = src_alias(ref)
        if alias not in g.sources:
            raise _internal(f"{where} references unknown source alias {alias!r}")
        index = g.sources[alias]
        if not 0 <= index < len(g.input_paths):
            raise _internal(f"source alias {alias!r} maps to out-of-range input index {index}")
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


def _check_fanout(nodes: list[Node], output_ref: FrameRef) -> None:
    counts = _count_consumers(nodes, output_ref)
    for slot, count in counts.items():
        if count > 1:
            raise _internal(
                f"pad {slot!r} has {count} consumers; ffmpeg pads are consume-once "
                "(the split pass must run before emit)"
            )


def _count_consumers(nodes: list[Node], output_ref: FrameRef) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in nodes:
        for ref in node.inputs:
            slot = _slot(ref)
            counts[slot] = counts.get(slot, 0) + 1
    out_slot = _slot(output_ref)
    counts[out_slot] = counts.get(out_slot, 0) + 1
    return counts


def _fresh_id(base: str, taken: dict[str, Node]) -> str:
    candidate = base
    suffix = 0
    while candidate in taken:
        suffix += 1
        candidate = f"{base}{suffix}"
    return candidate


def _sanitize_label(node_id: str) -> str:
    label = _LABEL_UNSAFE.sub("_", node_id)
    return label or "pad"


def _assign_labels(nodes: list[Node], pads: dict[str, int], output_ref: FrameRef) -> dict[str, str]:
    out_slot = _slot(output_ref)
    used: set[str] = {OUTPUT_LABEL}
    labels: dict[str, str] = {}
    for node in nodes:
        count = pads[node.id]
        for pad in range(count):
            slot = f"{node.id}:{pad}"
            if slot == out_slot:
                labels[slot] = OUTPUT_LABEL
                continue
            base = _sanitize_label(node.id)
            if count > 1:
                base = f"{base}{pad}"
            while base in used:
                base += "_"
            used.add(base)
            labels[slot] = base
    return labels


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
    pad also being the graph output.
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
        return f"{g.sources[src_alias(ref)]}:v"
    slot = _slot(ref)
    label = labels.get(slot)
    if label is None:
        raise _internal(f"no pad label was assigned for {slot!r}")
    return label


def _render_filter(node: Node) -> str:
    if not node.args:
        return node.filter
    value_only_filter = node.filter == _SPLIT_FILTER
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
