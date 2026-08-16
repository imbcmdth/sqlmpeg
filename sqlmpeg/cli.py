"""Command-line interface for sqlmpeg.

Thin wrapper around the library pipeline (``compile_sql`` -> ``emit`` ->
``build_ffmpeg_args``). See the "CLI" section of sqlmpeg-project.md and
plan 008 (plan 037 for the SQL-string-is-the-default-input convention below).

Subcommands:

* ``compile SQL [-f FILE] [--graph-only] [-o OUT] [--no-probe] [--portable]``
  -- print the full ffmpeg command (POSIX-quoted via ``shlex.join``, even on
  Windows -- it is documentation output, not something meant to be pasted
  into cmd.exe), or just the ``-filter_complex`` string with
  ``--graph-only``. Output path resolution (RFC-002, plan 027): ``-o`` if
  given, else the query's ``COPY ... TO`` sink path if it has one, else the
  ``out.mp4`` placeholder (today's default).
* ``explain SQL [-f FILE] [--no-probe] [--portable]`` -- dump the IR graph
  as JSON (the sink, if any, is part of that JSON already).
* ``validate SQL [-f FILE] [--json] [--no-probe] [--portable]`` -- exit 0
  silent on success; on error, exit 1 with either a one-line human message
  or ``err.to_dict()`` JSON.
* ``run SQL [-f FILE] [-o OUT] [--timeout SECS] [-y]`` -- compile and
  execute ffmpeg as a subprocess (guardrail #6: argv list, no shell, timeout
  enforced, stderr captured and surfaced on failure). ``run`` always
  probes -- the files must exist to execute, so there is no ``--no-probe``
  escape hatch here; it has no ``--portable`` either, since it needs an
  installed ffmpeg to execute against regardless. Output path: ``-o`` if
  given, else the query's sink path, else a usage error (exit 2) -- unlike
  ``compile``, ``run`` never falls back to a placeholder path.
* ``prompt [--dynamic]`` -- print the portable LLM system prompt (plan 012)
  to stdout; takes no other arguments and never touches the filesystem.
  ``--dynamic`` (plan 032, RFC-003) appends an "Installed filters" section
  built from this machine's ``ffmpeg -filters``/``-help`` output -- the one
  part of the printed prompt that is machine-dependent; without the flag,
  the output is identical to the portable, tier-1-only base prompt.

``compile``/``explain``/``validate``/``run`` take the query as SQL TEXT
directly on the command line -- ``sqlmpeg compile "SELECT ... FROM
input('x.mp4') a"`` -- since that is the common case. Pass ``-f/--file PATH``
instead to read the query from a file (``-f -`` reads stdin, e.g. for the LLM
repair loop's pipe). Exactly one of the positional SQL string or ``-f`` is
required; giving both or neither is a usage error, exit 2.

Muscle-memory guard: if the positional SQL string fails to compile -- ANY
error code, since a bare filename like ``query.sql`` parses as a SQL column
reference and fails as ``UNSUPPORTED_SQL``, not ``PARSE_ERROR`` -- and it
looks like a file was meant instead (it names a path that exists, or ends in
``.sql``/``.SQL``), a second stderr line suggests ``-f``. No legitimate query
ever looks like a file path. This is CLI-layer-only sugar: it never touches
``SqlmpegError`` or the machine-readable ``--json`` output, which always
prints the library's error verbatim on stdout.

``--no-probe`` (compile/explain/validate only) skips ffprobe entirely for a
byte-reproducible, fully offline compile (RFC-001 "Probing policy"): no
``STREAM_NOT_FOUND``/``BROADCAST_MISMATCH`` validation, ``SELECT *`` and bare
array splats fail with ``INPUT_NOT_FOUND``, and provenance metadata is never
attached.

``--portable`` (compile/explain/validate only; RFC-003) compiles against the
stdlib alone -- no ffmpeg filter registry is even constructed, so a query
naming a dynamic (tier-2) filter or passing a named option is rejected
(``UNKNOWN_FUNCTION`` / ``UNSUPPORTED_SQL``) exactly as it would be on a
machine with no ffmpeg installed at all. Use it to confirm a query will
compile on someone else's machine.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import registry as registry_module
from .compiler import compile_sql
from .emit import Emitted, build_ffmpeg_args, emit
from .errors import SqlmpegError
from .ir import Graph
from .prompt import build_system_prompt

__all__ = ["main"]

_DEFAULT_OUT = "out.mp4"
_DEFAULT_TIMEOUT = 600
_STDERR_TAIL_LINES = 30


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_usage(sys.stderr)
        return 2

    handler = _HANDLERS[args.command]
    return handler(args)


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


_QUERY_HELP = "SQL query text (exactly one of this or -f/--file is required)"
_FILE_HELP = "read the query from a file instead of the command line ('-' for stdin)"


def _add_query_arguments(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("query", nargs="?", default=None, help=_QUERY_HELP)
    subparser.add_argument("-f", "--file", default=None, help=_FILE_HELP)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sqlmpeg", description="SQL frontend for FFmpeg filtergraphs"
    )
    subparsers = parser.add_subparsers(dest="command")

    compile_p = subparsers.add_parser("compile", help="compile SQL to an ffmpeg command")
    _add_query_arguments(compile_p)
    compile_p.add_argument(
        "--graph-only", action="store_true", help="print only the filter_complex string"
    )
    compile_p.add_argument(
        "-o",
        "--output",
        default=None,
        help="output path (default: the query's sink path, else out.mp4)",
    )
    compile_p.add_argument(
        "--no-probe", action="store_true", help="skip ffprobe; fully offline, symbolic compile"
    )
    compile_p.add_argument(
        "--portable",
        action="store_true",
        help="reject filters and named options that depend on the installed ffmpeg",
    )

    explain_p = subparsers.add_parser("explain", help="dump the compiled IR graph as JSON")
    _add_query_arguments(explain_p)
    explain_p.add_argument(
        "--no-probe", action="store_true", help="skip ffprobe; fully offline, symbolic compile"
    )
    explain_p.add_argument(
        "--portable",
        action="store_true",
        help="reject filters and named options that depend on the installed ffmpeg",
    )

    validate_p = subparsers.add_parser("validate", help="check that a query compiles")
    _add_query_arguments(validate_p)
    validate_p.add_argument(
        "--json", action="store_true", dest="as_json", help="emit the error as JSON"
    )
    validate_p.add_argument(
        "--no-probe", action="store_true", help="skip ffprobe; fully offline, symbolic compile"
    )
    validate_p.add_argument(
        "--portable",
        action="store_true",
        help="reject filters and named options that depend on the installed ffmpeg",
    )

    run_p = subparsers.add_parser("run", help="compile and execute ffmpeg")
    _add_query_arguments(run_p)
    run_p.add_argument(
        "-o",
        "--output",
        default=None,
        help="output file path (default: the query's sink path)",
    )
    run_p.add_argument(
        "--timeout", type=float, default=_DEFAULT_TIMEOUT, help="ffmpeg timeout in seconds"
    )
    run_p.add_argument(
        "-y", action="store_true", dest="overwrite", help="pass -y (overwrite) to ffmpeg"
    )

    prompt_p = subparsers.add_parser("prompt", help="print the LLM system prompt for this dialect")
    prompt_p.add_argument(
        "--dynamic",
        action="store_true",
        help="append this machine's installed ffmpeg filter list (machine-dependent)",
    )

    return parser


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _read_file(path: str) -> str | None:
    """Read query text from `path` (or stdin for "-"). None + printed error on failure."""
    if path == "-":
        return sys.stdin.read()
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as err:
        print(f"error: could not read {path!r}: {err.strerror or err}", file=sys.stderr)
        return None


def _resolve_query(args: argparse.Namespace) -> tuple[str | None, int]:
    """Resolve the query text for compile/explain/validate/run.

    Exactly one of the positional ``query`` (inline SQL) or ``-f/--file`` is
    required. Returns ``(text, 0)`` on success, or ``(None, exit_code)`` with
    the error already printed to stderr: 2 for the usage violation (both or
    neither given), 1 for a file that could not be read.
    """
    has_query = args.query is not None
    has_file = args.file is not None
    if has_query and has_file:
        print(
            f"error: {args.command}: give a SQL string or -f/--file, not both",
            file=sys.stderr,
        )
        return None, 2
    if not has_query and not has_file:
        print(
            f"error: {args.command}: give a SQL string or -f/--file",
            file=sys.stderr,
        )
        return None, 2
    if has_file:
        text = _read_file(args.file)
        if text is None:
            return None, 1
        return text, 0
    assert args.query is not None
    return args.query, 0


def _maybe_print_file_hint(err: SqlmpegError, source: str | None) -> None:
    """Muscle-memory guard (plan 037): an inline positional string that names
    an existing file, or ends in .sql/.SQL, probably meant -f/--file. Fires on
    ANY compile error, not just PARSE_ERROR -- a bare filename like
    `query.sql` parses as a SQL column reference and fails as UNSUPPORTED_SQL,
    and no legitimate query ever looks like a file path. CLI-layer sugar only
    -- never touches `err` itself."""
    if source is None:
        return
    if os.path.exists(source) or source.lower().endswith(".sql"):
        print(
            f"hint: '{source}' looks like a file; did you mean -f '{source}'?",
            file=sys.stderr,
        )


def _print_error(err: SqlmpegError, *, source: str | None = None) -> None:
    print(f"error: {err}", file=sys.stderr)
    _maybe_print_file_hint(err, source)


def _check_output_dir(out_path: str) -> str | None:
    """Return an error message if `out_path`'s parent directory does not exist."""
    parent = Path(out_path).parent
    if str(parent) and not parent.exists():
        return f"error: output directory does not exist: {parent}"
    return None


def _resolve_out_path(cli_output: str | None, graph: Graph, *, default: str | None) -> str | None:
    """Output path precedence (RFC-002, plan 027): ``-o`` > sink path > `default`."""
    if cli_output is not None:
        return cli_output
    if graph.sink is not None:
        return graph.sink.path
    return default


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------


def _cmd_compile(args: argparse.Namespace) -> int:
    text, code = _resolve_query(args)
    if text is None:
        return code

    try:
        graph = compile_sql(text, probe=not args.no_probe, portable=args.portable)
        emitted = emit(graph)
    except SqlmpegError as err:
        _print_error(err, source=args.query)
        return 1

    if args.graph_only:
        print(emitted.filter_complex)
        return 0

    out_path = _resolve_out_path(args.output, graph, default=_DEFAULT_OUT)
    ffmpeg_args = build_ffmpeg_args(emitted, out_path)
    print(shlex.join(ffmpeg_args))
    return 0


def _cmd_explain(args: argparse.Namespace) -> int:
    text, code = _resolve_query(args)
    if text is None:
        return code

    try:
        graph = compile_sql(text, probe=not args.no_probe, portable=args.portable)
    except SqlmpegError as err:
        _print_error(err, source=args.query)
        return 1

    print(json.dumps(graph.to_dict(), indent=2))
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    text, code = _resolve_query(args)
    if text is None:
        return code

    try:
        compile_sql(text, probe=not args.no_probe, portable=args.portable)
    except SqlmpegError as err:
        if args.as_json:
            # Machine contract: stdout stays pure JSON, the library error
            # verbatim. The file-hint is human-output sugar only, so it goes
            # to stderr even here rather than perturbing stdout.
            print(json.dumps(err.to_dict()))
            _maybe_print_file_hint(err, args.query)
        else:
            _print_error(err, source=args.query)
        return 1

    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    text, code = _resolve_query(args)
    if text is None:
        return code

    try:
        graph: Graph = compile_sql(text)
        emitted: Emitted = emit(graph)
    except SqlmpegError as err:
        _print_error(err, source=args.query)
        return 1

    out_path = _resolve_out_path(args.output, graph, default=None)
    if out_path is None:
        print(
            "error: no output path given: pass -o, or use COPY ... TO in the query",
            file=sys.stderr,
        )
        return 2

    dir_error = _check_output_dir(out_path)
    if dir_error is not None:
        print(dir_error, file=sys.stderr)
        return 1

    if shutil.which("ffmpeg") is None:
        print("error: ffmpeg not found on PATH", file=sys.stderr)
        return 1

    ffmpeg_args = build_ffmpeg_args(emitted, out_path)
    if args.overwrite:
        ffmpeg_args.insert(1, "-y")

    try:
        result = subprocess.run(
            ffmpeg_args,
            timeout=args.timeout,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        print(f"error: ffmpeg timed out after {args.timeout}s", file=sys.stderr)
        return 1

    if result.returncode != 0:
        print(f"error: ffmpeg exited with code {result.returncode}", file=sys.stderr)
        tail = result.stderr.splitlines()[-_STDERR_TAIL_LINES:]
        for line in tail:
            print(line, file=sys.stderr)
        return result.returncode

    return 0


def _cmd_prompt(args: argparse.Namespace) -> int:
    dynamic = registry_module.load() if args.dynamic else None
    print(build_system_prompt(dynamic=dynamic))
    return 0


_HANDLERS = {
    "compile": _cmd_compile,
    "explain": _cmd_explain,
    "validate": _cmd_validate,
    "run": _cmd_run,
    "prompt": _cmd_prompt,
}


if __name__ == "__main__":
    sys.exit(main())
