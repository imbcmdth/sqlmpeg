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

One capability flag, not a matrix. ``allow_unsafe`` is the whole of it: the
tools that only answer -- about a query, or about what the registry publishes
-- are always there, and the ones that do something are behind it: ``run``,
which writes files on model say-so, and ``install``, which downloads code and
writes it to disk and to a project's lockfile. A permissions matrix for a
local dev tool invites passing every flag, and the per-call prompting already
lives in the MCP client. So the flag is a coarse capability switch, and the
precision that matters goes in each tool's DESCRIPTION, since that is the text
a client shows when it asks.
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
    sqlmpeg walks up from it for a `sqlmpeg.json` and a `sqlmpeg.lock`, and
    makes the namespaced functions of that project and of everything it (or
    this machine) installed callable. Omit it for a query that stands alone.
    """
    return tools.compile_query(query, vars, project)


def validate(
    query: str, vars: dict[str, str] | None = None, project: str | None = None
) -> dict[str, Any]:
    """Check that a query compiles; the repair loop's tool.

    Returns an empty object when the query is valid, otherwise the error:
    `code` (the error kind), `line` and `col` (where in the query, 1-based,
    or null), `message`, and `hint` (how to fix it, or null). Never fails --
    an invalid query is a result, not an error. A valid query that resolved a
    package worth mentioning answers with only a `warnings` array, which has
    no `code`: that is what tells a warning from an error.

    `vars` supplies :name / :'name' / :"name" substitutions.

    `project` is the directory (or query file path) the query belongs to;
    sqlmpeg walks up from it for a `sqlmpeg.json` and a `sqlmpeg.lock`, and
    makes the namespaced functions of that project and of everything it (or
    this machine) installed callable. Omit it for a query that stands alone.
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
    sqlmpeg walks up from it for a `sqlmpeg.json` and a `sqlmpeg.lock`, and
    makes the namespaced functions of that project and of everything it (or
    this machine) installed callable. Omit it for a query that stands alone.
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
    sqlmpeg walks up from it for a `sqlmpeg.json` and a `sqlmpeg.lock`, and
    makes the namespaced functions of that project and of everything it (or
    this machine) installed callable. Omit it for a query that stands alone.
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


def search(term: str | None = None) -> dict[str, Any]:
    """Find installable sqlmpeg packages in the package registry.

    `term` is matched case-insensitively against each package's name,
    namespace, description and the names of the functions it exports; omit it
    for the whole catalogue. A term matching nothing returns an empty list.

    Each package has a `name` (`<owner>/<name>`, what the install tool takes),
    its latest `version`, the `namespace` a query would call it by, a
    `description`, and the `functions` and `programs` it provides.

    Reads the registry over the network and writes nothing. `registry` is the
    site it read; `cached` is true when that site could not be reached and the
    catalogue cached on this machine answered instead, with `unreachable`
    saying why.
    """
    return tools.search_packages(term)


def install(package: str, project: str, namespace: str | None = None) -> dict[str, Any]:
    """Install a package from the registry. This DOWNLOADS CODE and WRITES FILES.

    It fetches an archive over the network, unpacks it into this machine's
    package store under the home directory, and edits two files in `project`:
    it pins the package in `sqlmpeg.lock` and records it as a dependency in
    `sqlmpeg.json`. The code it installs becomes callable by every query
    compiled in that project.

    The archive is verified against the sha256 the registry publishes before
    it is opened, so a download that does not match is discarded and nothing
    is written -- but the registry's own contents are not reviewed by anything
    here. Install only what the user asked for by name.

    `package` is `<owner>/<name>`, or `<owner>/<name>@<version>` for an exact
    version; without one, the highest published version is installed and
    pinned exactly.

    `project` is the directory the project lives in, and is required: a
    directory with no `sqlmpeg.lock` at or above it is not a project, and this
    tool never creates one.

    `namespace` installs the package under a namespace other than the one it
    claims, which is how two packages claiming the same one can both be
    installed. Installing over a namespace already in the lockfile replaces
    what held it, reported as `replaced`.
    """
    return tools.install_package(package, project, namespace)


def run(
    query: str,
    vars: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    overwrite: bool = False,
    project: str | None = None,
) -> dict[str, Any]:
    """Compile a query and execute ffmpeg. This WRITES FILES on disk.

    Every path the query's COPY ... TO names is created or, with `overwrite`,
    replaced -- this tool is the only one here that changes anything outside
    the answer it returns.

    Returns the run's `exit_code` (0 on success), `timed_out`, and one entry
    per command with its `argv`, `exit_code` and captured `stderr` (tail only
    when long). `outputs` lists the files written. `timeout` is per command,
    in seconds. `overwrite` false makes ffmpeg refuse to replace an existing
    file.

    `vars` supplies :name / :'name' / :"name" substitutions.

    `project` is the directory (or query file path) the query belongs to;
    sqlmpeg walks up from it for a `sqlmpeg.json` and a `sqlmpeg.lock`, and
    makes the namespaced functions of that project and of everything it (or
    this machine) installed callable. Omit it for a query that stands alone.
    """
    return tools.run_query(query, vars, timeout, overwrite, project)


def build_server(*, allow_unsafe: bool = False) -> MCPServer[Any]:
    """The configured server; `allow_unsafe` adds the tools that write: ``run`` and ``install``."""
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
    server.add_tool(search)
    if allow_unsafe:
        server.add_tool(run)
        server.add_tool(install)

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


def serve(*, allow_unsafe: bool = False) -> None:
    """Serve over stdin/stdout until the client disconnects."""
    build_server(allow_unsafe=allow_unsafe).run("stdio")
