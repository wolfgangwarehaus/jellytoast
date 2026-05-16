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

2026-05-15 (late evening) — A1-A5, A14, A20, A11, A6, A7, A8 all
merged to main (320 tests passing, 1 skipped). Two autonomous rounds
done today.

---

## 🟢 Recently merged

### Round 1 (5 branches): A1-A5
A1 search "air" fix, A2 artist page offline, A3 connectivity tests,
A4 scrobble tests, A5 migration tests.

### Round 2 (3 branches): A14, A20, A11
A14 server-side scrobble badge, A20 SPEC.md Phase 5 update,
A11 sleep timer scaffold.

### Round 3 (3 branches): A6, A7, A8
A6 `set_offline_mode` bool coercion + coerce test (also dropped the
obsolete `note_outcome` stub test). A7 EQ scaffold — settings,
`PlayerBus.eq_changed`, `apply_eq()`, 8 presets, 17 tests. A8
internet radio backend (Subsonic only) — 4 CRUD methods,
`QueueKind.INTERNET_RADIO`, `PlayerBus.radio_title_changed`, icy-title
observer, 12 tests.

---

## 🟡 Ready, not yet queued

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

After three rounds of fan-out today, the natural next batch builds on
what just landed (EQ, internet radio, sleep timer) without yet
needing the UI follow-ups — those want august's eyes. Each is
parallel-safe in its own worktree:

1. **A9** — seeded radio provider methods (S-M, complements A8)
2. **A10** — smart playlist `query_items` provider stub (S)
3. **A15** — cover-art offline behavior (S, small win)
4. **A12** — hotkey registry refactor (M, sets up the bigger feature)
5. **A21** — Phase 6 offline scaffold — `pause`/`resume`/`retry_failed` (M)

A13 (multi-server hostname) and A16-A19 (sweeps, lint, pre-commit)
are also ready any time but lower priority right now.
