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
from pathlib import Path
from types import SimpleNamespace

import pytest

from sqlmpeg.registry import Registry, load_reference

SNAPSHOT_PATH = Path(__file__).resolve().parent / "data" / "reference_registry.json"


@functools.cache
def _reference_registry() -> Registry:
    return load_reference(SNAPSHOT_PATH)


@pytest.fixture(autouse=True)
def _snapshot_function_surface(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    if request.node.get_closest_marker("exec") is not None:
        return
    from sqlmpeg import compiler

    monkeypatch.setattr(
        compiler, "registry_module", SimpleNamespace(load=_reference_registry)
    )
