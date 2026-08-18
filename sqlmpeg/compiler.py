"""The compiler pipeline: SQL text in, IR :class:`~sqlmpeg.ir.Graph` out.

``compile_sql`` chains the first three passes plus the split pass::

    parse -> resolve -> probe -> lower -> insert_splits

The returned graph is split-complete, i.e. every pad has exactly one consumer,
which is what :func:`sqlmpeg.emit.emit` expects.

Probing (RFC-001 "Probing policy") happens here, between resolve and lower:
every distinct input path is probed exactly once and the results are re-keyed
by ALIAS for :func:`sqlmpeg.lower.lower` (two aliases over one file share a
single probe). :func:`sqlmpeg.probe.probe` never raises and returns ``None``
for a URL, a missing file, or a missing/failing ffprobe, so a compile is never
blocked by an unreadable input — it just loses the extra validation (subscript
bounds) and the provenance metadata that probing would have added. Probing is
unconditional (RFC-010): the old ``probe=False`` escape hatch is gone, since
opportunistic degradation on missing/unreadable inputs already gives every
byte-reproducible-offline use case it was for, with no separate mode to
maintain.

The filter REGISTRY (RFC-007) is the whole function surface, not an
opportunistic extra: every call name resolves in it and nowhere else.
:func:`sqlmpeg.registry.load` never raises and degrades to an empty registry
when ffmpeg is missing, so a compile without ffmpeg is not an error — it is a
compile in which every call name is UNKNOWN_FUNCTION.

Guardrail #7 lives here: no input, however malformed, may produce anything but
a compile result or a :class:`~sqlmpeg.errors.SqlmpegError`. Each pass already
carries its own backstop; this one catches anything that still slips through
(including recursion limits and sqlglot internals) and reports it as
``INTERNAL``, the code the fuzz corpus asserts never fires.
"""

from __future__ import annotations

from . import registry as registry_module
from .errors import ErrorCode, SqlmpegError
from .ir import Graph
from .lower import lower
from .parser import Resolved, parse, resolve
from .probe import ProbeResult
from .probe import probe as probe_path
from .split import insert_splits

__all__ = ["compile_sql"]


def _probe_inputs(res: Resolved) -> dict[str, ProbeResult | None]:
    """Probe each distinct input path once; return the results keyed by alias."""
    by_path: dict[str, ProbeResult | None] = {}
    by_alias: dict[str, ProbeResult | None] = {}
    for alias, index in res.sources.items():
        path = res.input_paths[index]
        if path not in by_path:
            by_path[path] = probe_path(path)
        by_alias[alias] = by_path[path]
    return by_alias


def compile_sql(text: str) -> Graph:
    """Compile SQL `text` into a split-complete IR graph.

    Every input is probed opportunistically -- there is no ``probe=False``
    escape hatch (RFC-010); a missing or unreadable input degrades the same
    way it always has (see module docstring).

    The installed ffmpeg's filter set IS the function surface (RFC-007): every
    call resolves against :func:`sqlmpeg.registry.load`, so what compiles
    depends on what that ffmpeg reports. Tests that want a fixed, machine-
    independent surface call :func:`sqlmpeg.lower.lower` directly with a
    registry built from the captured snapshot.

    Raises ``SqlmpegError`` — and nothing else — on every rejection.
    """
    try:
        res = resolve(parse(text))
        probes = _probe_inputs(res)
        return insert_splits(lower(res, probes, registry=registry_module.load()))
    except SqlmpegError:
        raise
    except RecursionError as err:
        raise SqlmpegError(
            ErrorCode.INTERNAL,
            "internal error while compiling (query nests too deeply)",
            line=1,
            col=1,
            hint="please report this query as a bug",
        ) from err
    except Exception as err:  # guardrail #7: no panics on user input
        raise SqlmpegError(
            ErrorCode.INTERNAL,
            f"internal error while compiling ({err.__class__.__name__}: {err})",
            line=1,
            col=1,
            hint="please report this query as a bug",
        ) from err
