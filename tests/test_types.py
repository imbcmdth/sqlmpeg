"""Tests for the declared type system.

Two jobs. First, the registry is internally consistent: every field's type
reference resolves, every array column's elements are a declared record, and
every field carries a W/RO mark and a declared type reference — pinned here
per type, so an accidental edit to a declaration fails loudly.

Second, and the load-bearing half: every derived view still equals the exact
value it had when it was typed out by hand in parser.py. The expected
constants below are those literals, pasted. A change to a declaration that
silently moves a column set, a column ORDER, or a hint's wording has to break
one of these before it can reach a golden test.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from sqlmpeg import lower, parser, types
from sqlmpeg.types import COLUMN_TYPES, TYPES, Field, element_type, is_array, resolve

# Every declared field, per type: name -> (type reference, W or RO). The
# reference tables for stream records, non-stream records and the container,
# restated independently of the declarations they check.
EXPECTED_FIELDS: dict[str, dict[str, tuple[str, str]]] = {
    "text": {},
    "number": {},
    "boolean": {},
    "stream": {},
    "seek": {},
    "tag": {"key": ("text", "W"), "value": ("text", "W")},
    "flag": {"key": ("text", "W"), "set": ("boolean", "W")},
    "video_stream": {
        "index": ("number", "RO"),
        "language": ("text", "W"),
        "title": ("text", "W"),
        "codec": ("text", "RO"),
        "width": ("number", "RO"),
        "height": ("number", "RO"),
        "fps": ("text", "RO"),
        "bitrate": ("number", "RO"),
        "duration": ("number", "RO"),
        "color_transfer": ("text", "RO"),
        "tags": ("tag[]", "W"),
        "disposition": ("flag[]", "W"),
    },
    "audio_stream": {
        "index": ("number", "RO"),
        "language": ("text", "W"),
        "title": ("text", "W"),
        "codec": ("text", "RO"),
        "channels": ("number", "RO"),
        "channel_layout": ("text", "RO"),
        "sample_rate": ("number", "RO"),
        "bitrate": ("number", "RO"),
        "duration": ("number", "RO"),
        "tags": ("tag[]", "W"),
        "disposition": ("flag[]", "W"),
    },
    "subtitle_stream": {
        "index": ("number", "RO"),
        "language": ("text", "W"),
        "title": ("text", "W"),
        "codec": ("text", "RO"),
        "tags": ("tag[]", "W"),
        "disposition": ("flag[]", "W"),
    },
    "data_stream": {
        "index": ("number", "RO"),
        "language": ("text", "W"),
        "title": ("text", "W"),
        "codec": ("text", "RO"),
        "tags": ("tag[]", "W"),
        "disposition": ("flag[]", "W"),
    },
    "chapter": {
        "index": ("number", "RO"),
        "title": ("text", "W"),
        "start_t": ("number", "W"),
        "end_t": ("number", "W"),
    },
    "attachment": {
        "filename": ("text", "W"),
        "mimetype": ("text", "W"),
        "path": ("text", "W"),
    },
    "cue": {
        "index": ("number", "RO"),
        "start_t": ("number", "W"),
        "end_t": ("number", "W"),
        "text": ("text", "W"),
    },
    "container": {
        "video": ("video_stream[]", "W"),
        "audio": ("audio_stream[]", "W"),
        "subtitle": ("subtitle_stream[]", "W"),
        "data": ("data_stream[]", "W"),
        "chapters": ("chapter[]", "W"),
        "attachments": ("attachment[]", "W"),
        "t": ("seek", "RO"),
        "duration": ("number", "RO"),
        "tags": ("tag[]", "W"),
        "title": ("text", "W"),
        "artist": ("text", "W"),
        "album": ("text", "W"),
        "album_artist": ("text", "W"),
        "date": ("text", "W"),
        "genre": ("text", "W"),
        "comment": ("text", "W"),
        "composer": ("text", "W"),
        "track": ("text", "W"),
        "copyright": ("text", "W"),
        "encoder": ("text", "W"),
        "description": ("text", "W"),
    },
}

# The fields declared but not surfaced by the compiler today, as
# "<type>.<field>". Nothing else may carry `exposed=False`.
EXPECTED_UNEXPOSED = {
    "video_stream.tags",
    "video_stream.disposition",
    "audio_stream.tags",
    "audio_stream.disposition",
    "subtitle_stream.tags",
    "subtitle_stream.disposition",
    "data_stream.tags",
    "data_stream.disposition",
    "attachment.filename",
    "attachment.mimetype",
    "attachment.path",
    "cue.index",
    "cue.start_t",
    "cue.end_t",
    "cue.text",
    "container.attachments",
    "container.tags",
}

# The named columns that are one entry of their type's tag map.
EXPECTED_TAG_ENTRIES = {
    f"{stream}_stream.{name}"
    for stream in ("video", "audio", "subtitle", "data")
    for name in ("language", "title")
} | {
    "container.title",
    "container.artist",
    "container.album",
    "container.album_artist",
    "container.date",
    "container.genre",
    "container.comment",
    "container.composer",
    "container.track",
    "container.copyright",
    "container.encoder",
    "container.description",
}

EXPECTED_KINDS = {
    "text": "scalar",
    "number": "scalar",
    "boolean": "scalar",
    "stream": "handle",
    "seek": "handle",
    "tag": "record",
    "flag": "record",
    "video_stream": "stream",
    "audio_stream": "stream",
    "subtitle_stream": "stream",
    "data_stream": "stream",
    "chapter": "record",
    "attachment": "record",
    "cue": "record",
    "container": "container",
}

# --- The views, as parser.py spelled them out before they were derived. ---

EXPECTED_ROW_COMMON = {
    "index": "number",
    "language": "text",
    "title": "text",
    "codec": "text",
}

EXPECTED_ROW_SCHEMAS = {
    "audio": EXPECTED_ROW_COMMON
    | {
        "channels": "number",
        "channel_layout": "text",
        "sample_rate": "number",
        "bitrate": "number",
        "duration": "number",
    },
    "video": EXPECTED_ROW_COMMON
    | {
        "width": "number",
        "height": "number",
        "fps": "text",
        "bitrate": "number",
        "duration": "number",
        "color_transfer": "text",
    },
    "subtitle": dict(EXPECTED_ROW_COMMON),
    "data": dict(EXPECTED_ROW_COMMON),
    "chapters": {
        "index": "number",
        "title": "text",
        "start_t": "number",
        "end_t": "number",
    },
}

EXPECTED_INPUT_COLUMNS = frozenset(
    {"video", "audio", "subtitle", "data", "t", "duration", "chapters"}
)

EXPECTED_INPUT_TAG_COLUMNS = (
    "title",
    "artist",
    "album",
    "album_artist",
    "date",
    "genre",
    "comment",
    "composer",
    "track",
    "copyright",
    "encoder",
    "description",
)

EXPECTED_STREAM_ARRAY_COLUMNS = frozenset({"video", "audio", "subtitle", "data"})

EXPECTED_UNNEST_COLUMNS = EXPECTED_STREAM_ARRAY_COLUMNS | {"chapters"}


def _all_fields() -> list[tuple[str, Field]]:
    return [(declared.name, entry) for declared in TYPES.values() for entry in declared.fields]


def test_every_declared_type_is_keyed_by_its_own_name() -> None:
    for name, declared in TYPES.items():
        assert declared.name == name


def test_kinds_are_the_declared_ones() -> None:
    assert {name: declared.kind for name, declared in TYPES.items()} == EXPECTED_KINDS


def test_every_field_type_reference_resolves() -> None:
    for owner, entry in _all_fields():
        assert element_type(entry.type) in TYPES, f"{owner}.{entry.name}"
        assert resolve(entry.type) is TYPES[element_type(entry.type)]


def test_fields_match_the_reference_tables() -> None:
    """Name, type reference and W/RO mark, per type, in declaration order."""
    declared = {
        name: {f.name: (f.type, "W" if f.writable else "RO") for f in type_.fields}
        for name, type_ in TYPES.items()
    }
    assert declared == EXPECTED_FIELDS
    for name, type_ in TYPES.items():
        assert [f.name for f in type_.fields] == list(EXPECTED_FIELDS[name])


def test_field_names_are_unique_within_a_type() -> None:
    for declared in TYPES.values():
        names = [entry.name for entry in declared.fields]
        assert len(names) == len(set(names)), declared.name


def test_exposed_and_tag_entry_marks_are_the_declared_ones() -> None:
    unexposed = {f"{owner}.{f.name}" for owner, f in _all_fields() if not f.exposed}
    tag_entries = {f"{owner}.{f.name}" for owner, f in _all_fields() if f.tag_entry}
    assert unexposed == EXPECTED_UNEXPOSED
    assert tag_entries == EXPECTED_TAG_ENTRIES


def test_tag_entries_are_text_and_writable() -> None:
    for owner, entry in _all_fields():
        if entry.tag_entry:
            assert entry.type == "text", f"{owner}.{entry.name}"
            assert entry.writable, f"{owner}.{entry.name}"


def test_every_array_field_elements_a_declared_record() -> None:
    for owner, entry in _all_fields():
        if is_array(entry.type):
            assert resolve(entry.type).kind in {"stream", "record"}, f"{owner}.{entry.name}"


def test_scalars_and_handles_have_no_fields() -> None:
    for declared in TYPES.values():
        if declared.kind in {"scalar", "handle"}:
            assert declared.fields == ()


def test_column_types_are_declared_types() -> None:
    for name in COLUMN_TYPES:
        assert name in TYPES


def test_field_lookup() -> None:
    audio = TYPES["audio_stream"]
    assert audio.field("channels") == Field("channels", "number", writable=False)
    assert audio.field("width") is None


def test_resolve_rejects_an_undeclared_name() -> None:
    with pytest.raises(ValueError, match="undeclared type 'nope'"):
        resolve("nope[]")


def test_element_type_and_is_array() -> None:
    assert is_array("tag[]") and not is_array("tag")
    assert element_type("tag[]") == "tag"
    assert element_type("tag") == "tag"


def test_declarations_are_frozen() -> None:
    with pytest.raises(FrozenInstanceError):
        TYPES["chapter"].fields[0].name = "other"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        TYPES["chapter"].name = "other"  # type: ignore[misc]


def test_row_schemas_view() -> None:
    assert types.ROW_SCHEMAS == EXPECTED_ROW_SCHEMAS
    for column, schema in EXPECTED_ROW_SCHEMAS.items():
        assert list(types.ROW_SCHEMAS[column]) == list(schema), column


def test_row_common_view() -> None:
    assert types.ROW_COMMON == EXPECTED_ROW_COMMON
    assert list(types.ROW_COMMON) == list(EXPECTED_ROW_COMMON)


def test_column_set_views() -> None:
    assert types.INPUT_COLUMNS == EXPECTED_INPUT_COLUMNS
    assert types.UNNEST_COLUMNS == EXPECTED_UNNEST_COLUMNS
    assert types.STREAM_ARRAY_COLUMNS == EXPECTED_STREAM_ARRAY_COLUMNS


def test_named_column_views() -> None:
    assert types.INPUT_TAG_COLUMNS == EXPECTED_INPUT_TAG_COLUMNS
    assert types.STREAM_TAG_COLUMNS == ("language", "title")
    assert types.CHAPTERS_COLUMN == "chapters"
    assert types.INPUT_DURATION_COLUMN == "duration"
    assert types.TIME_COLUMN == "t"


def test_parser_reexports_are_the_view_objects() -> None:
    assert parser.ROW_SCHEMAS is types.ROW_SCHEMAS
    assert parser.INPUT_TAG_COLUMNS is types.INPUT_TAG_COLUMNS
    assert parser.INPUT_COLUMNS is types.INPUT_COLUMNS
    assert parser.UNNEST_COLUMNS is types.UNNEST_COLUMNS
    assert parser.STREAM_ARRAY_COLUMNS is types.STREAM_ARRAY_COLUMNS
    assert parser.CHAPTERS_COLUMN == types.CHAPTERS_COLUMN
    assert parser.INPUT_DURATION_COLUMN == types.INPUT_DURATION_COLUMN


def test_lower_stream_array_map_covers_the_stream_columns() -> None:
    """lower keeps its own column -> IR StreamType map; the keys must agree."""
    assert set(lower._ARRAY_COLUMNS) == types.STREAM_ARRAY_COLUMNS
    assert set(lower._PASSTHROUGH_ONLY) <= types.STREAM_ARRAY_COLUMNS
