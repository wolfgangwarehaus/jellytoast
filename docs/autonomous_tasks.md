# Autonomous tasks — queueable when stepping away

Work Claude can ship on a worktree branch without august watching.
Success measurable via tests or build output, not visual inspection.

Pair with `docs/TODO.md` (overall backlog) and
`docs/manual_test_plan.md` (visual checks).

## How to queue

Tell Claude something like:

> Pick the highest-priority autonomous task. Ship to
> `auto/<short-slug>`. Don't merge — leave the branch for review.

Each agent gets its own worktree so they don't conflict.

⚠️ **Worktree base caveat (learned 2026-05-20):** worktree-isolated
agents branch off the commit that was `main` at *session start*, not
current `main`. If earlier branches in the same session have already
merged, a later agent works against a stale tree — its diff may not
apply cleanly (A3 hit this: it edited the pre-split `cast_manager.py`
monolith and had to be hand-ported). For a chain of dependent
refactors, either fire them one-per-session, or expect to hand-port
the later ones.

## Last updated

2026-05-20 (PM) — queue still drained. The A1-A6 round, the
context-menu wiring, and `auto/smart-rule-schema-v2` (date-based
smart-playlist rules) all shipped to `main` — the latter merged and
verified against live servers. No autonomous work currently queued.

---

## 🟢 Ready to fire (in priority order)

(Empty as of 2026-05-20. The remaining `docs/TODO.md` items are all
visual, hardware-gated, or august-gated — see the NOT-autonomous list
below. The backend-only features that still need UI work — crossfade
controls, hotkey rebinding, tag editing, the sleep-timer and
smart-shuffle controls, the multi-server login field — are all UI
tasks, so they aren't autonomous candidates either. Add new entries
here as genuinely backend-only work surfaces.)

---

## 🟡 Candidates needing research first

(Empty as of 2026-05-20. Add new candidates here as they surface.)

---

## 🔴 NOT autonomous — needs august

For reference, so I don't accidentally try:

- Anything where the success criterion is "looks right" (paint,
  layout, animation).
- Real-world disconnect testing.
- Real-world scrobble end-to-end.
- Cast / AirPlay / DLNA / Sonos / Snapcast device behaviour — the
  backends + discovery fan-out ship but no hardware is available to
  verify against.
- Anything involving signing in to a new server.
- Anything that affects shared state outside the repo (pushing PRs
  to GitHub on august's behalf, posting issues, modifying CI, etc.).
- Crossfade Settings UI exposure — the QSettings keys + backend
  honour are tested; the checkbox + slider visuals are not.
- Multi-server hostnames login UI — backend tested; "+ Add alternate
  URL" affordance is visual.
- Hotkey rebinding UI — `QKeySequenceEdit` per row is visual.
- Tag editing UI — right-click affordance + dialog is visual (backend
  is tested).
- Smart-playlist right-click "Create from this X" affordance — visual;
  the recipe factories that power it (`from_artist` etc.) shipped
  2026-05-20, so only the QMenu hookup remains.
- EQ preset *curve* tuning (the values themselves — math against
  source curves is fine; subjective adjustment isn't).
- Capturing screenshots for Flathub submission.
- Writing AUR PKGBUILD content (mechanical but needs maintainer
  judgement on optdepends + post-install hooks — bundle with august).

---

## ✅ Recently shipped (paper trail)

**2026-05-20 (PM) — context-menu pickup + doc audit**:

- "Create smart playlist" + "Start radio" right-click entries wired
  into the song / album / artist / genre menus; the dead context-menu
  installer layer removed (+13 tests). Done in the main session, not
  on an `auto/*` branch.
- `auto/smart-rule-schema-v2` — `date_added` / `last_played`
  smart-playlist rule fields, run unattended (+42 tests). Merged to
  `main` 2026-05-20 and verified against live Jellyfin / Subsonic
  servers; the editor integration + a Jellyfin full-library-fetch
  timeout were fixed during that verification.
- Full docs audit + rewrite (this doc set).

**2026-05-20 session — A1-A6 round, all merged to `main`**:

- `auto/cast-dlna-split` — 1188-LOC `cast/dlna.py` → 9-file subpackage.
- `auto/cast-manager-split` — 794-LOC `cast_manager.py` → package
  (`_ChromecastMixin` + `_AirplayMixin` + thin orchestrator).
- CastManager DLNA / Sonos / Snapcast discovery fan-out — hand-ported
  from `auto/cast-manager-discovery-fanout` (stale-base, see caveat
  above); landed as `_OtherProtocolsMixin` (+15 tests).
- `auto/radio-entry-points-parity` — album / artist / genre right-click
  "Start radio" (+8 tests).
- `auto/smart-playlist-backend-hardening` — recipe factories, schema
  additions (`is_favorite` / `starts_with` / `ends_with` / random
  sort), `schema_version` persistence (+71 tests).
- `auto/ruff-cleanup-2026-05-19` — 11 lint findings cleared.
- Suite: 1348 → 1442.

**2026-05-18 → 2026-05-19 — B-round + visualizer / smart playlists /
internet radio direct to `main`**:

- **B1 — Visualizer paint widget** → shipped, upgraded bars-to-Bezier
  on 2026-05-19. Audio tap via `pw-record --target=jellytoast` +
  `parec` fallback.
- Direct-to-main 2026-05-19: smart playlists end-to-end, internet
  radio UI, mini-player volume slot, EQ Settings UI, downloads
  tile/cover overlays.

**2026-05-18 session — 9 `auto/*` branches + 2 research docs**:
- offline-phase6-wifi-only, offline-phase6-downloads-ui,
  font-token-cleanup, smart-playlist-presets, notifications-backend,
  radio-feeder, crossfade-v1-backend, backend-package-tests,
  qss-parse-fix.
- Research: `visualizer_rendering.md`, `provider_abstraction_cleanup.md`.

**2026-05-17 (round 1-4 + Phase D + Phase E)** — A1-A26 shipped
through 11 merges. `git log --oneline | grep -E "^[0-9a-f]+ (A[0-9]+
|Merge auto/)"` is canonical for historical detail.
