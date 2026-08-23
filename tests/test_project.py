"""Tests for projects and packages: the two files, discovery, and resolution.

Every project, lockfile and store is built under ``tmp_path``: nothing here
reads the working directory, the home directory or the network, and the filter
surface is the captured snapshot (tests/conftest.py), so ``compile_sql``
resolves ``volume`` on a machine with no ffmpeg. The store lives wherever
``store._cache_dir`` says, which the suite points at a temporary directory and
the ``store_home`` fixture points at one per test.

The headline check is :func:`test_a_package_call_compiles_to_the_inline_argv`:
a call into a package produces the same ffmpeg argv, byte for byte, as the
same body written into the query itself.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest

from sqlmpeg import cli, store
from sqlmpeg.compiler import compile_commands, compile_sql, compile_table_sql
from sqlmpeg.emit import build_ffmpeg_args, emit
from sqlmpeg.errors import ErrorCode, SqlmpegError
from sqlmpeg.functions import package_signatures
from sqlmpeg.mcp import tools as mcp_tools
from sqlmpeg.project import (
    LOCK_FORMAT_VERSION,
    Dependency,
    LinkEntry,
    PackageSet,
    RegistryEntry,
    discover,
    find_manifest,
    read_lockfile,
    read_manifest,
    with_entry,
    without_entry,
    write_lockfile,
    write_manifest,
)
from sqlmpeg.warnings import SqlmpegWarning, WarningCode

QUIETER = (
    "CREATE FUNCTION quieter(track audio_stream, factor number) RETURNS audio_stream AS $$\n"
    "  SELECT volume(track, factor)\n"
    "$$ LANGUAGE sql;\n"
)
PICK = (
    "CREATE FUNCTION pick(path text) RETURNS TABLE(track audio_stream) AS $$\n"
    "  SELECT f.audio[1] FROM input(path) f\n"
    "$$ LANGUAGE sql;\n"
)
NORMALIZE = (
    "CREATE FUNCTION normalize_lang(raw text) RETURNS text AS $$\n"
    "  SELECT CASE WHEN raw = 'english' THEN 'eng' ELSE raw END\n"
    "$$ LANGUAGE sql;\n"
)


def _derived_libs(files: dict[str, str]) -> dict[str, str]:
    """A libs map exporting every definition the src files hold, in file order."""
    libs: dict[str, str] = {}
    for name, body in files.items():
        if not name.startswith("src/"):
            continue
        for defined in re.findall(r"CREATE FUNCTION (\w+)\(", body):
            libs[defined] = name
    return libs


def _project(
    root: Path,
    *,
    files: dict[str, str] | None = None,
    manifest: dict[str, object] | None = None,
    text: str | None = None,
) -> Path:
    """Write a project under `root` and return its manifest path.

    The default manifest names the package ``me/edits`` and exports every
    definition the ``src/`` files hold; `manifest` overrides keys, `text`
    writes the file verbatim, for the malformed cases a dict cannot express.
    """
    for name, body in (files or {}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    written = root / "sqlmpeg.json"
    written.parent.mkdir(parents=True, exist_ok=True)
    if text is not None:
        written.write_text(text, encoding="utf-8")
    else:
        declared: dict[str, object] = {"name": "me/edits", "version": "0.1.0"}
        libs = _derived_libs(files or {})
        if libs:
            declared["libs"] = libs
        declared.update(manifest or {})
        declared = {key: value for key, value in declared.items() if value is not None}
        written.write_text(json.dumps(declared, indent=2) + "\n", encoding="utf-8")
    return written


def _packages(root: Path) -> PackageSet:
    found = discover(root)
    assert found is not None
    return found


def _argv(sql: str, packages: PackageSet | None = None) -> list[str]:
    return build_ffmpeg_args(emit(compile_sql(sql, packages=packages)))


def _rejects(sql: str, packages: PackageSet | None, code: ErrorCode, needle: str) -> SqlmpegError:
    with pytest.raises(SqlmpegError) as caught:
        compile_commands(sql, packages=packages)
    error = caught.value
    assert error.code is code, f"{error.code} != {code}: {error}"
    assert needle in error.message, error.message
    return error


# ---------------------------------------------------------------------------
# the headline: a package call and the same body inline are one command
# ---------------------------------------------------------------------------


def test_a_package_call_compiles_to_the_inline_argv(tmp_path: Path) -> None:
    _project(tmp_path, files={"src/tracks.sql": QUIETER})
    query = (
        "COPY (SELECT f.video[1], {call}(f.audio[1], 0.5) FROM input('film.mkv') f) TO 'out.mkv'"
    )
    packaged = _argv(query.format(call="me.quieter"), _packages(tmp_path))
    inline = _argv(QUIETER + query.format(call="quieter"))
    assert packaged == inline
    assert "[0:a:0]volume=volume=0.5[out1]" in " ".join(packaged)


def test_a_table_returning_package_function_is_a_row_source(tmp_path: Path) -> None:
    _project(tmp_path, files={"src/tracks.sql": PICK})
    query = "COPY (SELECT t.track FROM {call}('a.mka') t) TO 'out.mka'"
    packaged = _argv(query.format(call="me.pick"), _packages(tmp_path))
    inline = _argv(PICK + query.format(call="pick"))
    assert packaged == inline
    assert packaged[:3] == ["ffmpeg", "-i", "a.mka"]


def test_a_package_function_reads_rows_as_a_table_query(tmp_path: Path) -> None:
    _project(tmp_path, files={"src/lang.sql": NORMALIZE})
    sinks = compile_table_sql(
        "WITH said(raw) AS (VALUES ('english'), ('de'))\n"
        "SELECT me.normalize_lang(said.raw) AS language FROM said",
        packages=_packages(tmp_path),
    )
    assert sinks[0].result.rows == [["eng"], ["de"]]


def test_the_default_lib_is_reached_as_the_package_segment(tmp_path: Path) -> None:
    """`lib`'s export is named for the package segment: `me.edits(...)`."""
    edits = (
        "CREATE FUNCTION edits(track audio_stream) RETURNS audio_stream AS $$\n"
        "  SELECT volume(track, 0.5)\n"
        "$$ LANGUAGE sql;\n"
    )
    _project(
        tmp_path,
        files={"src/default.sql": edits},
        manifest={"lib": "src/default.sql", "libs": None},
    )
    argv = _argv(
        "COPY (SELECT me.edits(f.audio[1]) FROM input('film.mkv') f) TO 'out.mkv'",
        _packages(tmp_path),
    )
    assert "volume=volume=0.5" in " ".join(argv)


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def test_discovery_walks_up_from_a_subdirectory(tmp_path: Path) -> None:
    manifest = _project(tmp_path, files={"src/tracks.sql": QUIETER})
    deep = tmp_path / "queries" / "nested"
    deep.mkdir(parents=True)
    assert find_manifest(deep) == manifest
    found = discover(deep)
    assert found is not None
    assert found.names() == ("me/edits",)
    assert found.namespaces() == ("me",)


def test_discovery_accepts_a_query_file_path(tmp_path: Path) -> None:
    manifest = _project(tmp_path, files={"src/tracks.sql": QUIETER})
    query = tmp_path / "queries" / "out.sql"
    query.parent.mkdir()
    query.write_text("SELECT 1", encoding="utf-8")
    assert find_manifest(query) == manifest


def test_no_manifest_is_no_project(tmp_path: Path) -> None:
    bare = tmp_path / "no_project"
    bare.mkdir()
    assert find_manifest(bare) is None
    assert discover(bare) is None


def test_without_a_project_a_query_compiles_exactly_as_before(tmp_path: Path) -> None:
    bare = tmp_path / "no_project"
    bare.mkdir()
    sql = QUIETER + (
        "COPY (SELECT quieter(f.audio[1], 0.5) FROM input('film.mkv') f) TO 'out.mkv'"
    )
    assert _argv(sql, discover(bare)) == _argv(sql)


def test_without_a_project_a_namespaced_call_is_rejected_as_it_always_was(tmp_path: Path) -> None:
    bare = tmp_path / "no_project"
    bare.mkdir()
    sql = "COPY (SELECT me.quieter(f.audio[1], 0.5) FROM input('film.mkv') f) TO 'out.mkv'"
    with pytest.raises(SqlmpegError) as without_project:
        compile_commands(sql, packages=discover(bare))
    with pytest.raises(SqlmpegError) as never_asked:
        compile_commands(sql)
    assert str(without_project.value) == str(never_asked.value)


# ---------------------------------------------------------------------------
# a package's lib files are a library; the script is not
# ---------------------------------------------------------------------------


def test_an_uncalled_package_definition_is_fine(tmp_path: Path) -> None:
    _project(tmp_path, files={"src/tracks.sql": QUIETER + NORMALIZE})
    argv = _argv(
        "COPY (SELECT me.quieter(f.audio[1], 0.5) FROM input('film.mkv') f) TO 'out.mkv'",
        _packages(tmp_path),
    )
    assert argv[-1] == "out.mkv"


def test_an_uncalled_script_definition_is_still_an_error(tmp_path: Path) -> None:
    _project(tmp_path, files={"src/tracks.sql": QUIETER})
    _rejects(
        NORMALIZE + "COPY (SELECT f.video[1] FROM input('film.mkv') f) TO 'out.mkv'",
        _packages(tmp_path),
        ErrorCode.UNSUPPORTED_SQL,
        "'normalize_lang' is never called",
    )


def test_a_package_body_calls_its_own_sibling_not_the_script(tmp_path: Path) -> None:
    """A bare name in a library body means the library's own definition."""
    library = (
        "CREATE FUNCTION helper(track audio_stream) RETURNS audio_stream AS $$\n"
        "  SELECT volume(track, 0.25)\n"
        "$$ LANGUAGE sql;\n"
        "CREATE FUNCTION quieter(track audio_stream) RETURNS audio_stream AS $$\n"
        "  SELECT helper(track)\n"
        "$$ LANGUAGE sql;\n"
    )
    _project(tmp_path, files={"src/tracks.sql": library})
    shadow = (
        "CREATE FUNCTION helper(track audio_stream) RETURNS audio_stream AS $$\n"
        "  SELECT volume(track, 8)\n"
        "$$ LANGUAGE sql;\n"
    )
    argv = _argv(
        shadow + "COPY (SELECT me.quieter(f.audio[1]), helper(f.audio[2]) "
        "FROM input('film.mkv') f) TO 'out.mkv'",
        _packages(tmp_path),
    )
    graph = " ".join(argv)
    assert "volume=volume=0.25" in graph
    assert "volume=volume=8" in graph


def test_a_definition_libs_does_not_name_is_private(tmp_path: Path) -> None:
    """A lib file may define more than the manifest exports; the rest are the package's own."""
    library = (
        "CREATE FUNCTION helper(track audio_stream) RETURNS audio_stream AS $$\n"
        "  SELECT volume(track, 0.25)\n"
        "$$ LANGUAGE sql;\n"
        "CREATE FUNCTION quieter(track audio_stream) RETURNS audio_stream AS $$\n"
        "  SELECT helper(track)\n"
        "$$ LANGUAGE sql;\n"
    )
    _project(
        tmp_path,
        files={"src/tracks.sql": library},
        manifest={"libs": {"quieter": "src/tracks.sql"}},
    )
    packages = _packages(tmp_path)
    argv = _argv(
        "COPY (SELECT me.quieter(f.audio[1]) FROM input('film.mkv') f) TO 'out.mkv'", packages
    )
    assert "volume=volume=0.25" in " ".join(argv)
    error = _rejects(
        "COPY (SELECT me.helper(f.audio[1]) FROM input('film.mkv') f) TO 'out.mkv'",
        packages,
        ErrorCode.UNKNOWN_FUNCTION,
        "package 'me/edits' has no export 'helper'",
    )
    assert error.hint == "me/edits exports: quieter"


# ---------------------------------------------------------------------------
# rejections
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("claimed", ["ffmpeg", "sqlmpeg", "wasm"])
def test_a_reserved_namespace_is_refused(tmp_path: Path, claimed: str) -> None:
    manifest = _project(
        tmp_path, files={"src/tracks.sql": QUIETER}, manifest={"name": f"{claimed}/edits"}
    )
    with pytest.raises(SqlmpegError) as caught:
        read_manifest(manifest)
    assert f"namespace '{claimed}' is reserved" in caught.value.message
    assert caught.value.line == 2


def test_an_unknown_namespace_says_what_this_project_has(tmp_path: Path) -> None:
    _project(tmp_path, files={"src/tracks.sql": QUIETER})
    error = _rejects(
        "COPY (SELECT you.quieter(f.audio[1], 0.5) FROM input('film.mkv') f) TO 'out.mkv'",
        _packages(tmp_path),
        ErrorCode.UNKNOWN_FUNCTION,
        "unknown namespace 'you'",
    )
    assert error.hint == "namespaces this project can call: me"


def test_a_near_miss_namespace_gets_a_did_you_mean(tmp_path: Path) -> None:
    _project(
        tmp_path, files={"src/tracks.sql": QUIETER}, manifest={"name": "mine/edits"}
    )
    error = _rejects(
        "COPY (SELECT mien.quieter(f.audio[1], 0.5) FROM input('film.mkv') f) TO 'out.mkv'",
        _packages(tmp_path),
        ErrorCode.UNKNOWN_FUNCTION,
        "unknown namespace 'mien'",
    )
    assert error.hint == "did you mean mine.quieter()?"


def test_an_unknown_member_gets_a_did_you_mean(tmp_path: Path) -> None:
    _project(tmp_path, files={"src/tracks.sql": QUIETER})
    error = _rejects(
        "COPY (SELECT me.quiter(f.audio[1], 0.5) FROM input('film.mkv') f) TO 'out.mkv'",
        _packages(tmp_path),
        ErrorCode.UNKNOWN_FUNCTION,
        "package 'me/edits' has no export 'quiter'",
    )
    assert error.hint == "did you mean me.quieter()?"


def test_an_export_naming_no_file_is_refused(tmp_path: Path) -> None:
    manifest = _project(
        tmp_path,
        files={"src/tracks.sql": QUIETER},
        manifest={"libs": {"quieter": "lib/tracks.sql"}},
    )
    with pytest.raises(SqlmpegError) as caught:
        read_manifest(manifest)
    assert "export 'quieter' names no file: 'lib/tracks.sql'" in caught.value.message


def test_an_export_leaving_the_project_is_refused(tmp_path: Path) -> None:
    manifest = _project(
        tmp_path,
        files={"src/tracks.sql": QUIETER},
        manifest={"libs": {"quieter": "../tracks.sql"}},
    )
    with pytest.raises(SqlmpegError) as caught:
        read_manifest(manifest)
    assert "leaves the project directory" in caught.value.message


def test_an_export_that_is_a_pattern_is_refused(tmp_path: Path) -> None:
    manifest = _project(
        tmp_path,
        files={"src/tracks.sql": QUIETER},
        manifest={"libs": {"quieter": "src/*.sql"}},
    )
    with pytest.raises(SqlmpegError) as caught:
        read_manifest(manifest)
    assert "names the pattern 'src/*.sql', not a file" in caught.value.message


def test_a_file_that_does_not_define_its_export_is_refused(tmp_path: Path) -> None:
    _project(
        tmp_path,
        files={"src/tracks.sql": QUIETER},
        manifest={"libs": {"louder": "src/tracks.sql"}},
    )
    error = _rejects(
        "COPY (SELECT me.louder(f.audio[1], 2) FROM input('film.mkv') f) TO 'out.mkv'",
        _packages(tmp_path),
        ErrorCode.UNSUPPORTED_SQL,
        "does not define 'louder'",
    )
    assert "tracks.sql" in error.message


def test_a_lib_file_that_does_not_define_the_default_is_refused(tmp_path: Path) -> None:
    manifest = _project(
        tmp_path,
        files={"src/tracks.sql": QUIETER},
        manifest={"lib": "src/tracks.sql", "libs": None},
    )
    with pytest.raises(SqlmpegError) as caught:
        package_signatures(read_manifest(manifest))
    assert "does not define 'edits'" in caught.value.message
    assert "package segment" in (caught.value.hint or "")


def test_one_name_defined_twice_across_lib_files_is_refused(tmp_path: Path) -> None:
    _project(
        tmp_path,
        files={"src/a.sql": QUIETER, "src/b.sql": QUIETER + NORMALIZE},
        manifest={"libs": {"quieter": "src/a.sql", "normalize_lang": "src/b.sql"}},
    )
    error = _rejects(
        "COPY (SELECT me.quieter(f.audio[1], 0.5) FROM input('film.mkv') f) TO 'out.mkv'",
        _packages(tmp_path),
        ErrorCode.UNSUPPORTED_SQL,
        "package 'me/edits' defines 'quieter' twice",
    )
    assert "a.sql" in error.message and "b.sql" in error.message


def test_a_lib_file_that_fails_to_parse_names_the_file(tmp_path: Path) -> None:
    _project(
        tmp_path,
        files={"src/tracks.sql": "CREATE FUNCTION oops("},
        manifest={"libs": {"quieter": "src/tracks.sql"}},
    )
    error = _rejects(
        "COPY (SELECT me.quieter(f.audio[1], 0.5) FROM input('film.mkv') f) TO 'out.mkv'",
        _packages(tmp_path),
        ErrorCode.PARSE_ERROR,
        "tracks.sql",
    )
    # The lib file's own line means nothing in the query, so the anchor is
    # the call that reached for it.
    assert (error.line, error.col) == (1, 14)


def test_a_lib_file_holding_a_query_is_refused(tmp_path: Path) -> None:
    _project(tmp_path, files={"src/tracks.sql": QUIETER + "SELECT 1;"})
    _rejects(
        "COPY (SELECT me.quieter(f.audio[1], 0.5) FROM input('film.mkv') f) TO 'out.mkv'",
        _packages(tmp_path),
        ErrorCode.UNSUPPORTED_SQL,
        "is not a CREATE FUNCTION",
    )


def test_a_value_function_called_in_from_is_refused(tmp_path: Path) -> None:
    _project(tmp_path, files={"src/tracks.sql": NORMALIZE})
    _rejects(
        "COPY (SELECT t.x FROM me.normalize_lang('en') t) TO 'out.mkv'",
        _packages(tmp_path),
        ErrorCode.UNSUPPORTED_SQL,
        "function 'me.normalize_lang' returns a value, not a table",
    )


def test_a_table_function_called_as_a_value_is_refused(tmp_path: Path) -> None:
    _project(tmp_path, files={"src/tracks.sql": PICK})
    _rejects(
        "COPY (SELECT me.pick('a.mka') FROM input('film.mkv') f) TO 'out.mkv'",
        _packages(tmp_path),
        ErrorCode.UNSUPPORTED_SQL,
        "function 'me.pick' returns a table, not a value",
    )


def test_the_wrong_argument_count_names_the_qualified_signature(tmp_path: Path) -> None:
    _project(tmp_path, files={"src/tracks.sql": QUIETER})
    error = _rejects(
        "COPY (SELECT me.quieter(f.audio[1]) FROM input('film.mkv') f) TO 'out.mkv'",
        _packages(tmp_path),
        ErrorCode.UDF_ARG_TYPE,
        "me.quieter() got 1 argument, but it declares 2",
    )
    assert error.hint == "me.quieter(track audio_stream, factor number) RETURNS audio_stream"


def test_two_packages_in_one_namespace_reject_a_two_part_call(tmp_path: Path) -> None:
    """The interim rule: `ns.fn` reaches the ONE package under `ns`."""
    first = _library(tmp_path / "one", "me", "0.5", package="alpha")
    second = _library(tmp_path / "two", "me", "0.25", package="beta")
    project = tmp_path / "work"
    _project(project, files={}, manifest={"name": "other/edits"})
    _lock(project, [_link(first), _link(second)])
    error = _rejects(
        "COPY (SELECT me.quieter(f.audio[1]) FROM input('film.mkv') f) TO 'out.mkv'",
        _packages(project),
        ErrorCode.UNKNOWN_FUNCTION,
        "namespace 'me' holds more than one package",
    )
    assert "me/alpha" in error.message and "me/beta" in error.message


# ---------------------------------------------------------------------------
# manifest validation
# ---------------------------------------------------------------------------


def test_a_manifest_that_is_not_json_is_anchored(tmp_path: Path) -> None:
    manifest = _project(tmp_path, files={"src/tracks.sql": QUIETER}, text='{\n  "name",\n}\n')
    with pytest.raises(SqlmpegError) as caught:
        read_manifest(manifest)
    assert "is not valid JSON" in caught.value.message
    assert caught.value.line == 2


def test_a_manifest_that_is_not_an_object_is_refused(tmp_path: Path) -> None:
    manifest = _project(tmp_path, files={"src/tracks.sql": QUIETER}, text="[]\n")
    with pytest.raises(SqlmpegError) as caught:
        read_manifest(manifest)
    assert "is not a JSON object" in caught.value.message


@pytest.mark.parametrize("missing", ["name", "version"])
def test_every_required_key_is_required(tmp_path: Path, missing: str) -> None:
    declared: dict[str, object] = {"name": "me/edits", "version": "0.1.0"}
    del declared[missing]
    manifest = _project(tmp_path, text=json.dumps(declared, indent=2))
    with pytest.raises(SqlmpegError) as caught:
        read_manifest(manifest)
    assert f'is missing "{missing}"' in caught.value.message


@pytest.mark.parametrize(
    "claimed", ["me", "My/edits", "me/1st", "a-b/c", "a/b/c", "a.b/c", "", "me/"]
)
def test_a_name_must_be_two_plain_identifiers(tmp_path: Path, claimed: str) -> None:
    manifest = _project(tmp_path, manifest={"name": claimed})
    with pytest.raises(SqlmpegError) as caught:
        read_manifest(manifest)
    assert "name" in caught.value.message


def test_namespace_is_no_longer_a_key(tmp_path: Path) -> None:
    manifest = _project(tmp_path, manifest={"namespace": "me"})
    with pytest.raises(SqlmpegError) as caught:
        read_manifest(manifest)
    assert "unknown key 'namespace'" in caught.value.message
    assert "the name carries the namespace" in (caught.value.hint or "")


def test_exports_is_no_longer_a_key(tmp_path: Path) -> None:
    manifest = _project(tmp_path, manifest={"exports": ["src/*.sql"]})
    with pytest.raises(SqlmpegError) as caught:
        read_manifest(manifest)
    assert "unknown key 'exports'" in caught.value.message
    assert "libs" in (caught.value.hint or "")


def test_an_unknown_key_gets_a_did_you_mean(tmp_path: Path) -> None:
    manifest = _project(tmp_path, manifest={"binz": {}})
    with pytest.raises(SqlmpegError) as caught:
        read_manifest(manifest)
    assert "unknown key 'binz'" in caught.value.message
    assert caught.value.hint == "did you mean 'bin'?"


def test_a_description_and_dependencies_are_accepted(tmp_path: Path) -> None:
    manifest = _project(
        tmp_path,
        files={"src/tracks.sql": QUIETER},
        manifest={"description": "edits", "dependencies": {"tracks": "broadcast/tracks@^1.2.0"}},
    )
    package = read_manifest(manifest)
    assert package.name == "me/edits"
    assert package.namespace == "me" and package.package == "edits"
    assert package.version == "0.1.0"
    assert [path.name for path in package.exports.values()] == ["tracks.sql"]
    assert package.aliases == {"tracks": Dependency(name="broadcast/tracks", range="^1.2.0")}


def test_the_default_export_comes_first_and_is_named_for_the_segment(tmp_path: Path) -> None:
    edits = QUIETER.replace("quieter", "edits")
    manifest = _project(
        tmp_path,
        files={"src/default.sql": edits, "src/tracks.sql": QUIETER},
        manifest={"lib": "src/default.sql", "libs": {"quieter": "src/tracks.sql"}},
    )
    package = read_manifest(manifest)
    assert list(package.exports) == ["edits", "quieter"]
    assert package.export() == tmp_path / "src" / "default.sql"
    assert package.export("quieter") == tmp_path / "src" / "tracks.sql"
    assert package.export("nothing") is None


def test_libs_may_not_claim_the_packages_own_name(tmp_path: Path) -> None:
    edits = QUIETER.replace("quieter", "edits")
    manifest = _project(
        tmp_path,
        files={"src/default.sql": edits},
        manifest={"libs": {"edits": "src/default.sql"}},
    )
    with pytest.raises(SqlmpegError) as caught:
        read_manifest(manifest)
    assert "libs declares 'edits', the package's own name" in caught.value.message
    assert '"lib"' in (caught.value.hint or "")


def test_bins_may_not_claim_the_packages_own_name(tmp_path: Path) -> None:
    manifest = _project(
        tmp_path,
        files={"queries/split.sql": PROGRAM},
        manifest={"bins": {"edits": "queries/split.sql"}},
    )
    with pytest.raises(SqlmpegError) as caught:
        read_manifest(manifest)
    assert "bins declares 'edits', the package's own name" in caught.value.message


@pytest.mark.parametrize("alias", ["Tracks", "a-b", ""])
def test_an_alias_must_be_a_plain_identifier(tmp_path: Path, alias: str) -> None:
    manifest = _project(
        tmp_path, manifest={"dependencies": {alias: "broadcast/tracks@^1.0.0"}}
    )
    with pytest.raises(SqlmpegError) as caught:
        read_manifest(manifest)
    assert f"alias {alias!r} is not a plain identifier" in caught.value.message


def test_a_reserved_alias_is_refused(tmp_path: Path) -> None:
    manifest = _project(
        tmp_path, manifest={"dependencies": {"ffmpeg": "broadcast/tracks@^1.0.0"}}
    )
    with pytest.raises(SqlmpegError) as caught:
        read_manifest(manifest)
    assert "alias 'ffmpeg' is reserved" in caught.value.message


@pytest.mark.parametrize(
    "written", ["broadcast/tracks", "tracks@^1.0.0", "broadcast/tracks@", "", 7]
)
def test_a_dependency_value_keeps_name_and_range_together(
    tmp_path: Path, written: object
) -> None:
    manifest = _project(tmp_path, manifest={"dependencies": {"tracks": written}})
    with pytest.raises(SqlmpegError) as caught:
        read_manifest(manifest)
    assert "dependency 'tracks'" in caught.value.message


# ---------------------------------------------------------------------------
# what a package provides: exports, programs, or neither
# ---------------------------------------------------------------------------

PROGRAM = (
    "-- variables: source (input media path), dest (output path)\n"
    "COPY (SELECT f.video[1] FROM input(:'source') f) TO :'dest';\n"
)

_BINS = {"bins": {"split-chapters": "queries/split.sql"}}


def _manifest_text(**declared: object) -> str:
    """A manifest written key by key, so a rejection's line is predictable."""
    return json.dumps({"name": "me/edits", "version": "0.1.0", **declared}, indent=2)


def test_a_manifest_declaring_neither_half_is_a_package(tmp_path: Path) -> None:
    """The consumer project: a name and its dependencies, nothing provided."""
    manifest = _project(
        tmp_path, text=_manifest_text(dependencies={"tracks": "broadcast/tracks@^1.2.0"})
    )
    package = read_manifest(manifest)
    assert dict(package.exports) == {}
    assert dict(package.programs) == {}


def test_bins_declares_a_program_beside_the_exports(tmp_path: Path) -> None:
    manifest = _project(
        tmp_path,
        files={"src/tracks.sql": QUIETER, "queries/split.sql": PROGRAM},
        manifest=_BINS,
    )
    package = read_manifest(manifest)
    assert list(package.programs) == ["split-chapters"]
    assert package.program("split-chapters") == tmp_path / "queries" / "split.sql"
    assert package.program("nothing-like-it") is None


def test_bin_declares_the_default_program(tmp_path: Path) -> None:
    manifest = _project(
        tmp_path,
        files={"queries/split.sql": PROGRAM},
        text=_manifest_text(bin="queries/split.sql"),
    )
    package = read_manifest(manifest)
    assert list(package.programs) == ["edits"]
    assert package.program() == tmp_path / "queries" / "split.sql"


def test_a_package_may_ship_programs_and_export_nothing(tmp_path: Path) -> None:
    manifest = _project(
        tmp_path, files={"queries/split.sql": PROGRAM}, text=_manifest_text(**_BINS)
    )
    package = read_manifest(manifest)
    assert dict(package.exports) == {}
    assert list(package.programs) == ["split-chapters"]


def test_a_program_is_a_query_and_the_lib_rule_never_reaches_it(tmp_path: Path) -> None:
    """A bin file holds a whole query -- the rule that rejects one in a lib
    file is about lib files, and a compile that resolves into the package proves it."""
    _project(
        tmp_path,
        files={"src/tracks.sql": QUIETER, "queries/split.sql": PROGRAM},
        manifest=_BINS,
    )
    argv = _argv(
        "COPY (SELECT me.quieter(f.audio[1], 0.5) FROM input('film.mkv') f) TO 'out.mkv'",
        _packages(tmp_path),
    )
    assert "volume=volume=0.5" in " ".join(argv)


@pytest.mark.parametrize("claimed", ["Split", "1st", "-split", "split chapters", "", "split.sh"])
def test_a_program_name_is_a_command_name(tmp_path: Path, claimed: str) -> None:
    manifest = _project(
        tmp_path,
        files={"queries/split.sql": PROGRAM},
        text=_manifest_text(bins={claimed: "queries/split.sql"}),
    )
    with pytest.raises(SqlmpegError) as caught:
        read_manifest(manifest)
    assert f"program name {claimed!r} is not a command name" in caught.value.message
    assert caught.value.line == 5


def test_bins_that_is_not_an_object_is_refused(tmp_path: Path) -> None:
    manifest = _project(
        tmp_path,
        files={"queries/split.sql": PROGRAM},
        text=_manifest_text(bins=["queries/split.sql"]),
    )
    with pytest.raises(SqlmpegError) as caught:
        read_manifest(manifest)
    assert '"bins" must be a JSON object' in caught.value.message


def test_bin_that_is_an_object_is_refused(tmp_path: Path) -> None:
    """The old shape: a map under `bin`. One file now; the map is `bins`."""
    manifest = _project(
        tmp_path,
        files={"queries/split.sql": PROGRAM},
        text=_manifest_text(bin={"split": "queries/split.sql"}),
    )
    with pytest.raises(SqlmpegError) as caught:
        read_manifest(manifest)
    assert '"bin" must name one file' in caught.value.message


def test_a_program_that_names_no_string_is_refused(tmp_path: Path) -> None:
    manifest = _project(
        tmp_path, files={"queries/split.sql": PROGRAM}, text=_manifest_text(bins={"split": 1})
    )
    with pytest.raises(SqlmpegError) as caught:
        read_manifest(manifest)
    assert "program 'split' must name one file" in caught.value.message


def test_a_program_leaving_the_project_is_refused(tmp_path: Path) -> None:
    manifest = _project(
        tmp_path,
        files={"queries/split.sql": PROGRAM},
        text=_manifest_text(bins={"split": "../split.sql"}),
    )
    with pytest.raises(SqlmpegError) as caught:
        read_manifest(manifest)
    assert "leaves the project directory" in caught.value.message


def test_a_program_matching_no_file_is_refused(tmp_path: Path) -> None:
    manifest = _project(
        tmp_path,
        files={"queries/split.sql": PROGRAM},
        text=_manifest_text(bins={"split": "queries/gone.sql"}),
    )
    with pytest.raises(SqlmpegError) as caught:
        read_manifest(manifest)
    assert "program 'split' names no file: 'queries/gone.sql'" in caught.value.message


def test_a_program_that_is_a_pattern_is_refused(tmp_path: Path) -> None:
    manifest = _project(
        tmp_path,
        files={"queries/split.sql": PROGRAM},
        text=_manifest_text(bins={"split": "queries/*.sql"}),
    )
    with pytest.raises(SqlmpegError) as caught:
        read_manifest(manifest)
    assert "names the pattern 'queries/*.sql', not a file" in caught.value.message


def test_two_programs_written_under_one_name_are_refused(tmp_path: Path) -> None:
    """``json`` keeps the last of two same-named keys, which would drop one silently."""
    manifest = _project(
        tmp_path,
        files={"queries/split.sql": PROGRAM, "queries/other.sql": PROGRAM},
        text=(
            '{\n  "name": "me/edits",\n  "version": "0.1.0",\n'
            '  "bins": {\n    "split": "queries/split.sql",\n'
            '    "split": "queries/other.sql"\n  }\n}\n'
        ),
    )
    with pytest.raises(SqlmpegError) as caught:
        read_manifest(manifest)
    assert "bins declares 'split' twice" in caught.value.message
    assert caught.value.line == 5


def test_the_signatures_a_package_exports_are_readable_without_a_query(tmp_path: Path) -> None:
    manifest = _project(tmp_path, files={"src/tracks.sql": QUIETER + NORMALIZE})
    signatures = package_signatures(read_manifest(manifest))
    assert [signature.written for signature in signatures] == [
        "quieter(track audio_stream, factor number)",
        "normalize_lang(raw text)",
    ]
    assert [signature.package for signature in signatures] == ["me/edits", "me/edits"]
    assert [signature.returns for signature in signatures] == ["audio_stream", "text"]
    assert [signature.export.name for signature in signatures] == ["tracks.sql", "tracks.sql"]


def test_reading_the_signatures_of_a_broken_lib_file_is_refused(tmp_path: Path) -> None:
    manifest = _project(tmp_path, files={"src/tracks.sql": QUIETER + "SELECT 1;"})
    with pytest.raises(SqlmpegError) as caught:
        package_signatures(read_manifest(manifest))
    assert "is not a CREATE FUNCTION" in caught.value.message
    assert "src" in caught.value.message


# ---------------------------------------------------------------------------
# a qualifier owns the name under it
# ---------------------------------------------------------------------------


def test_a_script_function_does_not_shadow_a_filter_call() -> None:
    """`ffmpeg.<name>` is the installed ffmpeg's, whatever the script defines.

    The definition stays uncalled, which is what says the qualified call never
    reached it -- inlining it would have marked it used and compiled.
    """
    definition = (
        "CREATE FUNCTION quiet(x audio_stream) RETURNS audio_stream AS $$\n"
        "  SELECT volume(x, 0.5)\n"
        "$$ LANGUAGE sql;\n"
    )
    _rejects(
        definition + "COPY (SELECT ffmpeg.quiet(f.audio[1]) FROM input('a.mp4') f) TO 'o.mp4'",
        None,
        ErrorCode.UNSUPPORTED_SQL,
        "function 'quiet' is never called",
    )


def test_a_script_function_does_not_shadow_a_generated_source() -> None:
    definition = (
        "CREATE FUNCTION testsrc(path text) RETURNS TABLE(v video_stream) AS $$\n"
        "  SELECT f.video[1] FROM input(path) f\n"
        "$$ LANGUAGE sql;\n"
    )
    _rejects(
        definition + "COPY (SELECT t.video[1] FROM ffmpeg.testsrc(duration => 2) t) TO 'o.mp4'",
        None,
        ErrorCode.UNSUPPORTED_SQL,
        "'testsrc' is never called",
    )


# ---------------------------------------------------------------------------
# the CLI derives the project from -f's path, or the working directory
# ---------------------------------------------------------------------------


def test_the_cli_finds_the_project_above_the_query_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _project(tmp_path, files={"src/tracks.sql": QUIETER})
    query = tmp_path / "queries" / "out.sql"
    query.parent.mkdir()
    query.write_text(
        "COPY (SELECT me.quieter(f.audio[1], 0.5) FROM input('film.mkv') f) TO 'out.mkv'",
        encoding="utf-8",
    )
    assert cli.main(["compile", "-f", str(query)]) == 0
    assert "volume=volume=0.5" in capsys.readouterr().out


def test_the_cli_finds_the_project_above_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _project(tmp_path, files={"src/tracks.sql": QUIETER})
    deep = tmp_path / "queries"
    deep.mkdir()
    monkeypatch.chdir(deep)
    code = cli.main(
        ["compile", "COPY (SELECT me.quieter(f.audio[1], 0.5) FROM input('f.mkv') f) TO 'o.mkv'"]
    )
    assert code == 0
    assert "volume=volume=0.5" in capsys.readouterr().out


def test_the_cli_reports_a_malformed_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _project(tmp_path, files={"src/tracks.sql": QUIETER}, text="{ nope\n")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["validate", "SELECT f.video[1] FROM input('f.mkv') f"]) == 1
    assert "sqlmpeg.json" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# the MCP tools take the project as an argument, never from the process
# ---------------------------------------------------------------------------


def test_the_mcp_tools_resolve_against_the_named_project(tmp_path: Path) -> None:
    _project(tmp_path, files={"src/tracks.sql": QUIETER})
    query = "COPY (SELECT me.quieter(f.audio[1], 0.5) FROM input('f.mkv') f) TO 'o.mkv'"
    assert mcp_tools.validate_query(query, None, str(tmp_path)) == {}
    result = mcp_tools.compile_query(query, None, str(tmp_path))
    assert "volume=volume=0.5" in result["filter_complex"][0]


def test_the_mcp_tools_see_no_project_without_one(tmp_path: Path) -> None:
    _project(tmp_path, files={"src/tracks.sql": QUIETER})
    query = "COPY (SELECT me.quieter(f.audio[1], 0.5) FROM input('f.mkv') f) TO 'o.mkv'"
    assert mcp_tools.validate_query(query)["code"] == ErrorCode.UNSUPPORTED_SQL.value


def test_a_malformed_manifest_is_data_for_validate(tmp_path: Path) -> None:
    _project(tmp_path, files={"src/tracks.sql": QUIETER}, text="{ nope\n")
    error = mcp_tools.validate_query(
        "SELECT f.video[1] FROM input('f.mkv') f", None, str(tmp_path)
    )
    assert "sqlmpeg.json" in error["message"]


# ---------------------------------------------------------------------------
# the lockfile layers: the store, links, and which layer answers
# ---------------------------------------------------------------------------

QUERY = "COPY (SELECT {call}(f.audio[1]) FROM input('film.mkv') f) TO 'out.mkv'"


def _quieter(factor: str) -> str:
    """A one-argument ``quieter`` whose factor shows up in the filter graph.

    Which layer answered is then readable straight off the compiled command.
    """
    return (
        "CREATE FUNCTION quieter(track audio_stream) RETURNS audio_stream AS $$\n"
        f"  SELECT volume(track, {factor})\n"
        "$$ LANGUAGE sql;\n"
    )


def _library(
    root: Path, namespace: str, factor: str, *, version: str = "1.0.0", package: str = "lib"
) -> Path:
    """A package directory of its own: a manifest named `namespace`/`package`, one source."""
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "lib.sql").write_text(_quieter(factor), encoding="utf-8")
    (root / "sqlmpeg.json").write_text(
        json.dumps(
            {
                "name": f"{namespace}/{package}",
                "version": version,
                "libs": {"quieter": "src/lib.sql"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return root


def _digest(archive: bytes) -> str:
    return hashlib.sha256(archive).hexdigest()


def _installed(source: Path) -> dict[str, object]:
    """Put a package in the store the way installing does; return the entry that pins it."""
    package = read_manifest(source / "sqlmpeg.json")
    archive = store.pack(source)
    sha256 = _digest(archive)
    store.unpack(package.name, archive, sha256)
    return {
        "kind": "registry",
        "name": package.name,
        "version": package.version,
        "sha256": sha256,
        "store": store.entry_path(sha256),
    }


def _link(directory: Path) -> dict[str, object]:
    return {"kind": "link", "path": str(directory)}


def _lock(
    directory: Path,
    entries: list[dict[str, object]],
    *,
    reproducible: bool | None = None,
    text: str | None = None,
) -> Path:
    """Write a lockfile the way installing would, and return its path."""
    path = directory / "sqlmpeg.lock"
    directory.mkdir(parents=True, exist_ok=True)
    if text is not None:
        path.write_text(text, encoding="utf-8")
        return path
    linked = [entry for entry in entries if entry.get("kind") == "link"]
    honest = not linked if reproducible is None else reproducible
    data: dict[str, object] = {"format_version": LOCK_FORMAT_VERSION, "reproducible": honest}
    if not honest:
        data["not_reproducible_because"] = (
            "a package is linked to a working directory, so its files are not pinned here"
        )
    data["packages"] = entries
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def store_home(_isolated_store: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the store and the machine-wide lockfile at this test's own directory."""
    home = tmp_path / "cache"
    home.mkdir()
    monkeypatch.setattr(store, "_cache_dir", lambda: home)
    return home


def _heard(sql: str, packages: PackageSet | None) -> tuple[list[str], list[SqlmpegWarning]]:
    """The compiled argv, and everything the compile had to say about it."""
    said: list[SqlmpegWarning] = []
    argv = build_ffmpeg_args(emit(compile_sql(sql, packages=packages, on_warning=said.append)))
    return argv, said


def _codes(said: list[SqlmpegWarning]) -> list[WarningCode]:
    return [warning.code for warning in said]


def _refuses(root: Path, needle: str) -> SqlmpegError:
    with pytest.raises(SqlmpegError) as caught:
        discover(root)
    assert needle in caught.value.message, caught.value.message
    return caught.value


def test_a_locked_package_resolves_out_of_the_store(store_home: Path, tmp_path: Path) -> None:
    entry = _installed(_library(tmp_path / "built", "tracks", "0.5"))
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    _lock(project, [entry])
    argv, said = _heard(QUERY.format(call="tracks.quieter"), _packages(project))
    assert argv == _argv(_quieter("0.5") + QUERY.format(call="quieter"))
    assert said == []


def test_content_missing_from_the_store_is_refused(store_home: Path, tmp_path: Path) -> None:
    entry = _installed(_library(tmp_path / "built", "tracks", "0.5"))
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    _lock(project, [entry])
    shutil.rmtree(store.store_dir() / str(entry["store"]))
    error = _refuses(project, "is not in the store")
    assert "tracks/lib" in error.message


def test_a_store_path_from_another_layout_is_refused(store_home: Path, tmp_path: Path) -> None:
    entry = _installed(_library(tmp_path / "built", "tracks", "0.5"))
    entry["store"] = str(entry["store"]).replace(store.STORE_FORMAT, "v99", 1)
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    _lock(project, [entry])
    error = _refuses(project, "store format")
    assert "v99" in error.message


def test_a_store_path_that_is_not_the_digest_s_own_is_refused(
    store_home: Path, tmp_path: Path
) -> None:
    """An entry pinning one digest while pointing at another's content."""
    entry = _installed(_library(tmp_path / "built", "tracks", "0.5"))
    other = _installed(_library(tmp_path / "other", "far", "0.1"))
    entry["store"] = other["store"]
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    _lock(project, [entry])
    error = _refuses(project, "is not where content of")
    assert str(entry["sha256"]) in error.message


def test_a_lockfile_entry_the_stored_package_disagrees_with_is_refused(
    store_home: Path, tmp_path: Path
) -> None:
    entry = _installed(_library(tmp_path / "built", "tracks", "0.5"))
    entry["version"] = "9.9.9"
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    _lock(project, [entry])
    _refuses(project, "records version '9.9.9'")


# ---------------------------------------------------------------------------
# the archive: packing, verifying, extracting
# ---------------------------------------------------------------------------


def _archive(add: Callable[[tarfile.TarFile], None]) -> tuple[bytes, str]:
    """A gzipped tar built member by member, and the digest of its bytes."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as archive:
        add(archive)
    data = raw.getvalue()
    return data, _digest(data)


def _one_member(
    name: str, *, kind: bytes = tarfile.REGTYPE, linkname: str = ""
) -> tuple[bytes, str]:
    """An archive holding one member of the given name and kind."""

    def add(archive: tarfile.TarFile) -> None:
        info = tarfile.TarInfo(name)
        info.type = kind
        info.linkname = linkname
        if kind != tarfile.REGTYPE:
            archive.addfile(info)
            return
        info.size = len(b"payload")
        archive.addfile(info, io.BytesIO(b"payload"))

    return _archive(add)


def _unpacking(archive: bytes, sha256: str) -> SqlmpegError:
    with pytest.raises(SqlmpegError) as caught:
        store.unpack("broadcast/tracks", archive, sha256)
    return caught.value


def _nothing_stored(sha256: str) -> None:
    """Neither the entry nor a half-written one beside it survived the rejection."""
    entry = store.store_dir() / store.entry_path(sha256)
    assert not entry.exists()
    assert not entry.parent.exists() or list(entry.parent.iterdir()) == []


def test_packing_a_tree_twice_produces_the_same_bytes(tmp_path: Path) -> None:
    source = _library(tmp_path / "built", "tracks", "0.5")
    assert store.pack(source) == store.pack(source)


def test_packing_the_same_content_from_two_directories_produces_the_same_bytes(
    tmp_path: Path,
) -> None:
    first = _library(tmp_path / "one", "tracks", "0.5")
    second = _library(tmp_path / "two", "tracks", "0.5")
    assert store.pack(first) == store.pack(second)


def test_packing_a_tree_holding_a_link_is_refused(tmp_path: Path) -> None:
    source = _library(tmp_path / "built", "tracks", "0.5")
    try:
        (source / "src" / "elsewhere.sql").symlink_to(tmp_path / "outside.sql")
    except (NotImplementedError, OSError) as err:  # a platform that will not make one
        pytest.skip(f"symlinks unavailable: {err}")
    with pytest.raises(SqlmpegError) as caught:
        store.pack(source)
    assert "regular files and directories only" in caught.value.message


def test_a_verified_archive_unpacks_into_the_store(store_home: Path, tmp_path: Path) -> None:
    source = _library(tmp_path / "built", "tracks", "0.5")
    archive = store.pack(source)
    sha256 = _digest(archive)
    stored = store.unpack("tracks/lib", archive, sha256)
    assert stored == store.store_dir() / store.entry_path(sha256)
    assert (stored / "src" / "lib.sql").read_text(encoding="utf-8") == _quieter("0.5")
    read_manifest(stored / "sqlmpeg.json")


def test_an_archive_that_is_not_what_was_pinned_is_refused(
    store_home: Path, tmp_path: Path
) -> None:
    pinned = _digest(store.pack(_library(tmp_path / "built", "tracks", "0.5")))
    swapped = store.pack(_library(tmp_path / "other", "tracks", "0.25"))
    error = _unpacking(swapped, pinned)
    assert "hashes to" in error.message
    assert pinned in error.message
    _nothing_stored(pinned)


def test_bytes_that_are_not_an_archive_are_refused_by_their_digest(store_home: Path) -> None:
    # The rejection names the digest, not a malformed tar: the bytes were never
    # handed to an unpacker.
    pinned = "b" * 64
    error = _unpacking(b"not an archive at all", pinned)
    assert "hashes to" in error.message
    _nothing_stored(pinned)


def test_unpacking_a_digest_already_in_the_store_leaves_it_alone(
    store_home: Path, tmp_path: Path
) -> None:
    archive = store.pack(_library(tmp_path / "built", "tracks", "0.5"))
    sha256 = _digest(archive)
    stored = store.unpack("tracks/lib", archive, sha256)
    (stored / "marker").write_text("kept", encoding="utf-8")
    assert store.unpack("tracks/lib", archive, sha256) == stored
    assert (stored / "marker").read_text(encoding="utf-8") == "kept"


@pytest.mark.parametrize(
    "name",
    ["../escape.sql", "/etc/escape.sql", "..", "src/../../escape.sql", "..\\escape.sql"],
)
def test_a_member_that_leaves_the_destination_is_refused(store_home: Path, name: str) -> None:
    archive, sha256 = _one_member(name)
    error = _unpacking(archive, sha256)
    assert "leaves the directory" in error.message
    _nothing_stored(sha256)


@pytest.mark.parametrize(
    "kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE]
)
def test_a_member_that_is_not_a_file_or_a_directory_is_refused(
    store_home: Path, kind: bytes
) -> None:
    archive, sha256 = _one_member("passwd", kind=kind, linkname="src/lib.sql")
    error = _unpacking(archive, sha256)
    assert "neither a regular file nor a directory" in error.message
    _nothing_stored(sha256)


def test_an_archive_unpacking_past_the_size_cap_is_refused(
    store_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store, "_MAX_UNPACKED_BYTES", 64)

    def add(archive: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("big.sql")
        info.size = 4096
        archive.addfile(info, io.BytesIO(b"\0" * 4096))

    archive, sha256 = _archive(add)
    error = _unpacking(archive, sha256)
    assert "more than 64 bytes" in error.message
    _nothing_stored(sha256)


def test_an_archive_past_the_member_cap_is_refused(
    store_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store, "_MAX_MEMBERS", 2)

    def add(archive: tarfile.TarFile) -> None:
        for index in range(3):
            info = tarfile.TarInfo(f"{index}.sql")
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))

    archive, sha256 = _archive(add)
    error = _unpacking(archive, sha256)
    assert "more than 2 members" in error.message
    _nothing_stored(sha256)


def test_a_link_resolves_through_the_directorys_own_manifest(tmp_path: Path) -> None:
    linked = _library(tmp_path / "dev", "tracks", "0.5")
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    _lock(project, [_link(linked)])
    argv, said = _heard(QUERY.format(call="tracks.quieter"), _packages(project))
    assert argv == _argv(_quieter("0.5") + QUERY.format(call="quieter"))
    assert _codes(said) == [WarningCode.LINKED_PACKAGE]


def test_a_link_picks_up_an_edit_made_after_the_lockfile_was_written(tmp_path: Path) -> None:
    linked = _library(tmp_path / "dev", "tracks", "0.5")
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    _lock(project, [_link(linked)])
    before, _ = _heard(QUERY.format(call="tracks.quieter"), _packages(project))
    (linked / "src" / "lib.sql").write_text(_quieter("0.25"), encoding="utf-8")
    after, _ = _heard(QUERY.format(call="tracks.quieter"), _packages(project))
    assert "volume=volume=0.5" in " ".join(before)
    assert "volume=volume=0.25" in " ".join(after)


def test_a_link_by_relative_path_resolves_against_the_lockfile(tmp_path: Path) -> None:
    _library(tmp_path / "dev", "tracks", "0.5")
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    _lock(project, [{"kind": "link", "path": "../dev"}])
    argv, _ = _heard(QUERY.format(call="tracks.quieter"), _packages(project))
    assert "volume=volume=0.5" in " ".join(argv)


def test_a_link_warns_once_however_many_call_sites(tmp_path: Path) -> None:
    linked = _library(tmp_path / "dev", "tracks", "0.5")
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    _lock(project, [_link(linked)])
    _compiled, said = _heard(
        "COPY (SELECT tracks.quieter(f.audio[1]), tracks.quieter(f.audio[2]) "
        "FROM input('film.mkv') f) TO 'out.mkv'",
        _packages(project),
    )
    assert _codes(said) == [WarningCode.LINKED_PACKAGE]
    assert said[0].package == "tracks/lib"
    assert str(linked) in said[0].message


def test_a_linked_directory_with_no_manifest_is_refused(tmp_path: Path) -> None:
    empty = tmp_path / "dev"
    empty.mkdir()
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    _lock(project, [_link(empty)])
    _refuses(project, "holds no sqlmpeg.json")


def test_a_linked_package_may_rename_itself_without_a_re_link(tmp_path: Path) -> None:
    """The entry records only the directory; the name is the manifest's."""
    linked = _library(tmp_path / "dev", "tracks", "0.5")
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    _lock(project, [_link(linked)])
    assert "tracks/lib" in _packages(project).names()
    renamed = json.loads((linked / "sqlmpeg.json").read_text(encoding="utf-8"))
    renamed["name"] = "broadcast/audio"
    (linked / "sqlmpeg.json").write_text(json.dumps(renamed) + "\n", encoding="utf-8")
    packages = _packages(project)
    assert "broadcast/audio" in packages.names()
    argv, _ = _heard(QUERY.format(call="broadcast.quieter"), packages)
    assert "volume=volume=0.5" in " ".join(argv)


def test_the_manifest_wins_over_a_lockfile_naming_its_package(tmp_path: Path) -> None:
    linked = _library(tmp_path / "dev", "me", "0.25", package="edits")
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": _quieter("0.5")})
    _lock(project, [_link(linked)])
    argv, said = _heard(QUERY.format(call="me.quieter"), _packages(project))
    assert "volume=volume=0.5" in " ".join(argv)
    # The link lost the claim, so nothing resolves in it and nothing warns.
    assert said == []


def test_the_local_lockfile_wins_over_the_global_one(store_home: Path, tmp_path: Path) -> None:
    _lock(store_home, [_installed(_library(tmp_path / "far", "tracks", "0.25"))])
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    _lock(project, [_installed(_library(tmp_path / "near", "tracks", "0.5"))])
    argv, said = _heard(QUERY.format(call="tracks.quieter"), _packages(project))
    assert "volume=volume=0.5" in " ".join(argv)
    assert said == []


def test_all_three_layers_answer_in_order(store_home: Path, tmp_path: Path) -> None:
    _lock(
        store_home,
        [
            _installed(_library(tmp_path / "g-shadowed", "me", "0.1", package="edits")),
            _installed(_library(tmp_path / "g-tracks", "tracks", "0.2")),
            _installed(_library(tmp_path / "g-only", "far", "0.3")),
        ],
    )
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": _quieter("0.9")})
    _lock(project, [_installed(_library(tmp_path / "l-tracks", "tracks", "0.8"))])
    packages = _packages(project)
    assert packages.names() == ("far/lib", "me/edits", "tracks/lib")
    assert packages.namespaces() == ("far", "me", "tracks")
    assert [packages.packages[name].layer for name in packages.names()] == [
        "global",
        "project",
        "local",
    ]
    graph = " ".join(
        _heard(
            "COPY (SELECT me.quieter(f.audio[1]), tracks.quieter(f.audio[2]), "
            "far.quieter(f.audio[3]) FROM input('film.mkv') f) TO 'out.mkv'",
            packages,
        )[0]
    )
    assert "volume=volume=0.9" in graph
    assert "volume=volume=0.8" in graph
    assert "volume=volume=0.3" in graph


def test_landing_on_the_global_layer_inside_a_project_warns(
    store_home: Path, tmp_path: Path
) -> None:
    _lock(store_home, [_installed(_library(tmp_path / "far", "tracks", "0.5"))])
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    _compiled, said = _heard(QUERY.format(call="tracks.quieter"), _packages(project))
    assert _codes(said) == [WarningCode.GLOBAL_PACKAGE]
    assert said[0].package == "tracks/lib"
    assert "tracks/lib" in (said[0].hint or "")


def test_a_global_package_outside_a_project_has_nothing_to_warn_about(
    store_home: Path, tmp_path: Path
) -> None:
    _lock(store_home, [_installed(_library(tmp_path / "far", "tracks", "0.5"))])
    bare = tmp_path / "bare"
    bare.mkdir()
    packages = discover(bare)
    assert packages is not None and not packages.in_project
    argv, said = _heard(QUERY.format(call="tracks.quieter"), packages)
    assert "volume=volume=0.5" in " ".join(argv)
    assert said == []


def test_a_global_link_warns_about_both(store_home: Path, tmp_path: Path) -> None:
    linked = _library(tmp_path / "dev", "tracks", "0.5")
    _lock(store_home, [_link(linked)])
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    _compiled, said = _heard(QUERY.format(call="tracks.quieter"), _packages(project))
    assert set(_codes(said)) == {WarningCode.LINKED_PACKAGE, WarningCode.GLOBAL_PACKAGE}


def test_a_package_nothing_calls_is_never_warned_about(store_home: Path, tmp_path: Path) -> None:
    _lock(store_home, [_installed(_library(tmp_path / "far", "tracks", "0.5"))])
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": _quieter("0.5")})
    _compiled, said = _heard(QUERY.format(call="me.quieter"), _packages(project))
    assert said == []


def test_a_lockfile_alone_is_a_project(store_home: Path, tmp_path: Path) -> None:
    _lock(store_home, [_installed(_library(tmp_path / "far", "tracks", "0.5"))])
    work = tmp_path / "work"
    _lock(work, [_installed(_library(tmp_path / "near", "own", "0.5"))])
    packages = discover(work)
    assert packages is not None and packages.in_project
    assert packages.names() == ("own/lib", "tracks/lib")


def test_nothing_anywhere_is_still_no_project(store_home: Path, tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    assert discover(bare) is None


# ---------------------------------------------------------------------------
# aliases resolve through the set, and stay disjoint from namespaces
# ---------------------------------------------------------------------------


def test_the_set_carries_the_projects_aliases(store_home: Path, tmp_path: Path) -> None:
    entry = _installed(_library(tmp_path / "built", "broadcast", "0.5", package="tracks"))
    project = tmp_path / "work"
    _project(
        project,
        files={"src/own.sql": NORMALIZE},
        manifest={"dependencies": {"t": "broadcast/tracks@^1.0.0"}},
    )
    _lock(project, [entry])
    packages = _packages(project)
    assert packages.aliases == {"t": "broadcast/tracks"}
    aliased = packages.aliased("t")
    assert aliased is not None and aliased.name == "broadcast/tracks"
    assert packages.aliased("nothing") is None
    assert packages.find("broadcast", "tracks") is aliased


def test_an_alias_declared_but_not_installed_resolves_to_nothing(tmp_path: Path) -> None:
    _project(
        tmp_path,
        files={"src/own.sql": NORMALIZE},
        manifest={"dependencies": {"t": "broadcast/tracks@^1.0.0"}},
    )
    packages = _packages(tmp_path)
    assert packages.aliases == {"t": "broadcast/tracks"}
    assert packages.aliased("t") is None


def test_an_alias_equal_to_an_installed_namespace_is_refused(
    store_home: Path, tmp_path: Path
) -> None:
    entry = _installed(_library(tmp_path / "built", "broadcast", "0.5", package="tracks"))
    project = tmp_path / "work"
    _project(
        project,
        files={"src/own.sql": NORMALIZE},
        manifest={"dependencies": {"broadcast": "broadcast/tracks@^1.0.0"}},
    )
    _lock(project, [entry])
    error = _refuses(project, "alias 'broadcast' is also the namespace")
    assert "broadcast/tracks" in error.message


# ---------------------------------------------------------------------------
# a malformed lockfile is a typed rejection, like a malformed manifest
# ---------------------------------------------------------------------------


def test_a_lockfile_that_is_not_json_is_anchored(tmp_path: Path) -> None:
    _project(tmp_path, files={"src/own.sql": NORMALIZE})
    _lock(tmp_path, [], text="{ nope\n")
    error = _refuses(tmp_path, "sqlmpeg.lock")
    assert error.code is ErrorCode.UNSUPPORTED_SQL
    assert error.line == 1


def test_a_lockfile_from_another_format_version_is_refused(tmp_path: Path) -> None:
    _project(tmp_path, files={"src/own.sql": NORMALIZE})
    _lock(tmp_path, [], text='{"format_version": 1, "reproducible": true, "packages": []}\n')
    _refuses(tmp_path, "lockfile format 1")


@pytest.mark.parametrize("missing", ["format_version", "reproducible", "packages"])
def test_every_lockfile_key_is_required(tmp_path: Path, missing: str) -> None:
    _project(tmp_path, files={"src/own.sql": NORMALIZE})
    written = {"format_version": LOCK_FORMAT_VERSION, "reproducible": True, "packages": []}
    del written[missing]
    _lock(tmp_path, [], text=json.dumps(written))
    _refuses(tmp_path, f'is missing "{missing}"')


def test_a_lockfile_claiming_to_be_reproducible_while_linking_is_refused(tmp_path: Path) -> None:
    linked = _library(tmp_path / "dev", "tracks", "0.5")
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    _lock(project, [_link(linked)], reproducible=True)
    _refuses(project, "claims to be reproducible")


def test_a_lockfile_says_in_its_own_text_why_it_is_not_reproducible(tmp_path: Path) -> None:
    linked = _library(tmp_path / "dev", "tracks", "0.5")
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    written = _lock(project, [_link(linked)]).read_text(encoding="utf-8")
    assert '"reproducible": false' in written
    assert "not_reproducible_because" in written


def test_two_entries_naming_one_package_are_refused(tmp_path: Path) -> None:
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    entry = {
        "kind": "registry",
        "name": "broadcast/tracks",
        "version": "1.0.0",
        "sha256": "a" * 64,
        "store": store.entry_path("a" * 64),
    }
    _lock(project, [entry, dict(entry)])
    _refuses(project, "two entries name package 'broadcast/tracks'")


def test_a_registry_entry_and_a_link_naming_one_package_are_refused(tmp_path: Path) -> None:
    linked = _library(tmp_path / "dev", "broadcast", "0.5", package="tracks")
    entry = _installed(_library(tmp_path / "built", "broadcast", "0.25", package="tracks"))
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    _lock(project, [entry, _link(linked)])
    _refuses(project, "two entries name package 'broadcast/tracks'")


def test_an_entry_of_no_known_kind_is_refused(tmp_path: Path) -> None:
    _project(tmp_path, files={"src/own.sql": NORMALIZE})
    _lock(tmp_path, [{"path": "../dev"}])
    _refuses(tmp_path, 'a package entry has no "kind"')


def test_an_entry_missing_a_key_names_it(tmp_path: Path) -> None:
    _project(tmp_path, files={"src/own.sql": NORMALIZE})
    _lock(tmp_path, [{"kind": "link"}])
    _refuses(tmp_path, 'a link entry is missing "path"')


def test_an_unknown_entry_key_gets_a_did_you_mean(tmp_path: Path) -> None:
    _project(tmp_path, files={"src/own.sql": NORMALIZE})
    _lock(tmp_path, [{"kind": "link", "path": "../dev", "pth": "x"}])
    error = _refuses(tmp_path, "unknown key 'pth'")
    assert "did you mean 'path'?" in (error.hint or "")


def test_a_namespace_key_is_no_longer_part_of_an_entry(tmp_path: Path) -> None:
    _project(tmp_path, files={"src/own.sql": NORMALIZE})
    _lock(tmp_path, [{"kind": "link", "namespace": "tracks", "path": "../dev"}])
    _refuses(tmp_path, "unknown key 'namespace'")


def test_a_reserved_name_in_a_lockfile_is_refused(tmp_path: Path) -> None:
    _project(tmp_path, files={"src/own.sql": NORMALIZE})
    _lock(
        tmp_path,
        [
            {
                "kind": "registry",
                "name": "ffmpeg/tracks",
                "version": "1.0.0",
                "sha256": "a" * 64,
                "store": store.entry_path("a" * 64),
            }
        ],
    )
    _refuses(tmp_path, "reserved namespace")


def test_a_rejection_points_at_the_entry_it_is_about(tmp_path: Path) -> None:
    first = _library(tmp_path / "one", "good", "0.5")
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    path = _lock(project, [_link(first), {"kind": "link", "path": "../later", "pth": "x"}])
    error = _refuses(project, "unknown key 'pth'")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert error.line is not None
    assert '"../later"' in lines[error.line - 1]


# ---------------------------------------------------------------------------
# the diagnostic channel reaches the CLI's stderr and the MCP tool result
# ---------------------------------------------------------------------------


def test_the_cli_prints_the_warning_on_stderr_and_the_command_on_stdout(
    store_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _lock(store_home, [_installed(_library(tmp_path / "far", "tracks", "0.5"))])
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    monkeypatch.chdir(project)
    assert cli.main(["compile", QUERY.format(call="tracks.quieter")]) == 0
    captured = capsys.readouterr()
    assert "volume=volume=0.5" in captured.out
    assert "warning:" not in captured.out
    assert "warning: package 'tracks/lib' was resolved from the machine-wide" in captured.err
    assert "hint:" in captured.err


def test_the_cli_says_it_once_though_it_compiles_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    linked = _library(tmp_path / "dev", "tracks", "0.5")
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    _lock(project, [_link(linked)])
    monkeypatch.chdir(project)
    # A bare SELECT: `compile` refuses it, then tries the table fallback, so
    # the same text compiles twice in one command.
    assert cli.main(["compile", "SELECT tracks.quieter(f.audio[1]) FROM input('f.mkv') f"]) == 2
    assert capsys.readouterr().err.count("warning:") == 1


def test_validate_keeps_the_warning_off_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    linked = _library(tmp_path / "dev", "tracks", "0.5")
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    _lock(project, [_link(linked)])
    monkeypatch.chdir(project)
    assert cli.main(["validate", QUERY.format(call="tracks.quieter")]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "warning: package 'tracks/lib' is linked to" in captured.err


def test_the_mcp_compile_tool_returns_the_warnings(store_home: Path, tmp_path: Path) -> None:
    _lock(store_home, [_installed(_library(tmp_path / "far", "tracks", "0.5"))])
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    result = mcp_tools.compile_query(QUERY.format(call="tracks.quieter"), None, str(project))
    assert [w["code"] for w in result["warnings"]] == [WarningCode.GLOBAL_PACKAGE.value]
    assert result["warnings"][0]["package"] == "tracks/lib"


def test_the_mcp_validate_tool_answers_with_warnings_and_no_code(tmp_path: Path) -> None:
    linked = _library(tmp_path / "dev", "tracks", "0.5")
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    _lock(project, [_link(linked)])
    result = mcp_tools.validate_query(QUERY.format(call="tracks.quieter"), None, str(project))
    assert "code" not in result
    assert [w["code"] for w in result["warnings"]] == [WarningCode.LINKED_PACKAGE.value]


def test_the_mcp_tools_stay_silent_with_nothing_to_say(tmp_path: Path) -> None:
    _project(tmp_path, files={"src/own.sql": _quieter("0.5")})
    query = QUERY.format(call="me.quieter")
    assert mcp_tools.validate_query(query, None, str(tmp_path)) == {}
    assert mcp_tools.compile_query(query, None, str(tmp_path))["warnings"] == []


# ---------------------------------------------------------------------------
# `sqlmpeg list`: what the project and its dependencies provide
# ---------------------------------------------------------------------------


def _list(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *flags: str,
) -> tuple[int, str, str]:
    """Run `sqlmpeg list` with `root` as the working directory."""
    monkeypatch.chdir(root)
    code = cli.main(["list", *flags])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_list_prints_the_exports_programs_and_aliases_a_project_provides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _project(
        tmp_path,
        files={"src/tracks.sql": QUIETER + PICK, "queries/split.sql": PROGRAM},
        manifest={**_BINS, "dependencies": {"tracks": "broadcast/tracks@^1.2.0"}},
    )
    code, out, _err = _list(tmp_path, monkeypatch, capsys)
    assert code == 0
    assert "quieter(track audio_stream, factor number) | audio_stream" in out
    assert "pick(path text)" in out and "TABLE(track audio_stream)" in out
    assert "split-chapters" in out
    assert "source (input media path), dest (output path)" in out
    assert "queries/split.sql" in out
    assert "me/edits | 0.1.0   | project | false" in out
    assert "broadcast/tracks@^1.2.0" in out


def test_list_outside_a_project_prints_empty_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bare = tmp_path / "no_project"
    bare.mkdir()
    code, out, _err = _list(bare, monkeypatch, capsys)
    assert code == 0
    assert out.count("(0 rows)") == 4


def test_list_as_json_carries_the_signatures_and_the_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _project(
        tmp_path,
        files={"src/tracks.sql": QUIETER, "queries/split.sql": PROGRAM},
        manifest={**_BINS, "dependencies": {"tracks": "broadcast/tracks@^1.2.0"}},
    )
    code, out, _err = _list(tmp_path, monkeypatch, capsys, "--json")
    assert code == 0
    listed = json.loads(out)["packages"]
    assert [package["name"] for package in listed] == ["me/edits"]
    package = listed[0]
    assert package["layer"] == "project"
    assert package["linked"] is False
    assert package["exports"] == [
        {
            "name": "quieter",
            "params": [
                {"name": "track", "type": "audio_stream"},
                {"name": "factor", "type": "number"},
            ],
            "returns": "audio_stream",
            "file": "src/tracks.sql",
        }
    ]
    assert package["programs"] == [
        {
            "name": "split-chapters",
            "file": "queries/split.sql",
            "variables": [
                {"name": "source", "description": "input media path"},
                {"name": "dest", "description": "output path"},
            ],
        }
    ]
    assert package["aliases"] == [
        {"alias": "tracks", "package": "broadcast/tracks", "range": "^1.2.0"}
    ]


def test_list_names_the_layer_and_marks_a_linked_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    linked = _library(tmp_path / "dev", "tracks", "0.5")
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    _lock(project, [_link(linked)])
    code, out, _err = _list(project, monkeypatch, capsys, "--json")
    assert code == 0
    listed = {package["name"]: package for package in json.loads(out)["packages"]}
    assert listed["me/edits"]["layer"] == "project" and listed["me/edits"]["linked"] is False
    assert listed["tracks/lib"]["layer"] == "local" and listed["tracks/lib"]["linked"] is True
    assert [f["name"] for f in listed["tracks/lib"]["exports"]] == ["quieter"]


def test_list_reports_a_malformed_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _project(tmp_path, files={"src/tracks.sql": QUIETER}, text="{ nope\n")
    code, out, err = _list(tmp_path, monkeypatch, capsys)
    assert code == 1
    assert out == ""
    assert "sqlmpeg.json" in err


def test_list_reports_a_lib_file_that_is_not_a_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _project(tmp_path, files={"src/tracks.sql": QUIETER + "SELECT 1;"})
    code, _out, err = _list(tmp_path, monkeypatch, capsys)
    assert code == 1
    assert "is not a CREATE FUNCTION" in err


def test_list_takes_no_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as caught:
        cli.main(["list", "SELECT 1"])
    assert caught.value.code == 2


# ---------------------------------------------------------------------------
# writing the two files
# ---------------------------------------------------------------------------


def _registry_entry(name: str = "broadcast/tracks") -> RegistryEntry:
    sha256 = "a" * 64
    return RegistryEntry(
        name=name,
        version="1.2.0",
        sha256=sha256,
        store=store.entry_path(sha256),
    )


def test_a_written_lockfile_reads_back_as_what_was_written(tmp_path: Path) -> None:
    path = tmp_path / "sqlmpeg.lock"
    entries = (_registry_entry(), LinkEntry(path="../my-lib"))
    write_lockfile(path, entries)
    assert read_lockfile(path).entries == entries


def test_writing_a_lockfile_twice_writes_the_same_bytes(tmp_path: Path) -> None:
    path = tmp_path / "sqlmpeg.lock"
    entries = (_registry_entry(), LinkEntry(path="../my-lib"))
    write_lockfile(path, entries)
    first = path.read_bytes()
    write_lockfile(path, read_lockfile(path).entries)
    assert path.read_bytes() == first
    assert read_lockfile(path).entries == entries


def test_a_written_lockfile_is_lf_only(tmp_path: Path) -> None:
    path = tmp_path / "sqlmpeg.lock"
    write_lockfile(path, (_registry_entry(),))
    data = path.read_bytes()
    assert b"\r" not in data
    assert data.endswith(b"\n")


def test_a_lockfile_of_registry_entries_claims_to_be_reproducible(tmp_path: Path) -> None:
    path = tmp_path / "sqlmpeg.lock"
    write_lockfile(path, (_registry_entry(),))
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["format_version"] == LOCK_FORMAT_VERSION
    assert written["reproducible"] is True
    assert "not_reproducible_because" not in written
    assert read_lockfile(path).reproducible is True


def test_a_lockfile_holding_a_link_says_it_is_not_reproducible_and_why(tmp_path: Path) -> None:
    path = tmp_path / "sqlmpeg.lock"
    write_lockfile(path, (_registry_entry(), LinkEntry(path="../my-lib")))
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["reproducible"] is False
    assert "linked to a working directory" in written["not_reproducible_because"]
    assert read_lockfile(path).reproducible is False


def test_an_empty_lockfile_reads_back(tmp_path: Path) -> None:
    path = tmp_path / "sqlmpeg.lock"
    write_lockfile(path, ())
    lock = read_lockfile(path)
    assert lock.entries == () and lock.reproducible is True


def test_the_written_entry_order_is_the_order_given(tmp_path: Path) -> None:
    path = tmp_path / "sqlmpeg.lock"
    entries = (_registry_entry("zulu/lib"), _registry_entry("alpha/lib"))
    write_lockfile(path, entries)
    assert read_lockfile(path).entries == entries


def test_with_entry_replaces_and_without_entry_removes() -> None:
    held = (_registry_entry("broadcast/tracks"), _registry_entry("far/other"))
    link = LinkEntry(path="../dev")
    assert with_entry(held, link, held[0]) == (link, held[1])
    appended = with_entry(held, _registry_entry("new/lib"))
    assert appended[-1] == _registry_entry("new/lib")
    assert without_entry(held, held[0]) == (held[1],)
    assert without_entry(held, link) == held


def test_a_written_manifest_reads_back_as_the_package_it_declares(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.sql").write_text(QUIETER, encoding="utf-8")
    (tmp_path / "queries").mkdir()
    (tmp_path / "queries" / "split.sql").write_text(PROGRAM, encoding="utf-8")
    path = tmp_path / "sqlmpeg.json"
    write_manifest(
        path,
        name="me/edits",
        version="0.1.0",
        description="what it is",
        libs={"quieter": "src/lib.sql"},
        bin="queries/split.sql",
        bins={"split-chapters": "queries/split.sql"},
        dependencies={"tracks": "broadcast/tracks@^1.2.0"},
    )
    package = read_manifest(path)
    assert package.name == "me/edits" and package.version == "0.1.0"
    assert list(package.programs) == ["edits", "split-chapters"]
    assert package.aliases == {"tracks": Dependency(name="broadcast/tracks", range="^1.2.0")}
    assert b"\r" not in path.read_bytes()


def test_a_manifest_leaves_out_what_it_was_not_given(tmp_path: Path) -> None:
    path = tmp_path / "sqlmpeg.json"
    write_manifest(path, name="me/edits", version="0.1.0")
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "name": "me/edits",
        "version": "0.1.0",
    }
    assert dict(read_manifest(path).exports) == {}


def test_a_write_into_a_directory_that_is_a_file_is_a_rejection(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory\n", encoding="utf-8")
    with pytest.raises(SqlmpegError) as caught:
        write_lockfile(blocked / "sqlmpeg.lock", ())
    assert "could not be written" in caught.value.message


# ---------------------------------------------------------------------------
# `sqlmpeg init`
# ---------------------------------------------------------------------------


def _run(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *argv: str,
) -> tuple[int, str, str]:
    """Run the CLI with `root` as the working directory."""
    monkeypatch.chdir(root)
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_init_writes_a_project_that_reads_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "my-edits"
    root.mkdir()
    code, out, _err = _run(root, monkeypatch, capsys, "init", "--namespace", "me")
    assert code == 0
    assert "sqlmpeg.json" in out
    assert "--namespace" in out
    package = read_manifest(root / "sqlmpeg.json")
    assert package.name == "me/my_edits" and package.version == "0.1.0"
    assert list(package.programs) == ["resize"]
    assert read_lockfile(root / "sqlmpeg.lock").entries == ()
    assert "-- variables:" in (root / "queries" / "resize.sql").read_text(encoding="utf-8")


def test_init_then_list_then_run_the_starter_program(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "my-edits"
    root.mkdir()
    assert _run(root, monkeypatch, capsys, "init", "--namespace", "me")[0] == 0

    code, out, _err = _run(root, monkeypatch, capsys, "list")
    assert code == 0
    assert "resize" in out and "me/my_edits" in out

    code, out, _err = _run(
        root, monkeypatch, capsys, "compile", "resize", "-v", "source=in.mp4", "-v", "dest=out.mp4"
    )
    assert code == 0
    assert out.startswith("ffmpeg -i in.mp4 ")
    assert "scale=width=-2:height=720" in out


def test_init_takes_the_whole_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "whatever"
    root.mkdir()
    code, out, _err = _run(root, monkeypatch, capsys, "init", "--name", "broadcast/tracks")
    assert code == 0
    assert "--name" in out
    assert read_manifest(root / "sqlmpeg.json").name == "broadcast/tracks"


def test_init_derives_the_namespace_from_the_git_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    root = tmp_path / "tracks"
    root.mkdir()
    subprocess.run([git, "init", "-q"], cwd=root, check=True)
    subprocess.run(
        [git, "remote", "add", "origin", "https://github.com/broadcast/tracks.git"],
        cwd=root,
        check=True,
    )
    code, out, _err = _run(root, monkeypatch, capsys, "init")
    assert code == 0
    assert "git remote" in out
    assert read_manifest(root / "sqlmpeg.json").name == "broadcast/tracks"


def test_init_without_a_derivable_namespace_requires_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "work"
    root.mkdir()
    code, _out, err = _run(root, monkeypatch, capsys, "init")
    assert code == 1
    assert "--namespace" in err
    assert not (root / "sqlmpeg.json").exists()
    assert not (root / "queries").exists()


def test_init_refuses_to_overwrite_a_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _project(tmp_path, files={"src/own.sql": NORMALIZE})
    before = (tmp_path / "sqlmpeg.json").read_bytes()
    code, _out, err = _run(tmp_path, monkeypatch, capsys, "init", "--namespace", "me")
    assert code == 1
    assert "sqlmpeg.json already exists" in err
    assert (tmp_path / "sqlmpeg.json").read_bytes() == before


def test_init_refuses_to_overwrite_a_lockfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _lock(tmp_path, [])
    code, _out, err = _run(tmp_path, monkeypatch, capsys, "init", "--namespace", "me")
    assert code == 1
    assert "sqlmpeg.lock already exists" in err
    assert not (tmp_path / "sqlmpeg.json").exists()


@pytest.mark.parametrize("name", ["9lives", "..."])
def test_init_refuses_a_name_no_package_segment_comes_out_of(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    root = tmp_path / "work"
    root.mkdir()
    code, _out, err = _run(
        root, monkeypatch, capsys, "init", "--name", name, "--namespace", "me"
    )
    assert code == 1
    assert "gives no package segment" in err
    assert not (root / "sqlmpeg.json").exists()
    assert not (root / "queries").exists()


def test_init_refuses_a_namespace_that_is_not_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "work"
    root.mkdir()
    code, _out, err = _run(root, monkeypatch, capsys, "init", "--namespace", "Not One")
    assert code == 1
    assert "--namespace gives no namespace" in err


def test_init_refuses_a_reserved_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "work"
    root.mkdir()
    code, _out, err = _run(root, monkeypatch, capsys, "init", "--namespace", "ffmpeg")
    assert code == 1
    assert "reserved" in err


# ---------------------------------------------------------------------------
# `sqlmpeg link` and `sqlmpeg unlink`
# ---------------------------------------------------------------------------


def test_link_records_the_directory_and_names_the_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _library(tmp_path / "dev", "tracks", "0.5")
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    _lock(project, [])
    code, out, _err = _run(project, monkeypatch, capsys, "link", "../dev")
    assert code == 0
    assert "linked tracks/lib -> ../dev" in out
    lock = read_lockfile(project / "sqlmpeg.lock")
    assert lock.entries == (LinkEntry(path="../dev"),)
    assert lock.reproducible is False


def test_a_linked_package_is_callable_right_after_linking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _library(tmp_path / "dev", "tracks", "0.5")
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    _lock(project, [])
    assert _run(project, monkeypatch, capsys, "link", "../dev")[0] == 0
    code, out, err = _run(
        project, monkeypatch, capsys, "compile", QUERY.format(call="tracks.quieter")
    )
    assert code == 0
    assert "volume=volume=0.5" in out
    assert "warning: package 'tracks/lib' is linked to" in err


def test_link_replaces_what_pinned_the_package(
    store_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    _lock(project, [_installed(_library(tmp_path / "far", "tracks", "0.5"))])
    _library(tmp_path / "dev", "tracks", "0.9")
    code, out, _err = _run(project, monkeypatch, capsys, "link", "../dev")
    assert code == 0
    assert "replacing the installed tracks/lib 1.0.0" in out
    assert read_lockfile(project / "sqlmpeg.lock").entries == (LinkEntry(path="../dev"),)


def test_link_outside_a_project_names_both_ways_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _library(tmp_path / "dev", "tracks", "0.5")
    bare = tmp_path / "elsewhere"
    bare.mkdir()
    code, _out, err = _run(bare, monkeypatch, capsys, "link", "../dev")
    assert code == 2
    assert "sqlmpeg init" in err and "link -g" in err
    assert not (bare / "sqlmpeg.lock").exists()


def test_link_writes_the_machine_wide_lockfile(
    store_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    linked = _library(tmp_path / "dev", "tracks", "0.5")
    bare = tmp_path / "elsewhere"
    bare.mkdir()
    code, _out, _err = _run(bare, monkeypatch, capsys, "link", "-g", str(linked))
    assert code == 0
    lock = read_lockfile(store.global_lock_path())
    # Absolute, since the machine-wide lockfile lives under the cache directory.
    assert lock.entries == (LinkEntry(path=str(linked.resolve())),)


def test_link_refuses_a_directory_holding_no_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "dev").mkdir()
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    _lock(project, [])
    code, _out, err = _run(project, monkeypatch, capsys, "link", "../dev")
    assert code == 1
    assert "holds no sqlmpeg.json" in err
    assert read_lockfile(project / "sqlmpeg.lock").entries == ()


def test_unlink_removes_the_entry_by_package_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    linked = _library(tmp_path / "dev", "tracks", "0.5")
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    _lock(project, [_link(linked)])
    code, out, _err = _run(project, monkeypatch, capsys, "unlink", "tracks/lib")
    assert code == 0
    assert "unlinked 'tracks/lib'" in out
    lock = read_lockfile(project / "sqlmpeg.lock")
    assert lock.entries == () and lock.reproducible is True


def test_unlink_removes_a_dead_link_by_its_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A link whose directory lost its manifest has no name, and still goes away."""
    linked = _library(tmp_path / "dev", "tracks", "0.5")
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    _lock(project, [_link(linked)])
    (linked / "sqlmpeg.json").unlink()
    code, _out, _err = _run(project, monkeypatch, capsys, "unlink", str(linked))
    assert code == 0
    assert read_lockfile(project / "sqlmpeg.lock").entries == ()


def test_unlink_names_what_is_linked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    linked = _library(tmp_path / "dev", "tracks", "0.5")
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    _lock(project, [_link(linked)])
    code, _out, err = _run(project, monkeypatch, capsys, "unlink", "nope/nope")
    assert code == 1
    assert "nothing links 'nope/nope'" in err
    assert "hint: linked: tracks/lib" in err


def test_unlink_says_a_package_is_installed_rather_than_linked(
    store_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "work"
    _project(project, files={"src/own.sql": NORMALIZE})
    _lock(project, [_installed(_library(tmp_path / "far", "tracks", "0.5"))])
    code, _out, err = _run(project, monkeypatch, capsys, "unlink", "tracks/lib")
    assert code == 1
    assert "it is installed, not linked" in err
    assert len(read_lockfile(project / "sqlmpeg.lock").entries) == 1


def test_unlink_outside_a_project_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bare = tmp_path / "elsewhere"
    bare.mkdir()
    code, _out, err = _run(bare, monkeypatch, capsys, "unlink", "tracks/lib")
    assert code == 2
    assert "unlink -g" in err
    assert not (bare / "sqlmpeg.lock").exists()


# ---------------------------------------------------------------------------
# running a program by name
# ---------------------------------------------------------------------------

_MEDIA_QUERY = "COPY (SELECT scale(f.video[1], 640, 480) FROM input('x.mp4') f) TO 'out.mp4'"


@pytest.mark.parametrize("command", ["compile", "explain", "validate", "run"])
def test_every_subcommand_taking_a_query_takes_a_program_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    _project(
        tmp_path,
        files={"src/own.sql": NORMALIZE, "queries/split.sql": PROGRAM},
        manifest=_BINS,
    )
    if command == "run":
        # The default tier executes no ffmpeg: reaching the check for one is
        # already proof the name resolved and the program compiled.
        monkeypatch.setattr(cli.binaries, "ffmpeg_path", lambda: None)
    code, _out, err = _run(
        tmp_path,
        monkeypatch,
        capsys,
        command,
        "split-chapters",
        "-v",
        "source=in.mkv",
        "-v",
        "dest=out.mkv",
    )
    if command == "run":
        assert code == 1 and "ffmpeg not found" in err
    else:
        assert code == 0, err


def test_the_default_program_is_reached_as_the_package_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _project(
        tmp_path,
        files={"queries/split.sql": PROGRAM},
        manifest={"bin": "queries/split.sql"},
    )
    code, _out, err = _run(
        tmp_path,
        monkeypatch,
        capsys,
        "validate",
        "me.edits",
        "-v",
        "source=in.mkv",
        "-v",
        "dest=out.mkv",
    )
    assert code == 0, err


def test_a_program_name_is_not_looked_up_when_the_text_is_sql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Text starting with one of the four words is SQL whatever else it might
    # have matched.
    _project(
        tmp_path,
        files={"src/own.sql": NORMALIZE, "queries/split.sql": PROGRAM},
        manifest=_BINS,
    )
    code, out, _err = _run(tmp_path, monkeypatch, capsys, "compile", _MEDIA_QUERY)
    assert code == 0
    assert "scale=" in out


def test_a_leading_comment_does_not_hide_that_the_text_is_sql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _project(
        tmp_path,
        files={"src/own.sql": NORMALIZE, "queries/split.sql": PROGRAM},
        manifest=_BINS,
    )
    code, out, _err = _run(
        tmp_path, monkeypatch, capsys, "compile", f"-- a header\n/* and a block */\n{_MEDIA_QUERY}"
    )
    assert code == 0
    assert "scale=" in out


def test_a_bare_name_two_packages_ship_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    linked = tmp_path / "dev"
    _library(linked, "tracks", "0.5")
    (linked / "queries").mkdir()
    (linked / "queries" / "split.sql").write_text(PROGRAM, encoding="utf-8")
    written = json.loads((linked / "sqlmpeg.json").read_text(encoding="utf-8"))
    written["bins"] = {"split-chapters": "queries/split.sql"}
    (linked / "sqlmpeg.json").write_text(json.dumps(written) + "\n", encoding="utf-8")

    project = tmp_path / "work"
    _project(
        project,
        files={"src/own.sql": NORMALIZE, "queries/split.sql": PROGRAM},
        manifest=_BINS,
    )
    _lock(project, [_link(linked)])
    code, _out, err = _run(project, monkeypatch, capsys, "validate", "split-chapters")
    assert code == 1
    assert "more than one package ships a program named 'split-chapters'" in err
    assert "me.split-chapters, tracks.split-chapters" in err

    code, _out, err = _run(
        project,
        monkeypatch,
        capsys,
        "validate",
        "tracks.split-chapters",
        "-v",
        "source=in.mkv",
        "-v",
        "dest=out.mkv",
    )
    assert code == 0


def test_an_undefined_variable_names_what_the_program_declares(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _project(
        tmp_path,
        files={"src/own.sql": NORMALIZE, "queries/split.sql": PROGRAM},
        manifest=_BINS,
    )
    code, _out, err = _run(
        tmp_path, monkeypatch, capsys, "validate", "split-chapters", "-v", "source=in.mkv"
    )
    assert code == 1
    assert "':dest' was not set" in err
    assert "'split-chapters' declares source, dest" in err


def test_a_name_matching_nothing_fails_as_sql_and_names_the_programs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _project(
        tmp_path,
        files={"src/own.sql": NORMALIZE, "queries/split.sql": PROGRAM},
        manifest=_BINS,
    )
    code, _out, err = _run(tmp_path, monkeypatch, capsys, "compile", "split-chapter")
    assert code == 1
    assert "hint: installed programs: me.split-chapters" in err


def test_a_failing_query_is_not_offered_a_program(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _project(
        tmp_path,
        files={"src/own.sql": NORMALIZE, "queries/split.sql": PROGRAM},
        manifest=_BINS,
    )
    code, _out, err = _run(tmp_path, monkeypatch, capsys, "compile", "SELECT nope(1)")
    assert code == 1
    assert "installed programs" not in err


BAD_PROGRAM = "SELECT nope(1)\n"


def test_a_program_that_resolved_is_not_offered_the_other_programs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The rejection came from inside the program; listing the others is noise."""
    _project(
        tmp_path,
        files={"src/own.sql": NORMALIZE, "queries/split.sql": BAD_PROGRAM},
        manifest=_BINS,
    )
    code, _out, err = _run(tmp_path, monkeypatch, capsys, "compile", "split-chapters")
    assert code == 1
    assert "installed programs" not in err


def test_a_program_named_for_a_statement_word_is_refused(tmp_path: Path) -> None:
    _project(
        tmp_path,
        files={"src/own.sql": NORMALIZE, "queries/split.sql": PROGRAM},
        manifest={"bins": {"select": "queries/split.sql"}},
    )
    error = _refuses(tmp_path, "program name 'select' is a word a query begins with")
    assert "rename it" in (error.hint or "")


# ---------------------------------------------------------------------------
# `sqlmpeg publish`
# ---------------------------------------------------------------------------


def test_publish_says_it_is_not_open_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(tmp_path, monkeypatch, capsys, "publish")
    assert code == 1
    assert out == ""
    assert "publishing is not open yet" in err
    assert "pull request to the registry repository" in err
