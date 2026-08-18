"""ffmpeg/ffprobe binary discovery for sqlmpeg.

sqlmpeg requires BOTH ffmpeg and ffprobe. Discovery is PATH-first, so a system
install always wins; only when nothing is on PATH does it fall back to the
``static-ffmpeg`` provisioning package (a default dependency, chosen because
it is the only candidate that ships ffprobe as well as ffmpeg, fetching a
static prebuilt pair on first use and caching it under its own package dir).

``ffmpeg_path()`` / ``ffprobe_path()`` are the ONLY entry points other modules
may use to locate these binaries -- :mod:`sqlmpeg.registry`,
:mod:`sqlmpeg.probe` and :mod:`sqlmpeg.cli`'s ``run`` route through here
rather than ``shutil.which``, so one place knows about the provider fallback.

Both NEVER raise; they return ``None`` when a binary is on neither PATH nor
delivered by the provider (a broken install, unwritable cache dir, no network
on first use). ``INSTALL_HINT`` is the user-facing wording for that case.
"""

from __future__ import annotations

import shutil

INSTALL_HINT = (
    "the static-ffmpeg provisioner should have supplied ffmpeg/ffprobe "
    "automatically; check it installed correctly (pip show static-ffmpeg), "
    "or put a system ffmpeg/ffprobe on PATH yourself"
)


def _provider_paths() -> tuple[str, str] | None:
    """``(ffmpeg, ffprobe)`` from the ``static-ffmpeg`` provisioner, or None.

    Imported lazily: at module load it would cost a ~95MB first-use download
    even on the common path where PATH already has both binaries. Never
    raises -- an absent or broken provider, a failed download, or any other
    provisioning error degrades to None, exactly like a PATH miss.
    """
    try:
        from static_ffmpeg.run import get_or_fetch_platform_executables_else_raise
    except ImportError:
        return None
    try:
        ffmpeg, ffprobe = get_or_fetch_platform_executables_else_raise()
    except Exception:
        return None
    return ffmpeg, ffprobe


def ffmpeg_path() -> str | None:
    """The ffmpeg binary to use: PATH first, provider fallback, else None."""
    found = shutil.which("ffmpeg")
    if found is not None:
        return found
    provided = _provider_paths()
    return provided[0] if provided is not None else None


def ffprobe_path() -> str | None:
    """The ffprobe binary to use: PATH first, provider fallback, else None."""
    found = shutil.which("ffprobe")
    if found is not None:
        return found
    provided = _provider_paths()
    return provided[1] if provided is not None else None
