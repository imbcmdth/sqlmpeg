"""Split pass for sqlmpeg IR graphs.

The SQL surface is a DAG: an alias or CTE may be referenced more than once
(a fork), and each of the graph's Output rows itself counts as a consumer.
FFmpeg filtergraph pads, however, are consume-once -- a pad can only feed one
downstream input. This pass reconciles the two by finding every FrameRef with
more than one consumer and splicing an ffmpeg `split`/`asplit` filter node in
front of it, so each consumer gets its own dedicated output pad.

See ir.py's module docstring for the authoritative FrameRef grammar. This
pass is *a* producer of the pad-qualified `"<node-id>:<k>"` form -- for refs
it splits -- but not the only one: a node whose `outputs` list already has
more than one entry (e.g. a `concat v=1:a=1` node) produces that form
upstream of this pass, on its own.

Algorithm
---------
1. Count consumers of every FrameRef: each entry in every Node.inputs list,
   plus each `Output.ref` in `g.outputs` (one count per Output, so two
   Outputs referencing the same ref count as 2 consumers).
2. For any ref consumed N > 1 times, insert a new split node in front of it:
   `filter="split"` if the ref carries video, `filter="asplit"` if audio
   (see below for how that's determined), `args={"n": N}`, `inputs=[ref]`,
   `outputs=[type] * N`. Its id is `<sanitized-ref>_split`, where sanitizing
   replaces every ":" with "_" (e.g. "src:a:v:0" -> "src_a_v_0_split", "n3"
   -> "n3_split").
3. Rewire each consumer of that ref to `"<split-id>:<k>"` for k = 0..N-1, in
   deterministic order: node insertion order, walking each node's inputs
   left to right, then `g.outputs` in list order (rewired last).
4. Split nodes are inserted immediately before their first consumer, which
   keeps the resulting `nodes` dict topologically ordered (a split node's
   own input -- an existing node or a source -- is always already present
   earlier in the dict, since the original graph is itself topologically
   ordered).

A ref's stream type -- needed to pick `split` vs `asplit` and to fill in the
new node's `outputs` -- is resolved from the *original* graph `g`: a source
ref's type is `src_parts(ref)[1]`; a node ref's type is `node.outputs[pad]`
(pad 0 for the unqualified `"<node-id>"` form).

`insert_splits` is a pure function: it builds and returns a new Graph and
never mutates its input. It is idempotent -- every ref in the output graph
has exactly one consumer, so running it again is a no-op.
"""

from __future__ import annotations

from .ir import FrameRef, Graph, Node, Output, StreamType, is_src, src_parts


def _ref_type(g: Graph, ref: FrameRef) -> StreamType:
    """Resolve the stream type (video/audio) that `ref` carries in `g`."""
    if is_src(ref):
        return src_parts(ref)[1]
    if ":" in ref:
        node_id, pad_str = ref.rsplit(":", 1)
        pad = int(pad_str)
    else:
        node_id, pad = ref, 0
    return g.nodes[node_id].outputs[pad]


def insert_splits(g: Graph) -> Graph:
    """Splice `split`/`asplit` nodes in front of every FrameRef with fan-out > 1.

    Pure: returns a new Graph; `g` is left unmodified. Idempotent: calling
    this again on the result is a no-op.
    """
    counts: dict[FrameRef, int] = {}
    for node in g.nodes.values():
        for ref in node.inputs:
            counts[ref] = counts.get(ref, 0) + 1
    for output in g.outputs:
        counts[output.ref] = counts.get(output.ref, 0) + 1

    new_nodes: dict[str, Node] = {}
    split_ids: dict[FrameRef, str] = {}
    next_pad: dict[FrameRef, int] = {}

    def rewire(ref: FrameRef) -> FrameRef:
        if counts.get(ref, 0) <= 1:
            return ref
        split_id = split_ids.get(ref)
        if split_id is None:
            ref_type = _ref_type(g, ref)
            n = counts[ref]
            split_id = f"{ref.replace(':', '_')}_split"
            split_ids[ref] = split_id
            next_pad[ref] = 0
            new_nodes[split_id] = Node(
                id=split_id,
                filter="split" if ref_type == "video" else "asplit",
                args={"n": n},
                inputs=[ref],
                outputs=[ref_type] * n,
            )
        pad = next_pad[ref]
        next_pad[ref] = pad + 1
        return f"{split_id}:{pad}"

    for node in g.nodes.values():
        new_inputs = [rewire(ref) for ref in node.inputs]
        new_nodes[node.id] = Node(
            id=node.id,
            filter=node.filter,
            args=dict(node.args),
            inputs=new_inputs,
            outputs=list(node.outputs),
        )

    new_outputs = [
        Output(
            ref=rewire(output.ref),
            type=output.type,
            name=output.name,
            metadata=dict(output.metadata),
        )
        for output in g.outputs
    ]

    return Graph(
        input_paths=list(g.input_paths),
        sources=dict(g.sources),
        nodes=new_nodes,
        outputs=new_outputs,
        # This pass rewrites the graph's SHAPE; the sink is a property of the
        # whole job and passes through untouched (it is already validated).
        sink=g.sink,
    )
