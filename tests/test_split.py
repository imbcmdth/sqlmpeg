from __future__ import annotations

from sqlmpeg.ir import Graph, Node, Output, SinkUnit, StreamType
from sqlmpeg.split import insert_splits


def _out(ref: str, type_: StreamType = "video", name: str | None = None) -> Output:
    return Output(ref=ref, type=type_, name=name, metadata={})


def _no_fanout_graph() -> Graph:
    g = Graph(input_paths=["a.mp4", "b.mp4"], sources={"a": 0, "b": 1})
    g.nodes["n0"] = Node(
        id="n0",
        filter="crop",
        args={"w": 600, "h": 200, "x": 1200, "y": 50},
        inputs=["src:b:v:0"],
        outputs=["video"],
    )
    g.nodes["n1"] = Node(
        id="n1",
        filter="scale",
        args={"w": "iw*0.5", "h": -2},
        inputs=["n0"],
        outputs=["video"],
    )
    g.nodes["n2"] = Node(
        id="n2",
        filter="overlay",
        args={"x": 20, "y": 20},
        inputs=["src:a:v:0", "n1"],
        outputs=["video"],
    )
    g.sinks = [SinkUnit(outputs=[_out("n2")])]
    return g


def _node_fanout_graph() -> Graph:
    """A video node ref (n0) fans out to two consumers -> "split"."""
    g = Graph(input_paths=["a.mp4"], sources={"a": 0})
    g.nodes["n0"] = Node(
        id="n0",
        filter="scale",
        args={"w": "iw*0.5", "h": -2},
        inputs=["src:a:v:0"],
        outputs=["video"],
    )
    g.nodes["n1"] = Node(id="n1", filter="hflip", args={}, inputs=["n0"], outputs=["video"])
    g.nodes["n2"] = Node(id="n2", filter="vflip", args={}, inputs=["n0"], outputs=["video"])
    g.nodes["n3"] = Node(
        id="n3",
        filter="overlay",
        args={"x": 0, "y": 0},
        inputs=["n1", "n2"],
        outputs=["video"],
    )
    g.sinks = [SinkUnit(outputs=[_out("n3")])]
    return g


def _audio_fanout_graph() -> Graph:
    """An audio node ref (n0) fans out to two consumers -> "asplit"."""
    g = Graph(input_paths=["a.mp4"], sources={"a": 0})
    g.nodes["n0"] = Node(
        id="n0", filter="volume", args={"volume": 1.5}, inputs=["src:a:a:0"], outputs=["audio"]
    )
    g.nodes["n1"] = Node(id="n1", filter="highpass", args={}, inputs=["n0"], outputs=["audio"])
    g.nodes["n2"] = Node(id="n2", filter="lowpass", args={}, inputs=["n0"], outputs=["audio"])
    g.nodes["n3"] = Node(id="n3", filter="amix", args={}, inputs=["n1", "n2"], outputs=["audio"])
    g.sinks = [SinkUnit(outputs=[_out("n3", "audio")])]
    return g


def _src_fanout_graph() -> Graph:
    """A typed video src ref fans out to three consumers -> "split"."""
    g = Graph(input_paths=["a.mp4"], sources={"a": 0})
    g.nodes["n0"] = Node(id="n0", filter="f0", args={}, inputs=["src:a:v:0"], outputs=["video"])
    g.nodes["n1"] = Node(id="n1", filter="f1", args={}, inputs=["src:a:v:0"], outputs=["video"])
    g.nodes["n2"] = Node(id="n2", filter="f2", args={}, inputs=["src:a:v:0"], outputs=["video"])
    g.nodes["n3"] = Node(
        id="n3", filter="merge3", args={}, inputs=["n0", "n1", "n2"], outputs=["video"]
    )
    g.sinks = [SinkUnit(outputs=[_out("n3")])]
    return g


def _output_edge_fanout_graph() -> Graph:
    """n0 is consumed by a node (n1) and directly by the sole Output."""
    g = Graph(input_paths=["a.mp4"], sources={"a": 0})
    g.nodes["n0"] = Node(id="n0", filter="f0", args={}, inputs=["src:a:v:0"], outputs=["video"])
    g.nodes["n1"] = Node(id="n1", filter="f1", args={}, inputs=["n0"], outputs=["video"])
    g.sinks = [SinkUnit(outputs=[_out("n0")])]
    return g


def _mixed_video_audio_graph() -> Graph:
    """One video fan-out and one audio fan-out coexist in the same graph."""
    g = Graph(input_paths=["a.mp4"], sources={"a": 0})
    g.nodes["v0"] = Node(id="v0", filter="scale", args={}, inputs=["src:a:v:0"], outputs=["video"])
    g.nodes["v1"] = Node(id="v1", filter="hflip", args={}, inputs=["v0"], outputs=["video"])
    g.nodes["v2"] = Node(id="v2", filter="vflip", args={}, inputs=["v0"], outputs=["video"])
    g.nodes["v3"] = Node(
        id="v3", filter="overlay", args={}, inputs=["v1", "v2"], outputs=["video"]
    )
    g.nodes["a0"] = Node(
        id="a0", filter="volume", args={}, inputs=["src:a:a:0"], outputs=["audio"]
    )
    g.nodes["a1"] = Node(id="a1", filter="highpass", args={}, inputs=["a0"], outputs=["audio"])
    g.nodes["a2"] = Node(id="a2", filter="lowpass", args={}, inputs=["a0"], outputs=["audio"])
    g.nodes["a3"] = Node(id="a3", filter="amix", args={}, inputs=["a1", "a2"], outputs=["audio"])
    g.sinks = [SinkUnit(outputs=[_out("v3", "video"), _out("a3", "audio")])]
    return g


def _multi_output_graph() -> Graph:
    """n0 is consumed by a node (n1) and by two separate Output rows."""
    g = Graph(input_paths=["a.mp4"], sources={"a": 0})
    g.nodes["n0"] = Node(id="n0", filter="scale", args={}, inputs=["src:a:v:0"], outputs=["video"])
    g.nodes["n1"] = Node(id="n1", filter="hflip", args={}, inputs=["n0"], outputs=["video"])
    g.sinks = [SinkUnit(outputs=[_out("n0", name="orig"), _out("n0", name="dup")])]
    return g


def test_no_fanout_graph_is_unchanged() -> None:
    g = _no_fanout_graph()
    out = insert_splits(g)
    assert out.to_dict() == g.to_dict()


def test_insert_splits_does_not_mutate_input_graph() -> None:
    g = _node_fanout_graph()
    before = g.to_dict()
    insert_splits(g)
    after = g.to_dict()
    assert after == before
    # sanity: this graph does have fan-out, so a no-op result would be
    # a weak test -- confirm the *returned* graph actually differs.
    assert insert_splits(g).to_dict() != before


def test_node_fanout_inserts_split_and_rewires_in_insertion_order() -> None:
    g = _node_fanout_graph()
    out = insert_splits(g)

    assert list(out.nodes.keys()) == ["n0", "n0_split", "n1", "n2", "n3"]

    split_node = out.nodes["n0_split"]
    assert split_node.filter == "split"
    assert split_node.args == {"n": 2}
    assert split_node.inputs == ["n0"]
    assert split_node.outputs == ["video", "video"]

    assert out.nodes["n1"].inputs == ["n0_split:0"]
    assert out.nodes["n2"].inputs == ["n0_split:1"]
    assert out.nodes["n3"].inputs == ["n1", "n2"]
    assert out.outputs == [_out("n3")]


def test_audio_fanout_inserts_asplit() -> None:
    g = _audio_fanout_graph()
    out = insert_splits(g)

    assert list(out.nodes.keys()) == ["n0", "n0_split", "n1", "n2", "n3"]

    split_node = out.nodes["n0_split"]
    assert split_node.filter == "asplit"
    assert split_node.args == {"n": 2}
    assert split_node.inputs == ["n0"]
    assert split_node.outputs == ["audio", "audio"]

    assert out.nodes["n1"].inputs == ["n0_split:0"]
    assert out.nodes["n2"].inputs == ["n0_split:1"]
    assert out.outputs == [_out("n3", "audio")]


def test_src_fanout_inserts_split_and_rewires() -> None:
    g = _src_fanout_graph()
    out = insert_splits(g)

    assert list(out.nodes.keys()) == ["src_a_v_0_split", "n0", "n1", "n2", "n3"]

    split_node = out.nodes["src_a_v_0_split"]
    assert split_node.filter == "split"
    assert split_node.args == {"n": 3}
    assert split_node.inputs == ["src:a:v:0"]
    assert split_node.outputs == ["video", "video", "video"]

    assert out.nodes["n0"].inputs == ["src_a_v_0_split:0"]
    assert out.nodes["n1"].inputs == ["src_a_v_0_split:1"]
    assert out.nodes["n2"].inputs == ["src_a_v_0_split:2"]
    assert out.outputs == [_out("n3")]


def test_output_edge_counted_as_a_consumer() -> None:
    g = _output_edge_fanout_graph()
    out = insert_splits(g)

    assert list(out.nodes.keys()) == ["n0", "n0_split", "n1"]

    split_node = out.nodes["n0_split"]
    assert split_node.filter == "split"
    assert split_node.args == {"n": 2}
    assert split_node.inputs == ["n0"]

    # node consumer (n1) is rewired before the output, per node-insertion
    # order with outputs rewired last.
    assert out.nodes["n1"].inputs == ["n0_split:0"]
    assert out.outputs == [_out("n0_split:1")]


def test_mixed_video_and_audio_fanout() -> None:
    g = _mixed_video_audio_graph()
    out = insert_splits(g)

    assert list(out.nodes.keys()) == [
        "v0",
        "v0_split",
        "v1",
        "v2",
        "v3",
        "a0",
        "a0_split",
        "a1",
        "a2",
        "a3",
    ]

    v_split = out.nodes["v0_split"]
    assert v_split.filter == "split"
    assert v_split.outputs == ["video", "video"]

    a_split = out.nodes["a0_split"]
    assert a_split.filter == "asplit"
    assert a_split.outputs == ["audio", "audio"]

    assert out.nodes["v1"].inputs == ["v0_split:0"]
    assert out.nodes["v2"].inputs == ["v0_split:1"]
    assert out.nodes["a1"].inputs == ["a0_split:0"]
    assert out.nodes["a2"].inputs == ["a0_split:1"]
    assert out.outputs == [_out("v3", "video"), _out("a3", "audio")]


def test_multi_output_graph_shares_a_split() -> None:
    """Two Output rows referencing the same node ref as another node -> one
    split node feeds all three consumers, node first, then outputs in list
    order."""
    g = _multi_output_graph()
    out = insert_splits(g)

    assert list(out.nodes.keys()) == ["n0", "n0_split", "n1"]

    split_node = out.nodes["n0_split"]
    assert split_node.filter == "split"
    assert split_node.args == {"n": 3}
    assert split_node.inputs == ["n0"]
    assert split_node.outputs == ["video", "video", "video"]

    # node consumer (n1) gets pad 0; outputs get pads 1, 2 in list order.
    assert out.nodes["n1"].inputs == ["n0_split:0"]
    assert out.outputs == [
        _out("n0_split:1", name="orig"),
        _out("n0_split:2", name="dup"),
    ]


def test_idempotent() -> None:
    g = _node_fanout_graph()
    once = insert_splits(g)
    twice = insert_splits(once)
    assert once.to_dict() == twice.to_dict()


def test_idempotent_on_src_fanout() -> None:
    g = _src_fanout_graph()
    once = insert_splits(g)
    twice = insert_splits(once)
    assert once.to_dict() == twice.to_dict()


def test_idempotent_on_mixed_fanout() -> None:
    g = _mixed_video_audio_graph()
    once = insert_splits(g)
    twice = insert_splits(once)
    assert once.to_dict() == twice.to_dict()


def test_idempotent_on_multi_output_graph() -> None:
    g = _multi_output_graph()
    once = insert_splits(g)
    twice = insert_splits(once)
    assert once.to_dict() == twice.to_dict()


# ---------------------------------------------------------------------------
# subtitle/data refs are exempt -- they are never filtergraph pads
# ---------------------------------------------------------------------------


def _duplicate_subtitle_graph() -> Graph:
    """One subtitle src ref named by TWO Outputs (legal ffmpeg)."""
    g = Graph(input_paths=["a.mkv"], sources={"a": 0})
    g.sinks = [
        SinkUnit(
            outputs=[
                _out("src:a:s:0", "subtitle", name="caps"),
                _out("src:a:s:0", "subtitle", name="caps_again"),
            ]
        )
    ]
    return g


def test_duplicate_subtitle_ref_is_not_split() -> None:
    out = insert_splits(_duplicate_subtitle_graph())
    assert out.nodes == {}
    assert out.outputs == [
        _out("src:a:s:0", "subtitle", name="caps"),
        _out("src:a:s:0", "subtitle", name="caps_again"),
    ]


def test_duplicate_data_ref_is_not_split() -> None:
    g = Graph(input_paths=["a.mkv"], sources={"a": 0})
    g.sinks = [SinkUnit(outputs=[_out("src:a:d:0", "data"), _out("src:a:d:0", "data")])]
    out = insert_splits(g)
    assert out.nodes == {}
    assert [o.ref for o in out.outputs] == ["src:a:d:0", "src:a:d:0"]


def test_subtitle_exemption_does_not_disturb_a_video_fanout_in_the_same_graph() -> None:
    """A video ref alongside the duplicated captions still splits normally."""
    g = Graph(input_paths=["a.mkv"], sources={"a": 0})
    g.nodes["n0"] = Node(
        id="n0", filter="hflip", args={}, inputs=["src:a:v:0"], outputs=["video"]
    )
    g.nodes["n1"] = Node(
        id="n1", filter="vflip", args={}, inputs=["src:a:v:0"], outputs=["video"]
    )
    g.sinks = [
        SinkUnit(
            outputs=[
                _out("n0"),
                _out("n1"),
                _out("src:a:s:0", "subtitle"),
                _out("src:a:s:0", "subtitle"),
            ]
        )
    ]
    out = insert_splits(g)
    assert out.nodes["src_a_v_0_split"].outputs == ["video", "video"]
    assert out.nodes["n0"].inputs == ["src_a_v_0_split:0"]
    assert out.nodes["n1"].inputs == ["src_a_v_0_split:1"]
    assert [o.ref for o in out.outputs[2:]] == ["src:a:s:0", "src:a:s:0"]


def test_idempotent_on_duplicate_subtitle_refs() -> None:
    g = _duplicate_subtitle_graph()
    once = insert_splits(g)
    twice = insert_splits(once)
    assert once.to_dict() == twice.to_dict()


# ---------------------------------------------------------------------------
# several sinks, and the cross-GROUP passthrough exemption
# ---------------------------------------------------------------------------


def _two_sink_passthrough_graph() -> Graph:
    """The same audio src ref bare-mapped by TWO output files."""
    g = Graph(input_paths=["a.mp4"], sources={"a": 0})
    g.sinks = [
        SinkUnit(outputs=[_out("src:a:a:0", "audio")], path="one.m4a"),
        SinkUnit(outputs=[_out("src:a:a:0", "audio")], path="two.m4a"),
    ]
    return g


def test_a_source_stream_mapped_once_per_file_is_not_split() -> None:
    """Repeating `-map 0:a:0` in a second output FILE is legal ffmpeg, and it
    keeps both files stream-copying the track."""
    out = insert_splits(_two_sink_passthrough_graph())
    assert out.nodes == {}
    assert [unit.outputs[0].ref for unit in out.sinks] == ["src:a:a:0", "src:a:a:0"]


def test_the_cross_group_exemption_is_idempotent() -> None:
    once = insert_splits(_two_sink_passthrough_graph())
    assert once.to_dict() == insert_splits(once).to_dict()


def test_a_source_stream_mapped_twice_in_ONE_file_is_still_split() -> None:
    """Within a single output the exemption does not apply: that is the
    ordinary fan-out the pass has always split."""
    g = Graph(input_paths=["a.mp4"], sources={"a": 0})
    g.sinks = [
        SinkUnit(
            outputs=[_out("src:a:a:0", "audio"), _out("src:a:a:0", "audio")],
            path="one.m4a",
        )
    ]
    out = insert_splits(g)
    assert out.nodes["src_a_a_0_split"].outputs == ["audio", "audio"]
    assert [o.ref for o in out.outputs] == ["src_a_a_0_split:0", "src_a_a_0_split:1"]


def test_a_source_stream_a_filter_also_reads_is_still_split() -> None:
    """One group filters it, another bare-maps it: no exemption, it is a pad."""
    g = Graph(input_paths=["a.mp4"], sources={"a": 0})
    g.nodes["n0"] = Node(
        id="n0", filter="volume", args={}, inputs=["src:a:a:0"], outputs=["audio"]
    )
    g.sinks = [
        SinkUnit(outputs=[_out("n0", "audio")], path="one.m4a"),
        SinkUnit(outputs=[_out("src:a:a:0", "audio")], path="two.m4a"),
    ]
    out = insert_splits(g)
    assert out.nodes["src_a_a_0_split"].outputs == ["audio", "audio"]
    assert out.nodes["n0"].inputs == ["src_a_a_0_split:0"]
    assert out.sinks[1].outputs[0].ref == "src_a_a_0_split:1"


def test_one_node_pad_read_by_two_sinks_is_split() -> None:
    """A real filtergraph pad is consume-once across FILES too."""
    g = Graph(input_paths=["a.mp4"], sources={"a": 0})
    g.nodes["n0"] = Node(
        id="n0", filter="scale", args={}, inputs=["src:a:v:0"], outputs=["video"]
    )
    g.sinks = [
        SinkUnit(outputs=[_out("n0")], path="one.mp4"),
        SinkUnit(outputs=[_out("n0")], path="two.mp4"),
    ]
    out = insert_splits(g)
    assert out.nodes["n0_split"].outputs == ["video", "video"]
    assert [unit.outputs[0].ref for unit in out.sinks] == ["n0_split:0", "n0_split:1"]


def test_sink_paths_and_options_survive_the_split_pass() -> None:
    g = Graph(input_paths=["a.mp4"], sources={"a": 0})
    g.nodes["n0"] = Node(
        id="n0", filter="scale", args={}, inputs=["src:a:v:0"], outputs=["video"]
    )
    g.sinks = [
        SinkUnit(outputs=[_out("n0")], path="one.mp4", options={"crf": 20}),
        SinkUnit(outputs=[_out("n0")], path="two.mp4", options={"crf": 30}),
    ]
    out = insert_splits(g)
    assert [(unit.path, unit.options) for unit in out.sinks] == [
        ("one.mp4", {"crf": 20}),
        ("two.mp4", {"crf": 30}),
    ]
    out.sinks[0].options["crf"] = 99  # purity: copied, not shared
    assert g.sinks[0].options == {"crf": 20}


def test_container_tags_survive_the_split_pass() -> None:
    g = Graph(input_paths=["a.mp4"], sources={"a": 0})
    g.nodes["n0"] = Node(
        id="n0", filter="scale", args={}, inputs=["src:a:v:0"], outputs=["video"]
    )
    g.sinks = [
        SinkUnit(outputs=[_out("n0")], path="one.mp4", tags={"title": "One"}),
        SinkUnit(outputs=[_out("n0")], path="two.mp4", tags={"artist": None}),
    ]
    out = insert_splits(g)
    assert [unit.tags for unit in out.sinks] == [{"title": "One"}, {"artist": None}]
    out.sinks[0].tags["title"] = "changed"  # purity: copied, not shared
    assert g.sinks[0].tags == {"title": "One"}


# ---------------------------------------------------------------------------
# input seek: input_trims are not part of the pad shape
# ---------------------------------------------------------------------------


def test_input_trims_survive_the_split_pass() -> None:
    """This pass rewrites pads; a window belongs to an `-i`, so it passes
    through verbatim (the same rule `sink` follows)."""
    g = _no_fanout_graph()
    g.input_trims = {"a": (1.5, 4.0), "b": (0, 2)}
    out = insert_splits(g)
    assert out.input_trims == {"a": (1.5, 4.0), "b": (0, 2)}
    assert out.nodes.keys() == g.nodes.keys()  # nothing else changed either


def test_input_trims_are_copied_not_shared() -> None:
    """Purity: mutating the result must not reach back into the input graph."""
    g = _no_fanout_graph()
    g.input_trims = {"b": (0.0, 2.0)}
    out = insert_splits(g)
    out.input_trims["b"] = (9.0, 9.0)
    assert g.input_trims == {"b": (0.0, 2.0)}


def test_input_trims_survive_a_graph_that_does_split() -> None:
    g = _node_fanout_graph()
    g.input_trims = {"a": (0, 3)}
    out = insert_splits(g)
    assert out.nodes["n0_split"].outputs == ["video", "video"]
    assert out.input_trims == {"a": (0, 3)}


# ---------------------------------------------------------------------------
# input options: not part of the pad shape either
# ---------------------------------------------------------------------------


def test_input_options_survive_the_split_pass() -> None:
    """This pass rewrites pads; an option set belongs to an `-i`, so it passes
    through verbatim (the same rule `sink`/`input_trims` follow)."""
    g = _no_fanout_graph()
    g.input_options = {"a": {"loop": True}, "b": {"framerate": 15}}
    out = insert_splits(g)
    assert out.input_options == {"a": {"loop": True}, "b": {"framerate": 15}}
    assert out.nodes.keys() == g.nodes.keys()  # nothing else changed either


def test_input_options_are_copied_not_shared() -> None:
    """Purity: mutating the result must not reach back into the input graph."""
    g = _no_fanout_graph()
    g.input_options = {"b": {"loop": True}}
    out = insert_splits(g)
    out.input_options["b"]["loop"] = False
    assert g.input_options == {"b": {"loop": True}}


def test_input_options_survive_a_graph_that_does_split() -> None:
    g = _node_fanout_graph()
    g.input_options = {"a": {"stream_loop": -1}}
    out = insert_splits(g)
    assert out.nodes["n0_split"].outputs == ["video", "video"]
    assert out.input_options == {"a": {"stream_loop": -1}}


# ---------------------------------------------------------------------------
# generated sources: a zero-input node fans out like any other
# ---------------------------------------------------------------------------


def test_a_zero_input_node_fans_out_through_an_ordinary_split() -> None:
    """`FROM ffmpeg.testsrc(...) t` lowers to `inputs=[]` and is minted ONCE
    per alias, so two consumers of it are this pass's ordinary business -- a
    `split` in front, not a second generator."""
    g = Graph(input_paths=[], sources={})
    g.nodes["n1"] = Node(
        id="n1", filter="testsrc", args={"duration": 2}, inputs=[], outputs=["video"]
    )
    g.nodes["n2"] = Node(
        id="n2", filter="hflip", args={}, inputs=["n1"], outputs=["video"]
    )
    g.sinks = [SinkUnit(outputs=[_out("n2"), _out("n1")])]

    out = insert_splits(g)

    assert list(out.nodes) == ["n1", "n1_split", "n2"]
    assert out.nodes["n1"].inputs == []
    assert out.nodes["n1_split"].inputs == ["n1"]
    assert out.nodes["n1_split"].outputs == ["video", "video"]
    assert out.nodes["n2"].inputs == ["n1_split:0"]
    assert [o.ref for o in out.outputs] == ["n2", "n1_split:1"]
