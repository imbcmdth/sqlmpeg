"""The library half of the MCP server: one function per tool, no SDK import.

Everything a tool does happens here, over the library API -- never over
:mod:`sqlmpeg.cli`, whose handlers print to stdout, which a stdio server
cannot share with its protocol stream. Nothing in this module writes to
stdout or stderr; results come back as JSON-able dicts.

Rejections raise `SqlmpegError`, whose ``str()`` is the line-anchored message
the SDK turns into a failed tool call. :func:`validate_query` is the
deliberate exception: it NEVER raises and returns the error as data
(`SqlmpegError.to_dict()`, the shape ``docs/error-schema.json`` pins),
because it is the structured half of the repair loop.

What a compile has to say short of refusing comes back the same way, in a
``warnings`` array beside the answer: a package resolved from the machine-wide
lockfile instead of the project's, or one linked to a directory no lockfile
pins. Each warning is a code, a message, the namespace it is about, an anchor
and a hint. ``validate`` adds the array only when there is one, so a valid
query with nothing to say still answers with an empty object.

Two tools here are about packages rather than queries.
:func:`search_packages` reads the registry's catalogue;
:func:`install_package` downloads a package and writes it to the store and to
a project's lockfile, which is why the server registers it only when the
capability flag allows it.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

from .. import binaries
from .. import packages as packages_module
from .. import registry as registry_module
from ..compiler import classify, compile_commands, compile_table_sql
from ..emit import build_ffmpeg_commands, emit
from ..errors import ErrorCode, SqlmpegError
from ..execute import DEFAULT_TIMEOUT, execute
from ..ir import Graph, SinkUnit
from ..project import LOCKFILE_NAME, MANIFEST_NAME, PackageSet, discover, find_lockfile
from ..prompt import build_system_prompt
from ..table import TableResult, render_csv, render_table
from ..vars import substitute
from ..warnings import OnWarning, WarningLog

__all__ = [
    "STDERR_LIMIT",
    "compile_query",
    "dialect_prompt",
    "explain_query",
    "inspect_query",
    "install_package",
    "list_filters",
    "run_query",
    "search_packages",
    "validate_query",
]

# Longest ffmpeg stderr `run` returns; the tail is what carries the failure.
STDERR_LIMIT = 8000

_TABLE_MESSAGE = "this query has no media destination (no COPY, or every COPY is FORMAT csv)"
_TABLE_HINT = "call inspect for its rows -- only a media COPY ... TO compiles to ffmpeg"

_MEDIA_MESSAGE = "this query has a media COPY, so it has no rows to show"
_MEDIA_HINT = "call compile for its ffmpeg command"

_NO_PATH_MESSAGE = "a sink in this query names no destination path"
_NO_PATH_HINT = "give every COPY a TO '<path>'; TO STDOUT has no file to write"


def _prepare(query: str, variables: dict[str, str] | None) -> str:
    """The query text with its :name references substituted."""
    return substitute(query, dict(variables or {}))


def _packages(project: str | None) -> PackageSet | None:
    """The project `project` names, or None when the caller named none.

    An editor's agent knows which workspace a query belongs to and the server
    does not, so the path is an argument rather than something read out of the
    process's working directory.
    """
    return None if project is None else discover(Path(project))


def _sinks(graphs: list[Graph]) -> list[SinkUnit]:
    return [unit for graph in graphs for unit in graph.sinks]


def _is_table_query(
    text: str, packages: PackageSet | None = None, on_warning: OnWarning | None = None
) -> bool:
    """True if `text` succeeds as a table/csv query.

    A table query compiles through its own lenient pipeline, so "compiles =
    valid" still holds for one ``compile_commands`` rejected.
    """
    try:
        is_table_capable, _has_copy = classify(text, packages=packages, on_warning=on_warning)
    except SqlmpegError:
        return False
    if not is_table_capable:
        return False
    try:
        compile_table_sql(text, packages=packages, on_warning=on_warning)
    except SqlmpegError:
        return False
    return True


def _reported(warnings: WarningLog) -> list[dict[str, object]]:
    """What the compile had to say, as the tool result's ``warnings`` array.

    stdout is the protocol stream here, so a diagnostic has nowhere to be
    printed: it comes back as data, next to the answer it is about.
    """
    return [warning.to_dict() for warning in warnings.warnings]


def _warnings_only(warnings: WarningLog) -> dict[str, Any]:
    """`validate`'s success answer: empty, unless there is something to say.

    The tool's contract is that a valid query returns nothing; a warning is
    not an error, so what tells the two apart is the ``code`` an error
    carries, which this never has.
    """
    reported = _reported(warnings)
    return {"warnings": reported} if reported else {}


def _table_error() -> SqlmpegError:
    return SqlmpegError(ErrorCode.UNSUPPORTED_SQL, _TABLE_MESSAGE, line=1, col=1, hint=_TABLE_HINT)


def compile_query(
    query: str, variables: dict[str, str] | None = None, project: str | None = None
) -> dict[str, Any]:
    """The ffmpeg command(s) `query` compiles to."""
    text = _prepare(query, variables)
    packages = _packages(project)
    warnings = WarningLog()
    try:
        graphs = compile_commands(text, packages=packages, on_warning=warnings)
    except SqlmpegError:
        # A query with no streaming representation fails here; table mode is
        # the fallback, tried only for a query that could BE one. If it is,
        # the refusal names the tool that handles it.
        if _is_table_query(text, packages, warnings):
            raise _table_error() from None
        raise
    # A bare SELECT compiles, but names no destination -- compile never
    # invents one, so it is the same refusal as above.
    if any(unit.path is None for unit in _sinks(graphs)):
        raise _table_error()

    emitted = [emit(graph) for graph in graphs]
    commands: list[list[str]] = []
    for e in emitted:
        commands += build_ffmpeg_commands(e)
    return {
        "commands": commands,
        "filter_complex": [e.filter_complex for e in emitted],
        "outputs": [unit.path for unit in _sinks(graphs) if unit.path is not None],
        # loudnorm2: the first command measures, and the next carries
        # ${SQLMPEG_LN_*} placeholders only `run` fills in.
        "needs_measurement": any(bool(e.measure_filter_complex) for e in emitted),
        "warnings": _reported(warnings),
    }


def validate_query(
    query: str, variables: dict[str, str] | None = None, project: str | None = None
) -> dict[str, Any]:
    """Empty if `query` compiles, else the typed error object. Never raises.

    A query that compiled with something to say answers with a ``warnings``
    array and no ``code``; an error always carries one.
    """
    text: str | None = None
    packages: PackageSet | None = None
    compiling = False
    warnings = WarningLog()
    try:
        text = _prepare(query, variables)
        # Inside the try: a malformed manifest is a rejection like any other,
        # and this tool returns every rejection as data.
        packages = _packages(project)
        compiling = True
        compile_commands(text, packages=packages, on_warning=warnings)
    except SqlmpegError as err:
        # The table fallback answers only for a query that failed to COMPILE.
        # An undefined :name or a malformed manifest failed before that, and
        # is the answer itself.
        if compiling and text is not None and _is_table_query(text, packages, warnings):
            return _warnings_only(warnings)
        return err.to_dict()
    except Exception as err:  # no input may make a validate call fail
        return SqlmpegError(
            ErrorCode.INTERNAL,
            f"internal error while validating ({err.__class__.__name__}: {err})",
            line=1,
            col=1,
            hint="please report this query as a bug",
        ).to_dict()
    return _warnings_only(warnings)


def explain_query(
    query: str, variables: dict[str, str] | None = None, project: str | None = None
) -> dict[str, Any]:
    """The IR graph `query` compiles to, one per ffmpeg command."""
    warnings = WarningLog()
    graphs = compile_commands(
        _prepare(query, variables), packages=_packages(project), on_warning=warnings
    )
    return {"graphs": [graph.to_dict() for graph in graphs], "warnings": _reported(warnings)}


def _row_text(result: TableResult) -> list[list[str]]:
    """Every cell as the text a table prints, through the CSV renderer.

    The renderers are the only public path to a cell's text, and a stream,
    record or array cell has no JSON form of its own.
    """
    return list(csv.reader(io.StringIO(render_csv(result, header=False))))


def inspect_query(
    query: str, variables: dict[str, str] | None = None, project: str | None = None
) -> dict[str, Any]:
    """The rows of a table query: what tracks, chapters, cues or attachments a file has."""
    text = _prepare(query, variables)
    packages = _packages(project)
    warnings = WarningLog()
    is_table_capable, _has_copy = classify(text, packages=packages, on_warning=warnings)
    if not is_table_capable:
        raise SqlmpegError(
            ErrorCode.UNSUPPORTED_SQL, _MEDIA_MESSAGE, line=1, col=1, hint=_MEDIA_HINT
        )
    sinks = compile_table_sql(text, packages=packages, on_warning=warnings)
    return {
        "warnings": _reported(warnings),
        "results": [
            {
                "columns": list(sink.result.columns),
                "rows": _row_text(sink.result),
                "text": render_table(sink.result),
                "csv": sink.csv,
                "path": sink.path,
            }
            for sink in sinks
        ]
    }


def _matches(needle: str, name: str, doc: str) -> bool:
    return needle in name.lower() or needle in doc.lower()


def list_filters(pattern: str | None = None) -> dict[str, Any]:
    """What the LOCAL ffmpeg reports, optionally narrowed to a substring."""
    registry = registry_module.load()
    needle = (pattern or "").lower()

    filters: list[dict[str, Any]] = []
    for name in registry.names():
        f = registry.get(name)
        if f is None or not _matches(needle, name, f.doc):
            continue
        filters.append(
            {
                "name": name,
                "inputs": list(f.inputs),
                "output": f.output,
                "timeline": f.timeline,
                "doc": f.doc,
            }
        )

    sources: list[dict[str, Any]] = []
    for name in registry.source_names():
        s = registry.get_source(name)
        if s is None or not _matches(needle, name, s.doc):
            continue
        sources.append({"name": name, "output": s.output, "doc": s.doc})

    result: dict[str, Any] = {
        "available": registry.available(),
        "source": registry.source,
        "filters": filters,
        "sources": sources,
    }
    # An exact name also gets that filter's options, the `name => value`
    # surface. They cost an `ffmpeg -help filter=X`, so never for a listing.
    options = registry.options(pattern) if pattern is not None else None
    if options is not None:
        result["options"] = [
            {
                "name": o.name,
                "type": o.type,
                "doc": o.doc,
                "minimum": o.minimum,
                "maximum": o.maximum,
                "default": o.default,
                "constants": list(o.constants),
            }
            for o in options.values()
            if not o.unusable
        ]
    return result


def _tail(text: str) -> str:
    """The last `STDERR_LIMIT` characters of `text`, where a failure is stated."""
    if len(text) <= STDERR_LIMIT:
        return text
    return "[earlier output dropped]\n" + text[-STDERR_LIMIT:]


def run_query(
    query: str,
    variables: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    overwrite: bool = False,
    project: str | None = None,
) -> dict[str, Any]:
    """Compile `query` and run ffmpeg, writing the files its COPY names."""
    text = _prepare(query, variables)
    packages = _packages(project)
    warnings = WarningLog()
    is_table_capable, _has_copy = classify(text, packages=packages, on_warning=warnings)
    if is_table_capable:
        raise _table_error()

    graphs = compile_commands(text, packages=packages, on_warning=warnings)
    if any(unit.path is None for unit in _sinks(graphs)):
        raise SqlmpegError(
            ErrorCode.UNSUPPORTED_SQL, _NO_PATH_MESSAGE, line=1, col=1, hint=_NO_PATH_HINT
        )
    if binaries.ffmpeg_path() is None:
        raise SqlmpegError(
            ErrorCode.INTERNAL, "ffmpeg not found", line=1, col=1, hint=binaries.INSTALL_HINT
        )

    emitted = [emit(graph) for graph in graphs]
    # capture_stderr: a server owns no terminal for ffmpeg's progress lines.
    result = execute(emitted, timeout=timeout, overwrite=overwrite, capture_stderr=True)
    return {
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "measure_error": result.measure_error,
        "commands": [
            {"argv": c.argv, "exit_code": c.exit_code, "stderr": _tail(c.stderr)}
            for c in result.commands
        ],
        "outputs": [unit.path for unit in _sinks(graphs) if unit.path is not None],
        "warnings": _reported(warnings),
    }


def _reported_index(index: packages_module.Index) -> dict[str, Any]:
    """Where the catalogue came from, as fields rather than a printed note."""
    return {"registry": index.base, "cached": index.cached, "unreachable": index.unreachable}


def search_packages(term: str | None = None) -> dict[str, Any]:
    """The registry's catalogue, narrowed to `term`. Reads the network, writes nothing."""
    index = packages_module.load_index()
    return {
        **_reported_index(index),
        "packages": [listing.to_dict() for listing in packages_module.search(index, term)],
    }


def install_package(
    package: str, project: str, namespace: str | None = None
) -> dict[str, Any]:
    """Install `package` into the project at `project`: the store, then its lockfile.

    `project` is required and is never created: a directory with no lockfile
    at or above it is not a project, and inventing one is not this tool's
    call.
    """
    lock = find_lockfile(Path(project))
    if lock is None:
        raise SqlmpegError(
            ErrorCode.UNSUPPORTED_SQL,
            f"no {LOCKFILE_NAME} in {project} or above it",
            hint="run `sqlmpeg init` in that directory first; installing never creates one",
        )
    manifest = lock.parent / MANIFEST_NAME
    index = packages_module.load_index()
    installed = packages_module.install(
        index,
        package,
        lock=lock,
        manifest=manifest if manifest.is_file() else None,
        namespace=namespace,
    )
    return {
        **_reported_index(index),
        "name": installed.release.name,
        "version": installed.release.version,
        "namespace": installed.namespace,
        "claimed_namespace": installed.claimed,
        "sha256": installed.release.sha256,
        "downloaded": installed.downloaded,
        "lockfile": str(installed.lock),
        "manifest": None if installed.manifest is None else str(installed.manifest),
        "replaced": None if installed.replaced is None else installed.replaced.namespace,
    }


def dialect_prompt() -> str:
    """The system prompt describing the dialect, for this machine's ffmpeg."""
    return build_system_prompt(registry_module.load())
