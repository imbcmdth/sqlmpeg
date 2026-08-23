"""The project files -- ``sqlmpeg.json`` and ``sqlmpeg.lock`` -- and what a query may call.

A directory holding a ``sqlmpeg.json`` is a PROJECT, and a project is itself a
PACKAGE. A package is named ``<namespace>/<package>``, and that name is the
path a call writes: ``imbcmdth/audio`` is called as ``imbcmdth.audio``. The
manifest says what the package provides under that name::

    { "name": "imbcmdth/audio", "version": "1.0.0",
      "lib":  "src/audio.sql",
      "libs": { "quieter": "src/audio.sql" },
      "bin":  "queries/volume.sql",
      "bins": { "duck": "queries/duck.sql" },
      "dependencies": { "tracks": "broadcast/tracks@^1.2.0" } }

Singular is the default, plural is the map, on both halves. ``lib``/``libs``
are the exports: each key is an exported function name, its value the file
defining it, and ``lib``'s export is named for the package segment. ``bin``/
``bins`` are the programs: whole runnable queries, one file per name. The two
halves are read by ROLE -- a lib file holds definitions and nothing else, a
program's file is a query -- and a manifest declaring none of the four is a
consumer project that only holds dependencies.

``dependencies`` keys are ALIASES: the name this project's queries may use for
a package it installed. The value keeps the installed package's name and the
version range together; the range is recorded, never solved.

Beside it, ``sqlmpeg.lock`` records what the project INSTALLED: one entry per
package, either a registry entry pinning a version and the sha256 of the
archive its content was installed from, or a link entry naming a directory to
read live. A registry entry is keyed by the package's name; a link entry names
only the directory, and the package's name comes from the manifest there. It
is machine-owned -- installing writes it, nobody hand-edits it.

:func:`discover` builds the set a compile resolves in, from three layers, the
first claim on a name winning:

1. the local manifest's own package -- the project is a package,
2. the local lockfile -- what this project installed,
3. the global lockfile -- what a global install put on this machine.

All of it is OPTIONAL: none of the three found means no packages, and a query
compiles exactly as it did before this file existed.

This module reads and validates those two files and resolves a package to the
directory its files live in -- the project's own, a linked one, or one in
the content-addressed store (:mod:`sqlmpeg.store`). It does not parse SQL:
what those files DEFINE is :mod:`sqlmpeg.functions`' business, and keeping the
split that way is what lets ``functions.py`` import this module without a
cycle. In particular, "the named function exists in the named file" is checked
where the file is parsed, not here.

It also WRITES the two, since the reader owns what a valid one looks like:
:func:`write_manifest` for ``init``, :func:`write_lockfile` for everything
that records a package. Both replace the file in one step and pin LF endings,
and the lockfile writer decides the ``reproducible`` claim from the entries
themselves rather than trusting a caller to keep the two in step.

Every rejection is a `SqlmpegError`, anchored on the line where the offending
key is written when there is one to point at.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Literal

from . import store
from .errors import ErrorCode, SqlmpegError
from .parser import FILTER_NAMESPACE, MACRO_NAMESPACE

__all__ = [
    "LOCKFILE_NAME",
    "MANIFEST_NAME",
    "RESERVED_NAMESPACES",
    "STATEMENT_KEYWORDS",
    "Dependency",
    "LinkEntry",
    "Lockfile",
    "Package",
    "PackageSet",
    "RegistryEntry",
    "add_dependency",
    "discover",
    "find_lockfile",
    "find_manifest",
    "held_entry",
    "is_namespace",
    "is_package_name",
    "read_lockfile",
    "read_manifest",
    "stored_name",
    "with_entry",
    "without_entry",
    "write_lockfile",
    "write_manifest",
]

MANIFEST_NAME = "sqlmpeg.json"
LOCKFILE_NAME = "sqlmpeg.lock"

# A namespace becomes a call qualifier, so it may not be one the dialect
# already answers for: `ffmpeg.<filter>` and `sqlmpeg.<macro>` are resolved by
# lower, and `wasm` is held for the frei0r/wasm bridge.
WASM_NAMESPACE = "wasm"
RESERVED_NAMESPACES = frozenset({FILTER_NAMESPACE, MACRO_NAMESPACE, WASM_NAMESPACE})

# Unquoted identifiers fold to lowercase, so a name a query can write without
# quoting is a lowercase plain identifier. Both halves of a package name are
# one, and so are an exported function name and a dependency alias.
_IDENTIFIER_RE = re.compile(r"[a-z_][a-z0-9_]*")

# A package name: `<namespace>/<package>`, each half a plain identifier.
_NAME_RE = re.compile(rf"{_IDENTIFIER_RE.pattern}/{_IDENTIFIER_RE.pattern}")

# A program is typed as a command word, so its name is spelled like one: a
# lowercase letter, then lowercase letters, digits, '-' or '_'.
_PROGRAM_NAME_RE = re.compile(r"[a-z][a-z0-9_-]*")

# The four words a statement can begin with. Text starting with one is read as
# SQL, always; anything else typed where a query goes may name a program
# instead. So a program named for one of them could never be run by name.
STATEMENT_KEYWORDS = ("select", "copy", "create", "with")

# Characters that make a written path a pattern rather than a file.
_GLOB_CHARACTERS = "*?["

_REQUIRED = ("name", "version")
_KNOWN = frozenset({*_REQUIRED, "description", "lib", "libs", "bin", "bins", "dependencies"})

# Keys older manifests wrote, each with the hint that says what replaced it.
_RETIRED = {
    "namespace": 'the name carries the namespace: "name" is "<namespace>/<package>"',
    "exports": '"libs" replaced it: a map of exported function name to the file defining it',
}

_NAMESPACE_HINT = (
    "a namespace is a lowercase plain identifier: a letter or underscore, then "
    "letters, digits or underscores"
)
_NAME_HINT = (
    'a package name is "<namespace>/<package>", each half a lowercase plain identifier'
)
_MANIFEST_HINT = (
    'a manifest is one JSON object with "name" and "version"; "lib", "libs", '
    '"bin" and "bins" are what the package provides, all optional'
)
_LIBS_HINT = (
    "libs maps an exported function name to the file defining it, relative to "
    'the manifest, e.g. {"quieter": "src/audio.sql"}'
)
_BINS_HINT = (
    "bins maps a program name to one query file relative to the manifest, e.g. "
    '{"duck": "queries/duck.sql"}'
)
_DEPENDENCIES_HINT = (
    'dependencies maps an alias to "<namespace>/<package>@<range>", e.g. '
    '{"tracks": "broadcast/tracks@^1.2.0"}'
)
_PROGRAM_NAME_HINT = (
    "a program name is a command word: a lowercase letter, then lowercase "
    "letters, digits, '-' or '_'"
)


def is_namespace(text: str) -> bool:
    """True when `text` is spelled like a namespace -- a lowercase plain identifier.

    Shape only: whether the namespace is one sqlmpeg keeps for itself is
    `RESERVED_NAMESPACES`' answer, and a caller deriving a namespace wants the
    two apart to say which of them went wrong.
    """
    return _IDENTIFIER_RE.fullmatch(text) is not None


def is_package_name(text: str) -> bool:
    """True when `text` is spelled like a package name -- ``<namespace>/<package>``.

    Shape only, like :func:`is_namespace`: whether the first segment is
    reserved is a separate question with its own message.
    """
    return _NAME_RE.fullmatch(text) is not None


# Which of the three layers a package was found in. Factual, not a judgment:
# what to say about landing on "global" is the compiler's call, since only it
# knows the call site.
Layer = Literal["project", "local", "global"]


@dataclass(frozen=True)
class Dependency:
    """One manifest dependency: the installed package's name, and the range as written.

    The range is text the manifest holds and a reader may show; nothing here
    solves one.
    """

    name: str
    range: str


@dataclass(frozen=True)
class Package:
    """One package: its name, and what it provides under it.

    `name` is ``<namespace>/<package>``; the two halves are derived, never
    stored twice. `exports` maps each exported function name to the file
    defining it, the default (``lib``'s, named for the package segment) first;
    `programs` does the same for the runnable queries, ``bin``'s under the
    package segment. Several names may share one file. `aliases` is the
    manifest's ``dependencies``, keyed by alias.

    `linked` marks a package read straight out of a working directory rather
    than out of the store. Its files are whatever they are right now, so no
    digest pins them and no lockfile makes a build using it reproducible.
    """

    name: str
    version: str
    root: Path
    manifest: Path
    exports: Mapping[str, Path] = field(default_factory=dict)
    programs: Mapping[str, Path] = field(default_factory=dict)
    aliases: Mapping[str, Dependency] = field(default_factory=dict)
    layer: Layer = "project"
    linked: bool = False

    @property
    def namespace(self) -> str:
        """The first segment of the name: what qualifies a call."""
        return self.name.partition("/")[0]

    @property
    def package(self) -> str:
        """The second segment of the name: the default export's and program's name."""
        return self.name.partition("/")[2]

    def export(self, member: str | None = None) -> Path | None:
        """The file exporting `member`, or the default export's file for None."""
        return self.exports.get(self.package if member is None else member)

    def program(self, member: str | None = None) -> Path | None:
        """The file of program `member`, or the default program's file for None."""
        return self.programs.get(self.package if member is None else member)

    def dependency(self, alias: str) -> Dependency | None:
        """What `alias` names in this package's manifest, or None."""
        return self.aliases.get(alias)


@dataclass(frozen=True)
class PackageSet:
    """The packages a compile may resolve a call in, keyed by name.

    `aliases` maps each of the PROJECT manifest's dependency aliases to the
    package name it binds; a package the alias names but nothing installed is
    an alias with no entry in `packages`.

    `in_project` is True when the query sits inside a project -- a manifest or
    a lockfile was found above it. It is what makes landing on the global
    layer worth warning about: outside a project, a globally installed package
    is the only thing there is to resolve against.
    """

    root: Path
    packages: dict[str, Package] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    in_project: bool = True

    def get(self, name: str) -> Package | None:
        """The package `name` names, by its full ``<namespace>/<package>``."""
        return self.packages.get(name)

    def find(self, namespace: str, package: str) -> Package | None:
        """The package the two segments name, or None."""
        return self.packages.get(f"{namespace}/{package}")

    def aliased(self, alias: str) -> Package | None:
        """The installed package the project's `alias` binds, or None."""
        name = self.aliases.get(alias)
        return None if name is None else self.packages.get(name)

    def in_namespace(self, namespace: str) -> tuple[Package, ...]:
        """Every package under `namespace`, in name order."""
        return tuple(
            self.packages[name]
            for name in sorted(self.packages)
            if self.packages[name].namespace == namespace
        )

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.packages))

    def namespaces(self) -> tuple[str, ...]:
        return tuple(sorted({package.namespace for package in self.packages.values()}))


# -- rejections ------------------------------------------------------------


def _reject(
    path: Path,
    message: str,
    *,
    line: int | None = None,
    col: int | None = None,
    hint: str | None = None,
) -> SqlmpegError:
    """A manifest rejection, naming the file and the line when there is one."""
    return SqlmpegError(
        ErrorCode.UNSUPPORTED_SQL,
        f"{path}: {message}",
        line=line,
        col=col if line is not None else None,
        hint=hint,
    )


def _key_line(text: str, key: str) -> int | None:
    """The line `key` is written on, so a rejection about it can point there."""
    needle = f'"{key}"'
    for number, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return number
    return None


def _did_you_mean(name: str, candidates: list[str]) -> str | None:
    matches = difflib.get_close_matches(name, candidates, n=1, cutoff=0.6)
    return f"did you mean {matches[0]!r}?" if matches else None


# -- reading one manifest --------------------------------------------------


class _Object(dict[str, object]):
    """A parsed JSON object that remembers the keys its text wrote twice.

    ``json`` keeps the last of two same-named keys and says nothing, which
    would silently drop one of two programs written under one name.
    """

    repeated: tuple[str, ...] = ()


def _as_object(pairs: list[tuple[str, object]]) -> _Object:
    """The object-parsing hook: build the dict, and note every repeated key."""
    seen: set[str] = set()
    repeated: list[str] = []
    for key, _value in pairs:
        if key in seen and key not in repeated:
            repeated.append(key)
        seen.add(key)
    parsed = _Object(pairs)
    parsed.repeated = tuple(repeated)
    return parsed


def _text_field(
    data: dict[str, object], key: str, path: Path, text: str, *, hint: str = _MANIFEST_HINT
) -> str:
    value = data[key]
    if not isinstance(value, str) or not value.strip():
        raise _reject(
            path,
            f'"{key}" must be a non-empty string',
            line=_key_line(text, key),
            hint=hint,
        )
    return value


def _package_name(data: dict[str, object], path: Path, text: str) -> str:
    """The name `data` declares, validated: the shape, and the reserved first segment."""
    line = _key_line(text, "name")
    name = _text_field(data, "name", path, text)
    if not is_package_name(name):
        raise _reject(
            path, f"name {name!r} is not a package name", line=line, hint=_NAME_HINT
        )
    namespace = name.partition("/")[0]
    if namespace in RESERVED_NAMESPACES:
        reserved = ", ".join(sorted(RESERVED_NAMESPACES))
        raise _reject(
            path,
            f"namespace {namespace!r} is reserved",
            line=line,
            hint=f"{reserved} belong to sqlmpeg itself; pick another namespace",
        )
    return name


def _leaves_project(written: str) -> bool:
    """True for a written path that names something outside the manifest's directory."""
    relative = PurePosixPath(written.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        return True
    return re.match(r"[A-Za-z]:", written) is not None


def _file_value(
    written: object, owner: str, root: Path, path: Path, line: int | None, hint: str
) -> Path:
    """The one file `owner` names, validated: one path, under the project, and there."""
    if not isinstance(written, str) or not written.strip():
        raise _reject(path, f"{owner} must name one file", line=line, hint=hint)
    if _leaves_project(written):
        raise _reject(
            path,
            f"{owner} points at {written!r}, which leaves the project directory",
            line=line,
            hint="the file is relative to the manifest and stays under it",
        )
    if any(character in written for character in _GLOB_CHARACTERS):
        raise _reject(
            path,
            f"{owner} names the pattern {written!r}, not a file",
            line=line,
            hint="one name, one file",
        )
    file = root / Path(*PurePosixPath(written.replace("\\", "/")).parts)
    try:
        present = file.is_file()
    except (OSError, ValueError) as err:
        raise _reject(
            path,
            f"{owner}: {written!r} could not be read: {err}",
            line=line,
            hint=hint,
        ) from err
    if not present:
        raise _reject(
            path,
            f"{owner} names no file: {written!r}",
            line=line,
            hint=f"relative to {root}; check the path and the extension",
        )
    return file


def _map_of(data: dict[str, object], key: str, path: Path, text: str, hint: str) -> _Object:
    """The JSON object `key` holds, its repeated keys rejected."""
    at = _key_line(text, key)
    value = data[key]
    if not isinstance(value, dict):
        raise _reject(path, f'"{key}" must be a JSON object', line=at, hint=hint)
    if isinstance(value, _Object) and value.repeated:
        repeated = value.repeated[0]
        raise _reject(
            path,
            f"{key} declares {repeated!r} twice",
            line=_key_line(text, repeated) or at,
            hint="one name, one entry; keep the one you meant",
        )
    result = _Object(value)
    return result


def _exports(
    data: dict[str, object], segment: str, root: Path, path: Path, text: str
) -> dict[str, Path]:
    """The export map ``lib``/``libs`` declare: name to file, the default first."""
    exports: dict[str, Path] = {}
    if "lib" in data:
        exports[segment] = _file_value(
            data["lib"], '"lib"', root, path, _key_line(text, "lib"), _MANIFEST_HINT
        )
    if "libs" not in data:
        return exports
    at = _key_line(text, "libs")
    for name, written in _map_of(data, "libs", path, text, _LIBS_HINT).items():
        line = _key_line(text, name) or at
        if _IDENTIFIER_RE.fullmatch(name) is None:
            raise _reject(
                path,
                f"export name {name!r} is not a plain identifier",
                line=line,
                hint="an exported function name is a lowercase plain identifier",
            )
        if name == segment:
            raise _reject(
                path,
                f"libs declares {name!r}, the package's own name",
                line=line,
                hint='the package segment names the default export; declare its file as "lib"',
            )
        exports[name] = _file_value(written, f"export '{name}'", root, path, line, _LIBS_HINT)
    return exports


def _programs(
    data: dict[str, object], segment: str, root: Path, path: Path, text: str
) -> dict[str, Path]:
    """The program map ``bin``/``bins`` declare: name to file, the default first."""
    programs: dict[str, Path] = {}
    if "bin" in data:
        programs[segment] = _file_value(
            data["bin"], '"bin"', root, path, _key_line(text, "bin"), _MANIFEST_HINT
        )
    if "bins" not in data:
        return programs
    at = _key_line(text, "bins")
    for name, written in _map_of(data, "bins", path, text, _BINS_HINT).items():
        line = _key_line(text, name) or at
        if _PROGRAM_NAME_RE.fullmatch(name) is None:
            raise _reject(
                path,
                f"program name {name!r} is not a command name",
                line=line,
                hint=_PROGRAM_NAME_HINT,
            )
        if name in STATEMENT_KEYWORDS:
            raise _reject(
                path,
                f"program name {name!r} is a word a query begins with",
                line=line,
                hint=f"text beginning with {', '.join(STATEMENT_KEYWORDS)} is read as SQL, "
                "so a program of that name could never be run by name; rename it",
            )
        if name == segment:
            raise _reject(
                path,
                f"bins declares {name!r}, the package's own name",
                line=line,
                hint='the package segment names the default program; declare its file as "bin"',
            )
        programs[name] = _file_value(written, f"program '{name}'", root, path, line, _BINS_HINT)
    return programs


def _dependencies(data: dict[str, object], path: Path, text: str) -> dict[str, Dependency]:
    """The alias map ``dependencies`` declares; empty when it declares none."""
    if "dependencies" not in data:
        return {}
    at = _key_line(text, "dependencies")
    dependencies: dict[str, Dependency] = {}
    for alias, written in _map_of(data, "dependencies", path, text, _DEPENDENCIES_HINT).items():
        line = _key_line(text, alias) or at
        if _IDENTIFIER_RE.fullmatch(alias) is None:
            raise _reject(
                path,
                f"alias {alias!r} is not a plain identifier",
                line=line,
                hint="an alias is a lowercase plain identifier a query may qualify a call with",
            )
        if alias in RESERVED_NAMESPACES:
            reserved = ", ".join(sorted(RESERVED_NAMESPACES))
            raise _reject(
                path,
                f"alias {alias!r} is reserved",
                line=line,
                hint=f"{reserved} belong to sqlmpeg itself; pick another alias",
            )
        if not isinstance(written, str) or not written.strip():
            raise _reject(
                path,
                f"dependency '{alias}' must be a string",
                line=line,
                hint=_DEPENDENCIES_HINT,
            )
        name, separator, version_range = written.partition("@")
        if not is_package_name(name) or not separator or not version_range.strip():
            raise _reject(
                path,
                f"dependency '{alias}': {written!r} is not "
                '"<namespace>/<package>@<range>"',
                line=line,
                hint=_DEPENDENCIES_HINT,
            )
        if name.partition("/")[0] in RESERVED_NAMESPACES:
            raise _reject(
                path,
                f"dependency '{alias}': namespace {name.partition('/')[0]!r} is reserved",
                line=line,
                hint="no package is published under a reserved namespace",
            )
        dependencies[alias] = Dependency(name=name, range=version_range)
    return dependencies


def read_manifest(path: Path) -> Package:
    """Parse and validate one ``sqlmpeg.json`` into the package it declares.

    Raises ``SqlmpegError`` -- and nothing else -- on every rejection: an
    unreadable file, text that is not JSON, a missing or malformed key, a name
    whose first segment is reserved, a lib or bin that names no file, a
    malformed dependency. Whether a named file DEFINES what the manifest says
    it does is checked where the file is parsed, not here.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        raise _reject(path, f"could not be read: {err.strerror or err}") from err
    try:
        data = json.loads(text, object_pairs_hook=_as_object)
    except json.JSONDecodeError as err:
        raise _reject(
            path,
            f"is not valid JSON: {err.msg}",
            line=err.lineno,
            col=err.colno,
            hint=_MANIFEST_HINT,
        ) from err
    except (ValueError, RecursionError) as err:  # backstop: never a traceback
        raise _reject(path, "is not valid JSON", line=1, col=1, hint=_MANIFEST_HINT) from err
    if not isinstance(data, dict):
        raise _reject(path, "is not a JSON object", line=1, col=1, hint=_MANIFEST_HINT)

    for key in sorted(data):
        if key in _KNOWN:
            continue
        hint = (
            _RETIRED.get(key)
            or _did_you_mean(key, sorted(_KNOWN))
            or f"known keys: {', '.join(sorted(_KNOWN))}"
        )
        raise _reject(path, f"unknown key {key!r}", line=_key_line(text, key), hint=hint)
    for key in _REQUIRED:
        if key not in data:
            raise _reject(path, f'is missing "{key}"', line=1, col=1, hint=_MANIFEST_HINT)

    name = _package_name(data, path, text)
    segment = name.partition("/")[2]
    return Package(
        name=name,
        version=_text_field(data, "version", path, text),
        root=path.parent,
        manifest=path,
        exports=_exports(data, segment, path.parent, path, text),
        programs=_programs(data, segment, path.parent, path, text),
        aliases=_dependencies(data, path, text),
    )


# -- reading one lockfile --------------------------------------------------

# Bump on any change to the lockfile's shape. Another version's file is
# rejected rather than read optimistically: installing rewrites the lockfile,
# and guessing at a shape would resolve a call against content nobody pinned.
LOCK_FORMAT_VERSION = 2

_LOCK_HINT = 'a lockfile is one JSON object with "format_version", "reproducible" and "packages"'

_LOCK_REQUIRED = ("format_version", "reproducible", "packages")
_LOCK_KNOWN = frozenset({*_LOCK_REQUIRED, "not_reproducible_because"})

_REGISTRY_KEYS = ("kind", "name", "version", "sha256", "store")
_LINK_KEYS = ("kind", "path")
_KINDS = ("link", "registry")


@dataclass(frozen=True)
class RegistryEntry:
    """A package installed from the registry: a version, and the archive digest that pinned it."""

    name: str
    version: str
    sha256: str
    store: str


@dataclass(frozen=True)
class LinkEntry:
    """A package read live out of a directory: no version, no digest.

    That is not an omission. A link exists so edits to that directory land in
    the next compile, which is exactly what a digest cannot survive. The
    package's name is not recorded either -- it comes from the manifest in the
    directory, so renaming the package there needs no re-link.
    """

    path: str


LockEntry = RegistryEntry | LinkEntry


@dataclass(frozen=True)
class Lockfile:
    """One ``sqlmpeg.lock``: what it pins, and whether it pins all of it.

    `reproducible` is false when some entry is a link, and the file says so in
    its own text -- both the flag and a sentence naming why -- so a human
    reading it is not left to infer it from the entry kinds.
    """

    path: Path
    reproducible: bool
    entries: tuple[LockEntry, ...]

    def links(self) -> tuple[LinkEntry, ...]:
        return tuple(entry for entry in self.entries if isinstance(entry, LinkEntry))


def _value_line(text: str, value: object) -> int | None:
    """The line a string VALUE is written on, for a rejection about one entry.

    Entries repeat their keys, so a rejection anchored on ``"name"`` would
    point at the first entry whatever entry it is about; the name's own text
    is what tells them apart.
    """
    return _key_line(text, value) if isinstance(value, str) else None


def _entry_dict(raw: object, path: Path, text: str, index: int) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise _reject(
            path,
            f"package entry {index} is not a JSON object",
            line=_key_line(text, "packages"),
            hint=_LOCK_HINT,
        )
    return raw


def _entry_kind(data: dict[str, object], path: Path, line: int | None) -> str:
    kind = data.get("kind")
    if kind in _KINDS:
        assert isinstance(kind, str)
        return kind
    written = f"kind {kind!r}" if isinstance(kind, str) else 'no "kind"'
    hint = (_did_you_mean(kind, list(_KINDS)) if isinstance(kind, str) else None) or (
        "a package entry is a 'registry' one, pinning a version and a digest, "
        "or a 'link' one, naming a directory"
    )
    raise _reject(path, f"a package entry has {written}", line=line, hint=hint)


def _entry(raw: object, path: Path, text: str, index: int) -> LockEntry:
    """One ``packages`` element, validated into the entry it declares."""
    data = _entry_dict(raw, path, text, index)
    line = _value_line(text, data.get("name")) or _value_line(text, data.get("path"))
    kind = _entry_kind(data, path, line)
    keys = _REGISTRY_KEYS if kind == "registry" else _LINK_KEYS
    for key in sorted(data):
        if key in keys:
            continue
        hint = _did_you_mean(key, list(keys)) or f"a {kind} entry holds: {', '.join(keys)}"
        raise _reject(path, f"unknown key {key!r} in a {kind} entry", line=line, hint=hint)
    for key in keys:
        if key not in data:
            raise _reject(
                path,
                f'a {kind} entry is missing "{key}"',
                line=line,
                hint=f"a {kind} entry holds: {', '.join(keys)}",
            )
    if kind == "link":
        return LinkEntry(path=_text_field(data, "path", path, text, hint=_LOCK_HINT))
    name = _text_field(data, "name", path, text, hint=_LOCK_HINT)
    if not is_package_name(name):
        raise _reject(
            path, f"name {name!r} is not a package name", line=line, hint=_NAME_HINT
        )
    if name.partition("/")[0] in RESERVED_NAMESPACES:
        raise _reject(
            path,
            f"package '{name}' claims a reserved namespace",
            line=line,
            hint="no package is published under a reserved namespace; drop the entry",
        )
    return RegistryEntry(
        name=name,
        version=_text_field(data, "version", path, text, hint=_LOCK_HINT),
        sha256=_text_field(data, "sha256", path, text, hint=_LOCK_HINT),
        store=_text_field(data, "store", path, text, hint=_LOCK_HINT),
    )


def _entries(data: dict[str, object], path: Path, text: str) -> tuple[LockEntry, ...]:
    line = _key_line(text, "packages")
    raw = data["packages"]
    if not isinstance(raw, list):
        raise _reject(path, '"packages" must be a list', line=line, hint=_LOCK_HINT)
    entries: list[LockEntry] = []
    named: set[str] = set()
    linked: set[str] = set()
    for index, element in enumerate(raw):
        entry = _entry(element, path, text, index)
        if isinstance(entry, RegistryEntry):
            if entry.name in named:
                raise _reject(
                    path,
                    f"two entries name package '{entry.name}'",
                    line=_value_line(text, entry.name),
                    hint="one package, one entry; install the one you meant to keep",
                )
            named.add(entry.name)
        else:
            if entry.path in linked:
                raise _reject(
                    path,
                    f"two entries link {entry.path!r}",
                    line=_value_line(text, entry.path),
                    hint="one directory, one link",
                )
            linked.add(entry.path)
        entries.append(entry)
    return tuple(entries)


def read_lockfile(path: Path) -> Lockfile:
    """Parse and validate one ``sqlmpeg.lock`` into the packages it pins.

    Raises ``SqlmpegError`` -- and nothing else -- on every rejection: an
    unreadable file, text that is not JSON, another format version, a
    malformed entry, two entries naming one package, or a file that claims
    to be reproducible while linking a directory.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        raise _reject(path, f"could not be read: {err.strerror or err}") from err
    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        raise _reject(
            path, f"is not valid JSON: {err.msg}", line=err.lineno, col=err.colno, hint=_LOCK_HINT
        ) from err
    except (ValueError, RecursionError) as err:  # backstop: never a traceback
        raise _reject(path, "is not valid JSON", line=1, col=1, hint=_LOCK_HINT) from err
    if not isinstance(data, dict):
        raise _reject(path, "is not a JSON object", line=1, col=1, hint=_LOCK_HINT)

    for key in sorted(data):
        if key in _LOCK_KNOWN:
            continue
        hint = (
            _did_you_mean(key, sorted(_LOCK_KNOWN))
            or f"known keys: {', '.join(sorted(_LOCK_KNOWN))}"
        )
        raise _reject(path, f"unknown key {key!r}", line=_key_line(text, key), hint=hint)
    for key in _LOCK_REQUIRED:
        if key not in data:
            raise _reject(path, f'is missing "{key}"', line=1, col=1, hint=_LOCK_HINT)
    if data["format_version"] != LOCK_FORMAT_VERSION:
        raise _reject(
            path,
            f"was written in lockfile format {data['format_version']!r}, and this "
            f"sqlmpeg reads {LOCK_FORMAT_VERSION}",
            line=_key_line(text, "format_version"),
            hint="install the project's packages again to rewrite it",
        )
    reproducible = data["reproducible"]
    if not isinstance(reproducible, bool):
        raise _reject(
            path,
            '"reproducible" must be true or false',
            line=_key_line(text, "reproducible"),
            hint=_LOCK_HINT,
        )
    because = data.get("not_reproducible_because")
    if because is not None and not isinstance(because, str):
        raise _reject(
            path,
            '"not_reproducible_because" must be a string',
            line=_key_line(text, "not_reproducible_because"),
            hint="it is the sentence a reader of the file sees; leave it out when there "
            "is nothing to say",
        )

    lockfile = Lockfile(path=path, reproducible=reproducible, entries=_entries(data, path, text))
    linked = lockfile.links()
    if reproducible and linked:
        raise _reject(
            path,
            f"claims to be reproducible while linking {linked[0].path!r}",
            line=_key_line(text, "reproducible"),
            hint="a linked directory is edited in place, so nothing here pins it: a "
            "lockfile holding a link is not reproducible",
        )
    return lockfile


# -- writing the two files -------------------------------------------------

# The sentence a lockfile holding a link carries in its own text.
_LINKED_BECAUSE = (
    "a package is linked to a working directory, so its files are not pinned here"
)

_WRITE_HINT = "check that the directory exists and is writable"


def _unwritable(path: Path, err: OSError) -> SqlmpegError:
    return _reject(path, f"could not be written: {err.strerror or err}", hint=_WRITE_HINT)


def _write_atomically(path: Path, text: str) -> None:
    """Replace `path` with `text` in one step, LF-terminated on every platform.

    Written beside the destination and moved onto it, so a reader sees the old
    file or the new one and never half of either. Unlike the registry's disk
    cache, a failure here is a rejection: this file is the project's, and
    losing it silently is not an option the caller has.
    """
    directory = path.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=directory, prefix=f"{path.name}-", suffix=".tmp")
    except OSError as err:
        raise _unwritable(path, err) from err
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
        os.replace(temporary, path)
    except OSError as err:
        try:
            os.unlink(temporary)
        except OSError:  # already gone, or the directory refuses us twice
            pass
        raise _unwritable(path, err) from err


def _rendered(payload: dict[str, object]) -> str:
    """`payload` as the text of one project file: written order, 2-space indent, LF.

    Insertion order, never sorted: the order keys and entries were handed over
    in is the order the file holds them, so writing the same thing twice
    produces the same bytes and a rewrite shows only what changed.
    """
    return json.dumps(payload, indent=2) + "\n"


def write_manifest(
    path: Path,
    *,
    name: str,
    version: str,
    description: str | None = None,
    lib: str | None = None,
    libs: Mapping[str, str] | None = None,
    bin: str | None = None,
    bins: Mapping[str, str] | None = None,
    dependencies: Mapping[str, str] | None = None,
) -> None:
    """Write the ``sqlmpeg.json`` declaring this package.

    Only the two required keys are always written; an optional one is left
    out entirely rather than written empty.
    """
    payload: dict[str, object] = {"name": name, "version": version}
    if description:
        payload["description"] = description
    if lib:
        payload["lib"] = lib
    if libs:
        payload["libs"] = dict(libs)
    if bin:
        payload["bin"] = bin
    if bins:
        payload["bins"] = dict(bins)
    if dependencies:
        payload["dependencies"] = dict(dependencies)
    _write_atomically(path, _rendered(payload))


def add_dependency(path: Path, alias: str, name: str, version: str) -> None:
    """Record `name` at `version` under `alias` in the manifest at `path`.

    The file is rewritten from its own text rather than from a parsed
    `Package`, so what the author wrote stays as written and a rewrite shows
    only the dependency that changed. Any other alias naming `name` is
    dropped: one package, one alias.

    The version is written exact. A range is a thing a manifest may hold and
    a reader may show; nothing here solves one.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        raise _reject(path, f"could not be read: {err.strerror or err}") from err
    try:
        data = json.loads(text, object_pairs_hook=_as_object)
    except (ValueError, RecursionError) as err:
        raise _reject(path, "is not valid JSON", line=1, col=1, hint=_MANIFEST_HINT) from err
    if not isinstance(data, dict):
        raise _reject(path, "is not a JSON object", line=1, col=1, hint=_MANIFEST_HINT)
    held = data.get("dependencies", {})
    if not isinstance(held, dict) or any(not isinstance(value, str) for value in held.values()):
        raise _reject(
            path,
            '"dependencies" is not an object of alias to "<name>@<range>"',
            line=_key_line(text, "dependencies"),
            hint=_DEPENDENCIES_HINT,
        )
    kept = {
        key: value for key, value in held.items() if str(value).partition("@")[0] != name
    }
    kept[alias] = f"{name}@{version}"
    data["dependencies"] = kept
    _write_atomically(path, _rendered(dict(data)))


def _entry_payload(entry: LockEntry) -> dict[str, object]:
    """One entry as the lockfile writes it, keys in the order the reader lists them."""
    if isinstance(entry, LinkEntry):
        return {"kind": "link", "path": entry.path}
    return {
        "kind": "registry",
        "name": entry.name,
        "version": entry.version,
        "sha256": entry.sha256,
        "store": entry.store,
    }


def write_lockfile(path: Path, entries: Sequence[LockEntry]) -> None:
    """Write the ``sqlmpeg.lock`` pinning `entries`, in the order given.

    The reproducibility claim is this function's, not the caller's: a link is
    read live and no digest survives that, so any link among `entries` makes
    the file say it is not reproducible and say why. `read_lockfile` refuses a
    file claiming otherwise, so a caller allowed to set the flag could write
    one sqlmpeg would not read back.
    """
    linked = any(isinstance(entry, LinkEntry) for entry in entries)
    payload: dict[str, object] = {
        "format_version": LOCK_FORMAT_VERSION,
        "reproducible": not linked,
    }
    if linked:
        payload["not_reproducible_because"] = _LINKED_BECAUSE
    payload["packages"] = [_entry_payload(entry) for entry in entries]
    _write_atomically(path, _rendered(payload))


def stored_name(entry: LockEntry, lock_path: Path) -> str | None:
    """The package name `entry` pins: a registry entry's own, a link's manifest's.

    None for a link whose directory holds no readable manifest -- a dead link
    still has to be listable and removable, so this never raises.
    """
    if isinstance(entry, RegistryEntry):
        return entry.name
    try:
        return read_manifest(_linked_root(entry, lock_path) / MANIFEST_NAME).name
    except SqlmpegError:
        return None


def held_entry(entries: Sequence[LockEntry], name: str, lock_path: Path) -> LockEntry | None:
    """The entry pinning package `name`, whichever kind it is, or None."""
    for entry in entries:
        if stored_name(entry, lock_path) == name:
            return entry
    return None


def with_entry(
    entries: Sequence[LockEntry], entry: LockEntry, replaced: LockEntry | None = None
) -> tuple[LockEntry, ...]:
    """`entries` with `entry` in place of `replaced`, or appended when there is none.

    One package, one entry: the caller finds what an install or a link
    replaces with :func:`held_entry`, since only it knows the lockfile the
    entries came from.
    """
    if replaced is None:
        return (*entries, entry)
    return tuple(entry if held == replaced else held for held in entries)


def without_entry(entries: Sequence[LockEntry], entry: LockEntry) -> tuple[LockEntry, ...]:
    """`entries` without `entry`, the rest in order."""
    return tuple(held for held in entries if held != entry)


# -- discovery -------------------------------------------------------------


def _start_directory(start: Path) -> Path | None:
    """Where an upward walk from `start` begins, or None for a path we cannot read."""
    try:
        current = Path(start).resolve()
        if current.is_file():
            current = current.parent
    except (OSError, ValueError):  # an unreadable or malformed path is not a project
        return None
    return current


def _walk_up(start: Path, name: str) -> Path | None:
    """The nearest `name` at or above `start`, or None at the filesystem root."""
    current = _start_directory(start)
    if current is None:
        return None
    for directory in (current, *current.parents):
        candidate = directory / name
        try:
            if candidate.is_file():
                return candidate
        except (OSError, ValueError):
            continue
    return None


def find_manifest(start: Path) -> Path | None:
    """The nearest ``sqlmpeg.json`` at or above `start`, or None at the root.

    The walk stops at the filesystem root; a project is optional, and finding
    none is the ordinary case, not a rejection.
    """
    return _walk_up(start, MANIFEST_NAME)


def find_lockfile(start: Path) -> Path | None:
    """The lockfile that belongs to the project above `start`, or None.

    Beside the manifest when there is one -- installing writes the two
    together, and a lockfile further up belongs to the project further up, not
    to this one. With no manifest anywhere, the nearest lockfile above `start`
    is the project.
    """
    manifest = find_manifest(start)
    if manifest is None:
        return _walk_up(start, LOCKFILE_NAME)
    beside = manifest.parent / LOCKFILE_NAME
    try:
        return beside if beside.is_file() else None
    except (OSError, ValueError):
        return None


def _linked_root(entry: LinkEntry, lock_path: Path) -> Path:
    """The directory a link entry names, relative to the lockfile holding it."""
    try:
        return (lock_path.parent / Path(entry.path)).resolve()
    except (OSError, ValueError) as err:
        raise _reject(
            lock_path,
            f"link {entry.path!r} is not a directory path",
            hint="a link names the directory the package is developed in",
        ) from err


def _manifest_of(root: Path, named: str, lock: Lockfile, missing: str) -> Path:
    """The manifest a locked package is read through, or a rejection naming `missing`."""
    manifest = root / MANIFEST_NAME
    try:
        present = manifest.is_file()
    except (OSError, ValueError):
        present = False
    if not present:
        raise _reject(
            lock.path,
            f"{named}: {root} holds no {MANIFEST_NAME}",
            hint=missing,
        )
    return manifest


def _same(entry_value: str, found: str, field_name: str, name: str, lock: Lockfile) -> None:
    """Reject a lockfile entry the package it points at disagrees with."""
    if entry_value == found:
        return
    raise _reject(
        lock.path,
        f"package '{name}': the lockfile records {field_name} {entry_value!r} and the "
        f"package says {found!r}",
        hint="the package changed since it was installed; install it again",
    )


def _linked_package(entry: LinkEntry, lock: Lockfile, layer: Layer) -> Package:
    """A link resolved: the local layer again, rooted somewhere else.

    Same `read_manifest`, so a linked package is validated exactly as the
    project's own is -- and its files are read on every compile, which is
    what makes an edit show up without reinstalling. Its name is the
    manifest's, recorded nowhere else.
    """
    root = _linked_root(entry, lock.path)
    manifest = _manifest_of(
        root, f"link {entry.path!r}", lock, "link the directory again, or restore its manifest"
    )
    package = read_manifest(manifest)
    return replace(package, layer=layer, linked=True)


def _stored_package(entry: RegistryEntry, lock: Lockfile, layer: Layer) -> Package:
    """A registry entry resolved: the store directory its digest names."""
    try:
        root = store.load(entry.name, entry.store, entry.sha256)
    except SqlmpegError as err:
        # Renamed onto the lockfile: that is the file the reader has open, not
        # a path under a cache directory they never chose.
        raise _reject(lock.path, err.message, hint=err.hint) from err
    manifest = _manifest_of(
        root,
        f"package '{entry.name}'",
        lock,
        "the stored content is not a package; install it again",
    )
    package = read_manifest(manifest)
    _same(entry.name, package.name, "name", entry.name, lock)
    _same(entry.version, package.version, "version", entry.name, lock)
    return replace(package, layer=layer)


def _add_layer(packages: dict[str, Package], lock: Lockfile | None, layer: Layer) -> None:
    """Add `lock`'s packages under the names no earlier layer claimed."""
    if lock is None:
        return
    resolved: set[str] = set()
    for entry in lock.entries:
        if isinstance(entry, LinkEntry):
            package = _linked_package(entry, lock, layer)
        else:
            package = _stored_package(entry, lock, layer)
        if package.name in resolved:
            # The reader catches two same-kind claims; a registry entry and a
            # link resolving to one package is only knowable here.
            raise _reject(
                lock.path,
                f"two entries name package '{package.name}'",
                hint="one package, one entry; install or link the one you meant to keep",
            )
        resolved.add(package.name)
        if package.name in packages:  # first claim wins, layer by layer
            continue
        packages[package.name] = package


def _global_lockfile(local: Path | None) -> Lockfile | None:
    """The machine-wide lockfile, or None when nothing was installed globally."""
    path = store.global_lock_path()
    try:
        if not path.is_file() or (local is not None and path == local):
            return None
    except (OSError, ValueError):
        return None
    return read_lockfile(path)


def _check_aliases(project: Package, packages: dict[str, Package]) -> None:
    """Reject a project alias that is also an installed package's namespace.

    A two-part call resolves an alias or a namespace, never both; the two
    sets stay disjoint, and this is the one place both are known.
    """
    for alias in project.aliases:
        held = next(
            (package for package in packages.values() if package.namespace == alias), None
        )
        if held is None:
            continue
        try:
            line = _key_line(project.manifest.read_text(encoding="utf-8"), alias)
        except OSError:
            line = None
        raise _reject(
            project.manifest,
            f"alias '{alias}' is also the namespace of the installed package "
            f"'{held.name}'",
            line=line,
            hint="an alias may not shadow a namespace; rename the alias in dependencies",
        )


def discover(start: Path | str | None = None) -> PackageSet | None:
    """The package set for a query written in `start`, or None with nothing to resolve in.

    `start` is a directory or a query file's path; None means the working
    directory, which is the CLI's answer for a query typed on the command
    line.

    Three layers, the first claim on a name winning: the project's own
    manifest, then its lockfile, then the machine-wide one. The layering lives
    here and nowhere else -- what the compiler gets is one name to one
    package, with no idea which layer answered.

    Raises ``SqlmpegError`` for a manifest or lockfile that is found but
    malformed, for a locked package the store or the linked directory cannot
    produce, and for a project alias equal to an installed package's
    namespace.
    """
    base = Path(start) if start is not None else Path.cwd()
    manifest = find_manifest(base)
    local = find_lockfile(base)
    packages: dict[str, Package] = {}
    project: Package | None = None
    if manifest is not None:
        project = read_manifest(manifest)
        packages[project.name] = project
    _add_layer(packages, read_lockfile(local) if local is not None else None, "local")
    _add_layer(packages, _global_lockfile(local), "global")

    aliases: dict[str, str] = {}
    if project is not None:
        _check_aliases(project, packages)
        aliases = {alias: dependency.name for alias, dependency in project.aliases.items()}

    in_project = manifest is not None or local is not None
    if not in_project and not packages:
        return None
    root = manifest.parent if manifest is not None else local.parent if local is not None else base
    return PackageSet(root=root, packages=packages, aliases=aliases, in_project=in_project)
