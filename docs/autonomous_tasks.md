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

2026-05-15 (overnight) — Four autonomous rounds done today, 16
branches shipped and merged. Test count: 224 → 475 (+251 tests over
the day). Main is at parity with all queued work except the deferred
UI follow-ups.

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

### Round 4 (5 branches): A9, A10, A12, A15, A21
A9 seeded radio provider methods (`get_similar_songs` /
`get_instant_mix` / `get_genre_radio` on both providers,
`QueueContext.seed_kind` + `radio_played_ids`, 39 tests).
A10 smart playlist `query_items` provider stub + rule schema doc +
46 tests.
A12 hotkey registry refactor — `modules/hotkeys.py` decoupled from
main window, 5 shortcuts extracted, 12 tests.
A15 cover-art offline behavior — skip network in offline mode,
6 tests.
A21 Phase 6 offline scaffold — `pause`/`resume`/`retry_failed`/
`mark_stale`/`resync`/`repair` across `offline/manager.py`,
`offline/index.py`, `offline/snapshot.py`, `offline/__init__.py`;
52 tests.

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

### A13 — Multi-server hostname extension — **P2, M**
From `docs/research/parity_small_items.md`.
- Settings: `server/hostnames` JSON list (label + URL + priority)
- Connectivity tracker: when reachable→unreachable, try alternates
  before emitting `connectivity_changed(False)`
- New `PlayerBus.host_switched = Signal(str)` (label)
- Provider `with_url(url)` swap mechanism to retry against alternate
- Unit tests for the fallback walk
- Login UI "+ Add alternate URL" in a follow-up

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

### A22 — DLNA / UPnP cast backend — **P2, M**
From `docs/research/casting_dlna.md`. Logic-only slice; UI lands with august.
- Add `async-upnp-client>=0.47.0` as a dep
- New module `modules/cast/dlna.py` running a private asyncio loop in a worker thread (no app-wide qasync rewrite)
- SSDP discovery + AVTransport push + DIDL-Lite builder
- Route every push through the existing `modules/cast_proxy.py`
- 714-fallback transcode strategy (server-side bitrate fallback when the renderer refuses a codec)
- 1-second polling state tracker (GENA event subscriptions deferred)
- Reuse existing `cast_*` PlayerBus signals — no new signal family needed
- Add `cast/dlna_enabled`, `cast/dlna_user_agent_overrides` (power-user QSettings, no UI in v1) settings
- ~40-60 unit tests against a fake-renderer SOAP stub

### A23 — Sonos cast backend — **P2, M**
From `docs/research/casting_sonos.md`. Logic-only slice; UI lands with august.
- Add `soco>=0.31` as a dep
- New module `modules/cast/sonos.py`
- SSDP discovery via `ZonePlayer:1` ST string + topology query
- `play_uri` push through `modules/cast_proxy.py`
- Group / ungroup zone helpers (`coordinator` aware)
- Sync-events watchdog (soco event subscriptions, queued back to main thread)
- Reuse existing `cast_*` PlayerBus signals
- Add `cast/sonos_enabled`, `cast/sonos_preferred_zone`, `cast/sonos_group_with_master` settings
- Mocked-SOAP unit tests (~30+ tests)
- **No hardware to validate against** — ship "should-work, untested" per [[user_hardware]]; don't add device-specific quirk workarounds based on forum reports

### A24 — Snapcast control surface (Option B) — **P2, M**
From `docs/research/casting_snapcast.md`. v1 ships control only; audio routing (Option A) is v1.5 with august's eyes.
- Add `snapcast>=2.3.8` as a dep (asyncio-native, zero transitive deps)
- New module `modules/cast/snapcast.py`
- Avahi/mDNS discovery via `zeroconf` (already a dep) on `_snapcast-jsonrpc._tcp.local`
- JSON-RPC control: list clients/streams/groups, set per-client volume/mute, group/ungroup, switch source stream
- Server-event subscriptions (client connect/disconnect, volume changes, stream metadata)
- New `PlayerBus.snapcast_*` signal family (kept separate from `cast_*` per research doc rationale — Snapcast isn't a "push URL" model)
- Add `cast/snapcast_enabled`, `cast/snapcast_server_host`, `cast/snapcast_default_group` settings
- Unit tests against mocked JSON-RPC server
- Audio routing (Option A — mpv → snapserver pipe) deferred to a separate UI-with-august task

### A25 — Per-type cast toggle + discovery-timing settings — **P1, S**
Prep for A22-A24. Wire the new settings keys + add the discovery-timing radio in Settings.
- Add `cast/discovery_timing` setting ("startup" | "on_demand", default "on_demand")
- Add `cast/chromecast_enabled`, `cast/airplay_enabled` (default True) — existing protocols get retroactive toggle support
- Existing cast manager honors `cast/discovery_timing` — skip boot scan when "on_demand"
- Settings → Casting page: per-type toggle rows + the timing radio
- Unit tests for the gate logic

### A26 — Unified cast menu with collapsible sections — **P1, M**
Prep for A22-A24. Refactor the existing cast dialog from a flat device list into collapsible sections by cast type (Chromecast, AirPlay, future DLNA/Sonos/Snapcast).
- Section state persisted in QSettings (`cast_dialog/section_<type>_collapsed`)
- Empty sections collapse by default; sections with discovered devices expand
- Reuse existing cast popup layout — don't redesign the chrome
- Visual review needed; this is borderline "with august" but the structural refactor is mostly autonomous

---

## Recommended next autonomous batch

After today's four rounds, the natural next fan-out is cast-protocol
research-to-build. A25 + A26 are the prep work that A22-A24 need
landed first:

1. **A25** — per-type cast toggle + discovery-timing settings (S)
2. **A26** — unified collapsible cast menu (M, then A22-A24 plug into it)
3. **A22** — DLNA backend (M)
4. **A23** — Sonos backend (M)
5. **A24** — Snapcast control surface (M)

After casting: A13 (multi-server hostnames), then code-hygiene
sweeps (A16-A19).

Then the path forward is UI follow-ups (august's eyes required):
EQ settings page, smart-playlist rule builder, internet radio +
seeded radio surfaces, sleep timer dropdown, hotkey settings page,
downloads pause/resume buttons, Repair-downloads entry, Snapcast
audio routing (Option A).
