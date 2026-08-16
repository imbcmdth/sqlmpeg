"""Generate ``docs/stdlib.md`` from ``sqlmpeg.stdlib.FUNCTIONS``.

Guardrail #4: the function table is DATA, not code, and is the single
source of truth for names, arities, arg mapping, and docs. This script
renders ``docs/stdlib.md`` from that table rather than the file being
hand-written; the generated file is committed, and
``tests/test_docs.py`` asserts that regenerating produces byte-identical
output.

Regenerate after any change to ``sqlmpeg/stdlib.py``::

    python scripts/gen_docs.py

For each function, the ffmpeg filter(s) it lowers to are discovered by
actually calling ``spec.expand`` against a throwaway :class:`ExpandCtx`
that just records the filter name of every node it creates (mirroring the
``FakeCtx`` pattern in ``tests/test_stdlib.py``), passing dummy arguments
per parameter kind: ``"f"`` for ``frame``, ``1`` for ``num``, ``"x"`` for
``str``. This keeps the doc's filter list honest against the real
expansion code instead of being transcribed by hand.
"""

from __future__ import annotations

from pathlib import Path

from sqlmpeg.ir import FrameRef, StreamType
from sqlmpeg.stdlib import FUNCTIONS, FuncSpec, Param

_REPO_ROOT = Path(__file__).resolve().parent.parent
_OUTPUT_PATH = _REPO_ROOT / "docs" / "stdlib.md"

_DUMMY_ARGS: dict[str, object] = {"video": "f", "audio": "f", "num": 1, "str": "x"}

_HEADER = """\
# sqlmpeg stdlib

GENERATED FILE. Do not edit by hand -- this is rendered from the
`FUNCTIONS` table in `sqlmpeg/stdlib.py` (guardrail #4: the function table
is the single source of truth for names, arities, arg mapping, and docs).
Regenerate with:

    python scripts/gen_docs.py
"""


class _FilterCollector:
    """A throwaway ExpandCtx that records filter names instead of building a graph."""

    def __init__(self) -> None:
        self.filters: list[str] = []
        self._counter = 0

    def node(
        self,
        filter: str,
        args: dict[str, object],
        inputs: list[FrameRef],
        outputs: list[StreamType],
    ) -> FrameRef:
        self.filters.append(filter)
        node_id = f"n{self._counter}"
        self._counter += 1
        return node_id


def _signature(name: str, variant: tuple[Param, ...]) -> str:
    params = ", ".join(f"{p.name}: {p.kind}" for p in variant)
    return f"{name}({params})"


def _filters_for(spec: FuncSpec) -> list[str]:
    """Run EVERY overload's expansion with dummy args; the distinct filters used.

    Per overload rather than just the first, because an overload can lower to
    something else entirely (``delay``'s audio form is ``adelay``, its video
    form a ``format``+``tpad`` macro). For every spec whose overloads differ
    only in arity this is the same list the first one alone produced.
    """
    ctx = _FilterCollector()
    for index, variant in enumerate(spec.variants):
        args: list[object] = [_DUMMY_ARGS[p.kind] for p in variant]
        spec.impl(index).expand(ctx, args)

    seen: list[str] = []
    for name in ctx.filters:
        if name not in seen:
            seen.append(name)
    return seen


def _render_function(name: str, spec: FuncSpec) -> list[str]:
    lines: list[str] = [f"## {name}", ""]
    for variant in spec.variants:
        lines.append(f"`{_signature(name, variant)}`")
        lines.append("")
    lines.append(spec.doc)
    lines.append("")
    filters = ", ".join(f"`{f}`" for f in _filters_for(spec))
    lines.append(f"FFmpeg filter(s): {filters}")
    lines.append("")
    return lines


def render() -> str:
    lines: list[str] = _HEADER.splitlines()
    lines.append("")
    for name, spec in FUNCTIONS.items():
        lines.extend(_render_function(name, spec))
    return "\n".join(lines).rstrip("\n") + "\n"


def main() -> None:
    text = render()
    with open(_OUTPUT_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


if __name__ == "__main__":
    main()
