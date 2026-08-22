"""psql-style variable substitution for query text, and the header that declares them.

``substitute(text, variables)`` scans `text` for three reference forms --
``:'name'`` (quoted string literal), ``:"name"`` (quoted identifier), and
bare ``:name`` (raw text) -- and replaces each with a value from
`variables`. The scan skips the same opaque spans the SQL lexer itself
does: ``'...'`` strings, ``"..."`` identifiers, ``--`` line comments, and
``/* */`` block comments. A ``::`` cast and a lone ``:`` pass through
unchanged.

An undefined reference raises `SqlmpegError` (`UNSUPPORTED_SQL`) anchored at
the reference's own line:col.

``declared_variables(text)`` reads the other direction: the ``-- variables:``
header a runnable query carries, naming what the reader has to supply::

    -- variables: source (input media path), prefix (output name prefix)

It is a comment, so nothing enforces it and a query without one declares
nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import ErrorCode, SqlmpegError

__all__ = ["Variable", "declared_variables", "substitute"]

_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def substitute(text: str, variables: dict[str, str]) -> str:
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "'" or ch == '"':
            end = _scan_quoted(text, i, ch)
            out.append(text[i:end])
            i = end
            continue
        if text.startswith("--", i):
            end = text.find("\n", i)
            end = n if end == -1 else end
            out.append(text[i:end])
            i = end
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append(text[i:end])
            i = end
            continue
        if ch == ":" and text.startswith("::", i):
            out.append("::")
            i += 2
            continue
        if ch == ":":
            replacement, next_i = _match_reference(text, i, variables)
            if replacement is not None:
                out.append(replacement)
                i = next_i
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _scan_quoted(text: str, start: int, quote: str) -> int:
    """End offset (exclusive) of the ``'...'``/``"..."`` run at `start`; a
    doubled quote (``''``, ``\"\"``) stays inside the run."""
    i = start + 1
    n = len(text)
    while i < n:
        if text[i] == quote:
            if i + 1 < n and text[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    return n


def _match_reference(
    text: str, start: int, variables: dict[str, str]
) -> tuple[str | None, int]:
    """A `:name`/`:'name'`/`:"name"` reference at `start`, or (None, start)
    if nothing there fits the shape (the caller copies the colon as-is)."""
    next_ch = text[start + 1] if start + 1 < len(text) else ""
    quote = next_ch if next_ch in ("'", '"') else ""
    name_start = start + 2 if quote else start + 1
    match = _NAME_RE.match(text, name_start)
    if match is None:
        return None, start
    name_end = match.end()
    if quote:
        if name_end >= len(text) or text[name_end] != quote:
            return None, start
        ref_end = name_end + 1
    else:
        ref_end = name_end
    name = match.group()
    if name not in variables:
        line, col = _line_col(text, start)
        if variables:
            hint = f"defined: {', '.join(sorted(variables))}"
        else:
            hint = "define it with -v name=value"
        raise SqlmpegError(
            ErrorCode.UNSUPPORTED_SQL,
            f"undefined variable ':{name}'",
            line=line,
            col=col,
            hint=hint,
        )
    value = variables[name]
    if quote == "'":
        replacement = "'" + value.replace("'", "''") + "'"
    elif quote == '"':
        replacement = '"' + value.replace('"', '""') + '"'
    else:
        replacement = value
    return replacement, ref_end


def _line_col(text: str, offset: int) -> tuple[int, int]:
    """1-indexed (line, col) of `offset` in `text`."""
    line = text.count("\n", 0, offset) + 1
    last_newline = text.rfind("\n", 0, offset)
    col = offset - last_newline
    return line, col


# -- the declaring header --------------------------------------------------

_HEADER_RE = re.compile(r"^--\s*variables:\s*(?P<body>.+)$", re.MULTILINE)


@dataclass(frozen=True)
class Variable:
    """One variable a query declares: its name, and what the header says it is."""

    name: str
    description: str = ""


def declared_variables(text: str) -> tuple[Variable, ...]:
    """The variables `text`'s ``-- variables:`` header declares, in written order.

    Empty for a query with no such header: the header is documentation, and a
    query is free not to carry one. A description is whatever the parentheses
    after a name hold, commas and all; a name written without them declares
    itself and nothing more.
    """
    header = _HEADER_RE.search(text)
    if header is None:
        return ()
    body = header.group("body")
    found: list[Variable] = []
    at = 0
    while at < len(body):
        match = _NAME_RE.search(body, at)
        if match is None:
            break
        description, at = _description(body, match.end())
        found.append(Variable(name=match.group(), description=description))
        # Past the separating comma, so a description's own words are not read
        # as further names.
        comma = body.find(",", at)
        at = len(body) if comma == -1 else comma + 1
    return tuple(found)


def _description(body: str, start: int) -> tuple[str, int]:
    """The ``(...)`` description at `start`, and where it ends. ("", start) if there is none."""
    at = start
    while at < len(body) and body[at].isspace():
        at += 1
    if at >= len(body) or body[at] != "(":
        return "", start
    depth = 0
    for end in range(at, len(body)):
        if body[end] == "(":
            depth += 1
        elif body[end] == ")":
            depth -= 1
            if depth == 0:
                return body[at + 1 : end].strip(), end + 1
    return body[at + 1 :].strip(), len(body)  # unclosed: the rest of the line
