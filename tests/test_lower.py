"""Tests for the lower pass and the compiler pipeline (plans 005, 019, 020).

These go through the real parser: lowering is only ever handed a ``Resolved``
that ``resolve`` accepted, so hand-built inputs would test a shape that cannot
occur. ``compile_sql`` is used wherever the split pass is irrelevant or wanted;
``lower`` is called directly when a test needs the pre-split graph or a
synthetic :class:`~sqlmpeg.probe.ProbeResult`.

Paths in these queries deliberately do not exist, so ``compile_sql``'s
opportunistic probing degrades to symbolic lowering without shelling out
(RFC-001 "Probing policy"); probe-dependent behavior — which is ALL of
broadcasting, since an array's length comes from the file — is exercised
either with a hand-built ``ProbeResult`` through ``lower`` directly, or, for
the real thing, in an ``exec``-marked test against ``tests/fixtures/av.mp4``
(1 audio track) and ``tests/fixtures/av2.mp4`` (2 language-tagged tracks).
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from sqlmpeg import compiler
from sqlmpeg import lower as lower_module
from sqlmpeg.compiler import compile_sql
from sqlmpeg.emit import build_ffmpeg_args, emit
from sqlmpeg.errors import ErrorCode, SqlmpegError
from sqlmpeg.ir import Graph
from sqlmpeg.lower import lower
from sqlmpeg.parser import parse, resolve
from sqlmpeg.probe import ProbeResult, StreamMeta
from sqlmpeg.split import insert_splits

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"


# README ```sql blocks are dispatched by CONTENT, not by position, so moving
# an example up or down the page does not silently re-point a test. The
# headline names files nobody has; it is compiled against the real two-language
# fixtures instead, which is exactly how its shown command was produced.
_README_FIXTURE_PATHS = {"episode1.mkv": "av2.mp4", "episode2.mkv": "av3.mp4"}


def _readme_text() -> str:
    return (REPO_ROOT / "README.md").read_text(encoding="utf-8")


def _readme_block(needle: str) -> str:
    """The one ```sql block of README.md containing `needle`, verbatim."""
    blocks = re.findall(r"```sql\n(.*?)```", _readme_text(), re.DOTALL)
    assert blocks, "README.md no longer contains a ```sql block"
    matching = [str(block) for block in blocks if needle in block]
    assert len(matching) == 1, f"expected exactly one README ```sql block with {needle!r}"
    return matching[0]


def _readme_sql() -> str:
    """The PiP example: a CTE carrying a video column AND an audio column."""
    return _readme_block("WITH pip")


def _readme_union_sql() -> str:
    """The headline UNION ALL splat, re-pointed at the real fixtures.

    The example names 'episode1.mkv'/'episode2.mkv' for readability; av2.mp4
    and av3.mp4 are what those stand in for -- two files with one video and two
    audio tracks tagged eng/fra apiece -- and splatting an array needs a file
    that can actually be probed for its stream count.
    """
    sql = _readme_block("episode1.mkv")
    for shown, fixture in _README_FIXTURE_PATHS.items():
        sql = sql.replace(shown, (FIXTURES_DIR / fixture).as_posix())
    return sql


def _lower(sql: str, probes: dict[str, ProbeResult | None] | None = None) -> Graph:
    return lower(resolve(parse(sql)), probes or {})


def _reject(sql: str) -> SqlmpegError:
    with pytest.raises(SqlmpegError) as excinfo:
        compile_sql(sql)
    return _anchored(excinfo.value)


def _reject_lower(sql: str, probes: dict[str, ProbeResult | None]) -> SqlmpegError:
    """Like ``_reject``, for rejections that only a probed input can produce."""
    with pytest.raises(SqlmpegError) as excinfo:
        _lower(sql, probes)
    return _anchored(excinfo.value)


def _anchored(err: SqlmpegError) -> SqlmpegError:
    assert err.line is not None, "every rejection must be line-anchored"
    assert err.col is not None
    return err


def _filters(g: Graph) -> list[str]:
    return [node.filter for node in g.nodes.values()]


def _outputs(g: Graph) -> list[tuple[str, str, str | None]]:
    return [(o.ref, o.type, o.name) for o in g.outputs]


def _probe_result(
    videos: int = 1,
    audios: int = 1,
    *,
    video_tags: dict[str, str] | None = None,
    audio_tags: dict[str, str] | None = None,
    per_audio_tags: list[dict[str, str]] | None = None,
) -> ProbeResult:
    """A synthetic ProbeResult -- no ffprobe, no fixture, no disk.

    ``audio_tags`` tags every audio stream the same; ``per_audio_tags`` tags
    them one by one (and fixes ``audios``), which is what the broadcasting
    provenance tests need.
    """
    if per_audio_tags is not None:
        audios = len(per_audio_tags)
    streams = [
        StreamMeta(
            type="video",
            index=i,
            metadata=dict(video_tags or {}),
            width=320,
            height=240,
            fps="15/1",
            sample_rate=None,
        )
        for i in range(videos)
    ]
    streams += [
        StreamMeta(
            type="audio",
            index=i,
            metadata=dict(per_audio_tags[i] if per_audio_tags is not None else audio_tags or {}),
            width=None,
            height=None,
            fps=None,
            sample_rate=44100,
        )
        for i in range(audios)
    ]
    return ProbeResult(streams=streams)


# ---------------------------------------------------------------------------
# the README headline: UNION ALL over splatted audio arrays
# ---------------------------------------------------------------------------


@pytest.mark.exec
def test_readme_headline_concatenates_the_two_sources_pairwise(_fixtures: None) -> None:
    """Both branches splat `<alias>.audio`, so the concat signature is derived,
    not written: 1 video + 2 audio pads, inputs interleaved segment by segment."""
    g = compile_sql(_readme_union_sql())
    assert _filters(g) == ["concat"]
    concat = g.nodes["n1"]
    assert concat.args == {"n": 2, "v": 1, "a": 2}
    assert concat.inputs == [
        "src:a:v:0", "src:a:a:0", "src:a:a:1",
        "src:b:v:0", "src:b:a:0", "src:b:a:1",
    ]
    assert concat.outputs == ["video", "audio", "audio"]
    assert _outputs(g) == [
        ("n1:0", "video", None),
        ("n1:1", "audio", None),
        ("n1:2", "audio", None),
    ]


@pytest.mark.exec
def test_readme_headline_keeps_the_agreed_language_tags(_fixtures: None) -> None:
    """Both segments tag their tracks eng/fra, so the concat outputs keep them;
    the video pads' mp4-stamped `und` says nothing and is dropped."""
    g = compile_sql(_readme_union_sql())
    assert [o.metadata for o in g.outputs] == [
        {},
        {"language": "eng"},
        {"language": "fra"},
    ]


@pytest.mark.exec
def test_readme_headline_command_is_the_real_compilation(_fixtures: None) -> None:
    """The command shown under the headline is what sqlmpeg actually prints for
    that query, with only the fixture paths written back to the shown names."""
    args = build_ffmpeg_args(emit(compile_sql(_readme_union_sql())), "season.mkv")
    shown = shlex.join(args)
    for name, fixture in _README_FIXTURE_PATHS.items():
        shown = shown.replace((FIXTURES_DIR / fixture).as_posix(), name)
    assert shown in _readme_text(), shown


# ---------------------------------------------------------------------------
# the README PiP example (v0 compat: `frame` sugar still compiles)
# ---------------------------------------------------------------------------


def test_readme_example_lowers_to_expected_nodes() -> None:
    """The flagship PiP example: a CTE carrying video AND audio columns."""
    g = compile_sql(_readme_sql())
    assert g.to_dict() == {
        "inputs": ["game.mp4", "game.mp4"],
        # CTEs are traversed first, so the CTE's alias `b` takes input 0.
        "sources": {"b": 0, "a": 1},
        "nodes": [
            {
                "id": "n1",
                "filter": "crop",
                "args": {"w": 600, "h": 200, "x": 1200, "y": 50},
                "inputs": ["src:b:v:0"],
                "outputs": ["video"],
            },
            {
                "id": "n2",
                "filter": "scale",
                "args": {"w": "iw*0.5", "h": "-2"},
                "inputs": ["n1"],
                "outputs": ["video"],
            },
            {
                "id": "n3",
                "filter": "overlay",
                "args": {"x": 20, "y": 20},
                "inputs": ["src:a:v:0", "n2"],
                "outputs": ["video"],
            },
            {
                "id": "n4",
                "filter": "volume",
                "args": {"volume": 0.65},
                "inputs": ["src:a:a:0"],
                "outputs": ["audio"],
            },
            {
                "id": "n5",
                "filter": "volume",
                "args": {"volume": 0.35},
                "inputs": ["src:b:a:0"],
                "outputs": ["audio"],
            },
            {
                "id": "n6",
                "filter": "amix",
                "args": {"inputs": 2},
                "inputs": ["n4", "n5"],
                "outputs": ["audio"],
            },
        ],
        "outputs": [
            {"ref": "n3", "type": "video", "name": None, "metadata": {}},
            {"ref": "n6", "type": "audio", "name": None, "metadata": {}},
        ],
    }


def test_readme_example_selects_its_two_streams_explicitly() -> None:
    """The SELECT list is the output list: one video column, one mixed audio
    column, and nothing implicit."""
    g = compile_sql(_readme_sql())
    assert [o.type for o in g.outputs] == ["video", "audio"]


def test_readme_example_emits_a_filtergraph() -> None:
    e = emit(compile_sql(_readme_sql()))
    assert "crop=" in e.filter_complex
    assert "overlay=" in e.filter_complex
    assert "amix=" in e.filter_complex
    assert e.inputs == ["game.mp4", "game.mp4"]
    assert [m.target for m in e.maps] == ["[out0]", "[out1]"]
    assert [m.copy for m in e.maps] == [False, False]


def test_readme_example_scale_factor_is_not_a_decimal() -> None:
    """``Literal.to_py()`` yields Decimal for 0.5; the IR must carry float."""
    g = _lower("SELECT scale(a.frame, 0.5, 0.25) FROM input('x.mp4') a")
    args = g.nodes["n1"].args
    assert args == {"w": 0.5, "h": 0.25}
    assert all(type(v) is float for v in args.values())


# ---------------------------------------------------------------------------
# typed columns, subscripts, passthrough
# ---------------------------------------------------------------------------


def test_frame_sugar_is_the_first_video_stream() -> None:
    g = compile_sql("SELECT a.frame FROM input('x.mp4') a")
    assert g.nodes == {}
    assert _outputs(g) == [("src:a:v:0", "video", None)]
    assert g.sources == {"a": 0}


def test_frame_and_video_subscript_one_agree() -> None:
    assert _outputs(compile_sql("SELECT a.frame FROM input('x.mp4') a")) == _outputs(
        compile_sql("SELECT a.video[1] FROM input('x.mp4') a")
    )


def test_sql_subscripts_are_one_based() -> None:
    g = compile_sql("SELECT a.video[2], a.audio[3] FROM input('x.mp4') a")
    assert _outputs(g) == [
        ("src:a:v:1", "video", None),
        ("src:a:a:2", "audio", None),
    ]


def test_remap_only_query_is_pure_passthrough() -> None:
    """`SELECT a.video[1], a.audio[2]` -> two -map/-c copy streams, no filters."""
    g = compile_sql("SELECT a.video[1], a.audio[2] FROM input('foo.mp4') a")
    assert g.nodes == {}
    assert _outputs(g) == [
        ("src:a:v:0", "video", None),
        ("src:a:a:1", "audio", None),
    ]
    e = emit(g)
    assert e.filter_complex == ""
    assert [(m.target, m.type, m.copy) for m in e.maps] == [
        ("0:v:0", "video", True),
        ("0:a:1", "audio", True),
    ]


def test_filtered_video_with_passthrough_audio() -> None:
    g = compile_sql("SELECT scale(a.video[1], 0.5), a.audio[1] FROM input('foo.mp4') a")
    assert _filters(g) == ["scale"]
    assert _outputs(g) == [("n1", "video", None), ("src:a:a:0", "audio", None)]
    e = emit(g)
    assert [(m.target, m.copy) for m in e.maps] == [("[out0]", False), ("0:a:0", True)]


def test_select_alias_becomes_the_output_name() -> None:
    g = compile_sql(
        "SELECT a.video[1] AS picture, a.audio[1] AS Sound FROM input('x.mp4') a"
    )
    assert [o.name for o in g.outputs] == ["picture", "sound"]  # unquoted -> folded


def test_quoted_select_alias_keeps_its_case() -> None:
    g = compile_sql('SELECT a.audio[1] AS "Commentary" FROM input(\'x.mp4\') a')
    assert [o.name for o in g.outputs] == ["Commentary"]


def test_output_order_is_select_order() -> None:
    g = compile_sql("SELECT a.audio[1], a.video[1] FROM input('x.mp4') a")
    assert [o.type for o in g.outputs] == ["audio", "video"]


def test_time_column_outside_where_is_rejected() -> None:
    err = _reject("SELECT a.t FROM input('x.mp4') a")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "a.t" in err.message


def test_unknown_column_is_rejected() -> None:
    err = _reject("SELECT hflip(a.width) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "a.width" in err.message


def test_literal_projection_is_rejected() -> None:
    err = _reject("SELECT 1 FROM input('x.mp4') a")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "every SELECT column must be a stream expression" in err.message


def test_arithmetic_projection_is_rejected() -> None:
    err = _reject("SELECT 1 + 2 FROM input('x.mp4') a")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "every SELECT column must be a stream expression" in err.message


def test_frame_cannot_be_subscripted() -> None:
    err = _reject("SELECT a.frame[1] FROM input('x.mp4') a")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "a.frame" in err.message


# ---------------------------------------------------------------------------
# broadcasting: bare arrays splat (plan 020)
# ---------------------------------------------------------------------------


def test_bare_audio_array_splats_into_one_output_per_stream() -> None:
    g = _lower("SELECT a.audio FROM input('x.mp4') a", {"a": _probe_result(audios=3)})
    assert g.nodes == {}
    assert _outputs(g) == [
        ("src:a:a:0", "audio", None),
        ("src:a:a:1", "audio", None),
        ("src:a:a:2", "audio", None),
    ]


def test_bare_video_array_splats_in_file_order() -> None:
    g = _lower(
        "SELECT a.video FROM input('x.mp4') a", {"a": _probe_result(videos=2, audios=1)}
    )
    assert _outputs(g) == [("src:a:v:0", "video", None), ("src:a:v:1", "video", None)]


def test_a_splat_keeps_its_place_in_the_select_list() -> None:
    g = _lower(
        "SELECT a.audio, a.video[1] FROM input('x.mp4') a",
        {"a": _probe_result(videos=1, audios=2)},
    )
    assert [o.type for o in g.outputs] == ["audio", "audio", "video"]


def test_as_alias_is_repeated_verbatim_on_every_splatted_element() -> None:
    """The alias names the COLUMN, so elements are NOT suffixed with an ordinal."""
    g = _lower(
        "SELECT a.audio AS Track FROM input('x.mp4') a", {"a": _probe_result(audios=2)}
    )
    assert [o.name for o in g.outputs] == ["track", "track"]


def test_an_unaliased_splat_names_nothing() -> None:
    g = _lower("SELECT a.audio FROM input('x.mp4') a", {"a": _probe_result(audios=2)})
    assert [o.name for o in g.outputs] == [None, None]


def test_a_one_stream_array_still_splats() -> None:
    g = _lower("SELECT a.audio FROM input('x.mp4') a", {"a": _probe_result(audios=1)})
    assert _outputs(g) == [("src:a:a:0", "audio", None)]


def test_unprobeable_bare_array_cannot_be_enumerated() -> None:
    """No probe -> no length -> INPUT_NOT_FOUND, the natural error (RFC-001)."""
    err = _reject("SELECT a.audio FROM input('nope.mp4') a")
    assert err.code is ErrorCode.INPUT_NOT_FOUND
    assert "cannot enumerate the streams of 'nope.mp4'" in err.message
    assert err.line == 1 and err.col is not None


def test_unprobeable_bare_array_as_an_argument_is_the_same_error() -> None:
    err = _reject("SELECT volume(a.audio, 0.5) FROM input('nope.mp4') a")
    assert err.code is ErrorCode.INPUT_NOT_FOUND
    assert err.hint is not None and "a.audio[1]" in err.hint


def test_no_probe_flag_also_loses_the_ability_to_enumerate() -> None:
    with pytest.raises(SqlmpegError) as excinfo:
        compile_sql("SELECT a.audio FROM input('x.mp4') a", probe=False)
    assert excinfo.value.code is ErrorCode.INPUT_NOT_FOUND


# ---------------------------------------------------------------------------
# broadcasting: calls expand elementwise
# ---------------------------------------------------------------------------


def test_scalar_broadcast_makes_one_node_per_element() -> None:
    g = _lower(
        "SELECT volume(a.audio, 0.5) FROM input('x.mp4') a", {"a": _probe_result(audios=2)}
    )
    assert _filters(g) == ["volume", "volume"]
    assert g.nodes["n1"].inputs == ["src:a:a:0"]
    assert g.nodes["n2"].inputs == ["src:a:a:1"]
    assert g.nodes["n1"].args == g.nodes["n2"].args == {"volume": 0.5}
    assert _outputs(g) == [("n1", "audio", None), ("n2", "audio", None)]


def test_nested_broadcasts_compose() -> None:
    """`volume(reverb(a.audio, 0.3), 0.5)` : the inner array flows outward."""
    g = _lower(
        "SELECT volume(reverb(a.audio, 0.3), 0.5) FROM input('x.mp4') a",
        {"a": _probe_result(audios=2)},
    )
    assert _filters(g) == ["aecho", "aecho", "volume", "volume"]
    assert g.nodes["n3"].inputs == ["n1"]
    assert g.nodes["n4"].inputs == ["n2"]


def test_two_arrays_zip_elementwise() -> None:
    g = _lower(
        "SELECT amix(a.audio, b.audio) FROM input('x.mp4') a, input('y.mp4') b",
        {"a": _probe_result(audios=2), "b": _probe_result(audios=2)},
    )
    assert _filters(g) == ["amix", "amix"]
    assert g.nodes["n1"].inputs == ["src:a:a:0", "src:b:a:0"]
    assert g.nodes["n2"].inputs == ["src:a:a:1", "src:b:a:1"]


def test_a_scalar_argument_repeats_into_every_element() -> None:
    g = _lower(
        "SELECT amix(a.audio, b.audio[1]) FROM input('x.mp4') a, input('y.mp4') b",
        {"a": _probe_result(audios=2), "b": _probe_result(audios=1)},
    )
    assert g.nodes["n1"].inputs == ["src:a:a:0", "src:b:a:0"]
    assert g.nodes["n2"].inputs == ["src:a:a:1", "src:b:a:0"]


def test_a_repeated_scalar_is_fanned_out_by_the_split_pass() -> None:
    """Broadcasting reuses one ref N times; asplit is what makes that legal."""
    g = insert_splits(
        _lower(
            "SELECT amix(a.audio, b.audio[1]) FROM input('x.mp4') a, input('y.mp4') b",
            {"a": _probe_result(audios=2), "b": _probe_result(audios=1)},
        )
    )
    assert _filters(g) == ["asplit", "amix", "amix"]
    assert g.nodes["src_b_a_0_split"].args == {"n": 2}
    assert g.nodes["n1"].inputs == ["src:a:a:0", "src_b_a_0_split:0"]
    assert g.nodes["n2"].inputs == ["src:a:a:1", "src_b_a_0_split:1"]


def test_zip_length_mismatch_is_a_broadcast_mismatch() -> None:
    err = _reject_lower(
        "SELECT amix(a.audio, b.audio) FROM input('x.mp4') a, input('y.mp4') b",
        {"a": _probe_result(audios=3), "b": _probe_result(audios=2)},
    )
    assert err.code is ErrorCode.BROADCAST_MISMATCH
    assert "a.audio has 3 streams" in err.message
    assert "b.audio has 2 streams" in err.message


def test_broadcast_over_a_video_array() -> None:
    g = _lower(
        "SELECT hflip(a.video) FROM input('x.mp4') a", {"a": _probe_result(videos=2)}
    )
    assert _filters(g) == ["hflip", "hflip"]


def test_an_array_argument_still_type_checks_by_element() -> None:
    err = _reject_lower(
        "SELECT hflip(a.audio) FROM input('x.mp4') a", {"a": _probe_result(audios=2)}
    )
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "got hflip(audio)" in err.message


def test_broadcast_composes_with_the_where_trim() -> None:
    """One trim per element, shared by every consumer of that element."""
    g = _lower(
        "SELECT volume(a.audio, 0.5), reverb(a.audio, 0.3) FROM input('x.mp4') a "
        "WHERE a.t BETWEEN 1 AND 2",
        {"a": _probe_result(audios=2)},
    )
    assert _filters(g) == [
        "atrim",
        "asetpts",
        "atrim",
        "asetpts",
        "volume",
        "volume",
        "aecho",
        "aecho",
    ]
    assert g.nodes["n1"].inputs == ["src:a:a:0"]
    assert g.nodes["n3"].inputs == ["src:a:a:1"]
    # both calls consume the SAME two trimmed streams, one per element
    assert [g.nodes[n].inputs for n in ("n5", "n6", "n7", "n8")] == [
        ["n2"],
        ["n4"],
        ["n2"],
        ["n4"],
    ]


# ---------------------------------------------------------------------------
# broadcasting: provenance
# ---------------------------------------------------------------------------


def test_broadcast_outputs_carry_their_own_source_metadata() -> None:
    g = _lower(
        "SELECT reverb(a.audio, 0.3) FROM input('x.mp4') a",
        {
            "a": _probe_result(
                audios=2, per_audio_tags=[{"language": "eng"}, {"language": "fra"}]
            )
        },
    )
    assert [o.metadata for o in g.outputs] == [{"language": "eng"}, {"language": "fra"}]


def test_provenance_survives_a_chain_of_single_stream_calls() -> None:
    g = _lower(
        "SELECT volume(reverb(a.audio[1], 0.3), 0.5) FROM input('x.mp4') a",
        {"a": _probe_result(audio_tags={"language": "fra", "title": "VF"})},
    )
    assert g.outputs[0].metadata == {"language": "fra", "title": "VF"}


def test_provenance_survives_the_where_trim() -> None:
    g = _lower(
        "SELECT a.audio[1] FROM input('x.mp4') a WHERE a.t BETWEEN 0 AND 1",
        {"a": _probe_result(audio_tags={"language": "fra"})},
    )
    assert g.outputs[0].metadata == {"language": "fra"}


def test_amix_breaks_provenance() -> None:
    """Two stream inputs = no single source; the mix is nobody's language."""
    g = _lower(
        "SELECT amix(a.audio[1], a.audio[2]) FROM input('x.mp4') a",
        {
            "a": _probe_result(
                audios=2, per_audio_tags=[{"language": "eng"}, {"language": "fra"}]
            )
        },
    )
    assert _filters(g) == ["amix"]
    assert g.outputs[0].metadata == {}


def test_broadcast_through_amix_drops_every_elements_provenance() -> None:
    g = _lower(
        "SELECT amix(a.audio, b.audio) FROM input('x.mp4') a, input('y.mp4') b",
        {
            "a": _probe_result(
                audios=2, per_audio_tags=[{"language": "eng"}, {"language": "fra"}]
            ),
            "b": _probe_result(audios=2, audio_tags={"language": "deu"}),
        },
    )
    assert [o.metadata for o in g.outputs] == [{}, {}]


def test_concat_keeps_provenance_every_segment_agrees_on() -> None:
    """Both segments of the pad say `language=eng`, so the pad still does."""
    g = _lower(
        "SELECT a.audio[1] FROM input('x.mp4') a "
        "UNION ALL SELECT b.audio[1] FROM input('y.mp4') b",
        {
            "a": _probe_result(audio_tags={"language": "eng"}),
            "b": _probe_result(audio_tags={"language": "eng"}),
        },
    )
    assert g.outputs[0].metadata == {"language": "eng"}


def test_concat_drops_provenance_the_segments_disagree_on() -> None:
    """An English segment concatenated with a French one is neither."""
    g = _lower(
        "SELECT a.audio[1] FROM input('x.mp4') a "
        "UNION ALL SELECT b.audio[1] FROM input('y.mp4') b",
        {
            "a": _probe_result(audio_tags={"language": "eng"}),
            "b": _probe_result(audio_tags={"language": "fra"}),
        },
    )
    assert g.outputs[0].metadata == {}


def test_concat_drops_provenance_when_one_segment_has_none() -> None:
    """Agreement is on what SURVIVES the und-filter: an untagged segment has
    nothing to say, so the tagged one does not speak for the whole output."""
    g = _lower(
        "SELECT a.audio[1] FROM input('x.mp4') a "
        "UNION ALL SELECT b.audio[1] FROM input('y.mp4') b",
        {
            "a": _probe_result(audio_tags={"language": "eng"}),
            "b": _probe_result(audio_tags={"language": "und"}),
        },
    )
    assert g.outputs[0].metadata == {}


def test_concat_of_two_untagged_segments_carries_no_metadata() -> None:
    """Two `und` streams filter down to {} apiece: they agree on nothing, and
    an empty agreement is still no metadata."""
    g = _lower(
        "SELECT a.audio[1] FROM input('x.mp4') a "
        "UNION ALL SELECT b.audio[1] FROM input('y.mp4') b",
        {
            "a": _probe_result(audio_tags={"language": "und"}),
            "b": _probe_result(audio_tags={"language": "und"}),
        },
    )
    assert g.outputs[0].metadata == {}


def test_concat_drops_provenance_when_a_segment_was_not_probed() -> None:
    g = _lower(
        "SELECT a.audio[1] FROM input('x.mp4') a "
        "UNION ALL SELECT b.audio[1] FROM input('y.mp4') b",
        {"a": _probe_result(audio_tags={"language": "eng"}), "b": None},
    )
    assert g.outputs[0].metadata == {}


def test_concat_agrees_per_pad_over_flattened_array_columns() -> None:
    """Splatted arrays pair elementwise, and each pad agrees (or not) on its
    own: track 1 matches eng/eng, track 2 is fra against deu."""
    g = _lower(
        "SELECT a.audio FROM input('x.mp4') a UNION ALL SELECT b.audio FROM input('y.mp4') b",
        {
            "a": _probe_result(
                audios=2, per_audio_tags=[{"language": "eng"}, {"language": "fra"}]
            ),
            "b": _probe_result(
                audios=2, per_audio_tags=[{"language": "eng"}, {"language": "deu"}]
            ),
        },
    )
    assert [o.metadata for o in g.outputs] == [{"language": "eng"}, {}]


def test_concat_provenance_survives_a_filtered_segment() -> None:
    """Provenance reaches the concat through each branch's own 1:1 chain."""
    g = _lower(
        "SELECT volume(a.audio[1], 0.5) FROM input('x.mp4') a "
        "UNION ALL SELECT b.audio[1] FROM input('y.mp4') b",
        {
            "a": _probe_result(audio_tags={"language": "fra", "title": "VF"}),
            "b": _probe_result(audio_tags={"language": "fra", "title": "VF"}),
        },
    )
    assert g.outputs[0].metadata == {"language": "fra", "title": "VF"}


def test_concat_after_amix_carries_no_metadata() -> None:
    """A mixed segment has no source at all, so the pad cannot agree with the
    other one whatever it says."""
    g = _lower(
        "SELECT amix(a.audio[1], a.audio[2]) FROM input('x.mp4') a "
        "UNION ALL SELECT b.audio[1] FROM input('y.mp4') b",
        {
            "a": _probe_result(audios=2, audio_tags={"language": "eng"}),
            "b": _probe_result(audio_tags={"language": "eng"}),
        },
    )
    assert g.outputs[0].metadata == {}


# ---------------------------------------------------------------------------
# WHERE -> typed trim
# ---------------------------------------------------------------------------


def test_where_between_prepends_trim_and_setpts_on_video() -> None:
    g = _lower("SELECT hflip(a.frame) FROM input('x.mp4') a WHERE a.t BETWEEN 1 AND 2.5")
    assert g.to_dict()["nodes"] == [
        {
            "id": "n1",
            "filter": "trim",
            "args": {"start": 1, "end": 2.5},
            "inputs": ["src:a:v:0"],
            "outputs": ["video"],
        },
        {
            "id": "n2",
            "filter": "setpts",
            "args": {"expr": "PTS-STARTPTS"},
            "inputs": ["n1"],
            "outputs": ["video"],
        },
        {"id": "n3", "filter": "hflip", "args": {}, "inputs": ["n2"], "outputs": ["video"]},
    ]
    assert _outputs(g) == [("n3", "video", None)]


def test_where_between_uses_atrim_and_asetpts_on_audio() -> None:
    g = _lower("SELECT a.audio[1] FROM input('x.mp4') a WHERE a.t BETWEEN 1 AND 2")
    assert g.to_dict()["nodes"] == [
        {
            "id": "n1",
            "filter": "atrim",
            "args": {"start": 1, "end": 2},
            "inputs": ["src:a:a:0"],
            "outputs": ["audio"],
        },
        {
            "id": "n2",
            "filter": "asetpts",
            "args": {"expr": "PTS-STARTPTS"},
            "inputs": ["n1"],
            "outputs": ["audio"],
        },
    ]
    assert _outputs(g) == [("n2", "audio", None)]


def test_one_predicate_trims_video_and_audio_in_sync() -> None:
    g = _lower(
        "SELECT a.video[1], a.audio[1] FROM input('x.mp4') a WHERE a.t BETWEEN 5 AND 10"
    )
    assert _filters(g) == ["trim", "setpts", "atrim", "asetpts"]
    assert g.nodes["n1"].inputs == ["src:a:v:0"]
    assert g.nodes["n3"].inputs == ["src:a:a:0"]
    assert _outputs(g) == [("n2", "video", None), ("n4", "audio", None)]


def test_trim_is_spliced_once_per_stream_and_shared() -> None:
    g = _lower(
        "SELECT overlay(a.frame, a.frame, 5, 5) FROM input('x.mp4') a "
        "WHERE a.t BETWEEN 0 AND 3"
    )
    assert _filters(g) == ["trim", "setpts", "overlay"]
    assert g.nodes["n3"].inputs == ["n2", "n2"]  # both arms, pre-split


def test_where_trims_only_the_named_alias() -> None:
    g = _lower(
        "SELECT overlay(a.frame, b.frame, 0, 0) "
        "FROM input('x.mp4') a, input('y.mp4') b WHERE b.t BETWEEN 2 AND 4"
    )
    assert g.nodes["n1"].inputs == ["src:b:v:0"]
    assert g.nodes["n3"].inputs == ["src:a:v:0", "n2"]


def test_two_between_clauses_trim_both_aliases() -> None:
    g = _lower(
        "SELECT overlay(a.frame, b.frame, 0, 0) FROM input('x.mp4') a, input('y.mp4') b "
        "WHERE a.t BETWEEN 0 AND 1 AND b.t BETWEEN 2 AND 3"
    )
    assert _filters(g) == ["trim", "setpts", "trim", "setpts", "overlay"]
    assert g.nodes["n5"].inputs == ["n2", "n4"]


def test_untouched_streams_of_a_trimmed_alias_cost_nothing() -> None:
    """Trims are lazy: an unconsumed audio stream never gets an atrim."""
    g = _lower("SELECT a.video[1] FROM input('x.mp4') a WHERE a.t BETWEEN 0 AND 1")
    assert _filters(g) == ["trim", "setpts"]


# ---------------------------------------------------------------------------
# CTEs
# ---------------------------------------------------------------------------


def test_unnamed_single_video_cte_column_is_reachable_as_frame() -> None:
    g = _lower(
        "WITH c AS (SELECT hflip(a.frame) FROM input('x.mp4') a) "
        "SELECT vflip(c.frame) FROM c"
    )
    assert _filters(g) == ["hflip", "vflip"]
    assert g.nodes["n2"].inputs == ["n1"]


def test_cte_columns_are_reachable_by_their_as_names() -> None:
    g = _lower(
        "WITH c AS ("
        "  SELECT hflip(a.video[1]) AS pic, volume(a.audio[1], 0.5) AS snd "
        "  FROM input('x.mp4') a"
        ") SELECT c.snd, vflip(c.pic) FROM c"
    )
    assert _filters(g) == ["hflip", "volume", "vflip"]
    assert _outputs(g) == [("n2", "audio", None), ("n3", "video", None)]


def test_unknown_cte_column_lists_the_known_names() -> None:
    err = _reject(
        "WITH c AS (SELECT hflip(a.video[1]) AS pic, a.audio[1] AS snd "
        "FROM input('x.mp4') a) SELECT c.nope FROM c"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "c.nope" in err.message
    assert err.hint is not None and "pic" in err.hint and "snd" in err.hint


def test_a_scalar_cte_column_cannot_be_subscripted() -> None:
    err = _reject(
        "WITH c AS (SELECT a.audio[1] AS snd FROM input('x.mp4') a) "
        "SELECT c.snd[1] FROM c"
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "'c.snd' is a single stream" in err.message


# ---------------------------------------------------------------------------
# CTE array columns: splat, broadcast again, subscript (plan 020)
# ---------------------------------------------------------------------------


def test_cte_array_column_splats_in_the_outer_select() -> None:
    g = _lower(
        "WITH c AS (SELECT reverb(a.audio, 0.3) AS snd FROM input('x.mp4') a) "
        "SELECT c.snd FROM c",
        {"a": _probe_result(audios=2)},
    )
    assert _filters(g) == ["aecho", "aecho"]
    # the CTE's own AS name stays inside the CTE; the outer SELECT names the
    # output columns (here: not at all).
    assert _outputs(g) == [("n1", "audio", None), ("n2", "audio", None)]


def test_cte_array_column_broadcasts_again() -> None:
    g = _lower(
        "WITH c AS (SELECT a.audio AS snd FROM input('x.mp4') a) "
        "SELECT volume(c.snd, 0.5) FROM c",
        {"a": _probe_result(audios=2)},
    )
    assert _filters(g) == ["volume", "volume"]
    assert g.nodes["n1"].inputs == ["src:a:a:0"]
    assert g.nodes["n2"].inputs == ["src:a:a:1"]


def test_cte_array_column_is_subscriptable_one_based() -> None:
    g = _lower(
        "WITH c AS (SELECT reverb(a.audio, 0.3) AS snd FROM input('x.mp4') a) "
        "SELECT c.snd[2] FROM c",
        {"a": _probe_result(audios=2)},
    )
    # both elements are still built (the CTE lowered as a whole); only the
    # subscripted one reaches the output list.
    assert _filters(g) == ["aecho", "aecho"]
    assert _outputs(g) == [("n2", "audio", None)]


def test_cte_array_subscript_bounds_are_static() -> None:
    """The length was recorded when the CTE lowered: no probe is consulted here."""
    err = _reject_lower(
        "WITH c AS (SELECT a.audio AS snd FROM input('x.mp4') a) "
        "SELECT c.snd[3] FROM c",
        {"a": _probe_result(audios=2)},
    )
    assert err.code is ErrorCode.STREAM_NOT_FOUND
    assert "'c.snd[3]' does not exist" in err.message
    assert "column 'c.snd' has 2 streams" in err.message


def test_cte_array_column_provenance_reaches_the_outer_output() -> None:
    g = _lower(
        "WITH c AS (SELECT reverb(a.audio, 0.3) AS snd FROM input('x.mp4') a) "
        "SELECT c.snd FROM c",
        {
            "a": _probe_result(
                audios=2, per_audio_tags=[{"language": "eng"}, {"language": "fra"}]
            )
        },
    )
    assert [o.metadata for o in g.outputs] == [{"language": "eng"}, {"language": "fra"}]


def test_a_single_array_video_cte_column_is_not_frame_sugar() -> None:
    """`<cte>.frame` is singular sugar; an array column does not answer to it."""
    err = _reject_lower(
        "WITH c AS (SELECT a.video FROM input('x.mp4') a) SELECT hflip(c.frame) FROM c",
        {"a": _probe_result(videos=2)},
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "c.frame" in err.message


def test_where_trims_a_cte_column_by_its_type() -> None:
    g = _lower(
        "WITH c AS (SELECT a.audio[1] AS snd FROM input('x.mp4') a) "
        "SELECT c.snd FROM c WHERE c.t BETWEEN 1 AND 2"
    )
    assert _filters(g) == ["atrim", "asetpts"]
    assert g.nodes["n1"].inputs == ["src:a:a:0"]


def test_cte_union_all_gets_its_own_concat() -> None:
    g = _lower(
        "WITH u AS ("
        "  SELECT a.frame FROM input('x.mp4') a"
        "  UNION ALL SELECT b.frame FROM input('y.mp4') b"
        ") SELECT hflip(u.frame) FROM u"
    )
    assert _filters(g) == ["concat", "hflip"]
    assert g.nodes["n1"].inputs == ["src:a:v:0", "src:b:v:0"]
    assert g.nodes["n1"].outputs == ["video"]
    assert g.nodes["n2"].inputs == ["n1"]


# ---------------------------------------------------------------------------
# UNION ALL -> concat
# ---------------------------------------------------------------------------


def test_union_all_video_only_lowers_to_one_concat() -> None:
    g = compile_sql(
        "SELECT a.frame FROM input('x.mp4') a "
        "UNION ALL SELECT hflip(b.frame) FROM input('y.mp4') b "
        "UNION ALL SELECT c.frame FROM input('z.mp4') c"
    )
    concat = g.nodes["n2"]
    assert concat.filter == "concat"
    assert concat.args == {"n": 3, "v": 1, "a": 0}
    assert concat.inputs == ["src:a:v:0", "n1", "src:c:v:0"]
    assert concat.outputs == ["video"]
    assert _outputs(g) == [("n2", "video", None)]


def test_union_all_with_audio_interleaves_inputs_per_segment() -> None:
    g = compile_sql(
        "SELECT a.video[1], a.audio[1] FROM input('intro.mp4') a "
        "UNION ALL SELECT b.video[1], b.audio[1] FROM input('main.mp4') b"
    )
    concat = g.nodes["n1"]
    assert concat.args == {"n": 2, "v": 1, "a": 1}
    # per ffmpeg: [seg1 v][seg1 a][seg2 v][seg2 a]
    assert concat.inputs == ["src:a:v:0", "src:a:a:0", "src:b:v:0", "src:b:a:0"]
    assert concat.outputs == ["video", "audio"]
    assert _outputs(g) == [("n1:0", "video", None), ("n1:1", "audio", None)]


def test_concat_pads_map_back_to_the_branch_column_order() -> None:
    """Pads are videos-then-audios; the SELECT list keeps its own order."""
    g = compile_sql(
        "SELECT a.audio[1], a.video[1] FROM input('x.mp4') a "
        "UNION ALL SELECT b.audio[1], b.video[1] FROM input('y.mp4') b"
    )
    concat = g.nodes["n1"]
    assert concat.inputs == ["src:a:v:0", "src:a:a:0", "src:b:v:0", "src:b:a:0"]
    assert concat.outputs == ["video", "audio"]
    assert _outputs(g) == [("n1:1", "audio", None), ("n1:0", "video", None)]


def test_union_all_keeps_the_first_branch_column_names() -> None:
    g = compile_sql(
        "SELECT a.video[1] AS pic FROM input('x.mp4') a "
        "UNION ALL SELECT b.video[1] AS other FROM input('y.mp4') b"
    )
    assert [o.name for o in g.outputs] == ["pic"]


def test_union_all_type_mismatch_is_a_concat_mismatch() -> None:
    err = _reject(
        "SELECT a.video[1] FROM input('x.mp4') a\n"
        "UNION ALL SELECT b.audio[1] FROM input('y.mp4') b"
    )
    assert err.code is ErrorCode.CONCAT_MISMATCH
    assert err.line == 2, "the mismatching branch is the anchor"
    assert "branch 2" in err.message


def test_union_all_column_count_mismatch_is_a_concat_mismatch() -> None:
    err = _reject(
        "SELECT a.video[1], a.audio[1] FROM input('x.mp4') a\n"
        "UNION ALL SELECT b.video[1] FROM input('y.mp4') b"
    )
    assert err.code is ErrorCode.CONCAT_MISMATCH
    assert err.line == 2


def test_union_all_flattens_array_columns_into_concat_columns() -> None:
    g = _lower(
        "SELECT a.audio FROM input('x.mp4') a UNION ALL SELECT b.audio FROM input('y.mp4') b",
        {"a": _probe_result(audios=2), "b": _probe_result(audios=2)},
    )
    concat = g.nodes["n1"]
    assert concat.args == {"n": 2, "v": 0, "a": 2}
    assert concat.inputs == ["src:a:a:0", "src:a:a:1", "src:b:a:0", "src:b:a:1"]
    assert concat.outputs == ["audio", "audio"]
    assert _outputs(g) == [("n1:0", "audio", None), ("n1:1", "audio", None)]


def test_union_all_element_count_mismatch_is_a_concat_mismatch() -> None:
    """Same types, different array lengths: the flattened signatures differ."""
    err = _reject_lower(
        "SELECT a.audio FROM input('x.mp4') a\n"
        "UNION ALL SELECT b.audio FROM input('y.mp4') b",
        {"a": _probe_result(audios=2), "b": _probe_result(audios=1)},
    )
    assert err.code is ErrorCode.CONCAT_MISMATCH
    assert err.line == 2
    assert "branch 1 selects (audio[2])" in err.message
    assert "branch 2 selects (audio[1])" in err.message


def test_union_all_column_order_mismatch_is_a_concat_mismatch() -> None:
    err = _reject(
        "SELECT a.video[1], a.audio[1] FROM input('x.mp4') a\n"
        "UNION ALL SELECT b.audio[1], b.video[1] FROM input('y.mp4') b"
    )
    assert err.code is ErrorCode.CONCAT_MISMATCH


# ---------------------------------------------------------------------------
# function calls
# ---------------------------------------------------------------------------


def test_nested_calls_chain_bottom_up() -> None:
    g = _lower("SELECT blur(hflip(vflip(a.frame)), 4) FROM input('x.mp4') a")
    assert _filters(g) == ["vflip", "hflip", "gblur"]
    assert g.nodes["n1"].inputs == ["src:a:v:0"]
    assert g.nodes["n2"].inputs == ["n1"]
    assert g.nodes["n3"].inputs == ["n2"]
    assert _outputs(g) == [("n3", "video", None)]


def test_audio_calls_chain_bottom_up() -> None:
    g = _lower("SELECT volume(reverb(a.audio[1], 0.3), 0.5) FROM input('x.mp4') a")
    assert _filters(g) == ["aecho", "volume"]
    assert g.nodes["n1"].inputs == ["src:a:a:0"]
    assert g.nodes["n1"].outputs == ["audio"]
    assert _outputs(g) == [("n2", "audio", None)]


def test_function_lookup_is_case_insensitive() -> None:
    g = _lower("SELECT SCALE(a.frame, 0.5) FROM input('x.mp4') a")
    assert _filters(g) == ["scale"]


def test_macro_expands_to_several_nodes() -> None:
    g = _lower("SELECT blur_regions(a.frame, 10, 20, 30, 40, 8) FROM input('x.mp4') a")
    assert _filters(g) == ["crop", "gblur", "overlay"]
    assert list(g.nodes) == ["n1", "n2", "n3"]


def test_negative_numeric_literals_survive() -> None:
    g = _lower("SELECT scale(a.frame, -2, 720) FROM input('x.mp4') a")
    assert g.nodes["n1"].args == {"w": -2, "h": 720}


def test_string_literal_argument() -> None:
    g = _lower("SELECT draw_box(a.frame, 1, 2, 3, 4, 'red') FROM input('x.mp4') a")
    assert g.nodes["n1"].args["color"] == "red"


def test_unknown_function_suggests_a_close_match() -> None:
    err = _reject("SELECT scal(a.frame, 0.5) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UNKNOWN_FUNCTION
    assert "scal()" in err.message
    assert err.hint is not None and "scale()" in err.hint


def test_unknown_function_without_a_match_lists_the_stdlib() -> None:
    err = _reject("SELECT zzzz(a.frame) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UNKNOWN_FUNCTION
    assert err.hint is not None and "blur_regions" in err.hint


def test_unknown_nested_function_beats_the_outer_arity_check() -> None:
    err = _reject("SELECT blur(a.frame, nope(a.frame)) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UNKNOWN_FUNCTION
    assert "nope()" in err.message


def test_arity_error_lists_every_signature() -> None:
    err = _reject("SELECT scale(a.frame) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "scale(video, num)" in err.message
    assert "scale(video, num, num)" in err.message
    assert "got scale(video)" in err.message


def test_argument_kind_mismatch_is_typed() -> None:
    err = _reject("SELECT blur(a.frame, 'lots') FROM input('x.mp4') a")
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "got blur(video, str)" in err.message


def test_audio_stream_where_video_is_expected() -> None:
    err = _reject("SELECT hflip(a.audio[1]) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "hflip(video)" in err.message
    assert "got hflip(audio)" in err.message


def test_video_stream_where_audio_is_expected() -> None:
    err = _reject("SELECT amix(a.video[1], a.audio[1]) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "amix(audio, audio)" in err.message
    assert "got amix(video, audio)" in err.message


def test_video_result_where_audio_is_expected() -> None:
    """The kind of a nested call comes from its FuncSpec.returns."""
    err = _reject("SELECT volume(hflip(a.frame), 2) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "got volume(video, num)" in err.message


def test_stream_argument_where_a_number_is_expected() -> None:
    err = _reject("SELECT blur(a.frame, hflip(a.frame)) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "got blur(video, video)" in err.message


def test_non_literal_scalar_argument_is_rejected() -> None:
    err = _reject("SELECT blur(a.frame, 1 + 2) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "got blur(video, expr)" in err.message


def test_malformed_numeric_literal_is_a_typed_rejection() -> None:
    """sqlglot tokenizes `1e` as a number but ``to_py()`` raises on it."""
    err = _reject("SELECT blur(a.frame, 1e) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "1e" in err.message


def test_malformed_between_bound_is_a_typed_rejection() -> None:
    err = _reject("SELECT a.frame FROM input('x.mp4') a WHERE a.t BETWEEN 1e AND 2")
    assert err.code is ErrorCode.UNSUPPORTED_SQL


def test_overlay_keeps_its_four_positional_arguments() -> None:
    """Postgres has a builtin OVERLAY, so sqlglot hands lower named args."""
    g = _lower(
        "SELECT overlay(a.frame, b.frame, 20, 30) FROM input('x.mp4') a, input('y.mp4') b"
    )
    node = g.nodes["n1"]
    assert node.filter == "overlay"
    assert node.args == {"x": 20, "y": 30}
    assert node.inputs == ["src:a:v:0", "src:b:v:0"]


def test_overlay_arity_error_is_still_typed() -> None:
    err = _reject("SELECT overlay(a.frame, b.frame, 20) FROM input('x.mp4') a, input('y.mp4') b")
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "overlay(video, video, num, num)" in err.message


def test_a_colliding_builtin_is_an_unknown_function() -> None:
    err = _reject("SELECT trim(a.frame) FROM input('x.mp4') a")
    assert err.code is ErrorCode.UNKNOWN_FUNCTION


# ---------------------------------------------------------------------------
# probing: bounds, provenance, symbolic fallback
# ---------------------------------------------------------------------------


def test_probed_subscript_out_of_range_is_stream_not_found() -> None:
    with pytest.raises(SqlmpegError) as excinfo:
        _lower(
            "SELECT a.audio[2] FROM input('x.mp4') a",
            {"a": _probe_result(videos=1, audios=1)},
        )
    err = excinfo.value
    assert err.code is ErrorCode.STREAM_NOT_FOUND
    assert err.line == 1 and err.col is not None
    assert "a.audio[2]" in err.message
    assert "x.mp4" in err.message


def test_probed_frame_sugar_is_bounds_checked_too() -> None:
    with pytest.raises(SqlmpegError) as excinfo:
        _lower("SELECT a.frame FROM input('x.mp4') a", {"a": _probe_result(videos=0)})
    assert excinfo.value.code is ErrorCode.STREAM_NOT_FOUND


def test_probed_subscript_in_range_lowers_normally() -> None:
    g = _lower(
        "SELECT a.audio[2] FROM input('x.mp4') a", {"a": _probe_result(audios=2)}
    )
    assert _outputs(g) == [("src:a:a:1", "audio", None)]


def test_unprobed_subscript_is_symbolic_and_out_of_range_is_fine() -> None:
    """No probe -> no bounds check; ffmpeg validates at runtime."""
    g = compile_sql("SELECT a.audio[9] FROM input('does-not-exist.mp4') a")
    assert _outputs(g) == [("src:a:a:8", "audio", None)]


def test_passthrough_output_copies_language_and_title() -> None:
    g = _lower(
        "SELECT a.audio[1] FROM input('x.mp4') a",
        {"a": _probe_result(audio_tags={"language": "fra", "title": "VF"})},
    )
    assert g.outputs[0].metadata == {"language": "fra", "title": "VF"}


def test_undefined_language_tag_is_not_copied() -> None:
    """mp4 muxers stamp language=und on untagged streams; it says nothing."""
    g = _lower(
        "SELECT a.audio[1] FROM input('x.mp4') a",
        {"a": _probe_result(audio_tags={"language": "und", "title": "Main"})},
    )
    assert g.outputs[0].metadata == {"title": "Main"}


def test_filtered_output_threads_its_sources_provenance() -> None:
    """Plan 020: a 1:1 filter chain keeps the tags raw ffmpeg would have lost."""
    g = _lower(
        "SELECT volume(a.audio[1], 0.5) FROM input('x.mp4') a",
        {"a": _probe_result(audio_tags={"language": "fra"})},
    )
    assert g.outputs[0].metadata == {"language": "fra"}


def test_unprobed_passthrough_has_no_metadata() -> None:
    g = compile_sql("SELECT a.audio[1] FROM input('x.mp4') a")
    assert g.outputs[0].metadata == {}


# ---------------------------------------------------------------------------
# compile_sql: probing policy
# ---------------------------------------------------------------------------


def test_compile_sql_probes_each_distinct_path_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def counting_probe(path: str) -> ProbeResult | None:
        calls.append(path)
        return None

    monkeypatch.setattr(compiler, "probe_path", counting_probe)
    compile_sql(
        "WITH pip AS (SELECT b.frame FROM input('game.mp4') b) "
        "SELECT overlay(a.frame, pip.frame, 0, 0) FROM input('game.mp4') a, pip"
    )
    assert calls == ["game.mp4"]  # two aliases, one file, one probe


def test_compile_sql_no_probe_skips_probing_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(path: str) -> ProbeResult | None:
        raise AssertionError("probe() must not be called with probe=False")

    monkeypatch.setattr(compiler, "probe_path", boom)
    g = compile_sql("SELECT a.audio[9] FROM input('x.mp4') a", probe=False)
    assert _outputs(g) == [("src:a:a:8", "audio", None)]


def test_compile_sql_uses_probe_results_for_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(compiler, "probe_path", lambda path: _probe_result(audios=1))
    err = _reject("SELECT a.audio[4] FROM input('x.mp4') a")
    assert err.code is ErrorCode.STREAM_NOT_FOUND


# ---------------------------------------------------------------------------
# node ids, the pipeline, the backstop
# ---------------------------------------------------------------------------


def test_node_ids_are_sequential_across_ctes_and_branches() -> None:
    g = _lower(
        "WITH c AS (SELECT hflip(a.frame) FROM input('x.mp4') a) "
        "SELECT vflip(c.frame) FROM c "
        "UNION ALL SELECT blur(b.frame, 2) FROM input('y.mp4') b"
    )
    assert list(g.nodes) == ["n1", "n2", "n3", "n4"]
    assert _filters(g) == ["hflip", "vflip", "gblur", "concat"]


def test_compile_sql_runs_the_split_pass() -> None:
    sql = "SELECT overlay(a.frame, a.frame, 5, 5) FROM input('x.mp4') a"
    assert "split" not in _filters(_lower(sql))
    assert "split" in _filters(compile_sql(sql))


def test_split_pass_picks_asplit_for_audio() -> None:
    g = compile_sql("SELECT amix(a.audio[1], a.audio[1]) FROM input('x.mp4') a")
    assert "asplit" in _filters(g)


def test_compile_sql_wraps_unexpected_exceptions_as_internal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(res: object, probes: object) -> Graph:
        raise ValueError("kaboom")

    monkeypatch.setattr(compiler, "lower", boom)
    with pytest.raises(SqlmpegError) as excinfo:
        compile_sql("SELECT a.frame FROM input('x.mp4') a")
    assert excinfo.value.code is ErrorCode.INTERNAL
    assert "kaboom" in excinfo.value.message


def test_lower_wraps_unexpected_exceptions_as_internal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise ValueError("kaboom")

    monkeypatch.setattr(lower_module._Lowerer, "run", boom)
    with pytest.raises(SqlmpegError) as excinfo:
        _lower("SELECT a.frame FROM input('x.mp4') a")
    assert excinfo.value.code is ErrorCode.INTERNAL


def test_pipeline_output_survives_a_round_trip_through_dicts() -> None:
    g = compile_sql(_readme_sql())
    assert Graph.from_dict(g.to_dict()).to_dict() == g.to_dict()


# ---------------------------------------------------------------------------
# real probing against a generated fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _fixtures() -> None:
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "gen_fixtures.py")],
        check=True,
    )


@pytest.fixture(scope="module")
def _av_fixture(_fixtures: None) -> str:
    """testsrc2 + one sine track."""
    return (FIXTURES_DIR / "av.mp4").as_posix()


@pytest.fixture(scope="module")
def _av2_fixture(_fixtures: None) -> str:
    """testsrc2 + TWO sine tracks, tagged language=eng and language=fra."""
    return (FIXTURES_DIR / "av2.mp4").as_posix()


@pytest.fixture(scope="module")
def _av3_fixture(_fixtures: None) -> str:
    """av2's concat partner: same shape and tags, different sine frequencies."""
    return (FIXTURES_DIR / "av3.mp4").as_posix()


@pytest.mark.exec
def test_probed_fixture_rejects_an_out_of_range_subscript(_av_fixture: str) -> None:
    """av.mp4 has exactly one video and one audio stream."""
    err = _reject(f"SELECT a.video[2] FROM input('{_av_fixture}') a")
    assert err.code is ErrorCode.STREAM_NOT_FOUND
    assert "a.video[2]" in err.message


@pytest.mark.exec
def test_probed_fixture_accepts_its_real_streams(_av_fixture: str) -> None:
    g = compile_sql(f"SELECT a.video[1], a.audio[1] FROM input('{_av_fixture}') a")
    assert _outputs(g) == [
        ("src:a:v:0", "video", None),
        ("src:a:a:0", "audio", None),
    ]


@pytest.mark.exec
def test_no_probe_flag_skips_the_bounds_check_on_a_real_file(_av_fixture: str) -> None:
    g = compile_sql(f"SELECT a.audio[3] FROM input('{_av_fixture}') a", probe=False)
    assert _outputs(g) == [("src:a:a:2", "audio", None)]


@pytest.mark.exec
def test_empty_stream_array_is_a_typed_error(_fixtures: None) -> None:
    """testsrc.mp4 is video-only: splatting its audio must not silently
    produce a zero-output graph (a command with no -map)."""
    path = (FIXTURES_DIR / "testsrc.mp4").as_posix()
    err = _reject(f"SELECT a.audio FROM input('{path}') a")
    assert err.code is ErrorCode.STREAM_NOT_FOUND
    assert "no audio streams" in err.message


# ---------------------------------------------------------------------------
# broadcasting against the real 2-audio-track fixture
# ---------------------------------------------------------------------------


@pytest.mark.exec
def test_reverb_over_every_language_track(_av2_fixture: str) -> None:
    """The headline case: one aecho per language track, each output still tagged."""
    g = compile_sql(f"SELECT reverb(a.audio, 0.3) AS dubbed FROM input('{_av2_fixture}') a")
    assert _filters(g) == ["aecho", "aecho"]
    assert g.nodes["n1"].inputs == ["src:a:a:0"]
    assert g.nodes["n2"].inputs == ["src:a:a:1"]
    assert [(o.ref, o.name) for o in g.outputs] == [("n1", "dubbed"), ("n2", "dubbed")]
    assert [o.metadata for o in g.outputs] == [{"language": "eng"}, {"language": "fra"}]


@pytest.mark.exec
def test_probed_scalar_broadcast_makes_one_node_per_real_stream(_av2_fixture: str) -> None:
    g = compile_sql(f"SELECT volume(a.audio, 0.5) FROM input('{_av2_fixture}') a")
    assert _filters(g) == ["volume", "volume"]
    assert len(g.outputs) == 2


@pytest.mark.exec
def test_probed_splat_passes_every_track_through(_av2_fixture: str) -> None:
    g = compile_sql(f"SELECT a.video, a.audio FROM input('{_av2_fixture}') a")
    assert g.nodes == {}
    assert _outputs(g) == [
        ("src:a:v:0", "video", None),
        ("src:a:a:0", "audio", None),
        ("src:a:a:1", "audio", None),
    ]
    # the video track's mp4-stamped language=und says nothing and is dropped
    assert [o.metadata for o in g.outputs] == [
        {},
        {"language": "eng"},
        {"language": "fra"},
    ]


@pytest.mark.exec
def test_zip_mismatch_across_two_real_files(_av_fixture: str, _av2_fixture: str) -> None:
    """av2.mp4 has 2 audio tracks, av.mp4 has 1: nothing to zip."""
    err = _reject(
        f"SELECT amix(a.audio, b.audio) "
        f"FROM input('{_av2_fixture}') a, input('{_av_fixture}') b"
    )
    assert err.code is ErrorCode.BROADCAST_MISMATCH
    assert "a.audio has 2 streams" in err.message
    assert "b.audio has 1 stream" in err.message


@pytest.mark.exec
def test_probed_cte_array_subscript_against_a_real_file(_av2_fixture: str) -> None:
    err = _reject(
        f"WITH c AS (SELECT a.audio AS snd FROM input('{_av2_fixture}') a) "
        f"SELECT c.snd[3] FROM c"
    )
    assert err.code is ErrorCode.STREAM_NOT_FOUND
    assert "column 'c.snd' has 2 streams" in err.message


# ---------------------------------------------------------------------------
# UNION ALL over splatted arrays, against the two real 2-track fixtures
# ---------------------------------------------------------------------------


@pytest.mark.exec
def test_union_splat_pairs_every_language_track(_av2_fixture: str, _av3_fixture: str) -> None:
    """The flagship case: two dual-language files, `<alias>.audio` splatted in
    both branches, and the tracks pair up by position -- eng with eng, fra with
    fra -- into one concat with 1 video and 2 audio pads."""
    g = compile_sql(
        f"SELECT a.frame, a.audio FROM input('{_av2_fixture}') a "
        f"UNION ALL SELECT b.frame, b.audio FROM input('{_av3_fixture}') b"
    )
    assert _filters(g) == ["concat"]
    assert g.nodes["n1"].args == {"n": 2, "v": 1, "a": 2}
    assert g.nodes["n1"].inputs == [
        "src:a:v:0", "src:a:a:0", "src:a:a:1",
        "src:b:v:0", "src:b:a:0", "src:b:a:1",
    ]
    assert len(g.outputs) == 3
    assert [o.type for o in g.outputs] == ["video", "audio", "audio"]
    assert [o.metadata for o in g.outputs] == [
        {},
        {"language": "eng"},
        {"language": "fra"},
    ]


@pytest.mark.exec
def test_union_of_disagreeing_tracks_drops_the_language(
    _av2_fixture: str, _av3_fixture: str
) -> None:
    """av2's first track is eng and av3's second is fra: the concat output is
    neither, so it carries no language at all."""
    g = compile_sql(
        f"SELECT a.audio[1] FROM input('{_av2_fixture}') a "
        f"UNION ALL SELECT b.audio[2] FROM input('{_av3_fixture}') b"
    )
    assert [o.metadata for o in g.outputs] == [{}]
