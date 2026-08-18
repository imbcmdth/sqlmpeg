"""ffmpeg/ffprobe binary discovery for sqlmpeg (RFC-010 "ffmpeg is required").

sqlmpeg requires BOTH ffmpeg and ffprobe. Discovery is PATH-first -- a system
install always wins, so a video engineer who already has ffmpeg set up never
gets a second one they didn't ask for -- falling back to the ``static-ffmpeg``
provisioning package (a default dependency, vetted at RFC-010 implementation
time: it is the only candidate found that ships ffprobe as well as ffmpeg,
downloading a static prebuilt pair on first use for win32/darwin/darwin_arm64/
linux/linux_arm64 from ``github.com/zackees/ffmpeg_bins`` and caching them
under its own package directory) only when nothing is on PATH.

``ffmpeg_path()`` / ``ffprobe_path()`` are the ONLY entry points other modules
should use to locate these binaries -- :mod:`sqlmpeg.registry`,
:mod:`sqlmpeg.probe` and :mod:`sqlmpeg.cli`'s ``run`` command all route
through here instead of calling ``shutil.which`` directly, so there is
exactly one place that knows about the provider fallback.

Both NEVER raise and return ``None`` when a binary is on neither PATH nor
delivered by the provider -- which, since the provider is a default
dependency, only happens if it failed (a broken install, an unwritable cache
directory, no network on its first-use download, ...). ``INSTALL_HINT`` is
the user-facing wording for that case.
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

    Lazy import: ``static_ffmpeg`` is a default dependency, but importing it
    -- and, on first use, having it download a ~95MB binary pair -- at module
    load time would pay that cost even on the (common) path where PATH
    already has both binaries. Never raises: an absent or broken provider
    package, a failed download, or any other provisioning error degrades to
    None, exactly like a PATH miss.
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
