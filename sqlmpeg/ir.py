"""Intermediate representation for sqlmpeg.

The IR is the load-bearing structure: golden tests assert here, not on
emitted filtergraph strings. See the "Architecture" / "IR" section of
sqlmpeg-project.md.

FrameRef grammar (authoritative)
--------------------------------
A `FrameRef` is a plain `str` and is always exactly one of the following forms:

    "src:<alias>:v:<k>"   -> a raw, typed input video stream: the k-th video
                              stream (0-BASED) of the input bound to `alias`
                              in `Graph.sources`. Renders as ffmpeg
                              `<idx>:v:<k>` where `idx = sources[alias]`.
    "src:<alias>:a:<k>"   -> same, for the k-th audio stream (0-based).
    "src:<alias>:s:<k>"   -> same, for the k-th subtitle stream (0-based).
    "src:<alias>:d:<k>"   -> same, for the k-th data stream (0-based; e.g.
                              tmcd timecode, GoPro gpmd).
    "<node-id>"           -> a Node's output pad 0 (implicit; valid for any
                              node with a single consumer, or before the
                              split pass has run).
    "<node-id>:<p>"       -> a Node's output pad p (p = 0..N-1); produced by
                              the split pass when a ref fans out to more than
                              one consumer, and by any node whose `outputs`
                              list has more than one entry (`split`/`asplit`,
                              or `concat v=1:a=1` with pad 0 = video, pad
                              1 = audio).

`k` in a source ref is the ffmpeg *per-type* stream index and is always
0-based at the IR layer; the SQL surface (`a.video[1]`, `a.audio[2]`, ...) is
1-based per Postgres array semantics, and lowering converts. Every source ref
is stream-typed and indexed -- there is no untyped `"src:<alias>"` form.

A node id must never itself look like a source ref (must not start with
`"src:"`); `is_src()` relies on this invariant.

Subtitle / data refs are PASSTHROUGH-ONLY: ffmpeg filtergraphs
carry only video/audio, so a `"src:<alias>:s:<k>"` or `"src:<alias>:d:<k>"`
ref may only appear as a `SinkUnit.outputs` entry (a bare `-map`), never as a
`Node.inputs` entry and never produced by a `Node.outputs` entry. This module
does not enforce that; it is a property of well-formed IR that parser/lower
uphold and split/emit check.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

StreamType = Literal["video", "audio", "subtitle", "data"]

FrameRef = str  # a Node.id, "<node-id>:<pad>", or "src:<alias>:v|a:<k>"

_SRC_PREFIX = "src:"


def is_src(ref: FrameRef) -> bool:
    """True if `ref` points at a raw input stream rather than a Node."""
    return ref.startswith(_SRC_PREFIX)


def src_parts(ref: FrameRef) -> tuple[str, StreamType, int]:
    """Split a source FrameRef into (alias, stream_type, index).

    `ref` must be a typed source ref, `k` a 0-based per-type ffmpeg stream
    index. Raises ValueError otherwise -- a programmer error, since every ref
    reaching here is already-validated IR, not user input.
    """
    rest = ref[len(_SRC_PREFIX):]
    alias, type_marker, index_str = rest.rsplit(":", 2)
    stream_type = _parse_type_marker(type_marker)
    return alias, stream_type, int(index_str)


def src_alias(ref: FrameRef) -> str:
    """Return the alias portion of a source FrameRef."""
    return src_parts(ref)[0]


def _parse_type_marker(marker: str) -> StreamType:
    if marker == "v":
        return "video"
    if marker == "a":
        return "audio"
    if marker == "s":
        return "subtitle"
    if marker == "d":
        return "data"
    raise ValueError(f"invalid source ref type marker {marker!r}")


def _parse_stream_type(value: object) -> StreamType:
    if value == "video":
        return "video"
    if value == "audio":
        return "audio"
    if value == "subtitle":
        return "subtitle"
    if value == "data":
        return "data"
    raise ValueError(f"invalid stream type: {value!r}")


@dataclass
class Node:
    id: str
    filter: str  # ffmpeg filter name (post macro-expansion)
    args: dict[str, object]  # normalized, SQL arg order already mapped to ffmpeg names
    inputs: list[FrameRef]
    # one entry per output pad; split/asplit have N same-type entries,
    # concat v=1:a=1 has ["video", "audio"]
    outputs: list[StreamType]

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "filter": self.filter,
            "args": dict(self.args),
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
        }

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Node:
        node_id = d["id"]
        node_filter = d["filter"]
        node_args = d["args"]
        node_inputs = d["inputs"]
        node_outputs = d["outputs"]
        assert isinstance(node_id, str)
        assert isinstance(node_filter, str)
        assert isinstance(node_args, dict)
        assert isinstance(node_inputs, list)
        assert isinstance(node_outputs, list)
        return cls(
            id=node_id,
            filter=node_filter,
            args=dict(node_args),
            inputs=[str(x) for x in node_inputs],
            outputs=[_parse_stream_type(x) for x in node_outputs],
        )


@dataclass
class Output:
    """One top-level SELECT column."""

    ref: FrameRef
    type: StreamType
    name: str | None  # SELECT ... AS name, else None
    # provenance/tags (e.g. {"language": "fra"}) -> -metadata:s:; the reserved
    # "disposition" key renders as -disposition: instead (see emit.py).
    metadata: dict[str, str]

    def to_dict(self) -> dict[str, object]:
        return {
            "ref": self.ref,
            "type": self.type,
            "name": self.name,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Output:
        raw_ref = d["ref"]
        raw_type = d["type"]
        raw_name = d["name"]
        raw_metadata = d["metadata"]
        assert isinstance(raw_ref, str)
        assert raw_name is None or isinstance(raw_name, str)
        assert isinstance(raw_metadata, dict)
        return cls(
            ref=raw_ref,
            type=_parse_stream_type(raw_type),
            name=raw_name,
            metadata={str(k): str(v) for k, v in raw_metadata.items()},
        )


@dataclass
class SinkUnit:
    """One output FILE: its stream list, its destination and its options.

    One unit per ``COPY (query) TO 'path' WITH (options)``, in script order --
    and one per surviving ROW of a fan-out ``TO (<expression>)``, in row order.

    * ``outputs`` is that COPY's top-level SELECT list, in ``-map`` order.
      Output STREAM indices are per-file in ffmpeg, so ``-c:<i>`` and
      ``-metadata:s:<i>`` are indexed within this list, not across the
      command (emit's ``OutputGroup``).
    * ``path`` is None for the bare-SELECT case (no COPY at all; ``options``
      is empty then), and for a ``COPY ... TO STDOUT WITH (FORMAT csv)`` sink
      that ends up in a media graph alongside a real COPY. Neither CLI
      subcommand invents a destination for it.
    * ``options`` is insertion-ordered with values already validated against
      `sqlmpeg.sink.SINK_OPTIONS` by lower.
    * ``tags`` are the file's CONTAINER tags, key -> value, rendered as
      ``-metadata key=value``. A None value CLEARS the key and still renders
      (``-metadata key=``): ffmpeg copies an input's globals by default.
    * ``window`` is this FILE's own ``(start, end)`` trim in seconds, rendered
      as ``-ss``/``-to`` OUTPUT options ahead of the unit's maps. Either bound
      may be None. It re-encodes every stream it covers, so emit drops the
      ``-c:<i> copy`` a passthrough map would otherwise carry: output-side
      seeks and stream copy write corrupt files. Set only by a fan-out whose
      rows carry a time window; an input-wide window lives in
      :attr:`Graph.input_trims` instead.

    Filtergraph LABELS are graph-scoped, not file-scoped: one
    ``-filter_complex`` serves every unit, so emit keeps ``out<i>`` unique
    across the whole graph (see :attr:`Graph.outputs`).
    """

    outputs: list[Output] = field(default_factory=list)
    path: str | None = None
    options: dict[str, object] = field(default_factory=dict)
    tags: dict[str, str | None] = field(default_factory=dict)
    window: tuple[float | None, float | None] | None = None

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "outputs": [output.to_dict() for output in self.outputs],
            "path": self.path,  # explicit null for the bare-SELECT case
            "options": dict(self.options),
        }
        # Emitted only when non-empty, like Graph's own optional fields: a unit
        # tagging nothing keeps the smaller shape the goldens pin.
        if self.tags:
            d["tags"] = dict(self.tags)
        if self.window is not None:
            d["window"] = [self.window[0], self.window[1]]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> SinkUnit:
        raw_outputs = d["outputs"]
        raw_path = d["path"]
        raw_options = d["options"]
        raw_tags = d.get("tags")
        raw_window = d.get("window")
        assert isinstance(raw_outputs, list)
        assert raw_path is None or isinstance(raw_path, str)
        assert isinstance(raw_options, dict)
        assert raw_tags is None or isinstance(raw_tags, dict)
        assert raw_window is None or isinstance(raw_window, list)
        outputs: list[Output] = []
        for raw_output in raw_outputs:
            assert isinstance(raw_output, dict)
            outputs.append(Output.from_dict(raw_output))
        tags: dict[str, str | None] = {}
        if raw_tags is not None:
            tags = {
                str(k): None if v is None else str(v) for k, v in raw_tags.items()
            }
        window: tuple[float | None, float | None] | None = None
        if raw_window is not None:
            start, end = raw_window
            window = (
                float(start) if start is not None else None,
                float(end) if end is not None else None,
            )
        return cls(
            outputs=outputs,
            path=raw_path,
            options=dict(raw_options),
            tags=tags,
            window=window,
        )


@dataclass
class Graph:
    input_paths: list[str]  # -i order; index is the ffmpeg input index
    sources: dict[str, int]  # alias -> index into input_paths
    nodes: dict[str, Node] = field(default_factory=dict)  # insertion-ordered
    # One unit per output FILE, in script order. Units share this
    # graph's nodes -- a view read by three COPYs, or by three files of one
    # fan-out, is lowered ONCE and fanned out by the split pass.
    sinks: list[SinkUnit] = field(default_factory=list)
    # alias -> (start, end) seconds. Emit renders "-ss <start> -to
    # <end>" immediately before that alias's -i, trimming every stream type of
    # the input coherently. Either bound may be None -- a missing start omits
    # "-ss", a missing end omits "-to" -- but never both, since a WHERE clause
    # always supplies at least one (parser._time_bounds).
    input_trims: dict[str, tuple[float | None, float | None]] = field(default_factory=dict)
    # alias -> its `input('path', name => value, ...)` options,
    # insertion-ordered, already validated against `sqlmpeg.inputs`. Emit
    # renders each input's options immediately before its own `-i`, ahead of
    # any `-ss`/`-to` from `input_trims`.
    input_options: dict[str, dict[str, object]] = field(default_factory=dict)

    @property
    def outputs(self) -> list[Output]:
        """Every unit's outputs concatenated, in sink order (read-only).

        The UNION the split pass and emit's consume-once check count over,
        since a pad feeding two output files is still consumed twice. Derived,
        not stored: an Output belongs to exactly one :class:`SinkUnit`, and
        its index HERE is an ffmpeg output stream index only for a
        single-unit graph (ffmpeg restarts stream numbering per file). Emit
        uses it for the graph-scoped ``out<i>`` labels.
        """
        return [output for unit in self.sinks for output in unit.outputs]

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "inputs": list(self.input_paths),
            "sources": dict(self.sources),
            "nodes": [node.to_dict() for node in self.nodes.values()],
            "sinks": [unit.to_dict() for unit in self.sinks],
        }
        # Emitted only when non-empty: a graph using neither keeps the smaller
        # shape the goldens pin.
        if self.input_trims:
            d["input_trims"] = {
                alias: [start, end] for alias, (start, end) in self.input_trims.items()
            }
        if self.input_options:
            d["input_options"] = {
                alias: dict(options) for alias, options in self.input_options.items()
            }
        return d

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Graph:
        raw_inputs = d["inputs"]
        raw_sources = d["sources"]
        raw_nodes = d["nodes"]
        raw_sinks = d["sinks"]
        assert isinstance(raw_inputs, list)
        assert isinstance(raw_sources, dict)
        assert isinstance(raw_nodes, list)
        assert isinstance(raw_sinks, list)

        nodes: dict[str, Node] = {}
        for raw_node in raw_nodes:
            assert isinstance(raw_node, dict)
            node = Node.from_dict(raw_node)
            nodes[node.id] = node

        sinks: list[SinkUnit] = []
        for raw_sink in raw_sinks:
            assert isinstance(raw_sink, dict)
            sinks.append(SinkUnit.from_dict(raw_sink))

        raw_input_trims = d.get("input_trims")
        input_trims: dict[str, tuple[float | None, float | None]] = {}
        if raw_input_trims is not None:
            assert isinstance(raw_input_trims, dict)
            for alias, bounds in raw_input_trims.items():
                assert isinstance(bounds, list)
                start, end = bounds
                input_trims[str(alias)] = (
                    float(start) if start is not None else None,
                    float(end) if end is not None else None,
                )

        raw_input_options = d.get("input_options")
        input_options: dict[str, dict[str, object]] = {}
        if raw_input_options is not None:
            assert isinstance(raw_input_options, dict)
            for alias, options in raw_input_options.items():
                assert isinstance(options, dict)
                input_options[str(alias)] = dict(options)

        return cls(
            input_paths=[str(p) for p in raw_inputs],
            sources={str(k): int(v) for k, v in raw_sources.items()},
            nodes=nodes,
            sinks=sinks,
            input_trims=input_trims,
            input_options=input_options,
        )


_MergeKey = tuple[str, tuple[tuple[str, object], ...]]


def _merge_key(g: Graph, index: int, aliases: list[str], trimmed: set[str]) -> _MergeKey | None:
    """This input slot's dedup key, or None if it can never merge.

    None for any slot carrying a trimmed alias -- a trim is a property of that
    alias's own ``-i``, so two windows are never folded together
    even when equal. Otherwise the key is the path plus the resolved options
    every alias on this slot set, sorted by name: two slots merge only when
    both match exactly.
    """
    if any(alias in trimmed for alias in aliases):
        return None
    options: dict[str, object] = {}
    for alias in aliases:
        options.update(g.input_options.get(alias, {}))
    return (g.input_paths[index], tuple(sorted(options.items())))


def dedup_inputs(g: Graph) -> Graph:
    """Fold untrimmed same-path same-options aliases onto one ``-i``.

    Aliases keep their names; only ``Graph.sources`` (alias -> input index)
    and ``Graph.input_paths`` are rewritten, in first-occurrence order, so
    labels/chains/fanout need no change -- they already resolve a source ref's
    input index through ``g.sources`` at render time. Returns `g` itself when
    nothing merges. Callers run it once every input option and trim window is
    known -- which is what the "no trim window" test needs.
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
