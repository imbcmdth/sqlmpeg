"""psql-style variable substitution for query text, and the header that declares them.

``substitute(text, variables)`` scans `text` for three reference forms --
``:'name'`` (quoted string literal), ``:"name"`` (quoted identifier), and
bare ``:name`` (raw text) -- and replaces each with a value from
`variables`. The scan skips the same opaque spans the SQL lexer itself
does: ``'...'`` strings, ``"..."`` identifiers, ``--`` line comments, and
``/* */`` block comments. A ``::`` cast and a lone ``:`` pass through
unchanged.

An UNSET reference substitutes to the bare keyword ``NULL`` -- absence, which
every binding site treats as "not written" and the required positions reject.
The returned :class:`Substitution` maps each such NULL's (line, col) in the
substituted text back to the variable's name, so a rejection at the NULL's
point of use can say ``':source' was not set`` instead of "NULL is not a
path". The map exists for messages only.

``referenced(text)`` reports which names `text` references, over the same
scan. ``declared_variables(text)`` reads the other direction: the
``-- variables:`` header a runnable query carries, naming what the reader
has to supply::

    -- variables: source (input media path), prefix (output name prefix)

It is a comment, so nothing enforces it and a query without one declares
nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .errors import ErrorCode, SqlmpegError

__all__ = [
    "Substitution",
    "Variable",
    "declared_variables",
    "referenced",
    "substitute",
    "unset_error",
    "unset_variable",
]

_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_NULL = "NULL"


@dataclass(frozen=True)
class Substitution:
    """Substituted query text, plus where each unset variable's NULL landed.

    `unset` is keyed by the NULL keyword's 1-based (line, col) in `text` --
    the same coordinates sqlglot records on the parsed node, which is how a
    later rejection finds the variable's name.
    """

    text: str
    unset: dict[tuple[int, int], str] = field(default_factory=dict)


def substitute(text: str, variables: dict[str, str]) -> Substitution:
    out: list[str] = []
    length = 0  # of the output built so far
    nulls: list[tuple[int, str]] = []  # (output offset, variable name)
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "'" or ch == '"':
            end = _scan_quoted(text, i, ch)
            out.append(text[i:end])
            length += end - i
            i = end
            continue
        if text.startswith("--", i):
            end = text.find("\n", i)
            end = n if end == -1 else end
            out.append(text[i:end])
            length += end - i
            i = end
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append(text[i:end])
            length += end - i
            i = end
            continue
        if ch == ":" and text.startswith("::", i):
            out.append("::")
            length += 2
            i += 2
            continue
        if ch == ":":
            found = _match_reference(text, i)
            if found is not None:
                name, ref_end = found
                if name in variables:
                    replacement = _replacement(text[i + 1], variables[name])
                else:
                    replacement = _NULL
                    nulls.append((length, name))
                out.append(replacement)
                length += len(replacement)
                i = ref_end
                continue
        out.append(ch)
        length += 1
        i += 1
    result = "".join(out)
    return Substitution(
        text=result,
        unset={_line_col(result, offset): name for offset, name in nulls},
    )


def referenced(text: str) -> set[str]:
    """Every variable name `text` references, over the same scan as `substitute`."""
    names: set[str] = set()
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "'" or ch == '"':
            i = _scan_quoted(text, i, ch)
            continue
        if text.startswith("--", i):
            end = text.find("\n", i)
            i = n if end == -1 else end
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        if ch == ":" and text.startswith("::", i):
            i += 2
            continue
        if ch == ":":
            found = _match_reference(text, i)
            if found is not None:
                name, ref_end = found
                names.add(name)
                i = ref_end
                continue
        i += 1
    return names


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


def _match_reference(text: str, start: int) -> tuple[str, int] | None:
    """The `:name`/`:'name'`/`:"name"` reference at `start` as (name, end
    offset), or None if nothing there fits the shape (the caller copies the
    colon as-is)."""
    next_ch = text[start + 1] if start + 1 < len(text) else ""
    quote = next_ch if next_ch in ("'", '"') else ""
    name_start = start + 2 if quote else start + 1
    match = _NAME_RE.match(text, name_start)
    if match is None:
        return None
    name_end = match.end()
    if quote:
        if name_end >= len(text) or text[name_end] != quote:
            return None
        ref_end = name_end + 1
    else:
        ref_end = name_end
    return match.group(), ref_end


def _replacement(quote: str, value: str) -> str:
    """A set variable's value, quoted the way the reference form asks."""
    if quote == "'":
        return "'" + value.replace("'", "''") + "'"
    if quote == '"':
        return '"' + value.replace('"', '""') + '"'
    return value


def _line_col(text: str, offset: int) -> tuple[int, int]:
    """1-indexed (line, col) of `offset` in `text`."""
    line = text.count("\n", 0, offset) + 1
    last_newline = text.rfind("\n", 0, offset)
    col = offset - last_newline
    return line, col


# -- the unset-variable rejection, built and read back in one place --------

_UNSET_RE = re.compile(r"^':([A-Za-z_][A-Za-z0-9_]*)' was not set\b")


def unset_error(
    code: ErrorCode,
    name: str,
    *,
    what: str,
    line: int | None,
    col: int | None,
) -> SqlmpegError:
    """The rejection for an unset variable's NULL landing where a value is
    required. `what` says what the position needed, and lands in the hint."""
    return SqlmpegError(
        code,
        f"':{name}' was not set",
        line=line,
        col=col,
        hint=f"{what}; set it with -v {name}=<value>",
    )


def unset_variable(err: SqlmpegError) -> str | None:
    """The variable name an :func:`unset_error` rejection is about, or None."""
    match = _UNSET_RE.match(err.message)
    return match.group(1) if match is not None else None


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
