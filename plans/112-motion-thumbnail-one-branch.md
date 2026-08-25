# 112 — Closing: the motion thumbnail as one branch

Maintainer, 2026-08-24. The last step of plan 108. Needs waves A and B
landed; independent of 111. Lives in the registry repo
(`D:\projects\sqlmpeg-registry`), not in sqlmpeg.

*Overlaps plan 113 part B, which culls and reshapes the same
registry: do the two together, once the compiler waves are in and
the registry's final shape is settled.*

`want/images`' motion thumbnail is five hand-copied branches, one per
clip, because the count could not come from a parameter. With a series
bounding a seek and `VARIADIC` gathering the result, it becomes one:

    COPY (
      WITH shots AS (
        SELECT ffmpeg.concat(VARIADIC f.video) AS frame
        FROM input(:'source') f, generate_series(1, :count) i
        WHERE f.t >= (f.duration - :len) * (i.i - 1) / (:count - 1)
          AND f.t <= (f.duration - :len) * (i.i - 1) / (:count - 1) + :len
      ),
      small AS (
        SELECT fps(scale(shots.frame, :width, -2), :fps) AS frame
        FROM shots
      )
      SELECT paletteuse(small.frame, palettegen(small.frame))
      FROM small
    ) TO :'dest'

Two shapes an earlier sketch used do not resolve, by pre-existing
rules the seek-rows wave confirmed: `array_agg` may not live in a CTE
body, and a `VARIADIC` call may not nest inside another call. The form
above — the gather in its own CTE, spreading the per-row array
directly — is the one the compiler's byte-identity test pins
(`tests/test_lower.py::test_the_motion_thumbnail_gather_matches_its_hand_written_form`).

`(i.i - 1) / (:count - 1)` runs 0 to 1 across whatever count is passed, so
the head and tail clips fall out of the same arithmetic that produces
the middle ones.

## Steps

1. Rewrite the program, with `count` a declared variable defaulting to
   the five it has today.
2. **Prove it against the old one**: same count and length must compile
   to byte-identical argv. That is the whole verification — if the argv
   differs, one of the two is wrong and it is worth knowing which.
3. Decide `want/video`'s `shot`. It exists because the fraction
   arithmetic was worth naming; a series makes it a one-liner. Retire it
   only if nothing else wants it — it is the library's first exported
   function and the only proof a cross-package call works, so check what
   depends on it before removing anything.
4. Bump and publish through the registry's normal build.

## House rules

- **Do not commit.** Leave the work uncommitted for review.
- Never write "fence" or "gate" in code, comments, docs or reports.
- Never cite a plan or RFC number in code, comments or docs.
- Report concisely.
