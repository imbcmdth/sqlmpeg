"""The project manifest: ``sqlmpeg.json``, and the packages a query may call into.

A directory holding a ``sqlmpeg.json`` is a PROJECT, and a project is itself a
PACKAGE: the manifest claims a namespace and lists the SQL sources that
namespace exports, so a query in ``queries/`` can call a function defined in
``src/``::

    { "name": "my-edits", "version": "0.1.0",
      "namespace": "me", "sources": ["src/*.sql"] }

:func:`discover` walks up from a directory to the filesystem root looking for
that file. A project is OPTIONAL: nothing found means no packages, and a query
compiles exactly as it did before this file existed.

This module reads and validates the manifest and resolves its source globs to
real paths. It does not parse SQL: what those files DEFINE is
:mod:`sqlmpeg.functions`' business, and keeping the split that way is what
lets ``functions.py`` import this module without a cycle.

Every rejection is a `SqlmpegError`, anchored on the manifest line where the
offending key is written when there is one to point at.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .errors import ErrorCode, SqlmpegError
from .parser import FILTER_NAMESPACE, MACRO_NAMESPACE

__all__ = [
    "MANIFEST_NAME",
    "RESERVED_NAMESPACES",
    "Package",
    "PackageSet",
    "discover",
    "find_manifest",
    "read_manifest",
]

MANIFEST_NAME = "sqlmpeg.json"

# A namespace becomes a call qualifier, so it may not be one the dialect
# already answers for: `ffmpeg.<filter>` and `sqlmpeg.<macro>` are resolved by
# lower, and `wasm` is held for the frei0r/wasm bridge.
WASM_NAMESPACE = "wasm"
RESERVED_NAMESPACES = frozenset({FILTER_NAMESPACE, MACRO_NAMESPACE, WASM_NAMESPACE})

# Unquoted identifiers fold to lowercase, so a namespace a query can write
# without quoting is a lowercase plain identifier.
_NAMESPACE_RE = re.compile(r"[a-z_][a-z0-9_]*")

_REQUIRED = ("name", "version", "namespace", "sources")
_KNOWN = frozenset({*_REQUIRED, "description", "dependencies"})

_NAMESPACE_HINT = (
    "a namespace is a lowercase plain identifier: a letter or underscore, then "
    "letters, digits or underscores"
)
_MANIFEST_HINT = (
    'a manifest is one JSON object with "name", "version", "namespace" and '
    '"sources"'
)
_SOURCES_HINT = 'sources is a list of glob patterns relative to the manifest, e.g. ["src/*.sql"]'


@dataclass(frozen=True)
class Package:
    """One package: the namespace it claims and the sources that namespace exports."""

    namespace: str
    name: str
    version: str
    root: Path
    sources: tuple[Path, ...]
    manifest: Path


@dataclass(frozen=True)
class PackageSet:
    """The packages a compile may resolve a namespaced call in, by namespace.

    Wave A holds at most one -- the project's own. The lockfile layers add
    more without changing what the compiler does with them.
    """

    root: Path
    packages: dict[str, Package] = field(default_factory=dict)

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


def _namespace(data: dict[str, object], path: Path, text: str) -> str:
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
    line = _key_line(text, "sources")
    value = data["sources"]
    if not isinstance(value, list) or not value:
        raise _reject(path, '"sources" must be a non-empty list', line=line, hint=_SOURCES_HINT)
    patterns: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise _reject(
                path, "every source pattern is a non-empty string", line=line, hint=_SOURCES_HINT
            )
        patterns.append(entry)
    return tuple(patterns)


def _source_files(root: Path, patterns: tuple[str, ...], path: Path, text: str) -> tuple[Path, ...]:
    """Every file the patterns name, in pattern then name order, deduplicated.

    A pattern that matches nothing is a rejection: it is a typo or a file that
    was moved, and silently exporting nothing would surface later as a
    missing function.
    """
    line = _key_line(text, "sources")
    found: list[Path] = []
    seen: set[Path] = set()
    for pattern in patterns:
        written = PurePosixPath(pattern.replace("\\", "/"))
        if written.is_absolute() or ".." in written.parts or re.match(r"[A-Za-z]:", pattern):
            raise _reject(
                path,
                f"source pattern {pattern!r} leaves the project directory",
                line=line,
                hint="a pattern is relative to the manifest and stays under it",
            )
        try:
            matches = sorted(match for match in root.glob(str(written)) if match.is_file())
        except (OSError, ValueError) as err:
            raise _reject(
                path,
                f"source pattern {pattern!r} could not be read: {err}",
                line=line,
                hint=_SOURCES_HINT,
            ) from err
        if not matches:
            raise _reject(
                path,
                f"source pattern {pattern!r} matches no file",
                line=line,
                hint=f"relative to {root}; check the path and the extension",
            )
        for match in matches:
            if match in seen:
                continue
            seen.add(match)
            found.append(match)
    return tuple(found)


def read_manifest(path: Path) -> Package:
    """Parse and validate one ``sqlmpeg.json`` into the package it declares.

    Raises ``SqlmpegError`` -- and nothing else -- on every rejection: an
    unreadable file, text that is not JSON, a missing or malformed key, a
    reserved namespace, a source pattern matching no file.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        raise _reject(path, f"could not be read: {err.strerror or err}") from err
    try:
        data = json.loads(text)
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
        sources=_source_files(path.parent, _patterns(data, path, text), path, text),
        manifest=path,
    )


# -- discovery -------------------------------------------------------------


def find_manifest(start: Path) -> Path | None:
    """The nearest ``sqlmpeg.json`` at or above `start`, or None at the root.

    The walk stops at the filesystem root; a project is optional, and finding
    none is the ordinary case, not a rejection.
    """
    try:
        current = Path(start).resolve()
        if current.is_file():
            current = current.parent
    except (OSError, ValueError):  # an unreadable or malformed path is not a project
        return None
    for directory in (current, *current.parents):
        candidate = directory / MANIFEST_NAME
        try:
            if candidate.is_file():
                return candidate
        except (OSError, ValueError):
            continue
    return None


def discover(start: Path | str | None = None) -> PackageSet | None:
    """The package set for a query written in `start`, or None outside a project.

    `start` is a directory or a query file's path; None means the working
    directory, which is the CLI's answer for a query typed on the command
    line. Raises ``SqlmpegError`` for a manifest that is found but malformed.
    """
    base = Path(start) if start is not None else Path.cwd()
    manifest = find_manifest(base)
    if manifest is None:
        return None
    package = read_manifest(manifest)
    return PackageSet(root=manifest.parent, packages={package.namespace: package})
