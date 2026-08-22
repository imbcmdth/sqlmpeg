"""``sqlmpeg mcp``: the compiler as a stdio MCP server.

Two halves, split so the SDK is optional. :mod:`sqlmpeg.mcp.tools` is every
tool's body over the library API and imports nothing outside sqlmpeg;
:mod:`sqlmpeg.mcp.server` is the SDK wiring and is imported only when the
server actually starts. So the tools are unit-testable, and ``import
sqlmpeg.mcp`` works, on a machine that has no ``mcp`` package.

:func:`sdk_available` is what the CLI checks before starting: a missing SDK
is a message naming :data:`INSTALL_HINT`, never an ImportError traceback.
"""

from __future__ import annotations

import importlib.util

from .tools import (
    compile_query,
    dialect_prompt,
    explain_query,
    inspect_query,
    list_filters,
    run_query,
    validate_query,
)

__all__ = [
    "INSTALL_HINT",
    "compile_query",
    "dialect_prompt",
    "explain_query",
    "inspect_query",
    "list_filters",
    "run_query",
    "sdk_available",
    "serve",
    "validate_query",
]

INSTALL_HINT = (
    "the MCP server needs the 'mcp' package, an optional extra: install it "
    'with `pip install "sqlmpeg[mcp]"`'
)


def sdk_available() -> bool:
    """True if the ``mcp`` package is importable.

    Never raises: a broken install answers the same as a missing one, and
    either way the caller has a message to print rather than a traceback.
    """
    try:
        return importlib.util.find_spec("mcp") is not None
    except (ImportError, ValueError):
        return False


def serve(*, allow_run: bool = False) -> None:
    """Serve MCP over stdin/stdout until the client disconnects.

    `allow_run` also registers the ``run`` tool, which executes ffmpeg and
    writes files. Import the SDK here, not at module load: everything above
    works without it.
    """
    from .server import serve as _serve

    _serve(allow_run=allow_run)
