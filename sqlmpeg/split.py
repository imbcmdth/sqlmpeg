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
   plus each `Output.ref` in `g.outputs` -- the UNION of every sink unit's
   outputs (RFC-006), because a pad feeding two different output FILES is
   still consumed twice. One count per Output, so two Outputs referencing the
   same ref count as 2 consumers, whichever sinks they belong to.
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

Passthrough-only exemption (RFC-004)
------------------------------------
Subtitle and data source refs are EXEMPT from all of the above: they never
enter the filtergraph, so there is no pad to fan out and no `split` filter
that could do it (ffmpeg has none for subtitles). A `"src:<alias>:s:<k>"` or
`"src:<alias>:d:<k>"` ref referenced by two Outputs passes through untouched
and emit renders the same bare `-map` twice, which is legal ffmpeg. Such a ref
can only ever appear in a sink unit's outputs (ir.py's grammar note), so
exempting it here cannot leave a real filtergraph pad over-consumed.

Cross-GROUP passthrough exemption (RFC-006)
-------------------------------------------
The same argument extends, one step further, to a VIDEO/AUDIO source ref that
no filter node consumes and that each sink unit maps at most once: repeating
`-map 0:a:0` in two different OUTPUT FILES is exactly as legal as repeating a
subtitle map, and it is what keeps both files stream-COPYING that track
instead of routing it through an `asplit` and re-encoding it. So:

* zero node consumers AND at most one Output per sink unit -> exempt, no
  split, emit allows the repeat;
* anything else -- a filter node consumes it too, or one single unit maps it
  twice (`SELECT a.audio[1], a.audio[1]` into ONE file) -- goes through the
  ordinary split, counted over every consumer as before.

The consume-once rule for real filtergraph pads (`"<node-id>[:<pad>]"`) stays
strict in every direction: two sinks reading one view's pad DO get a split.

`insert_splits` is a pure function: it builds and returns a new Graph and
never mutates its input. It is idempotent -- every ref in the output graph
has exactly one consumer, so running it again is a no-op. Everything that is
not part of the graph's pad SHAPE -- each sink unit's `path`/`options`,
`Graph.input_trims`, `Graph.input_options` -- is copied to the new Graph
verbatim.
"""

from __future__ import annotations

from .ir import FrameRef, Graph, Node, Output, SinkUnit, StreamType, is_src, src_parts

# Stream types a filtergraph cannot carry, and therefore cannot split.
_PASSTHROUGH_ONLY: frozenset[StreamType] = frozenset({"subtitle", "data"})


def _is_passthrough_only(ref: FrameRef) -> bool:
    """True for a subtitle/data source ref -- never a filtergraph pad."""
    return is_src(ref) and src_parts(ref)[1] in _PASSTHROUGH_ONLY


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


def _exempt_refs(g: Graph) -> set[FrameRef]:
    """Source refs that may stay fanned out: the bare `-map`s (RFC-004/006).

    A subtitle/data source ref always qualifies -- no filtergraph carries it.
    A video/audio source ref qualifies when NO node consumes it and no single
    sink unit maps it more than once, i.e. when every one of its consumers is
    a passthrough `-map` in a different output FILE.
    """
    exempt: set[FrameRef] = set()
    filtered: set[FrameRef] = {
        ref for node in g.nodes.values() for ref in node.inputs if is_src(ref)
    }
    for unit in g.sinks:
        seen: dict[FrameRef, int] = {}
        for output in unit.outputs:
            if not is_src(output.ref):
                continue
            seen[output.ref] = seen.get(output.ref, 0) + 1
        for ref, count in seen.items():
            if _is_passthrough_only(ref):
                exempt.add(ref)
            elif count == 1 and ref not in filtered:
                exempt.add(ref)
            else:
                exempt.discard(ref)
                filtered.add(ref)  # a within-file repeat disqualifies it here too
    return exempt


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
    exempt = _exempt_refs(g)

    new_nodes: dict[str, Node] = {}
    split_ids: dict[FrameRef, str] = {}
    next_pad: dict[FrameRef, int] = {}

    def rewire(ref: FrameRef) -> FrameRef:
        if counts.get(ref, 0) <= 1:
            return ref
        if ref in exempt:
            # Repeating a bare -map is legal ffmpeg; there is no pad to fan
            # out (subtitle/data), or the repeats are one per output FILE.
            # See the module docstring.
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

    # Sink units are rewired in order, and each unit's outputs in list order,
    # so the pad a consumer gets stays deterministic across the whole graph.
    new_sinks = [
        SinkUnit(
            outputs=[
                Output(
                    ref=rewire(output.ref),
                    type=output.type,
                    name=output.name,
                    metadata=dict(output.metadata),
                )
                for output in unit.outputs
            ],
            path=unit.path,
            options=dict(unit.options),
        )
        for unit in g.sinks
    ]

    return Graph(
        input_paths=list(g.input_paths),
        sources=dict(g.sources),
        nodes=new_nodes,
        sinks=new_sinks,
        # This pass rewrites the graph's SHAPE; each sink's path/options, the
        # input trims and the input options are properties of the whole job --
        # of the output files and of the `-i` entries respectively, none of
        # which is a filtergraph pad -- so all three pass through untouched
        # (they are already validated).
        input_trims=dict(g.input_trims),
        input_options={alias: dict(options) for alias, options in g.input_options.items()},
    )
