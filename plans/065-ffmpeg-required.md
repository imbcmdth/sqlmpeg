# 065 — ffmpeg required: binaries.py, flag deletions  (model: sonnet ·
branch ffmpeg-required · RFC-010 wave 1; green per-wave, no TDD red)

Read plans/rfc-010-ffmpeg-required.md (authoritative; amended: the
probe= parameter dies entirely).

## STOP gate: provider vetting, empirically
Actually pip-install candidate provisioning packages into a THROWAWAY
venv (not the project's) and verify each ships BOTH ffmpeg and ffprobe,
noting platform coverage and delivery model (wheel-embedded vs first-use
download, sizes). static-ffmpeg is the expected winner (has ffprobe);
imageio-ffmpeg the expected reject (ffmpeg only). If NO candidate ships
ffprobe on win/mac/linux, STOP and report. Windows verification is
local + documented evidence for the other platforms (wheel contents /
upstream docs, cited).

## Deliverables
1. sqlmpeg/binaries.py: `ffmpeg_path()` / `ffprobe_path()` - PATH first
   (shutil.which), provider fallback (lazy import; provider absent ->
   None + the install hint). All which("ffmpeg"/"ffprobe") call sites
   (registry.py, probe.py, cli.py run) route through it. pyproject:
   provider as a DEFAULT dependency.
2. probe.py: local-path existence check before spawning ffprobe (URLs
   and non-file specs still attempt); missing -> None, no subprocess.
3. compile_sql loses the probe parameter entirely; every call site
   drops it. BYTE-DIFF PROOF: regen goldens before/after - the plan
   predicts zero diffs (illustrative paths probe to None either way);
   any diff is a STOP-and-report.
4. cli.py: --no-probe removed from compile/explain/validate (argparse
   rejects it); run's ffmpeg-missing message updated per the RFC.
5. prompt.py: the no-registry fallback branch deleted - one prompt,
   always registry-rendered (registry unavailable at prompt time =
   the provisioner failed; the error says so); regen system-prompt.md.
6. errors.py/lower.py: _NO_REGISTRY_HINT and the no-ffmpeg
   UNKNOWN_FUNCTION branch replaced with the provisioner-failed wording
   (the empty-registry code path itself STAYS - guardrail #7).
7. Tests: test_cli flag-rejection tests; binaries.py unit tests (PATH
   wins, fallback consulted, both-absent hint) with the provider
   mocked; existing probe=False call sites updated; suite green.
   Do NOT touch README.md, docs/*.md prose (066 is the orchestrator's);
   docs/system-prompt.md regen is fine (generated).

## Verify
ruff + mypy --strict on changed modules; FULL default suite green; full
`-m exec` green; golden byte-diff result stated explicitly. Report:
provider verdict table with evidence, files changed, hint wordings,
anything 066's docs must say. No git.
