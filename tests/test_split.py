from __future__ import annotations

from sqlmpeg.ir import Graph, Node
from sqlmpeg.split import insert_splits


def _no_fanout_graph() -> Graph:
    g = Graph(input_paths=["a.mp4", "b.mp4"], sources={"a": 0, "b": 1})
    g.nodes["n0"] = Node(
        id="n0",
        filter="crop",
        args={"w": 600, "h": 200, "x": 1200, "y": 50},
        inputs=["src:b"],
    )
    g.nodes["n1"] = Node(
        id="n1",
        filter="scale",
        args={"w": "iw*0.5", "h": -2},
        inputs=["n0"],
    )
    g.nodes["n2"] = Node(
        id="n2",
        filter="overlay",
        args={"x": 20, "y": 20},
        inputs=["src:a", "n1"],
    )
    g.output = "n2"
    return g


def _node_fanout_graph() -> Graph:
    g = Graph(input_paths=["a.mp4"], sources={"a": 0})
    g.nodes["n0"] = Node(
        id="n0",
        filter="scale",
        args={"w": "iw*0.5", "h": -2},
        inputs=["src:a"],
    )
    g.nodes["n1"] = Node(id="n1", filter="hflip", args={}, inputs=["n0"])
    g.nodes["n2"] = Node(id="n2", filter="vflip", args={}, inputs=["n0"])
    g.nodes["n3"] = Node(
        id="n3",
        filter="overlay",
        args={"x": 0, "y": 0},
        inputs=["n1", "n2"],
    )
    g.output = "n3"
    return g


def _src_fanout_graph() -> Graph:
    g = Graph(input_paths=["a.mp4"], sources={"a": 0})
    g.nodes["n0"] = Node(id="n0", filter="f0", args={}, inputs=["src:a"])
    g.nodes["n1"] = Node(id="n1", filter="f1", args={}, inputs=["src:a"])
    g.nodes["n2"] = Node(id="n2", filter="f2", args={}, inputs=["src:a"])
    g.nodes["n3"] = Node(id="n3", filter="merge3", args={}, inputs=["n0", "n1", "n2"])
    g.output = "n3"
    return g


def _output_edge_fanout_graph() -> Graph:
    g = Graph(input_paths=["a.mp4"], sources={"a": 0})
    g.nodes["n0"] = Node(id="n0", filter="f0", args={}, inputs=["src:a"])
    g.nodes["n1"] = Node(id="n1", filter="f1", args={}, inputs=["n0"])
    g.output = "n0"
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

    assert out.nodes["n1"].inputs == ["n0_split:0"]
    assert out.nodes["n2"].inputs == ["n0_split:1"]
    assert out.nodes["n3"].inputs == ["n1", "n2"]
    assert out.output == "n3"


def test_src_fanout_inserts_split_and_rewires() -> None:
    g = _src_fanout_graph()
    out = insert_splits(g)

    assert list(out.nodes.keys()) == ["src_a_split", "n0", "n1", "n2", "n3"]

    split_node = out.nodes["src_a_split"]
    assert split_node.filter == "split"
    assert split_node.args == {"n": 3}
    assert split_node.inputs == ["src:a"]

    assert out.nodes["n0"].inputs == ["src_a_split:0"]
    assert out.nodes["n1"].inputs == ["src_a_split:1"]
    assert out.nodes["n2"].inputs == ["src_a_split:2"]
    assert out.output == "n3"


def test_output_edge_counted_as_a_consumer() -> None:
    g = _output_edge_fanout_graph()
    out = insert_splits(g)

    assert list(out.nodes.keys()) == ["n0", "n0_split", "n1"]

    split_node = out.nodes["n0_split"]
    assert split_node.filter == "split"
    assert split_node.args == {"n": 2}
    assert split_node.inputs == ["n0"]

    # node consumer (n1) is rewired before the output, per node-insertion
    # order with output treated as the last consumer.
    assert out.nodes["n1"].inputs == ["n0_split:0"]
    assert out.output == "n0_split:1"


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
