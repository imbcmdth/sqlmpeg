"""The compiler pipeline: SQL text in, IR :class:`~sqlmpeg.ir.Graph` out.

``compile_sql`` chains the first three passes plus the split pass::

    parse -> resolve -> lower -> insert_splits

The returned graph is split-complete, i.e. every pad has exactly one consumer,
which is what :func:`sqlmpeg.emit.emit` expects.

Guardrail #7 lives here: no input, however malformed, may produce anything but
a compile result or a :class:`~sqlmpeg.errors.SqlmpegError`. Each pass already
carries its own backstop; this one catches anything that still slips through
(including recursion limits and sqlglot internals) and reports it as
``INTERNAL``, the code the fuzz corpus asserts never fires.
"""

from __future__ import annotations

from .errors import ErrorCode, SqlmpegError
from .ir import Graph
from .lower import lower
from .parser import parse, resolve
from .split import insert_splits

__all__ = ["compile_sql"]


def compile_sql(text: str) -> Graph:
    """Compile SQL `text` into a split-complete IR graph.

    Raises ``SqlmpegError`` — and nothing else — on every rejection.
    """
    try:
        return insert_splits(lower(resolve(parse(text))))
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
