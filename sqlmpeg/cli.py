"""Command-line interface for sqlmpeg.

Thin wrapper around the library pipeline (``compile_sql`` -> ``emit`` ->
``build_ffmpeg_args``). See the "CLI" section of sqlmpeg-project.md and
plan 008.

Subcommands:

* ``compile QUERY.sql [--graph-only] [-o OUT]`` -- print the full ffmpeg
  command (POSIX-quoted via ``shlex.join``, even on Windows -- it is
  documentation output, not something meant to be pasted into cmd.exe), or
  just the ``-filter_complex`` string with ``--graph-only``.
* ``explain QUERY.sql`` -- dump the IR graph as JSON.
* ``validate QUERY.sql [--json]`` -- exit 0 silent on success; on error,
  exit 1 with either a one-line human message or ``err.to_dict()`` JSON.
* ``run QUERY.sql -o OUT [--timeout SECS] [-y]`` -- compile and execute
  ffmpeg as a subprocess (guardrail #6: argv list, no shell, timeout
  enforced, stderr captured and surfaced on failure).

``QUERY.sql`` may be ``-`` to read the query text from stdin in every
subcommand.
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .compiler import compile_sql
from .emit import Emitted, build_ffmpeg_args, emit
from .errors import SqlmpegError
from .ir import Graph

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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sqlmpeg", description="SQL frontend for FFmpeg filtergraphs"
    )
    subparsers = parser.add_subparsers(dest="command")

    compile_p = subparsers.add_parser("compile", help="compile SQL to an ffmpeg command")
    compile_p.add_argument("query", help="path to a .sql file, or - for stdin")
    compile_p.add_argument(
        "--graph-only", action="store_true", help="print only the filter_complex string"
    )
    compile_p.add_argument("-o", "--output", default=_DEFAULT_OUT, help="output path placeholder")

    explain_p = subparsers.add_parser("explain", help="dump the compiled IR graph as JSON")
    explain_p.add_argument("query", help="path to a .sql file, or - for stdin")

    validate_p = subparsers.add_parser("validate", help="check that a query compiles")
    validate_p.add_argument("query", help="path to a .sql file, or - for stdin")
    validate_p.add_argument(
        "--json", action="store_true", dest="as_json", help="emit the error as JSON"
    )

    run_p = subparsers.add_parser("run", help="compile and execute ffmpeg")
    run_p.add_argument("query", help="path to a .sql file, or - for stdin")
    run_p.add_argument("-o", "--output", required=True, help="output file path")
    run_p.add_argument(
        "--timeout", type=float, default=_DEFAULT_TIMEOUT, help="ffmpeg timeout in seconds"
    )
    run_p.add_argument(
        "-y", action="store_true", dest="overwrite", help="pass -y (overwrite) to ffmpeg"
    )

    return parser


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _read_query(path: str) -> str | None:
    """Read query text from `path` (or stdin for "-"). None + printed error on failure."""
    if path == "-":
        return sys.stdin.read()
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as err:
        print(f"error: could not read {path!r}: {err.strerror or err}", file=sys.stderr)
        return None


def _print_error(err: SqlmpegError) -> None:
    print(f"error: {err}", file=sys.stderr)


def _check_output_dir(out_path: str) -> str | None:
    """Return an error message if `out_path`'s parent directory does not exist."""
    parent = Path(out_path).parent
    if str(parent) and not parent.exists():
        return f"error: output directory does not exist: {parent}"
    return None


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------


def _cmd_compile(args: argparse.Namespace) -> int:
    text = _read_query(args.query)
    if text is None:
        return 1

    try:
        graph = compile_sql(text)
        emitted = emit(graph)
    except SqlmpegError as err:
        _print_error(err)
        return 1

    if args.graph_only:
        print(emitted.filter_complex)
        return 0

    ffmpeg_args = build_ffmpeg_args(emitted, args.output)
    print(shlex.join(ffmpeg_args))
    return 0


def _cmd_explain(args: argparse.Namespace) -> int:
    text = _read_query(args.query)
    if text is None:
        return 1

    try:
        graph = compile_sql(text)
    except SqlmpegError as err:
        _print_error(err)
        return 1

    print(json.dumps(graph.to_dict(), indent=2))
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    text = _read_query(args.query)
    if text is None:
        return 1

    try:
        compile_sql(text)
    except SqlmpegError as err:
        if args.as_json:
            print(json.dumps(err.to_dict()))
        else:
            _print_error(err)
        return 1

    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    text = _read_query(args.query)
    if text is None:
        return 1

    try:
        graph: Graph = compile_sql(text)
        emitted: Emitted = emit(graph)
    except SqlmpegError as err:
        _print_error(err)
        return 1

    dir_error = _check_output_dir(args.output)
    if dir_error is not None:
        print(dir_error, file=sys.stderr)
        return 1

    if shutil.which("ffmpeg") is None:
        print("error: ffmpeg not found on PATH", file=sys.stderr)
        return 1

    ffmpeg_args = build_ffmpeg_args(emitted, args.output)
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


_HANDLERS = {
    "compile": _cmd_compile,
    "explain": _cmd_explain,
    "validate": _cmd_validate,
    "run": _cmd_run,
}


if __name__ == "__main__":
    sys.exit(main())
