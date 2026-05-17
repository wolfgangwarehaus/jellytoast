# Autonomous tasks — queueable when stepping away

Work Claude can ship on a worktree branch without august watching.
Success measurable via tests or build output, not visual inspection.

Pair with `docs/TODO.md` (overall backlog) and
`docs/manual_test_plan.md` (visual checks).

## How to queue

Tell Claude something like:

> Pick the highest-priority autonomous task. Ship to
> `auto/<short-slug>`. Don't merge — leave the branch for review.

Or batch multiple:

> Fire off A_new_1 + A_new_2 + A_new_3 in parallel.

Each agent gets its own worktree so they don't conflict.

## Last updated

2026-05-17 — the `auto/*` queue is at **zero**. Today three rounds +
phase D + phase E shipped 11 branches and the cleanup pass closed
the loop. Net contribution: 533 → 979 tests (+446). All A1-A26 have
landed or been superseded.

---

## 🟢 All historical batches merged (A1-A26)

Pre-2026-05-17 rounds 1-4 (A1-A21) shipped through 2026-05-15.
Phase D (A22-A26) shipped 2026-05-17. Phase E (A19 pre-commit hooks)
shipped 2026-05-17. See `CHANGELOG.md` for per-branch detail.

The legacy "Round 1-4" detail has been retired from this doc;
`git log --oneline | grep -E "^[0-9a-f]+ (A[0-9]+|Merge auto/)"`
is canonical.

---

## 🟡 Ready, not yet queued

Currently empty. The next round of autonomous work needs research /
design first — most P1/P2 items in `docs/TODO.md` are either UI
follow-ups for already-shipped backends (which need august's eyes)
or features without a research doc yet.

Candidates to write up before queuing:

- **Visualizer rendering widget research** — the FFT pipeline
  shipped, but the rendering quality / curve smoothing / bar style
  is explicitly subjective and on the NOT-autonomous list. Need a
  research doc that pins down the visual spec narrowly enough that
  one autonomous slice can ship it.
- **Settings duplicate property cleanup** — A25 `cast_<type>_enabled`
  vs per-protocol module `<type>_enabled`. Small, mechanical,
  test-driven. Could queue without research.
- **Provider abstraction cleanup** — `cast_manager.py` (789 lines) +
  `modules/cast/dlna.py` (1188 lines) hit the size where a new
  contributor will struggle. Splits are tricky enough they want a
  research doc.

---

## 🔴 NOT autonomous — needs august

For reference, so I don't accidentally try:

- Anything where the success criterion is "looks right" (paint,
  layout, animation).
- Real-world disconnect testing.
- Real-world scrobble end-to-end.
- Cast / AirPlay device behaviour (still no DLNA / Sonos / Snapcast
  hardware available; backends shipped untested).
- Anything involving signing in to a new server.
- Anything that affects shared state outside the repo (pushing PRs
  to GitHub on august's behalf, posting issues, modifying CI, etc.).
- Visualizer rendering quality.
- Crossfade audio quality + curve subjective tuning.
- EQ preset *curve* tuning (the values themselves — math against
  source curves is fine; subjective adjustment isn't).
- Capturing screenshots for Flathub submission.
