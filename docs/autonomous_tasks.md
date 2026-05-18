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

2026-05-18 — full session refresh. Round 1 (A1-A6) all shipped to
`auto/*` branches; round 2 (B1-B3) unlocked by research docs landing.

---

## 🟢 Ready to fire (in priority order)

### B1: Visualizer paint widget — **P1, M, NEW (unblocked 2026-05-18)**
Research doc landed: `docs/research/visualizer_rendering.md`. Spec
is concrete enough to implement verbatim without subjective tuning:

- 32 bars (matches `_BAND_COUNT = 32` in the FFT 1:1).
- Grounded vertical rectangles, square tops, 2px gap, flat fill.
- Asymmetric exponential smoothing (`attack_α=0.35`, `release_α=0.12`).
- Single widget-spanning `QLinearGradient` ACCENT_DEEP → ACCENT;
  live-applies via `theme_changed → update()`.
- Idle: decay to 0.02 baseline floor (≥2px); never hold last frame.
- Cast active: static centered cast-icon + "Casting to <device>".

Slice plan: new `modules/visualizer_widget.py` + tri-state refactor
of `now_playing_page.py` (`_show_lyrics: bool` → `np_left_pane_mode`
str). ~250-350 LOC. Spec §11 defines the testable surface.

**Hold until** `auto/crossfade-v1-backend` merges (no file overlap,
but `player_state.py` may have signal-block conflicts).

Branch: `auto/visualizer-widget`.

### B2: cast_manager.py split — **P1-tech-debt, M, NEW**
Research doc landed: `docs/research/provider_abstraction_cleanup.md`.
Concrete file shape:

```
modules/cast_manager/
    __init__.py     # re-exports + lazy-import globals
    _common.py
    _chromecast.py  # ~340 lines
    _airplay.py     # ~210 lines
    _manager.py     # ~80 lines
```

**Critical**: existing test patches (`tests/test_cast_gating.py`)
monkeypatch six module-level lazy-import globals on
`modules.cast_manager` — these MUST be re-exported from the new
package `__init__.py` exactly. The test patches are the public
contract.

**Hold until** no in-review branch touches cast_manager (none
currently do — safe to fire after merges land).

Branch: `auto/cast-manager-split`.

### B3: cast/dlna.py split — **P1-tech-debt, S+M, NEW**
Same research doc. Concrete file shape:

```
modules/cast/dlna/
    __init__.py
    _constants.py
    _settings.py
    _models.py
    didl.py         # ~190 lines
    codec.py        # ~80 lines
    discovery.py    # ~80 lines
    _loop.py        # ~95 lines
    controller.py   # ~380 lines (deliberately kept whole — six-field
                    # state machine doesn't decompose cleanly)
```

`tests/test_cast_dlna.py` imports nine underscore-prefixed helpers by
full path — same test-patches-are-contract caveat as B2.

Per the research doc: **sequence DLNA before cast_manager** because
cast_manager imports from cast/dlna; doing dlna first means
cast_manager's import paths in B2 are stable.

Branch: `auto/cast-dlna-split`.

---

## 🟡 Candidates needing research first

(Empty as of 2026-05-18 — both prior research candidates landed docs
this session. Add new candidates here as they surface.)

---

## 🔴 NOT autonomous — needs august

For reference, so I don't accidentally try:

- Anything where the success criterion is "looks right" (paint,
  layout, animation) — except spec'd visualizer paint per B1.
- Real-world disconnect testing.
- Real-world scrobble end-to-end.
- Cast / AirPlay device behaviour (still no DLNA / Sonos / Snapcast
  hardware available; backends shipped untested).
- Anything involving signing in to a new server.
- Anything that affects shared state outside the repo (pushing PRs
  to GitHub on august's behalf, posting issues, modifying CI, etc.).
- Visualizer rendering quality (spec'd; ship-time match-to-spec is
  testable, but post-merge "does it look right" is august's).
- Crossfade audio quality + curve subjective tuning (plumbing landed
  on `auto/crossfade-v1-backend` with linear ramps + explicit hook
  for august to swap in tuned curves).
- EQ preset *curve* tuning (the values themselves — math against
  source curves is fine; subjective adjustment isn't).
- Capturing screenshots for Flathub submission.
- Writing AUR PKGBUILD content (mechanical but needs maintainer
  judgement on optdepends + post-install hooks — bundle with august).

---

## ✅ Recently shipped (paper trail)

**2026-05-18 session — 11 `auto/*` branches + 2 research docs**:

*Round 1 — A1 through A6 + the QSS audit + backend tests:*
- `auto/offline-phase6-wifi-only` (+14 tests)
- `auto/offline-phase6-downloads-ui` (+8 tests)
- `auto/font-token-cleanup` (mechanical sweep, no new tests)
- `auto/smart-playlist-presets` (+16 tests)
- `auto/notifications-backend` (+9 tests)
- `auto/radio-feeder` (+14 tests)
- `auto/crossfade-v1-backend` (+20 tests)
- `auto/backend-package-tests` (+39 tests)
- `auto/qss-parse-fix` (+1 regression test; no offender found
  statically — left audit harness for future debugging)

*Research:*
- `docs/research/visualizer_rendering.md` — unblocks B1
- `docs/research/provider_abstraction_cleanup.md` — unblocks B2 + B3

Net tests across all `auto/*` branches: **+121** (1057 → ~1178 if
all merge clean).

**2026-05-17 (round 1-4 + Phase D + Phase E)** — A1-A26 shipped
through 11 merges. `git log --oneline | grep -E "^[0-9a-f]+ (A[0-9]+
|Merge auto/)"` is canonical for historical detail.
