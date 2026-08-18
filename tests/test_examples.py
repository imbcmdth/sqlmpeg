"""Tests for docs/examples.md -- the cookbook (plan 049).

Data-driven, not one test per recipe: every ```sql / ```pgsql fence in the
cookbook is compiled by running the exact command line the fence below it
shows, and the printed ffmpeg command must match that fence byte for byte.
Adding a recipe to the cookbook therefore adds its test here automatically,
and a recipe whose command drifts fails with the diff in front of it.

The two tiers are the ones the cookbook's own "How to read this file" section
documents:

* ```sql -- OFFLINE. Compiled with ffmpeg made unavailable (``shutil.which``
  returns None, so the filter registry loads empty) and probing stubbed out,
  which is exactly what a machine with no ffmpeg and no readable input file
  sees. Byte-reproducible anywhere, so these run in the default suite.
* ```pgsql -- needs this machine: a tier-2 filter, a named option, a
  generated source, or a bare-array broadcast whose length only the file
  knows. Compiled (never executed) against the real installed ffmpeg in an
  ``exec``-marked test.

The command fence is run through :func:`sqlmpeg.cli.main` rather than the
library API, so the ``$ sqlmpeg ...`` line a reader would paste is itself
under test -- flags included. ``-f query.sql`` is rewritten to a temp file
holding that recipe's SQL; the path never reaches the printed command.
"""

from __future__ import annotations

import io
import re
import shlex
import subprocess
import sys
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path

import pytest

from sqlmpeg import cli
from sqlmpeg.registry import Registry

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_PATH = REPO_ROOT / "docs" / "examples.md"

_TIERS = {"sql": "offline", "pgsql": "exec"}

_FENCE_RE = re.compile(r"^```(?P<info>[^\n]*)\n(?P<body>.*?)^```$", re.DOTALL | re.MULTILINE)
_HEADING_RE = re.compile(r"^#{1,6} (?P<text>.+)$", re.MULTILINE)


@dataclass(frozen=True)
class Example:
    """One cookbook recipe: its heading, its query, and the fence below it."""

    heading: str
    tier: str
    sql: str
    command: str | None  # the whole ``` fence body ("$ ...\n<command>\n"), if any


def _heading_before(text: str, position: int) -> str:
    """The nearest markdown heading above `position` (the recipe's title)."""
    found = "(no heading)"
    for match in _HEADING_RE.finditer(text, 0, position):
        found = match.group("text").strip()
    return found


def _parse(text: str) -> list[Example]:
    """Every ```sql / ```pgsql fence, paired with the fence that follows it.

    Pairing is positional and strict: the command fence must be the very next
    fence in the file, with no info string. Anything else (a second query
    fence, or nothing at all) leaves ``command`` None, which the tests below
    turn into a failure that says what to add.
    """
    fences = list(_FENCE_RE.finditer(text))
    examples: list[Example] = []
    for index, fence in enumerate(fences):
        tier = _TIERS.get(fence.group("info").strip())
        if tier is None:
            continue
        following = fences[index + 1] if index + 1 < len(fences) else None
        command = (
            following.group("body")
            if following is not None and following.group("info").strip() == ""
            else None
        )
        examples.append(
            Example(
                heading=_heading_before(text, fence.start()),
                tier=tier,
                sql=fence.group("body"),
                command=command,
            )
        )
    return examples


def _ids(examples: list[Example]) -> list[str]:
    """Stable, readable test ids: the heading, slugified, deduplicated."""
    ids: list[str] = []
    for example in examples:
        slug = re.sub(r"[^a-z0-9]+", "-", example.heading.lower()).strip("-")
        suffix = 2
        candidate = slug
        while candidate in ids:
            candidate = f"{slug}-{suffix}"
            suffix += 1
        ids.append(candidate)
    return ids


_EXAMPLES = _parse(EXAMPLES_PATH.read_text(encoding="utf-8"))
_OFFLINE = [e for e in _EXAMPLES if e.tier == "offline"]
_EXEC = [e for e in _EXAMPLES if e.tier == "exec"]

_MISSING_FENCE_HELP = (
    "every query in docs/examples.md is followed by a fence holding the real "
    "compiled command: a ``` fence whose first line is the `$ sqlmpeg compile "
    "...` invocation and whose remaining lines are exactly what it prints"
)


def _split_command(example: Example) -> tuple[list[str], str]:
    """``(argv, expected stdout)`` from the recipe's command fence."""
    assert example.command is not None, f"{example.heading}: {_MISSING_FENCE_HELP}"
    shown, _, expected = example.command.partition("\n")
    assert shown.startswith("$ sqlmpeg "), (
        f"{example.heading}: the command fence must start with a `$ sqlmpeg ...` "
        f"line, got {shown!r}"
    )
    assert expected, f"{example.heading}: {_MISSING_FENCE_HELP}"
    argv = shlex.split(shown[2:])[1:]
    assert "-f" in argv or "--file" in argv, (
        f"{example.heading}: the shown command must read the query with -f, so that the "
        f"```sql fence above it is the text being compiled; inline SQL could drift from it"
    )
    return argv, expected


def _run(example: Example, tmp_path: Path) -> str:
    """Run the recipe's own command line; return what it printed."""
    argv, _ = _split_command(example)
    for index, token in enumerate(argv):
        if token in ("-f", "--file"):
            query = tmp_path / argv[index + 1]
            query.write_text(example.sql, encoding="utf-8")
            argv[index + 1] = str(query)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = cli.main(argv)
    assert code == 0, f"{example.heading}: `{shlex.join(argv)}` exited {code}"
    return buffer.getvalue()


def _go_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the compile see a machine with no ffmpeg and no readable inputs.

    A fresh, unloadable ``Registry`` rather than the process-wide singleton
    (another test may already have loaded the real one), and ``probe_path``
    stubbed rather than relying on an unreadable path, because the command
    fence shows a plain ``sqlmpeg compile`` -- the flags under test are the
    ones a reader would type, so the isolation has to come from underneath
    them. ``binaries.ffmpeg_path`` is stubbed (not just ``shutil.which``) so
    this stays offline even when the ``static-ffmpeg`` provisioner (RFC-010)
    is actually installed and already has a cached binary. Patched by dotted
    path rather than by attribute, since the two modules re-export their
    imports and ``--strict`` will not read through a re-export.
    """
    monkeypatch.setattr("sqlmpeg.registry.binaries.ffmpeg_path", lambda: None)
    monkeypatch.setattr("sqlmpeg.compiler.registry_module.load", Registry)
    monkeypatch.setattr("sqlmpeg.compiler.probe_path", lambda path: None)


@pytest.fixture(scope="module")
def _fixtures() -> None:
    """The generated media the exec-tier recipes name verbatim."""
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "gen_fixtures.py")],
        check=True,
    )


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------


def test_the_cookbook_has_examples() -> None:
    assert len(_OFFLINE) > 1 and len(_EXEC) > 1


@pytest.mark.parametrize("example", _EXAMPLES, ids=_ids(_EXAMPLES))
def test_every_example_shows_its_compiled_command(example: Example) -> None:
    """Checked for BOTH tiers in the default suite: an exec recipe missing its
    fence would otherwise go unnoticed until someone ran `-m exec`."""
    _split_command(example)


def test_examples_md_uses_lf_newlines_only() -> None:
    assert b"\r" not in EXAMPLES_PATH.read_bytes()


# ---------------------------------------------------------------------------
# the recipes themselves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("example", _OFFLINE, ids=_ids(_OFFLINE))
def test_offline_example_compiles_to_the_shown_command(
    example: Example, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _go_offline(monkeypatch)
    assert _run(example, tmp_path) == _split_command(example)[1]


@pytest.mark.exec
@pytest.mark.parametrize("example", _EXEC, ids=_ids(_EXEC))
def test_exec_example_compiles_to_the_shown_command(
    example: Example, tmp_path: Path, _fixtures: None
) -> None:
    assert _run(example, tmp_path) == _split_command(example)[1]
