"""Intermediate representation for sqlmpeg.

The IR is the load-bearing structure: golden tests assert here, not on
emitted filtergraph strings. See the "Architecture" / "IR" section of
sqlmpeg-project.md.

FrameRef grammar (v2, RFC-001 "stream-aware" — authoritative statement; split.py's
module docstring held the v1 grammar and will be brought in line with this one by
plan 017; RFC-004 widened the marker set to include `s`/`d` — see below)
------------------------------------------------------------------------------
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
                              the split pass (plan 007/017) when a ref fans
                              out to more than one consumer, and by any node
                              whose `outputs` list has more than one entry
                              (e.g. `split`/`asplit`, or a `concat v=1:a=1`
                              node with pad 0 = video, pad 1 = audio).

`k` in a source ref is the ffmpeg *per-type* stream index and is always
0-based at the IR layer; the SQL surface (`a.video[1]`, `a.audio[2]`, ...) is
1-based per Postgres array semantics, and lowering (plan 019) converts.

v0/v1's untyped `"src:<alias>"` (no `:v:`/`:a:` suffix, no index) is RETIRED
in v2 — every source ref is now stream-typed and indexed. `is_src()` still
recognizes any ref starting with the `"src:"` prefix; `src_alias()` and the
new `src_parts()` parse the typed form.

A node id must never itself look like a source ref (i.e. must not start with
`"src:"`); this invariant is relied on by `is_src()` and is unchanged from
v1.

Subtitle / data refs are PASSTHROUGH-ONLY (RFC-004): ffmpeg filtergraphs
carry only video/audio, so a `"src:<alias>:s:<k>"` or `"src:<alias>:d:<k>"`
ref may only ever appear as a `Graph.outputs` entry (a bare `-map`), never as
a `Node.inputs` entry and never produced by a `Node.outputs` entry. This
module does not enforce that constraint -- it is a property of well-formed
IR that later passes (parser/lower in wave 2, split/emit exemptions in wave
3) are responsible for upholding and checking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

StreamType = Literal["video", "audio", "subtitle", "data"]

FrameRef = str  # a Node.id, "<node-id>:<pad>", or "src:<alias>:v|a:<k>"

_SRC_PREFIX = "src:"


def is_src(ref: FrameRef) -> bool:
    """True if `ref` points at a raw input stream rather than a Node."""
    return ref.startswith(_SRC_PREFIX)


def src_parts(ref: FrameRef) -> tuple[str, StreamType, int]:
    """Split a source FrameRef into (alias, stream_type, index).

    `ref` must be of the form "src:<alias>:v:<k>" or "src:<alias>:a:<k>",
    with `k` a 0-based, per-type ffmpeg stream index. Raises ValueError if
    `ref` is not a well-formed typed source ref (this is a programmer error —
    every ref reaching this function is expected to already be validated
    IR, not user input).
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
    outputs: list[StreamType]  # one entry per output pad; ["video"] typical.
    # split/asplit: N same-type entries. concat v=1:a=1: ["video", "audio"].

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
    metadata: dict[str, str]  # provenance (e.g. {"language": "fra"}) -> -metadata:s:

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
class Sink:
    """One `COPY (query) TO 'path' WITH (options)` destination (RFC-002).

    ``options`` is insertion-ordered and its values are already normalized
    (validated against `sqlmpeg.sink.SINK_OPTIONS` by lower -- plan 026).
    """

    path: str
    options: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "options": dict(self.options),
        }

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Sink:
        raw_path = d["path"]
        raw_options = d["options"]
        assert isinstance(raw_path, str)
        assert isinstance(raw_options, dict)
        return cls(path=raw_path, options=dict(raw_options))


@dataclass
class Graph:
    input_paths: list[str]  # -i order; index is the ffmpeg input index
    sources: dict[str, int]  # alias -> index into input_paths
    nodes: dict[str, Node] = field(default_factory=dict)  # insertion-ordered
    outputs: list[Output] = field(default_factory=list)  # order = -map order
    sink: Sink | None = None  # COPY ... TO ... WITH (...); None for bare SELECT
    # RFC-004 input-seek amendment: alias -> (start, end) seconds. Emit (wave
    # 3) renders "-ss <start> -to <end>" immediately before that alias's -i,
    # trimming every stream type of that input coherently. Empty by default;
    # to_dict emits the "input_trims" key only when non-empty (same
    # golden-compat pattern as "sink" above). Plan 039 (open-ended windows):
    # either bound may be None -- a missing start omits "-ss", a missing end
    # omits "-to" -- but not both (a WHERE clause always supplies at least
    # one bound; see parser._time_bounds).
    input_trims: dict[str, tuple[float | None, float | None]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "inputs": list(self.input_paths),
            "sources": dict(self.sources),
            "nodes": [node.to_dict() for node in self.nodes.values()],
            "outputs": [output.to_dict() for output in self.outputs],
        }
        # "sink" and "input_trims" are emitted ONLY when present/non-empty,
        # so every existing golden .ir.json stays byte-identical -- these
        # changes are additive.
        if self.sink is not None:
            d["sink"] = self.sink.to_dict()
        if self.input_trims:
            d["input_trims"] = {
                alias: [start, end] for alias, (start, end) in self.input_trims.items()
            }  # None bounds render as JSON null, same as any other field
        return d

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Graph:
        raw_inputs = d["inputs"]
        raw_sources = d["sources"]
        raw_nodes = d["nodes"]
        raw_outputs = d["outputs"]
        assert isinstance(raw_inputs, list)
        assert isinstance(raw_sources, dict)
        assert isinstance(raw_nodes, list)
        assert isinstance(raw_outputs, list)

        nodes: dict[str, Node] = {}
        for raw_node in raw_nodes:
            assert isinstance(raw_node, dict)
            node = Node.from_dict(raw_node)
            nodes[node.id] = node

        outputs: list[Output] = []
        for raw_output in raw_outputs:
            assert isinstance(raw_output, dict)
            outputs.append(Output.from_dict(raw_output))

        raw_sink = d.get("sink")
        sink: Sink | None = None
        if raw_sink is not None:
            assert isinstance(raw_sink, dict)
            sink = Sink.from_dict(raw_sink)

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

        return cls(
            input_paths=[str(p) for p in raw_inputs],
            sources={str(k): int(v) for k, v in raw_sources.items()},
            nodes=nodes,
            outputs=outputs,
            sink=sink,
            input_trims=input_trims,
        )
