"""The project files -- ``sqlmpeg.json`` and ``sqlmpeg.lock`` -- and what a query may call.

A directory holding a ``sqlmpeg.json`` is a PROJECT, and a project is itself a
PACKAGE: the manifest claims a namespace and says what the package provides,
so a query in ``queries/`` can call a function defined in ``src/``::

    { "name": "my-edits", "version": "0.1.0", "namespace": "me",
      "exports": ["src/*.sql"],
      "bin": { "split-chapters": "queries/split.sql" } }

A package provides either half or both. ``exports`` is the library: files of
``CREATE FUNCTION`` definitions, reached as ``ns.fn(...)``. ``bin`` is named
runnable programs: one whole parameterized query per name. The two are read
by ROLE -- an export holds definitions and nothing else, a program is a query
-- and a manifest declaring neither is a project that only claims a namespace
and holds dependencies.

Beside it, ``sqlmpeg.lock`` records what the project INSTALLED: one entry per
package, either a registry entry pinning a version and the sha256 of the
content in the store, or a link entry naming a directory to read live. It is
machine-owned -- installing writes it, nobody hand-edits it.

:func:`discover` builds the set a compile resolves in, from three layers, the
first claim on a namespace winning:

1. the local manifest's own exports -- the project is a package,
2. the local lockfile -- what this project installed,
3. the global lockfile -- what a global install put on this machine.

All of it is OPTIONAL: none of the three found means no packages, and a query
compiles exactly as it did before this file existed.

This module reads and validates those two files and resolves a package to the
directory its files live in -- the project's own, a linked one, or one in
the content-addressed store (:mod:`sqlmpeg.store`). It does not parse SQL:
what those files DEFINE is :mod:`sqlmpeg.functions`' business, and keeping the
split that way is what lets ``functions.py`` import this module without a
cycle.

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
    "LinkEntry",
    "Lockfile",
    "Package",
    "PackageSet",
    "Program",
    "RegistryEntry",
    "discover",
    "find_lockfile",
    "find_manifest",
    "is_namespace",
    "read_lockfile",
    "read_manifest",
    "with_entry",
    "without_namespace",
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

# Unquoted identifiers fold to lowercase, so a namespace a query can write
# without quoting is a lowercase plain identifier.
_NAMESPACE_RE = re.compile(r"[a-z_][a-z0-9_]*")

# A program is typed as a command word, so its name is spelled like one: a
# lowercase letter, then lowercase letters, digits, '-' or '_'.
_PROGRAM_NAME_RE = re.compile(r"[a-z][a-z0-9_-]*")

# The four words a statement can begin with. Text starting with one is read as
# SQL, always; anything else typed where a query goes may name a program
# instead. So a program named for one of them could never be run by name.
STATEMENT_KEYWORDS = ("select", "copy", "create", "with")

# Characters that make a written path a pattern rather than a file.
_GLOB_CHARACTERS = "*?["

_REQUIRED = ("name", "version", "namespace")
_KNOWN = frozenset({*_REQUIRED, "bin", "description", "dependencies", "exports"})

_NAMESPACE_HINT = (
    "a namespace is a lowercase plain identifier: a letter or underscore, then "
    "letters, digits or underscores"
)
_MANIFEST_HINT = (
    'a manifest is one JSON object with "name", "version" and "namespace"; '
    '"exports" and "bin" are what the package provides, and both are optional'
)
_EXPORTS_HINT = 'exports is a list of glob patterns relative to the manifest, e.g. ["src/*.sql"]'
_BIN_HINT = (
    "bin maps a program name to one query file relative to the manifest, e.g. "
    '{"split-chapters": "queries/split.sql"}'
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
    return _NAMESPACE_RE.fullmatch(text) is not None


# Which of the three layers a package was found in. Factual, not a judgment:
# what to say about landing on "global" is the compiler's call, since only it
# knows the call site.
Layer = Literal["project", "local", "global"]


@dataclass(frozen=True)
class Program:
    """One named program: a query file the package ships to be run by name.

    Not an export: its text is a whole query, and the definition reader never
    opens it.
    """

    name: str
    path: Path


@dataclass(frozen=True)
class Package:
    """One package: the namespace it claims, and what it provides under it.

    `exports` are the files whose ``CREATE FUNCTION`` definitions the
    namespace answers for; `programs` are the named queries it ships. Either
    may be empty -- a manifest with neither claims a namespace and holds
    dependencies, which is what a consumer project's does.

    `linked` marks a package read straight out of a working directory rather
    than out of the store. Its files are whatever they are right now, so no
    digest pins them and no lockfile makes a build using it reproducible.
    """

    namespace: str
    name: str
    version: str
    root: Path
    exports: tuple[Path, ...]
    manifest: Path
    programs: tuple[Program, ...] = ()
    layer: Layer = "project"
    linked: bool = False

    def program(self, name: str) -> Program | None:
        """The program `name`, or None when this package ships no such program."""
        for program in self.programs:
            if program.name == name:
                return program
        return None


@dataclass(frozen=True)
class PackageSet:
    """The packages a compile may resolve a namespaced call in, by namespace.

    `in_project` is True when the query sits inside a project -- a manifest or
    a lockfile was found above it. It is what makes landing on the global
    layer worth warning about: outside a project, a globally installed package
    is the only thing there is to resolve against.
    """

    root: Path
    packages: dict[str, Package] = field(default_factory=dict)
    in_project: bool = True

    def get(self, namespace: str) -> Package | None:
        return self.packages.get(namespace)

    def namespaces(self) -> tuple[str, ...]:
        return tuple(sorted(self.packages))


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


def _text_field(data: dict[str, object], key: str, path: Path, text: str) -> str:
    value = data[key]
    if not isinstance(value, str) or not value.strip():
        raise _reject(
            path,
            f'"{key}" must be a non-empty string',
            line=_key_line(text, key),
            hint=_MANIFEST_HINT,
        )
    return value


def _namespace(data: dict[str, object], path: Path, text: str, *, line: int | None = None) -> str:
    """The namespace `data` claims, validated. `line` overrides where to point.

    A lockfile writes ``"namespace"`` once per entry, so a rejection about one
    entry has to be anchored by the caller that knows which entry it is.
    """
    if line is None:
        line = _key_line(text, "namespace")
    namespace = _text_field(data, "namespace", path, text)
    if _NAMESPACE_RE.fullmatch(namespace) is None:
        raise _reject(
            path,
            f"namespace {namespace!r} is not a plain identifier",
            line=line,
            hint=_NAMESPACE_HINT,
        )
    if namespace in RESERVED_NAMESPACES:
        reserved = ", ".join(sorted(RESERVED_NAMESPACES))
        raise _reject(
            path,
            f"namespace {namespace!r} is reserved",
            line=line,
            hint=f"{reserved} belong to sqlmpeg itself; pick another namespace",
        )
    return namespace


def _patterns(data: dict[str, object], path: Path, text: str) -> tuple[str, ...]:
    """The glob patterns ``exports`` names; empty when it names none."""
    if "exports" not in data:
        return ()
    line = _key_line(text, "exports")
    value = data["exports"]
    if not isinstance(value, list) or not value:
        raise _reject(path, '"exports" must be a non-empty list', line=line, hint=_EXPORTS_HINT)
    patterns: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise _reject(
                path, "every export pattern is a non-empty string", line=line, hint=_EXPORTS_HINT
            )
        patterns.append(entry)
    return tuple(patterns)


def _leaves_project(written: str) -> bool:
    """True for a written path that names something outside the manifest's directory."""
    relative = PurePosixPath(written.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        return True
    return re.match(r"[A-Za-z]:", written) is not None


def _export_files(root: Path, patterns: tuple[str, ...], path: Path, text: str) -> tuple[Path, ...]:
    """Every file the patterns name, in pattern then name order, deduplicated.

    A pattern that matches nothing is a rejection: it is a typo or a file that
    was moved, and silently exporting nothing would surface later as a
    missing function.
    """
    line = _key_line(text, "exports")
    found: list[Path] = []
    seen: set[Path] = set()
    for pattern in patterns:
        if _leaves_project(pattern):
            raise _reject(
                path,
                f"export pattern {pattern!r} leaves the project directory",
                line=line,
                hint="a pattern is relative to the manifest and stays under it",
            )
        written = PurePosixPath(pattern.replace("\\", "/"))
        try:
            matches = sorted(match for match in root.glob(str(written)) if match.is_file())
        except (OSError, ValueError) as err:
            raise _reject(
                path,
                f"export pattern {pattern!r} could not be read: {err}",
                line=line,
                hint=_EXPORTS_HINT,
            ) from err
        if not matches:
            raise _reject(
                path,
                f"export pattern {pattern!r} matches no file",
                line=line,
                hint=f"relative to {root}; check the path and the extension",
            )
        for match in matches:
            if match in seen:
                continue
            seen.add(match)
            found.append(match)
    return tuple(found)


def _program_file(
    written: str, name: str, root: Path, path: Path, line: int | None
) -> Path:
    """The one file program `name` names, validated: under the project, and there."""
    if _leaves_project(written):
        raise _reject(
            path,
            f"program '{name}' points at {written!r}, which leaves the project directory",
            line=line,
            hint="a program's file is relative to the manifest and stays under it",
        )
    if any(character in written for character in _GLOB_CHARACTERS):
        raise _reject(
            path,
            f"program '{name}' names the pattern {written!r}, not a file",
            line=line,
            hint="one name, one file; a glob belongs in exports",
        )
    file = root / Path(*PurePosixPath(written.replace("\\", "/")).parts)
    try:
        present = file.is_file()
    except (OSError, ValueError) as err:
        raise _reject(
            path,
            f"program '{name}': {written!r} could not be read: {err}",
            line=line,
            hint=_BIN_HINT,
        ) from err
    if not present:
        raise _reject(
            path,
            f"program '{name}' names no file: {written!r}",
            line=line,
            hint=f"relative to {root}; check the path and the extension",
        )
    return file


def _programs(data: dict[str, object], root: Path, path: Path, text: str) -> tuple[Program, ...]:
    """The programs ``bin`` declares, in written order; empty when it declares none."""
    if "bin" not in data:
        return ()
    at = _key_line(text, "bin")
    value = data["bin"]
    if not isinstance(value, dict):
        raise _reject(path, '"bin" must be a JSON object', line=at, hint=_BIN_HINT)
    if isinstance(value, _Object) and value.repeated:
        repeated = value.repeated[0]
        raise _reject(
            path,
            f"bin declares program {repeated!r} twice",
            line=_key_line(text, repeated) or at,
            hint="one name, one program; keep the one you meant",
        )
    programs: list[Program] = []
    for name, written in value.items():
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
        if not isinstance(written, str) or not written.strip():
            raise _reject(
                path,
                f"program '{name}' must name one file",
                line=line,
                hint=_BIN_HINT,
            )
        programs.append(Program(name=name, path=_program_file(written, name, root, path, line)))
    return tuple(programs)


def read_manifest(path: Path) -> Package:
    """Parse and validate one ``sqlmpeg.json`` into the package it declares.

    Raises ``SqlmpegError`` -- and nothing else -- on every rejection: an
    unreadable file, text that is not JSON, a missing or malformed key, a
    reserved namespace, an export pattern matching no file, a program that
    names no file.
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
        hint = _did_you_mean(key, sorted(_KNOWN)) or f"known keys: {', '.join(sorted(_KNOWN))}"
        raise _reject(path, f"unknown key {key!r}", line=_key_line(text, key), hint=hint)
    for key in _REQUIRED:
        if key not in data:
            raise _reject(path, f'is missing "{key}"', line=1, col=1, hint=_MANIFEST_HINT)

    return Package(
        namespace=_namespace(data, path, text),
        name=_text_field(data, "name", path, text),
        version=_text_field(data, "version", path, text),
        root=path.parent,
        exports=_export_files(path.parent, _patterns(data, path, text), path, text),
        manifest=path,
        programs=_programs(data, path.parent, path, text),
    )


# -- reading one lockfile --------------------------------------------------

# Bump on any change to the lockfile's shape. Another version's file is
# rejected rather than read optimistically: installing rewrites the lockfile,
# and guessing at a shape would resolve a call against content nobody pinned.
LOCK_FORMAT_VERSION = 1

_LOCK_HINT = 'a lockfile is one JSON object with "format_version", "reproducible" and "packages"'

_LOCK_REQUIRED = ("format_version", "reproducible", "packages")
_LOCK_KNOWN = frozenset({*_LOCK_REQUIRED, "not_reproducible_because"})

_REGISTRY_KEYS = ("kind", "name", "version", "namespace", "sha256", "store")
_LINK_KEYS = ("kind", "namespace", "path")
_KINDS = ("link", "registry")


@dataclass(frozen=True)
class RegistryEntry:
    """A package installed from the registry: a version, and content pinned by digest."""

    namespace: str
    name: str
    version: str
    sha256: str
    store: str


@dataclass(frozen=True)
class LinkEntry:
    """A package read live out of a directory: no version, no digest.

    That is not an omission. A link exists so edits to that directory land in
    the next compile, which is exactly what a digest cannot survive.
    """

    namespace: str
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

    Entries repeat their keys, so a rejection anchored on ``"namespace"``
    would point at the first entry whatever entry it is about; the namespace's
    own text is what tells them apart.
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
    line = _value_line(text, data.get("namespace"))
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
    namespace = _namespace(data, path, text, line=line)
    if kind == "link":
        return LinkEntry(namespace=namespace, path=_text_field(data, "path", path, text))
    return RegistryEntry(
        namespace=namespace,
        name=_text_field(data, "name", path, text),
        version=_text_field(data, "version", path, text),
        sha256=_text_field(data, "sha256", path, text),
        store=_text_field(data, "store", path, text),
    )


def _entries(data: dict[str, object], path: Path, text: str) -> tuple[LockEntry, ...]:
    line = _key_line(text, "packages")
    raw = data["packages"]
    if not isinstance(raw, list):
        raise _reject(path, '"packages" must be a list', line=line, hint=_LOCK_HINT)
    entries: list[LockEntry] = []
    claimed: set[str] = set()
    for index, element in enumerate(raw):
        entry = _entry(element, path, text, index)
        if entry.namespace in claimed:
            raise _reject(
                path,
                f"two packages claim namespace '{entry.namespace}'",
                line=_value_line(text, entry.namespace),
                hint="one namespace, one package; install the one you meant to keep",
            )
        claimed.add(entry.namespace)
        entries.append(entry)
    return tuple(entries)


def read_lockfile(path: Path) -> Lockfile:
    """Parse and validate one ``sqlmpeg.lock`` into the packages it pins.

    Raises ``SqlmpegError`` -- and nothing else -- on every rejection: an
    unreadable file, text that is not JSON, another format version, a
    malformed entry, two entries claiming one namespace, or a file that claims
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
            f"claims to be reproducible while linking '{linked[0].namespace}' to a directory",
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
    namespace: str,
    description: str | None = None,
    exports: Sequence[str] = (),
    programs: Mapping[str, str] | None = None,
    dependencies: Mapping[str, str] | None = None,
) -> None:
    """Write the ``sqlmpeg.json`` declaring this package.

    Only the three required keys are always written; an optional one is left
    out entirely rather than written empty, since an empty ``exports`` is a
    rejection and an empty ``bin`` says nothing.
    """
    payload: dict[str, object] = {"name": name, "version": version, "namespace": namespace}
    if description:
        payload["description"] = description
    if exports:
        payload["exports"] = list(exports)
    if programs:
        payload["bin"] = dict(programs)
    if dependencies:
        payload["dependencies"] = dict(dependencies)
    _write_atomically(path, _rendered(payload))


def _entry_payload(entry: LockEntry) -> dict[str, object]:
    """One entry as the lockfile writes it, keys in the order the reader lists them."""
    if isinstance(entry, LinkEntry):
        return {"kind": "link", "namespace": entry.namespace, "path": entry.path}
    return {
        "kind": "registry",
        "name": entry.name,
        "version": entry.version,
        "namespace": entry.namespace,
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


def with_entry(entries: Sequence[LockEntry], entry: LockEntry) -> tuple[LockEntry, ...]:
    """`entries` with `entry` in place of whatever held its namespace, else appended.

    One namespace, one package: installing or linking over a namespace already
    claimed replaces the claim rather than adding a second one the reader
    would refuse.
    """
    if all(held.namespace != entry.namespace for held in entries):
        return (*entries, entry)
    return tuple(entry if held.namespace == entry.namespace else held for held in entries)


def without_namespace(entries: Sequence[LockEntry], namespace: str) -> tuple[LockEntry, ...]:
    """`entries` without the one claiming `namespace`, the rest in order."""
    return tuple(held for held in entries if held.namespace != namespace)


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


def _linked_root(entry: LinkEntry, lock: Lockfile) -> Path:
    """The directory a link entry names, relative to the lockfile holding it."""
    try:
        return (lock.path.parent / Path(entry.path)).resolve()
    except (OSError, ValueError) as err:
        raise _reject(
            lock.path,
            f"package '{entry.namespace}': {entry.path!r} is not a directory path",
            hint="a link names the directory the package is developed in",
        ) from err


def _manifest_of(root: Path, namespace: str, lock: Lockfile, missing: str) -> Path:
    """The manifest a locked package is read through, or a rejection naming `missing`."""
    manifest = root / MANIFEST_NAME
    try:
        present = manifest.is_file()
    except (OSError, ValueError):
        present = False
    if not present:
        raise _reject(
            lock.path,
            f"package '{namespace}': {root} holds no {MANIFEST_NAME}",
            hint=missing,
        )
    return manifest


def _same(entry_value: str, found: str, field_name: str, namespace: str, lock: Lockfile) -> None:
    """Reject a lockfile entry the package it points at disagrees with."""
    if entry_value == found:
        return
    raise _reject(
        lock.path,
        f"package '{namespace}': the lockfile records {field_name} {entry_value!r} and the "
        f"package says {found!r}",
        hint="the package changed since it was installed; install or link it again",
    )


def _linked_package(entry: LinkEntry, lock: Lockfile, layer: Layer) -> Package:
    """A link resolved: the local layer again, rooted somewhere else.

    Same `read_manifest`, so a linked package is validated exactly as the
    project's own is -- and its exports are read on every compile, which is
    what makes an edit show up without reinstalling.
    """
    root = _linked_root(entry, lock)
    manifest = _manifest_of(
        root, entry.namespace, lock, "link the directory again, or restore its manifest"
    )
    package = read_manifest(manifest)
    _same(entry.namespace, package.namespace, "namespace", entry.namespace, lock)
    return replace(package, layer=layer, linked=True)


def _stored_package(entry: RegistryEntry, lock: Lockfile, layer: Layer) -> Package:
    """A registry entry resolved: the store directory its digest names, verified."""
    try:
        root = store.load(entry.name, entry.store, entry.sha256)
    except SqlmpegError as err:
        # Renamed onto the lockfile: that is the file the reader has open, not
        # a path under a cache directory they never chose.
        raise _reject(lock.path, err.message, hint=err.hint) from err
    manifest = _manifest_of(
        root, entry.namespace, lock, "the stored content is not a package; install it again"
    )
    package = read_manifest(manifest)
    _same(entry.namespace, package.namespace, "namespace", entry.namespace, lock)
    _same(entry.name, package.name, "name", entry.namespace, lock)
    _same(entry.version, package.version, "version", entry.namespace, lock)
    return replace(package, layer=layer)


def _add_layer(packages: dict[str, Package], lock: Lockfile | None, layer: Layer) -> None:
    """Add `lock`'s packages under the namespaces no earlier layer claimed."""
    if lock is None:
        return
    for entry in lock.entries:
        if entry.namespace in packages:  # first match wins, layer by layer
            continue
        if isinstance(entry, LinkEntry):
            packages[entry.namespace] = _linked_package(entry, lock, layer)
        else:
            packages[entry.namespace] = _stored_package(entry, lock, layer)


def _global_lockfile(local: Path | None) -> Lockfile | None:
    """The machine-wide lockfile, or None when nothing was installed globally."""
    path = store.global_lock_path()
    try:
        if not path.is_file() or (local is not None and path == local):
            return None
    except (OSError, ValueError):
        return None
    return read_lockfile(path)


def discover(start: Path | str | None = None) -> PackageSet | None:
    """The package set for a query written in `start`, or None with nothing to resolve in.

    `start` is a directory or a query file's path; None means the working
    directory, which is the CLI's answer for a query typed on the command
    line.

    Three layers, the first claim on a namespace winning: the project's own
    manifest, then its lockfile, then the machine-wide one. The layering lives
    here and nowhere else -- what the compiler gets is one namespace to one
    package, with no idea which layer answered.

    Raises ``SqlmpegError`` for a manifest or lockfile that is found but
    malformed, or for a locked package the store or the linked directory
    cannot produce.
    """
    base = Path(start) if start is not None else Path.cwd()
    manifest = find_manifest(base)
    local = find_lockfile(base)
    packages: dict[str, Package] = {}
    if manifest is not None:
        project = read_manifest(manifest)
        packages[project.namespace] = project
    _add_layer(packages, read_lockfile(local) if local is not None else None, "local")
    _add_layer(packages, _global_lockfile(local), "global")

    in_project = manifest is not None or local is not None
    if not in_project and not packages:
        return None
    root = manifest.parent if manifest is not None else local.parent if local is not None else base
    return PackageSet(root=root, packages=packages, in_project=in_project)
