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

Tier-2 behavior (RFC-003) is tested twice over: once against a `Registry`
built from the captured ffmpeg output embedded below, so the default suite
stays offline and machine-independent, and once (``exec``-marked) against the
real installed ffmpeg, where only what ffmpeg itself guarantees is asserted.
"""

# ruff: noqa: E501
# The `-filters` / `-help filter=X` fixture text below is embedded verbatim
# (byte-for-byte, not retyped or rewrapped) so it stays trustworthy as a
# record of real ffmpeg output; a few of its option lines exceed the 100-col
# limit. Whole-file exemption for the same reason tests/test_registry.py
# takes one: a per-line noqa comment would have to sit inside the fixture
# string it applies to, corrupting it.

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import sqlglot

from sqlmpeg import compiler
from sqlmpeg import lower as lower_module
from sqlmpeg import registry as registry_module
from sqlmpeg.compiler import compile_sql
from sqlmpeg.emit import build_ffmpeg_args, emit
from sqlmpeg.errors import ErrorCode, SqlmpegError
from sqlmpeg.ir import Graph, StreamType
from sqlmpeg.lower import lower
from sqlmpeg.parser import parse, resolve
from sqlmpeg.probe import ProbeResult, StreamMeta
from sqlmpeg.registry import Registry
from sqlmpeg.split import insert_splits

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"


# README ```sql blocks are dispatched by CONTENT, not by position, so moving
# an example up or down the page does not silently re-point a test. Both
# examples name files nobody has; each is compiled against the real
# two-language fixtures instead, which is exactly how its shown command was
# produced. Two separate mappings (not one shared dict) because both examples
# reuse the same two fixtures under different shown names -- a single
# fixture->name mapping could not tell them apart in the reverse direction.
_UNION_README_PATHS = {"episode1.mkv": "av2.mp4", "episode2.mkv": "av3.mp4"}
_FLAGSHIP_README_PATHS = {"film.mkv": "av2.mp4", "commentary.mkv": "av3.mp4"}


def _readme_text() -> str:
    return (REPO_ROOT / "README.md").read_text(encoding="utf-8")


def _readme_block(needle: str, *, exclude: str | None = None) -> str:
    """The one ```sql block of README.md containing `needle`, verbatim.

    `exclude`, if given, drops any block that ALSO contains that substring --
    needed for the Encoding section (plan 028), whose ```sql block wraps the
    flagship query verbatim in `COPY (...)`, so it contains every needle the
    flagship's own block does (e.g. "commentary").
    """
    blocks = re.findall(r"```sql\n(.*?)```", _readme_text(), re.DOTALL)
    assert blocks, "README.md no longer contains a ```sql block"
    matching = [
        str(block)
        for block in blocks
        if needle in block and (exclude is None or exclude not in block)
    ]
    assert len(matching) == 1, f"expected exactly one README ```sql block with {needle!r}"
    return matching[0]


def _readme_union_sql() -> str:
    """The headline UNION ALL splat, re-pointed at the real fixtures.

    The example names 'episode1.mkv'/'episode2.mkv' for readability; av2.mp4
    and av3.mp4 are what those stand in for -- two files with one video and two
    audio tracks tagged eng/fra apiece -- and splatting an array needs a file
    that can actually be probed for its stream count.
    """
    sql = _readme_block("episode1.mkv")
    for shown, fixture in _UNION_README_PATHS.items():
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


_LAYOUT_TYPES: dict[str, StreamType] = {
    "v": "video",
    "a": "audio",
    "s": "subtitle",
    "d": "data",
}


def _layout_probe(
    layout: str, tags: dict[int, dict[str, str]] | None = None
) -> ProbeResult:
    """A ProbeResult in FILE order, written as a compact layout (RFC-004).

    One character per stream -- ``v``/``a``/``s``/``d`` -- so ``"vasd"`` is the
    four-type container a star has to expand in order, and ``"vas"`` is
    avs.mkv's shape. Per-type indices are assigned in file order, exactly as
    :mod:`sqlmpeg.probe` does. `tags` maps a FILE position (not a per-type
    index) to that stream's metadata.
    """
    counters: dict[str, int] = {}
    streams: list[StreamMeta] = []
    for position, letter in enumerate(layout):
        index = counters.get(letter, 0)
        counters[letter] = index + 1
        streams.append(
            StreamMeta(
                type=_LAYOUT_TYPES[letter],
                index=index,
                metadata=dict((tags or {}).get(position, {})),
                width=320 if letter == "v" else None,
                height=240 if letter == "v" else None,
                fps="15/1" if letter == "v" else None,
                sample_rate=44100 if letter == "a" else None,
            )
        )
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
    for name, fixture in _UNION_README_PATHS.items():
        shown = shown.replace((FIXTURES_DIR / fixture).as_posix(), name)
    assert shown in _readme_text(), shown


# ---------------------------------------------------------------------------
# the README flagship: PiP composite + broadcast-zip mix (plan 024)
# ---------------------------------------------------------------------------


def _readme_flagship_sql() -> str:
    """The headline: a CTE carrying a video column AND a whole audio array,
    re-pointed at the real fixtures (film=av2, commentary=av3).

    `c.audio` (in the CTE) and `f.audio` (in the outer `volume()` calls) are
    both bare arrays -- broadcasting them needs a real, readable file to know
    how many streams there are, same reason the union-splat example below
    needs one.
    """
    sql = _readme_block("commentary", exclude="COPY (")
    for shown, fixture in _FLAGSHIP_README_PATHS.items():
        sql = sql.replace(shown, (FIXTURES_DIR / fixture).as_posix())
    return sql


@pytest.mark.exec
def test_readme_flagship_lowers_to_expected_nodes(_fixtures: None) -> None:
    """scale->overlay composites the video; volume broadcasts over each
    2-track audio array (4 nodes) and amix zips the pairs (2 nodes)."""
    g = compile_sql(_readme_flagship_sql())
    assert _filters(g) == [
        "scale", "overlay", "volume", "volume", "volume", "volume", "amix", "amix",
    ]
    # CTEs are traversed first, so the CTE's alias `c` takes input 0.
    assert g.sources == {"c": 0, "f": 1}
    assert g.nodes["n2"].inputs == ["src:f:v:0", "n1"]  # overlay(f.frame, pip.frame, ...)
    # amix zips f.audio[k]*0.65 with c.audio[k]*0.35, one pair per language
    assert g.nodes["n7"].inputs == ["n3", "n5"]
    assert g.nodes["n8"].inputs == ["n4", "n6"]


@pytest.mark.exec
def test_readme_flagship_selects_its_three_streams(_fixtures: None) -> None:
    """The SELECT list is the output list: one composited video column, and
    the zipped mix broadcasts into one audio column per language."""
    g = compile_sql(_readme_flagship_sql())
    assert [o.type for o in g.outputs] == ["video", "audio", "audio"]


@pytest.mark.exec
def test_readme_flagship_amix_pairs_keep_the_agreed_language(_fixtures: None) -> None:
    """Deliverable 1: a multi-stream call threads provenance when every zipped
    input agrees. Both fixtures tag audio[1]=eng, audio[2]=fra, so each mixed
    pair keeps its language; the composited video has no tag on either side
    (probed, untagged video streams) for `overlay` to agree on."""
    g = compile_sql(_readme_flagship_sql())
    assert [o.metadata for o in g.outputs] == [
        {},
        {"language": "eng"},
        {"language": "fra"},
    ]


@pytest.mark.exec
def test_readme_flagship_emits_a_filtergraph(_fixtures: None) -> None:
    e = emit(compile_sql(_readme_flagship_sql()))
    assert "scale=" in e.filter_complex
    assert "overlay=" in e.filter_complex
    assert "amix=" in e.filter_complex
    assert [m.target for m in e.maps] == ["[out0]", "[out1]", "[out2]"]
    assert [m.copy for m in e.maps] == [False, False, False]


@pytest.mark.exec
def test_readme_flagship_command_is_the_real_compilation(_fixtures: None) -> None:
    """The command shown under the headline is what sqlmpeg actually prints for
    that query, with only the fixture paths written back to the shown names."""
    args = build_ffmpeg_args(emit(compile_sql(_readme_flagship_sql())), "pip.mkv")
    shown = shlex.join(args)
    for name, fixture in _FLAGSHIP_README_PATHS.items():
        shown = shown.replace((FIXTURES_DIR / fixture).as_posix(), name)
    assert shown in _readme_text(), shown


# ---------------------------------------------------------------------------
# the README Encoding section: the flagship wrapped in COPY (plan 028)
# ---------------------------------------------------------------------------


def _readme_encoding_sql() -> str:
    """The Encoding section's ```sql block: the flagship verbatim inside a
    COPY ... TO ... WITH (...), re-pointed at the real fixtures the same way."""
    sql = _readme_block("COPY (")
    for shown, fixture in _FLAGSHIP_README_PATHS.items():
        sql = sql.replace(shown, (FIXTURES_DIR / fixture).as_posix())
    return sql


@pytest.mark.exec
def test_readme_encoding_wraps_the_flagship_query_in_a_sink(_fixtures: None) -> None:
    """Wrapping the flagship in COPY adds a sink and leaves the rest of the
    graph untouched -- same shape as `test_sink_does_not_change_the_graph_shape`."""
    plain = compile_sql(_readme_flagship_sql()).to_dict()
    wrapped = compile_sql(_readme_encoding_sql()).to_dict()
    assert wrapped.pop("sink") == {
        "path": "pip.mkv",
        "options": {
            "video_codec": "libx264",
            "crf": 20,
            "audio_codec": "aac",
            "audio_bitrate": "192k",
        },
    }
    assert wrapped == plain


@pytest.mark.exec
def test_readme_encoding_command_is_the_real_compilation(_fixtures: None) -> None:
    """The command shown under Encoding is what sqlmpeg actually prints for
    that query, with only the fixture paths written back to the shown names.

    No `-o` override: the sink's own `TO 'pip.mkv'` supplies the path, same
    precedence `sqlmpeg run query.sql` (no `-o`) would use.
    """
    args = build_ffmpeg_args(emit(compile_sql(_readme_encoding_sql())))
    shown = shlex.join(args)
    for name, fixture in _FLAGSHIP_README_PATHS.items():
        shown = shown.replace((FIXTURES_DIR / fixture).as_posix(), name)
    assert shown in _readme_text(), shown


# ---------------------------------------------------------------------------
# the README "Any ffmpeg filter" example (plan 032, RFC-003)
# ---------------------------------------------------------------------------

_DYNAMIC_README_PATHS = {"clip.mp4": "av.mp4"}


def _readme_dynamic_sql() -> str:
    """The tier-2 example in the "Any ffmpeg filter" section, re-pointed at
    the real fixture -- `unsharp` is not in any stdlib table, so compiling it
    needs the installed ffmpeg to introspect, same as any other tier-2 call."""
    sql = _readme_block("unsharp")
    for shown, fixture in _DYNAMIC_README_PATHS.items():
        sql = sql.replace(shown, (FIXTURES_DIR / fixture).as_posix())
    return sql


@pytest.mark.exec
def test_readme_dynamic_filter_example_compiles(_fixtures: None) -> None:
    """Name, pad signature and options all come from the installed ffmpeg;
    the two named options render in the order they were written."""
    g = compile_sql(_readme_dynamic_sql())
    assert _filters(g) == ["unsharp"]
    assert g.nodes["n1"].args == {"luma_msize_x": 7, "luma_amount": 1.5}
    assert _outputs(g) == [("n1", "video", None), ("src:a:a:0", "audio", None)]


@pytest.mark.exec
def test_readme_dynamic_filter_command_is_the_real_compilation(_fixtures: None) -> None:
    """The command shown under "Any ffmpeg filter" is what sqlmpeg actually
    prints for that query, with only the fixture path written back to the
    shown name."""
    args = build_ffmpeg_args(emit(compile_sql(_readme_dynamic_sql())), "out.mp4")
    shown = shlex.join(args)
    for name, fixture in _DYNAMIC_README_PATHS.items():
        shown = shown.replace((FIXTURES_DIR / fixture).as_posix(), name)
    assert shown in _readme_text(), shown


def test_readme_flagship_scale_factor_is_not_a_decimal() -> None:
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


def test_broadcast_composes_with_the_input_seek() -> None:
    """One window on the -i covers every element of the broadcast array.

    RFC-004's input seek: no atrim/asetpts pair per element any more -- the
    calls consume the raw stream refs and the whole input is seeked once.
    """
    g = _lower(
        "SELECT volume(a.audio, 0.5), reverb(a.audio, 0.3) FROM input('x.mp4') a "
        "WHERE a.t BETWEEN 1 AND 2",
        {"a": _probe_result(audios=2)},
    )
    assert _filters(g) == ["volume", "volume", "aecho", "aecho"]
    assert g.input_trims == {"a": (1, 2)}
    assert [g.nodes[n].inputs for n in ("n1", "n2", "n3", "n4")] == [
        ["src:a:a:0"],
        ["src:a:a:1"],
        ["src:a:a:0"],
        ["src:a:a:1"],
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


def test_amix_keeps_provenance_both_inputs_agree_on() -> None:
    """Plan 024: a multi-stream call is a join like concat -- it threads the
    tag when every stream feeding it agrees, so mixing two English tracks
    yields an English track."""
    g = _lower(
        "SELECT amix(a.audio[1], a.audio[2]) FROM input('x.mp4') a",
        {
            "a": _probe_result(
                audios=2, per_audio_tags=[{"language": "eng"}, {"language": "eng"}]
            )
        },
    )
    assert _filters(g) == ["amix"]
    assert g.outputs[0].metadata == {"language": "eng"}


def test_amix_drops_provenance_its_two_inputs_disagree_on() -> None:
    """f.audio[1]=eng mixed with f.audio[2]=fra: two stream inputs that say
    different things have nothing in common to thread."""
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


def test_broadcast_through_amix_keeps_each_pairs_agreed_language() -> None:
    """The flagship shape: two 2-track sources tagged eng/fra alike, zipped by
    amix -- each pair keeps the language both sides of it agree on."""
    g = _lower(
        "SELECT amix(a.audio, b.audio) FROM input('x.mp4') a, input('y.mp4') b",
        {
            "a": _probe_result(
                audios=2, per_audio_tags=[{"language": "eng"}, {"language": "fra"}]
            ),
            "b": _probe_result(
                audios=2, per_audio_tags=[{"language": "eng"}, {"language": "fra"}]
            ),
        },
    )
    assert [o.metadata for o in g.outputs] == [{"language": "eng"}, {"language": "fra"}]


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
    """`a`'s two mixed tracks disagree (eng vs fra), so the amix segment has
    no source at all and the pad cannot agree with the other one whatever it
    says."""
    g = _lower(
        "SELECT amix(a.audio[1], a.audio[2]) FROM input('x.mp4') a "
        "UNION ALL SELECT b.audio[1] FROM input('y.mp4') b",
        {
            "a": _probe_result(
                audios=2, per_audio_tags=[{"language": "eng"}, {"language": "fra"}]
            ),
            "b": _probe_result(audio_tags={"language": "eng"}),
        },
    )
    assert g.outputs[0].metadata == {}


def test_concat_after_agreeing_amix_keeps_the_shared_language() -> None:
    """Plan 024: `a`'s two mixed tracks agree (eng and eng), so the amix
    segment threads that language into the concat pad, and the other segment
    agrees too -- the tag survives two joins deep."""
    g = _lower(
        "SELECT amix(a.audio[1], a.audio[2]) FROM input('x.mp4') a "
        "UNION ALL SELECT b.audio[1] FROM input('y.mp4') b",
        {
            "a": _probe_result(
                audios=2, per_audio_tags=[{"language": "eng"}, {"language": "eng"}]
            ),
            "b": _probe_result(audio_tags={"language": "eng"}),
        },
    )
    assert g.outputs[0].metadata == {"language": "eng"}


# ---------------------------------------------------------------------------
# WHERE on an INPUT alias -> input-level seek (RFC-004 amendment, plan 035)
#
# An input alias owns its own -i, so its window becomes Graph.input_trims and
# emit renders -ss/-to in front of that -i. No filter node is spliced and the
# stream refs come out of lowering UNCHANGED -- which is what makes a trimmed
# passthrough (stream copy) possible. The filter trim survives for CTE names
# only; those tests live in the CTE section below.
# ---------------------------------------------------------------------------


def test_where_between_seeks_the_input_and_leaves_the_video_ref_alone() -> None:
    g = _lower("SELECT hflip(a.frame) FROM input('x.mp4') a WHERE a.t BETWEEN 1 AND 2.5")
    assert g.to_dict()["nodes"] == [
        {
            "id": "n1",
            "filter": "hflip",
            "args": {},
            "inputs": ["src:a:v:0"],
            "outputs": ["video"],
        },
    ]
    assert g.input_trims == {"a": (1, 2.5)}
    assert g.to_dict()["input_trims"] == {"a": [1, 2.5]}
    assert _outputs(g) == [("n1", "video", None)]


def test_where_between_seeks_the_input_for_audio_too() -> None:
    """No atrim/asetpts pair: one seek covers every stream type of the input."""
    g = _lower("SELECT a.audio[1] FROM input('x.mp4') a WHERE a.t BETWEEN 1 AND 2")
    assert g.to_dict()["nodes"] == []
    assert g.input_trims == {"a": (1, 2)}
    assert _outputs(g) == [("src:a:a:0", "audio", None)]


def test_a_trimmed_column_that_nothing_filters_stays_a_passthrough() -> None:
    """The new capability: a WHERE no longer forces a re-encode.

    The ref is still a source ref, so emit maps it bare and stream-copies it,
    with the window carried as input options on the -i.
    """
    g = compile_sql(
        "SELECT a.video[1] FROM input('x.mp4') a WHERE a.t BETWEEN 5 AND 60",
        probe=False,
    )
    assert g.nodes == {}
    assert _outputs(g) == [("src:a:v:0", "video", None)]
    emitted = emit(g)
    assert emitted.filter_complex == ""
    assert build_ffmpeg_args(emitted, "out.mp4") == [
        "ffmpeg",
        "-ss",
        "5",
        "-to",
        "60",
        "-i",
        "x.mp4",
        "-map",
        "0:v:0",
        "-c:0",
        "copy",
        "out.mp4",
    ]


def test_one_predicate_seeks_video_and_audio_in_sync() -> None:
    g = _lower(
        "SELECT a.video[1], a.audio[1] FROM input('x.mp4') a WHERE a.t BETWEEN 5 AND 10"
    )
    assert _filters(g) == []
    assert g.input_trims == {"a": (5, 10)}
    assert _outputs(g) == [("src:a:v:0", "video", None), ("src:a:a:0", "audio", None)]


def test_one_window_serves_every_consumer_of_that_input() -> None:
    g = _lower(
        "SELECT overlay(a.frame, a.frame, 5, 5) FROM input('x.mp4') a "
        "WHERE a.t BETWEEN 0 AND 3"
    )
    assert _filters(g) == ["overlay"]
    assert g.input_trims == {"a": (0, 3)}
    assert g.nodes["n1"].inputs == ["src:a:v:0", "src:a:v:0"]  # both arms, pre-split


def test_where_seeks_only_the_named_alias() -> None:
    g = _lower(
        "SELECT overlay(a.frame, b.frame, 0, 0) "
        "FROM input('x.mp4') a, input('y.mp4') b WHERE b.t BETWEEN 2 AND 4"
    )
    assert g.input_trims == {"b": (2, 4)}
    assert g.nodes["n1"].inputs == ["src:a:v:0", "src:b:v:0"]


def test_two_between_clauses_seek_both_inputs() -> None:
    g = _lower(
        "SELECT overlay(a.frame, b.frame, 0, 0) FROM input('x.mp4') a, input('y.mp4') b "
        "WHERE a.t BETWEEN 0 AND 1 AND b.t BETWEEN 2 AND 3"
    )
    assert _filters(g) == ["overlay"]
    assert g.input_trims == {"a": (0, 1), "b": (2, 3)}
    assert g.nodes["n1"].inputs == ["src:a:v:0", "src:b:v:0"]


def test_each_windowed_alias_gets_its_own_i_in_the_argv() -> None:
    """Two inputs, two windows: each -ss/-to sits in front of its own -i."""
    emitted = emit(
        compile_sql(
            "SELECT overlay(a.frame, b.frame, 0, 0) "
            "FROM input('x.mp4') a, input('y.mp4') b "
            "WHERE a.t BETWEEN 0 AND 1 AND b.t BETWEEN 2.5 AND 3",
            probe=False,
        )
    )
    assert emitted.input_trims == [(0, 1), (2.5, 3)]
    args = build_ffmpeg_args(emitted, "out.mp4")
    assert args[:11] == [
        "ffmpeg",
        "-ss",
        "0",
        "-to",
        "1",
        "-i",
        "x.mp4",
        "-ss",
        "2.5",
        "-to",
        "3",
    ]
    assert args[11:13] == ["-i", "y.mp4"]


def test_union_all_branches_seek_their_own_inputs() -> None:
    """Per-alias windows survive a UNION ALL: one -i each, concat unchanged."""
    g = compile_sql(
        "SELECT a.frame FROM input('x.mp4') a WHERE a.t BETWEEN 0 AND 1 "
        "UNION ALL "
        "SELECT b.frame FROM input('y.mp4') b WHERE b.t BETWEEN 2 AND 3",
        probe=False,
    )
    assert _filters(g) == ["concat"]
    assert g.nodes["n1"].inputs == ["src:a:v:0", "src:b:v:0"]
    assert g.input_trims == {"a": (0, 1), "b": (2, 3)}
    assert emit(g).input_trims == [(0, 1), (2, 3)]


def test_an_input_window_is_probe_independent() -> None:
    """The bounds are pure numbers from the SQL: probe=False changes nothing."""
    query = "SELECT a.video[1] FROM input('x.mp4') a WHERE a.t BETWEEN 1.5 AND 4"
    assert compile_sql(query, probe=False).input_trims == {"a": (1.5, 4)}
    assert compile_sql(query, probe=True).input_trims == {"a": (1.5, 4)}


def test_the_seek_covers_the_whole_input_selected_or_not() -> None:
    """A seek is an input option: unselected streams are seeked too (harmlessly
    -- they are never -mapped), and no filter node is created for anything."""
    g = _lower(
        "SELECT a.video[1] FROM input('x.mp4') a WHERE a.t BETWEEN 0 AND 1",
        {"a": _probe_result(videos=1, audios=2)},
    )
    assert _filters(g) == []
    assert g.input_trims == {"a": (0, 1)}
    assert _outputs(g) == [("src:a:v:0", "video", None)]


# ---------------------------------------------------------------------------
# open-ended input windows (plan 039): >= / <=, either operand order, merging
# ---------------------------------------------------------------------------


def test_tail_only_where_seeks_with_no_upper_bound() -> None:
    g = _lower("SELECT a.frame FROM input('x.mp4') a WHERE a.t >= 5")
    assert g.input_trims == {"a": (5, None)}
    emitted = emit(g)
    assert emitted.input_trims == [(5, None)]
    assert build_ffmpeg_args(emitted, "out.mp4")[:4] == ["ffmpeg", "-ss", "5", "-i"]


def test_head_only_where_seeks_with_no_lower_bound() -> None:
    g = _lower("SELECT a.frame FROM input('x.mp4') a WHERE a.t <= 60")
    assert g.input_trims == {"a": (None, 60)}
    emitted = emit(g)
    assert emitted.input_trims == [(None, 60)]
    assert build_ffmpeg_args(emitted, "out.mp4")[:4] == ["ffmpeg", "-to", "60", "-i"]


def test_flipped_operand_order_produces_the_same_window() -> None:
    """``120 <= a.t`` is the mirror of ``a.t >= 120`` -- exact, not approximate."""
    g_unflipped = _lower("SELECT a.frame FROM input('x.mp4') a WHERE a.t >= 120")
    g_flipped = _lower("SELECT a.frame FROM input('x.mp4') a WHERE 120 <= a.t")
    assert g_unflipped.input_trims == g_flipped.input_trims == {"a": (120, None)}


def test_gte_and_lte_merge_into_the_same_window_as_between() -> None:
    g_inequalities = _lower("SELECT a.frame FROM input('x.mp4') a WHERE a.t >= 1 AND a.t <= 2")
    g_between = _lower("SELECT a.frame FROM input('x.mp4') a WHERE a.t BETWEEN 1 AND 2")
    assert g_inequalities.input_trims == g_between.input_trims == {"a": (1, 2)}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a.frame FROM input('x.mp4') a WHERE a.t >= 5 AND a.t <= 2",
        "SELECT a.frame FROM input('x.mp4') a WHERE a.t BETWEEN 5 AND 2",
    ],
)
def test_empty_time_window_is_rejected(sql: str) -> None:
    err = _reject(sql)
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "empty time window" in err.message


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
    """A CTE name is a filtergraph pad, not an -i, so its window stays a FILTER
    trim (RFC-004: the one surviving use of trim/atrim)."""
    g = _lower(
        "WITH c AS (SELECT a.audio[1] AS snd FROM input('x.mp4') a) "
        "SELECT c.snd FROM c WHERE c.t BETWEEN 1 AND 2"
    )
    assert _filters(g) == ["atrim", "asetpts"]
    assert g.nodes["n1"].inputs == ["src:a:a:0"]
    assert g.nodes["n1"].args == {"start": 1, "end": 2}
    assert g.input_trims == {}  # nothing is seeked: the -i is untouched


def test_cte_open_lower_trim_node_carries_only_start() -> None:
    """Plan 039: a CTE trim with only one bound omits the other's arg entirely."""
    g = _lower(
        "WITH c AS (SELECT hflip(a.frame) AS pic FROM input('x.mp4') a) "
        "SELECT c.pic FROM c WHERE c.t >= 3"
    )
    assert _filters(g) == ["hflip", "trim", "setpts"]
    assert g.nodes["n2"].filter == "trim"
    assert g.nodes["n2"].args == {"start": 3}


def test_cte_open_upper_trim_node_carries_only_end() -> None:
    g = _lower(
        "WITH c AS (SELECT a.audio[1] AS snd FROM input('x.mp4') a) "
        "SELECT c.snd FROM c WHERE c.t <= 4"
    )
    assert _filters(g) == ["atrim", "asetpts"]
    assert g.nodes["n1"].args == {"end": 4}


def test_a_where_inside_a_cte_body_still_seeks_the_input() -> None:
    """The window is on `a`, an input alias, even though it is written inside a
    CTE body -- aliases are globally unique, so the seek is a graph property."""
    g = _lower(
        "WITH c AS ("
        "  SELECT hflip(a.frame) AS pic FROM input('x.mp4') a WHERE a.t BETWEEN 1 AND 2"
        ") SELECT vflip(c.pic) FROM c"
    )
    assert _filters(g) == ["hflip", "vflip"]
    assert g.input_trims == {"a": (1, 2)}


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


def test_overlay_keeps_the_agreed_video_tag() -> None:
    """Plan 024: overlay is a multi-stream join exactly like amix -- when both
    probed video streams it composites agree on a tag, the composite keeps
    it. (Use the same file under two aliases, same as the README headline.)"""
    g = _lower(
        "SELECT overlay(a.frame, b.frame, 0, 0) FROM input('x.mp4') a, input('y.mp4') b",
        {
            "a": _probe_result(video_tags={"language": "eng"}),
            "b": _probe_result(video_tags={"language": "eng"}),
        },
    )
    assert g.outputs[0].metadata == {"language": "eng"}


def test_overlay_drops_provenance_its_two_inputs_disagree_on() -> None:
    g = _lower(
        "SELECT overlay(a.frame, b.frame, 0, 0) FROM input('x.mp4') a, input('y.mp4') b",
        {
            "a": _probe_result(video_tags={"language": "eng"}),
            "b": _probe_result(video_tags={"language": "fra"}),
        },
    )
    assert g.outputs[0].metadata == {}


def test_overlay_drops_provenance_when_one_side_is_unprobed() -> None:
    """One input could not be probed at all, so it has no source to agree
    with the other -- same rule an unprobed concat segment follows."""
    g = _lower(
        "SELECT overlay(a.frame, b.frame, 0, 0) FROM input('x.mp4') a, input('y.mp4') b",
        {"a": _probe_result(video_tags={"language": "eng"}), "b": None},
    )
    assert g.outputs[0].metadata == {}


def test_a_colliding_builtin_is_an_unknown_function() -> None:
    """`lower` is a Postgres builtin sqlglot parses into its own Func class, and
    it is neither a stdlib function nor (in any ffmpeg) a filter name."""
    err = _reject("SELECT lower(a.frame) FROM input('x.mp4') a")
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
# RFC-004: subtitle / data columns -- same surface, passthrough-only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT a.subtitle[1] FROM input('x.mkv') a", ("src:a:s:0", "subtitle")),
        ("SELECT a.subtitle[2] FROM input('x.mkv') a", ("src:a:s:1", "subtitle")),
        ("SELECT a.data[1] FROM input('x.mkv') a", ("src:a:d:0", "data")),
    ],
)
def test_subtitle_and_data_subscripts_lower_to_s_and_d_refs(
    sql: str, expected: tuple[str, str]
) -> None:
    g = _lower(sql, {"a": _layout_probe("vassdd")})
    assert _outputs(g) == [(expected[0], expected[1], None)]
    assert g.nodes == {}  # passthrough-only: never a filtergraph node


def test_bare_subtitle_array_splats_like_any_other() -> None:
    g = _lower("SELECT a.subtitle FROM input('x.mkv') a", {"a": _layout_probe("vass")})
    assert _outputs(g) == [
        ("src:a:s:0", "subtitle", None),
        ("src:a:s:1", "subtitle", None),
    ]


def test_bare_data_array_splats_like_any_other() -> None:
    g = _lower("SELECT a.data FROM input('x.mkv') a", {"a": _layout_probe("vdd")})
    assert _outputs(g) == [
        ("src:a:d:0", "data", None),
        ("src:a:d:1", "data", None),
    ]


def test_bare_subtitle_array_over_an_unprobed_input_is_input_not_found() -> None:
    err = _reject("SELECT a.subtitle FROM input('does-not-exist.mkv') a")
    assert err.code is ErrorCode.INPUT_NOT_FOUND


def test_subtitle_subscript_is_bounds_checked_when_probed() -> None:
    err = _reject_lower(
        "SELECT a.subtitle[2] FROM input('x.mkv') a", {"a": _layout_probe("vas")}
    )
    assert err.code is ErrorCode.STREAM_NOT_FOUND
    assert "a.subtitle[2]" in err.message
    assert "1 subtitle stream" in err.message


def test_empty_subtitle_array_is_a_typed_error() -> None:
    err = _reject_lower(
        "SELECT a.subtitle FROM input('x.mkv') a", {"a": _layout_probe("va")}
    )
    assert err.code is ErrorCode.STREAM_NOT_FOUND
    assert "no subtitle streams" in err.message


def test_unprobed_subtitle_subscript_stays_symbolic() -> None:
    g = compile_sql("SELECT a.subtitle[3] FROM input('does-not-exist.mkv') a")
    assert _outputs(g) == [("src:a:s:2", "subtitle", None)]


def test_subtitle_passthrough_carries_its_language_tag() -> None:
    """Provenance rides the SAME passthrough metadata path audio tags do."""
    g = _lower(
        "SELECT a.subtitle[1] FROM input('x.mkv') a",
        {"a": _layout_probe("vas", tags={2: {"language": "eng", "title": "English"}})},
    )
    assert g.outputs[0].metadata == {"language": "eng", "title": "English"}


def test_unknown_input_column_hint_lists_subtitle_and_data() -> None:
    err = _reject("SELECT a.captions FROM input('x.mkv') a")
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert err.hint is not None
    assert "subtitle" in err.hint and "data" in err.hint


# -- passthrough-only: no function ever takes one ---------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT scale(a.subtitle[1], 0.5) FROM input('x.mkv') a",
        "SELECT hflip(a.subtitle[1]) FROM input('x.mkv') a",
        "SELECT volume(a.data[1], 0.5) FROM input('x.mkv') a",
        "SELECT amix(a.audio[1], a.subtitle[1]) FROM input('x.mkv') a",
        # the array form too: broadcasting does not launder the type
        "SELECT hflip(a.subtitle) FROM input('x.mkv') a",
    ],
)
def test_stdlib_call_over_a_passthrough_stream_is_udf_arg_type(sql: str) -> None:
    err = _reject_lower(sql, {"a": _layout_probe("vasd")})
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "cannot be filtered, only selected" in err.message


def test_dynamic_call_over_a_subtitle_stream_is_udf_arg_type_not_internal(
    _registry: Registry,
) -> None:
    """Tier 2's pad signature only ever holds video/audio, so a subtitle
    argument must produce the SAME typed rejection, never an INTERNAL."""
    err = _reject_dyn(
        "SELECT gblur(a.subtitle[1], sigma => 5) FROM input('x.mkv') a",
        _registry,
        {"a": _layout_probe("vas")},
    )
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "cannot be filtered, only selected" in err.message


def test_a_cte_may_carry_a_subtitle_column_through_as_passthrough() -> None:
    g = _lower(
        "WITH c AS (SELECT a.video[1] AS v, a.subtitle[1] AS caps "
        "FROM input('x.mkv') a) "
        "SELECT c.v, c.caps FROM c",
        {"a": _layout_probe("vas", tags={2: {"language": "eng"}})},
    )
    assert _outputs(g) == [
        ("src:a:v:0", "video", None),
        ("src:a:s:0", "subtitle", None),
    ]
    assert g.outputs[1].metadata == {"language": "eng"}


def test_filtering_a_cte_subtitle_column_is_still_rejected() -> None:
    err = _reject_lower(
        "WITH c AS (SELECT a.subtitle[1] AS caps FROM input('x.mkv') a) "
        "SELECT hflip(c.caps) FROM c",
        {"a": _layout_probe("vas")},
    )
    assert err.code is ErrorCode.UDF_ARG_TYPE


# -- passthrough-only: an INPUT seek trims them; a CTE's filter trim cannot


def test_where_over_a_consumed_subtitle_stream_is_rejected() -> None:
    """Measured 2026-08-15: ffmpeg does NOT retime caption packets under an
    input -ss (copy or transcode), so a seeked-and-selected caption track
    would desync by the seek amount. Rejected rather than shipped broken."""
    err = _reject_lower(
        "SELECT a.subtitle[1] FROM input('x.mkv') a WHERE a.t BETWEEN 1 AND 2",
        {"a": _layout_probe("vas")},
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "out of sync" in err.message


def test_where_over_a_consumed_subtitle_stream_is_rejected_for_an_open_window() -> None:
    """Plan 039: the desync rejection keys on `graph.input_trims` membership,
    so an open-ended window (`t >= x`, no upper bound at all) triggers it just
    as a closed BETWEEN does -- there is no window shape that is safe."""
    err = _reject_lower(
        "SELECT a.subtitle[1] FROM input('x.mkv') a WHERE a.t >= 1",
        {"a": _layout_probe("vas")},
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "out of sync" in err.message


def test_where_over_a_consumed_data_stream_is_rejected() -> None:
    err = _reject_lower(
        "SELECT a.data FROM input('x.mkv') a WHERE a.t BETWEEN 1 AND 2",
        {"a": _layout_probe("vad")},
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "data" in err.message


def test_star_plus_where_over_a_captioned_input_is_rejected() -> None:
    """`SELECT *` consumes the caption track, so the same desync rejection
    applies; trim + star works on caption-less files (covered elsewhere)."""
    err = _reject_lower(
        "SELECT * FROM input('x.mkv') a WHERE a.t BETWEEN 1 AND 2",
        {"a": _layout_probe("vas")},
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "out of sync" in err.message


def test_where_plus_unselected_captions_still_seeks() -> None:
    """The trim stays legal when the captions are NOT selected: unmapped
    streams are seeked harmlessly."""
    g = _lower(
        "SELECT a.video[1], a.audio[1] FROM input('x.mkv') a WHERE a.t BETWEEN 1 AND 2",
        {"a": _layout_probe("vas")},
    )
    assert _filters(g) == []
    assert g.input_trims == {"a": (1, 2)}
    assert _outputs(g) == [
        ("src:a:v:0", "video", None),
        ("src:a:a:0", "audio", None),
    ]


def test_where_over_a_cte_carrying_a_subtitle_column_is_rejected() -> None:
    """Permanent per RFC-004: a CTE trim is a filtergraph trim."""
    err = _reject_lower(
        "WITH c AS (SELECT a.video[1] AS v, a.subtitle[1] AS caps "
        "FROM input('x.mkv') a) "
        "SELECT c.v, c.caps FROM c WHERE c.t BETWEEN 1 AND 2",
        {"a": _layout_probe("vas")},
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "a CTE's captions cannot be trimmed" in err.message
    assert err.hint is not None and "external subtitle file" in err.hint


def test_where_that_does_not_touch_the_caption_alias_still_seeks_its_own_input() -> None:
    """A window is per alias: b's caption track is mapped whole, a is seeked."""
    g = _lower(
        "SELECT a.video[1], b.subtitle[1] FROM input('x.mkv') a, input('y.mkv') b "
        "WHERE a.t BETWEEN 1 AND 2",
        {"a": _layout_probe("vas"), "b": _layout_probe("vas")},
    )
    assert _filters(g) == []
    assert g.input_trims == {"a": (1, 2)}
    assert _outputs(g) == [("src:a:v:0", "video", None), ("src:b:s:0", "subtitle", None)]


def test_a_captioned_input_may_be_trimmed_when_captions_are_not_selected() -> None:
    g = _lower(
        "SELECT a.video[1] FROM input('x.mkv') a WHERE a.t BETWEEN 1 AND 2",
        {"a": _layout_probe("vas")},
    )
    assert _filters(g) == []
    assert g.input_trims == {"a": (1, 2)}


# -- passthrough-only: concat has no s/d pads -------------------------------


def test_union_all_branch_with_a_subtitle_column_is_rejected() -> None:
    err = _reject_lower(
        "SELECT a.video[1], a.subtitle[1] FROM input('x.mkv') a "
        "UNION ALL "
        "SELECT b.video[1], b.subtitle[1] FROM input('y.mkv') b",
        {"a": _layout_probe("vas"), "b": _layout_probe("vas")},
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "video and audio only" in err.message


# ---------------------------------------------------------------------------
# RFC-004: SELECT * and <alias>.*
# ---------------------------------------------------------------------------


def test_bare_star_expands_every_stream_in_file_order() -> None:
    g = _lower("SELECT * FROM input('x.mkv') a", {"a": _layout_probe("vasd")})
    assert _outputs(g) == [
        ("src:a:v:0", "video", None),
        ("src:a:a:0", "audio", None),
        ("src:a:s:0", "subtitle", None),
        ("src:a:d:0", "data", None),
    ]
    assert g.nodes == {}  # a star is pure passthrough


def test_star_follows_the_containers_own_stream_order() -> None:
    """File order, not type order: an audio-first container stays audio-first."""
    g = _lower("SELECT * FROM input('x.mkv') a", {"a": _layout_probe("asv")})
    assert [(o.ref, o.type) for o in g.outputs] == [
        ("src:a:a:0", "audio"),
        ("src:a:s:0", "subtitle"),
        ("src:a:v:0", "video"),
    ]


def test_bare_star_covers_every_from_alias_in_from_order() -> None:
    g = _lower(
        "SELECT * FROM input('x.mkv') a, input('y.mkv') b",
        {"a": _layout_probe("va"), "b": _layout_probe("vs")},
    )
    assert _outputs(g) == [
        ("src:a:v:0", "video", None),
        ("src:a:a:0", "audio", None),
        ("src:b:v:0", "video", None),
        ("src:b:s:0", "subtitle", None),
    ]


def test_qualified_star_covers_one_alias_and_mixes_with_other_columns() -> None:
    g = _lower(
        "SELECT a.*, b.audio[1] FROM input('x.mkv') a, input('y.mkv') b",
        {"a": _layout_probe("vas"), "b": _layout_probe("vaa")},
    )
    assert _outputs(g) == [
        ("src:a:v:0", "video", None),
        ("src:a:a:0", "audio", None),
        ("src:a:s:0", "subtitle", None),
        ("src:b:a:0", "audio", None),
    ]


def test_a_star_may_follow_an_explicit_column() -> None:
    g = _lower(
        "SELECT b.video[1], a.* FROM input('x.mkv') a, input('y.mkv') b",
        {"a": _layout_probe("as"), "b": _layout_probe("v")},
    )
    assert _outputs(g) == [
        ("src:b:v:0", "video", None),
        ("src:a:a:0", "audio", None),
        ("src:a:s:0", "subtitle", None),
    ]


def test_star_carries_provenance_metadata_per_stream() -> None:
    g = _lower(
        "SELECT * FROM input('x.mkv') a",
        {
            "a": _layout_probe(
                "vas",
                tags={
                    1: {"language": "fra"},
                    2: {"language": "eng", "title": "English"},
                },
            )
        },
    )
    assert [o.metadata for o in g.outputs] == [
        {},
        {"language": "fra"},
        {"language": "eng", "title": "English"},
    ]


def test_star_over_an_unprobed_input_is_input_not_found() -> None:
    err = _reject("SELECT * FROM input('does-not-exist.mkv') a")
    assert err.code is ErrorCode.INPUT_NOT_FOUND
    assert "does-not-exist.mkv" in err.message


def test_qualified_star_over_an_unprobed_input_is_input_not_found() -> None:
    err = _reject_lower(
        "SELECT b.*, a.video[1] FROM input('x.mkv') a, input('y.mkv') b",
        {"a": _layout_probe("v"), "b": None},
    )
    assert err.code is ErrorCode.INPUT_NOT_FOUND


def test_star_over_a_stream_less_input_is_a_typed_error() -> None:
    """An attachment-only container would expand to nothing at all."""
    err = _reject_lower("SELECT * FROM input('x.mkv') a", {"a": _layout_probe("")})
    assert err.code is ErrorCode.STREAM_NOT_FOUND
    assert "selects nothing" in err.message


def test_star_over_a_cte_expands_its_columns_statically() -> None:
    """No probe is consulted: the CTE's shape was fixed when its body lowered."""
    g = _lower(
        "WITH c AS (SELECT a.video[1] AS v, a.subtitle[1] AS caps "
        "FROM input('x.mkv') a) "
        "SELECT c.* FROM c",
        {"a": _layout_probe("vas")},
    )
    assert _outputs(g) == [
        ("src:a:v:0", "video", "v"),
        ("src:a:s:0", "subtitle", "caps"),
    ]


def test_star_over_a_cte_splats_its_array_columns() -> None:
    g = _lower(
        "WITH c AS (SELECT a.audio AS tracks FROM input('x.mkv') a) "
        "SELECT c.* FROM c",
        {"a": _layout_probe("vaa")},
    )
    assert _outputs(g) == [
        ("src:a:a:0", "audio", "tracks"),
        ("src:a:a:1", "audio", "tracks"),
    ]


def test_a_star_inside_a_cte_body_expands_there() -> None:
    g = _lower(
        "WITH c AS (SELECT * FROM input('x.mkv') a) SELECT c.* FROM c",
        {"a": _layout_probe("vas")},
    )
    assert [(o.ref, o.type) for o in g.outputs] == [
        ("src:a:v:0", "video"),
        ("src:a:a:0", "audio"),
        ("src:a:s:0", "subtitle"),
    ]


def test_star_over_an_unknown_cte_or_alias_is_unknown_alias() -> None:
    err = _reject_lower(
        "SELECT nope.* FROM input('x.mkv') a", {"a": _layout_probe("v")}
    )
    assert err.code is ErrorCode.UNKNOWN_ALIAS


def test_star_output_is_still_stream_copied_end_to_end() -> None:
    """`SELECT *` is a remux: no filtergraph, one -map + -c copy per stream."""
    g = insert_splits(_lower("SELECT * FROM input('x.mkv') a", {"a": _layout_probe("vas")}))
    emitted = emit(g)
    assert emitted.filter_complex == ""
    args = build_ffmpeg_args(emitted, "out.mkv")
    assert args.count("-map") == 3
    assert [m.target for m in emitted.maps] == ["0:v:0", "0:a:0", "0:s:0"]


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
    def boom(res: object, probes: object, **kwargs: object) -> Graph:
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


@pytest.mark.exec
def test_pipeline_output_survives_a_round_trip_through_dicts(_fixtures: None) -> None:
    g = compile_sql(_readme_flagship_sql())
    assert Graph.from_dict(g.to_dict()).to_dict() == g.to_dict()


# ---------------------------------------------------------------------------
# COPY ... TO ... WITH (...) -- the sink (RFC-002, plan 026)
# ---------------------------------------------------------------------------

SINK_QUERY = "SELECT a.frame FROM input('x.mp4') a"


def _sink_of(options: str) -> dict[str, object]:
    """The lowered sink of `SINK_QUERY` wrapped in a COPY with `options`."""
    with_clause = f" WITH ({options})" if options else ""
    g = _lower(f"COPY ({SINK_QUERY}) TO 'out.mkv'{with_clause}")
    assert g.sink is not None
    assert g.sink.path == "out.mkv"
    return g.sink.options


def test_bare_select_has_no_sink() -> None:
    g = _lower(SINK_QUERY)
    assert g.sink is None
    assert "sink" not in g.to_dict()


def test_copy_lowers_to_a_sink() -> None:
    assert _sink_of("video_codec 'libx264', crf 20, faststart true") == {
        "video_codec": "libx264",
        "crf": 20,
        "faststart": True,
    }


def test_sink_options_keep_their_written_order() -> None:
    assert list(_sink_of("crf 20, preset 'slow', audio_codec 'aac'")) == [
        "crf",
        "preset",
        "audio_codec",
    ]


def test_copy_without_options_lowers_to_an_empty_sink() -> None:
    assert _sink_of("") == {}


@pytest.mark.parametrize("written, value", [("true", True), ("false", False)])
def test_faststart_takes_a_bool(written: str, value: bool) -> None:
    assert _sink_of(f"faststart {written}") == {"faststart": value}


def test_sink_option_values_are_normalized_python_scalars() -> None:
    """The IR only ever carries str/int/bool -- no sqlglot nodes, no Decimal."""
    options = _sink_of("video_codec 'libx264', sample_rate 48000, faststart true")
    assert [type(v) for v in options.values()] == [str, int, bool]


def test_sink_survives_the_split_pass_and_serializes() -> None:
    g = compile_sql(
        "COPY (SELECT a.frame, a.frame FROM input('x.mp4') a) "
        "TO 'out.mp4' WITH (video_codec 'libx264', crf 18, faststart true)"
    )
    # the projection is used twice, so the split pass rebuilt the graph
    assert "split" in _filters(g)
    assert g.to_dict()["sink"] == {
        "path": "out.mp4",
        "options": {"video_codec": "libx264", "crf": 18, "faststart": True},
    }
    assert Graph.from_dict(g.to_dict()).to_dict() == g.to_dict()


def test_unknown_sink_option_is_anchored_on_its_own_line() -> None:
    err = _reject(
        f"COPY (\n  {SINK_QUERY}\n) TO 'out.mkv' WITH (\n"
        f"  crf 20,\n  bogus_option 'x'\n)"
    )
    assert err.code is ErrorCode.UNKNOWN_SINK_OPTION
    assert "unknown sink option 'bogus_option'" in err.message
    assert err.line == 5


def test_unknown_sink_option_suggests_the_near_miss() -> None:
    err = _reject(f"COPY ({SINK_QUERY}) TO 'out.mkv' WITH (crff 20)")
    assert err.code is ErrorCode.UNKNOWN_SINK_OPTION
    assert err.hint == "did you mean 'crf'?"


@pytest.mark.parametrize(
    "options, message",
    [
        ("crf 'high'", "expects an int, got 'high'"),
        ("crf 1.5", "expects an int, got 1.5"),
        ("crf true", "expects an int, got True"),
        ("faststart 1", "expects a bool, got 1"),
        ("faststart 'yes'", "expects a bool, got 'yes'"),
        ("faststart NULL", "expects a bool, got NULL"),
        # a bare word and a double-quoted word are neither a string nor a bool
        ("preset slow", "expects a str, got the bare word slow"),
        ('preset "slow"', 'expects a str, got the identifier "slow"'),
        ("video_codec 42", "expects a str, got 42"),
        ("video_codec true", "expects a str, got True"),
    ],
)
def test_bad_sink_option_value_is_a_type_error(options: str, message: str) -> None:
    err = _reject(f"COPY ({SINK_QUERY}) TO 'out.mkv' WITH ({options})")
    assert err.code is ErrorCode.SINK_OPTION_TYPE
    assert message in err.message
    assert err.hint is not None


def test_an_invalid_inner_query_beats_the_sink_options() -> None:
    """The COPY wrapper never masks (or is masked by) the query's own errors."""
    err = _reject(
        "COPY (SELECT a.frame FROM input('x.mp4') a GROUP BY a.frame) "
        "TO 'out.mkv' WITH (bogus_option 1)"
    )
    assert err.code is ErrorCode.NO_STREAMING_EQUIVALENT

    err = _reject(
        "COPY (SELECT nosuchfilter(a.frame) FROM input('x.mp4') a) "
        "TO 'out.mkv' WITH (bogus_option 1)"
    )
    assert err.code is ErrorCode.UNKNOWN_FUNCTION


def test_sink_does_not_change_the_graph_shape() -> None:
    """Wrapping a query in a COPY adds a sink and touches nothing else."""
    plain = compile_sql(SINK_QUERY).to_dict()
    wrapped = compile_sql(f"COPY ({SINK_QUERY}) TO 'out.mkv' WITH (crf 20)").to_dict()
    assert wrapped.pop("sink") == {"path": "out.mkv", "options": {"crf": 20}}
    assert wrapped == plain


# ---------------------------------------------------------------------------
# RFC-003: dynamic filters + named arguments, against an OFFLINE registry
# ---------------------------------------------------------------------------
#
# These build a real `Registry` over faked subprocess output, exactly as
# tests/test_registry.py does, so the whole tier-2 surface is exercised with
# no ffmpeg on the machine and no machine-dependent expectations. The
# `exec`-marked tests further down run the same shapes against the REAL
# installed ffmpeg, where the option tables are whatever that binary says.

_FILTERS_FIXTURE = """\
Filters:
  T.. = Timeline support
  .S. = Slice threading
  ..C = Command support
  A = Audio input/output
  V = Video input/output
  N = Dynamic number and/or type of input/output
  | = Source or sink filter
 .S. acrossover        A->N       Split audio into per-bands streams.
 ... aecho             A->A       Add echoing to the audio.
 ..C crop              V->V       Crop the input video.
 T.C feedback          VV->VV     Apply feedback video filter.
 TSC deband            V->V       Debands video.
 TSC gblur             V->V       Apply Gaussian Blur filter.
 ..C scale             V->V       Scale the input video size and/or convert the image format.
 ... split             V->N       Pass on the input to N video outputs.
 ... testsrc           |->V       Generate test pattern.
 ... trim              V->V       Pick one continuous section from the input, drop the rest.
 TS. unsharp           V->V       Sharpen or blur the input video.
 .S. xfade             VV->V      Cross fade one video with another video.
"""

# Each entry is the AVOptions block of a real `ffmpeg -hide_banner -help
# filter=X` capture (ffmpeg 7.1), trimmed to the block the parser reads --
# and, for xfade, to the first 16 of its 59 transition constants, which is
# still more than a message lists before it starts counting.
_HELP_FIXTURES: dict[str, str] = {
    "gblur": """\
gblur AVOptions:
   sigma             <float>      ..FV.....T. set sigma (from 0 to 1024) (default 0.5)
   steps             <int>        ..FV.....T. set number of steps (from 1 to 6) (default 1)
   planes            <int>        ..FV.....T. set planes to filter (from 0 to 15) (default 15)
   sigmaV            <float>      ..FV.....T. set vertical sigma (from -1 to 1024) (default -1)

""",
    "unsharp": """\
unsharp AVOptions:
   luma_msize_x      <int>        ..FV....... set luma matrix horizontal size (from 3 to 23) (default 5)
   lx                <int>        ..FV....... set luma matrix horizontal size (from 3 to 23) (default 5)
   luma_amount       <float>      ..FV....... set luma effect strength (from -2 to 5) (default 1)
   la                <float>      ..FV....... set luma effect strength (from -2 to 5) (default 1)

""",
    "deband": """\
deband AVOptions:
   range             <int>        ..FV.....T. set range (from INT_MIN to INT_MAX) (default 16)
   r                 <int>        ..FV.....T. set range (from INT_MIN to INT_MAX) (default 16)
   blur              <boolean>    ..FV.....T. set blur (default true)
   b                 <boolean>    ..FV.....T. set blur (default true)
   coupling          <boolean>    ..FV.....T. set plane coupling (default false)
   c                 <boolean>    ..FV.....T. set plane coupling (default false)

""",
    "aecho": """\
aecho AVOptions:
   in_gain           <float>      ..F.A...... set signal input gain (from 0 to 1) (default 0.6)
   out_gain          <float>      ..F.A...... set signal output gain (from 0 to 1) (default 0.3)
   delays            <string>     ..F.A...... set list of signal delays (default "1000")
   decays            <string>     ..F.A...... set list of signal decays (default "0.5")

""",
    "crop": """\
crop AVOptions:
   out_w             <string>     ..FV.....T. set the width crop area expression (default "iw")
   w                 <string>     ..FV.....T. set the width crop area expression (default "iw")
   out_h             <string>     ..FV.....T. set the height crop area expression (default "ih")
   h                 <string>     ..FV.....T. set the height crop area expression (default "ih")
   keep_aspect       <boolean>    ..FV....... keep aspect ratio (default false)
   exact             <boolean>    ..FV....... do exact cropping (default false)

""",
    "scale": """\
scale AVOptions:
   w                 <string>     ..FV.....T. Output video width
   width             <string>     ..FV.....T. Output video width
   h                 <string>     ..FV.....T. Output video height
   height            <string>     ..FV.....T. Output video height
   flags             <string>     ..FV....... Flags to pass to libswscale (default "")
   interl            <boolean>    ..FV....... set interlacing (default false)

""",
    "xfade": """\
xfade AVOptions:
   transition        <int>        ..FV....... set cross fade transition (from -1 to 57) (default fade)
     custom          -1           ..FV....... custom transition
     fade            0            ..FV....... fade transition
     wipeleft        1            ..FV....... wipe left transition
     wiperight       2            ..FV....... wipe right transition
     wipeup          3            ..FV....... wipe up transition
     wipedown        4            ..FV....... wipe down transition
     slideleft       5            ..FV....... slide left transition
     slideright      6            ..FV....... slide right transition
     slideup         7            ..FV....... slide up transition
     slidedown       8            ..FV....... slide down transition
     circlecrop      9            ..FV....... circle crop transition
     rectcrop        10           ..FV....... rect crop transition
     distance        11           ..FV....... distance transition
     fadeblack       12           ..FV....... fadeblack transition
     fadewhite       13           ..FV....... fadewhite transition
   duration          <duration>   ..FV....... set cross fade duration (default 1)
   offset            <duration>   ..FV....... set cross fade start relative to first input stream (default 0)
   expr              <string>     ..FV....... set expression for custom transition

""",
}

_FIXTURE_VERSION_LINE = (
    "ffmpeg version 7.1-full_build-www.gyan.dev Copyright (c) 2000-2024 the FFmpeg developers"
)


@pytest.fixture
def _registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Registry:
    """A real Registry over the captured fixtures above -- no ffmpeg needed.

    A fresh instance rather than ``registry.load()``: the singleton is
    process-wide, and the disk cache is redirected into tmp_path so a test
    never reads (or writes) the developer's own ~/.cache/sqlmpeg.
    """
    monkeypatch.setattr(registry_module, "_cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(registry_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "-version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=_FIXTURE_VERSION_LINE, stderr="")
        if "-filters" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=_FILTERS_FIXTURE, stderr="")
        for arg in argv:
            if arg.startswith("filter="):
                name = arg[len("filter=") :]
                return subprocess.CompletedProcess(
                    argv, 0, stdout=_HELP_FIXTURES.get(name, ""), stderr=""
                )
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    monkeypatch.setattr(registry_module.subprocess, "run", fake_run)
    return Registry()


def _dyn(
    sql: str,
    registry: Registry | None,
    probes: dict[str, ProbeResult | None] | None = None,
    *,
    portable: bool = False,
) -> Graph:
    return lower(resolve(parse(sql)), probes or {}, registry=registry, portable=portable)


def _reject_dyn(
    sql: str,
    registry: Registry | None,
    probes: dict[str, ProbeResult | None] | None = None,
    *,
    portable: bool = False,
) -> SqlmpegError:
    with pytest.raises(SqlmpegError) as excinfo:
        _dyn(sql, registry, probes, portable=portable)
    return _anchored(excinfo.value)


# -- tier 2: calling a filter the registry reports --------------------------


def test_dynamic_filter_lowers_to_a_plain_node(_registry: Registry) -> None:
    g = _dyn("SELECT gblur(a.frame, sigma => 5) FROM input('x.mp4') a", _registry)
    node = g.nodes["n1"]
    assert node.filter == "gblur"
    assert node.args == {"sigma": 5}
    assert node.inputs == ["src:a:v:0"]
    assert node.outputs == ["video"]
    assert _outputs(g) == [("n1", "video", None)]


def test_dynamic_filter_without_options_sets_no_args(_registry: Registry) -> None:
    g = _dyn("SELECT gblur(a.frame) FROM input('x.mp4') a", _registry)
    assert g.nodes["n1"].args == {}


def test_dynamic_options_keep_their_written_order(_registry: Registry) -> None:
    """emit renders args in insertion order, so written order is the rendered
    order -- both directions are checked here."""
    g = _dyn(
        "SELECT unsharp(a.frame, luma_amount => 1.5, luma_msize_x => 7) "
        "FROM input('x.mp4') a",
        _registry,
    )
    assert list(g.nodes["n1"].args.items()) == [("luma_amount", 1.5), ("luma_msize_x", 7)]
    assert "unsharp=luma_amount=1.5:luma_msize_x=7" in emit(insert_splits(g)).filter_complex


def test_dynamic_filter_takes_only_its_pads_positionally(_registry: Registry) -> None:
    err = _reject_dyn("SELECT gblur(a.frame, 5) FROM input('x.mp4') a", _registry)
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "gblur(video), got gblur(video, num)" in err.message
    assert err.hint is not None and "options by name" in err.hint


def test_dynamic_filter_checks_its_pad_types(_registry: Registry) -> None:
    err = _reject_dyn("SELECT gblur(a.audio[1], sigma => 1) FROM input('x.mp4') a", _registry)
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "got gblur(audio)" in err.message


def test_two_pad_dynamic_filter_needs_both_inputs(_registry: Registry) -> None:
    err = _reject_dyn("SELECT xfade(a.frame) FROM input('x.mp4') a", _registry)
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "xfade(video, video), got xfade(video)" in err.message


def test_two_pad_dynamic_filter_lowers_both_inputs(_registry: Registry) -> None:
    g = _dyn(
        "SELECT xfade(a.frame, b.frame, transition => 'wipeleft', duration => 1) "
        "FROM input('x.mp4') a, input('y.mp4') b",
        _registry,
    )
    assert g.nodes["n1"].inputs == ["src:a:v:0", "src:b:v:0"]
    assert g.nodes["n1"].args == {"transition": "wipeleft", "duration": 1}


def test_excluded_filters_are_not_callable(_registry: Registry) -> None:
    """The v1 scope fence lives in the registry: dynamic pads (acrossover),
    multiple outputs (feedback) and sources (testsrc) are all in the fixture's
    -filters output but excluded, so lowering never sees them at all."""
    for sql in (
        "SELECT acrossover(a.audio[1]) FROM input('x.mp4') a",
        "SELECT feedback(a.frame, a.frame) FROM input('x.mp4') a",
        "SELECT testsrc(a.frame) FROM input('x.mp4') a",
    ):
        assert _reject_dyn(sql, _registry).code is ErrorCode.UNKNOWN_FUNCTION


def test_the_stdlib_wins_a_name_collision(_registry: Registry) -> None:
    """`scale` and `crop` are both stdlib functions and real ffmpeg filters:
    the stdlib's argument order and remapping are what a query gets."""
    g = _dyn("SELECT scale(a.frame, 0.5) FROM input('x.mp4') a", _registry)
    assert g.nodes["n1"].args == {"w": "iw*0.5", "h": "-2"}
    g = _dyn("SELECT crop(a.frame, 1, 2, 3, 4) FROM input('x.mp4') a", _registry)
    assert g.nodes["n1"].args == {"w": 3, "h": 4, "x": 1, "y": 2}


def test_a_builtin_that_is_also_a_filter_resolves_to_tier_two(_registry: Registry) -> None:
    """sqlglot parses `trim(...)` with its own TRIM grammar, which parks the
    argument under `this` rather than in the argument list -- so the call
    resolves to ffmpeg's trim filter but arrives with NO positional args. The
    rejection is typed (and names the pad signature), not a panic."""
    err = _reject_dyn("SELECT trim(a.frame) FROM input('x.mp4') a", _registry)
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "trim(video), got trim()" in err.message


def test_a_dynamic_call_nests_inside_a_stdlib_call(_registry: Registry) -> None:
    g = _dyn("SELECT scale(gblur(a.frame, sigma => 2), 0.5) FROM input('x.mp4') a", _registry)
    assert _filters(g) == ["gblur", "scale"]
    assert g.nodes["n2"].inputs == ["n1"]


def test_a_stdlib_call_nests_inside_a_dynamic_call(_registry: Registry) -> None:
    g = _dyn("SELECT gblur(scale(a.frame, 0.5), sigma => 2) FROM input('x.mp4') a", _registry)
    assert _filters(g) == ["scale", "gblur"]
    assert g.nodes["n2"].inputs == ["n1"]


# -- named option validation (both tiers, same two codes) -------------------


def test_unknown_option_suggests_a_real_one(_registry: Registry) -> None:
    err = _reject_dyn("SELECT gblur(a.frame, sigmma => 5) FROM input('x.mp4') a", _registry)
    assert err.code is ErrorCode.UNKNOWN_FILTER_OPTION
    assert "filter 'gblur' has no option 'sigmma'" in err.message
    assert err.hint is not None and "sigma" in err.hint


def test_unknown_option_without_a_match_lists_the_real_options(_registry: Registry) -> None:
    err = _reject_dyn("SELECT gblur(a.frame, zzzz => 5) FROM input('x.mp4') a", _registry)
    assert err.code is ErrorCode.UNKNOWN_FILTER_OPTION
    assert err.hint is not None
    assert "sigma" in err.hint and "planes" in err.hint


def test_option_names_are_case_sensitive(_registry: Registry) -> None:
    """ffmpeg AVOption names are case-sensitive, so the name is NOT folded the
    Postgres way: sigmaV is a real option, SIGMA is not."""
    g = _dyn("SELECT gblur(a.frame, sigmaV => 3) FROM input('x.mp4') a", _registry)
    assert g.nodes["n1"].args == {"sigmaV": 3}
    err = _reject_dyn("SELECT gblur(a.frame, SIGMA => 5) FROM input('x.mp4') a", _registry)
    assert err.code is ErrorCode.UNKNOWN_FILTER_OPTION


def test_numeric_option_rejects_a_string(_registry: Registry) -> None:
    err = _reject_dyn("SELECT gblur(a.frame, sigma => '5') FROM input('x.mp4') a", _registry)
    assert err.code is ErrorCode.FILTER_OPTION_TYPE
    assert "expects a number" in err.message


def test_numeric_option_enforces_the_introspected_range(_registry: Registry) -> None:
    err = _reject_dyn("SELECT gblur(a.frame, sigma => 5000) FROM input('x.mp4') a", _registry)
    assert err.code is ErrorCode.FILTER_OPTION_TYPE
    assert "from 0 to 1024" in err.message
    assert "got 5000" in err.message


def test_numeric_option_range_check_is_two_sided(_registry: Registry) -> None:
    assert (
        _reject_dyn("SELECT gblur(a.frame, steps => 0) FROM input('x.mp4') a", _registry).code
        is ErrorCode.FILTER_OPTION_TYPE
    )
    assert _dyn(
        "SELECT gblur(a.frame, steps => 6) FROM input('x.mp4') a", _registry
    ).nodes["n1"].args == {"steps": 6}


def test_an_unbounded_numeric_option_takes_any_number(_registry: Registry) -> None:
    """deband's `range` is `(from INT_MIN to INT_MAX)`, which does not parse as
    a float -- the registry records no bounds and no range is enforced."""
    g = _dyn("SELECT deband(a.frame, range => -4000) FROM input('x.mp4') a", _registry)
    assert g.nodes["n1"].args == {"range": -4000}


def test_boolean_option_takes_bare_true_and_false(_registry: Registry) -> None:
    g = _dyn("SELECT deband(a.frame, blur => false) FROM input('x.mp4') a", _registry)
    assert g.nodes["n1"].args == {"blur": False}
    # emit renders an ffmpeg boolean as 1/0
    assert "deband=blur=0" in emit(insert_splits(g)).filter_complex


def test_boolean_option_rejects_a_number(_registry: Registry) -> None:
    err = _reject_dyn("SELECT deband(a.frame, blur => 1) FROM input('x.mp4') a", _registry)
    assert err.code is ErrorCode.FILTER_OPTION_TYPE
    assert "expects true or false" in err.message


def test_enum_option_accepts_one_of_its_constants(_registry: Registry) -> None:
    g = _dyn(
        "SELECT xfade(a.frame, b.frame, transition => 'circlecrop') "
        "FROM input('x.mp4') a, input('y.mp4') b",
        _registry,
    )
    assert g.nodes["n1"].args == {"transition": "circlecrop"}


def test_enum_option_rejects_anything_else(_registry: Registry) -> None:
    err = _reject_dyn(
        "SELECT xfade(a.frame, b.frame, transition => 'nope') "
        "FROM input('x.mp4') a, input('y.mp4') b",
        _registry,
    )
    assert err.code is ErrorCode.FILTER_OPTION_TYPE
    assert "named constants" in err.message
    assert "wipeleft" in err.message


def test_enum_option_message_stops_counting_at_a_dozen(_registry: Registry) -> None:
    """xfade's transition has 15 constants in the fixture (59 in a real
    ffmpeg): the message lists the first dozen and counts the rest."""
    err = _reject_dyn(
        "SELECT xfade(a.frame, b.frame, transition => 'nope') "
        "FROM input('x.mp4') a, input('y.mp4') b",
        _registry,
    )
    assert "(3 more)" in err.message


def test_enum_option_suggests_a_near_miss_constant(_registry: Registry) -> None:
    err = _reject_dyn(
        "SELECT xfade(a.frame, b.frame, transition => 'wipelft') "
        "FROM input('x.mp4') a, input('y.mp4') b",
        _registry,
    )
    assert err.hint is not None and "wipeleft" in err.hint


def test_enum_option_rejects_the_constants_number(_registry: Registry) -> None:
    """The registry records constant NAMES, not their values, so a bare number
    is not something sqlmpeg can check -- it is rejected, with the names."""
    err = _reject_dyn(
        "SELECT xfade(a.frame, b.frame, transition => 1) "
        "FROM input('x.mp4') a, input('y.mp4') b",
        _registry,
    )
    assert err.code is ErrorCode.FILTER_OPTION_TYPE
    assert err.hint is not None and "constant name" in err.hint


def test_a_string_option_also_takes_a_bare_number(_registry: Registry) -> None:
    """xfade's duration/offset are ffmpeg `<duration>` options, i.e. strings --
    but an option value is text on the command line either way, and writing
    `duration => '1'` for a number would be a papercut."""
    g = _dyn(
        "SELECT xfade(a.frame, b.frame, duration => 1, offset => 2.5) "
        "FROM input('x.mp4') a, input('y.mp4') b",
        _registry,
    )
    assert g.nodes["n1"].args == {"duration": 1, "offset": 2.5}


def test_a_string_option_rejects_a_boolean(_registry: Registry) -> None:
    err = _reject_dyn(
        "SELECT xfade(a.frame, b.frame, expr => true) "
        "FROM input('x.mp4') a, input('y.mp4') b",
        _registry,
    )
    assert err.code is ErrorCode.FILTER_OPTION_TYPE
    assert "expects a string" in err.message


def test_option_rejection_anchors_on_the_value(_registry: Registry) -> None:
    """A Kwarg's Var name carries no token position, so the anchor is the
    value literal -- here on line 2, where the option was written."""
    err = _reject_dyn(
        "SELECT gblur(a.frame,\n       sigma => 5000)\nFROM input('x.mp4') a", _registry
    )
    assert err.line == 2


# -- tier-1 named extras ----------------------------------------------------


def test_stdlib_named_extra_reaches_the_underlying_filter(_registry: Registry) -> None:
    """blur's named_target is gblur, so `planes` is validated against gblur's
    options and merged AFTER the positionally-mapped sigma."""
    g = _dyn("SELECT blur(a.frame, 5, planes => 1) FROM input('x.mp4') a", _registry)
    assert g.nodes["n1"].filter == "gblur"
    assert list(g.nodes["n1"].args.items()) == [("sigma", 5), ("planes", 1)]


def test_stdlib_named_extras_keep_their_written_order(_registry: Registry) -> None:
    g = _dyn(
        "SELECT scale(a.frame, 1280, 720, interl => true, flags => 'lanczos') "
        "FROM input('x.mp4') a",
        _registry,
    )
    assert list(g.nodes["n1"].args.items()) == [
        ("w", 1280),
        ("h", 720),
        ("interl", True),
        ("flags", "lanczos"),
    ]


def test_stdlib_named_extra_is_validated_like_a_dynamic_one(_registry: Registry) -> None:
    err = _reject_dyn("SELECT blur(a.frame, 5, planes => 99) FROM input('x.mp4') a", _registry)
    assert err.code is ErrorCode.FILTER_OPTION_TYPE
    assert "from 0 to 15" in err.message

    err = _reject_dyn("SELECT blur(a.frame, 5, planez => 1) FROM input('x.mp4') a", _registry)
    assert err.code is ErrorCode.UNKNOWN_FILTER_OPTION
    assert "filter 'gblur'" in err.message


def test_stdlib_named_extra_cannot_override_the_positional_signature(
    _registry: Registry,
) -> None:
    """`w` is what crop's positional signature maps its width onto (ffmpeg's own
    long name for it is out_w), so this is a conflict, never a silent override."""
    err = _reject_dyn(
        "SELECT crop(a.frame, 0, 0, 10, 10, w => 5) FROM input('x.mp4') a", _registry
    )
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "already sets 'w'" in err.message
    assert err.hint is not None and "positionally" in err.hint


def test_stdlib_named_transition_merges_into_the_four_arg_crossfade(
    _registry: Registry,
) -> None:
    """The 4-argument crossfade leaves `transition` unset (ffmpeg defaults to
    fade), so both the named form and the 5-argument positional overload can
    supply it -- the RFC amendment's own example."""
    both = "FROM input('x.mp4') a, input('y.mp4') b"
    g = _dyn(
        f"SELECT crossfade(a.frame, b.frame, 1, 8, transition => 'wipeleft') {both}",
        _registry,
    )
    assert g.nodes["n1"].args == {"duration": 1, "offset": 8, "transition": "wipeleft"}
    g = _dyn(f"SELECT crossfade(a.frame, b.frame, 1, 8, 'wipeleft') {both}", _registry)
    assert g.nodes["n1"].args["transition"] == "wipeleft"


def test_stdlib_named_extra_conflicts_with_the_five_arg_crossfade(
    _registry: Registry,
) -> None:
    """When the 5-arg overload sets the transition positionally, a named one
    on top of it is a genuine conflict."""
    err = _reject_dyn(
        "SELECT crossfade(a.frame, b.frame, 1, 8, 'fade', transition => 'wipeleft') "
        "FROM input('x.mp4') a, input('y.mp4') b",
        _registry,
    )
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "already sets 'transition'" in err.message


def test_stdlib_named_extra_that_the_signature_leaves_free(_registry: Registry) -> None:
    """The 4-argument crossfade sets no `expr`, so a named one merges in."""
    g = _dyn(
        "SELECT crossfade(a.frame, b.frame, 1, 8, expr => 'A') "
        "FROM input('x.mp4') a, input('y.mp4') b",
        _registry,
    )
    assert g.nodes["n1"].args["expr"] == "A"


def test_a_macro_rejects_named_extras(_registry: Registry) -> None:
    """blur_regions expands to crop+gblur+overlay, so there is no single filter
    to set an option on -- rejected whether or not a registry is available."""
    for registry in (_registry, None):
        err = _reject_dyn(
            "SELECT blur_regions(a.frame, 1, 2, 3, 4, 5, planes => 1) FROM input('x.mp4') a",
            registry,
        )
        assert err.code is ErrorCode.UDF_ARG_TYPE
        assert "more than one ffmpeg filter" in err.message


def test_named_extras_on_a_filter_this_ffmpeg_lacks(_registry: Registry) -> None:
    """subtitles is a stdlib function whose named_target is not in this
    (fixture) ffmpeg's filter set: typed, not a crash and not a guess."""
    err = _reject_dyn(
        "SELECT subtitles(a.frame, 'subs.srt', force_style => 'x') FROM input('x.mp4') a",
        _registry,
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "does not provide" in err.message


# -- broadcasting is tier-agnostic -----------------------------------------


def test_a_dynamic_call_broadcasts_over_an_array(_registry: Registry) -> None:
    g = _dyn(
        "SELECT aecho(a.audio, in_gain => 0.5) FROM input('x.mp4') a",
        _registry,
        {"a": _probe_result(per_audio_tags=[{"language": "eng"}, {"language": "fra"}])},
    )
    assert _filters(g) == ["aecho", "aecho"]
    assert g.nodes["n1"].inputs == ["src:a:a:0"]
    assert g.nodes["n2"].inputs == ["src:a:a:1"]
    assert g.nodes["n1"].args == g.nodes["n2"].args == {"in_gain": 0.5}
    # provenance threads through a single-stream-input dynamic call, exactly
    # as it does through a stdlib one
    assert [o.metadata for o in g.outputs] == [{"language": "eng"}, {"language": "fra"}]


def test_a_two_pad_dynamic_call_zips_two_arrays(_registry: Registry) -> None:
    probes = {
        "a": _probe_result(videos=2, audios=0, video_tags={"language": "eng"}),
        "b": _probe_result(videos=2, audios=0, video_tags={"language": "eng"}),
    }
    g = _dyn(
        "SELECT xfade(a.video, b.video, duration => 1) "
        "FROM input('x.mp4') a, input('y.mp4') b",
        _registry,
        probes,
    )
    assert _filters(g) == ["xfade", "xfade"]
    assert g.nodes["n1"].inputs == ["src:a:v:0", "src:b:v:0"]
    assert g.nodes["n2"].inputs == ["src:a:v:1", "src:b:v:1"]
    # both inputs of each pad agree on the language, so the join keeps it
    assert [o.metadata for o in g.outputs] == [{"language": "eng"}] * 2


def test_a_dynamic_call_reports_a_zip_mismatch(_registry: Registry) -> None:
    err = _reject_dyn(
        "SELECT xfade(a.video, b.video) FROM input('x.mp4') a, input('y.mp4') b",
        _registry,
        {
            "a": _probe_result(videos=2, audios=0),
            "b": _probe_result(videos=1, audios=0),
        },
    )
    assert err.code is ErrorCode.BROADCAST_MISMATCH


def test_a_dynamic_join_drops_a_disagreeing_tag(_registry: Registry) -> None:
    g = _dyn(
        "SELECT xfade(a.frame, b.frame) FROM input('x.mp4') a, input('y.mp4') b",
        _registry,
        {
            "a": _probe_result(video_tags={"language": "eng"}),
            "b": _probe_result(video_tags={"language": "fra"}),
        },
    )
    assert g.outputs[0].metadata == {}


def test_a_dynamic_node_is_split_like_any_other(_registry: Registry) -> None:
    g = insert_splits(
        _dyn(
            "WITH c AS (SELECT gblur(a.frame, sigma => 2) AS f FROM input('x.mp4') a) "
            "SELECT hstack(c.f, c.f) FROM c",
            _registry,
        )
    )
    assert [node.filter for node in g.nodes.values()] == ["gblur", "split", "hstack"]


# -- no registry at all: no ffmpeg, or --portable ---------------------------


def test_without_a_registry_a_filter_name_is_an_unknown_function() -> None:
    """`deband` is close to no stdlib name, so the hint has room to say WHY the
    filter set is missing rather than a did-you-mean."""
    err = _reject_dyn("SELECT deband(a.frame, range => 8) FROM input('x.mp4') a", None)
    assert err.code is ErrorCode.UNKNOWN_FUNCTION
    assert err.hint is not None and "ffmpeg on PATH" in err.hint


def test_a_close_stdlib_name_still_wins_the_hint() -> None:
    """gblur is what blur() expands to, so the stdlib suggestion is the useful
    one even though the reason it did not resolve is the missing filter set."""
    err = _reject_dyn("SELECT gblur(a.frame, sigma => 5) FROM input('x.mp4') a", None)
    assert err.hint is not None and "blur()" in err.hint


def test_portable_says_so_instead() -> None:
    err = _reject_dyn(
        "SELECT deband(a.frame, range => 8) FROM input('x.mp4') a", None, portable=True
    )
    assert err.code is ErrorCode.UNKNOWN_FUNCTION
    assert err.hint is not None and "--portable" in err.hint


def test_named_extras_need_an_ffmpeg() -> None:
    err = _reject_dyn("SELECT blur(a.frame, 5, planes => 1) FROM input('x.mp4') a", None)
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "ffmpeg was not found" in err.message
    assert err.hint is not None and "install ffmpeg" in err.hint


def test_portable_rejects_named_extras_the_same_way() -> None:
    """One rule: named arguments ARE your installed ffmpeg. --portable rejects
    exactly what a machine without ffmpeg rejects, so a portable query compiles
    everywhere."""
    err = _reject_dyn(
        "SELECT blur(a.frame, 5, planes => 1) FROM input('x.mp4') a", None, portable=True
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "--portable" in err.message


def test_portable_leaves_the_stdlib_alone() -> None:
    g = _dyn("SELECT blur(a.frame, 5) FROM input('x.mp4') a", None, portable=True)
    assert g.nodes["n1"].args == {"sigma": 5}


def test_did_you_mean_spans_both_tiers(_registry: Registry) -> None:
    err = _reject_dyn("SELECT gblu(a.frame) FROM input('x.mp4') a", _registry)
    assert err.code is ErrorCode.UNKNOWN_FUNCTION
    assert err.hint is not None and "gblur()" in err.hint


def test_did_you_mean_still_prefers_the_stdlib_list(_registry: Registry) -> None:
    err = _reject_dyn("SELECT zzzz(a.frame) FROM input('x.mp4') a", _registry)
    assert err.hint is not None and "blur_regions" in err.hint


def test_compile_sql_portable_skips_the_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """--portable (plan 032's CLI flag) must not even build a registry: the
    point is a compile that behaves like a machine with no ffmpeg."""
    def boom() -> Registry:
        raise AssertionError("registry.load() must not be called with portable=True")

    monkeypatch.setattr(compiler.registry_module, "load", boom)
    g = compile_sql("SELECT blur(a.frame, 5) FROM input('x.mp4') a", portable=True)
    assert g.nodes["n1"].filter == "gblur"


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


# ---------------------------------------------------------------------------
# plan 029: crossfade of two WHERE-trimmed segments (compile-only)
# ---------------------------------------------------------------------------


@pytest.mark.exec
def test_crossfade_of_two_trimmed_segments_compiles(
    _av2_fixture: str, _av3_fixture: str
) -> None:
    """Each side is trimmed to a 2s window via WHERE, then crossfaded over 1s
    starting 1s into the first segment -- exercises xfade as a real multi-input
    tier-1 call against two independently seeked inputs. Since plan 035 the
    windows are input options (one per -i), so xfade consumes the raw refs and
    the graph is the single node."""
    g = compile_sql(
        f"SELECT crossfade(a.frame, b.frame, 1, 1) "
        f"FROM input('{_av2_fixture}') a, input('{_av3_fixture}') b "
        f"WHERE a.t BETWEEN 0 AND 2 AND b.t BETWEEN 0 AND 2"
    )
    assert _filters(g) == ["xfade"]
    assert g.input_trims == {"a": (0, 2), "b": (0, 2)}
    xfade = g.nodes["n1"]
    assert xfade.filter == "xfade"
    assert xfade.args == {"duration": 1, "offset": 1}
    assert xfade.inputs == ["src:a:v:0", "src:b:v:0"]
    assert xfade.outputs == ["video"]
    assert _outputs(g) == [("n1", "video", None)]


# ---------------------------------------------------------------------------
# plan 038: delay's VIDEO overload (per-variant expansion)
# ---------------------------------------------------------------------------
#
# `delay` is the only spec whose overloads differ in more than arity: both are
# (stream, seconds), so nothing but the matched variant INDEX can tell an
# adelay node from the format+tpad macro. These pin that the index really is
# what selects the expansion, the output type and the named-argument target.

_AD_INSERT = (
    "SELECT overlay(f.video[1], delay(scale(a.video[1], 0.33), 1), 20, 20), "
    "       amix(f.audio[1], volume(delay(a.audio[1], 1), 0.5)) "
    "FROM input('film.mp4') f, input('ad.mp4') a"
)


def test_video_delay_expands_to_format_plus_tpad() -> None:
    g = _lower("SELECT delay(a.frame, 1) FROM input('x.mp4') a")
    assert _filters(g) == ["format", "tpad"]
    assert g.nodes["n1"].args == {"pix_fmts": "yuva420p"}
    assert g.nodes["n2"].args == {"start_duration": 1, "stop": 1, "color": "black@0"}
    assert g.nodes["n2"].inputs == ["n1"]
    assert _outputs(g) == [("n2", "video", None)]


def test_audio_delay_still_expands_to_one_adelay() -> None:
    g = _lower("SELECT delay(a.audio[1], 1) FROM input('x.mp4') a")
    assert _filters(g) == ["adelay"]
    assert g.nodes["n1"].args == {"delays": 1000, "all": 1}
    assert _outputs(g) == [("n1", "audio", None)]


def test_the_stream_kind_alone_picks_the_delay_overload() -> None:
    """Same name, same arity, same literal -- two different expansions."""
    g = _lower(
        "SELECT delay(a.frame, 1), delay(a.audio[1], 1) FROM input('x.mp4') a"
    )
    assert _filters(g) == ["format", "tpad", "adelay"]
    assert [o.type for o in g.outputs] == ["video", "audio"]


def test_a_delayed_video_is_classified_as_video_by_its_caller() -> None:
    """The nested-call type check reads the matched OVERLOAD's return type, so
    a video delay satisfies overlay's second video parameter."""
    g = _lower(
        "SELECT overlay(a.frame, delay(b.frame, 1), 20, 20) "
        "FROM input('x.mp4') a, input('y.mp4') b"
    )
    assert _filters(g) == ["format", "tpad", "overlay"]
    assert g.nodes["n3"].inputs == ["src:a:v:0", "n2"]


def test_a_delayed_audio_is_still_classified_as_audio() -> None:
    g = _lower("SELECT volume(delay(a.audio[1], 1), 0.5) FROM input('x.mp4') a")
    assert _filters(g) == ["adelay", "volume"]
    assert g.nodes["n2"].inputs == ["n1"]


def test_the_ad_insert_composition_lowers_end_to_end() -> None:
    """The plan 038 driving case: a clip delayed onto a film, video and audio
    (golden 096-ad-insert pins the whole IR; this pins the shape)."""
    g = _lower(_AD_INSERT)
    assert _filters(g) == [
        "scale",
        "format",
        "tpad",
        "overlay",
        "adelay",
        "volume",
        "amix",
    ]
    assert g.nodes["n4"].inputs == ["src:f:v:0", "n3"]
    assert g.nodes["n7"].inputs == ["src:f:a:0", "n6"]
    assert [o.type for o in g.outputs] == ["video", "audio"]


def test_delay_arity_error_lists_both_overloads() -> None:
    err = _reject_lower("SELECT delay(a.frame, a.frame) FROM input('x.mp4') a", {})
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "delay(audio, num) | delay(video, num)" in err.message
    assert "got delay(video, video)" in err.message


def test_video_delay_broadcasts_over_a_video_array() -> None:
    g = _lower(
        "SELECT delay(a.video, 1) FROM input('x.mp4') a",
        {"a": _probe_result(videos=2, audios=0)},
    )
    assert _filters(g) == ["format", "tpad", "format", "tpad"]
    assert g.nodes["n1"].inputs == ["src:a:v:0"]
    assert g.nodes["n3"].inputs == ["src:a:v:1"]


def test_video_delay_threads_provenance_like_any_1_to_1_chain() -> None:
    g = _lower(
        "SELECT delay(a.frame, 1) FROM input('x.mp4') a",
        {"a": _probe_result(video_tags={"language": "eng"})},
    )
    assert [o.metadata for o in g.outputs] == [{"language": "eng"}]


def test_video_delay_rejects_named_extras_as_a_macro(_registry: Registry) -> None:
    """The video overload is two filters, so there is no single option set to
    reach through to -- the same rule blur_regions follows."""
    err = _reject_dyn(
        "SELECT delay(a.frame, 1, color => 'red') FROM input('x.mp4') a", _registry
    )
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "expands to more than one ffmpeg filter" in err.message


def test_audio_delay_still_targets_adelay_for_named_extras(_registry: Registry) -> None:
    """named_target is per OVERLOAD: the audio one still reaches adelay, which
    the fixture ffmpeg does not have -- so the rejection NAMES adelay rather
    than calling the call a macro."""
    err = _reject_dyn(
        "SELECT delay(a.audio[1], 1, all => false) FROM input('x.mp4') a", _registry
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "'adelay'" in err.message


def test_delay_over_a_passthrough_stream_is_still_udf_arg_type() -> None:
    err = _reject_lower("SELECT delay(a.subtitle[1], 1) FROM input('x.mp4') a", {})
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "cannot take a subtitle stream" in err.message


# ---------------------------------------------------------------------------
# plan 038: the `ffmpeg.<filter>(...)` namespace
# ---------------------------------------------------------------------------
#
# One spelling of a filter name that no SQL grammar has an opinion about, and
# that resolves in the registry ALONE. The offline fixture registry above has
# `trim` in it, which is exactly the interesting case: bare `trim(a.frame)` is
# Postgres's string TRIM and loses the argument.


def test_a_namespaced_call_lowers_to_an_ordinary_node(_registry: Registry) -> None:
    g = _dyn("SELECT ffmpeg.gblur(a.frame, sigma => 5) FROM input('x.mp4') a", _registry)
    assert g.nodes["n1"].filter == "gblur"  # the NODE knows nothing of the namespace
    assert g.nodes["n1"].args == {"sigma": 5}
    assert g.nodes["n1"].inputs == ["src:a:v:0"]
    assert _outputs(g) == [("n1", "video", None)]


def test_the_namespace_resolves_past_the_stdlib(_registry: Registry) -> None:
    """`scale` is both a stdlib function and a real filter. The bare name is
    the stdlib's, forever; the namespaced one is the filter's."""
    raw = _dyn(
        "SELECT ffmpeg.scale(a.frame, width => 640) FROM input('x.mp4') a", _registry
    )
    assert raw.nodes["n1"].filter == "scale"
    # ffmpeg's own option names, not the stdlib's w=iw*<factor>, h=-2 mapping.
    assert raw.nodes["n1"].args == {"width": 640}

    stdlib = _dyn("SELECT scale(a.frame, 0.5) FROM input('x.mp4') a", _registry)
    assert stdlib.nodes["n1"].args == {"w": "iw*0.5", "h": "-2"}


def test_the_namespace_reaches_a_name_postgres_claimed(_registry: Registry) -> None:
    """Bare `trim(a.frame)` parses as Postgres's TRIM and arrives with NO
    positional arguments; the namespaced spelling keeps them."""
    err = _reject_dyn("SELECT trim(a.frame) FROM input('x.mp4') a", _registry)
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "got trim()" in err.message

    g = _dyn("SELECT ffmpeg.trim(a.frame) FROM input('x.mp4') a", _registry)
    assert g.nodes["n1"].filter == "trim"
    assert g.nodes["n1"].inputs == ["src:a:v:0"]


def test_the_namespace_qualifier_folds_like_any_identifier(_registry: Registry) -> None:
    g = _dyn("SELECT FFMPEG.GBlur(a.frame, sigma => 1) FROM input('x.mp4') a", _registry)
    assert g.nodes["n1"].filter == "gblur"


def test_a_namespaced_call_checks_its_pad_signature(_registry: Registry) -> None:
    err = _reject_dyn("SELECT ffmpeg.gblur(a.audio[1]) FROM input('x.mp4') a", _registry)
    assert err.code is ErrorCode.UDF_ARG_TYPE
    assert "ffmpeg.gblur(video), got ffmpeg.gblur(audio)" in err.message


def test_a_namespaced_option_is_validated_the_ordinary_way(_registry: Registry) -> None:
    err = _reject_dyn(
        "SELECT ffmpeg.gblur(a.frame, sigmma => 5) FROM input('x.mp4') a", _registry
    )
    assert err.code is ErrorCode.UNKNOWN_FILTER_OPTION
    # The message names the FILTER, which is what has the options.
    assert "filter 'gblur' has no option 'sigmma'" in err.message
    assert err.hint is not None and "sigma" in err.hint


def test_a_namespaced_unknown_name_suggests_the_namespaced_spelling(
    _registry: Registry,
) -> None:
    err = _reject_dyn("SELECT ffmpeg.gblurr(a.frame) FROM input('x.mp4') a", _registry)
    assert err.code is ErrorCode.UNKNOWN_FUNCTION
    assert "unknown function ffmpeg.gblurr()" in err.message
    assert err.hint == "did you mean ffmpeg.gblur()?"


def test_a_namespaced_did_you_mean_never_reaches_into_the_stdlib(
    _registry: Registry,
) -> None:
    """`reverb` is a stdlib name and no filter of this ffmpeg. The bare
    spelling's did-you-mean spans both tiers and finds it; the namespace's
    spans the registry alone, so it must not offer a tier-1 name."""
    assert _reject_dyn(
        "SELECT reverbb(a.audio[1]) FROM input('x.mp4') a", _registry
    ).hint == "did you mean reverb()?"

    err = _reject_dyn("SELECT ffmpeg.reverb(a.audio[1]) FROM input('x.mp4') a", _registry)
    assert err.code is ErrorCode.UNKNOWN_FUNCTION
    assert err.hint is not None
    assert "reverb" not in err.hint.replace("ffmpeg.<filter>", "")
    assert "not one of them" in err.hint


def test_the_scope_fence_applies_to_the_namespace_too(_registry: Registry) -> None:
    for sql in (
        "SELECT ffmpeg.acrossover(a.audio[1]) FROM input('x.mp4') a",
        "SELECT ffmpeg.feedback(a.frame, a.frame) FROM input('x.mp4') a",
        "SELECT ffmpeg.testsrc(a.frame) FROM input('x.mp4') a",
        "SELECT ffmpeg.split(a.frame) FROM input('x.mp4') a",
    ):
        assert _reject_dyn(sql, _registry).code is ErrorCode.UNKNOWN_FUNCTION


def test_namespaced_calls_nest_in_both_directions(_registry: Registry) -> None:
    g = _dyn(
        "SELECT scale(ffmpeg.gblur(a.frame, sigma => 2), 0.5) FROM input('x.mp4') a",
        _registry,
    )
    assert _filters(g) == ["gblur", "scale"]
    g = _dyn(
        "SELECT ffmpeg.gblur(scale(a.frame, 0.5), sigma => 2) FROM input('x.mp4') a",
        _registry,
    )
    assert _filters(g) == ["scale", "gblur"]


def test_a_namespaced_call_broadcasts_like_any_other(_registry: Registry) -> None:
    g = _dyn(
        "SELECT ffmpeg.aecho(a.audio, in_gain => 0.5) FROM input('x.mp4') a",
        _registry,
        {"a": _probe_result(per_audio_tags=[{"language": "eng"}, {"language": "fra"}])},
    )
    assert _filters(g) == ["aecho", "aecho"]
    assert [o.metadata for o in g.outputs] == [{"language": "eng"}, {"language": "fra"}]


def test_without_a_registry_the_namespace_is_an_unknown_function() -> None:
    err = _reject_dyn("SELECT ffmpeg.gblur(a.frame) FROM input('x.mp4') a", None)
    assert err.code is ErrorCode.UNKNOWN_FUNCTION
    assert err.hint is not None and "ffmpeg was not found on PATH" in err.hint


def test_portable_says_so_for_the_namespace_too() -> None:
    err = _reject_dyn(
        "SELECT ffmpeg.gblur(a.frame) FROM input('x.mp4') a", None, portable=True
    )
    assert err.code is ErrorCode.UNKNOWN_FUNCTION
    assert err.hint is not None and "--portable" in err.hint


def test_ffmpeg_is_reserved_as_an_input_alias() -> None:
    err = _reject_lower("SELECT ffmpeg.frame FROM input('x.mp4') ffmpeg", {})
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "reserved for the filter namespace" in err.message


def test_ffmpeg_is_reserved_as_a_cte_name() -> None:
    err = _reject_lower(
        "WITH ffmpeg AS (SELECT a.frame FROM input('x.mp4') a) "
        "SELECT ffmpeg.frame FROM ffmpeg",
        {},
    )
    assert err.code is ErrorCode.UNSUPPORTED_SQL
    assert "reserved for the filter namespace" in err.message


def test_a_bare_ffmpeg_column_points_at_the_call_form() -> None:
    """`ffmpeg.gblur` with no parentheses is a COLUMN, and the namespace only
    exists in call position -- so the hint names the call form."""
    err = _reject_lower("SELECT ffmpeg.gblur FROM input('x.mp4') a", {})
    assert err.code is ErrorCode.UNKNOWN_ALIAS
    assert err.hint is not None and "ffmpeg.gblur(<stream>" in err.hint


def test_an_unrelated_qualified_call_is_still_rejected() -> None:
    """The namespace is exactly one name; `foo.gblur(...)` is not a call
    sqlmpeg knows how to read."""
    err = _reject_lower("SELECT foo.gblur(a.frame) FROM input('x.mp4') a", {})
    assert err.code is ErrorCode.UNSUPPORTED_SQL


# ---------------------------------------------------------------------------
# RFC-003: the same shapes against the REAL installed ffmpeg (plan 031)
# ---------------------------------------------------------------------------
#
# The offline tests above pin the semantics against captured fixtures; these
# pin the CONTRACT with the actual binary -- that the option tables sqlmpeg
# introspects really do describe filters ffmpeg then accepts, and that a
# compiled tier-2 command runs. Expectations here are therefore about what
# ffmpeg does, never about a specific option list.


def _run_compiled(query: str, out_path: Path) -> None:
    """Compile, emit and RUN a query; the exit code is the assertion."""
    args = build_ffmpeg_args(emit(compile_sql(query)), str(out_path))
    args.insert(1, "-y")
    result = subprocess.run(args, capture_output=True, text=True, timeout=60.0)
    assert result.returncode == 0, result.stderr
    assert out_path.exists()


@pytest.mark.exec
def test_a_pure_tier_two_filter_compiles_and_runs(
    _av_fixture: str, tmp_path: Path
) -> None:
    """curves is in no stdlib table: name, pad signature, `preset` and its
    constants all come from the installed ffmpeg -- and the command runs."""
    query = (
        f"SELECT curves(a.frame, preset => 'lighter'), a.audio[1] "
        f"FROM input('{_av_fixture}') a"
    )
    g = compile_sql(query)
    assert g.nodes["n1"].filter == "curves"
    assert g.nodes["n1"].args == {"preset": "lighter"}
    assert "curves=preset=lighter" in emit(g).filter_complex
    _run_compiled(query, tmp_path / "curves.mp4")


@pytest.mark.exec
def test_two_named_options_on_a_real_filter_run(_av_fixture: str, tmp_path: Path) -> None:
    query = (
        f"SELECT unsharp(a.frame, luma_msize_x => 7, luma_amount => 1.5) "
        f"FROM input('{_av_fixture}') a"
    )
    assert compile_sql(query).nodes["n1"].args == {"luma_msize_x": 7, "luma_amount": 1.5}
    _run_compiled(query, tmp_path / "unsharp.mp4")


@pytest.mark.exec
def test_a_real_boolean_option_renders_as_ffmpeg_wants_it(
    _av_fixture: str, tmp_path: Path
) -> None:
    """deband's `blur` is a real <boolean> AVOption; SQL writes true/false and
    emit renders 1/0, which is what ffmpeg parses."""
    query = f"SELECT deband(a.frame, blur => false) FROM input('{_av_fixture}') a"
    assert "deband=blur=0" in emit(compile_sql(query)).filter_complex
    _run_compiled(query, tmp_path / "deband.mp4")


@pytest.mark.exec
def test_the_real_gblur_range_comes_from_ffmpeg(_av_fixture: str) -> None:
    err = _reject(f"SELECT gblur(a.frame, sigma => 5000) FROM input('{_av_fixture}') a")
    assert err.code is ErrorCode.FILTER_OPTION_TYPE
    assert "0 to 1024" in err.message


@pytest.mark.exec
def test_the_real_xfade_transition_constants_are_enforced(
    _av2_fixture: str, _av3_fixture: str, tmp_path: Path
) -> None:
    """xfade as a DYNAMIC call (the stdlib name is crossfade): its transition
    is an ffmpeg enum, so a constant name is checked against the real list."""
    both = f"FROM input('{_av2_fixture}') a, input('{_av3_fixture}') b"
    err = _reject(f"SELECT xfade(a.frame, b.frame, transition => 'sideways') {both}")
    assert err.code is ErrorCode.FILTER_OPTION_TYPE
    assert "wipeleft" in err.message

    query = (
        f"SELECT xfade(a.frame, b.frame, transition => 'wipeleft', "
        f"duration => 1, offset => 1) {both}"
    )
    assert compile_sql(query).nodes["n1"].args == {
        "transition": "wipeleft",
        "duration": 1,
        "offset": 1,
    }
    _run_compiled(query, tmp_path / "xfade.mp4")


@pytest.mark.exec
def test_a_real_unknown_option_lists_the_real_ones(_av_fixture: str) -> None:
    err = _reject(f"SELECT gblur(a.frame, sigmma => 5) FROM input('{_av_fixture}') a")
    assert err.code is ErrorCode.UNKNOWN_FILTER_OPTION
    assert err.hint is not None and "sigma" in err.hint


@pytest.mark.exec
def test_a_tier_one_named_extra_runs(_av_fixture: str, tmp_path: Path) -> None:
    """blur() reaches through to gblur's full option set: `planes` is not in any
    sqlmpeg table, it was read out of this ffmpeg."""
    query = f"SELECT blur(a.frame, 5, planes => 1) FROM input('{_av_fixture}') a"
    assert compile_sql(query).nodes["n1"].args == {"sigma": 5, "planes": 1}
    _run_compiled(query, tmp_path / "blur-planes.mp4")


@pytest.mark.exec
def test_a_tier_two_audio_filter_broadcasts_over_real_tracks(
    _av2_fixture: str, tmp_path: Path
) -> None:
    """The broadcast machinery is type-driven, so a dynamic audio filter
    expands over every language track exactly as reverb() does -- tags and
    all -- and the result runs."""
    query = (
        f"SELECT a.video[1], aecho(a.audio, in_gain => 0.5) "
        f"FROM input('{_av2_fixture}') a"
    )
    g = compile_sql(query)
    assert _filters(g) == ["aecho", "aecho"]
    assert g.nodes["n1"].inputs == ["src:a:a:0"]
    assert g.nodes["n2"].inputs == ["src:a:a:1"]
    assert [o.metadata for o in g.outputs] == [
        {},
        {"language": "eng"},
        {"language": "fra"},
    ]
    _run_compiled(query, tmp_path / "aecho.mkv")


@pytest.mark.exec
def test_did_you_mean_reaches_into_the_real_filter_set(_av_fixture: str) -> None:
    err = _reject(f"SELECT gblu(a.frame) FROM input('{_av_fixture}') a")
    assert err.code is ErrorCode.UNKNOWN_FUNCTION
    assert err.hint is not None and "gblur()" in err.hint


# ---------------------------------------------------------------------------
# plan 038: the collision census, measured against the REAL filter set
# ---------------------------------------------------------------------------
#
# Which filter names Postgres parses specially is a property of sqlglot's
# grammar crossed with this ffmpeg's filter list, so it is MEASURED rather
# than reasoned about: parse `<name>(...)` for every in-fence filter, in
# several argument shapes (a collision can depend on the arity -- `overlay(a)`
# is a PARSE_ERROR while `overlay(a, b, 1, 2)` is the builtin), and collect
# every name that does not arrive as an ordinary anonymous call.
#
# The list this pins is the one docs/dynamic-filters.md publishes. It is
# allowed to grow with a new ffmpeg or a new sqlglot; what must never grow is
# the set of filters you cannot reach, which is why the second half of the
# census compiles every collided name through the namespace.

_CENSUS_ARG_FORMS = (
    "a.frame",
    "a.frame, b.frame",
    "a.frame, b.frame, 1, 2",
    "a.frame, x => 1",
)

# Measured against ffmpeg 7.1 (464 in-fence filters) and sqlglot 30.17.
_KNOWN_COLLISIONS = frozenset(
    {
        "copy",
        "corr",
        "format",
        "median",
        "normalize",
        "null",
        "overlay",
        "pad",
        "random",
        "reverse",
        "trim",
    }
)


def _parses_as_a_plain_call(sql: str, *, namespaced: bool) -> bool:
    """Did sqlglot read this as an ordinary function call and nothing else?

    A namespaced call's ordinary shape is one level deeper --
    ``Dot(Identifier(ffmpeg), Anonymous(...))`` -- which is the whole reason
    the namespace works: the qualifier is what the parser sees first, so no
    special form ever matches.
    """
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except Exception:
        return False
    node = tree.expressions[0]
    if not namespaced:
        return isinstance(node, sqlglot.exp.Anonymous)
    return isinstance(node, sqlglot.exp.Dot) and isinstance(
        node.args.get("expression"), sqlglot.exp.Anonymous
    )


def _census(names: list[str], *, namespaced: bool = False) -> set[str]:
    prefix = "ffmpeg." if namespaced else ""
    return {
        name
        for name in names
        for form in _CENSUS_ARG_FORMS
        if not _parses_as_a_plain_call(
            f"SELECT {prefix}{name}({form}) FROM t", namespaced=namespaced
        )
    }


@pytest.mark.exec
def test_the_collision_census_is_the_documented_set() -> None:
    """Every filter name Postgres parses specially, enumerated empirically."""
    names = registry_module.load().names()
    assert names, "no filters: this test needs a real ffmpeg"
    assert _census(names) == _KNOWN_COLLISIONS & set(names)


@pytest.mark.exec
def test_the_namespace_never_collides_with_the_grammar() -> None:
    """The other half of the census: prefixed with the namespace, EVERY filter
    name parses as an ordinary call in every argument shape."""
    names = registry_module.load().names()
    assert names, "no filters: this test needs a real ffmpeg"
    assert _census(names, namespaced=True) == set()


@pytest.mark.exec
def test_every_collided_filter_compiles_through_the_namespace(
    _av_fixture: str, _av2_fixture: str
) -> None:
    """The point of the whole feature: no in-fence filter is unreachable.

    Each collided name is called with exactly its own input pads (from the
    real pad signature), one distinct alias per pad so nothing needs a split,
    so the compile is the genuine one -- probe, registry lookup, pad type
    check and all.
    """
    registry = registry_module.load()
    collided = sorted(_KNOWN_COLLISIONS & set(registry.names()))
    assert collided, "no collisions on this ffmpeg: nothing to prove"
    files = (_av_fixture, _av2_fixture)

    for name in collided:
        dynamic = registry.get(name)
        assert dynamic is not None
        aliases = "abcd"[: len(dynamic.inputs)]
        assert len(aliases) == len(dynamic.inputs), name
        pads = ", ".join(
            f"{alias}.video[1]" if kind == "video" else f"{alias}.audio[1]"
            for alias, kind in zip(aliases, dynamic.inputs)
        )
        sources = ", ".join(
            f"input('{files[i % len(files)]}') {alias}"
            for i, alias in enumerate(aliases)
        )
        query = f"SELECT ffmpeg.{name}({pads}) FROM {sources}"
        graph = compile_sql(query)
        assert [node.filter for node in graph.nodes.values()] == [name], query


@pytest.mark.exec
def test_the_real_overlay_options_are_reachable_through_the_namespace(
    _av2_fixture: str, _av3_fixture: str, tmp_path: Path
) -> None:
    """`overlay`'s named extras are unreachable in its stdlib spelling (the
    OVERLAY..PLACING grammar makes `=>` a PARSE_ERROR); namespaced, the whole
    option set is there -- and the command runs."""
    query = (
        "SELECT ffmpeg.overlay(a.frame, b.frame, x => 20, y => 20, "
        "eof_action => 'pass') "
        f"FROM input('{_av2_fixture}') a, input('{_av3_fixture}') b"
    )
    g = compile_sql(query)
    assert g.nodes["n1"].filter == "overlay"
    assert g.nodes["n1"].args == {"x": 20, "y": 20, "eof_action": "pass"}
    _run_compiled(query, tmp_path / "ns-overlay.mp4")

    assert _reject(
        "SELECT overlay(a.frame, b.frame, x => 20, y => 20) "
        f"FROM input('{_av2_fixture}') a, input('{_av3_fixture}') b"
    ).code is ErrorCode.PARSE_ERROR


@pytest.mark.exec
def test_the_real_trim_filter_runs_through_the_namespace(
    _av_fixture: str, tmp_path: Path
) -> None:
    """ffmpeg's `trim` filter, the one Postgres's TRIM grammar hides.

    The option names are ffmpeg's own as the registry reports them, aliases
    deduped to the longer spelling (`starti`, not `start`) -- read out of the
    binary, not from any table here.
    """
    query = (
        f"SELECT ffmpeg.trim(a.frame, starti => 0.5, durationi => 1) "
        f"FROM input('{_av_fixture}') a"
    )
    assert compile_sql(query).nodes["n1"].args == {"starti": 0.5, "durationi": 1}
    _run_compiled(query, tmp_path / "ns-trim.mp4")
