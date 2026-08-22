"""Grammar-driven bug hunt: generate MOSTLY-VALID dialect queries and demand
that every one compiles or is rejected with a typed, non-internal error.

The committed fuzz test (tests/test_fuzz.py) mutates text, and mutated SQL
almost always dies at parse - it cannot reach the row model, the sinks, or
the split pass. This generator builds queries from the registry (correct
filter arity and stream types), real row columns per FROM-item kind, and
randomized probe results (0-3 tracks per type, missing codecs, empty
chapters), with a tunable deviation rate, so the deep passes are reached.
Both bugs fixed in the 0.25.0 cycle came from here; a million mutation
examples found neither.

Local tool, not a test: it runs millions of cases. Usage:

    python scripts/hunt.py 200000 --seed 3
    python scripts/hunt.py 50000 --seed 7 --p-bad 0.2 --mutate

Findings (distinct by failing frame) go to --out as JSON, smallest
reproducing input per finding, and print to stdout. An empty findings list
is the expected result.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.disable(logging.CRITICAL)

from sqlmpeg import compiler  # noqa: E402
from sqlmpeg import vars as vars_module  # noqa: E402
from sqlmpeg.errors import ErrorCode, SqlmpegError  # noqa: E402
from sqlmpeg.inputs import INPUT_OPTIONS  # noqa: E402
from sqlmpeg.ir import Graph  # noqa: E402
from sqlmpeg.probe import (  # noqa: E402
    AttachmentMeta,
    ChapterMeta,
    CueMeta,
    ProbeResult,
    StreamMeta,
)
from sqlmpeg.registry import load_reference  # noqa: E402
from sqlmpeg.sink import SINK_OPTIONS  # noqa: E402
from sqlmpeg.types import DISPOSITION_KEYS  # noqa: E402

_VIDEO = StreamMeta(
    type="video",
    index=0,
    metadata={},
    width=1920,
    height=1080,
    fps="30/1",
    sample_rate=None,
    codec="h264",
)
_AUDIO = StreamMeta(
    type="audio",
    index=0,
    metadata={"language": "eng"},
    width=None,
    height=None,
    fps=None,
    sample_rate=48000,
    codec="aac",
    channels=2,
    channel_layout="stereo",
)
_SUB = StreamMeta(
    type="subtitle",
    index=0,
    metadata={"language": "eng"},
    width=None,
    height=None,
    fps=None,
    sample_rate=None,
    codec="srt",
)
_PROBE = ProbeResult(
    streams=[_VIDEO, _AUDIO, _SUB],
    chapters=[
        ChapterMeta(index=1, start_t=0.0, end_t=30.0, title="Intro"),
        ChapterMeta(index=2, start_t=30.0, end_t=90.0, title="Credits"),
    ],
    attachments=[
        AttachmentMeta(
            index=1, filename="font.ttf", mimetype="application/x-truetype-font"
        )
    ],
)

_DUMMY = {
    "source": "in.mp4",
    "dest": "out.mp4",
    "main": "in.mp4",
    "second": "in2.mp4",
    "overlay": "logo.mp4",
    "language": "eng",
    "codec": "aac",
    "width": "1920",
    "height": "1080",
    "start": "5",
    "end": "60",
    "factor": "2",
    "first": "one.mp4",
    "music": "music.m4a",
    "voice": "voiceover.wav",
    "subs": "subs.en.vtt",
    "cut": "120",
    "high": "1080p.mp4",
    "mid": "720p.mp4",
    "low": "480p.mp4",
    "insert": "promo.mp4",
    "crf": "23",
    "gain": "0.5",
    "w": "640",
    "h": "480",
    "x": "100",
    "y": "50",
    "at": "10",
    "duration": "1",
    "dir": "clock",
    "rate": "1",
    "prefix": "ch",
    "ext": "m4a",
    "title": "My Film",
    "artist": "Me",
}

TOKENS = [
    "SELECT",
    "FROM",
    "WHERE",
    "UNION ALL",
    "WITH",
    "AS",
    "BETWEEN",
    "AND",
    "GROUP BY",
    "ORDER BY",
    "COPY",
    "TO",
    "STDOUT",
    "CREATE VIEW",
    "unnest(",
    "array_agg(",
    "input(",
    "::text",
    "||",
    ":'source'",
    ':"x"',
    ":rate",
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
    "\U0001f3ac",
    "\u2122",
    "\u2028",
    "\ufeff",
    "\u0130",
]


def mutate(text: str, rng: random.Random) -> str:
    ops = ["inject_token", "inject_unicode"]
    if text:
        ops += ["delete", "duplicate"]
    if len(text) >= 2:
        ops += ["swap"]
    op = rng.choice(ops)
    n = len(text)
    if op == "delete":
        start = rng.randrange(n)
        length = rng.randint(1, n - start)
        return text[:start] + text[start + length :]
    if op == "duplicate":
        start = rng.randrange(n)
        length = rng.randint(1, n - start)
        end = start + length
        return text[:end] + text[start:end] + text[end:]
    if op == "swap":
        a = rng.randrange(n)
        b = rng.randrange(n)
        chars = list(text)
        chars[a], chars[b] = chars[b], chars[a]
        return "".join(chars)
    if op == "inject_unicode":
        pos = rng.randint(0, n)
        ch = chr(rng.randrange(0x110000))
        while 0xD800 <= ord(ch) <= 0xDFFF:
            ch = chr(rng.randrange(0x110000))
        return text[:pos] + ch + text[pos:]
    pos = rng.randint(0, n)
    return text[:pos] + rng.choice(TOKENS) + text[pos:]


def key_of(err: BaseException) -> str:
    cause = err.__cause__ or err
    tb = traceback.extract_tb(cause.__traceback__)
    tail = tb[-3:]
    return f"{cause.__class__.__name__}|" + "|".join(
        f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in tail
    )


def record(findings: dict, err: BaseException, text: str, use_probe: bool, phase: str) -> None:
    k = phase + "|" + key_of(err)
    entry = findings.get(k)
    if entry is None:
        cause = err.__cause__ or err
        findings[k] = {
            "key": k,
            "hits": 1,
            "message": str(err)[:400],
            "cause": f"{cause.__class__.__name__}: {cause}"[:300],
            "frames": [
                f"{Path(f.filename).name}:{f.lineno}:{f.name}"
                for f in traceback.extract_tb(cause.__traceback__)[-6:]
            ],
            "input": text,
            "probe": use_probe,
        }
        return
    entry["hits"] += 1
    if len(text) < len(entry["input"]):
        entry["input"] = text
        entry["probe"] = use_probe


REG = load_reference(ROOT / "tests" / "data" / "reference_registry.json")
compiler.registry_module = SimpleNamespace(load=lambda: REG)

VIDEO_FILTERS = [
    n for n in REG.names() if set(REG.get(n).inputs) == {"video"} and REG.get(n).output == "video"
]
AUDIO_FILTERS = [
    n for n in REG.names() if set(REG.get(n).inputs) == {"audio"} and REG.get(n).output == "audio"
]
SOURCES = REG.source_names()
SINK_NAMES = sorted(SINK_OPTIONS)
INPUT_NAMES = sorted(INPUT_OPTIONS)

# Tags and disposition flags are read by path, so those row columns are two
# written parts.
TRACK_COLS = [
    "tags.language",
    "tags.title",
    "disposition.default",
    "disposition.forced",
    "codec",
    "index",
]
VIDEO_ROW_COLS = TRACK_COLS + ["width", "height", "fps"]
AUDIO_ROW_COLS = TRACK_COLS + ["channels", "channel_layout", "sample_rate"]
CHAPTER_COLS = ["index", "title", "start_t", "end_t"]
CUE_COLS = ["index", "text", "start_t", "end_t"]
ATTACHMENT_COLS = ["index", "filename", "mimetype"]

# Aliases that name a probed field rather than a tag key.
TAG_ALIASES = ["codec", "index", "width", "channels", "duration", "sample_rate"]

RESERVED = {
    "limit",
    "order",
    "in",
    "all",
    "and",
    "or",
    "not",
    "select",
    "from",
    "where",
    "to",
    "as",
    "with",
    "end",
    "case",
    "when",
    "then",
    "else",
    "is",
    "null",
    "true",
    "false",
    "default",
    "table",
    "group",
    "by",
    "on",
    "using",
    "join",
    "left",
    "right",
    "full",
    "inner",
    "cross",
    "natural",
    "union",
    "offset",
    "having",
    "distinct",
    "copy",
    "create",
    "view",
    "primary",
    "references",
    "any",
    "some",
    "asc",
    "desc",
    "between",
    "like",
    "ilike",
    "similar",
}

GOOD_LITERALS = ["1", "2", "0.5", "'x'", "'iw/2'", "10", "'eng'"]
BAD_LITERALS = [
    "0",
    "-1",
    "1e400",
    "-1e400",
    "NULL",
    "''",
    "1000000000000000000000",
    "'a''b'",
    "1e-400",
]


class Item:
    def __init__(self, alias: str, kind: str, base: str | None = None) -> None:
        self.alias = alias
        # input | vrow | arow | srow | chapter | cue | attachment | source-* | rel
        self.kind = kind
        self.base = base


class Ctx:
    def __init__(self, rng: random.Random, p_bad: float) -> None:
        self.rng = rng
        self.p_bad = p_bad
        self.items: list[Item] = []
        self.names: list[str] = []

    def bad(self) -> bool:
        return self.rng.random() < self.p_bad

    def pick(self, seq):
        return seq[self.rng.randrange(len(seq))]


def video_streams(ctx: Ctx) -> list[str]:
    out = []
    for it in ctx.items:
        if it.kind == "input":
            out.append(f"{it.alias}.video[1]")
        elif it.kind == "vrow":
            out.append(it.alias)
        elif it.kind == "source-v":
            out.append(f"{it.alias}.video[1]")
        elif it.kind == "rel":
            out.append(f"{it.alias}.v")
    return out


def audio_streams(ctx: Ctx) -> list[str]:
    out = []
    for it in ctx.items:
        if it.kind == "input":
            out.append(f"{it.alias}.audio[1]")
        elif it.kind == "arow":
            out.append(it.alias)
        elif it.kind == "source-a":
            out.append(f"{it.alias}.audio[1]")
        elif it.kind == "rel":
            out.append(f"{it.alias}.a")
    return out


def gen_video(ctx: Ctx, depth: int = 0) -> str:
    rng = ctx.rng
    base = video_streams(ctx)
    if ctx.bad():
        alias = ctx.pick(ctx.items).alias if ctx.items else "a0"
        return ctx.pick(
            [
                f"{alias}.video[{ctx.pick(['0', '-1', '9', '1+1', chr(39) + '1' + chr(39)])}]",
                f"{alias}.chapters",
                f"{alias}.data[1]",
                f"{alias}.subtitle[1]",
                f"{alias}.video",
                "*",
                f"scale({alias}.video[1], 640, -2).width",
                f"hflip({alias}.video[1]).tags.language",
                f"{alias}.nosuch",
            ]
        )
    if not base or depth > 2 or rng.random() < 0.45:
        return ctx.pick(base) if base else "a0.video[1]"
    return gen_call(ctx, "video", depth)


def gen_audio(ctx: Ctx, depth: int = 0) -> str:
    rng = ctx.rng
    base = audio_streams(ctx)
    if not base or depth > 2 or rng.random() < 0.5:
        return ctx.pick(base) if base else "a0.audio[1]"
    return gen_call(ctx, "audio", depth)


N_VIDEO = ["hstack", "vstack"]
N_AUDIO = ["amix", "amerge", "interleave", "ainterleave"]


def gen_call(ctx: Ctx, kind: str, depth: int) -> str:
    rng = ctx.rng
    if rng.random() < 0.07:
        name = ctx.pick(N_VIDEO if kind == "video" else N_AUDIO)
        gen = gen_video if kind == "video" else gen_audio
        n = rng.randint(0, 4) if ctx.bad() else rng.randint(2, 3)
        return f"{name}({', '.join(gen(ctx, depth + 1) for _ in range(n))})"
    if rng.random() < 0.05:
        gen = gen_video if kind == "video" else gen_audio
        fill = (
            "ffmpeg.color(duration => 1)" if kind == "video" else "ffmpeg.anullsrc(duration => 1)"
        )
        return f"COALESCE({gen(ctx, depth + 1)}, {fill})"
    if kind == "video" and rng.random() < 0.08:
        macro = ctx.pick(["blur_regions", "speed", "delay"])
        if macro == "blur_regions":
            return f"sqlmpeg.blur_regions({gen_video(ctx, depth + 1)}, 10, 10, 50, 50, 3)"
        if macro == "speed":
            return f"sqlmpeg.speed({gen_video(ctx, depth + 1)}, 2)"
        return f"sqlmpeg.delay({gen_video(ctx, depth + 1)}, 1)"
    pool = VIDEO_FILTERS if kind == "video" else AUDIO_FILTERS
    name = ctx.pick(pool)
    filt = REG.get(name)
    gen = gen_video if kind == "video" else gen_audio
    parts = [gen(ctx, depth + 1) for _ in filt.inputs]
    if ctx.bad():
        parts.append(gen(ctx, depth + 1))
    opts = REG.options(name) or {}
    names = sorted(opts)
    used: set[str] = set()
    for _ in range(rng.randint(0, 2)):
        if not names:
            break
        key = ctx.pick(names) if not ctx.bad() else "nosuchopt"
        if key in used or not key.isidentifier() or key in RESERVED:
            continue
        used.add(key)
        parts.append(f"{key} => {gen_opt_value(ctx, opts.get(key))}")
    prefix = "ffmpeg." if rng.random() < 0.15 else ""
    return f"{prefix}{name}({', '.join(parts)})"


def gen_opt_value(ctx: Ctx, spec) -> str:
    rng = ctx.rng
    if spec is None or ctx.bad():
        return ctx.pick(BAD_LITERALS)
    t = spec.type
    if spec.constants and rng.random() < 0.6:
        return "'" + ctx.pick(list(spec.constants)) + "'"
    if t in ("int", "num"):
        lo = spec.minimum if spec.minimum is not None else 0
        hi = spec.maximum if spec.maximum is not None else 10
        if lo != lo or hi != hi or abs(lo) > 1e12 or abs(hi) > 1e12:
            lo, hi = 0, 10
        v = rng.uniform(lo, min(hi, lo + 100))
        return str(int(v)) if t == "int" else f"{v:.3f}"
    if t == "bool":
        return ctx.pick(["true", "false"])
    return "'" + ctx.pick(["x", "1", "0.5", "iw/2"]) + "'"


def gen_value(ctx: Ctx, depth: int = 0) -> str:
    rng = ctx.rng
    r = rng.random()
    if depth > 2 or r < 0.45:
        if ctx.items and rng.random() < 0.45:
            return gen_row_col(ctx)
        return ctx.pick(BAD_LITERALS if ctx.bad() else GOOD_LITERALS)
    if r < 0.55:
        return f"({gen_value(ctx, depth + 1)} || {gen_value(ctx, depth + 1)})"
    if r < 0.65:
        op = ctx.pick(["+", "-", "*", "/"])
        return f"({gen_value(ctx, depth + 1)} {op} {gen_value(ctx, depth + 1)})"
    if r < 0.75:
        return f"({gen_value(ctx, depth + 1)})::text"
    if r < 0.8:
        return f"CAST({gen_value(ctx, depth + 1)} AS text)"
    if r < 0.9:
        return (
            f"CASE WHEN {gen_pred(ctx, depth + 1)} THEN {gen_value(ctx, depth + 1)}"
            + (f" ELSE {gen_value(ctx, depth + 1)}" if rng.random() < 0.7 else "")
            + " END"
        )
    return ":'source'"


def gen_row_col(ctx: Ctx) -> str:
    it = ctx.pick(ctx.items)
    if it.kind == "input":
        col = ctx.pick(["duration", "t", "tags.title", "tags.artist"])
    elif it.kind == "vrow":
        col = ctx.pick(VIDEO_ROW_COLS)
    elif it.kind == "arow":
        col = ctx.pick(AUDIO_ROW_COLS)
    elif it.kind == "chapter":
        col = ctx.pick(CHAPTER_COLS)
    elif it.kind == "cue":
        col = ctx.pick(CUE_COLS)
    elif it.kind == "attachment":
        col = ctx.pick(ATTACHMENT_COLS)
    else:
        col = ctx.pick(["index", "t", "duration"])
    if ctx.bad():
        col = "nosuchcol"
    return f"{it.alias}.{col}"


def gen_pred(ctx: Ctx, depth: int = 0) -> str:
    rng = ctx.rng
    r = rng.random()
    if depth > 2 or r < 0.5:
        op = ctx.pick(["=", "!=", "<", "<=", ">", ">="])
        return f"{gen_value(ctx, depth + 1)} {op} {gen_value(ctx, depth + 1)}"
    if r < 0.65:
        it = ctx.pick(ctx.items) if ctx.items else None
        alias = it.alias if it else "a0"
        col = "t" if (it is None or it.kind == "input") else ctx.pick(["t", "start_t"])
        return f"{alias}.{col} BETWEEN {gen_value(ctx, depth + 1)} AND {gen_value(ctx, depth + 1)}"
    if r < 0.75:
        return f"{gen_value(ctx, depth + 1)} IS {'NOT ' if rng.random() < 0.5 else ''}NULL"
    if r < 0.8:
        vals = ", ".join(gen_value(ctx, depth + 1) for _ in range(rng.randint(1, 3)))
        kw = "NOT IN" if rng.random() < 0.5 else "IN"
        return f"{gen_value(ctx, depth + 1)} {kw} ({vals})"
    if r < 0.85 and ctx.items:
        # A boolean stands alone as a condition.
        return gen_row_col(ctx)
    if r < 0.95:
        return f"({gen_pred(ctx, depth + 1)} {ctx.pick(['AND', 'OR'])} {gen_pred(ctx, depth + 1)})"
    return f"NOT ({gen_pred(ctx, depth + 1)})"


_ARRAY_KIND = {
    "video": "vrow",
    "audio": "arow",
    "subtitle": "srow",
    "chapters": "chapter",
    "cues": "cue",
    "attachments": "attachment",
    "data": "srow",
}


def gen_from(ctx: Ctx) -> str:
    rng = ctx.rng
    pieces: list[str] = []
    for i in range(rng.randint(1, 3)):
        alias = f"a{i}"
        r = rng.random()
        inputs = [it for it in ctx.items if it.kind == "input"]
        if r < 0.5 or not ctx.items:
            path = (
                ctx.pick(["'in.mp4'", "'logo.png'", ":'source'"])
                if not ctx.bad()
                else ctx.pick(["''", "'http://e.example/a'", "(:'source')"])
            )
            opts = []
            for _ in range(rng.randint(0, 2)):
                key = ctx.pick(INPUT_NAMES) if not ctx.bad() else "nosuch"
                spec = INPUT_OPTIONS.get(key)
                if spec is None:
                    val = ctx.pick(BAD_LITERALS)
                elif spec.type == "bool":
                    val = ctx.pick(["true", "false"])
                elif spec.type in ("int", "num"):
                    val = ctx.pick(["1", "15", "0.5", "-1"])
                else:
                    val = "'x'"
                opts.append(f"{key} => {val}")
            tail = (", " + ", ".join(opts)) if opts else ""
            pieces.append(f"input({path}{tail}) {alias}")
            ctx.items.append(Item(alias, "input"))
        elif r < 0.62:
            src = ctx.pick(SOURCES)
            kind = "source-v" if REG.get_source(src).output == "video" else "source-a"
            opts = REG.options(src) or {}
            named = []
            seen: set[str] = set()
            for _ in range(rng.randint(0, 2)):
                if not opts:
                    break
                key = ctx.pick(sorted(opts))
                if key in seen or not key.isidentifier() or key in RESERVED:
                    continue
                seen.add(key)
                named.append(f"{key} => {gen_opt_value(ctx, opts[key])}")
            pieces.append(f"ffmpeg.{src}({', '.join(named)}) {alias}")
            ctx.items.append(Item(alias, kind))
        elif r < 0.9 and inputs:
            base = ctx.pick(inputs).alias
            arr = ctx.pick(
                ["video", "audio", "subtitle", "chapters", "cues", "attachments", "data"]
            )
            piece = f"unnest({base}.{arr}) {alias}"
            rows = [
                it
                for it in ctx.items
                if it.kind in ("vrow", "arow", "srow", "chapter", "cue", "attachment")
            ]
            if rows and rng.random() < 0.3:
                kind = ctx.pick(
                    ["JOIN", "INNER JOIN", "LEFT JOIN", "LEFT OUTER JOIN", "FULL OUTER JOIN"]
                )
                if ctx.bad():
                    kind = ctx.pick(["RIGHT JOIN", "CROSS JOIN", "NATURAL JOIN"])
                ctx.items.append(Item(alias, _ARRAY_KIND[arr], base))
                pieces.append(f"{kind} {piece} ON {gen_pred(ctx)}")
                continue
            pieces.append(piece)
            ctx.items.append(Item(alias, _ARRAY_KIND[arr], base))
        elif ctx.names:
            name = ctx.pick(ctx.names)
            pieces.append(f"{name} {alias}")
            ctx.items.append(Item(alias, "rel"))
        else:
            pieces.append(f"input('in.mp4') {alias}")
            ctx.items.append(Item(alias, "input"))
    out = pieces[0]
    for piece in pieces[1:]:
        out += (
            " "
            if "JOIN" in piece.split()[0]
            or piece.split()[0] in ("INNER", "LEFT", "FULL", "RIGHT", "CROSS", "NATURAL")
            else ", "
        ) + piece
    return out


def gen_select(ctx: Ctx, depth: int = 0, view: bool = False) -> str:
    rng = ctx.rng
    saved = list(ctx.items)
    ctx.items = []
    parts: list[str] = []
    if depth == 0 and rng.random() < 0.2:
        ctes = []
        for i in range(rng.randint(1, 2)):
            name = f"q{i}{rng.randrange(100)}"
            ctes.append(f"{name} AS ({gen_select(ctx, depth + 1)})")
            ctx.names.append(name)
        parts.append("WITH " + ", ".join(ctes))
    from_clause = gen_from(ctx)
    cols = []
    grouped = rng.random() < 0.2
    for _ in range(rng.randint(1, 3)):
        r = rng.random()
        if not view and ctx.items and rng.random() < 0.05:
            # A star as a WHOLE projection: legal over a container, a rejection
            # over rows in a media query, every column of both in a table one.
            cols.append(ctx.pick(["*", f"{ctx.pick(ctx.items).alias}.*"]))
            continue
        if r < 0.45:
            col = gen_video(ctx)
        elif r < 0.65:
            col = gen_audio(ctx)
        elif r < 0.75 or grouped:
            col = f"array_agg({gen_video(ctx) if rng.random() < 0.5 else gen_audio(ctx)})"
        else:
            # A read-only field name as the alias is a rejection, a free-form
            # key is a tag: both shapes go through.
            key = ctx.pick(TAG_ALIASES) if ctx.bad() else f"tag{rng.randrange(3)}"
            col = f"{gen_value(ctx)} AS {key}"
        if view:
            col += f" AS {'v' if 'audio' not in col else 'a'}"
        elif not col.endswith(")") and " AS " not in col:
            col += f" AS c{rng.randrange(5)}"
        cols.append(col)
    parts.append("SELECT " + ", ".join(cols))
    parts.append("FROM " + from_clause)
    if rng.random() < 0.45:
        parts.append("WHERE " + gen_pred(ctx))
    if grouped:
        parts.append("GROUP BY " + gen_row_col(ctx))
    if ctx.bad():
        parts.append(ctx.pick(["HAVING 1 = 1", "LIMIT 2", "ORDER BY 1", "OFFSET 1"]))
    text = "\n".join(parts)
    if depth == 0 and rng.random() < 0.15:
        text += "\nUNION ALL\n" + gen_select(ctx, depth + 1)
    ctx.items = saved
    return text


def gen_dest(ctx: Ctx) -> str:
    rng = ctx.rng
    r = rng.random()
    if r < 0.4:
        return ctx.pick(["'out.mp4'", "'out.mkv'", "'out.%04d.png'", "'out.csv'"])
    if r < 0.5:
        return "STDOUT"
    if r < 0.6 and ctx.bad():
        return ctx.pick(["''", "(1 / 0)", "(NULL)"])
    return f"({gen_value(ctx)})"


def gen_sink_opts(ctx: Ctx) -> str:
    rng = ctx.rng
    parts = []
    for _ in range(rng.randint(1, 3)):
        key = ctx.pick(SINK_NAMES) if not ctx.bad() else "nosuch"
        spec = SINK_OPTIONS.get(key)
        if spec is None or ctx.bad():
            val = ctx.pick(BAD_LITERALS)
        elif spec.type == "bool":
            val = ctx.pick(["true", "false"])
        elif spec.type == "int":
            val = ctx.pick(["1", "20", "0"])
        elif spec.type == "num":
            val = ctx.pick(["1", "2.5"])
        else:
            val = ctx.pick(["'libx264'", "'aac'", "'csv'", "'slow'", "'yuv420p'", "'192k'"])
        parts.append(f"{key} {val}")
    return ", ".join(parts)


def gen_copy(ctx: Ctx) -> str:
    rng = ctx.rng
    text = f"COPY (\n{gen_select(ctx)}\n) TO {gen_dest(ctx)}"
    if rng.random() < 0.6:
        text += f" WITH ({gen_sink_opts(ctx)})"
    return text


_CHAPTER_BOUNDS = ["0", "30", "60", "120", "-5", "0.5", "NULL", "'x'", "true"]
_CHAPTER_TITLES = ["'Intro'", "NULL", "'a=b'", "1", "'x' || 'y'"]
_CUE_TEXTS = ["'Hello'", "NULL", "''", "'a --> b'", "'Tom & <b>'", "1", "'x' || 'y'"]


def gen_chapters_copy(ctx: Ctx) -> str:
    """A `chapters` output column, written as a literal array or gathered.

    Bounds come out of order, overlapping and mistyped as often as not, so the
    span checks are exercised alongside the ones that compile.
    """
    rng = ctx.rng
    records = []
    for _ in range(rng.randint(1, 3)):
        cells = [
            ctx.pick(_CHAPTER_TITLES),
            ctx.pick(_CHAPTER_BOUNDS),
            ctx.pick(_CHAPTER_BOUNDS),
        ]
        if ctx.bad():
            cells = cells[: rng.randint(0, 4)]
        records.append(f"ROW({', '.join(cells)})::chapter")
    if rng.random() < 0.5:
        column = f"ARRAY[{', '.join(records)}] AS chapters"
        if ctx.bad():
            column = ctx.pick(["NULL AS chapters", "'x' AS chapters", "ARRAY[] AS chapters"])
        return (
            f"COPY (\n  SELECT a0.video[1], {column} FROM input('in.mp4') a0\n"
            ") TO 'out.mkv'"
        )
    columns = ["start_t", "end_t", "title"]
    if ctx.bad():
        columns = ctx.pick([["start_t", "end_t"], ["a", "b", "c"]])
    rows = []
    for _ in range(rng.randint(1, 3)):
        cells = [
            ctx.pick(_CHAPTER_TITLES) if name == "title" else ctx.pick(_CHAPTER_BOUNDS)
            for name in columns
        ]
        rows.append("(" + ", ".join(cells) + ")")
    return (
        f"COPY (\n  WITH marks({', '.join(columns)}) AS (VALUES {', '.join(rows)})\n"
        "  SELECT a0.video[1], "
        "array_agg(ROW(m.title, m.start_t, m.end_t)::chapter) AS chapters\n"
        "  FROM input('in.mp4') a0, marks m GROUP BY a0.video[1]\n"
        ") TO 'out.mkv'"
    )


def gen_cues_copy(ctx: Ctx) -> str:
    """A cue array in a STREAM position, written as a literal or gathered.

    Same deviation rate as the chapter list: bounds out of order, mistyped,
    and payloads WebVTT cannot carry, so the span and text checks are
    exercised alongside the ones that compile.
    """
    rng = ctx.rng
    records = []
    for _ in range(rng.randint(1, 3)):
        cells = [
            ctx.pick(_CUE_TEXTS),
            ctx.pick(_CHAPTER_BOUNDS),
            ctx.pick(_CHAPTER_BOUNDS),
        ]
        if ctx.bad():
            cells = cells[: rng.randint(0, 4)]
        records.append(f"ROW({', '.join(cells)})::cue")
    if rng.random() < 0.5:
        column = f"ARRAY[{', '.join(records)}]"
        if ctx.bad():
            column = ctx.pick(["ARRAY[]", "ARRAY[] AS cues", f"{records[0]}"])
        return (
            f"COPY (\n  SELECT a0.video[1], {column} FROM input('in.mp4') a0\n"
            ") TO 'out.mkv'"
        )
    source = ctx.pick(["a0.chapters", "a1.cues"])
    if ctx.bad():
        source = ctx.pick(["a0.audio", "a0.cues", "a0.nosuch"])
    return (
        "COPY (\n  SELECT a0.video[1], "
        "array_agg(ROW(c.title, c.start_t, c.end_t)::cue)\n"
        f"  FROM input('in.mp4') a0, input('subs.vtt') a1, unnest({source}) c\n"
        "  GROUP BY a0.video[1]\n"
        ") TO 'out.mkv'"
    )


_ATTACHMENT_NAMES = ["'font.ttf'", "NULL", "''", "1", "'a' || 'b'"]
_ATTACHMENT_TYPES = ["'application/x-truetype-font'", "'text/plain'", "NULL", "''", "2"]
_ATTACHMENT_PATHS = ["'font.ttf'", "'fonts/x.otf'", "NULL", "''", ":'source'", "3"]


def gen_attachments_copy(ctx: Ctx) -> str:
    """An `attachments` output column, written as a literal array or gathered.

    Names, types and paths are mistyped and NULL as often as not, so the
    field checks are exercised alongside the lists that compile.
    """
    rng = ctx.rng
    records = []
    for _ in range(rng.randint(1, 3)):
        cells = [
            ctx.pick(_ATTACHMENT_NAMES),
            ctx.pick(_ATTACHMENT_TYPES),
            ctx.pick(_ATTACHMENT_PATHS),
        ]
        if ctx.bad():
            cells = cells[: rng.randint(0, 4)]
        records.append(f"ROW({', '.join(cells)})::attachment")
    if rng.random() < 0.5:
        column = f"ARRAY[{', '.join(records)}] AS attachments"
        if ctx.bad():
            column = ctx.pick(
                [
                    "NULL AS attachments",
                    "'x' AS attachments",
                    "ARRAY[] AS attachments",
                    "a0.attachments AS attachments",
                ]
            )
        return (
            f"COPY (\n  SELECT a0.video[1], {column} FROM input('in.mp4') a0\n"
            ") TO 'out.mkv'"
        )
    source = ctx.pick(["a0.attachments", "a1.attachments"])
    if ctx.bad():
        source = ctx.pick(["a0.audio", "a0.chapters", "a0.nosuch"])
    return (
        "COPY (\n  SELECT a0.video[1], "
        "array_agg(ROW(c.filename, c.mimetype, 'font.ttf')::attachment) AS attachments\n"
        f"  FROM input('in.mp4') a0, input('other.mkv') a1, unnest({source}) c\n"
        "  GROUP BY a0.video[1]\n"
        ") TO 'out.mkv'"
    )


def gen_query(rng: random.Random, p_bad: float) -> str:
    ctx = Ctx(rng, p_bad)
    r = rng.random()
    if r < 0.04:
        return gen_chapters_copy(ctx)
    if r < 0.08:
        return gen_cues_copy(ctx)
    if r < 0.12:
        return gen_attachments_copy(ctx)
    if r < 0.25:
        return gen_select(ctx)
    if r < 0.75:
        return gen_copy(ctx)
    stmts = []
    for i in range(rng.randint(1, 2)):
        name = f"v{i}"
        stmts.append(f"CREATE VIEW {name} AS\n{gen_select(ctx, view=True)}")
        ctx.names.append(name)
    for _ in range(rng.randint(1, 2)):
        stmts.append(gen_copy(ctx))
    return ";\n".join(stmts)


# -- random probe results ---------------------------------------------------


def rand_stream(rng, kind, index):
    meta = {}
    if rng.random() < 0.7:
        meta["language"] = rng.choice(["eng", "fra", "und", "", "zxx"])
    if rng.random() < 0.3:
        meta["title"] = rng.choice(["Track", "", "x" * 200])
    flags = {}
    if rng.random() < 0.7:
        flags = {key: rng.random() < 0.2 for key in DISPOSITION_KEYS}
    codec = rng.choice(
        {
            "video": ["h264", "hevc", "png", None, "vp9"],
            "audio": ["aac", "ac3", "opus", None],
            "subtitle": ["subrip", "mov_text", "webvtt", None],
            "data": ["bin_data", None],
        }[kind]
    )
    return StreamMeta(
        type=kind,
        index=index,
        metadata=meta,
        width=rng.choice([None, 0, 1920, 4096, -1]) if kind == "video" else None,
        height=rng.choice([None, 0, 1080, 2160]) if kind == "video" else None,
        fps=rng.choice([None, "30/1", "30000/1001", "0/0", ""]) if kind == "video" else None,
        sample_rate=rng.choice([None, 48000, 0]) if kind == "audio" else None,
        codec=codec,
        channels=rng.choice([None, 1, 2, 6, 0]) if kind == "audio" else None,
        channel_layout=rng.choice([None, "stereo", "5.1", ""]) if kind == "audio" else None,
        bitrate=rng.choice([None, 128000, 0]),
        duration=rng.choice([None, 0.0, 4.0, 1e9]),
        color_transfer=rng.choice([None, "bt709", "smpte2084"]) if kind == "video" else None,
        disposition=flags,
    )


def rand_probe(rng):
    if rng.random() < 0.08:
        return None
    streams = []
    counts = {k: rng.choice([0, 0, 1, 1, 2, 3]) for k in ("video", "audio", "subtitle", "data")}
    for kind, n in counts.items():
        for i in range(n):
            streams.append(rand_stream(rng, kind, i))
    chapters = []
    for i in range(rng.choice([0, 0, 1, 2, 4])):
        chapters.append(
            ChapterMeta(
                index=i + 1,
                start_t=rng.choice([None, 0.0, float(i), -1.0]),
                end_t=rng.choice([None, 1.0, float(i + 1), 0.0]),
                title=rng.choice([None, "", f"Ch {i}", "x" * 100]),
            )
        )
    attachments = []
    for i in range(rng.choice([0, 0, 1, 2])):
        attachments.append(
            AttachmentMeta(
                index=i + 1,
                filename=rng.choice([None, "", f"font{i}.ttf", "x" * 100]),
                mimetype=rng.choice([None, "", "application/x-truetype-font"]),
            )
        )
    tags = {}
    if rng.random() < 0.5:
        tags = {"title": rng.choice(["T", ""]), "artist": "A"}
    # A WebVTT sidecar is the only thing that has cues, so it is also the only
    # probe that carries any; everything else exercises the rejection.
    webvtt = rng.random() < 0.3
    cues = []
    if webvtt:
        for i in range(rng.choice([0, 1, 2, 4])):
            cues.append(
                CueMeta(
                    index=i + 1,
                    text=rng.choice(["", f"Cue {i}", "x" * 100, "a --> b"]),
                    start_t=rng.choice([0.0, float(i), -1.0]),
                    end_t=rng.choice([1.0, float(i) + 0.5, 0.0]),
                )
            )
    return ProbeResult(
        streams=streams,
        duration=rng.choice([None, 0.0, 4.0, 1e9]),
        chapters=chapters,
        format_name="webvtt" if webvtt else rng.choice([None, "matroska,webm"]),
        cues=cues,
        attachments=attachments,
        tags=tags,
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("count", type=int, nargs="?", default=5000, help="queries to generate")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", type=Path, default=Path("hunt-findings.json"))
    ap.add_argument(
        "--p-bad",
        type=float,
        default=0.08,
        help="deviation rate: chance each generated piece is deliberately off-grammar",
    )
    ap.add_argument(
        "--mutate", action="store_true", help="also apply textual mutations to 40%% of queries"
    )
    args = ap.parse_args()
    count, seed, out, p_bad = args.count, args.seed, args.out, args.p_bad
    do_mutate = args.mutate
    rng = random.Random(seed)
    findings: dict[str, dict] = {}
    compiled = rejected = parse_err = 0
    for _ in range(count):
        text = gen_query(rng, p_bad)
        if do_mutate and rng.random() < 0.4:
            for _ in range(rng.randint(1, 2)):
                text = mutate(text, rng)
        probe = rand_probe(rng) if rng.random() < 0.6 else (_PROBE if rng.random() < 0.9 else None)
        use_probe = probe is not None
        compiler.probe_path = lambda p, _r=probe: _r
        try:
            subbed = vars_module.substitute(text, _DUMMY)
        except SqlmpegError as err:
            if err.code is ErrorCode.INTERNAL:
                record(findings, err, text, use_probe, "substitute")
            continue
        except Exception as err:  # noqa: BLE001
            record(findings, err, text, use_probe, "substitute-escape")
            continue
        for phase, fn in (("compile", compiler.compile_sql), ("table", compiler.compile_table_sql)):
            try:
                result = fn(subbed)
            except SqlmpegError as err:
                rejected += 1
                if err.code is ErrorCode.PARSE_ERROR:
                    parse_err += 1
                if err.code is ErrorCode.INTERNAL:
                    record(findings, err, subbed, use_probe, phase)
                continue
            except Exception as err:  # noqa: BLE001
                record(findings, err, subbed, use_probe, phase + "-escape")
                continue
            if phase == "compile":
                assert isinstance(result, Graph)
            compiled += 1
    print(
        f"examples={count} seed={seed} p_bad={p_bad} "
        f"compiled={compiled} rejected={rejected} parse_errors={parse_err}"
    )
    print(f"distinct findings: {len(findings)}")
    dump = sorted(findings.values(), key=lambda d: -d["hits"])
    for d in dump:
        print(f"--- {d['hits']}x {d['key']}")
        print(f"    cause: {d['cause']}")
        print(f"    frames: {d['frames'][-3:]}")
    out.write_text(json.dumps(dump, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
