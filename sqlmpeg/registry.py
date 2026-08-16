"""ffmpeg filter registry for sqlmpeg (RFC-003 "Dynamic filters").

Introspects the ffmpeg binary on PATH for the FULL set of filters it
supports (~460-560 depending on build), beyond the curated stdlib. Two
ffmpeg CLI outputs drive this:

  `ffmpeg -hide_banner -filters` -- one line per filter: a 3-character flag
  column (T/. S/. C/.), the filter name, a pad spec (`V->V`, `AA->A`, `N`
  for dynamic pad count, `|` for source/sink), and a one-line description.
  This is the ONLY source for pad signatures; -help does not restate them
  in a machine-parseable way for sources/sinks.

  `ffmpeg -hide_banner -help filter=<name>` -- per-option lines of the form
  `   <name>   <type>   <flags>   <description> (from A to B) (default D)`
  at a fixed 3-space indent, with enum constants as `<name> <value> <flags>
  <description>` lines indented 5 spaces directly below their parent
  option.

NEVER raises: any subprocess failure, timeout, missing ffmpeg, or
unparseable output degrades permissively (see RFC "Registry mechanics" --
"Unparseable single options degrade to type str ... rather than dropping
the filter"). Depends only on `sqlmpeg.ir` (for `StreamType`) and the
stdlib.

Format quirks discovered empirically against ffmpeg 7.1
(`ffmpeg version 7.1-full_build-www.gyan.dev`, captured 2026-08) that this
parser is built around -- see tests/test_registry.py for the captured
fixtures this was verified against:

  - The `-filters` legend (`Filters:` + 7 lines of `X = ...` key) is NEVER
    explicitly skipped by line count -- the per-line regex requires the
    3-char flag column at column 1, which the legend text never matches
    (its lines start with two spaces, not one-space-plus-flags). This
    makes parsing resilient to legend wording changes across ffmpeg
    versions.
  - Pad specs seen in a full scan of ffmpeg 7.1's ~560 filters: `A->A`,
    `AA->A`, `V->V`, `VV->V`, `VVV->V`, `VVVV->V`, `A->V`, `VV->A` (never
    observed, but mixed in/out letters are parsed verbatim per character
    regardless of whether a concrete example exists). Multi-output specs
    exist and are excluded (`VV->VV` e.g. `feedback`, `scale2ref`). `N`
    (dynamic pad count, e.g. `split`: `V->N`, `concat`: `N->N`) and `|`
    (source/sink, e.g. `testsrc`: `|->V`, `anullsink`: `A->|`) are excluded
    per the RFC v1 scope fence. Every zero-input filter observed uses `|`
    as its input character (never an empty string); the "zero inputs"
    exclusion is defensive belt-and-suspenders for a hypothetical future
    ffmpeg encoding, not something seen in practice.
  - The `-help filter=X` AVOptions header does NOT always read
    `"X AVOptions:"` -- ffmpeg groups filters that share an implementation
    under one header, e.g. `split`'s header is `"(a)split AVOptions:"`
    (shared with `asplit`), `acompressor`'s is
    `"acompressor/sidechaincompress AVOptions:"` (51 of ~460 included
    filters have a mismatched header in a full scan). The parser therefore
    does NOT match the header text against the filter name; it takes the
    FIRST line containing "AVOptions:" in the help output as the start of
    the option block, unconditionally.
  - Some filters' help output has MULTIPLE "AVOptions:" sections separated
    by a blank line -- e.g. `scale` has its own section, then a
    `SWScaler AVOptions:` section (option names there are even prefixed
    with a literal `-`, e.g. `-sws_flags`, at 2-space indent) and a
    `framesync AVOptions:` section; `overlay` has its own section then
    `framesync AVOptions:`. Only the FIRST block (up to the first blank
    line) is parsed; later sections belong to a different AVClass and are
    not this filter's own options.
  - Filters with no options at all (e.g. `anullsink`) have no "AVOptions:"
    line in their help output; this degrades permissively to an empty
    options dict, not an error.
  - Short/long option aliases are listed as separate AVOption lines with
    IDENTICAL description text, e.g. `subtitles` has `filename`/`f`,
    `testsrc` has `size`/`s`, `rate`/`r`, `scale` has `w`/`width`,
    `h`/`height`. Empirically the LONGER name is the one worth keeping
    (confirmed against 456 long-first, 29 short-first, and 17 equal-length
    duplicate-doc groups across a full scan of ffmpeg 7.1's included
    filters); file ORDER is not a reliable signal by itself (`scale` lists
    `w` before `width`, but `size` before `s` -- inconsistent). This module
    dedups by identical doc text within a filter and keeps the longest
    name, breaking length ties by first occurrence.
  - Enum options are `<int>`-typed AVOptions whose default is often a
    CONSTANT NAME rather than a number (e.g. xfade's `transition` default
    is "fade", not "0"); the constants are the 5-space-indented lines
    immediately following the option line, each `<name> <value> <flags>
    <description>` (description may be empty, e.g. overlay's `format`
    enum). `default` is stored completely verbatim (whatever text ffmpeg
    printed) for documentation purposes only -- it is never validated
    against the constant list or re-typed.
  - `(from A to B)` bounds are not always numeric (`DBL_MAX`, `INT_MAX`,
    `INT_MIN` appear, e.g. hqdn3d's `luma_spatial` is
    `(from 0 to DBL_MAX)`); `minimum`/`maximum` are `None` when a bound
    does not parse as a Python float.
  - No real `binary` or `dictionary` typed option was observed in a full
    scan of all ~460 included ffmpeg 7.1 filters (the RFC's exclusion rule
    for these types -- mapped to `type="str"`, `unusable=True` -- is
    implemented but exercised only by a constructed fixture in tests, not
    a captured one).
  - `boolean`-typed options can have a non-boolean-looking default, e.g.
    subtitles' `wrap_unicode` is `<boolean> ... (default auto)` -- again,
    `default` is stored verbatim without validation.
  - Types seen beyond the RFC's table (`channel_layout`, `pix_fmt`) are
    mapped to `"str"` via a permissive fallback (anything not in the
    explicit type map degrades to `"str"`, matching the "unparseable
    option lines degrade to str" policy for option TYPES as well as whole
    lines).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from sqlmpeg.ir import StreamType

_TIMEOUT_SECONDS = 10.0

FilterOptionType = Literal["num", "str", "bool"]


@dataclass(frozen=True)
class FilterOption:
    name: str
    type: FilterOptionType
    doc: str
    minimum: float | None
    maximum: float | None
    default: str | None  # verbatim ffmpeg text, doc use only -- never validated
    constants: tuple[str, ...]  # enum constant names, () if not an enum
    unusable: bool = False  # binary/dictionary AVOption types -- plan 031 rejects usage


@dataclass(frozen=True)
class DynamicFilter:
    name: str
    inputs: tuple[StreamType, ...]  # from the pad spec, e.g. ("video", "video")
    output: StreamType
    doc: str
    # Options are NOT stored here -- Registry.options() loads and caches
    # them lazily, per-filter, on first reference (never all ~460 filters
    # upfront, per the RFC's "-help parsed lazily on first REFERENCE").


# --- subprocess plumbing (guardrail #6: argv lists, timeout, never raise) --


def _run(argv: list[str]) -> str | None:
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _get_version_line(ffmpeg: str) -> str | None:
    out = _run([ffmpeg, "-version"])
    if out is None:
        return None
    lines = out.splitlines()
    if not lines:
        return None
    return lines[0]


# --- `-filters` parsing ------------------------------------------------------

# One space, then the 3-char flag column (each of T/S/C is either its
# letter or '.'), then the name, the pad spec, and the rest of the line as
# the description. This intentionally does NOT match the two-space-indented
# legend lines ("  T.. = Timeline support") or the "Filters:" banner line,
# so no separate header-skipping logic is needed.
_FILTER_LINE_RE = re.compile(r"^ [T.][S.][C.] (\S+)\s+(\S+)\s+(.*)$")

_PAD_CHARS: dict[str, StreamType] = {"V": "video", "A": "audio"}


def _parse_filters_list(ffmpeg: str) -> dict[str, DynamicFilter] | None:
    out = _run([ffmpeg, "-hide_banner", "-filters"])
    if out is None:
        return None
    filters: dict[str, DynamicFilter] = {}
    for line in out.splitlines():
        m = _FILTER_LINE_RE.match(line)
        if not m:
            continue
        name, spec, doc = m.groups()
        if "->" not in spec:
            continue
        inp, _, outp = spec.partition("->")
        # v1 scope fence: exclude dynamic pad count (N), source/sink (|),
        # multi-output, and (defensively) zero-input specs.
        if not inp or "N" in spec or "|" in spec or len(outp) != 1:
            continue
        try:
            inputs = tuple(_PAD_CHARS[c] for c in inp)
            output = _PAD_CHARS[outp]
        except KeyError:
            continue
        filters[name] = DynamicFilter(name=name, inputs=inputs, output=output, doc=doc.strip())
    return filters


# --- `-help filter=X` parsing ------------------------------------------------

_OPTION_LINE_RE = re.compile(r"^   (\S+)\s+<(\w+)>\s+(\S+)\s+(.*)$")
_CONST_LINE_RE = re.compile(r"^     (\S+)\s+(\S+)\s+(\S+)\s*(.*)$")
_RANGE_RE = re.compile(r"\(from (\S+) to (\S+)\)")
_DEFAULT_RE = re.compile(r"\(default (.*)\)\s*$")

_TYPE_MAP: dict[str, FilterOptionType] = {
    "int": "num",
    "int64": "num",
    "float": "num",
    "double": "num",
    "rational": "num",
    "boolean": "bool",
    "string": "str",
    "color": "str",
    "duration": "str",
    "image_size": "str",
    "video_rate": "str",
    "flags": "str",
}
_UNUSABLE_TYPES = frozenset({"binary", "dictionary"})


@dataclass
class _RawOption:
    name: str
    type: str
    doc: str
    minimum: float | None
    maximum: float | None
    default: str | None
    constants: list[str] = field(default_factory=list)


def _try_float(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def _parse_option_block(lines: list[str]) -> dict[str, FilterOption]:
    """Parse option/constant lines up to the first blank line (or EOF)."""
    raw: list[_RawOption] = []
    current: _RawOption | None = None
    for line in lines:
        if line.strip() == "":
            break
        indent = len(line) - len(line.lstrip(" "))
        if indent >= 5 and current is not None:
            m = _CONST_LINE_RE.match(line)
            if m:
                current.constants.append(m.group(1))
            continue
        if indent == 3:
            m2 = _OPTION_LINE_RE.match(line)
            if m2:
                oname, otype, _flags, rest = m2.groups()
                default = None
                dm = _DEFAULT_RE.search(rest)
                if dm:
                    default = dm.group(1).strip()
                minimum = maximum = None
                rm = _RANGE_RE.search(rest)
                if rm:
                    minimum = _try_float(rm.group(1))
                    maximum = _try_float(rm.group(2))
                doc = _DEFAULT_RE.sub("", _RANGE_RE.sub("", rest)).strip()
                current = _RawOption(oname, otype, doc, minimum, maximum, default)
            else:
                # Option-shaped line (3-space indent) that doesn't match the
                # expected `<name> <type> <flags> desc` shape: degrade to a
                # bare str option (RFC: stay quiet, don't drop the filter).
                parts = line.strip().split(None, 1)
                if not parts:
                    current = None
                    continue
                oname = parts[0]
                doc = parts[1] if len(parts) > 1 else ""
                current = _RawOption(oname, "string", doc, None, None, None)
            raw.append(current)
            continue
        # Any other indentation is not a recognized option/constant line;
        # ignore it without disturbing `current` (permissive).
    return _dedup_and_convert(raw)


def _dedup_and_convert(raw: list[_RawOption]) -> dict[str, FilterOption]:
    # Group by identical doc text (ffmpeg's signal for short/long aliases,
    # e.g. scale's `w`/`width`) and keep only the longest name per group,
    # breaking length ties by first occurrence.
    by_doc: dict[str, list[int]] = {}
    for i, o in enumerate(raw):
        by_doc.setdefault(o.doc, []).append(i)
    keep: set[int] = set()
    for idxs in by_doc.values():
        best = idxs[0]
        for i in idxs[1:]:
            if len(raw[i].name) > len(raw[best].name):
                best = i
        keep.add(best)

    result: dict[str, FilterOption] = {}
    for i in sorted(keep):
        o = raw[i]
        unusable = o.type in _UNUSABLE_TYPES
        if unusable or o.constants:
            ftype: FilterOptionType = "str"
        else:
            ftype = _TYPE_MAP.get(o.type, "str")
        result[o.name] = FilterOption(
            name=o.name,
            type=ftype,
            doc=o.doc,
            minimum=o.minimum,
            maximum=o.maximum,
            default=o.default,
            constants=tuple(o.constants),
            unusable=unusable,
        )
    return result


def _parse_filter_help(ffmpeg: str, name: str) -> dict[str, FilterOption]:
    out = _run([ffmpeg, "-hide_banner", "-help", f"filter={name}"])
    if out is None:
        return {}
    lines = out.splitlines()
    start = None
    for i, line in enumerate(lines):
        if "AVOptions:" in line:
            start = i + 1
            break
    if start is None:
        return {}
    return _parse_option_block(lines[start:])


# --- disk cache: ~/.cache/sqlmpeg/, keyed by hash of `ffmpeg -version` -------


def _cache_dir() -> Path:
    try:
        return Path.home() / ".cache" / "sqlmpeg"
    except RuntimeError:  # pragma: no cover -- no resolvable home directory
        return Path(tempfile.gettempdir()) / "sqlmpeg-cache"


def _cache_path(version_line: str) -> Path:
    digest = hashlib.sha256(version_line.encode("utf-8")).hexdigest()[:16]
    return _cache_dir() / f"registry-{digest}.json"


def _require_str(v: object) -> str:
    if not isinstance(v, str):
        raise ValueError("expected str")
    return v


def _require_optional_str(v: object) -> str | None:
    if v is None:
        return None
    return _require_str(v)


def _require_bool(v: object) -> bool:
    if not isinstance(v, bool):
        raise ValueError("expected bool")
    return v


def _require_optional_float(v: object) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError("expected float")
    return float(v)


def _require_list(v: object) -> list[object]:
    if not isinstance(v, list):
        raise ValueError("expected list")
    return v


def _require_option_type(v: object) -> FilterOptionType:
    if v == "num":
        return "num"
    if v == "str":
        return "str"
    if v == "bool":
        return "bool"
    raise ValueError(f"bad option type {v!r}")


def _require_stream_type(v: object) -> StreamType:
    if v == "video":
        return "video"
    if v == "audio":
        return "audio"
    raise ValueError(f"bad stream type {v!r}")


@dataclass
class _DiskCache:
    filters: dict[str, DynamicFilter]
    options: dict[str, dict[str, FilterOption]]


def _decode_filters(raw: object) -> dict[str, DynamicFilter]:
    if not isinstance(raw, dict):
        raise ValueError("filters not a dict")
    result: dict[str, DynamicFilter] = {}
    for name, entry in raw.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            raise ValueError("bad filter entry")
        inputs_raw = _require_list(entry["inputs"])
        inputs = tuple(_require_stream_type(x) for x in inputs_raw)
        output = _require_stream_type(entry["output"])
        doc = _require_str(entry["doc"])
        result[name] = DynamicFilter(name=name, inputs=inputs, output=output, doc=doc)
    return result


def _decode_option(raw: object) -> FilterOption:
    if not isinstance(raw, dict):
        raise ValueError("option not a dict")
    return FilterOption(
        name=_require_str(raw["name"]),
        type=_require_option_type(raw["type"]),
        doc=_require_str(raw["doc"]),
        minimum=_require_optional_float(raw["minimum"]),
        maximum=_require_optional_float(raw["maximum"]),
        default=_require_optional_str(raw["default"]),
        constants=tuple(_require_str(c) for c in _require_list(raw["constants"])),
        unusable=_require_bool(raw["unusable"]),
    )


def _decode_options(raw: object) -> dict[str, dict[str, FilterOption]]:
    if not isinstance(raw, dict):
        raise ValueError("options not a dict")
    result: dict[str, dict[str, FilterOption]] = {}
    for filter_name, opts in raw.items():
        if not isinstance(filter_name, str) or not isinstance(opts, dict):
            raise ValueError("bad options entry")
        result[filter_name] = {
            opt_name: _decode_option(opt)
            for opt_name, opt in opts.items()
            if isinstance(opt_name, str)
        }
    return result


def _read_disk_cache(version_line: str) -> _DiskCache | None:
    path = _cache_path(version_line)
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw_text)
        if not isinstance(data, dict):
            return None
        if data.get("version_line") != version_line:
            return None
        filters = _decode_filters(data["filters"])
        options = _decode_options(data["options"])
        return _DiskCache(filters=filters, options=options)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _write_disk_cache(
    version_line: str,
    filters: dict[str, DynamicFilter],
    options: dict[str, dict[str, FilterOption]],
) -> None:
    data: dict[str, object] = {
        "version_line": version_line,
        "filters": {
            name: {"inputs": list(f.inputs), "output": f.output, "doc": f.doc}
            for name, f in filters.items()
        },
        "options": {
            filter_name: {
                opt_name: {
                    "name": o.name,
                    "type": o.type,
                    "doc": o.doc,
                    "minimum": o.minimum,
                    "maximum": o.maximum,
                    "default": o.default,
                    "constants": list(o.constants),
                    "unusable": o.unusable,
                }
                for opt_name, o in opts.items()
            }
            for filter_name, opts in options.items()
        },
    }
    # Cache is purely an optimization -- any filesystem failure is silently
    # swallowed, never raised.
    try:
        cache_dir = _cache_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=cache_dir, prefix="registry-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp_name, _cache_path(version_line))
        except OSError:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    except OSError:
        pass


# --- Registry -----------------------------------------------------------


class Registry:
    """Lazily-loaded view of the installed ffmpeg's filter set.

    `-filters` is parsed at most once per process, on first call to any of
    `available()`, `get()`, `options()`, or `names()`. Per-filter
    `-help filter=X` is parsed at most once per filter, on first call to
    `options(name)` for that filter -- never for all filters upfront.
    NEVER raises.
    """

    def __init__(self) -> None:
        self._ffmpeg: str | None = None
        self._version_line: str | None = None
        self._loaded = False
        self._filters: dict[str, DynamicFilter] = {}
        self._options: dict[str, dict[str, FilterOption]] = {}

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True

        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            return
        version_line = _get_version_line(ffmpeg)
        if version_line is None:
            return
        self._ffmpeg = ffmpeg
        self._version_line = version_line

        disk = _read_disk_cache(version_line)
        if disk is not None:
            self._filters = disk.filters
            self._options = disk.options
            return

        filters = _parse_filters_list(ffmpeg)
        if not filters:
            return
        self._filters = filters
        _write_disk_cache(version_line, self._filters, self._options)

    def available(self) -> bool:
        """True if ffmpeg was found on PATH and `-filters` parsed."""
        self._ensure_loaded()
        return bool(self._filters)

    def names(self) -> list[str]:
        """All included (non-excluded) filter names, ffmpeg's own order."""
        self._ensure_loaded()
        return list(self._filters)

    def get(self, name: str) -> DynamicFilter | None:
        """None if `name` is unknown to this ffmpeg OR was excluded (v1 scope fence)."""
        self._ensure_loaded()
        return self._filters.get(name)

    def options(self, name: str) -> dict[str, FilterOption] | None:
        """None if `name` is unknown; {} if known but has no (parseable) options."""
        self._ensure_loaded()
        if name not in self._filters:
            return None
        cached = self._options.get(name)
        if cached is not None:
            return cached
        if self._ffmpeg is None:
            return {}
        opts = _parse_filter_help(self._ffmpeg, name)
        self._options[name] = opts
        if self._version_line is not None:
            _write_disk_cache(self._version_line, self._filters, self._options)
        return opts


_registry: Registry | None = None


def load() -> Registry:
    """Return the process-wide Registry singleton (memoized)."""
    global _registry
    if _registry is None:
        _registry = Registry()
    return _registry


def clear_cache() -> None:
    """Reset the process memo AND remove on-disk registry cache file(s). For tests."""
    global _registry
    _registry = None
    try:
        cache_dir = _cache_dir()
        if cache_dir.is_dir():
            for entry in cache_dir.iterdir():
                if entry.name.startswith("registry-") and entry.name.endswith(".json"):
                    try:
                        entry.unlink()
                    except OSError:
                        pass
    except OSError:
        pass
