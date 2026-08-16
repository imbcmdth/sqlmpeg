from __future__ import annotations

import pytest

from sqlmpeg.errors import ErrorCode, SqlmpegError
from sqlmpeg.ir import Graph, Node, Output, Sink, is_src, src_alias, src_parts


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


def test_sink_error_codes_exist() -> None:
    assert ErrorCode.UNKNOWN_SINK_OPTION.value == "UNKNOWN_SINK_OPTION"
    assert ErrorCode.SINK_OPTION_TYPE.value == "SINK_OPTION_TYPE"


# ---------------------------------------------------------------------------
# Sink (plan 025)
# ---------------------------------------------------------------------------


def test_sink_round_trip() -> None:
    s = Sink(
        path="out.mkv",
        options={"video_codec": "libx264", "crf": 20, "faststart": True},
    )
    d = s.to_dict()
    s2 = Sink.from_dict(d)
    assert s2 == s
    assert d == {
        "path": "out.mkv",
        "options": {"video_codec": "libx264", "crf": 20, "faststart": True},
    }


def test_graph_without_sink_to_dict_has_no_sink_key() -> None:
    g = _build_graph()
    assert g.sink is None
    d = g.to_dict()
    assert "sink" not in d


def test_graph_with_sink_round_trip() -> None:
    g = _build_graph()
    g.sink = Sink(path="out.mp4", options={"video_codec": "libx264"})
    d1 = g.to_dict()
    assert "sink" in d1
    assert d1["sink"] == {"path": "out.mp4", "options": {"video_codec": "libx264"}}
    g2 = Graph.from_dict(d1)
    assert g2.sink == g.sink
    d2 = g2.to_dict()
    assert d1 == d2


def test_graph_from_dict_tolerates_missing_sink_key() -> None:
    g = _build_graph()
    d = g.to_dict()
    assert "sink" not in d
    g2 = Graph.from_dict(d)
    assert g2.sink is None


# ---------------------------------------------------------------------------
# subtitle / data stream types + src refs (RFC-004, plan 033)
# ---------------------------------------------------------------------------


def test_src_parts_subtitle() -> None:
    assert src_parts("src:a:s:0") == ("a", "subtitle", 0)


def test_src_parts_data() -> None:
    assert src_parts("src:a:d:3") == ("a", "data", 3)


def test_output_round_trip_subtitle() -> None:
    o = Output(
        ref="src:s:s:0",
        type="subtitle",
        name=None,
        metadata={"language": "eng"},
    )
    d = o.to_dict()
    o2 = Output.from_dict(d)
    assert o2 == o
    assert d["type"] == "subtitle"


def test_output_round_trip_data() -> None:
    o = Output(ref="src:a:d:0", type="data", name=None, metadata={})
    d = o.to_dict()
    o2 = Output.from_dict(d)
    assert o2 == o
    assert d["type"] == "data"


def test_output_from_dict_rejects_invalid_stream_type() -> None:
    with pytest.raises(ValueError):
        Output.from_dict(
            {"ref": "src:a:x:0", "type": "bogus", "name": None, "metadata": {}}
        )


def test_node_outputs_reject_subtitle_data_type_strings_are_still_parseable() -> None:
    # Node.outputs is typed StreamType too -- subtitle/data parse, even though
    # by RFC-004's passthrough-only rule no node ever legitimately declares
    # them (that constraint is enforced by later passes, not by ir.py).
    n = Node(
        id="n0",
        filter="noop",
        args={},
        inputs=["src:a:s:0"],
        outputs=["subtitle"],
    )
    d = n.to_dict()
    n2 = Node.from_dict(d)
    assert n2.outputs == ["subtitle"]


# ---------------------------------------------------------------------------
# Graph.input_trims (RFC-004 input-seek amendment, plan 033)
# ---------------------------------------------------------------------------


def test_graph_input_trims_defaults_empty() -> None:
    g = _build_graph()
    assert g.input_trims == {}
    assert "input_trims" not in g.to_dict()


def test_graph_input_trims_round_trip() -> None:
    g = _build_graph()
    g.input_trims = {"a": (5.0, 60.0)}
    d1 = g.to_dict()
    assert d1["input_trims"] == {"a": [5.0, 60.0]}
    g2 = Graph.from_dict(d1)
    assert g2.input_trims == {"a": (5.0, 60.0)}
    d2 = g2.to_dict()
    assert d1 == d2


def test_graph_input_trims_multiple_aliases_round_trip() -> None:
    g = _build_graph()
    g.input_trims = {"a": (0.0, 10.0), "b": (2.5, 30.25)}
    d = g.to_dict()
    g2 = Graph.from_dict(d)
    assert g2.input_trims == {"a": (0.0, 10.0), "b": (2.5, 30.25)}


def test_graph_from_dict_tolerates_missing_input_trims_key() -> None:
    g = _build_graph()
    d = g.to_dict()
    assert "input_trims" not in d
    g2 = Graph.from_dict(d)
    assert g2.input_trims == {}


# ---------------------------------------------------------------------------
# Graph.input_trims open-ended windows (plan 039)
# ---------------------------------------------------------------------------


def test_graph_input_trims_open_upper_round_trips_as_json_null() -> None:
    g = _build_graph()
    g.input_trims = {"a": (5.0, None)}
    d = g.to_dict()
    assert d["input_trims"] == {"a": [5.0, None]}
    g2 = Graph.from_dict(d)
    assert g2.input_trims == {"a": (5.0, None)}
    assert g2.to_dict() == d


def test_graph_input_trims_open_lower_round_trips_as_json_null() -> None:
    g = _build_graph()
    g.input_trims = {"a": (None, 60.0)}
    d = g.to_dict()
    assert d["input_trims"] == {"a": [None, 60.0]}
    g2 = Graph.from_dict(d)
    assert g2.input_trims == {"a": (None, 60.0)}
    assert g2.to_dict() == d


# ---------------------------------------------------------------------------
# Graph.input_options (RFC-005 SS4, plan 041)
# ---------------------------------------------------------------------------


def test_graph_input_options_defaults_empty() -> None:
    g = _build_graph()
    assert g.input_options == {}
    assert "input_options" not in g.to_dict()


def test_graph_input_options_round_trip() -> None:
    g = _build_graph()
    g.input_options = {"a": {"loop": True, "framerate": 15}}
    d1 = g.to_dict()
    assert d1["input_options"] == {"a": {"loop": True, "framerate": 15}}
    g2 = Graph.from_dict(d1)
    assert g2.input_options == {"a": {"loop": True, "framerate": 15}}
    d2 = g2.to_dict()
    assert d1 == d2


def test_graph_input_options_multiple_aliases_round_trip() -> None:
    g = _build_graph()
    g.input_options = {"a": {"loop": True}, "b": {"hwaccel": "cuda"}}
    d = g.to_dict()
    g2 = Graph.from_dict(d)
    assert g2.input_options == {"a": {"loop": True}, "b": {"hwaccel": "cuda"}}


def test_graph_from_dict_tolerates_missing_input_options_key() -> None:
    g = _build_graph()
    d = g.to_dict()
    assert "input_options" not in d
    g2 = Graph.from_dict(d)
    assert g2.input_options == {}


def test_graph_input_options_are_independent_of_sink_and_input_trims() -> None:
    """The three "additive, omit-when-empty" fields do not interfere."""
    g = _build_graph()
    g.input_options = {"a": {"itsoffset": -1.5}}
    g.input_trims = {"a": (0.0, 2.0)}
    d = g.to_dict()
    assert d["input_options"] == {"a": {"itsoffset": -1.5}}
    assert d["input_trims"] == {"a": [0.0, 2.0]}
    assert "sink" not in d
    g2 = Graph.from_dict(d)
    assert g2.input_options == g.input_options
    assert g2.input_trims == g.input_trims
