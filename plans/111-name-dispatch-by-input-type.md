# 111 — Follow-up: a filter name dispatches on its input type

Maintainer, 2026-08-24. Section 4 of plan 108. A convenience for
authors; nothing waits on it. Wave B does its own variant picking
internally, so this is not on its critical path.

## The rule

**When a call names `X` and `aX` also exists, the input stream's type
picks which.** Video in, `X`; audio in, `aX`.

The compiler knows every stream's type, so this is never a guess.

## Why it is safe

All 29 such pairs in the reference registry separate cleanly on input —
every `X` takes video-only inputs, every `aX` audio-only. Re-run the
check before starting, since the registry moves:

    uv run python -c "
    import json
    from pathlib import Path
    f=json.loads(Path('tests/data/reference_registry.json').read_text())['filters']
    bad=[(n,sorted(set(f[n]['inputs'])),'a'+n,sorted(set(f['a'+n]['inputs'])))
         for n in f if 'a'+n in f
         and (set(f[n]['inputs'])!={'video'} or set(f['a'+n]['inputs'])!={'audio'})]
    print('not clean:', bad)
    "

Three pairs disagree on OUTPUT type — `drawgraph`, `graphmonitor` and
`histogram` return video from audio. That costs nothing: dispatch reads
the input, and misusing the result is an ordinary type error downstream.
Do not let output types influence the rule.

## Boundaries

- **Raw names are untouched.** `ffmpeg.setpts` and `ffmpeg.asetpts` stay
  exactly what ffmpeg has. Dispatch is a convenience over the curated
  surface, never a reinterpretation of an explicit raw call.
- **One behaviour change, and only one**: `fade(a)` on an audio stream
  is a type error today and means `afade` afterwards. That is the trade
  being taken deliberately — someone handing audio to `fade` wants their
  audio faded. Find every test that pins such a rejection and change it
  knowingly, one at a time; a test that stops failing for an unexamined
  reason is a regression in disguise.
- **Broadcast still applies.** A bare array in an argument position
  means one node per element; the element type picks the variant
  once for the whole array, since a stream array is homogeneous.
- The dialect prompt should describe the rule, since a model writing
  `fade` over audio now gets working output rather than an error.

## Recipes first

One recipe in `docs/examples.md`: the same operation over a video track
and an audio track, written with one name, compiling to `X` and `aX`.

## Verification

- The dispatched call compiles to argv byte-identical to writing `aX`
  explicitly.
- An explicit `ffmpeg.X` on an audio stream is still the type error it
  is today — dispatch must not reach raw calls.
- `ruff`, `mypy --strict`, and the unit tier stay green.

## House rules

- **Do not commit.** Leave the work uncommitted for review.
- Never write "fence" or "gate" in code, comments, docs or reports.
- Never cite a plan or RFC number in code, comments, docstrings or error
  messages.
- Short factual WHAT comments only.
- Run the targeted tests, not the whole suite. Report concisely.
