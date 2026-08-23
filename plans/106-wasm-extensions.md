# 106 — Wasm extensions: `LANGUAGE wasm`, and nothing else

Maintainer, 2026-08-23. Supersedes the "functions in another language"
section of `ROADMAP.md`, which had sqlmpeg compiling Rust.

## The realisation this turns on

Postgres's `LANGUAGE c` does not mean "the body is C". It means the
function lives behind the C ABI in a shared object, and the body is not
code at all — it is a module and a symbol:

    CREATE FUNCTION add_one(int) RETURNS int
    AS 'MODULE_PATHNAME', 'add_one' LANGUAGE c;

The language names a CALLING CONVENTION. Anything that can produce a
conforming object works. Take exactly that:

    CREATE FUNCTION deband(v video_stream, strength number DEFAULT 4)
    RETURNS video_stream
    AS 'wasm/deband.wasm', 'deband' LANGUAGE wasm;

The declaration gives the shape — parameters, defaults, return type.
The module gives the implementation. The two-part `AS` answers "which
export", a question an inline body would have made us invent an answer
for.

**`LANGUAGE rust` is dropped and will not be added.** Compiling a source
language is someone else's problem, and the whole codegen story goes
with it: no WIT templating, no `sqlmpeg build`, no toolchain in the
registry's CI, no reproducible-Rust-build worry, no "who compiled this
and can you trust it" protocol. sqlmpeg compiles SQL. It does not
compile Rust.

That also makes extensions language-agnostic by construction — Rust, Go,
Zig, C, AssemblyScript, anything that targets wasm — rather than by us
writing a codegen per language.

## The return type still says what kind of extension it is

Unchanged, and the reason this fits without new machinery: a
value-returning function inlines into the graph, so `RETURNS
video_stream` is a filter stage; a table-returning one is a row source,
so `RETURNS TABLE(...)` is an analysis pass. Filters, analysis functions
and SQL functions stay one feature with one surface.

## Frames are rows to the MODULE, never to SQL

Also unchanged, and now cleaner: a module iterates frame-rows — pts,
dimensions, the stream's tags, the planes — and that belongs to the WIT
world, not the dialect. Every relation the compiler sees is countable at
compile time, which is what makes `WHERE` filter tracks during
compilation, the one-row rule decidable, and `GROUP BY` a compile-time
partition. A frame relation would be the first whose cardinality is
unknown until ffmpeg runs.

## Development

Fork an example repo: a Rust crate wired to the WIT world with one
function to replace. Build, get a `.wasm`. That repo is not part of
sqlmpeg and does not gate anything here.

An author ships **the `.wasm` and the `CREATE FUNCTION` declaration**.
The Rust source is theirs to publish or not — it is not required, not
expected, and nothing in the registry reads it.

## Distribution: already built

A wasm filter is an ORDINARY package export. The `CREATE FUNCTION` sits
in a `lib` file listed in the manifest's `lib` map, reached as
`acme.deband.deband(v)`. The `.wasm` is a file in the package directory,
so the archive digest already covers it and the store already holds it.

No new distribution mechanism, no new resolution rule, no new client
command. The `AS` path is relative to the manifest, like every other
file a manifest names.

## What must be checked

**The signature is a claim.** `RETURNS video_stream` is what the SQL
asserts; the module's export is what runs. A component's exported type
is inspectable, so the compiler verifies the declaration against the
module and a mismatch is a typed rejection rather than a crash at run
time. The registry does the same at publish.

**Capabilities are DERIVED, not declared.** A component's imports are
its capability requirements: a module importing `wasi:nn` needs
inference, one importing nothing is pure. So the package page shows what
a filter can reach without trusting a manifest field, and the compiler
refuses a module needing `wasi:nn` against a host that cannot grant it —
read off the module itself. A declared field can be wrong; an import
list cannot.

## The trust change, stated plainly

Packages have shipped SQL until now: readable, and validated by
compiling it. A package may now ship a binary nobody can read, with no
source to compare it against — we are not asking for source and could
not verify it if we were. That is a real change and it should be written
down rather than discovered.

What holds it up:

- wasm is sandboxed with no ambient authority, so a module reaches only
  what it imports;
- imports are visible before install, so "this filter wants inference"
  or "wants the network" is on the package page;
- the digest pins exactly what was published, and a published version
  cannot change under a pin.

What does not hold it up: any claim that the binary corresponds to any
particular source. A package MAY name a source repository and build
command for a human to check; nothing verifies it, and the field should
say so.

## Hosts

Two, running the same module — the maintainer's requirement:

- **frei0r/ladspa**, in-process and single-threaded, for the command
  line. No wasi, so a module importing anything beyond the world is
  refused against it at compile time.
- **The sidecar**, ffmpeg piping out to a process and back, for
  throughput and for capabilities.

Which one is a compile-time policy, decided from the module's imports
and the graph's shape, and `explain` should show it.

## The long pole

The sidecar makes a filter graph DISJOINT. `Emitted` today is a list of
commands run in sequence and stopped at the first failure. The sidecar
needs a graph of concurrent processes joined by pipes, where a death
propagates rather than hanging the others, and where a timeout covers
the set rather than each member. That is an IR change and an execution
change, and it is larger than anything else here.

## Order

1. **The WIT world.** Everything is downstream of what a module sees.
   Getting it wrong means rewriting both hosts and every published
   module. It is now a published contract authors target rather than
   something generated around them, which makes it easier to version and
   harder to change quietly.
2. **`LANGUAGE wasm` in the parser and the expander**: the two-part
   `AS`, the signature check against the module, the capability read.
   Small, once the world exists.
3. **The frei0r host**, which needs no IR change and proves the loop end
   to end: authored, published, installed, called, run.
4. **The disjoint-graph IR**, then the sidecar.

`wasi:nn` is deliberately last. It is the most interesting capability
and the least load-bearing: one pure pixel filter through the whole loop
first. If that works, inference is an increment; if it does not, wasi-nn
will not save it.
