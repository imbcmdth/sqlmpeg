"""Tests for sqlmpeg.binaries.

All monkeypatched -- PATH via ``shutil.which``, the provider via
``sys.modules["static_ffmpeg.run"]`` (the lazy-import seam) -- so these never
touch a real ffmpeg, never import the real ``static_ffmpeg`` package, and
never risk its first-use download. Unmarked so they stay in the default
suite.
"""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

from sqlmpeg import binaries


def _install_fake_provider(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ffmpeg: str = "/provider/ffmpeg",
    ffprobe: str = "/provider/ffprobe",
) -> list[int]:
    """Fake ``static_ffmpeg.run`` module; returns a call counter list."""
    calls: list[int] = []
    fake_module = ModuleType("static_ffmpeg.run")

    def fake_get() -> tuple[str, str]:
        calls.append(1)
        return ffmpeg, ffprobe

    fake_module.get_or_fetch_platform_executables_else_raise = fake_get  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "static_ffmpeg.run", fake_module)
    monkeypatch.setitem(sys.modules, "static_ffmpeg", ModuleType("static_ffmpeg"))
    return calls


def _remove_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``from static_ffmpeg.run import ...`` raise ImportError.

    Setting a ``sys.modules`` entry to ``None`` is the documented way to
    force an ``ImportError`` on the next import of that name (PEP 328),
    regardless of whether the real package happens to be installed.
    """
    monkeypatch.setitem(sys.modules, "static_ffmpeg.run", None)
    monkeypatch.setitem(sys.modules, "static_ffmpeg", None)


# ---------------------------------------------------------------------------
# PATH wins
# ---------------------------------------------------------------------------


def test_ffmpeg_path_prefers_path_over_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binaries.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls = _install_fake_provider(monkeypatch)
    assert binaries.ffmpeg_path() == "/usr/bin/ffmpeg"
    assert calls == []  # provider never consulted -- PATH already answered


def test_ffprobe_path_prefers_path_over_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binaries.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls = _install_fake_provider(monkeypatch)
    assert binaries.ffprobe_path() == "/usr/bin/ffprobe"
    assert calls == []


# ---------------------------------------------------------------------------
# fallback consulted when PATH misses
# ---------------------------------------------------------------------------


def test_ffmpeg_path_falls_back_to_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binaries.shutil, "which", lambda name: None)
    _install_fake_provider(monkeypatch, ffmpeg="/cache/ffmpeg", ffprobe="/cache/ffprobe")
    assert binaries.ffmpeg_path() == "/cache/ffmpeg"


def test_ffprobe_path_falls_back_to_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binaries.shutil, "which", lambda name: None)
    _install_fake_provider(monkeypatch, ffmpeg="/cache/ffmpeg", ffprobe="/cache/ffprobe")
    assert binaries.ffprobe_path() == "/cache/ffprobe"


def test_provider_is_consulted_exactly_once_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binaries.shutil, "which", lambda name: None)
    calls = _install_fake_provider(monkeypatch)
    binaries.ffmpeg_path()
    assert calls == [1]


# ---------------------------------------------------------------------------
# both absent: never raises, returns None, INSTALL_HINT exists
# ---------------------------------------------------------------------------


def test_ffmpeg_path_is_none_when_path_and_provider_both_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(binaries.shutil, "which", lambda name: None)
    _remove_provider(monkeypatch)
    assert binaries.ffmpeg_path() is None


def test_ffprobe_path_is_none_when_path_and_provider_both_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(binaries.shutil, "which", lambda name: None)
    _remove_provider(monkeypatch)
    assert binaries.ffprobe_path() is None


def test_a_broken_provider_degrades_to_none_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A download failure, a locked cache dir, ... -- anything the provider
    package might raise -- must never propagate out of sqlmpeg."""
    monkeypatch.setattr(binaries.shutil, "which", lambda name: None)
    fake_module = ModuleType("static_ffmpeg.run")

    def _boom() -> tuple[str, str]:
        raise RuntimeError("network unreachable")

    fake_module.get_or_fetch_platform_executables_else_raise = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "static_ffmpeg.run", fake_module)
    monkeypatch.setitem(sys.modules, "static_ffmpeg", ModuleType("static_ffmpeg"))

    assert binaries.ffmpeg_path() is None
    assert binaries.ffprobe_path() is None


def test_install_hint_is_a_nonempty_string() -> None:
    assert isinstance(binaries.INSTALL_HINT, str)
    assert binaries.INSTALL_HINT.strip() != ""
    assert "static-ffmpeg" in binaries.INSTALL_HINT
