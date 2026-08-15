"""Fuzz test for guardrail #7 ("no panics on user input").

Seed corpus: the ``.sql`` text of every fixture under ``tests/golden/``
(both the accepted queries and the ones that are expected to be rejected --
the mutation strategy does not care which). Each example takes a seed and
applies a handful of random mutations to it: slice deletion, slice
duplication, single-character swap, random-token injection, and
random-unicode injection.

Property (spec: "Fuzz" in the Testing section, guardrail #7): ``compile_sql``
on arbitrarily mutated SQL text either returns a ``Graph`` or raises
``SqlmpegError`` -- and nothing else. This intentionally does NOT assert
``err.code is not ErrorCode.INTERNAL``: ``INTERNAL`` is an allowed outcome
for garbage input, it is only forbidden to escape the ``SqlmpegError``
wrapper (see ``compiler.py``'s guardrail #7 docstring).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from sqlmpeg.compiler import compile_sql
from sqlmpeg.errors import SqlmpegError
from sqlmpeg.ir import Graph

_GOLDEN_DIR = Path(__file__).parent / "golden"
_CORPUS: list[str] = sorted(
    p.read_text(encoding="utf-8") for p in _GOLDEN_DIR.glob("*.sql")
)
assert _CORPUS, f"no seed queries found under {_GOLDEN_DIR}"

_MAX_EXAMPLES = 200
_MAX_MUTATIONS_PER_EXAMPLE = 5

# Tokens drawn from the SQL surface (spec.md dialect + stdlib) plus a few
# generic troublemakers, injected at random positions.
_TOKENS: list[str] = [
    "SELECT",
    "FROM",
    "WHERE",
    "UNION ALL",
    "WITH",
    "AS",
    "BETWEEN",
    "AND",
    "GROUP BY",
    "input(",
    ")",
    "(",
    ",",
    ".",
    "'",
    '"',
    ";",
    "--",
    "/*",
    "*/",
    "\n",
    "\t",
    "scale",
    "crop",
    "overlay",
    "hflip",
    "blur_regions",
    "nope",
    "NULL",
    "TRUE",
    "0",
    "-1",
    "0.5",
    "1e400",
    "🎬",  # movie camera emoji
    "™",  # trademark sign
    " ",  # line separator
    "﻿",  # byte order mark
    "İ",  # dotted capital I (Turkish)
]

_Draw = Callable[[st.SearchStrategy[Any]], Any]


def _delete_slice(text: str, draw: _Draw) -> str:
    n = len(text)
    start = draw(st.integers(min_value=0, max_value=n - 1))
    length = draw(st.integers(min_value=1, max_value=n - start))
    return text[:start] + text[start + length :]


def _duplicate_slice(text: str, draw: _Draw) -> str:
    n = len(text)
    start = draw(st.integers(min_value=0, max_value=n - 1))
    length = draw(st.integers(min_value=1, max_value=n - start))
    end = start + length
    return text[:end] + text[start:end] + text[end:]


def _swap_chars(text: str, draw: _Draw) -> str:
    n = len(text)
    a = draw(st.integers(min_value=0, max_value=n - 1))
    b = draw(st.integers(min_value=0, max_value=n - 1))
    chars = list(text)
    chars[a], chars[b] = chars[b], chars[a]
    return "".join(chars)


def _inject_token(text: str, draw: _Draw) -> str:
    pos = draw(st.integers(min_value=0, max_value=len(text)))
    token = draw(st.sampled_from(_TOKENS))
    return text[:pos] + token + text[pos:]


def _inject_unicode(text: str, draw: _Draw) -> str:
    pos = draw(st.integers(min_value=0, max_value=len(text)))
    ch = draw(st.characters())
    return text[:pos] + ch + text[pos:]


def _mutate_once(text: str, draw: _Draw) -> str:
    ops = ["inject_token", "inject_unicode"]
    if len(text) >= 1:
        ops += ["delete", "duplicate"]
    if len(text) >= 2:
        ops += ["swap"]
    op = draw(st.sampled_from(ops))
    if op == "delete":
        return _delete_slice(text, draw)
    if op == "duplicate":
        return _duplicate_slice(text, draw)
    if op == "swap":
        return _swap_chars(text, draw)
    if op == "inject_unicode":
        return _inject_unicode(text, draw)
    return _inject_token(text, draw)


@given(data=st.data())
@settings(max_examples=_MAX_EXAMPLES, deadline=None)
def test_compile_sql_never_panics_on_mutated_corpus(data: st.DataObject) -> None:
    text = data.draw(st.sampled_from(_CORPUS), label="seed")
    num_mutations = data.draw(
        st.integers(min_value=1, max_value=_MAX_MUTATIONS_PER_EXAMPLE),
        label="num_mutations",
    )
    for _ in range(num_mutations):
        text = _mutate_once(text, data.draw)

    try:
        result = compile_sql(text)
    except SqlmpegError:
        return
    assert isinstance(result, Graph)
