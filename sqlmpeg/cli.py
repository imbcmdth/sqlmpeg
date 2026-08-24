"""Command-line interface for sqlmpeg.

Thin wrapper around the library pipeline (``compile_sql`` -> ``emit`` ->
``build_ffmpeg_commands``). See the "CLI" section of sqlmpeg-project.md.

A compile is a SEQUENCE of ffmpeg commands — one for every query but a
``two_pass`` sink or a ``sqlmpeg.loudnorm2`` graph (two each), and a
stream-copied fan-out with trim windows (one per output file, the only
form ffmpeg cuts copied streams correctly in).
``compile`` prints them joined by `` && `` on one line; ``run`` hands them to
``sqlmpeg.execute``, which runs them in order, stops at the first nonzero exit
and reports it, with ``--timeout`` applied per command.

``loudnorm2`` is the one compile whose printed line is not pure ffmpeg: its
measuring pass is wrapped in ``eval "$(... | sqlmpeg loudnorm2env)"``, which
makes the printed form POSIX-shell only. ``run`` needs no shell — it captures
the measuring pass's stderr, parses it in process (``sqlmpeg.loudnorm``) and
substitutes the numbers straight into the second command's argv.

Subcommands:

* ``compile SQL [-f FILE] [--graph-only]`` -- print the full ffmpeg command
  (POSIX-quoted via ``shlex.join`` even on Windows: it is documentation
  output, not something to paste into cmd.exe), or just the
  ``-filter_complex`` string with ``--graph-only``. The query names its own
  destination with ``COPY ... TO``; a query with no media ``COPY`` -- a bare
  SELECT, or one whose every ``COPY`` is ``FORMAT csv`` -- has nothing to
  compile and is refused (run it instead).
* ``explain SQL [-f FILE]`` -- dump the IR graph as JSON, sinks included.
* ``validate SQL [-f FILE] [--json]`` -- exit 0 silent on success; on error,
  exit 1 with a one-line human message or ``err.to_dict()`` JSON.
* ``run SQL [-f FILE] [--timeout SECS] [-y]`` -- compile and execute ffmpeg
  as a subprocess (guardrail #6: argv list, no shell, timeout enforced,
  stderr surfaced on failure). A query with no media ``COPY`` prints its
  result set as a table (or CSV, for ``COPY ... WITH (FORMAT csv)``);
  otherwise it runs the compiled ffmpeg command(s) against the ``COPY``'s
  own destination paths.
* ``list [--json]`` -- print what the project at the working directory and its
  dependencies provide: one table of packages, one of the functions they
  export, one of the programs they ship with the variables each declares, and
  one of the dependencies each manifest declares. Takes no query; the export
  list is the manifest's ``lib``, with parameter types read from the files.
* ``init [--name NAME] [--namespace NS]`` -- write ``sqlmpeg.json``, an empty
  ``sqlmpeg.lock`` and a starter program into the working directory. The
  package segment is the directory's name unless ``--name`` says otherwise;
  the namespace is ``--namespace``'s, or derived from the git remote's owner,
  or required. Refuses to overwrite any of the three files.
* ``search [TERM] [--json]`` -- fetch the registry's catalogue and print what
  matches TERM, filtered locally over each package's name, description and
  exported function names. A term matching nothing is an empty table, exit 0.
* ``install PKG[@VERSION] [-g]`` -- resolve a package in the catalogue,
  verify the archive it publishes against the digest it records, put it in
  the store, and pin it in the lockfile and the manifest. No version means
  the highest published one, written exact. Then walks its own manifest's
  dependencies and installs each the same way, recursively, at its highest
  published version -- only the package named on the command line is
  recorded in the manifest, what came along is the lockfile's own. Same
  project rule as ``link``: outside a project and without ``-g``, exit 2.
* ``link PATH [-g]`` / ``unlink NAME [-g]`` -- record (or drop) a package
  read live out of a directory, in this project's lockfile or, with ``-g``,
  the machine-wide one. The entry records only the directory; the package's
  name comes from the manifest there. Outside a project and without ``-g``
  there is no lockfile to write and none is invented: usage error, exit 2.
* ``publish`` -- not open yet; exits 1 naming where submissions go.
* ``prompt`` -- print the LLM system prompt to stdout. Takes no arguments and
  touches no files, but calls ``registry.load()`` to render the filter
  reference from this machine's ``ffmpeg -filters``/``-help`` output.
* ``mcp [--allow-unsafe]`` -- serve the compiler to an editor or agent as a
  stdio MCP server (``sqlmpeg.mcp``). Takes no arguments and needs the
  optional ``mcp`` extra; without it, one stderr line naming the install
  command and exit 1. stdout carries the protocol from the moment it starts,
  so nothing else may write there -- ``--allow-unsafe`` adds the tools that
  do something other than answer about a query.
* ``loudnorm2env`` -- read ffmpeg's stderr on stdin, print the
  ``export SQLMPEG_LN_*=`` lines its loudnorm JSON block holds. Takes no
  arguments and touches no files; exit 1 with one stderr line if there is no
  such block. It exists for the printed ``loudnorm2`` command line, which
  pipes pass 1 into it.

``compile``/``explain``/``validate``/``run`` take the query as SQL TEXT on
the command line. ``-f/--file PATH`` reads it from a file instead (``-f -``
reads stdin, e.g. for the LLM repair loop's pipe). Exactly one of the two is
required; both or neither is a usage error, exit 2. If the positional string
fails to compile and looks like a filename, a stderr hint suggests ``-f``
(see ``_maybe_print_file_hint``).

The positional may also name a PROGRAM a package ships (``sqlmpeg run
split-chapters -v source=film.mkv``). One rule decides which it is, in
``_resolve_query`` so every one of the four subcommands reads it the same
way: text beginning with ``SELECT``, ``COPY``, ``CREATE`` or ``WITH`` is SQL,
always; anything else that matches a program's name is that program's file;
anything else is SQL and fails as it always did. ``ns.pkg.program`` names one
of a map ``bin``'s entries, ``ns.pkg`` a string ``bin``'s program; either says
which package when a bare name matches more than one.

A compile can also have something to say short of refusing: a call that
resolved to a machine-wide package rather than to one this project installed,
or a call into a linked directory, which no lockfile pins. Those print as
``warning:`` lines on stderr after the command has run -- never on stdout,
which carries the ffmpeg command, the IR JSON or ``validate --json``'s error
object -- and never twice for one package.

Two flags are deliberately absent. ``--no-probe`` made a READABLE
file compile as if unreadable, silently stripping provenance metadata -- a
determinism switch that changed the result; opportunistic probing already
degrades on unreadable inputs. ``--portable`` had no portable
subset left to mean anything against: every function is a filter of the
installed ffmpeg, so the ffmpeg build answers "will this compile elsewhere".
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from . import binaries, loudnorm, store
from . import packages as packages_module
from . import registry as registry_module
from .compiler import classify, compile_commands, compile_table_sql
from .emit import Emitted, build_ffmpeg_commands, emit
from .errors import ErrorCode, SqlmpegError
from .execute import DEFAULT_TIMEOUT, execute
from .functions import Signature, package_signatures
from .ir import Graph, SinkUnit
from .project import (
    LOCKFILE_NAME,
    MANIFEST_NAME,
    RESERVED_NAMESPACES,
    STATEMENT_KEYWORDS,
    LinkEntry,
    LockEntry,
    Package,
    PackageSet,
    discover,
    find_lockfile,
    held_entry,
    is_namespace,
    read_lockfile,
    read_manifest,
    stored_name,
    with_entry,
    without_entry,
    write_lockfile,
    write_manifest,
)
from .prompt import build_system_prompt
from .table import CellValue, TableResult, TableSink, render_csv, render_table
from .vars import Variable, declared_variables, referenced, substitute, unset_variable
from .warnings import OnWarning, WarningLog

__all__ = ["main"]

# `compile` prints a command SEQUENCE as one line: shell chaining, so the
# printed line runs the passes in order when pasted.
_CHAIN = " && "

# `run` is the DEFAULT subcommand, unconditionally: any argv whose
# first token is not one of these names IS run's argv, flags included
# (`sqlmpeg -f q.sql`). No plausibility checking -- a mistyped subcommand falls
# through to run's SQL parser and dies as a line-anchored PARSE_ERROR, a
# better diagnostic than a usage line. Consequence: `sqlmpeg -h` shows run's
# help, not the top-level one.
_SUBCOMMANDS = frozenset(
    {
        "compile",
        "explain",
        "validate",
        "run",
        "list",
        "init",
        "search",
        "install",
        "link",
        "unlink",
        "publish",
        "prompt",
        "mcp",
        loudnorm.ENV_SUBCOMMAND,
    }
)


def _version() -> str:
    return metadata.version("sqlmpeg")


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    # psql's spelling (-v is taken by variables there too); checked before the
    # run dispatch, which would otherwise hand the flag to the SQL parser.
    if argv and argv[0] in ("--version", "-V"):
        print(f"sqlmpeg {_version()}")
        return 0
    if not argv or argv[0] not in _SUBCOMMANDS:
        argv = ["run", *argv]

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_usage(sys.stderr)
        return 2

    handler = _HANDLERS[args.command]
    # One sink per invocation: `compile` and `validate` compile the same text
    # twice (the table-query fallback), and the reader wants each warning once.
    warnings = WarningLog()
    try:
        return handler(args, warnings)
    finally:
        _print_warnings(warnings)


_QUERY_HELP = "SQL query text (exactly one of this or -f/--file is required)"
_FILE_HELP = "read the query from a file instead of the command line ('-' for stdin)"
_SET_HELP = "define a variable for :name/:'name'/:\"name\" substitution (repeatable)"


def _add_query_arguments(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("query", nargs="?", default=None, help=_QUERY_HELP)
    subparser.add_argument("-f", "--file", default=None, help=_FILE_HELP)
    subparser.add_argument(
        "-v",
        "--set",
        dest="set_vars",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help=_SET_HELP,
    )


def _add_global_argument(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "-g",
        "--global",
        action="store_true",
        dest="global_lock",
        help="write the machine-wide lockfile instead of this project's",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sqlmpeg", description=f"sqlmpeg {_version()} - SQL frontend for FFmpeg filtergraphs"
    )
    parser.add_argument("-V", "--version", action="version", version=f"sqlmpeg {_version()}")
    subparsers = parser.add_subparsers(dest="command")

    compile_p = subparsers.add_parser("compile", help="compile SQL to an ffmpeg command")
    _add_query_arguments(compile_p)
    compile_p.add_argument(
        "--graph-only", action="store_true", help="print only the filter_complex string"
    )
    explain_p = subparsers.add_parser("explain", help="dump the compiled IR graph as JSON")
    _add_query_arguments(explain_p)
    validate_p = subparsers.add_parser("validate", help="check that a query compiles")
    _add_query_arguments(validate_p)
    validate_p.add_argument(
        "--json", action="store_true", dest="as_json", help="emit the error as JSON"
    )
    # `sqlmpeg -h` lands on run's help via the default dispatch, so run's
    # description carries the version the way the top-level one does.
    run_p = subparsers.add_parser(
        "run",
        help="compile and execute ffmpeg",
        description=f"sqlmpeg {_version()} - compile and execute ffmpeg (the default subcommand)",
    )
    _add_query_arguments(run_p)
    run_p.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help="ffmpeg timeout in seconds"
    )
    run_p.add_argument(
        "-y", action="store_true", dest="overwrite", help="pass -y (overwrite) to ffmpeg"
    )

    list_p = subparsers.add_parser(
        "list", help="print what this project and its dependencies provide"
    )
    list_p.add_argument(
        "--json", action="store_true", dest="as_json", help="emit the listing as JSON"
    )
    init_p = subparsers.add_parser(
        "init", help="write sqlmpeg.json, sqlmpeg.lock and a starter program here"
    )
    init_p.add_argument(
        "--name",
        default=None,
        help="the package name, <namespace>/<package> or just the package "
        "segment (default: this directory's name)",
    )
    init_p.add_argument(
        "--namespace",
        default=None,
        help="the namespace half of the name (default: derived from the git remote's owner)",
    )

    search_p = subparsers.add_parser("search", help="find packages in the registry")
    search_p.add_argument(
        "term",
        nargs="?",
        default=None,
        help="matched against each package's name, description and "
        "function names (default: everything published)",
    )
    search_p.add_argument(
        "--json", action="store_true", dest="as_json", help="emit the results as JSON"
    )

    install_p = subparsers.add_parser("install", help="install a package from the registry")
    install_p.add_argument(
        "package",
        help="<namespace>/<package>, or <namespace>/<package>@<version> for an exact version",
    )
    _add_global_argument(install_p)

    link_p = subparsers.add_parser("link", help="read a package live out of a directory")
    link_p.add_argument("path", help="the directory holding the package's sqlmpeg.json")
    _add_global_argument(link_p)
    unlink_p = subparsers.add_parser("unlink", help="drop a linked package")
    unlink_p.add_argument(
        "name", help="the linked package's name (or its directory) to stop reading live"
    )
    _add_global_argument(unlink_p)
    subparsers.add_parser("publish", help="publish a package to the registry")

    subparsers.add_parser("prompt", help="print the LLM system prompt for this dialect")
    mcp_p = subparsers.add_parser("mcp", help="serve the compiler to an editor or agent over MCP")
    mcp_p.add_argument(
        "--allow-unsafe",
        action="store_true",
        dest="allow_unsafe",
        help="also expose the tools that do more than answer about a query",
    )
    subparsers.add_parser(
        loudnorm.ENV_SUBCOMMAND,
        help="read ffmpeg's stderr on stdin, print loudnorm's measurements as exports",
    )

    return parser


def _read_file(path: str) -> str | None:
    """Read query text from `path` (or stdin for "-"). None + printed error on failure."""
    if path == "-":
        return sys.stdin.read()
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as err:
        print(f"error: could not read {path!r}: {err.strerror or err}", file=sys.stderr)
        return None


_VAR_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _parse_set_vars(pairs: list[str], command: str) -> tuple[dict[str, str] | None, int]:
    """Parse repeated ``-v/--set NAME=VALUE`` pairs into a dict (last wins).

    Returns ``(variables, 0)`` on success, or ``(None, 2)`` with a usage
    error already printed to stderr for a malformed pair: no ``=``, or a
    name outside ``[A-Za-z_][A-Za-z0-9_]*``.
    """
    variables: dict[str, str] = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or _VAR_NAME_RE.fullmatch(name) is None:
            print(
                f"error: {command}: malformed -v/--set {pair!r}, want "
                "NAME=VALUE with NAME matching [A-Za-z_][A-Za-z0-9_]*",
                file=sys.stderr,
            )
            return None, 2
        variables[name] = value
    return variables, 0


def _project_start(args: argparse.Namespace) -> Path:
    """Where the upward walk for ``sqlmpeg.json`` starts.

    A ``-f PATH`` query is read from a file, and the project it belongs to is
    the one above that file's own directory. A positional query and ``-f -``
    were typed at the shell, so the walk starts at the working directory.
    """
    if args.file is not None and args.file != "-":
        return Path(args.file).parent
    return Path.cwd()


_LEADING_WORD_RE = re.compile(r"[A-Za-z]+")


def _starts_a_statement(text: str) -> bool:
    """True when `text` begins a SQL statement, past leading whitespace and comments.

    The whole of rule one: four words, and a positional starting with any of
    them is SQL whatever else it might have named. The scan skips what the
    lexer would skip so a query written under a ``--`` header still reads as
    one.
    """
    at = 0
    end = len(text)
    while at < end:
        if text[at].isspace():
            at += 1
        elif text.startswith("--", at):
            newline = text.find("\n", at)
            at = end if newline == -1 else newline + 1
        elif text.startswith("/*", at):
            close = text.find("*/", at + 2)
            at = end if close == -1 else close + 2
        else:
            break
    word = _LEADING_WORD_RE.match(text, at)
    return word is not None and word.group().lower() in STATEMENT_KEYWORDS


def _qualified_program(package: Package, program: str) -> str:
    """`program` written the way `run` reaches it: two segments for the default, three otherwise."""
    if program == package.package:
        return f"{package.namespace}.{package.package}"
    return f"{package.namespace}.{package.package}.{program}"


def _program_names(packages: PackageSet | None) -> list[str]:
    """Every program the discovered packages ship, each qualified."""
    if packages is None:
        return []
    return [
        _qualified_program(package, program)
        for name in packages.names()
        for package in [_package(packages, name)]
        for program in package.programs
    ]


def _package(packages: PackageSet, name: str) -> Package:
    found = packages.get(name)
    assert found is not None  # a name `names()` just handed back
    return found


def _matching_programs(name: str, packages: PackageSet | None) -> list[tuple[Package, str]]:
    """The (package, program name) pairs `name` names, qualified or bare.

    Three segments (``ns.pkg.program``) name one entry of a map `bin`; two
    (``ns.pkg``) name a string `bin`'s program. A bare name is looked up
    across every installed package, which may match more than one.
    """
    if packages is None:
        return []
    parts = name.split(".")
    if len(parts) in (2, 3):
        namespace, package_name = parts[0], parts[1]
        package = packages.find(namespace, package_name)
        if package is None:
            return []
        member = parts[2] if len(parts) == 3 else None
        program = package.program(member)
        return [] if program is None else [(package, package.package if member is None else member)]
    found: list[tuple[Package, str]] = []
    for claimed in packages.names():
        package = _package(packages, claimed)
        if package.program(name) is not None:
            found.append((package, name))
    return found


def _program_text(
    name: str, packages: PackageSet | None
) -> tuple[str, tuple[str, str]] | None:
    """The query text of the program `name` names, or None when it names none.

    Two packages shipping one name is a rejection rather than a pick: the
    qualified form says which, and guessing would run the wrong program.
    """
    found = _matching_programs(name, packages)
    if not found:
        return None
    if len(found) > 1:
        written = ", ".join(_qualified_program(package, program) for package, program in found)
        raise SqlmpegError(
            ErrorCode.UNSUPPORTED_SQL,
            f"more than one package ships a program named '{name}'",
            hint=f"name the one you mean: {written}",
        )
    package, program = found[0]
    file = package.program(program)
    assert file is not None  # _matching_programs only keeps shipped programs
    try:
        return file.read_text(encoding="utf-8"), (package.name, package.version)
    except OSError as err:
        raise SqlmpegError(
            ErrorCode.UNSUPPORTED_SQL,
            f"program '{_qualified_program(package, program)}' could not be read: "
            f"{err.strerror or err}",
            hint=f"its file is {file}",
        ) from err


@dataclass(frozen=True)
class _Query:
    """What `_resolve_query` hands the subcommand handlers.

    `text` is the substituted query; `unset` is the substitution's map from an
    unset variable's NULL to its name, threaded into every compile so a
    rejection can say which variable was not set. `program` is the name the
    positional resolved to (None for inline SQL or ``-f``), and `source` the
    pre-substitution text, whose ``-- variables:`` header the error hint reads.
    """

    text: str
    unset: dict[tuple[int, int], str]
    program: str | None
    source: str
    #: The (name, version) of the package a program belongs to, so a call it
    #: makes reaches the version THAT package declares rather than whatever
    #: the project happens to have. None for inline SQL and for `-f`.
    owner: tuple[str, str] | None = None


def _program_variables_error(err: SqlmpegError, name: str, text: str) -> SqlmpegError:
    """`err` again, its hint naming what the program's own header declares."""
    declared = declared_variables(text)
    if not declared:
        return err
    written = ", ".join(variable.name for variable in declared)
    return SqlmpegError(
        err.code,
        err.message,
        line=err.line,
        col=err.col,
        hint=f"'{name}' declares {written}; define each with -v name=value",
    )


def _resolve_query(
    args: argparse.Namespace,
) -> tuple[_Query | None, PackageSet | None, int]:
    """Resolve the query text and its project for compile/explain/validate/run.

    Exactly one of the positional ``query`` (inline SQL) or ``-f/--file`` is
    required. Returns ``(query, packages, 0)`` on success, or
    ``(None, None, exit_code)`` with the error already printed to stderr: 2
    for a usage violation (both or neither given; a malformed ``-v``; a ``-v``
    naming a variable the text never references), 1 for a file that could not
    be read.

    ``packages`` is the project the query was written in, or None when the walk
    finds no manifest -- the ordinary case, and the one where a compile is
    exactly what it was before projects existed.

    A positional that does not begin a statement and names a program one of
    those packages ships is that program: its file's text replaces it, and
    every subcommand taking a query takes a program name too.

    ``-v/--set`` substitution runs here, once, so every handler inherits it.
    An UNSET reference substitutes to NULL rather than failing -- absence,
    which the compile itself judges -- so the check points the other way: a
    ``-v`` for a name the text never references is the usage error, naming
    what the text does reference. A `SqlmpegError` from a malformed manifest
    is not caught here; it propagates to the caller's own handling.
    """
    has_query = args.query is not None
    has_file = args.file is not None
    if has_query and has_file:
        print(
            f"error: {args.command}: give a SQL string or -f/--file, not both",
            file=sys.stderr,
        )
        return None, None, 2
    if not has_query and not has_file:
        print(
            f"error: {args.command}: give a SQL string or -f/--file",
            file=sys.stderr,
        )
        return None, None, 2
    if has_file:
        text = _read_file(args.file)
        if text is None:
            return None, None, 1
    else:
        assert args.query is not None
        text = args.query

    packages = discover(_project_start(args))
    program: str | None = None
    owner: tuple[str, str] | None = None
    if not has_file and not _starts_a_statement(text):
        shipped = _program_text(text, packages)
        if shipped is not None:
            program, (text, owner) = text, shipped

    variables, code = _parse_set_vars(args.set_vars, args.command)
    if variables is None:
        return None, None, code
    names = referenced(text)
    unknown = sorted(name for name in variables if name not in names)
    if unknown:
        what = f"program '{program}'" if program is not None else "the query"
        listed = (
            "it references " + ", ".join(f":{name}" for name in sorted(names))
            if names
            else "it references no variables"
        )
        written = ", ".join(f"{name}=..." for name in unknown)
        verb = "names a variable" if len(unknown) == 1 else "name variables"
        print(
            f"error: {args.command}: -v {written} {verb} {what} never "
            f"references; {listed}",
            file=sys.stderr,
        )
        return None, None, 2
    sub = substitute(text, variables)
    return (
        _Query(text=sub.text, unset=sub.unset, program=program, source=text, owner=owner),
        packages,
        0,
    )


def _maybe_print_file_hint(
    err: SqlmpegError, source: str | None, packages: PackageSet | None = None
) -> None:
    """Suggest -f when an inline positional string names an existing file or
    ends in .sql/.SQL. Fires on ANY compile error, not just PARSE_ERROR: a
    bare filename like `query.sql` parses as a SQL column reference and fails
    as UNSUPPORTED_SQL. CLI sugar only -- never touches `err`.

    A positional that does not begin a statement and names no program is a
    program name that matched nothing, so the programs there ARE to run are
    named too. One that DID match is not: the rejection came from inside that
    program, and a list of the others is noise over it."""
    if source is None:
        return
    if os.path.exists(source) or source.lower().endswith(".sql"):
        print(
            f"hint: '{source}' looks like a file; did you mean -f '{source}'?",
            file=sys.stderr,
        )
    if _starts_a_statement(source) or _matching_programs(source, packages):
        return
    shipped = _program_names(packages)
    if shipped:
        print(f"hint: installed programs: {', '.join(shipped)}", file=sys.stderr)


def _print_warnings(warnings: WarningLog) -> None:
    """Print what the compile had to say, to stderr.

    stderr and not stdout: `compile`'s stdout is the ffmpeg command, and
    `validate --json` is the repair loop's JSON. Printed after the command has
    run, so nothing interleaves with what it wrote.
    """
    for warning in warnings.warnings:
        print(f"warning: {warning.message}", file=sys.stderr)
        if warning.hint is not None:
            print(f"hint: {warning.hint}", file=sys.stderr)


def _print_error(
    err: SqlmpegError,
    *,
    source: str | None = None,
    packages: PackageSet | None = None,
    query: _Query | None = None,
) -> None:
    # A program run by name gets the richer hint: an unset-variable rejection
    # names what the program's own `-- variables:` header declares.
    if query is not None and query.program is not None and unset_variable(err) is not None:
        err = _program_variables_error(err, query.program, query.source)
    print(f"error: {err}", file=sys.stderr)
    _maybe_print_file_hint(err, source, packages)


def _check_output_dir(out_path: str) -> str | None:
    """Return an error message if `out_path`'s parent directory does not exist.

    A destination containing "://" is a protocol URL (udp, rtmp, srt, ...):
    ffmpeg owns it, there is no directory to check.
    """
    if "://" in out_path:
        return None
    parent = Path(out_path).parent
    if str(parent) and not parent.exists():
        return f"error: output directory does not exist: {parent}"
    return None


def _sinks(graphs: list[Graph]) -> list[SinkUnit]:
    """Every command's sink units, in command order."""
    return [unit for graph in graphs for unit in graph.sinks]


def _needs_out_path(graphs: list[Graph]) -> bool:
    """True if some sink names no destination — i.e. the bare-SELECT case."""
    return any(unit.path is None for unit in _sinks(graphs))


def _output_paths(graphs: list[Graph]) -> list[str]:
    """Every file this command will write, for the directory-existence check."""
    return [unit.path for unit in _sinks(graphs) if unit.path is not None]


def _is_table_capable_query(
    text: str, packages: PackageSet | None, on_warning: OnWarning
) -> bool:
    """True if `text` succeeds as a table/csv query -- the fallback `compile`
    and `validate` try before giving up on a `compile_sql` error."""
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


_TABLE_USAGE_HINT = (
    "error: compile has nothing to show: this query has no media destination "
    "(no COPY, or every COPY is FORMAT csv); run it instead -- `sqlmpeg run ...` "
    "prints its result set as a table"
)
_NO_OUTPUT_PATH_ERROR = "error: no output path given: use COPY ... TO in the query"


def _print_table_sinks(sinks: list[TableSink]) -> int:
    """Print (or write) every sink of a table/csv query. `run`'s table half."""
    for sink in sinks:
        if not sink.csv:
            print(render_table(sink.result))
            continue
        text = render_csv(sink.result, header=sink.header)
        if sink.path is None:
            print(text, end="")  # already newline-terminated per row
            continue
        dir_error = _check_output_dir(sink.path)
        if dir_error is not None:
            print(dir_error, file=sys.stderr)
            return 1
        Path(sink.path).write_text(text, encoding="utf-8")
    return 0


def _cmd_compile(args: argparse.Namespace, on_warning: OnWarning) -> int:
    query: _Query | None = None
    packages: PackageSet | None = None
    try:
        query, packages, code = _resolve_query(args)
        if query is None:
            return code
        graphs = compile_commands(
            query.text,
            packages=packages,
            on_warning=on_warning,
            unset=query.unset,
            owner=query.owner,
        )
        emitted = [emit(graph) for graph in graphs]
    except SqlmpegError as err:
        # A query with no streaming representation at all (metadata
        # columns, an un-COALESCEd join gap) fails HERE, so table mode is the
        # fallback -- tried only after compilation failed, and only for a
        # query that could BE one. If the fallback fails too, the original
        # error surfaces; it is usually more informative.
        # `query` is None only when `_resolve_query` raised before it could
        # return (a malformed manifest, an unreadable program), which cannot
        # be table-capable either, so it is guarded out of `classify`.
        if query is not None and _is_table_capable_query(query.text, packages, on_warning):
            print(_TABLE_USAGE_HINT, file=sys.stderr)
            return 2
        _print_error(err, source=args.query, packages=packages, query=query)
        return 1

    if args.graph_only:
        # One line per command; a compile is a sequence only for two_pass,
        # loudnorm2 and the copy-and-trim fan-out.
        print("\n".join(e.filter_complex for e in emitted))
        return 0

    # A bare SELECT compiles fine here (the streaming lowerer allows it), but
    # it has no COPY ... TO destination -- compile never invents one, so it
    # is the same refusal as the except branch above.
    if _needs_out_path(graphs):
        print(_TABLE_USAGE_HINT, file=sys.stderr)
        return 2
    print(_CHAIN.join(_shell_commands(emitted)))
    return 0


def _shell_commands(emitted: list[Emitted]) -> list[str]:
    """Every command of the compile as a shell-ready line, in order.

    ``shlex.join`` for all but a ``loudnorm2`` compile: there the measuring
    pass is wrapped in the ``eval "$(...)"`` that exports what it measured,
    and the write pass keeps its ``${SQLMPEG_LN_*}`` references expandable
    (:func:`sqlmpeg.loudnorm.shell_join`).
    """
    lines: list[str] = []
    for e in emitted:
        commands = build_ffmpeg_commands(e)
        if not e.measure_filter_complex:
            lines += [shlex.join(command) for command in commands]
            continue
        measure, *rest = commands
        lines.append(loudnorm.measure_command(shlex.join(measure)))
        lines += [loudnorm.shell_join(command) for command in rest]
    return lines


def _cmd_explain(args: argparse.Namespace, on_warning: OnWarning) -> int:
    query: _Query | None = None
    packages: PackageSet | None = None
    try:
        query, packages, code = _resolve_query(args)
        if query is None:
            return code
        graphs = compile_commands(
            query.text,
            packages=packages,
            on_warning=on_warning,
            unset=query.unset,
            owner=query.owner,
        )
    except SqlmpegError as err:
        _print_error(err, source=args.query, packages=packages, query=query)
        return 1

    # One object for a single command, a JSON ARRAY for a sequence's.
    payload: object = graphs[0].to_dict() if len(graphs) == 1 else [
        graph.to_dict() for graph in graphs
    ]
    print(json.dumps(payload, indent=2))
    return 0


def _cmd_validate(args: argparse.Namespace, on_warning: OnWarning) -> int:
    query: _Query | None = None
    packages: PackageSet | None = None
    try:
        query, packages, code = _resolve_query(args)
        if query is None:
            return code
        compile_commands(
            query.text,
            packages=packages,
            on_warning=on_warning,
            unset=query.unset,
            owner=query.owner,
        )
    except SqlmpegError as err:
        # "compiles = valid" still holds: a table/csv query compiles through
        # its own lenient pipeline, tried here exactly as in `_cmd_compile`.
        if query is not None and _is_table_capable_query(query.text, packages, on_warning):
            return 0
        if args.as_json:
            # Machine contract: stdout is pure JSON, the library error
            # verbatim. The file hint goes to stderr so it cannot perturb it.
            print(json.dumps(err.to_dict()))
            _maybe_print_file_hint(err, args.query, packages)
        else:
            _print_error(err, source=args.query, packages=packages, query=query)
        return 1

    return 0


def _cmd_run(args: argparse.Namespace, on_warning: OnWarning) -> int:
    query: _Query | None = None
    packages: PackageSet | None = None
    try:
        query, packages, code = _resolve_query(args)
        if query is None:
            return code
        is_table_capable, _has_copy = classify(
            query.text,
            packages=packages,
            on_warning=on_warning,
            unset=query.unset,
            owner=query.owner,
        )
    except SqlmpegError as err:
        _print_error(err, source=args.query, packages=packages, query=query)
        return 1

    # No media COPY -- a bare SELECT, or every COPY is FORMAT csv -- IS a
    # table query, always: the table/csv path, which needs no ffmpeg.
    if is_table_capable:
        try:
            sinks = compile_table_sql(
                query.text,
            packages=packages,
            on_warning=on_warning,
            unset=query.unset,
            owner=query.owner,
            )
        except SqlmpegError as err:
            _print_error(err, source=args.query, packages=packages, query=query)
            return 1
        return _print_table_sinks(sinks)

    try:
        graphs: list[Graph] = compile_commands(
            query.text,
            packages=packages,
            on_warning=on_warning,
            unset=query.unset,
            owner=query.owner,
        )
        emitted: list[Emitted] = [emit(graph) for graph in graphs]
    except SqlmpegError as err:
        _print_error(err, source=args.query, packages=packages, query=query)
        return 1

    # A media COPY names its own destination, so this fires only for the rare
    # script that mixes a media COPY with a `COPY ... TO STDOUT WITH (FORMAT
    # csv)` sink -- STDOUT has no file path for a media run.
    if _needs_out_path(graphs):
        print(_NO_OUTPUT_PATH_ERROR, file=sys.stderr)
        return 2

    for path in _output_paths(graphs):
        dir_error = _check_output_dir(path)
        if dir_error is not None:
            print(dir_error, file=sys.stderr)
            return 1

    if binaries.ffmpeg_path() is None:
        print(f"error: ffmpeg not found: {binaries.INSTALL_HINT}", file=sys.stderr)
        return 1

    # `sqlmpeg.execute` owns the loop; the CLI owns the printing. stderr stays
    # uncaptured (`capture_stderr` left false) so ffmpeg writes its progress
    # straight to the terminal, and `echo` puts each `$ <cmd>` line in front
    # of the output it produced.
    result = execute(
        emitted,
        timeout=args.timeout,
        overwrite=args.overwrite,
        echo=_echo_command,
    )

    if result.timed_out:
        print(f"error: ffmpeg timed out after {args.timeout}s", file=sys.stderr)
        return 1
    if result.measure_error is not None:
        print(f"error: {result.measure_error}", file=sys.stderr)
        return 1
    if result.exit_code != 0:
        print(result.commands[-1].stderr, file=sys.stderr, end="")
        print(f"error: ffmpeg exited with code {result.exit_code}", file=sys.stderr)
        return result.exit_code

    return 0


def _echo_command(argv: list[str]) -> None:
    print("$", shlex.join(argv))


@dataclass(frozen=True)
class _ListedProgram:
    """One program: the variables its query declares, split required from optional.

    The split is derived, not read off the header -- see :func:`_required_variables`.
    """

    name: str
    path: Path
    required: tuple[Variable, ...]
    optional: tuple[Variable, ...]


@dataclass(frozen=True)
class _Listed:
    """What one package provides, read once and printed either way."""

    package: Package
    functions: tuple[Signature, ...]
    programs: tuple[_ListedProgram, ...]


def _relative(path: Path, root: Path) -> str:
    """`path` as the manifest writes it, or its own text when it is elsewhere."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _try_compile(
    text: str, packages: PackageSet | None, unset: dict[tuple[int, int], str]
) -> SqlmpegError | None:
    """None on a clean compile, else the rejection -- streaming first, then table.

    Mirrors `_is_table_capable_query`: a query with no streaming
    representation (metadata columns, an un-COALESCEd join gap) is retried as
    a table query before giving up, exactly as `compile`/`validate` do, so a
    program of either shape gets the same required/optional derivation.
    """
    try:
        compile_commands(text, packages=packages, unset=unset)
        return None
    except SqlmpegError as err:
        stream_err = err
    try:
        is_table_capable, _has_copy = classify(text, packages=packages, unset=unset)
    except SqlmpegError as err:
        return err
    if not is_table_capable:
        return stream_err
    try:
        compile_table_sql(text, packages=packages, unset=unset)
        return None
    except SqlmpegError as err:
        return err


def _required_variables(
    text: str, names: frozenset[str], packages: PackageSet | None
) -> frozenset[str]:
    """Every declared variable a compile rejects as required when it alone is unset.

    One compile per name: every OTHER declared variable gets a placeholder,
    leaving only this one NULL, so a rejection this round can only be about
    it -- and counts only when it IS (:func:`unset_variable`, the shape wave
    A gives every required NULL: `input(NULL)`, `TO NULL`, a NULL stream
    position, or a NULL on the curated required-options list). Testing one
    name at a time, rather than accumulating placeholders across rounds,
    survives a query where an earlier position's own NULL would otherwise
    block ever reaching a later one -- a UNION branch's trim bound ahead of
    its own `input()`, say -- so every declared name gets an independent
    read regardless of where in the query it sits.
    """
    required: set[str] = set()
    for name in names:
        values = {other: "1" for other in names if other != name}
        sub = substitute(text, values)
        err = _try_compile(sub.text, packages, sub.unset)
        if err is not None and unset_variable(err) == name:
            required.add(name)
    return frozenset(required)


def _listed(package: Package, packages: PackageSet | None) -> _Listed:
    """Read one package's exports and programs. Raises like any other read.

    The export list is the manifest's; `package_signatures` parses the files
    only for the parameter types, and checks each export is defined where the
    manifest says. `packages` is the whole discovered set (not just `package`
    itself), the same one a real compile of one of its programs would resolve
    namespaced calls against.
    """
    programs = []
    for name, path in package.programs.items():
        text = path.read_text(encoding="utf-8")
        declared = declared_variables(text)
        required = _required_variables(
            text, frozenset(variable.name for variable in declared), packages
        )
        programs.append(
            _ListedProgram(
                name=name,
                path=path,
                required=tuple(v for v in declared if v.name in required),
                optional=tuple(v for v in declared if v.name not in required),
            )
        )
    return _Listed(
        package=package, functions=package_signatures(package), programs=tuple(programs)
    )


def _listing_json(listed: list[_Listed]) -> str:
    """The listing as one JSON object, for scripting."""
    packages = [
        {
            "name": entry.package.name,
            "version": entry.package.version,
            "layer": entry.package.layer,
            "linked": entry.package.linked,
            "root": str(entry.package.root),
            "exports": [
                {
                    "name": signature.name,
                    "params": [
                        {"name": param.name, "type": param.type} for param in signature.params
                    ],
                    "returns": signature.returns,
                    "file": _relative(signature.export, entry.package.root),
                }
                for signature in entry.functions
            ],
            "programs": [
                {
                    "name": listed_program.name,
                    "file": _relative(listed_program.path, entry.package.root),
                    "required": [
                        {"name": variable.name, "description": variable.description}
                        for variable in listed_program.required
                    ],
                    "optional": [
                        {"name": variable.name, "description": variable.description}
                        for variable in listed_program.optional
                    ],
                }
                for listed_program in entry.programs
            ],
            "dependencies": [
                {"name": name, "range": range_}
                for name, range_ in entry.package.dependencies.items()
            ],
        }
        for entry in listed
    ]
    return json.dumps({"packages": packages}, indent=2)


def _package_rows(listed: list[_Listed]) -> TableResult:
    rows: list[list[CellValue]] = [
        [
            entry.package.name,
            entry.package.version,
            entry.package.layer,
            entry.package.linked,
        ]
        for entry in listed
    ]
    return TableResult(columns=["package", "version", "layer", "linked"], rows=rows)


def _export_rows(listed: list[_Listed]) -> TableResult:
    rows: list[list[CellValue]] = [
        [
            entry.package.name,
            signature.written,
            signature.returns,
            _relative(signature.export, entry.package.root),
        ]
        for entry in listed
        for signature in entry.functions
    ]
    return TableResult(columns=["package", "export", "returns", "file"], rows=rows)


def _program_rows(listed: list[_Listed]) -> TableResult:
    rows: list[list[CellValue]] = [
        [
            entry.package.name,
            listed_program.name,
            _written_variables(listed_program.required),
            _written_variables(listed_program.optional),
            _relative(listed_program.path, entry.package.root),
        ]
        for entry in listed
        for listed_program in entry.programs
    ]
    return TableResult(
        columns=["package", "program", "required", "optional", "file"], rows=rows
    )


def _dependency_rows(listed: list[_Listed]) -> TableResult:
    rows: list[list[CellValue]] = [
        [entry.package.name, name, range_]
        for entry in listed
        for name, range_ in entry.package.dependencies.items()
    ]
    return TableResult(columns=["package", "dependency", "range"], rows=rows)


def _written_variables(variables: tuple[Variable, ...]) -> str:
    """The declared variables as the header writes them, descriptions and all."""
    return ", ".join(
        f"{variable.name} ({variable.description})" if variable.description else variable.name
        for variable in variables
    )


def _cmd_list(args: argparse.Namespace, on_warning: OnWarning) -> int:
    """Print what the project at the working directory provides. Takes no query.

    The same upward walk every other subcommand does, so the answer is the one
    a compile here would resolve against.
    """
    try:
        found = discover(Path.cwd())
        packages = [] if found is None else [found.packages[name] for name in found.names()]
        listed = [_listed(package, found) for package in packages]
    except SqlmpegError as err:
        _print_error(err)
        return 1
    except OSError as err:
        named = err.filename or "a file this project names"
        print(f"error: could not read {named}: {err.strerror or err}", file=sys.stderr)
        return 1

    if args.as_json:
        print(_listing_json(listed))
        return 0

    # One section per kind, each headed by its own name: four tables in a row
    # are unreadable without one.
    sections = [
        f"{heading}\n{render_table(table)}"
        for heading, table in (
            ("packages", _package_rows(listed)),
            ("exports", _export_rows(listed)),
            ("programs", _program_rows(listed)),
            ("dependencies", _dependency_rows(listed)),
        )
    ]
    print("\n\n".join(sections))
    return 0


# The starter `init` writes: a program, not an export. A lib must name a file
# that defines its export, so a fresh directory has nothing to declare one
# with -- and a runnable program is the first thing there is to try.
_STARTER_PROGRAM = "resize"
_STARTER_FILE = "queries/resize.sql"
_STARTER_QUERY = """\
-- Scale a file's video to 720p, its audio carried through untouched.
-- variables: source (input media path), dest (output path)
-- example: sqlmpeg run resize -v source=in.mp4 -v dest=out.mp4
COPY (
  SELECT scale(f.video[1], -2, 720), f.audio[1]
  FROM input(:'source') f
) TO :'dest'
"""

_NOT_A_NAMESPACE_HINT = (
    "a namespace is a lowercase plain identifier: a letter or underscore, then "
    "letters, digits or underscores -- pass --namespace to give one"
)


def _folded_identifier(name: str) -> str:
    """`name` folded to a plain identifier: lowercase, everything else an underscore."""
    return re.sub(r"[^a-z0-9_]", "_", name.lower())


def _git_remote_owner(directory: Path) -> str | None:
    """The owner segment of the git remote `origin`'s URL, or None.

    One cheap subprocess; anything that goes wrong -- no git, no repository,
    no remote, an unparseable URL -- is None, never an error: it only feeds a
    default the flag overrides.
    """
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run(
            [git, "-C", str(directory), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    if url.endswith(".git"):
        url = url[: -len(".git")]
    if "://" in url:
        url = url.split("://", 1)[1]
    # scp-like `git@host:owner/repo` becomes `git@host/owner/repo`.
    parts = [part for part in url.replace(":", "/").split("/") if part]
    return parts[-2] if len(parts) >= 2 else None


def _checked_namespace(written: str, given: str) -> str | None:
    """`written` as a usable namespace, or None with the rejection printed."""
    usable = is_namespace(written) and any(char.isalnum() for char in written)
    if not usable:
        print(f"error: init: {given} gives no namespace: {written!r}", file=sys.stderr)
        print(f"hint: {_NOT_A_NAMESPACE_HINT}", file=sys.stderr)
        return None
    if written in RESERVED_NAMESPACES:
        reserved = ", ".join(sorted(RESERVED_NAMESPACES))
        print(f"error: init: namespace {written!r} is reserved", file=sys.stderr)
        print(
            f"hint: {reserved} belong to sqlmpeg itself; pass --namespace with another",
            file=sys.stderr,
        )
        return None
    return written


def _init_name(args: argparse.Namespace, directory: Path) -> tuple[str | None, str]:
    """The ``<namespace>/<package>`` name `init` writes, and where its namespace came from.

    ``--name`` with a slash is the whole name; otherwise the package segment
    is ``--name``'s or the directory's, and the namespace is ``--namespace``'s
    or the git remote's owner. None with the rejection already printed.
    """
    written_name = str(args.name) if args.name is not None else None
    if written_name is not None and "/" in written_name:
        if args.namespace is not None:
            print(
                "error: init: --name carries the namespace; give one or the other",
                file=sys.stderr,
            )
            return None, ""
        namespace, _, segment = written_name.partition("/")
        checked = _checked_namespace(namespace, "--name")
        if checked is None:
            return None, ""
        return _init_full_name(checked, segment, "--name"), "--name"

    segment_source = written_name if written_name is not None else directory.name
    if not segment_source:
        print(f"error: init: {directory} has no name to take the package's from", file=sys.stderr)
        print("hint: pass --name", file=sys.stderr)
        return None, ""
    segment = _folded_identifier(segment_source)
    if not (is_namespace(segment) and any(char.isalnum() for char in segment)):
        print(
            f"error: init: {segment_source!r} gives no package segment: {segment!r}",
            file=sys.stderr,
        )
        print("hint: pass --name with a package name", file=sys.stderr)
        return None, ""

    if args.namespace is not None:
        chosen = _checked_namespace(str(args.namespace), "--namespace")
        if chosen is None:
            return None, ""
        return _init_full_name(chosen, segment, "--namespace"), "--namespace"

    owner = _git_remote_owner(directory)
    derived = _checked_silently(_folded_identifier(owner)) if owner is not None else None
    if derived is None:
        print("error: init: no namespace to name the package under", file=sys.stderr)
        print(
            "hint: pass --namespace, or --name <namespace>/<package>; none could be "
            "derived from a git remote here",
            file=sys.stderr,
        )
        return None, ""
    return _init_full_name(derived, segment, "the git remote"), "the git remote 'origin'"


def _checked_silently(written: str) -> str | None:
    """`written` as a usable namespace, or None -- for a derived default only."""
    usable = is_namespace(written) and any(char.isalnum() for char in written)
    return written if usable and written not in RESERVED_NAMESPACES else None


def _init_full_name(namespace: str, segment: str, given: str) -> str | None:
    """The two halves joined, the segment checked, or None with the rejection printed."""
    if not (is_namespace(segment) and any(char.isalnum() for char in segment)):
        print(f"error: init: {given} gives no package segment: {segment!r}", file=sys.stderr)
        print(
            "hint: each half of a package name is a lowercase plain identifier",
            file=sys.stderr,
        )
        return None
    return f"{namespace}/{segment}"


def _cmd_init(args: argparse.Namespace, on_warning: OnWarning) -> int:
    """Write the three files a project starts as, into the working directory."""
    directory = Path.cwd()
    written = [directory / MANIFEST_NAME, directory / LOCKFILE_NAME, directory / _STARTER_FILE]
    for path in written:
        if path.exists():
            print(f"error: init: {path} already exists", file=sys.stderr)
            print(
                "hint: init writes a project from scratch and overwrites nothing",
                file=sys.stderr,
            )
            return 1

    name, namespace_source = _init_name(args, directory)
    if name is None:
        return 1

    manifest, lockfile, starter = written
    try:
        starter.parent.mkdir(parents=True, exist_ok=True)
        starter.write_text(_STARTER_QUERY, encoding="utf-8", newline="\n")
        write_manifest(
            manifest,
            name=name,
            version="0.1.0",
            bin={_STARTER_PROGRAM: _STARTER_FILE},
        )
        write_lockfile(lockfile, ())
        # What was just written has to read back, or the next command refuses
        # a project this one made.
        read_manifest(manifest)
        read_lockfile(lockfile)
    except SqlmpegError as err:
        _print_error(err)
        return 1
    except OSError as err:
        print(
            f"error: init: {starter} could not be written: {err.strerror or err}",
            file=sys.stderr,
        )
        return 1

    print(f"wrote {MANIFEST_NAME}, {LOCKFILE_NAME} and {_STARTER_FILE} in {directory}")
    print(
        f"package '{name}' (namespace from {namespace_source}); a project that installs "
        f"it calls its functions as {name.replace('/', '.')}.name()"
    )
    print(
        f"run the starter program: sqlmpeg run {_STARTER_PROGRAM} "
        f"-v source=in.mp4 -v dest=out.mp4"
    )
    return 0


def _lock_to_write(args: argparse.Namespace) -> tuple[Path | None, int]:
    """The lockfile `link`/`unlink` writes, or None with the usage error printed.

    Never creates one as a side effect: outside a project there is nothing to
    record a package in, and inventing a lockfile in whatever directory they
    happen to stand in is not a project.
    """
    if args.global_lock:
        return store.global_lock_path(), 0
    found = find_lockfile(Path.cwd())
    if found is None:
        print(
            f"error: {args.command}: no {LOCKFILE_NAME} in {Path.cwd()} or above it",
            file=sys.stderr,
        )
        print(
            f"hint: run `sqlmpeg init` to start a project here, or "
            f"`sqlmpeg {args.command} -g ...` to put it on this machine",
            file=sys.stderr,
        )
        return None, 2
    return found, 0


def _held_entries(path: Path) -> tuple[LockEntry, ...]:
    """What `path` already pins, or nothing when there is no file there yet."""
    return read_lockfile(path).entries if path.is_file() else ()


def _held_dependencies(path: Path) -> dict[str, str]:
    """What `path`'s own project directly installed, carried over by a rewrite that isn't one."""
    return dict(read_lockfile(path).dependencies) if path.is_file() else {}


def _described(entry: LockEntry) -> str:
    """What an entry being replaced was, for the line that says it is going."""
    if isinstance(entry, LinkEntry):
        return f"the link to {entry.path}"
    return f"the installed {entry.name} {entry.version}"


def _note_cached_catalogue(index: packages_module.Index) -> None:
    """Say that the catalogue came off this machine rather than the registry.

    stderr, like every other diagnostic: `search --json`'s stdout is the
    result a script reads.
    """
    if not index.cached:
        return
    print(f"note: {index.unreachable}", file=sys.stderr)
    print("note: answering from the catalogue cached on this machine", file=sys.stderr)


def _search_rows(listings: tuple[packages_module.Listing, ...]) -> TableResult:
    rows: list[list[CellValue]] = [
        [listing.name, listing.version, listing.description] for listing in listings
    ]
    return TableResult(columns=["package", "version", "description"], rows=rows)


def _cmd_search(args: argparse.Namespace, on_warning: OnWarning) -> int:
    """Print the registry's catalogue, narrowed to `term`. Reads nothing local."""
    try:
        index = packages_module.load_index()
    except SqlmpegError as err:
        _print_error(err)
        return 1
    _note_cached_catalogue(index)
    found = packages_module.search(index, args.term)

    if args.as_json:
        payload = {
            "registry": index.base,
            "cached": index.cached,
            "packages": [listing.to_dict() for listing in found],
        }
        print(json.dumps(payload, indent=2))
        return 0
    # An empty table, not a rejection: a term nothing matches is an answer.
    print(render_table(_search_rows(found)))
    return 0


def _cmd_install(args: argparse.Namespace, on_warning: OnWarning) -> int:
    """Install a package into this project's lockfile, or -g's, and record it.

    Fetches what it depends on too, recursively -- each at its highest
    published version, unless the lockfile already pins that exact version.
    """
    lock, code = _lock_to_write(args)
    if lock is None:
        return code
    manifest = lock.parent / MANIFEST_NAME
    try:
        index = packages_module.load_index()
        _note_cached_catalogue(index)
        installed = packages_module.install(
            index,
            args.package,
            lock=lock,
            manifest=manifest if manifest.is_file() else None,
        )
    except SqlmpegError as err:
        _print_error(err)
        return 1

    release = installed.release
    print(f"installed {release.name} {release.version} in {lock}")
    if installed.replaced is not None:
        print(f"  replacing {_described(installed.replaced)}")
    if not installed.downloaded:
        print("  its content was already in the store; nothing was downloaded")
    if installed.manifest is not None:
        print(f"  recorded in {installed.manifest.name} as a dependency")
    if installed.brought:
        brought = ", ".join(f"{one.name} {one.version}" for one in installed.brought)
        print(f"  brought along as dependencies: {brought}")
    full = release.name.replace("/", ".")
    print(f"a query calls it as {full}.<name>() -- `sqlmpeg list` shows what it provides")
    return 0


def _written_link_path(target: Path, lock: Path, *, relative: bool) -> str:
    """How the lockfile writes the linked directory.

    Relative to the lockfile for a project's own, which keeps the file
    readable beside the tree it points into; absolute for the machine-wide
    one, which sits under the cache directory where relative would only be a
    climb. A path on another drive has no relative form and stays absolute.
    """
    resolved = target.resolve()
    if not relative:
        return str(resolved)
    try:
        return Path(os.path.relpath(resolved, lock.parent)).as_posix()
    except ValueError:
        return str(resolved)


def _cmd_link(args: argparse.Namespace, on_warning: OnWarning) -> int:
    """Record a package read live out of `path`, in this project's lockfile or -g's."""
    target = Path(args.path)
    manifest = target / MANIFEST_NAME
    if not manifest.is_file():
        print(f"error: link: {target} holds no {MANIFEST_NAME}", file=sys.stderr)
        print("hint: link the directory a package's manifest sits in", file=sys.stderr)
        return 1
    lock, code = _lock_to_write(args)
    if lock is None:
        return code

    try:
        package = read_manifest(manifest)
        entries = _held_entries(lock)
        replaced = held_entry(entries, package.name, lock)
        entry = LinkEntry(
            path=_written_link_path(target, lock, relative=not args.global_lock),
        )
        write_lockfile(
            lock, with_entry(entries, entry, replaced), dependencies=_held_dependencies(lock)
        )
    except SqlmpegError as err:
        _print_error(err)
        return 1

    print(f"linked {package.name} -> {entry.path} in {lock}")
    if replaced is not None:
        print(f"  replacing {_described(replaced)}")
    return 0


def _linked_as(entry: LinkEntry, lock: Path) -> str:
    """How one link is named to the user: the package's name, or its bare path."""
    name = stored_name(entry, lock)
    return name if name is not None else f"the unreadable link {entry.path!r}"


def _matching_link(entries: tuple[LockEntry, ...], written: str, lock: Path) -> LinkEntry | None:
    """The link entry `written` names -- by package name, or by directory."""
    for entry in entries:
        if not isinstance(entry, LinkEntry):
            continue
        if stored_name(entry, lock) == written or entry.path == written:
            return entry
        # A dead link is still removable by the directory it points at.
        try:
            if (lock.parent / Path(entry.path)).resolve() == Path(written).resolve():
                return entry
        except (OSError, ValueError):
            continue
    return None


def _cmd_unlink(args: argparse.Namespace, on_warning: OnWarning) -> int:
    """Drop the link `name` names and rewrite the lockfile."""
    lock, code = _lock_to_write(args)
    if lock is None:
        return code
    try:
        entries = _held_entries(lock)
    except SqlmpegError as err:
        _print_error(err)
        return 1

    held = _matching_link(entries, args.name, lock)
    if held is None:
        installed = held_entry(entries, args.name, lock)
        why = "" if installed is None else " -- it is installed, not linked"
        print(f"error: unlink: nothing links '{args.name}' in {lock}{why}", file=sys.stderr)
        linked = [
            _linked_as(entry, lock) for entry in entries if isinstance(entry, LinkEntry)
        ]
        print(
            f"hint: linked: {', '.join(linked)}" if linked else "hint: nothing here is linked",
            file=sys.stderr,
        )
        return 1

    try:
        write_lockfile(lock, without_entry(entries, held), dependencies=_held_dependencies(lock))
    except SqlmpegError as err:
        _print_error(err)
        return 1
    print(f"unlinked '{args.name}' from {lock}")
    return 0


def _cmd_publish(args: argparse.Namespace, on_warning: OnWarning) -> int:
    """The command surface says what publishing will be; nothing publishes yet."""
    print("error: publish: publishing is not open yet", file=sys.stderr)
    print(
        "hint: submit a package as a pull request to the registry repository",
        file=sys.stderr,
    )
    return 1


def _cmd_prompt(args: argparse.Namespace, on_warning: OnWarning) -> int:
    print(build_system_prompt(registry_module.load()))
    return 0


def _cmd_mcp(args: argparse.Namespace, on_warning: OnWarning) -> int:
    """Serve MCP over stdin/stdout; takes no query.

    stdout is the protocol stream from here on, so this handler prints
    nothing to it -- the missing-SDK message goes to stderr like every other
    CLI error, before the server would have started.
    """
    from . import mcp as mcp_module

    if not mcp_module.sdk_available():
        print(f"error: {mcp_module.INSTALL_HINT}", file=sys.stderr)
        return 1
    mcp_module.serve(allow_unsafe=args.allow_unsafe)
    return 0


def _cmd_loudnorm2env(args: argparse.Namespace, on_warning: OnWarning) -> int:
    """stdin (ffmpeg's stderr) -> the ``export SQLMPEG_LN_*=`` block.

    The other half of the printed ``loudnorm2`` command line. Nothing else
    calls it: ``run`` parses the same text through the same function without
    a shell in between.
    """
    try:
        values = loudnorm.parse(sys.stdin.read())
    except ValueError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    print(loudnorm.export_lines(values))
    return 0


_HANDLERS = {
    "compile": _cmd_compile,
    "explain": _cmd_explain,
    "validate": _cmd_validate,
    "run": _cmd_run,
    "list": _cmd_list,
    "init": _cmd_init,
    "search": _cmd_search,
    "install": _cmd_install,
    "link": _cmd_link,
    "unlink": _cmd_unlink,
    "publish": _cmd_publish,
    "prompt": _cmd_prompt,
    "mcp": _cmd_mcp,
    loudnorm.ENV_SUBCOMMAND: _cmd_loudnorm2env,
}


if __name__ == "__main__":
    sys.exit(main())
