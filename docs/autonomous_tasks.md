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

> Fire off A1 + A3 + A5 in parallel.

Each agent gets its own worktree so they don't conflict.

## Last updated

2026-05-15 (evening) — A1-A5 merged to main (275 tests passing).
A14, A20, A11 shipped on auto/* branches awaiting review. A6 newly
unblocked now that A3 is on main.

---

## 🟢 Ready now

Each task lists its priority (`docs/TODO.md` tier) and rough size.

### A1-A5 — **MERGED TO MAIN** (2026-05-15)
A1 search "air" fix, A2 artist page offline, A3 connectivity tests,
A4 scrobble tests, A5 migration tests — all on main. Test suite
275 passing, 1 skipped.

### A14 — Server-side scrobble badge — **SHIPPED**, `auto/scrobble-badge`
P2, S. ✓ Done. New `_ScrobbleBadge` QLabel surfaced in
now-playing bar + page; `PlayerBus.scrobble_status_changed` added;
5 unit tests. Awaiting merge. No expected conflicts.

### A20 — SPEC.md Phase 5 update — **SHIPPED**, `auto/spec-phase5`
P0, S. ✓ Done. §5 (Offline / downloads) + §6 (Library / browse)
updated with connectivity tracker, chip, filters, scrobble flush.
Awaiting merge.

### A11 — Sleep timer scaffold — **SHIPPED**, `auto/sleep-timer-scaffold`
P2, S. ✓ Done. 3 PlayerBus signals + start/cancel + 3 fire modes
(`pause`, `end_of_track`, `fade_stop` — fade degrades to immediate
pause with TODO). 10 new tests. Awaiting merge.

---

## 🟡 Ready, not yet queued

### A6 — Fix `set_offline_mode` non-bool coercion — **NEWLY UNBLOCKED**
P3, S. A3 has landed on main, so the dropped test
(`test_set_offline_mode_coerces_truthy`) and the one-liner fix can
ship together. In `modules/offline/connectivity.py`:

```python
def set_offline_mode(enabled: bool) -> None:
    enabled = bool(enabled)   # ← add this
    ...
```

Also restore the test in `tests/test_offline_connectivity.py`.

### A7 — EQ scaffold (no UI) — **P1, S**
From `docs/research/eq_dsp.md`. The Qt-free pieces of EQ.
- Add `playback/eq_enabled`, `playback/eq_preset`,
  `playback/eq_bands` properties to `modules/settings.py`
- Add `PlayerBus.eq_changed = Signal(bool, list)` to
  `modules/player_state.py`
- Add `Player.apply_eq(enabled, bands)` to
  `modules/player_backend.py` — writes mpv `af=anequalizer=...` string
- Add 8 built-in preset dicts in a new module `modules/eq_presets.py`
- Add unit tests for the band-string formatter (no Qt needed)
- UI lands in a follow-up branch with august's eyes

### A8 — Internet radio backend (Subsonic only) — **P1, M**
From `docs/research/radio_and_seeded_queues.md`. Logic-only slice.
- Wire `getInternetRadioStations` / `createInternetRadioStation` /
  `updateInternetRadioStation` / `deleteInternetRadioStation` into
  `modules/providers/subsonic.py` + `base.py`
- Add `QueueContext.INTERNET_RADIO` to `modules/queue_manager.py`
- Add `PlayerBus.radio_title_changed = Signal(str)`
- Wire mpv `observe_property('metadata/by-key/icy-title', ...)` to
  the new signal in `modules/player_backend.py`
- Unit tests for provider methods (mock requests)
- Jellyfin gets the local-only path in a follow-up
- UI is the next branch with august

### A9 — Seeded radio provider methods — **P1, S-M**
From `docs/research/radio_and_seeded_queues.md`.
- Add `get_similar_songs`, `get_instant_mix`, `get_genre_radio` to
  `modules/providers/base.py`
- Subsonic: alias `get_instant_mix` → `getSimilarSongs2`
- Jellyfin: implement both natively
- Add `seed_kind` and `radio_played_ids` fields to `QueueContext`
- Unit tests for both providers (mock the API)

### A10 — Smart playlist `query_items` provider stub — **P1, S**
From `docs/research/smart_playlists.md`.
- Add `query_items(rules: dict) -> List[items]` to
  `modules/providers/base.py` with NotImplementedError
- Subsonic stub: translates a small initial rule subset to
  `getAlbumList2` / `getSongsByGenre` chains
- Jellyfin stub: translates rules to `/Items` filter params
- Rule schema documented in
  `modules/providers/smart_rule_schema.py`
- Unit tests with stubbed provider responses
- UI + storage in a follow-up branch

### A12 — Hotkey registry refactor — **P2, M**
From `docs/research/parity_small_items.md`. The precondition for
rebinding.
- Extract the inlined shortcuts from `jellytoast.py:549-570` into a
  new `modules/hotkeys.py` registry
- Each shortcut: `{action_id, default_seq, label, callable}`
- Load custom bindings from `hotkeys/<action_id>` QSettings
- Settings → Hotkeys page becomes a model-driven list
- Tests for the registry's load/save/reset
- `QKeySequenceEdit` capture UI in a follow-up

### A13 — Multi-server hostname extension — **P2, M**
From `docs/research/parity_small_items.md`.
- Settings: `server/hostnames` JSON list (label + URL + priority)
- Connectivity tracker: when reachable→unreachable, try alternates
  before emitting `connectivity_changed(False)`
- New `PlayerBus.host_switched = Signal(str)` (label)
- Provider `with_url(url)` swap mechanism to retry against alternate
- Unit tests for the fallback walk
- Login UI "+ Add alternate URL" in a follow-up

### A15 — Cover-art offline behavior — **P2, S**
From earlier session note. When `offline.is_offline_mode()`, cover-
load helpers (`modules.ui_helpers.load_image_async`) should skip the
network attempt and serve from disk cache only.
- Add the early-return path in `load_image_async`
- Unit test the gate (mock QNAM)
- No UI surface needed

### A16 — Dead-code sweep
P3, S. Walk the repo for leftovers from removed features
(QWebEngineView references, old singleton paths, commented-out
code). Branch `auto/dead-code-sweep`; the diff is the verification.

### A17 — TODO-comment cleanup
P3, S. Grep for `TODO` / `FIXME` / `XXX` in modules. Each one
either gets filed in `docs/TODO.md` (deletes the comment) or
deleted as stale.

### A18 — Lint pass
P3, S. Run `ruff check modules/` (or whatever the project uses; look
at `pyproject.toml`). Fix the easy ones; surface the non-trivial
ones.

### A19 — Pre-commit hook scaffold
P3, S. Add a `.pre-commit-config.yaml` with `ruff check` + `ruff
format`. Update `pyproject.toml` if needed for `ruff` config. Don't
install hooks globally — leave that for august.

### A21 — Phase 6 offline scaffold
P1, M. From `docs/research/` analysis of Phase 6.
- Implement `pause()` / `resume()` / `retry_failed()` in
  `modules/offline/manager.py` (currently `NotImplementedError`)
- Add `nodes.state = "stale"` flag handling
- Wire `offline.snapshot.resync(item_id)`
- Add a "Repair downloads" path
- Unit tests for each new method
- UI lands in a follow-up

---

## 🔴 NOT autonomous — needs august

For reference, so I don't accidentally try:

- Anything where the success criterion is "looks right" (paint,
  layout, animation).
- Real-world disconnect testing.
- Real-world scrobble end-to-end.
- Cast / AirPlay device behaviour.
- Anything involving signing in to a new server.
- Anything that affects shared state outside the repo (pushing PRs,
  posting issues, modifying CI, etc.).
- Visualizer rendering quality.
- Crossfade audio quality + curve subjective tuning.
- EQ preset *curve* tuning (the values themselves — math against
  source curves is fine; subjective adjustment isn't).

---

## Recommended next autonomous batch

After merging A14 + A20 + A11 (the three shipped above), this is the
recommended next fan-out (each parallel-safe in its own worktree):

1. **A6** — `set_offline_mode` bool coercion + restored test (S, trivial)
2. **A7** — EQ scaffold (S, sets up the bigger feature)
3. **A8** — internet radio backend, Subsonic only (M)
4. **A15** — cover-art offline behavior (S, small win)
5. **A9** — seeded radio provider methods (S-M)

That gives 5 more branches ready for review, ~1 day of agent work
overlapping in parallel.
