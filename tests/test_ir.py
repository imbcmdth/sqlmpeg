from __future__ import annotations

import pytest

from sqlmpeg.errors import ErrorCode, SqlmpegError
from sqlmpeg.ir import Graph, Node, Output, is_src, src_alias, src_parts


def _build_graph() -> Graph:
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
    g.outputs = [
        Output(ref="n2", type="video", name=None, metadata={}),
        Output(
            ref="src:a:a:0",
            type="audio",
            name="eng_audio",
            metadata={"language": "eng"},
        ),
    ]
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

    nodes = d["nodes"]
    assert isinstance(nodes, list)
    assert [n["id"] for n in nodes] == ["n0", "n1", "n2"]
    for n in nodes:
        assert set(n.keys()) == {"id", "filter", "args", "inputs", "outputs"}
    assert nodes[0]["outputs"] == ["video"]

    outputs = d["outputs"]
    assert isinstance(outputs, list)
    assert len(outputs) == 2
    for o in outputs:
        assert set(o.keys()) == {"ref", "type", "name", "metadata"}
    assert outputs[0] == {
        "ref": "n2",
        "type": "video",
        "name": None,
        "metadata": {},
    }
    assert outputs[1] == {
        "ref": "src:a:a:0",
        "type": "audio",
        "name": "eng_audio",
        "metadata": {"language": "eng"},
    }


def test_graph_outputs_default_empty() -> None:
    g = Graph(input_paths=["a.mp4"], sources={"a": 0})
    assert g.outputs == []
    assert g.to_dict()["outputs"] == []


def test_node_outputs_multi_pad() -> None:
    n = Node(
        id="n0",
        filter="split",
        args={"n": 2},
        inputs=["src:a:v:0"],
        outputs=["video", "video"],
    )
    d = n.to_dict()
    assert d["outputs"] == ["video", "video"]
    n2 = Node.from_dict(d)
    assert n2.outputs == ["video", "video"]


def test_is_src() -> None:
    assert is_src("src:a:v:0") is True
    assert is_src("src:pip:a:1") is True
    assert is_src("n0") is False
    assert is_src("n0:1") is False


def test_src_alias() -> None:
    assert src_alias("src:a:v:0") == "a"
    assert src_alias("src:pip:a:12") == "pip"


def test_src_parts_video() -> None:
    assert src_parts("src:a:v:0") == ("a", "video", 0)


def test_src_parts_audio() -> None:
    assert src_parts("src:a:a:2") == ("a", "audio", 2)


def test_src_parts_invalid_type_marker_raises() -> None:
    with pytest.raises(ValueError):
        src_parts("src:a:x:0")


def test_output_round_trip_metadata() -> None:
    o = Output(
        ref="src:a:a:0",
        type="audio",
        name=None,
        metadata={"language": "fra", "title": "Director commentary"},
    )
    d = o.to_dict()
    o2 = Output.from_dict(d)
    assert o2 == o


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


def test_new_v2_error_codes_exist() -> None:
    assert ErrorCode.STREAM_NOT_FOUND.value == "STREAM_NOT_FOUND"
    assert ErrorCode.INPUT_NOT_FOUND.value == "INPUT_NOT_FOUND"
    assert ErrorCode.BROADCAST_MISMATCH.value == "BROADCAST_MISMATCH"
