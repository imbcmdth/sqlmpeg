"""SQL frontend for FFmpeg filtergraphs.

- ``compile_sql`` / ``compile_commands`` -- SQL text in, IR :class:`~sqlmpeg.ir.Graph`
  (or one per ffmpeg command) out.
- ``compile_table_sql`` -- SQL text in, printable table/csv result set(s) out.
- ``classify`` -- cheap static check of whether SQL text is a table query.
- ``emit`` -- a :class:`~sqlmpeg.ir.Graph` in, an :class:`~sqlmpeg.emit.Emitted`
  filtergraph description out.
- ``build_ffmpeg_commands`` -- an :class:`~sqlmpeg.emit.Emitted` in, the ffmpeg
  argv list(s) to run out.
- ``execute`` -- those :class:`~sqlmpeg.emit.Emitted` objects in, an
  :class:`~sqlmpeg.execute.ExecutionResult` (a :class:`~sqlmpeg.execute.CommandResult`
  per command) out, ffmpeg having run.
- ``render_table`` / ``render_csv`` -- one ``compile_table_sql``
  :class:`~sqlmpeg.table.TableSink`'s result in, its printable text out.
- ``probe`` -- a media path in, its :class:`~sqlmpeg.probe.ProbeResult` out (None
  if it cannot be read).
- ``discover`` -- a directory or query file path in, the
  :class:`~sqlmpeg.project.PackageSet` of the ``sqlmpeg.json`` project above it
  out (None outside a project), for ``compile_sql``'s ``packages`` argument.
- ``load_registry`` -- this machine's ffmpeg described as a
  :class:`~sqlmpeg.registry.Registry`, the argument ``build_system_prompt`` takes.
- ``build_system_prompt`` -- a :class:`~sqlmpeg.registry.Registry` in, an LLM
  system prompt describing the dialect out.
- ``SqlmpegError`` / ``ErrorCode`` -- the typed exception every rejection raises.
- ``SqlmpegWarning`` / ``WarningCode`` -- what a compile says without refusing it,
  through ``compile_sql``'s optional ``on_warning`` callback.

Usage::

    graph = compile_sql("SELECT a.video[1] FROM input('clip.mp4') a")
    emitted = emit(graph)
    argv = build_ffmpeg_commands(emitted)[0]
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .compiler import classify, compile_commands, compile_sql, compile_table_sql
from .emit import build_ffmpeg_commands, emit
from .errors import ErrorCode, SqlmpegError
from .execute import CommandResult, ExecutionResult, execute
from .probe import probe
from .project import PackageSet, discover
from .prompt import build_system_prompt
from .registry import Registry
from .registry import load as load_registry
from .table import TableSink, render_csv, render_table
from .warnings import SqlmpegWarning, WarningCode

try:
    __version__ = version("sqlmpeg")
except PackageNotFoundError:  # source checkout without install metadata
    __version__ = "0+unknown"

__all__ = [
    "CommandResult",
    "ErrorCode",
    "ExecutionResult",
    "PackageSet",
    "Registry",
    "SqlmpegError",
    "SqlmpegWarning",
    "TableSink",
    "WarningCode",
    "build_ffmpeg_commands",
    "build_system_prompt",
    "classify",
    "compile_commands",
    "compile_sql",
    "compile_table_sql",
    "discover",
    "emit",
    "execute",
    "load_registry",
    "probe",
    "render_csv",
    "render_table",
]
