"""The compiler pipeline: SQL text in, IR :class:`~sqlmpeg.ir.Graph` out.

``compile_sql`` chains the first three passes plus the split pass::

    parse -> resolve -> probe -> lower -> insert_splits

The returned graph is split-complete — every pad has exactly one consumer,
which is what :func:`sqlmpeg.emit.emit` expects.

Probing happens between resolve and lower: every
distinct input path is probed exactly once, results re-keyed by ALIAS for
:func:`sqlmpeg.lower.lower`, so two aliases over one file share a probe.
:func:`sqlmpeg.probe.probe` never raises and returns ``None`` for a URL, a
missing file, or a missing/failing ffprobe, so an unreadable input never
blocks a compile — it only loses subscript-bound validation and provenance
metadata. Probing is unconditional; there is no ``probe=False``.

The filter REGISTRY is the whole function surface: every call name
resolves in it and nowhere else. :func:`sqlmpeg.registry.load` never raises
and degrades to an empty registry when ffmpeg is missing, so a compile without
ffmpeg is not an error — it is one where every call name is UNKNOWN_FUNCTION.

Guardrail #7 lives here: no input, however malformed, may produce anything but
a compile result or a :class:`~sqlmpeg.errors.SqlmpegError`. Each pass carries
its own backstop; this one catches the rest (recursion limits, sqlglot
internals) as ``INTERNAL``, the code the fuzz corpus asserts never fires.
"""

from __future__ import annotations

from . import registry as registry_module
from .errors import ErrorCode, SqlmpegError
from .ir import Graph
from .lower import lower_commands, lower_table
from .parser import Resolved, parse, resolve
from .probe import ProbeResult
from .probe import probe as probe_path
from .split import insert_splits
from .table import TableSink

__all__ = ["classify", "compile_commands", "compile_sql", "compile_table_sql"]


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

    The FIRST command's graph, which is the whole query except for the one
    fan-out shape that compiles to a command sequence;
    :func:`compile_commands` returns every command's.

    Every input is probed opportunistically (see module docstring). The
    installed ffmpeg's filter set IS the function surface, so what
    compiles depends on what that ffmpeg reports; tests wanting a fixed,
    machine-independent surface call :func:`sqlmpeg.lower.lower` directly with
    a registry built from the captured snapshot.

    Raises ``SqlmpegError`` — and nothing else — on every rejection.
    """
    return compile_commands(text)[0]


def compile_commands(text: str) -> list[Graph]:
    """Compile SQL `text` into one split-complete IR graph per ffmpeg COMMAND.

    Usually one graph, a fan-out ``COPY ... TO (<expression>)`` included: its
    files become sink units of a single graph, one ffmpeg command with several
    outputs. The exception is a fan-out that trims and stream-copies every
    stream it maps, which stays one graph per file. Same probing and registry
    contract as :func:`compile_sql`.

    Raises ``SqlmpegError`` — and nothing else — on every rejection.
    """
    try:
        res = resolve(parse(text))
        probes = _probe_inputs(res)
        graphs = lower_commands(res, probes, registry=registry_module.load())
        return [insert_splits(graph) for graph in graphs]
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


def classify(text: str) -> tuple[bool, bool]:
    """``(is_table_capable, has_copy)`` for `text`.

    Cheap and static: parse + resolve only, no probing. ``is_table_capable``
    is True when `text` has no media destination -- a bare SELECT, or every
    COPY a ``FORMAT csv`` one. A bare SELECT is always table-capable by this
    check alone; the CLI decides from it whether to use
    :func:`compile_table_sql` or fall back to :func:`compile_sql`.

    Raises ``SqlmpegError`` on a query that does not even resolve.
    """
    res = resolve(parse(text))
    return all(sink.is_csv for sink in res.sinks), bool(res.sinks)


def compile_table_sql(text: str) -> list[TableSink]:
    """Compile SQL `text` into its printable table/csv result set(s).

    The sibling of :func:`compile_sql` for a table query: one
    :class:`~sqlmpeg.table.TableSink` per COPY, or one for a bare SELECT.
    Metadata columns and NULL-row gaps, both rejections under
    :func:`compile_sql`, are legal here — that is the whole difference. Inputs
    are probed opportunistically, same as :func:`compile_sql`.

    Raises ``SqlmpegError`` — and nothing else — on every rejection.
    """
    try:
        res = resolve(parse(text))
        probes = _probe_inputs(res)
        return lower_table(res, probes, registry=registry_module.load())
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
