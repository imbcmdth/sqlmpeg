# RFC-010 — ffmpeg is required; --no-probe dies

Status: draft 2026-08-18. Follows RFC-007 (registry = the function
surface) and RFC-009 (probe = the row model) to their conclusion: the
parts of the dialect that work without the binaries have shrunk to a
sliver, and the modes that expose that sliver are footguns.

## The two deletions

1. **The no-ffmpeg fallback mode dies.** sqlmpeg requires ffmpeg AND
   ffprobe. Discovery is PATH-first - a system install always wins - with
   a provisioning package as the fallback, so `pip install sqlmpeg` is
   one-and-done on a bare machine. Every "ffmpeg not found" error path,
   the `_NO_REGISTRY` hints, the prompt's no-registry fallback rendering,
   and the "sqlmpeg without ffmpeg is a parser" doc story are deleted.
2. **`--no-probe` dies as user surface.** Its compile-for-elsewhere use
   case was never the flag's: opportunistic probing (RFC-001, unchanged)
   already degrades silently on missing/unreadable inputs. What the flag
   uniquely did was make a READABLE file compile as if unreadable - and
   since provenance rides on probing, that silently strips
   `-metadata:s:N language=...` from the output: same SQL, two different
   commands. A determinism switch that changes the result is not a
   determinism switch. The `probe=` parameter dies ENTIRELY (amended
   2026-08-18: not even a library/test seam - "we own the fixtures, go
   ahead and probe them"). `compile_sql` always probes; missing files
   degrade opportunistically as ever, which is measurably identical to
   what probe=False produced on the goldens' illustrative paths (verify
   in-wave, byte-diff before/after). Row-model unit tests already inject
   synthetic probe dicts at the `lower()` seam - that is test
   parameterization, not a mode, and it stays.

## Provisioning (folds in wave 055)

A default dependency that ships/installs static ffmpeg+ffprobe when none
is on PATH, behind one `sqlmpeg/binaries.py` helper replacing the
scattered `shutil.which` calls (registry, probe, cli run). Provider
vetted EMPIRICALLY at implementation time; the disqualifier to check
first is ffprobe (several popular packages ship ffmpeg alone; probing is
load-bearing, so no ffprobe = no deal). Candidates to vet: static-ffmpeg
(downloads on first use, has ffprobe), imageio-ffmpeg (ffmpeg only -
expected reject), others the implementing agent finds. Document the
chosen provider's model (wheel-embedded vs first-use download, sizes,
platforms). PATH always wins so video engineers never get a second
ffmpeg they didn't ask for.

## What changes where

- cli.py: `--no-probe` removed from compile/explain/validate (usage
  error). `run`'s "ffmpeg not found" hint becomes "the provisioner
  should have handled this; check <provider> installed correctly".
- registry/probe: `which` calls route through binaries.py; the
  empty-registry degradation path stays INTERNALLY (guardrail #7 - a
  broken provisioner must still fail typed, not crash) but is no longer
  a documented mode.
- errors.md: UNKNOWN_FUNCTION's no-ffmpeg paragraph rewritten;
  UNSUPPORTED_SQL loses nothing (flag removal is argparse-level).
- README + filters.md + tracks.md: the honest-parser framing and every
  `--no-probe` mention retired; install section gains the provisioning
  story. Cookbook "How to read this file": the offline tier's meaning
  becomes "the shown command depends on no local media" (the harness
  keeps stubbing probe underneath - test machinery, not user surface).
- prompt.py: the no-registry fallback branch deleted; one prompt,
  always rendered from a registry.
- probe.py: a local-path existence check before spawning ffprobe -
  without the probe= parameter, goldens/fuzzing would otherwise pay a
  failing subprocess per illustrative path; missing file -> None with no
  spawn is both faster and the same policy.
- tests: conftest snapshot pinning unchanged; test_cli --no-probe tests
  become flag-rejection tests; every compile_sql(probe=False) call site
  drops the argument (goldens byte-diffed before/after to prove the
  no-op); fixture-probing tests assert STABLE fields only (tags, counts,
  layouts, dimensions, approximate durations) - never exact encoder
  output like bitrate, which drifts across the ffmpeg that generated the
  fixture.

## Waves

- 065 (sonnet): binaries.py + provider vetting (STOP gate: the chosen
  provider must ship ffprobe on win/mac/linux, verified by actually
  installing it) + which-call routing + flag removal + test updates.
- 066 (orchestrator): docs prose, README install story, prompt wording
  review, version bump (minor), merge, tag, push.

## Non-goals

Bundling binaries in OUR wheel (the provider owns that problem);
removing compile_sql(probe=...) from the library; changing opportunistic
probe degradation for missing/remote files (that IS the offline story
now, and it needs no flag).
