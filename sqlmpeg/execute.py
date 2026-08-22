"""Run a compiled sequence of ffmpeg commands.

:func:`execute` is the half of ``run`` that is not printing: it walks the
:class:`~sqlmpeg.emit.Emitted` list, builds each command's argv
(:func:`~sqlmpeg.emit.build_ffmpeg_commands`), inserts ``-hide_banner`` and
``-y``/``-n``, runs it as a subprocess with a per-command timeout, and stops
at the first nonzero exit -- whose code becomes the run's. No shell, on any
platform: the argv list goes straight to :func:`subprocess.run`.

``sqlmpeg.loudnorm2`` is the one graph whose commands are not independent.
Its measuring pass prints the measurements as a JSON block on stderr, so that
pass is ALWAYS captured, parsed in process (:func:`sqlmpeg.loudnorm.parse`)
and substituted into the correction pass's argv
(:func:`sqlmpeg.loudnorm.substitute`) -- the ``eval "$(...)"`` the printed
command line shows is only for a pasted command.

Two stderr modes, because two callers want opposite things:

* ``capture_stderr=False`` (the default, and what the CLI passes) leaves
  ffmpeg's stderr inherited -- progress lines land on the user's terminal as
  they are written, and :attr:`CommandResult.stderr` is empty for every
  command but loudnorm2's measuring pass;
* ``capture_stderr=True`` pipes every command's stderr into its
  :class:`CommandResult`, for a library or server caller that has no terminal
  to share.

Nothing here prints or raises: the caller reads :class:`ExecutionResult` --
the argv actually run per command, the exit code, the captured stderr, and
which of the two non-ffmpeg failures (a timeout, an unparseable measuring
pass) ended the run -- and words its own messages.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from . import loudnorm
from .emit import Emitted, build_ffmpeg_commands

__all__ = [
    "DEFAULT_TIMEOUT",
    "CommandResult",
    "ExecutionResult",
    "execute",
]

# Per command, not per run.
DEFAULT_TIMEOUT = 600

# What a timeout and an unparseable measuring pass report, neither being an
# ffmpeg exit code.
_FAILED = 1


@dataclass(frozen=True)
class CommandResult:
    """One ffmpeg subprocess: the argv actually run, and how it ended."""

    argv: list[str]
    exit_code: int
    # The command's stderr when it was captured, "" when it went to the
    # caller's own stderr. `captured` tells the two empties apart.
    stderr: str = ""
    captured: bool = False


@dataclass(frozen=True)
class ExecutionResult:
    """Every command run, in order, plus the run's own outcome."""

    commands: list[CommandResult] = field(default_factory=list)
    # The first nonzero ffmpeg exit, or 1 for a timeout / measuring-pass
    # failure, or 0.
    exit_code: int = 0
    # True when a command hit the timeout; its argv is the last `commands`
    # entry, and no later command started.
    timed_out: bool = False
    # The `loudnorm.parse` failure text when a measuring pass printed no
    # loudnorm JSON block; None otherwise.
    measure_error: str | None = None


def execute(
    emitted: Sequence[Emitted],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    overwrite: bool = False,
    capture_stderr: bool = False,
    echo: Callable[[list[str]], None] | None = None,
) -> ExecutionResult:
    """Run every command of `emitted`, in order, stopping at the first failure.

    A two-pass sink compiles to two commands, a loudnorm2 graph to two, a
    fan-out COPY to one per row, every other query to one. `timeout` is per
    command. `overwrite` picks ffmpeg's ``-y`` over ``-n``. `echo` is called
    with each argv just before its subprocess starts, so a caller that prints
    the command line interleaves with ffmpeg's own output the way the CLI
    does.
    """
    results: list[CommandResult] = []
    measured: dict[str, str] = {}

    for e in emitted:
        commands = build_ffmpeg_commands(e)
        measures = bool(e.measure_filter_complex)
        for index, command in enumerate(commands):
            # The measuring pass is captured whatever the caller asked for:
            # parsing its stderr is the only reason it runs.
            measuring = measures and index == 0
            capture = capture_stderr or measuring

            argv = [loudnorm.substitute(word, measured) for word in command]
            argv.insert(1, "-y" if overwrite else "-n")
            argv.insert(1, "-hide_banner")

            if echo is not None:
                echo(argv)

            try:
                code, captured = _run_ffmpeg(argv, timeout, capture=capture)
            except subprocess.TimeoutExpired as err:
                # Whatever the killed child had written by then, if captured.
                partial = err.stderr if isinstance(err.stderr, str) else ""
                results.append(CommandResult(argv, _FAILED, partial, capture))
                return ExecutionResult(results, _FAILED, timed_out=True)

            results.append(CommandResult(argv, code, captured, capture))
            if code != 0:
                return ExecutionResult(results, code)

            if measuring:
                try:
                    measured = loudnorm.parse(captured)
                except ValueError as err:
                    return ExecutionResult(results, _FAILED, measure_error=str(err))

    return ExecutionResult(results)


def _run_ffmpeg(argv: list[str], timeout: float, *, capture: bool) -> tuple[int, str]:
    """Run one ffmpeg command; ``(exit code, its stderr)``.

    Uncaptured stderr writes straight through to the caller's terminal,
    progress lines included, and comes back as "".
    """
    if not capture:
        return subprocess.run(argv, timeout=timeout).returncode, ""
    done = subprocess.run(argv, timeout=timeout, stderr=subprocess.PIPE, text=True)
    return done.returncode, done.stderr
