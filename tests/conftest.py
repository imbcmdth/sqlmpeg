"""Suite-wide default: ``compile_sql`` resolves against the captured snapshot.

RFC-007 made the installed ffmpeg's filter set THE function surface, which
would quietly make every ``compile_sql``-based test depend on whichever
ffmpeg the machine happens to have -- and fail on a machine with none. CI
runs the default suite BEFORE installing ffmpeg, deliberately: the non-exec
tier must be deterministic on a bare machine, and
``tests/data/reference_registry.json`` exists for exactly that (RFC-007
"Offline compile": the snapshot serves golden tests, fuzzing, and offline
CI).

The seam is the compiler module's ``registry_module`` reference: swapping it
for a shim redirects ``compile_sql`` (and everything above it: the CLI, the
cookbook harness, the prompt example checks) without touching
``sqlmpeg.registry`` itself, so registry-introspection tests, direct
``lower()`` calls with hand-built registries, and ``shutil.which``
simulations all behave exactly as they would in production.

Exec-marked tests are exempt: they run their compiled commands through the
real ffmpeg, so they must compile against the real ffmpeg's own registry.
"""

from __future__ import annotations

import functools
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

from sqlmpeg import registry as registry_module
from sqlmpeg.registry import Registry, load_reference

SNAPSHOT_PATH = Path(__file__).resolve().parent / "data" / "reference_registry.json"


@functools.cache
def _reference_registry() -> Registry:
    return load_reference(SNAPSHOT_PATH)


@pytest.fixture
def pinned_ffmpeg() -> None:
    """Skip -- loudly, never fail -- unless the installed ffmpeg IS the
    snapshot's pinned build.

    A handful of exec tests compare the committed snapshot against the live
    binary (freshness, option order, diagnostic text). Those comparisons are
    only meaningful on the exact ffmpeg the snapshot was generated from;
    on any other version the differences are ordinary release drift, which
    is the situation sqlmpeg exists to handle, not a defect. Behavioral
    guarantees stay tested on EVERY version (the md5 positional-fidelity
    exec test carries them); what skips here is only the byte-level
    version-pinned redundancy. The skip also warns, so a CI run against a
    different ffmpeg reports the drift in the warnings summary without
    breaking the build.
    """
    live = registry_module.load()
    live.available()
    pinned = _reference_registry().snapshot_of
    installed = live._version_line
    if installed != pinned:
        message = (
            f"installed ffmpeg is not the snapshot's pinned build; "
            f"version-pinned snapshot checks skipped (informational, not a "
            f"failure). installed: {installed!r}, snapshot: {pinned!r}. "
            f"To refresh the pin, run scripts/gen_snapshot.py on the new "
            f"version and review the diff."
        )
        warnings.warn(message, stacklevel=1)
        pytest.skip(message)


@pytest.fixture(autouse=True)
def _snapshot_function_surface(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    if request.node.get_closest_marker("exec") is not None:
        return
    from sqlmpeg import cli, compiler

    monkeypatch.setattr(
        compiler, "registry_module", SimpleNamespace(load=_reference_registry)
    )
    # cli.py holds its own reference for the `prompt` subcommand's registry
    # (RFC-010 made the prompt registry-rendered, always) -- same shim, same
    # reason: the default tier is deterministic on a bare machine.
    monkeypatch.setattr(
        cli, "registry_module", SimpleNamespace(load=_reference_registry)
    )
