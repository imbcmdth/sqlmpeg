from __future__ import annotations

from sqlmpeg.errors import ErrorCode, SqlmpegError
from sqlmpeg.ir import Graph, Node, is_src, src_alias


def _build_graph() -> Graph:
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


def test_graph_round_trip() -> None:
    g = _build_graph()
    d1 = g.to_dict()
    g2 = Graph.from_dict(d1)
    d2 = g2.to_dict()
    assert d1 == d2


def test_graph_to_dict_shape() -> None:
    g = _build_graph()
    d = g.to_dict()
    assert d["inputs"] == ["a.mp4", "b.mp4"]
    assert d["sources"] == {"a": 0, "b": 1}
    assert d["output"] == "n2"

    nodes = d["nodes"]
    assert isinstance(nodes, list)
    assert [n["id"] for n in nodes] == ["n0", "n1", "n2"]
    for n in nodes:
        assert set(n.keys()) == {"id", "filter", "args", "inputs"}


def test_is_src_and_src_alias() -> None:
    assert is_src("src:a") is True
    assert is_src("n0") is False
    assert src_alias("src:a") == "a"
    assert src_alias("src:pip") == "pip"


def test_sqlmpeg_error_to_dict_matches_spec_shape() -> None:
    err = SqlmpegError(
        ErrorCode.UDF_ARG_TYPE,
        "overlay() expects (frame, frame, int, int), got (frame, varchar, int, int)",
        line=3,
        col=12,
        hint="did you mean to wrap the second argument in input()?",
    )
    assert err.to_dict() == {
        "line": 3,
        "col": 12,
        "code": "UDF_ARG_TYPE",
        "message": "overlay() expects (frame, frame, int, int), got (frame, varchar, int, int)",
        "hint": "did you mean to wrap the second argument in input()?",
    }


def test_sqlmpeg_error_to_dict_with_missing_fields() -> None:
    err = SqlmpegError(ErrorCode.INTERNAL, "unexpected state")
    d = err.to_dict()
    assert d["line"] is None
    assert d["col"] is None
    assert d["hint"] is None
    assert d["code"] == "INTERNAL"
    assert d["message"] == "unexpected state"


def test_sqlmpeg_error_str_full() -> None:
    err = SqlmpegError(
        ErrorCode.UDF_ARG_TYPE,
        "bad args",
        line=3,
        col=12,
        hint="try this",
    )
    assert str(err) == "line 3:12: UDF_ARG_TYPE: bad args (hint: try this)"


def test_sqlmpeg_error_str_no_position_no_hint() -> None:
    err = SqlmpegError(ErrorCode.INTERNAL, "boom")
    assert str(err) == "INTERNAL: boom"
