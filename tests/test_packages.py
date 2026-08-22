"""Tests for the package registry client: the catalogue, ``search`` and ``install``.

No test here touches the network. A fixture registry is a directory under
``tmp_path`` holding the exact files a published one serves -- ``index.json``,
``p/<owner>/<name>.json`` and ``archives/<sha256>`` -- and ``SQLMPEG_REGISTRY``
points the client at it. That is the same seam a private registry behind any
static host uses, so what is exercised is the real fetch path and not a stub.

The store and the index cache live under this test's own directory (the
``store_home`` fixture), so nothing leaks between tests or into a developer's
home.

The headline check is :func:`test_init_then_install_then_a_query_calling_it`:
the whole loop, from an empty directory to a compiled ffmpeg command that
carries the installed package's own function body.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from sqlmpeg import cli, packages, store
from sqlmpeg.errors import SqlmpegError
from sqlmpeg.mcp import tools as mcp_tools
from sqlmpeg.project import RegistryEntry, read_lockfile, read_manifest

QUERY = "COPY (SELECT {call}(f.audio[1]) FROM input('film.mkv') f) TO 'out.mkv'"


def _quieter(factor: str) -> str:
    """A one-argument ``quieter`` whose factor shows up in the compiled graph."""
    return (
        "CREATE FUNCTION quieter(track audio_stream) RETURNS audio_stream AS $$\n"
        f"  SELECT volume(track, {factor})\n"
        "$$ LANGUAGE sql;\n"
    )


def _package(
    root: Path,
    *,
    name: str = "broadcast/tracks",
    namespace: str = "tracks",
    version: str = "1.0.0",
    factor: str = "0.5",
    description: str = "audio track tools",
) -> Path:
    """A package directory: a manifest, and one export defining ``quieter``."""
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "lib.sql").write_text(_quieter(factor), encoding="utf-8")
    (root / "sqlmpeg.json").write_text(
        json.dumps(
            {
                "name": name,
                "version": version,
                "namespace": namespace,
                "description": description,
                "exports": ["src/*.sql"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return root


def _read_json(path: Path) -> dict[str, object]:
    return dict(json.loads(path.read_text(encoding="utf-8")))


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _publish(
    registry: Path,
    source: Path,
    *,
    functions: tuple[str, ...] = ("quieter",),
    programs: tuple[str, ...] = (),
) -> str:
    """Publish `source` into the fixture registry the way its CI would.

    Writes the three files a client reads -- the archive, the package's detail
    and the catalogue entry -- and returns the archive's digest.
    """
    package = read_manifest(source / "sqlmpeg.json")
    archive = store.pack(source)
    sha256 = hashlib.sha256(archive).hexdigest()
    (registry / "archives").mkdir(parents=True, exist_ok=True)
    (registry / "archives" / sha256).write_bytes(archive)

    detail = registry / "p" / f"{package.name}.json"
    held: dict[str, object] = (
        _read_json(detail)
        if detail.is_file()
        else {"format_version": 1, "name": package.name, "versions": []}
    )
    versions = list(held["versions"]) if isinstance(held.get("versions"), list) else []
    versions.append({"version": package.version, "sha256": sha256, "size": len(archive)})
    held["versions"] = versions
    _write_json(detail, held)

    index = registry / "index.json"
    catalogue: dict[str, object] = (
        _read_json(index) if index.is_file() else {"format_version": 1, "packages": []}
    )
    listed = list(catalogue["packages"]) if isinstance(catalogue.get("packages"), list) else []
    entry = {
        "name": package.name,
        "version": package.version,
        "namespace": package.namespace,
        "description": _read_json(source / "sqlmpeg.json").get("description", ""),
        "functions": list(functions),
        "programs": list(programs),
    }
    listed = [held for held in listed if held.get("name") != package.name] + [entry]
    catalogue["packages"] = listed
    _write_json(index, catalogue)
    return sha256


@pytest.fixture
def store_home(_isolated_store: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the store, the machine-wide lockfile and the index cache here."""
    home = tmp_path / "cache"
    home.mkdir()
    monkeypatch.setattr(store, "_cache_dir", lambda: home)
    return home


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty fixture registry, and the environment pointed at it."""
    root = tmp_path / "registry"
    root.mkdir()
    monkeypatch.setenv(packages.REGISTRY_ENV, str(root))
    return root


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


def _project(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Path:
    """A project started the way a user starts one."""
    root.mkdir(parents=True, exist_ok=True)
    assert _run(root, monkeypatch, capsys, "init")[0] == 0
    return root


# ---------------------------------------------------------------------------
# the base URL
# ---------------------------------------------------------------------------


def test_the_default_registry_is_read_when_the_environment_names_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(packages.REGISTRY_ENV, raising=False)
    assert packages.base_url() == packages.DEFAULT_REGISTRY
    assert packages.DEFAULT_REGISTRY.startswith("https://")


def test_the_environment_overrides_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(packages.REGISTRY_ENV, "https://packages.example/registry ")
    assert packages.base_url() == "https://packages.example/registry"


def test_a_file_url_reads_the_directory_it_names(
    store_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "registry"
    root.mkdir()
    _publish(root, _package(tmp_path / "built"))
    monkeypatch.setenv(packages.REGISTRY_ENV, root.as_uri())
    assert packages.load_index().names() == ("broadcast/tracks",)


# ---------------------------------------------------------------------------
# the whole loop
# ---------------------------------------------------------------------------


def test_init_then_install_then_a_query_calling_it(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _publish(registry, _package(tmp_path / "built"))
    project = _project(tmp_path / "work", monkeypatch, capsys)

    code, out, _err = _run(project, monkeypatch, capsys, "install", "broadcast/tracks")
    assert code == 0
    assert "installed 'tracks' -> broadcast/tracks 1.0.0" in out

    entries = read_lockfile(project / "sqlmpeg.lock").entries
    assert len(entries) == 1
    entry = entries[0]
    assert isinstance(entry, RegistryEntry)
    assert (entry.namespace, entry.name, entry.version) == ("tracks", "broadcast/tracks", "1.0.0")
    assert entry.store == store.entry_path(entry.sha256)

    code, out, _err = _run(
        project, monkeypatch, capsys, "compile", QUERY.format(call="tracks.quieter")
    )
    assert code == 0
    assert "volume=volume=0.5" in out


def test_install_records_the_dependency_in_the_manifest(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _publish(registry, _package(tmp_path / "built"))
    project = _project(tmp_path / "work", monkeypatch, capsys)
    before = _read_json(project / "sqlmpeg.json")

    code, out, _err = _run(project, monkeypatch, capsys, "install", "broadcast/tracks")
    assert code == 0
    assert "recorded in sqlmpeg.json as a dependency" in out

    after = _read_json(project / "sqlmpeg.json")
    assert after["dependencies"] == {"broadcast/tracks": "1.0.0"}
    # Everything the manifest already said is still there, unchanged.
    assert {key: after[key] for key in before} == before
    # And it still reads back through the same validation every command applies.
    assert read_manifest(project / "sqlmpeg.json").namespace == "work"


def test_the_lockfile_regenerates_byte_identically(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _publish(registry, _package(tmp_path / "built"))
    project = _project(tmp_path / "work", monkeypatch, capsys)
    assert _run(project, monkeypatch, capsys, "install", "broadcast/tracks")[0] == 0
    first = (project / "sqlmpeg.lock").read_bytes()
    assert _run(project, monkeypatch, capsys, "install", "broadcast/tracks")[0] == 0
    assert (project / "sqlmpeg.lock").read_bytes() == first
    assert _read_json(project / "sqlmpeg.json")["dependencies"] == {"broadcast/tracks": "1.0.0"}


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------


def test_no_version_installs_the_highest_published_one(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _publish(registry, _package(tmp_path / "v1_9", version="1.9.0", factor="0.9"))
    _publish(registry, _package(tmp_path / "v1_10", version="1.10.0", factor="0.10"))
    project = _project(tmp_path / "work", monkeypatch, capsys)

    code, out, _err = _run(project, monkeypatch, capsys, "install", "broadcast/tracks")
    assert code == 0
    # 1.10.0 over 1.9.0: dot-separated parts compared as numbers, not as text.
    assert "broadcast/tracks 1.10.0" in out
    assert read_lockfile(project / "sqlmpeg.lock").entries[0].version == "1.10.0"


def test_an_exact_version_is_taken_and_pinned(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _publish(registry, _package(tmp_path / "v1_9", version="1.9.0", factor="0.9"))
    _publish(registry, _package(tmp_path / "v1_10", version="1.10.0", factor="0.10"))
    project = _project(tmp_path / "work", monkeypatch, capsys)

    assert _run(project, monkeypatch, capsys, "install", "broadcast/tracks@1.9.0")[0] == 0
    assert read_lockfile(project / "sqlmpeg.lock").entries[0].version == "1.9.0"
    code, out, _err = _run(
        project, monkeypatch, capsys, "compile", QUERY.format(call="tracks.quieter")
    )
    assert code == 0 and "volume=volume=0.9" in out


def test_a_version_the_registry_lacks_names_the_published_ones(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _publish(registry, _package(tmp_path / "built", version="1.0.0"))
    project = _project(tmp_path / "work", monkeypatch, capsys)
    code, _out, err = _run(project, monkeypatch, capsys, "install", "broadcast/tracks@2.0.0")
    assert code == 1
    assert "no version 2.0.0" in err and "published: 1.0.0" in err
    assert read_lockfile(project / "sqlmpeg.lock").entries == ()


def test_a_package_the_registry_lacks_is_refused(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _publish(registry, _package(tmp_path / "built"))
    project = _project(tmp_path / "work", monkeypatch, capsys)
    code, _out, err = _run(project, monkeypatch, capsys, "install", "someone/tracks")
    assert code == 1
    assert "no package 'someone/tracks'" in err
    assert "did you mean broadcast/tracks?" in err


def test_a_positional_that_is_not_a_package_name_is_refused(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _publish(registry, _package(tmp_path / "built"))
    project = _project(tmp_path / "work", monkeypatch, capsys)
    code, _out, err = _run(project, monkeypatch, capsys, "install", "../../etc/passwd")
    assert code == 1
    assert "does not name a package" in err


# ---------------------------------------------------------------------------
# the store, and what a bad download costs
# ---------------------------------------------------------------------------


def test_a_tampered_archive_leaves_nothing_in_the_store_or_the_lockfile(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sha256 = _publish(registry, _package(tmp_path / "built"))
    swapped = store.pack(_package(tmp_path / "other", factor="9.9"))
    (registry / "archives" / sha256).write_bytes(swapped)
    # The size the registry records is the swapped archive's too, so what
    # rejects the download is the digest and nothing before it.
    detail = registry / "p" / "broadcast" / "tracks.json"
    payload = _read_json(detail)
    payload["versions"] = [
        {**dict(held), "size": len(swapped)} for held in list(payload["versions"])
    ]
    _write_json(detail, payload)
    project = _project(tmp_path / "work", monkeypatch, capsys)

    code, _out, err = _run(project, monkeypatch, capsys, "install", "broadcast/tracks")
    assert code == 1
    assert "was expected" in err and "nothing was written" in err
    assert "Traceback" not in err
    assert not (store.store_dir() / store.entry_path(sha256)).exists()
    assert read_lockfile(project / "sqlmpeg.lock").entries == ()
    assert "dependencies" not in _read_json(project / "sqlmpeg.json")


def test_an_archive_of_another_size_than_the_registry_recorded_is_refused(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sha256 = _publish(registry, _package(tmp_path / "built"))
    (registry / "archives" / sha256).write_bytes(b"")
    project = _project(tmp_path / "work", monkeypatch, capsys)
    code, _out, err = _run(project, monkeypatch, capsys, "install", "broadcast/tracks")
    assert code == 1
    assert "recorded" in err and "nothing was written" in err


def test_content_already_in_the_store_is_not_downloaded_again(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sha256 = _publish(registry, _package(tmp_path / "built"))
    first = _project(tmp_path / "one", monkeypatch, capsys)
    assert _run(first, monkeypatch, capsys, "install", "broadcast/tracks")[0] == 0

    # The only copy of the archive left is the one in the store.
    (registry / "archives" / sha256).unlink()
    second = _project(tmp_path / "two", monkeypatch, capsys)
    code, out, _err = _run(second, monkeypatch, capsys, "install", "broadcast/tracks")
    assert code == 0
    assert "already in the store" in out
    assert read_lockfile(second / "sqlmpeg.lock").entries[0].sha256 == sha256


# ---------------------------------------------------------------------------
# where an install may write
# ---------------------------------------------------------------------------


def test_install_outside_a_project_names_both_ways_forward(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _publish(registry, _package(tmp_path / "built"))
    bare = tmp_path / "elsewhere"
    bare.mkdir()
    code, _out, err = _run(bare, monkeypatch, capsys, "install", "broadcast/tracks")
    assert code == 2
    assert "sqlmpeg init" in err and "install -g" in err
    assert not (bare / "sqlmpeg.lock").exists()


def test_install_writes_the_machine_wide_lockfile(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _publish(registry, _package(tmp_path / "built"))
    bare = tmp_path / "elsewhere"
    bare.mkdir()
    code, out, _err = _run(bare, monkeypatch, capsys, "install", "-g", "broadcast/tracks")
    assert code == 0
    assert "recorded in" not in out  # there is no manifest beside a machine-wide lockfile
    entry = read_lockfile(store.global_lock_path()).entries[0]
    assert isinstance(entry, RegistryEntry) and entry.namespace == "tracks"


def test_installing_over_a_namespace_says_what_it_replaced(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _publish(registry, _package(tmp_path / "v1", version="1.0.0", factor="0.5"))
    _publish(registry, _package(tmp_path / "v2", version="2.0.0", factor="0.25"))
    project = _project(tmp_path / "work", monkeypatch, capsys)
    assert _run(project, monkeypatch, capsys, "install", "broadcast/tracks@1.0.0")[0] == 0

    code, out, _err = _run(project, monkeypatch, capsys, "install", "broadcast/tracks@2.0.0")
    assert code == 0
    assert "replacing the installed broadcast/tracks 1.0.0" in out
    entries = read_lockfile(project / "sqlmpeg.lock").entries
    assert len(entries) == 1 and entries[0].version == "2.0.0"
    code, out, _err = _run(
        project, monkeypatch, capsys, "compile", QUERY.format(call="tracks.quieter")
    )
    assert code == 0 and "volume=volume=0.25" in out


# ---------------------------------------------------------------------------
# --as
# ---------------------------------------------------------------------------


def test_as_installs_under_another_namespace_and_the_query_calls_it_by_that_name(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _publish(registry, _package(tmp_path / "built"))
    project = _project(tmp_path / "work", monkeypatch, capsys)

    code, out, _err = _run(
        project, monkeypatch, capsys, "install", "broadcast/tracks", "--as", "audio"
    )
    assert code == 0
    assert "installed 'audio' -> broadcast/tracks 1.0.0" in out
    assert "the package calls itself 'tracks'" in out
    assert read_lockfile(project / "sqlmpeg.lock").entries[0].namespace == "audio"

    code, out, _err = _run(
        project, monkeypatch, capsys, "compile", QUERY.format(call="audio.quieter")
    )
    assert code == 0 and "volume=volume=0.5" in out
    # And nothing answers under the namespace the package claimed.
    code, _out, err = _run(
        project, monkeypatch, capsys, "compile", QUERY.format(call="tracks.quieter")
    )
    assert code == 1 and "tracks" in err


def test_two_packages_claiming_one_namespace_both_install(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _publish(registry, _package(tmp_path / "first", factor="0.5"))
    _publish(
        registry,
        _package(tmp_path / "second", name="other/tracks", factor="0.25"),
    )
    project = _project(tmp_path / "work", monkeypatch, capsys)
    assert _run(project, monkeypatch, capsys, "install", "broadcast/tracks")[0] == 0
    assert _run(project, monkeypatch, capsys, "install", "other/tracks", "--as", "other")[0] == 0

    listed = read_lockfile(project / "sqlmpeg.lock").entries
    assert [entry.namespace for entry in listed] == ["tracks", "other"]
    code, out, _err = _run(
        project, monkeypatch, capsys, "compile", QUERY.format(call="other.quieter")
    )
    assert code == 0 and "volume=volume=0.25" in out


@pytest.mark.parametrize("written", ["Not One", "ffmpeg"])
def test_as_refuses_a_namespace_no_query_could_use(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    written: str,
) -> None:
    _publish(registry, _package(tmp_path / "built"))
    project = _project(tmp_path / "work", monkeypatch, capsys)
    code, _out, err = _run(
        project, monkeypatch, capsys, "install", "broadcast/tracks", "--as", written
    )
    assert code == 1
    assert written in err
    assert read_lockfile(project / "sqlmpeg.lock").entries == ()


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def _catalogue(registry: Path, tmp_path: Path) -> None:
    """Two packages that share no field, so a match names which one answered."""
    _publish(
        registry,
        _package(
            tmp_path / "one",
            name="broadcast/tracks",
            namespace="tracks",
            description="audio track tools",
        ),
        functions=("quieter",),
    )
    _publish(
        registry,
        _package(
            tmp_path / "two",
            name="studio/captions",
            namespace="subs",
            description="subtitle wrangling",
        ),
        functions=("burn_in",),
        programs=("extract",),
    )


@pytest.mark.parametrize(
    ("term", "expected"),
    [
        ("tracks", "broadcast/tracks"),  # the name
        ("subs", "studio/captions"),  # the namespace
        ("subtitle", "studio/captions"),  # the description
        ("quieter", "broadcast/tracks"),  # a function it exports
        ("QUIETER", "broadcast/tracks"),  # case-insensitively
    ],
)
def test_search_filters_over_every_field_it_claims_to(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    term: str,
    expected: str,
) -> None:
    _catalogue(registry, tmp_path)
    code, out, _err = _run(tmp_path, monkeypatch, capsys, "search", term, "--json")
    assert code == 0
    assert [held["name"] for held in json.loads(out)["packages"]] == [expected]


def test_search_with_no_term_lists_everything(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _catalogue(registry, tmp_path)
    code, out, _err = _run(tmp_path, monkeypatch, capsys, "search")
    assert code == 0
    assert "broadcast/tracks" in out and "studio/captions" in out
    assert "(2 rows)" in out


def test_a_term_matching_nothing_is_an_empty_table_and_not_an_error(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _catalogue(registry, tmp_path)
    code, out, err = _run(tmp_path, monkeypatch, capsys, "search", "nothing-like-this")
    assert code == 0
    assert "(0 rows)" in out
    assert err == ""


def test_search_reads_no_project_and_works_in_a_bare_directory(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _catalogue(registry, tmp_path)
    bare = tmp_path / "elsewhere"
    bare.mkdir()
    assert _run(bare, monkeypatch, capsys, "search", "tracks")[0] == 0


# ---------------------------------------------------------------------------
# the index cache
# ---------------------------------------------------------------------------


def test_a_second_install_answers_from_the_cached_catalogue(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _publish(registry, _package(tmp_path / "built"))
    first = _project(tmp_path / "one", monkeypatch, capsys)
    assert _run(first, monkeypatch, capsys, "install", "broadcast/tracks")[0] == 0

    # The registry is gone as far as the catalogue is concerned.
    (registry / "index.json").unlink()
    second = _project(tmp_path / "two", monkeypatch, capsys)
    code, out, err = _run(second, monkeypatch, capsys, "install", "broadcast/tracks")
    assert code == 0
    assert "installed 'tracks'" in out
    assert "the registry could not be read" in err
    assert "cached on this machine" in err
    assert read_lockfile(second / "sqlmpeg.lock").entries[0].version == "1.0.0"


def test_search_falls_back_to_the_cached_catalogue_too(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _catalogue(registry, tmp_path)
    assert _run(tmp_path, monkeypatch, capsys, "search")[0] == 0
    (registry / "index.json").unlink()
    code, out, err = _run(tmp_path, monkeypatch, capsys, "search", "tracks")
    assert code == 0
    assert "broadcast/tracks" in out
    assert "cached on this machine" in err


def test_an_unreachable_registry_with_nothing_cached_is_a_typed_error(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _out, err = _run(tmp_path, monkeypatch, capsys, "search")
    assert code == 1
    assert "the registry could not be read at" in err
    assert str(registry / "index.json") in err
    assert "Traceback" not in err


def test_a_cache_that_cannot_be_read_costs_a_fetch_and_nothing_else(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _publish(registry, _package(tmp_path / "built"))
    # The cache is an optimization: garbage in it is discarded, never read.
    packages._cache_path().write_text("{not json", encoding="utf-8")
    assert packages.load_index().names() == ("broadcast/tracks",)
    assert packages.load_index().cached is False


def test_the_cache_is_keyed_by_the_registry_it_came_from(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _publish(registry, _package(tmp_path / "built"))
    assert packages.load_index().names() == ("broadcast/tracks",)
    other = tmp_path / "another-registry"
    other.mkdir()
    monkeypatch.setenv(packages.REGISTRY_ENV, str(other))
    # Nothing cached for this one, so its own emptiness is what is reported.
    with pytest.raises(SqlmpegError) as caught:
        packages.load_index()
    assert "could not be read" in caught.value.message


# ---------------------------------------------------------------------------
# what the registry serves is data off the network
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "needle"),
    [
        ("not json at all", "it is not JSON"),
        ("[]", "it is not a JSON object"),
        ('{"format_version": 99, "packages": []}', "format version 99"),
        ('{"format_version": 1, "packages": {}}', "'packages' is not a list"),
        ('{"format_version": 1, "packages": [{"name": "a/b"}]}', "'version' is not"),
        (
            '{"format_version": 1, "packages": [{"name": "../etc", "version": "1", '
            '"namespace": "x"}]}',
            "is not a package name",
        ),
    ],
)
def test_a_catalogue_this_client_cannot_read_is_refused(
    store_home: Path,
    registry: Path,
    monkeypatch: pytest.MonkeyPatch,
    written: str,
    needle: str,
) -> None:
    (registry / "index.json").write_text(written, encoding="utf-8")
    with pytest.raises(SqlmpegError) as caught:
        packages.load_index()
    assert needle in caught.value.message, caught.value.message


def test_a_detail_file_with_a_bad_digest_is_refused(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _publish(registry, _package(tmp_path / "built"))
    detail = registry / "p" / "broadcast" / "tracks.json"
    payload = _read_json(detail)
    versions = list(payload["versions"])  # type: ignore[arg-type]
    versions[0] = {**dict(versions[0]), "sha256": "nope"}
    payload["versions"] = versions
    _write_json(detail, payload)

    project = _project(tmp_path / "work", monkeypatch, capsys)
    code, _out, err = _run(project, monkeypatch, capsys, "install", "broadcast/tracks")
    assert code == 1
    assert "is not a sha256 digest" in err


def test_an_archive_for_another_package_than_the_registry_listed_is_refused(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The digest matches, so the bytes are the ones published -- and the
    # package inside still says it is something else.
    _publish(registry, _package(tmp_path / "built", version="1.0.0"))
    detail = registry / "p" / "broadcast" / "tracks.json"
    payload = _read_json(detail)
    held_versions = list(payload["versions"])  # type: ignore[arg-type]
    versions = [{**dict(held), "version": "2.0.0"} for held in held_versions]
    payload["versions"] = versions
    _write_json(detail, payload)

    project = _project(tmp_path / "work", monkeypatch, capsys)
    code, _out, err = _run(project, monkeypatch, capsys, "install", "broadcast/tracks@2.0.0")
    assert code == 1
    assert "records version '2.0.0' and the package says '1.0.0'" in err
    assert read_lockfile(project / "sqlmpeg.lock").entries == ()


# ---------------------------------------------------------------------------
# the MCP tools
# ---------------------------------------------------------------------------


def test_the_search_tool_returns_the_catalogue_and_where_it_came_from(
    store_home: Path, registry: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _catalogue(registry, tmp_path)
    result = mcp_tools.search_packages("subtitle")
    assert [held["name"] for held in result["packages"]] == ["studio/captions"]
    assert result["registry"] == str(registry)
    assert result["cached"] is False


def test_the_install_tool_installs_into_the_project_it_is_given(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _publish(registry, _package(tmp_path / "built"))
    project = _project(tmp_path / "work", monkeypatch, capsys)
    result = mcp_tools.install_package("broadcast/tracks", str(project))
    assert result["namespace"] == "tracks"
    assert result["version"] == "1.0.0"
    assert result["downloaded"] is True
    assert read_lockfile(project / "sqlmpeg.lock").entries[0].name == "broadcast/tracks"
    assert capsys.readouterr().out == ""  # stdout is the protocol stream


def test_the_install_tool_refuses_a_directory_that_is_not_a_project(
    store_home: Path,
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _publish(registry, _package(tmp_path / "built"))
    bare = tmp_path / "elsewhere"
    bare.mkdir()
    with pytest.raises(SqlmpegError) as caught:
        mcp_tools.install_package("broadcast/tracks", str(bare))
    assert "sqlmpeg.lock" in caught.value.message
    assert caught.value.hint is not None and "sqlmpeg init" in caught.value.hint
