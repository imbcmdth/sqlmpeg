# 000 — Shared interface contracts (read me first)

Authoritative contracts between modules. Every plan's agent MUST conform to these
signatures exactly. If a contract seems wrong, note it in your final report — do not
unilaterally change it. Full product spec: `../sqlmpeg-project.md` (read it).

## Package layout

```
sqlmpeg/
  __init__.py     # version only — owned by plan 001, do not touch elsewhere
  errors.py       # plan 002
  ir.py           # plan 002
  stdlib.py       # plan 003 (function table)
  parser.py       # plan 004 (parse + resolve)
  lower.py        # plan 005
  split.py        # plan 006
  emit.py         # plan 007
  compiler.py     # plan 005 (compile_sql pipeline)
  cli.py          # plan 008
```

## errors.py (plan 002)

```python
class ErrorCode(str, Enum):
    PARSE_ERROR = "PARSE_ERROR"
    UNKNOWN_FUNCTION = "UNKNOWN_FUNCTION"
    UNKNOWN_ALIAS = "UNKNOWN_ALIAS"
    UDF_ARG_TYPE = "UDF_ARG_TYPE"
    SINGLE_OUTPUT_ONLY = "SINGLE_OUTPUT_ONLY"
    NO_STREAMING_EQUIVALENT = "NO_STREAMING_EQUIVALENT"
    CONCAT_MISMATCH = "CONCAT_MISMATCH"      # reserved for v1 probing
    UNSUPPORTED_SQL = "UNSUPPORTED_SQL"      # catch-all for constructs outside dialect
    INTERNAL = "INTERNAL"                    # bug backstop; fuzz asserts this never fires

class SqlmpegError(Exception):
    code: ErrorCode
    message: str
    line: int | None
    col: int | None
    hint: str | None
    def to_dict(self) -> dict[str, object]: ...  # keys: line, col, code, message, hint
```

`to_dict()` matches the JSON error contract in the spec verbatim.

## ir.py (plan 002)

```python
FrameRef = str  # a Node.id, or "src:<alias>" for a raw input stream

@dataclass
class Node:
    id: str
    filter: str            # ffmpeg filter name (post macro-expansion)
    args: dict[str, object]  # normalized, SQL arg order already mapped to ffmpeg names
    inputs: list[FrameRef]

@dataclass
class Graph:
    input_paths: list[str]        # -i order; index is the ffmpeg input index
    sources: dict[str, int]       # alias -> index into input_paths
    nodes: dict[str, Node]        # insertion-ordered
    output: FrameRef
    def to_dict(self) -> dict: ...      # deterministic JSON for `explain` + golden tests
    @classmethod
    def from_dict(cls, d: dict) -> "Graph": ...
```

Note: `input_paths` extends the spec's dataclass — emit needs paths, and golden IR
must be self-contained.

## stdlib.py (plan 003)

```python
ParamKind = Literal["frame", "num", "str"]

@dataclass(frozen=True)
class Param:
    name: str
    kind: ParamKind

class ExpandCtx(Protocol):
    def node(self, filter: str, args: dict[str, object], inputs: list[FrameRef]) -> FrameRef:
        """Create a Node with a fresh id, register it, return its id."""

@dataclass(frozen=True)
class FuncSpec:
    name: str
    variants: tuple[tuple[Param, ...], ...]   # overloads; arity+kinds checked by lower
    doc: str                                  # one line, drives --help/docs/LLM prompt
    expand: Callable[[ExpandCtx, list[object]], FrameRef]
    # expand args: FrameRef for frame params, python int/float/str for literals,
    # in SQL argument order. Returns the FrameRef of the subgraph output.

FUNCTIONS: dict[str, FuncSpec]  # THE single source of truth (guardrail #4)
```

Simple filters: `expand` is one `ctx.node(...)` call. Macros (`blur_regions`): several.

## parser.py (plan 004)

```python
QueryExpr = exp.Select | exp.Union   # a plain SELECT or a UNION ALL of them

def parse(text: str) -> sqlglot.exp.Expression   # read="postgres"; raises SqlmpegError(PARSE_ERROR)

@dataclass
class Resolved:
    select: QueryExpr                     # top-level query, CTEs still attached
    input_paths: list[str]                # -i order; index = ffmpeg input index; MAY REPEAT PATHS
    sources: dict[str, int]               # alias -> input index (dedup key is the ALIAS, not path)
    ctes: dict[str, QueryExpr]            # CTE name -> body, definition order (body may be UNION ALL)
    branches: list[exp.Select]            # select flattened into UNION ALL branches (len 1 if plain)

def resolve(tree) -> Resolved   # raises UNKNOWN_ALIAS / SINGLE_OUTPUT_ONLY /
                                # NO_STREAMING_EQUIVALENT / UNSUPPORTED_SQL
def union_branches(query: exp.Expr) -> list[exp.Select]  # also usable on CTE bodies
```

Input index order is traversal order: CTEs in definition order first, then top-level
branches. Aliases are Postgres-folded (unquoted → lowercase). AS SHIPPED in parser.py.

## lower.py + compiler.py (plan 005)

```python
def lower(res: Resolved) -> Graph            # lower.py
def compile_sql(text: str) -> Graph          # compiler.py: parse→resolve→lower→split
```

## split.py (plan 006)

```python
def insert_splits(g: Graph) -> Graph   # pure; fan-out>1 gets split=N spliced in
```

## emit.py (plan 007)

```python
@dataclass
class Emitted:
    inputs: list[str]        # file paths in -i order
    filter_complex: str
    output_label: str        # label WITHOUT brackets, e.g. "out"

def emit(g: Graph) -> Emitted
def build_ffmpeg_args(e: Emitted, out_path: str) -> list[str]
# ["ffmpeg", "-i", ..., "-filter_complex", fc, "-map", "[out]", "-c:a", "copy"?, out_path]
```

## Ground rules for every agent

- Python ≥3.10 typing (`X | None`, no `typing.Optional`). Full annotations everywhere.
- mypy --strict must pass on your module; ruff check must pass.
- Never raise anything except `SqlmpegError` from library code paths that consume user input.
- Do not `git commit`. Do not touch files owned by other plans.
- Venv interpreter: `D:\projects\sqlmpeg\.venv\Scripts\python.exe`.
- Run your module's tests before reporting done.
