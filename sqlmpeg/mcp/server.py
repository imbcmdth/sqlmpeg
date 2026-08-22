"""The MCP SDK wiring: tool and resource registration, and the stdio loop.

The only module that imports ``mcp``. Every tool body is in
:mod:`sqlmpeg.mcp.tools`; what is here is the argument plumbing and the text
a model reads to choose a tool.

stdout belongs to the protocol. Nothing in sqlmpeg prints -- the library
raises `SqlmpegError` where the CLI would print, and no diagnostic has any
other channel -- and while ``stdio_server`` is serving it points file
descriptor 1 at stderr and serves the wire from a private duplicate, so a
stray write from any library or child process misses the protocol stream.
``run``'s ffmpeg children inherit that redirected descriptor, and their
stderr is captured into the tool result rather than written anywhere.

``run`` is registered only when the caller passes `allow_run`. The other
tools return text about a query; ``run`` writes files on model say-so, which
is a different trust posture and so a deliberate opt-in.
"""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from .. import __version__
from ..execute import DEFAULT_TIMEOUT
from . import tools

__all__ = ["build_server", "serve"]

DIALECT_URI = "sqlmpeg://dialect"

_INSTRUCTIONS = """\
sqlmpeg compiles Postgres-dialect SQL into ffmpeg commands: a FROM is an
input file, a column is a stream, a function call is a filter, and COPY ... TO
is the output file.

Read the sqlmpeg://dialect resource before writing a query -- it is the whole
grammar, generated for this machine's ffmpeg. When a query is rejected, call
validate to get the typed error (code, line, col, message, hint), fix what it
names, and validate again. Call filters to check that a filter and its options
exist in the local ffmpeg build rather than assuming.\
"""


# One function per tool below, named FOR the tool: the SDK reads each tool's
# name, argument schema and description straight off the function, so a
# rename here renames the tool.


def compile(
    query: str, vars: dict[str, str] | None = None, project: str | None = None
) -> dict[str, Any]:
    """Compile a query into the ffmpeg command(s) it runs as, without running them.

    Returns `commands` (each an argv list, run in order), `filter_complex`
    (the graph string per command), `outputs` (the files the query would
    write), and `needs_measurement` (true when the first command measures for
    the next, whose ${SQLMPEG_LN_*} placeholders only the run tool fills in).

    `vars` supplies :name / :'name' / :"name" substitutions.

    `project` is the directory (or query file path) the query belongs to;
    sqlmpeg walks up from it for a `sqlmpeg.json` and makes that project's
    namespaced functions callable. Omit it for a query that stands alone.
    """
    return tools.compile_query(query, vars, project)


def validate(
    query: str, vars: dict[str, str] | None = None, project: str | None = None
) -> dict[str, Any]:
    """Check that a query compiles; the repair loop's tool.

    Returns an empty object when the query is valid, otherwise the error:
    `code` (the error kind), `line` and `col` (where in the query, 1-based,
    or null), `message`, and `hint` (how to fix it, or null). Never fails --
    an invalid query is a result, not an error.

    `vars` supplies :name / :'name' / :"name" substitutions.

    `project` is the directory (or query file path) the query belongs to;
    sqlmpeg walks up from it for a `sqlmpeg.json` and makes that project's
    namespaced functions callable. Omit it for a query that stands alone.
    """
    return tools.validate_query(query, vars, project)


def explain(
    query: str, vars: dict[str, str] | None = None, project: str | None = None
) -> dict[str, Any]:
    """The compiled filter graph as JSON: nodes, edges, inputs and sinks.

    One graph per ffmpeg command. Use it to see which filter a column
    expression became and how the streams were wired.

    `vars` supplies :name / :'name' / :"name" substitutions.

    `project` is the directory (or query file path) the query belongs to;
    sqlmpeg walks up from it for a `sqlmpeg.json` and makes that project's
    namespaced functions callable. Omit it for a query that stands alone.
    """
    return tools.explain_query(query, vars, project)


def inspect(
    query: str, vars: dict[str, str] | None = None, project: str | None = None
) -> dict[str, Any]:
    """Run a query that returns rows: what is inside a media file.

    For a query with no media COPY -- a bare SELECT, or a COPY ... WITH
    (FORMAT csv) -- over a file's tracks, chapters, cues or attachments. Reads
    the file, writes nothing. Each result has `columns`, `rows`, and `text`
    (the same rows as a printable table).

    `vars` supplies :name / :'name' / :"name" substitutions.

    `project` is the directory (or query file path) the query belongs to;
    sqlmpeg walks up from it for a `sqlmpeg.json` and makes that project's
    namespaced functions callable. Omit it for a query that stands alone.
    """
    return tools.inspect_query(query, vars, project)


def filters(pattern: str | None = None) -> dict[str, Any]:
    """The filters and sources the LOCAL ffmpeg build has, not what is typical.

    `pattern` is a case-insensitive substring matched against each name and
    its one-line description; omit it for everything. When `pattern` is
    exactly one filter's name, the result also carries that filter's
    `options` -- the `name => value` arguments it accepts, with types, ranges
    and enum constants.
    """
    return tools.list_filters(pattern)


def run(
    query: str,
    vars: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    overwrite: bool = False,
    project: str | None = None,
) -> dict[str, Any]:
    """Compile a query and execute ffmpeg, WRITING the files its COPY ... TO names.

    Returns the run's `exit_code` (0 on success), `timed_out`, and one entry
    per command with its `argv`, `exit_code` and captured `stderr` (tail only
    when long). `outputs` lists the files written. `timeout` is per command,
    in seconds. `overwrite` false makes ffmpeg refuse to replace an existing
    file.

    `vars` supplies :name / :'name' / :"name" substitutions.

    `project` is the directory (or query file path) the query belongs to;
    sqlmpeg walks up from it for a `sqlmpeg.json` and makes that project's
    namespaced functions callable. Omit it for a query that stands alone.
    """
    return tools.run_query(query, vars, timeout, overwrite, project)


def build_server(*, allow_run: bool = False) -> MCPServer[Any]:
    """The configured server; `allow_run` adds the file-writing ``run`` tool."""
    # log_level configures the root logger, and at INFO sqlglot narrates every
    # array subscript it rewrites -- a line per query in the client's log pane.
    server: MCPServer[Any] = MCPServer(
        "sqlmpeg", version=__version__, instructions=_INSTRUCTIONS, log_level="WARNING"
    )
    server.add_tool(compile)
    server.add_tool(validate)
    server.add_tool(explain)
    server.add_tool(inspect)
    server.add_tool(filters)
    if allow_run:
        server.add_tool(run)

    @server.resource(
        DIALECT_URI,
        name="sqlmpeg dialect",
        description=(
            "The sqlmpeg SQL dialect: grammar, the functions this machine's "
            "ffmpeg provides, worked examples, and what each error code means."
        ),
        mime_type="text/plain",
    )
    def _dialect() -> str:
        return tools.dialect_prompt()

    return server


def serve(*, allow_run: bool = False) -> None:
    """Serve over stdin/stdout until the client disconnects."""
    build_server(allow_run=allow_run).run("stdio")
