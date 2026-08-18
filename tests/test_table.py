"""Tests for the table/CSV renderer (RFC-011, plan 067).

Format is PINNED byte-for-byte by cookbook recipes 30-32 (see
tests/test_examples.py, which runs those through the CLI); this file is the
renderer's own unit coverage -- widths, rstrip, empty cells, the "(1 row)"
singular, and the stream-placeholder forms -- built directly against
:class:`~sqlmpeg.table.TableResult` rather than a compiled query.
"""

from __future__ import annotations

from sqlmpeg.table import StreamCell, TableResult, render_csv, render_table


def test_widths_are_max_of_header_and_values() -> None:
    result = TableResult(columns=["a", "bb"], rows=[["x", "y"], ["long", "z"]])
    text = render_table(result)
    lines = text.split("\n")
    assert lines[0] == " a    | bb"
    assert lines[1] == "------+----"


def test_every_line_is_rstripped() -> None:
    result = TableResult(columns=["a", "b"], rows=[["x", ""]])
    text = render_table(result)
    for line in text.split("\n"):
        assert line == line.rstrip()
    # An empty last cell ends the row right at its "|".
    assert text.split("\n")[2] == " x |"


def test_null_cell_is_empty() -> None:
    result = TableResult(columns=["a"], rows=[[None]])
    text = render_table(result)
    # A single-column NULL row's only cell IS the last one, so the whole
    # line rstrips away to nothing.
    assert text.split("\n")[2] == ""


def test_footer_uses_singular_for_one_row() -> None:
    result = TableResult(columns=["a"], rows=[["x"]])
    assert render_table(result).endswith("(1 row)")


def test_footer_uses_plural_for_zero_and_many_rows() -> None:
    assert render_table(TableResult(columns=["a"], rows=[])).endswith("(0 rows)")
    assert render_table(
        TableResult(columns=["a"], rows=[["x"], ["y"], ["z"]])
    ).endswith("(3 rows)")


def test_stream_cell_source_passthrough_placeholder() -> None:
    result = TableResult(columns=["t"], rows=[[StreamCell(type="video", spec="0:v:0")]])
    text = render_table(result)
    assert "<video 0:v:0>" in text


def test_stream_cell_filtered_node_placeholder() -> None:
    result = TableResult(columns=["t"], rows=[[StreamCell(type="audio", spec="n2")]])
    text = render_table(result)
    assert "<audio n2>" in text


def test_int_and_float_cells_render_via_str() -> None:
    result = TableResult(columns=["n", "f"], rows=[[1, 2.5]])
    text = render_table(result)
    assert "1" in text.split("\n")[2]
    assert "2.5" in text.split("\n")[2]


def test_recipe_30_shape_byte_for_byte() -> None:
    """Cross-check against the pinned recipe 30 bytes without compiling."""
    result = TableResult(
        columns=["index", "language", "codec", "channel_layout"],
        rows=[[1, "eng", "aac", "mono"], [2, "fra", "aac", "mono"]],
    )
    assert render_table(result) == (
        " index | language | codec | channel_layout\n"
        "-------+----------+-------+----------------\n"
        " 1     | eng      | aac   | mono\n"
        " 2     | fra      | aac   | mono\n"
        "(2 rows)"
    )


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def test_csv_header_true_emits_column_row() -> None:
    result = TableResult(columns=["a", "b"], rows=[["x", "y"]])
    assert render_csv(result, header=True) == "a,b\nx,y\n"


def test_csv_header_false_omits_column_row() -> None:
    result = TableResult(columns=["a", "b"], rows=[["x", "y"]])
    assert render_csv(result, header=False) == "x,y\n"


def test_csv_null_cell_is_empty_field() -> None:
    result = TableResult(columns=["a", "b"], rows=[["x", None]])
    assert render_csv(result, header=False) == "x,\n"


def test_csv_quotes_only_when_needed() -> None:
    result = TableResult(columns=["a"], rows=[["plain"], ["has,comma"], ['has"quote']])
    text = render_csv(result, header=False)
    lines = text.splitlines()
    assert lines[0] == "plain"
    assert lines[1] == '"has,comma"'
    assert lines[2] == '"has""quote"'


def test_csv_uses_lf_not_crlf() -> None:
    result = TableResult(columns=["a"], rows=[["x"], ["y"]])
    text = render_csv(result, header=False)
    assert "\r" not in text


def test_csv_stream_cell_placeholder() -> None:
    result = TableResult(columns=["t"], rows=[[StreamCell(type="audio", spec="0:a:0")]])
    assert render_csv(result, header=False) == "<audio 0:a:0>\n"
