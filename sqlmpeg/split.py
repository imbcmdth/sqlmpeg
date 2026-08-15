"""Split pass for sqlmpeg IR graphs.

The SQL surface is a DAG: an alias or CTE may be referenced more than once
(a fork), and the graph's single output slot itself counts as a consumer.
FFmpeg filtergraph pads, however, are consume-once -- a pad can only feed one
downstream input. This pass reconciles the two by finding every FrameRef with
more than one consumer and splicing an ffmpeg `split` filter node in front of
it, so each consumer gets its own dedicated output pad.

FrameRef grammar
-----------------
This pass is the sole producer of pad-qualified refs, so the following
grammar is authoritative for every later pass (notably `emit`, plan 007):

    "<node-id>"       -> the node's output pad 0 (implicit; the only form
                          used before this pass runs, and still valid
                          afterwards for any node with a single consumer)
    "<node-id>:<k>"   -> the node's output pad k (k = 0..N-1); only produced
                          by this pass, as a consumer-facing rewrite of a
                          fanned-out ref into a freshly inserted `split=N`
                          node
    "src:<alias>"     -> a raw input stream identified by `alias` (as in
                          ir.py; may itself be split if it fans out, in
                          which case its consumers are rewritten to
                          "<alias-derived-split-id>:<k>", NOT "src:alias:k")

A FrameRef is therefore always exactly one of the three forms above.
`is_src()` / `src_alias()` in ir.py keep working unmodified: they only look
at the "src:" prefix, and a pad-qualified rewrite of a source ref (e.g.
"src:a" fanning out to "src_a_split:0") is a plain node ref -- the "src:"
prefix is consumed by the rewrite, not preserved alongside the pad suffix.

Algorithm
---------
1. Count consumers of every FrameRef: each entry in every Node.inputs list,
   plus `g.output` (counted once, as the last consumer).
2. For any ref consumed N > 1 times, insert a new
   Node(filter="split", args={"n": N}, inputs=[ref]) with id
   `<sanitized-ref>_split`, where sanitizing replaces ":" with "_"
   (e.g. "src:a" -> "src_a_split", "n3" -> "n3_split").
3. Rewire each consumer of that ref to `"<split-id>:<k>"` for k = 0..N-1, in
   deterministic order: node insertion order, walking each node's inputs
   left to right, with `g.output` rewired last.
4. Split nodes are inserted immediately before their first consumer, which
   keeps the resulting `nodes` dict topologically ordered (a split node's
   own input -- an existing node or a source -- is always already present
   earlier in the dict, since the original graph is itself topologically
   ordered).

`insert_splits` is a pure function: it builds and returns a new Graph and
never mutates its input. It is idempotent -- every ref in the output graph
has exactly one consumer, so running it again is a no-op.
"""

from __future__ import annotations

from .ir import FrameRef, Graph, Node


def insert_splits(g: Graph) -> Graph:
    """Splice `split=N` nodes in front of every FrameRef with fan-out > 1.

    Pure: returns a new Graph; `g` is left unmodified. Idempotent: calling
    this again on the result is a no-op.
    """
    counts: dict[FrameRef, int] = {}
    for node in g.nodes.values():
        for ref in node.inputs:
            counts[ref] = counts.get(ref, 0) + 1
    counts[g.output] = counts.get(g.output, 0) + 1

    new_nodes: dict[str, Node] = {}
    split_ids: dict[FrameRef, str] = {}
    next_pad: dict[FrameRef, int] = {}

    def rewire(ref: FrameRef) -> FrameRef:
        if counts.get(ref, 0) <= 1:
            return ref
        split_id = split_ids.get(ref)
        if split_id is None:
            split_id = f"{ref.replace(':', '_')}_split"
            split_ids[ref] = split_id
            next_pad[ref] = 0
            new_nodes[split_id] = Node(
                id=split_id,
                filter="split",
                args={"n": counts[ref]},
                inputs=[ref],
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
        )

    new_output = rewire(g.output)

    return Graph(
        input_paths=list(g.input_paths),
        sources=dict(g.sources),
        nodes=new_nodes,
        output=new_output,
    )
