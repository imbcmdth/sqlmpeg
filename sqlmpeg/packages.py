"""The PACKAGE registry client: the catalogue, and getting a package out of it.

Not :mod:`sqlmpeg.registry`, which is the FILTER registry -- what the local
ffmpeg binary supports. Two registries in one package would be a lasting
confusion, so the filter one keeps its name and this one is named for what it
serves: installable packages.

There is no service to talk to. The registry is a static site: ``index.json``
is the whole catalogue, ``p/<owner>/<name>.json`` is one package's detail
(every published version, and per version the archive's sha256 and size), and
``archives/<sha256>`` is the archive itself. The client fetches files over
HTTP with the stdlib and nothing else.

The base URL is one setting: :data:`DEFAULT_REGISTRY`, overridable by the
``SQLMPEG_REGISTRY`` environment variable. It may also name a local directory
holding those same files -- which is what a private registry behind any static
host, and every check in the test suite, is.

``index.json`` is cached on disk beside the store. That cache is an
OPTIMIZATION and follows the filter registry's discipline: a format version,
an atomic write, and every ``OSError`` swallowed -- a cache that cannot be
read or written costs a fetch and nothing else. It is the opposite of the
store's discipline, where a missing entry is the only copy of what a lockfile
pinned and so is a rejection. The difference is why the two live in different
modules. The one thing the cache is allowed to decide is what happens when the
registry cannot be reached at all: a cached catalogue is served instead, and
the caller is told it was.

Nothing here writes to stdout or stderr, and nothing raises anything but
``SqlmpegError``: a registry that is unreachable, unparseable, or serving an
archive that does not match its digest is a typed rejection naming the URL,
never a traceback.

Ordering matters at install: the archive's bytes are verified against the
digest the registry recorded before anything opens them (:func:`sqlmpeg.store.unpack`
does that), and the lockfile is written only after the content is in the
store. An install that cannot verify a hash leaves both untouched.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import store
from .errors import ErrorCode, SqlmpegError
from .project import (
    MANIFEST_NAME,
    RESERVED_NAMESPACES,
    LockEntry,
    RegistryEntry,
    add_dependency,
    is_namespace,
    read_lockfile,
    read_manifest,
    with_entry,
    write_lockfile,
)

__all__ = [
    "DEFAULT_REGISTRY",
    "REGISTRY_ENV",
    "Index",
    "Installed",
    "Listing",
    "Release",
    "base_url",
    "install",
    "load_index",
    "resolve",
    "search",
]

# Where packages come from unless the environment says otherwise.
DEFAULT_REGISTRY = "https://imbcmdth.github.io/sqlmpeg-registry"

REGISTRY_ENV = "SQLMPEG_REGISTRY"

# Every request gets one. A registry that accepts a connection and then says
# nothing must not hang a command forever.
TIMEOUT = 30.0

# The shape of the files this client reads. A registry publishing another
# version is refused rather than guessed at -- the alternative is installing
# content off a file whose keys mean something else.
FORMAT_VERSION = 1

# What a catalogue and one package's detail may weigh. Both are JSON the
# client parses whole, and neither has any business being large.
_MAX_INDEX_BYTES = 16 * 1024 * 1024
# The archive cap the store's own unpacked-size cap is the other half of.
_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024

_INDEX_NAME = "index.json"

# One package name: `<owner>/<name>`, both halves starting with a letter or
# digit. Checked before the name is ever part of a URL or a path, so a name
# off the network cannot name a directory above the one it belongs in.
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*")
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]*")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

_NAME_HINT = "a package name is <owner>/<name>, lowercase"
_REGISTRY_HINT = (
    f"set {REGISTRY_ENV} to another registry, or check the network connection"
)
_PUBLISHED_HINT = "run `sqlmpeg search` to see what is published"


def _reject(message: str, hint: str) -> SqlmpegError:
    return SqlmpegError(ErrorCode.UNSUPPORTED_SQL, message, hint=hint)


# --------------------------------------------------------------------------
# what the catalogue holds
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Listing:
    """One package as the catalogue describes it: what `search` prints and filters."""

    name: str
    version: str
    namespace: str
    description: str
    functions: tuple[str, ...]
    programs: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "namespace": self.namespace,
            "description": self.description,
            "functions": list(self.functions),
            "programs": list(self.programs),
        }


@dataclass(frozen=True)
class Index:
    """The catalogue, and where it came from.

    `cached` is true when the registry could not be reached and the disk cache
    answered instead; `unreachable` is what went wrong, for a caller with
    somewhere to say it. Both are facts about this fetch, not a judgment: the
    CLI prints a note, an MCP tool returns the fields.
    """

    base: str
    listings: tuple[Listing, ...]
    cached: bool = False
    unreachable: str | None = None

    def get(self, name: str) -> Listing | None:
        for listing in self.listings:
            if listing.name == name:
                return listing
        return None

    def names(self) -> tuple[str, ...]:
        return tuple(listing.name for listing in self.listings)


@dataclass(frozen=True)
class Release:
    """One published version: what to fetch, and what its bytes must hash to."""

    name: str
    version: str
    sha256: str
    size: int


@dataclass(frozen=True)
class Installed:
    """What one install did, for the caller that reports it.

    `namespace` is what the lockfile now maps to this package and `claimed` is
    what the package calls itself; they differ when it was installed under
    another namespace. `replaced` is whatever held that namespace before, or
    None.
    """

    release: Release
    namespace: str
    claimed: str
    replaced: LockEntry | None
    root: Path
    lock: Path
    manifest: Path | None
    downloaded: bool


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------


def base_url() -> str:
    """The registry to read, the environment's if it set one."""
    written = os.environ.get(REGISTRY_ENV)
    return written.strip() if written and written.strip() else DEFAULT_REGISTRY


def _local_root(base: str) -> Path | None:
    """The directory `base` names, or None when it is an HTTP registry.

    Anything that is not http(s) is read off the filesystem -- a ``file://``
    URL or a plain directory path. That is also the rule that keeps urllib
    from being handed a protocol nobody asked for: a registry is a static
    site or a directory of files, and there is no third kind.
    """
    if base.startswith(("http://", "https://")):
        return None
    if base.startswith("file://"):
        parsed = urllib.parse.urlparse(base)
        return Path(urllib.request.url2pathname(parsed.netloc + parsed.path))
    return Path(base)


def _where(relative: str) -> str:
    """The URL or path `relative` names under the current registry, for a message."""
    base = base_url()
    root = _local_root(base)
    if root is not None:
        return str(root.joinpath(*relative.split("/")))
    return f"{base.rstrip('/')}/{relative}"


def _unreachable(relative: str, reason: str) -> SqlmpegError:
    return _reject(
        f"the registry could not be read at {_where(relative)}: {reason}", _REGISTRY_HINT
    )


def _too_large(relative: str, limit: int) -> SqlmpegError:
    return _reject(
        f"the registry served more than {limit} bytes at {_where(relative)}",
        "that is not a file this registry publishes; check the base URL",
    )


def _fetch(relative: str, limit: int) -> bytes:
    """The bytes the registry serves at `relative`, or a typed rejection.

    Never a traceback and never unbounded: a failure names the URL it was
    reading, and a response over `limit` is abandoned rather than held.
    """
    base = base_url()
    root = _local_root(base)
    if root is not None:
        path = root.joinpath(*relative.split("/"))
        try:
            with open(path, "rb") as handle:
                content = handle.read(limit + 1)
        except OSError as err:
            raise _unreachable(relative, err.strerror or str(err)) from err
        if len(content) > limit:
            raise _too_large(relative, limit)
        return content

    url = f"{base.rstrip('/')}/{relative}"
    request = urllib.request.Request(url, headers={"Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            content = bytes(response.read(limit + 1))
    except urllib.error.HTTPError as err:
        raise _unreachable(relative, f"HTTP {err.code}") from err
    except (OSError, ValueError, urllib.error.URLError) as err:
        reason = getattr(err, "reason", None)
        raise _unreachable(relative, str(reason or err)) from err
    if len(content) > limit:
        raise _too_large(relative, limit)
    return content


# --------------------------------------------------------------------------
# parsing what came back
# --------------------------------------------------------------------------


def _malformed(relative: str, detail: str) -> SqlmpegError:
    return _reject(
        f"{_where(relative)} is not a registry file this sqlmpeg reads: {detail}",
        "the registry is serving something else, or a newer format; check the base URL",
    )


def _document(raw: bytes, relative: str) -> dict[str, object]:
    """`raw` as the one JSON object a registry file is, format version checked."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as err:
        raise _malformed(relative, "it is not JSON") from err
    if not isinstance(data, dict):
        raise _malformed(relative, "it is not a JSON object")
    if data.get("format_version") != FORMAT_VERSION:
        raise _malformed(
            relative,
            f"it is format version {data.get('format_version')!r} and this sqlmpeg "
            f"reads {FORMAT_VERSION}",
        )
    return data


def _text(data: dict[str, object], key: str, relative: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise _malformed(relative, f"{key!r} is not a non-empty string")
    return value


def _optional_text(data: dict[str, object], key: str, relative: str) -> str:
    value = data.get(key, "")
    if not isinstance(value, str):
        raise _malformed(relative, f"{key!r} is not a string")
    return value


def _names(data: dict[str, object], key: str, relative: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise _malformed(relative, f"{key!r} is not a list of strings")
    return tuple(str(item) for item in value)


def _objects(data: dict[str, object], key: str, relative: str) -> list[dict[str, object]]:
    value = data.get(key)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise _malformed(relative, f"{key!r} is not a list of objects")
    return [item for item in value if isinstance(item, dict)]


def _checked_name(name: str, relative: str) -> str:
    if _NAME_RE.fullmatch(name) is None:
        raise _malformed(relative, f"{name!r} is not a package name")
    return name


def _listing(raw: dict[str, object], relative: str) -> Listing:
    return Listing(
        name=_checked_name(_text(raw, "name", relative), relative),
        version=_text(raw, "version", relative),
        namespace=_text(raw, "namespace", relative),
        description=_optional_text(raw, "description", relative),
        functions=_names(raw, "functions", relative),
        programs=_names(raw, "programs", relative),
    )


def _parse_index(raw: bytes) -> tuple[Listing, ...]:
    data = _document(raw, _INDEX_NAME)
    return tuple(_listing(item, _INDEX_NAME) for item in _objects(data, "packages", _INDEX_NAME))


# --------------------------------------------------------------------------
# the index cache
# --------------------------------------------------------------------------

# Bump on any change to what the cache file holds. A mismatch -- including an
# absent key -- is discarded and refetched, never read as this shape.
_CACHE_FORMAT_VERSION = 1


def _cache_path() -> Path:
    """Where this registry's catalogue is cached.

    Beside the store, under the one home-directory seam both share; keyed by
    the base URL, so pointing sqlmpeg at another registry never reads the
    catalogue of the last one.
    """
    digest = hashlib.sha256(base_url().encode("utf-8")).hexdigest()[:16]
    return store._cache_dir() / f"packages-index-{digest}.json"


def _read_cache() -> bytes | None:
    """The catalogue this registry last served, or None. Purely an optimization."""
    path = _cache_path()
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("format_version") != _CACHE_FORMAT_VERSION or data.get("base") != base_url():
        return None
    index = data.get("index")
    if not isinstance(index, str):
        return None
    return index.encode("utf-8")


def _write_cache(raw: bytes) -> None:
    """Cache what the registry just served. Every filesystem failure is swallowed."""
    try:
        payload = json.dumps(
            {
                "format_version": _CACHE_FORMAT_VERSION,
                "base": base_url(),
                "index": raw.decode("utf-8"),
            }
        )
    except UnicodeDecodeError:
        return
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            dir=path.parent, prefix="packages-index-", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                file.write(payload)
            os.replace(temporary, path)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    except OSError:
        pass


def load_index() -> Index:
    """The catalogue, fetched fresh, or the cached one when the registry is unreachable.

    No expiry: the fetch is tried every time, because a stale catalogue would
    resolve an install to an older version than the one published. The cache
    answers only when there is nothing else to answer with, and the Index says
    so when it did.
    """
    base = base_url()
    try:
        raw = _fetch(_INDEX_NAME, _MAX_INDEX_BYTES)
    except SqlmpegError as err:
        cached = _read_cache()
        if cached is None:
            raise
        return Index(
            base=base, listings=_parse_index(cached), cached=True, unreachable=err.message
        )
    listings = _parse_index(raw)
    # Cached only after it parsed: a file this client cannot read is not worth
    # answering a later command with.
    _write_cache(raw)
    return Index(base=base, listings=listings)


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------


def search(index: Index, term: str | None = None) -> tuple[Listing, ...]:
    """The catalogue entries matching `term`, in the order the catalogue holds them.

    Matched case-insensitively against the name, the namespace, the
    description and the names of the functions the package exports. No term
    matches everything; a term matching nothing is an empty result, which is
    an answer and not a rejection.
    """
    if term is None or not term.strip():
        return index.listings
    needle = term.strip().lower()
    return tuple(listing for listing in index.listings if _matches(listing, needle))


def _matches(listing: Listing, needle: str) -> bool:
    fields = (listing.name, listing.namespace, listing.description, *listing.functions)
    return any(needle in field.lower() for field in fields)


# --------------------------------------------------------------------------
# resolving one package to one archive
# --------------------------------------------------------------------------


def _version_key(version: str) -> tuple[tuple[int, int, str], ...]:
    """Sort key for a version: dot-separated parts, numeric ones compared as numbers.

    Enough for the exact-pin world v0 lives in -- it orders 1.10.0 above 1.9.0,
    which string order does not -- and it never has to decide what a range
    means, because nothing here solves one.
    """
    parts: list[tuple[int, int, str]] = []
    for piece in version.split("."):
        if piece.isdigit():
            parts.append((0, int(piece), ""))
        else:
            parts.append((1, 0, piece))
    return tuple(parts)


def _requested(request: str) -> tuple[str, str | None]:
    """`<name>` or `<name>@<version>` split, both halves checked for shape."""
    name, separator, version = request.partition("@")
    if _NAME_RE.fullmatch(name) is None:
        raise _reject(f"{request!r} does not name a package", _NAME_HINT)
    if not separator:
        return name, None
    if _VERSION_RE.fullmatch(version) is None:
        raise _reject(
            f"{request!r} does not name a version",
            "a version is written after '@', e.g. broadcast/tracks@1.2.0",
        )
    return name, version


def _detail_path(name: str) -> str:
    return f"p/{name}.json"


def _release(raw: dict[str, object], name: str, relative: str) -> Release:
    sha256 = _text(raw, "sha256", relative)
    if _SHA256_RE.fullmatch(sha256) is None:
        raise _malformed(relative, f"{sha256!r} is not a sha256 digest")
    size = raw.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise _malformed(relative, "'size' is not a byte count")
    return Release(
        name=name, version=_text(raw, "version", relative), sha256=sha256, size=size
    )


def _did_you_mean(name: str, index: Index) -> str:
    """Whatever the catalogue holds whose name carries this one's, for a name it lacks."""
    stem = name.partition("/")[2] or name
    close = [held for held in index.names() if stem and stem in held]
    return f"did you mean {', '.join(sorted(close)[:3])}?" if close else _PUBLISHED_HINT


def resolve(index: Index, request: str) -> Release:
    """The published version `request` names: the highest one when it names none.

    The catalogue answers whether the package exists; the detail file answers
    which versions it has and what each one's archive must hash to. Exact
    pins only -- there is nothing to solve here, and nothing that does.
    """
    name, wanted = _requested(request)
    if index.get(name) is None:
        raise _reject(f"the registry has no package '{name}'", _did_you_mean(name, index))
    relative = _detail_path(name)
    data = _document(_fetch(relative, _MAX_INDEX_BYTES), relative)
    if _checked_name(_text(data, "name", relative), relative) != name:
        raise _malformed(relative, f"it describes another package than '{name}'")
    releases = [_release(raw, name, relative) for raw in _objects(data, "versions", relative)]
    if not releases:
        raise _reject(f"the registry publishes no version of '{name}'", _PUBLISHED_HINT)
    if wanted is None:
        return max(releases, key=lambda release: _version_key(release.version))
    for release in releases:
        if release.version == wanted:
            return release
    published = ", ".join(
        release.version
        for release in sorted(releases, key=lambda release: _version_key(release.version))
    )
    raise _reject(
        f"the registry has no version {wanted} of '{name}'", f"published: {published}"
    )


def fetch(release: Release) -> Path:
    """`release`'s content in the store, downloading it only when it is not there.

    The bytes are verified against the digest the registry recorded before
    anything opens them, which is :func:`sqlmpeg.store.unpack`'s contract: a
    download that does not match is discarded unopened and nothing is written.
    """
    relative = f"archives/{release.sha256}"
    archive = _fetch(relative, _MAX_ARCHIVE_BYTES)
    if len(archive) != release.size:
        raise _reject(
            f"package '{release.name}': the registry served {len(archive)} bytes at "
            f"{_where(relative)} and recorded {release.size}",
            "the download is not what was published; nothing was written",
        )
    return store.unpack(release.name, archive, release.sha256)


def stored(release: Release) -> Path | None:
    """Where `release` already sits in the store, or None when nothing does."""
    directory = store.store_dir() / store.entry_path(release.sha256)
    try:
        return directory if directory.is_dir() else None
    except OSError:  # pragma: no cover -- a path the OS refuses to stat
        return None


# --------------------------------------------------------------------------
# installing
# --------------------------------------------------------------------------


def _claimed(namespace: str) -> str:
    """The namespace an install writes, checked to be one this project may use."""
    if not is_namespace(namespace):
        raise _reject(
            f"{namespace!r} is not a namespace",
            "a namespace is a lowercase plain identifier: a letter or underscore, "
            "then letters, digits or underscores",
        )
    if namespace in RESERVED_NAMESPACES:
        reserved = ", ".join(sorted(RESERVED_NAMESPACES))
        raise _reject(
            f"namespace {namespace!r} is reserved",
            f"{reserved} belong to sqlmpeg itself; install it under another one",
        )
    return namespace


def _agrees(release: Release, root: Path) -> str:
    """The namespace the installed package claims, its name and version checked.

    The registry said what it was publishing and the package says what it is;
    a disagreement is caught here rather than at the next compile, where the
    lockfile reader would refuse an entry this command had just written.
    """
    package = read_manifest(root / MANIFEST_NAME)
    for recorded, found, field in (
        (release.name, package.name, "name"),
        (release.version, package.version, "version"),
    ):
        if recorded != found:
            raise _reject(
                f"package '{release.name}': the registry records {field} {recorded!r} and "
                f"the package says {found!r}",
                "the registry is serving an archive for another package; report it",
            )
    return package.namespace


def install(
    index: Index,
    request: str,
    *,
    lock: Path,
    manifest: Path | None = None,
    namespace: str | None = None,
) -> Installed:
    """Install `request` into the lockfile `lock`, and record it in `manifest`.

    In order: resolve the version, put its content in the store, then write
    the lockfile. Nothing is recorded before the content is there, so an
    install that fails anywhere leaves a project pinning only what it had.

    `namespace` installs the package under a name other than the one it
    claims. Two packages may both want ``tracks``, and the lockfile is where
    one name maps to one package; the manifest inside the archive is only the
    author's proposal. Whatever the namespace ends up being, an entry already
    holding it is replaced.
    """
    release = resolve(index, request)
    already = stored(release)
    root = already if already is not None else fetch(release)
    claimed = _agrees(release, root)
    written = _claimed(namespace) if namespace is not None else _claimed(claimed)

    entries = read_lockfile(lock).entries if lock.is_file() else ()
    replaced = next((held for held in entries if held.namespace == written), None)
    entry = RegistryEntry(
        namespace=written,
        name=release.name,
        version=release.version,
        sha256=release.sha256,
        store=store.entry_path(release.sha256),
    )
    write_lockfile(lock, with_entry(entries, entry))
    if manifest is not None:
        add_dependency(manifest, release.name, release.version)
    return Installed(
        release=release,
        namespace=written,
        claimed=claimed,
        replaced=replaced,
        root=root,
        lock=lock,
        manifest=manifest,
        downloaded=already is None,
    )
