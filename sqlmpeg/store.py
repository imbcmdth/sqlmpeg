"""The package store: installed package content, addressed by its sha256.

A lockfile entry pins a digest and where under the store the content with that
digest lives; this module turns the pair back into a directory to read, and
refuses to hand one back that does not hash to what was pinned. Writing the
store is `sqlmpeg install`'s job, not this module's.

Layout follows the registry's disk cache (`registry.py`): everything under
``~/.cache/sqlmpeg/``, :func:`_cache_dir` the only place that names the home
directory (and the seam a test redirects), and a format version -- here the
first component of every store path, so content written by a future layout
cannot be read as this one's.

One discipline differs, and the difference matters. The registry cache is an
OPTIMIZATION: every failure there is swallowed and the answer rebuilt from
ffmpeg. A store entry is the only copy of what a lockfile pinned, so a blob
that is missing, unreadable, written by another layout, or whose content does
not hash to the recorded digest is a rejection naming the package -- never a
fall back to some other content.

The digest is over a DIRECTORY, since that is what a package is: every file
under it, in path order, each contributing its relative path, its length and
its bytes. Same input, same digest, on any machine.
"""

from __future__ import annotations

import hashlib
import re
import tempfile
from pathlib import Path, PurePosixPath

from .errors import ErrorCode, SqlmpegError

__all__ = ["STORE_FORMAT", "digest", "entry_path", "global_lock_path", "load", "store_dir"]

# The store layout's version, and the first component of every store path.
# Bump it on any change to what a store directory holds or how it is hashed:
# an entry written by another version is rejected, never guessed at.
STORE_FORMAT = "v1"

_SHA256_RE = re.compile(r"[0-9a-f]{64}")

_REINSTALL_HINT = "install the package again to put its content back in the store"


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


def _files(root: Path) -> list[tuple[str, Path]]:
    """Every file under `root` as (relative posix path, path), in path order."""
    found = [
        (path.relative_to(root).as_posix(), path) for path in root.rglob("*") if path.is_file()
    ]
    return sorted(found)


def digest(root: Path) -> str:
    """The sha256 of the directory `root`'s content.

    Raises OSError if the tree cannot be read; callers turn that into a
    rejection naming the package.
    """
    running = hashlib.sha256()
    for relative, path in _files(root):
        data = path.read_bytes()
        running.update(relative.encode("utf-8"))
        running.update(b"\0")
        running.update(str(len(data)).encode("ascii"))
        running.update(b"\0")
        running.update(data)
    return running.hexdigest()


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
    """The store directory for `stored`, verified to hash to `sha256`.

    `package` names the package in every rejection: the reader of the message
    has a lockfile in front of them, not a path under a cache directory.
    """
    if _SHA256_RE.fullmatch(sha256) is None:
        raise _reject(
            f"package '{package}': {sha256!r} is not a sha256 digest",
            "a digest is 64 lowercase hex characters",
        )
    directory = _directory(package, stored)
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
    try:
        found = digest(directory)
    except OSError as err:
        raise _reject(
            f"package '{package}': its store content could not be read: {err.strerror or err}",
            _REINSTALL_HINT,
        ) from err
    if found != sha256:
        raise _reject(
            f"package '{package}': its store content hashes to {found}, and the lockfile "
            f"pins {sha256}",
            "the stored content changed after it was installed; install it again",
        )
    return directory
