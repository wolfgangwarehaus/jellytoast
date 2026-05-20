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

> Fire off A1 + A4 + A5 in parallel.

Each agent gets its own worktree so they don't conflict.

## Last updated

2026-05-19 — full session refresh after the audit. B1 (visualizer
widget) shipped on `main`; B2/B3 (cast splits) carried forward as
A1/A2. New entries A3 (CastManager wiring) + A4 (radio entry-point
parity) + A5 (ruff cleanup).

---

## 🟢 Ready to fire (in priority order)

### A1: cast/dlna.py split — **P2-tech-debt, S+M, sequence first**
Research doc landed: `docs/research/provider_abstraction_cleanup.md`.
1188 LOC monolith → subpackage. Concrete file shape:

```
modules/cast/dlna/
    __init__.py     # re-exports + module-level lazy globals
    _constants.py   # SSDP / MIME / codec constants (~60)
    _settings.py    # settings access helpers (~70)
    _models.py      # DlnaDevice / TrackMetadata / PushDecision (~70)
    didl.py         # build_didl_lite, _format_duration, _xml_* (~190)
    codec.py        # decide_push_format, decide_retry_after_error,
                    # is_available probe (~80)
    discovery.py    # dedupe_search_response, parse_host_from_location,
                    # _parse_udn_from_usn (~80)
    _loop.py        # _DlnaLoopThread (~95)
    controller.py   # DlnaController state machine (~380, deliberately
                    # whole — six-field state machine doesn't decompose)
```

**Critical**: `tests/test_cast_dlna.py` imports nine underscore-prefixed
helpers by full path (`from modules.cast.dlna import _xml_text`, etc.).
The test patches are the public contract — preserve every name via
re-export in `__init__.py`. Run `tests/test_cast_dlna.py` and
`tests/test_cast_gating.py` after the split; both must stay green.

Sequence **before** A2 (cast_manager imports from cast/dlna; doing
dlna first means cast_manager's import paths are stable when A2 fires).

Branch: `auto/cast-dlna-split`.

### A2: cast_manager.py split — **P2-tech-debt, M**
Same research doc. 794 LOC monolith → package. Concrete file shape:

```
modules/cast_manager/
    __init__.py     # re-exports + lazy-import globals
    _common.py      # shared dataclasses + helpers (~120)
    _chromecast.py  # ~340 lines
    _airplay.py     # ~210 lines
    _manager.py     # ~80 lines (orchestrator)
```

**Critical**: `tests/test_cast_gating.py` monkeypatches six
module-level lazy-import globals on `modules.cast_manager` — these
MUST be re-exported from the new package `__init__.py` exactly. The
test patches are the public contract.

Existing callers: `jellytoast.py:157`, `now_playing_bar.py:76`,
`cast_proxy.py:200`, `player_backend.py:667`. All use top-level
`CastManager` import — re-export covers them.

**Hold until** A1 lands (sequencing per research doc).

Branch: `auto/cast-manager-split`.

### A3: CastManager DLNA / Sonos / Snapcast discovery wiring — **P1, M**
`cast_manager.py:735-737` `discover_all()` currently only calls
`discover_chromecasts()` + `discover_airplay()`. The backend
discovery methods exist:
- `DlnaController.discover(timeout, on_device=None)` — blocking SSDP
- `discover_sonos(timeout=1.0)` + `discover_async()` (cast/sonos.py:148, 656)
- `discover_servers(timeout=...)` for Snapcast (cast/snapcast.py:214)

Tasks (all testable via mocked backends):
- Add `discover_dlna()`, `discover_sonos()`, `discover_snapcast()`
  methods to CastManager. Each respects A25 per-type toggles
  (`cast/dlna_enabled` etc.) and `cast/discovery_timing`
  (startup vs on_demand).
- `discover_all()` orchestrates all five backends.
- Per-protocol device-list emit hooks so CastDialog sections fill
  (A26 sections already exist; sniff their existing receiver-list
  emit pattern from `_on_chromecast_found`).
- Push methods route to existing backend `play(url, metadata)`
  functions per protocol. Verify each exists before re-implementing.

Unit tests:
- Each `discover_<type>` honours its per-type toggle.
- `discover_all` short-circuits disabled protocols (no I/O attempted).
- Discovery results emit through the existing device-list signal.

**Hold until** A1 + A2 land (otherwise the file you'd edit is the
about-to-split monolith).

Branch: `auto/cast-manager-discovery-fanout`.

### A4: Radio entry-point parity (album / artist / genre right-click) — **P1, S**
`install_song_context_menu` adds "Start radio from this song" for
single tracks (`ui_helpers.py:1583-1631`, seed_kind="track"). The
album / artist / genre variants are missing.

Tasks:
- New `install_album_context_menu(widget, item_provider)` —
  "Start album radio" action. Calls `provider.get_instant_mix(album_id)`,
  builds `QueueContext(seed_kind="album", seed_id=...)`, installs
  via existing queue manager radio path.
- New `install_artist_context_menu` — "Start artist radio" → uses
  `provider.get_similar_songs(artist_id)`.
- New `install_genre_context_menu` — "Start genre radio" → uses
  `provider.get_genre_radio(genre_name)`.
- Wire installers into existing tile/page surfaces. Grep for
  `install_song_context_menu` callsites; the album-tile /
  artist-tile / genre-tile equivalents likely already have
  context-menu hooks.

Unit tests:
- Each installer emits the right QueueContext on action trigger.
- Provider methods are called with the right id.
- RadioFeeder takes over for refill on each seed kind (it already
  honours `seed_kind` per the 2026-05-18 shipped backend).

Branch: `auto/radio-entry-points-parity`.

### A5: Ruff cleanup — **P3, XS**
`ruff check .` reports 11 errors (10 auto-fixable). Mostly unused
imports — e.g. `tests/test_visualizer_widget.py:19-20` imports
`QSize` + `QPainter` neither of which are referenced.

Tasks:
- `ruff check . --fix` then audit each remaining manual fix.
- Run the test suite — green required.
- One commit, mechanical.

Branch: `auto/ruff-cleanup-2026-05-19`.

### A6: Smart-playlist backend hardening — **P1, S/M** *(NEW, see below)*
Per the 2026-05-19 audit + user request: the smart-playlist
right-click "Create from this song" affordance is missing, and a
handful of backend gaps make the editor feel hollow vs Symfonium /
Feishin. Concrete unit-testable work below.

Tasks:
- **Create-from-this-X recipes** — new factories in
  `modules/smart_playlists/presets.py`:
  - `from_artist(artist_name)` → match artist equals, sort by play_count desc
  - `from_album(album_name)` → match album equals
  - `from_genre(genre_name)` → match genre equals, sort random
  - `from_year(year)` → year equals
  Each returns the rules dict the editor consumes.
- **`smart_rule_schema` operator gaps** — verify these are wired in
  the schema + evaluator + Jellyfin/Subsonic query builders:
  - `contains` for string fields (title, artist, album)
  - `in_last` for date fields (e.g. "added in last 30 days")
  - `is_favorite` boolean
  - `not_in_playlist(name)` exclusion
- **Sample / limit semantics** — verify `limit` + `sort=random`
  combinations work end-to-end; add tests for n>library_size,
  empty-result, and stable ordering with `sort != random`.
- **Auto-refresh on play** — when "Play" is hit on a smart playlist,
  re-evaluate rules so the snapshot is current. Today the editor
  evaluates only on save (verify; if so, push live re-eval into
  `SmartPlaylistsView` play handler).
- **Persistence migration safety** — add a defensive schema-version
  field on each persisted entry so future rule additions don't
  break existing playlists (currently `{name, rules, created_at}`
  with no version).

Unit tests:
- Each preset factory returns valid rules per `validate_rules`.
- New operators round-trip through provider query builders on both
  Jellyfin + Subsonic (use existing mock-provider patterns).
- Auto-refresh: change library state between save + play → play
  picks up new matches.
- Persistence: a v0 (versionless) entry loads cleanly; a v1 entry
  with extra metadata round-trips.

Then wire the right-click affordance (visual; flag separately as a
non-autonomous follow-up so august adds the QMenu entries himself).
The backend bits above are pure logic — autonomous-eligible.

Branch: `auto/smart-playlist-backend-hardening`.

---

## 🟡 Candidates needing research first

(Empty as of 2026-05-19. Add new candidates here as they surface.)

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
- Crossfade Settings UI exposure — the QSettings keys + backend
  honour are tested; the checkbox + slider visuals are not.
- Multi-server hostnames login UI — backend tested; "+ Add alternate
  URL" affordance is visual.
- Hotkey rebinding UI — `QKeySequenceEdit` per row is visual.
- Tag editing UI — right-click affordance + dialog is visual (backend
  is tested).
- Smart-playlist right-click "Create from this song" affordance —
  visual; the preset recipes that power it (A6 above) are autonomous.
- EQ preset *curve* tuning (the values themselves — math against
  source curves is fine; subjective adjustment isn't).
- Capturing screenshots for Flathub submission.
- Writing AUR PKGBUILD content (mechanical but needs maintainer
  judgement on optdepends + post-install hooks — bundle with august).

---

## ✅ Recently shipped (paper trail)

**2026-05-18 → 2026-05-19 — full B-round merged + visualizer / smart
playlists / internet radio landed direct to `main`**:

*B-round (2026-05-18 unblocked, all shipped via main commits 7e0bed0 +
468c599):*
- **B1 — Visualizer paint widget** → shipped, then upgraded
  bars-to-Bezier on 2026-05-19 PM. Audio tap via `pw-record
  --target=jellytoast` + `parec` fallback.

*Direct-to-main 2026-05-19 work that bypassed the `auto/*` queue
(too tightly coupled to live verification):*
- Smart playlists end-to-end (editor + view + 4 presets +
  persistence).
- Internet radio UI (Radio tab, station rows, popular-picker,
  ICY-art lookup, LIVE indicator).
- Mini-player volume right-edge slot.
- EQ Settings UI polish + non-modal Settings dialog.
- Downloads tile/cover overlays (downloaded indicator, progress
  ring, BL/BR corner buttons).

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
- `auto/qss-parse-fix` (+1 regression test)

*Research:*
- `docs/research/visualizer_rendering.md` — unblocked B1
- `docs/research/provider_abstraction_cleanup.md` — unblocks A1 + A2

**2026-05-17 (round 1-4 + Phase D + Phase E)** — A1-A26 shipped
through 11 merges. `git log --oneline | grep -E "^[0-9a-f]+ (A[0-9]+
|Merge auto/)"` is canonical for historical detail.
