"""The package store: installed package content, addressed by its archive's sha256.

A package is not only SQL -- a wasm filter ships a binary -- so what travels
between a registry and a client is one compressed archive, and the digest a
lockfile pins is over that archive's bytes. Hashing what travels is what lets
a bad download be thrown away without being opened: bytes in, hash, compare to
the pin, discard. Nothing unverified reaches the extractor.

Three things live here. :func:`pack` builds a package directory into a
deterministic gzipped tar, so the same content produces the same bytes and so
the same digest on any machine. :func:`unpack` verifies bytes against a digest
and writes what they hold into the store. :func:`load` turns a lockfile entry
back into a directory to read.

Layout follows the registry's disk cache (`registry.py`): everything under
``~/.cache/sqlmpeg/``, :func:`_cache_dir` the only place that names the home
directory (and the seam a test redirects), and a format version -- here the
first component of every store path, so content written by a future layout
cannot be read as this one's.

One discipline differs, and the difference matters. The registry cache is an
OPTIMIZATION: every failure there is swallowed and the answer rebuilt from
ffmpeg. A store entry is the only copy of what a lockfile pinned, so an entry
that is missing, unreadable or written by another layout is a rejection naming
the package -- never a fall back to some other content.

Reads out of the store do not hash anything. The digest did its work at the
boundary where the bytes were untrusted; a store entry is the user's own cache
under their own home, on the near side of that boundary.

Verified is not the same as safe to extract: a digest proves an archive is the
one that was published, not that its members behave. The extractor takes
regular files and directories under the destination and nothing else -- no
absolute paths, no ``..``, no links, no devices -- with a member count and an
uncompressed size cap.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import re
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from .errors import ErrorCode, SqlmpegError

__all__ = [
    "STORE_FORMAT",
    "entry_path",
    "global_lock_path",
    "load",
    "pack",
    "store_dir",
    "unpack",
]

# The store layout's version, and the first component of every store path.
# Bump it on any change to what a store directory holds: an entry written by
# another version is rejected, never guessed at.
STORE_FORMAT = "v1"

_SHA256_RE = re.compile(r"[0-9a-f]{64}")

_REINSTALL_HINT = "install the package again to put its content back in the store"

_ARCHIVE_HINT = "the archive is not a package this sqlmpeg will unpack; report it to its author"

# Fixed modes and ownership, so the same content packs to the same bytes
# whatever the umask and whoever is packing.
_FILE_MODE = 0o644
_DIR_MODE = 0o755

# What an archive may hold. A package is SQL plus at most a filter binary, so
# both sit far above anything legitimate and still bound what a hostile
# archive costs: extraction stops at the member that crosses one.
_MAX_MEMBERS = 4096
_MAX_UNPACKED_BYTES = 64 * 1024 * 1024


def _cache_dir() -> Path:
    try:
        return Path.home() / ".cache" / "sqlmpeg"
    except RuntimeError:  # pragma: no cover -- no resolvable home directory
        return Path(tempfile.gettempdir()) / "sqlmpeg-cache"


def store_dir() -> Path:
    """The root every stored package sits under."""
    return _cache_dir() / "packages"


def global_lock_path() -> Path:
    """The machine-wide lockfile, written by a global install.

    Beside the store rather than in a config directory: the two are written
    together, they are recovered together by reinstalling, and one home-
    directory seam covers both.
    """
    return _cache_dir() / "sqlmpeg.lock"


def entry_path(sha256: str) -> str:
    """Where content of this digest belongs, relative to :func:`store_dir`."""
    return f"{STORE_FORMAT}/{sha256[:2]}/{sha256}"


def _reject(message: str, hint: str) -> SqlmpegError:
    return SqlmpegError(ErrorCode.UNSUPPORTED_SQL, message, hint=hint)


def _require_digest(package: str, sha256: str) -> None:
    """Reject a digest that is not one, before it names a path or a comparison."""
    if _SHA256_RE.fullmatch(sha256) is None:
        raise _reject(
            f"package '{package}': {sha256!r} is not a sha256 digest",
            "a digest is 64 lowercase hex characters",
        )


# --------------------------------------------------------------------------
# packing
# --------------------------------------------------------------------------


def _entries(root: Path) -> list[tuple[str, Path]]:
    """Every directory and file under `root` as (relative posix path, path), in path order.

    Sorted, so a parent comes before what it holds and the member order is the
    tree's own order rather than the filesystem's.
    """
    found: list[tuple[str, Path]] = []
    for path in root.rglob("*"):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise _reject(
                f"{path} cannot be packed: a package holds regular files and directories only",
                "remove the link or special file from the package directory",
            )
        found.append((path.relative_to(root).as_posix(), path))
    return sorted(found)


def pack(root: Path) -> bytes:
    """The package directory `root` as one gzipped tar, byte-for-byte reproducible.

    Sorted member order, zeroed mtimes, uid/gid 0 with empty owner names, fixed
    modes and a gzip header with no timestamp: the same content packs to the
    same bytes on any machine, which is what keeps a published version's digest
    stable.

    Raises ``SqlmpegError`` for a tree holding anything but regular files and
    directories, and OSError if it cannot be read.
    """
    raw = io.BytesIO()
    with gzip.GzipFile(
        filename="", mode="wb", compresslevel=9, fileobj=raw, mtime=0
    ) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for relative, path in _entries(root):
                info = tarfile.TarInfo(relative)
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                if path.is_dir():
                    info.type = tarfile.DIRTYPE
                    info.mode = _DIR_MODE
                    archive.addfile(info)
                    continue
                data = path.read_bytes()
                info.type = tarfile.REGTYPE
                info.mode = _FILE_MODE
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    return raw.getvalue()


# --------------------------------------------------------------------------
# unpacking
# --------------------------------------------------------------------------


def _target(package: str, name: str, destination: Path) -> Path:
    """Where the member `name` writes to under `destination`, or a rejection.

    Absolute names, ``..`` anywhere, and separators the archive has no business
    using are refused by their shape; what survives is checked again against
    where it actually resolves.
    """
    written = PurePosixPath(name)
    shaped = bool(name) and not (
        name.startswith("/")
        or "\\" in name
        or ":" in name
        or ".." in written.parts
        or written.is_absolute()
    )
    if shaped:
        target = destination.joinpath(*written.parts)
        if _under(destination, target):
            return target
    raise _reject(
        f"package '{package}': its archive holds the member path {name!r}, which leaves "
        "the directory it would be extracted into",
        _ARCHIVE_HINT,
    )


def _under(destination: Path, target: Path) -> bool:
    """True when `target` resolves inside `destination`."""
    root = Path(os.path.realpath(destination))
    resolved = Path(os.path.realpath(target))
    return resolved != root and root in resolved.parents


def _extract(package: str, archive: bytes, destination: Path) -> None:
    """Write the members of `archive` under `destination`, refusing every other kind.

    One pass, checking as it goes: a hostile archive is abandoned at the member
    that crosses a cap rather than after it has been decompressed whole.
    """
    members = 0
    unpacked = 0
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as opened:
        for member in opened:
            members += 1
            if members > _MAX_MEMBERS:
                raise _reject(
                    f"package '{package}': its archive holds more than {_MAX_MEMBERS} members",
                    _ARCHIVE_HINT,
                )
            if not (member.isreg() or member.isdir()):
                raise _reject(
                    f"package '{package}': its archive holds {member.name!r}, which is neither "
                    "a regular file nor a directory",
                    _ARCHIVE_HINT,
                )
            target = _target(package, member.name, destination)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            unpacked += member.size
            if unpacked > _MAX_UNPACKED_BYTES:
                raise _reject(
                    f"package '{package}': its archive unpacks to more than "
                    f"{_MAX_UNPACKED_BYTES} bytes",
                    _ARCHIVE_HINT,
                )
            content = opened.extractfile(member)
            if content is None:  # pragma: no cover -- isreg() already ruled this out
                raise _reject(
                    f"package '{package}': its archive holds {member.name!r} with no content",
                    _ARCHIVE_HINT,
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as handle:
                shutil.copyfileobj(content, handle)


def unpack(package: str, archive: bytes, sha256: str) -> Path:
    """Verify `archive` against `sha256`, then write what it holds into the store.

    The bytes are hashed before any tar API sees them, so an archive that is
    not the one pinned is discarded unopened. What survives is extracted beside
    the destination and moved onto it, so an entry is complete or absent and
    never half of either; content already stored under that digest is that same
    content, so it is left alone.

    Returns the store directory the content now sits in. :func:`entry_path`
    names the same place for a lockfile entry.
    """
    _require_digest(package, sha256)
    found = hashlib.sha256(archive).hexdigest()
    if found != sha256:
        raise _reject(
            f"package '{package}': its archive hashes to {found}, and {sha256} was expected",
            "the download is not what was published; nothing was written",
        )
    destination = store_dir() / entry_path(sha256)
    if destination.is_dir():
        return destination
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(dir=destination.parent, prefix=f"{sha256[:16]}-"))
    except OSError as err:
        raise _reject(
            f"package '{package}': the store could not be written: {err.strerror or err}",
            "check that the cache directory exists and is writable",
        ) from err
    try:
        try:
            _extract(package, archive, staging)
        except (EOFError, OSError, tarfile.TarError) as err:
            raise _reject(
                f"package '{package}': its archive could not be unpacked: {err}",
                _ARCHIVE_HINT,
            ) from err
        try:
            os.replace(staging, destination)
        except OSError as err:
            # Another process storing the same digest got there first, which is
            # the same content by definition; anything else is a rejection.
            if not destination.is_dir():
                raise _reject(
                    f"package '{package}': the store could not be written: {err.strerror or err}",
                    "check that the cache directory exists and is writable",
                ) from err
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return destination


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


def _directory(package: str, stored: str) -> Path:
    """The store path `stored` names, checked for shape and format version."""
    written = PurePosixPath(stored.replace("\\", "/"))
    parts = written.parts
    if written.is_absolute() or ".." in parts or not parts:
        raise _reject(
            f"package '{package}': the store path {stored!r} leaves the store",
            "a store path is relative to the store directory and stays under it",
        )
    if parts[0] != STORE_FORMAT:
        raise _reject(
            f"package '{package}': the store path {stored!r} was written by store "
            f"format {parts[0]!r}, and this sqlmpeg reads {STORE_FORMAT!r}",
            _REINSTALL_HINT,
        )
    return store_dir().joinpath(*parts)


def load(package: str, stored: str, sha256: str) -> Path:
    """The store directory for `stored`, checked to be there.

    Nothing is hashed here. `sha256` is what the archive was verified against
    when it was installed, and the tree it unpacked to is read as it stands.
    The two are still checked against each other: an entry naming one digest
    while pointing at another's content would read content nothing pinned.

    `package` names the package in every rejection: the reader of the message
    has a lockfile in front of them, not a path under a cache directory.
    """
    _require_digest(package, sha256)
    directory = _directory(package, stored)
    if stored != entry_path(sha256):
        raise _reject(
            f"package '{package}': the store path {stored!r} is not where content of "
            f"digest {sha256} lives",
            _REINSTALL_HINT,
        )
    try:
        present = directory.is_dir()
    except OSError as err:  # pragma: no cover -- a path the OS refuses to stat
        raise _reject(
            f"package '{package}': its store directory could not be read: {err.strerror or err}",
            _REINSTALL_HINT,
        ) from err
    if not present:
        raise _reject(
            f"package '{package}': its content is not in the store at {directory}",
            _REINSTALL_HINT,
        )
    return directory
