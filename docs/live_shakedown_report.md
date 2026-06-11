# jellytoast — Live Shakedown Report

Rigorous end-to-end exercise of the **running** app, driven via the dev test
bridge (`JT_TEST_BRIDGE=1` → `dev/jt_ctl.py`), `ydotool` real input, MPRIS2
D-Bus, and `spectacle` screenshots. Started 2026-06-02 on branch
`test/live-harness`.

## How this run is driven

- **Bridge (deterministic control + observation):** `modules/test_bridge.py`
  opens a per-user `QLocalServer` that evals Python on the GUI thread. Client:
  `python dev/jt_ctl.py {ping|eval|exec} "<code>"`. Namespace: `app`, `win`,
  `bus`, `mini`, `settings`, `cast`, `qm`, `mpv`, `get_now_playing()`,
  `get_provider()`, `get_settings()`.
- **Real input (`ydotool`):** clicks/scroll/drag where input fidelity matters
  (hit-testing, scroll momentum, drag-reorder, resize).
- **MPRIS2:** `qdbus6 org.mpris.MediaPlayer2.jellytoast …` for OS media-key /
  KDE-media-widget integration.
- **Visual:** `spectacle -f -b -n -o <png>` full-composite screenshots.

Scope this session: local audio **muted** (`volume=0`); casts to **Chromecast /
group** + **DLNA / Sonos** at low device volume; AirPlay/Snapcast deferred.

## Summary (session 1 — 2026-06-02)

**Scorecard:** 2 real bugs found+fixed+regression-locked (1×P1, 1×P2); 4 false-alarms cleared by verification; core subsystems verified working.

| Category | Result |
|---|---|
| 1 Lifecycle/auth | ◑ boot OK; login/logout/resume pending (disruptive) |
| 2 Navigation | ✅ all surfaces; **F1 (P1) fixed**, F2′ (P2) fixed, N1 noted |
| 3 Playback (muted) | ✅ all OK (play/pause/seek/next/prev/repeat/shuffle/mute) |
| 4 Surfaces | ✅ mini player, lyrics↔visualizer, favorite (restored) |
| 5 Server/library swap | ✅ lib1↔lib2↔both (297/58/355 union); content scopes correctly |
| 6 Casting | ✅ **Chromecast (Sunroom) live+audible**; DLNA/Sonos pending hardware |
| 7 Offline/downloads | ✅ download → `file://` blob playback → downloads view → remove |
| 8 Scroll/loading | ✅ grids load; QTest wheel → SmoothScrollFilter scrolls (0→360) |
| 9 Click/hit-testing | ✅ QTest clicks (button + grid tile) hit-test + route correctly; window resize/drag via compositor pending (ydotool) |
| 10 Theming | ✅ live accent + dark↔light via `theme_changed`, no restart/crash (reverted) |
| 11 OS integration | ✅ **MPRIS bidirectional**; tray/notifications pending |
| 12 Stress/chaos | ✅ no crash, no doubling, **no leak (plateau)** |

**Both fixes confirmed LIVE on the relaunched instance (PID 33352):** F1 cold songs load → **3,500 rows**; F2′ ABBA header → **"1 album"**.

**Fixed this session (regression-locked, suite 2432 green):**
- **F1 (P0/P1):** `songs_view` cold-load self-supersede — `_clear()` bumped `_load_gen` after the cold fetch captured it → result always dropped, view permanently blank on a cache-cold library. Fixed (gen re-sync) + test + **live-verified 0→50 rows**.
- **F2′ (P2):** `artist_page` header album-count race — `_on_meta_loaded` clobbered `_on_albums_loaded`'s count. Fixed (shared `_rebuild_info()`) + test.

**Test infrastructure built:** `modules/test_bridge.py` (env-gated GUI-thread eval socket) + `dev/jt_ctl.py` + `dev/jt_drive.py`; `faulthandler` + `QTest` in the bridge namespace.

## Severity legend

- **P0** crash / data loss / silent wrong behavior
- **P1** broken feature / visibly wrong
- **P2** minor / cosmetic / edge-case
- **OK** verified working

---

## Findings

| # | Cat | Severity | Surface | Symptom | Status |
|---|-----|----------|---------|---------|--------|
| **F1** | 2/3 | **P0/P1** | Songs view | **Cold (uncached) load never populates** — empty "No songs yet" with a full library. `load_songs` captured `gen`, then its own `_clear()` bumped `_load_gen`, so the cold fetch was born stale → `_on_cold_fetch` dropped its own result; `_page_fetch_in_flight` stuck `True`; disk cache never written, so it never self-heals. | ✅ **FIXED** (`songs_view.py` gen re-sync after `_clear()`) + **regression test** (fails pre-fix) + **live-verified** (0→50 real rows) |
| **F2′** | 2 | P2 | Artist page | Album-count in the header info line is **racy** — `_on_meta_loaded` overwrites the count `_on_albums_loaded` set; whichever async fetch resolves last wins. ABBA shows blank info instead of "1 album". | 🔎 Confirmed, fix pending |
| N1 | 2 | P2 (testability) | Radio / Downloads views | `RadioView` / `DownloadsLibraryView` top-level widgets have **no `objectName`** (only their rows do) → can't be targeted by `findChild`/QSS by name. | 🔎 Noted |
| — | 2 | — | Top-bar tab label | *Investigated, NOT a bug:* label looked out of sync when driven via the private `_show_native_music_grid`, but the real `tab_requested(int,label)` path syncs the "Albums→Artists" label + grid correctly. | ✅ Verified OK |
| — | 2 | — | Artist page "No albums" | *Investigated, NOT a bug:* was a stale-`_artist_id` race in the **test harness's** rapid navigation; the page's own guard correctly drops superseded loads. Fresh isolated loads render every artist's albums. | ✅ Verified OK |

---

## Category log

### 1. Lifecycle & auth
- Authed on Subsonic/Navidrome (`192.168.50.100:4533`, user `avtips`), boot OK, all post-show singletons present (`mpv_ctrl`, `cast_manager`, `queue_mgr`, `mini_player`). Local audio muted at `volume=0` for the session. _Login/logout/re-login + session-resume still to exercise._

### 2. Navigation crawl — **mostly OK; 1×P1, 1×P2, 1×testability**
Visited every surface (lazy-built, stack grew 2→13): home, albums(297, no doubling), artists(A-Z), playlists, songs, genres, suggestions, smart-playlists, radio, downloads, search, artist-page, album-browse(→npPage), now-playing (live + preview). **No crashes, no Qt warnings, no tracebacks** across the whole sweep.
- Albums grid: 297 albums, **no [A,A,B,B] adjacent doubling** (the old doubling bug stays fixed); covers + layout clean.
- **F1 (P1):** songs view empty on cold load — see Findings. FIXED + locked + live-verified.
- **F2′ (P2):** artist header album-count race — see Findings.
- **N1:** radio/downloads views lack top-level objectNames.
- Tab-label sync + artist-page album loading both investigated and **cleared** (test-harness artifacts, not app bugs).

### 3. Playback (muted) — **all OK**
Drove the real mpv pipeline silently (vol=0) on Adele "21" (11-track queue):
- **Play** → "Rolling in the Deep", position **advances** (2170→3493ms over 1.3s = mpv genuinely decoding).
- **Pause** freezes position (Δ0); **resume** unfreezes; **seek**(90s)→90973ms.
- **Next/prev** navigate correctly (track id changes, prev returns).
- **Repeat** off/all/one round-trip; **shuffle** is a true permutation (order changed, set identical — no track loss); **mute** toggles.
- No errors / Qt warnings / tracebacks.

### 4. Playback surfaces (bar / page / mini / lyrics↔visualizer) — **all OK**
- **Mini player**: show → compact (384×96) → `toggle_mode` → expanded (384×480) → back → hidden. Clean.
- **Now-playing page**: lyrics↔visualizer toggle works; **synced lyrics** render for "Rolling in the Deep"; right-pane album queue shows all 11 tracks with the current one highlighted; `Streaming · EQ · Crossfade · FLAC · 2059 kbps` badges present. Visualizer pane renders flat **because audio is muted** (no spectrum signal) — expected, not a bug.
- **Favorite heart**: `false → toggle → true → toggle → false` — round-trips correctly; original state **restored** (no stray favorite left on the server).
- _Tray menu + main↔mini handoff still to exercise._

### 5. Server & library swaps — **library swap OK**
Two Navidrome libraries ("Music Library" id1=297 albums, "Discovery" id2=58). Driven via the real `top_bar.libraries_selected` signal:
- lib1 → grid **297**; → lib2 → **58** (`parent='2'`); → both → **355** (297+58 union, `parent=''`, selection=`[]`="all"); → back to lib1 → **297**. Content scopes correctly, no errors.
- _Provider swap (Subsonic↔Jellyfin) not exercised — disruptive (would sign out Navidrome); needs a Jellyfin server._

### 6. Casting (Chromecast/group + DLNA/Sonos, low vol)
- **Discovery OK** (`discovery_timing=startup`, `stream_routing=auto`, all 5 protocols enabled): 11 devices found, **stable across 2/4/6/8s snapshots** (no flakiness). 7 Chromecast (4 cast + audio) + 3 groups + 1 AirPlay (lg tv).
- **N2 RESOLVED — not a bug:** `.248` is a **Philips Hue bridge** (`SERVER: Hue/1.0 UPnP/1.0 IpBridge`, advertises `upnp:rootdevice`/`basic:1`, not `MediaRenderer`). The app **correctly** drops it. Good behavior.
- **Chromecast cast (Sunroom speaker) — ✅ VERIFIED LIVE + AUDIBLE:** connect (instant), volume control (set 15%→12%, confirmed via `chromecast_get_volume`), pause/resume on device, seek (→46s), disconnect clears `active_cast`, **device master volume restored** (0.29). Transport routes correctly; cover + FLAC stream pushed (`Casting: Rolling in the Deep`).
- *Investigated, NOT a bug:* `bus.cast_active` stayed `True` after a **direct** `cast.stop_cast()` — because that flag is mirrored from the `cast_stopped` signal, which the UI disconnect paths (`cast_dialog.py:967`, `jellytoast.py:2970`) emit, not `stop_cast()` itself. Emitting `cast_stopped` clears it. (Minor latent note: any non-UI caller of `stop_cast()` would leak the flag — but no such path exists.)
- **DLNA cast (LG TV) — ✅ VERIFIED LIVE (first hardware verification of DLNA):** the `lg tv` (192.168.50.144) came up as a UPnP `MediaRenderer` (LG WebOS DMRplus); cast connected (`type=dlna`), **playback position advanced** (5000→8000ms via `GetPositionInfo`), **volume via RenderingControl** (`GetVolumeResponse CurrentVolume=12`), pause/resume + seek (→41s) via AVTransport, clean disconnect (AVTransport `Stop`). Closes the long-standing "DLNA unverified on hardware" gap. (Note: same LG TV is a *broken* AirPlay-2 receiver but a *working* DLNA renderer.)
- **Sonos / Snapcast:** still not discovered (no SonosZone / Snapcast server on the network). Untested-this-session.

### 7. Offline / downloads — **OK**
- `offline.download(track)` → completes, blob on disk (`…/downloads/50/50ee….flac`), `is_downloaded=True`.
- Offline mode ON → playback of the blob: `is_local=True`, `stream_url` `file://…`, position advances (1498ms) — offline playback works.
- Downloads library view populates correctly (`_rows`=2, both IDs, `empty` hidden). _Note (not a bug): this view uses per-row `_DownloadRow` widgets, not the model/view pattern the big grids use — fine for a small list._
- `offline.remove(id)` cleans up. Test track removed; august's pre-existing download left untouched.

### 8. Scrolling & loading — **OK**
- All grids/lists load (albums 297, songs 3500, etc.). QTest `QWheelEvent` on the albums grid → `SmoothScrollFilter` scrolled the bar 0→360.
- _Real wheel-through-compositor (ydotool) not run — moves august's cursor; QTest in-process is the more reliable path per the research._

### 9. Click / hit-testing / resize / drag — **clicks OK; resize/drag deferred**
- QTest real clicks: top-bar search button → `searchView`; library-grid tile #0 → hit-tested → album browse (`npPage` preview). Hit-testing + handlers fire correctly.
- _Window resize, top-bar drag-move, mini-player drag/resize: deferred — these need real compositor input (ydotool, moves the cursor) and per the research mini-player `QWidget.move()` is a Wayland no-op anyway. Best done with august watching._

### 10. Theming — **OK (live-apply)**
- Accent `#967de1`→`#22cc88` via `settings.accent_color` + `ui_helpers.refresh_theme()` + `theme_changed.emit()` → current-track/queue recolored green (verified by screenshot), `ui_helpers.ACCENT` updated.
- `theme_mode` `frosted_dark`→`frosted_light` → whole UI flipped to light, **no restart, no crash** (the `_reapply_accent` contract holds).
- Both **reverted** to originals (purple / frosted_dark).

### 11. OS integration (MPRIS / tray / notifications) — **MPRIS OK**
Driven externally via `qdbus6` on `org.mpris.MediaPlayer2.jellytoast` (the real path KDE's media widget + media keys use):
- Metadata correct (title/album/artist/albumArtist/length-µs); `PlaybackStatus` + `Volume` mirror the app; `CanGoNext=true`.
- **Next** → app → "Rumour Has It" (Playing); **Previous** → back; **PlayPause** → app pause flipped — bidirectional sync confirmed.
- _Tray menu actions + notifications still to exercise._

### 12. Stress / chaos — **PASS (no crash, no leak)**
- **40 rapid navs (no settle):** content_stack stayed **13** (surfaces reused, not rebuilt), app responsive, no crash.
- **Re-nav to albums 8×:** grid model = **297, no doubling** (double-load race well-guarded).
- **24 rapid next/prev/pause/seek:** queue intact (11), responsive.
- **Zero** tracebacks / Qt warnings / cross-thread errors throughout (`faulthandler` armed).
- **Leak check (2 identical cycles + idle):** cycle A grew threads 60→75 / fds 205→216 (one-time pool warm-up); **cycle B added 0** (75→75, 216→216, RSS 1362→1357MB) → **plateau, not a leak**.
- _Note (not a bug):_ baseline footprint ~1.25–1.36 GB RSS / 60–75 threads — heavy-ish; a future `psutil` soak-audit (per research) could trim it.

---

## Summary (session 2 — 2026-06-11, autonomous round)

Full UI tour driven through the bridge while august was away (audio muted at
the mpv handle throughout; settings snapshot restored byte-identical after).
Covered: library picker round-trip, all 9 music tabs, search, sort/view-mode,
cast dialog, sleep menu, volume popup, mini player, context menus
(album/artist/track), track radio (queue 1→26 INSTANT_MIX on Subsonic), Now
Playing + album/artist/genre pages, every Settings page, all 4 theme modes ×
2 accents live-applied, offline-mode chip, A-Z rail jump, shuffle/repeat
round-trips. Screenshots: `/tmp/jt-shots/` (session-local).

**Scorecard:** 2 real bugs + 2 paper-cuts found; 3 fixed + verified live on
`fix/live-round-findings-0611` (suite 2857 green, ruff clean); 1 left as a
design question.

### F4 (P1, fixed) — §-1 audio output picker never listed devices
`player_backend.audio_device_choices` read `self._mpv["audio-device-list"]`;
python-mpv `__getitem__` targets `options/…`, and `audio-device-list` is a
runtime property → every call raised, the `except` returned `[]`, and the
picker offered only Auto — on every platform. Fix: attribute access
(`self._mpv.audio_device_list`); the old test masked it by mocking the handle
as a dict, replaced with a property-only mock that raises on `__getitem__`
exactly like live mpv. Verified live: 32 devices in the Settings picker.
*The §-1 manual walkthrough's "picker populates" step would have failed.*

### F1 (P2, fixed) — opaque black scrollbar gutter on overflowing QScrollArea pages
Search results / Suggestions (and any `install_autofade_scrollbars` QScrollArea
page that overflows) painted a solid-black 8px strip in the scrollbar gutter —
over frost in dark themes and glaring against solid light. Root cause: under
QStyleSheetStyle the QScrollArea paints an opaque unthemed-palette background
in the gutter; descendant QSS rules on the host view don't cure it. Fix: the
installer appends `QScrollArea { background: transparent; border: none; }` on
the widget itself (QListView callers unaffected). Verified by red-fill
`win.render()` probe: gutter now blends with the body veil in Search +
Suggestions.

### F3 (P3, fixed) — EQ "Curve" toggle not gated by bit-perfect
With bit-perfect on, Enable/Linear-phase/sliders disable but the Curve
view-toggle stayed enabled — one live control in a dead section. Added
`_eq_advanced_check` to `_refresh_bit_perfect_gating`.

### F2 (P3, open design question) — mini-player button never toggles closed
`np_bar.queue_btn` (tooltip "Open mini player") is checkable, but
`bus.show_mini_player` only ever `show()`s — second click re-shows, the check
state stays False while the mini player is open, and the main window offers no
close path. Also naming drift: `queue_btn` / `show_queue_requested` actually
drive the mini player. Decide: true toggle vs drop the checkable flag.

### Paper-cuts / observations (no action taken)
- Sleep menu wording mixes "1 hour" with abbreviated "1 h 30 min".
- SearchView leaves the previous tab's label in the view dropdown.
- manual_test_plan §2 says queue header reads "QUEUE — X Radio"; the app shows
  "INSTANT MIX · <seed>" (plan wording drift, app is fine).
- View-menu `setActiveAction` pre-highlight can't be confirmed via the bridge
  (popup state clears on Wayland insta-close) — still wants an eyes-on check
  with arrow keys.

### Verified working (highlights)
- Library picker: stay-open checkable menu, selection round-trip persists, no
  grid doubling on reload; multi-select label flips to "Music".
- Smart playlist editor on **Subsonic** (§1 partial): opens pre-seeded
  ("More like X": genre + year ±3 + not-album), live preview re-queries on
  rule edit, Esc closes. Save / Save & Play untested (no server writes).
- Esc closes Cast dialog + Settings (keyboard-nav pickup items ✓).
- Theme live-apply: all 4 modes + auto round-trip; body fills correct
  (frosted_dark 172α / dark opaque / frosted_light 140α / light opaque);
  accent teal/red restamp genre tiles, checkboxes, selectors, swatch rings;
  transport icons are neutral by design.
- Offline mode: chip appears, grid filters to downloaded content, A-Z rail
  dims to available letters; clean restore.
- A-Z rail jump scrolls and holds (no SmoothScrollFilter snap-back).
- Bit-perfect gating: volume popup padlock, badge segments, crossfade/EQ
  greys; muted handle reflected in the vol icon.

### Harness notes (for the next session)
- Synthetic QTest clicks can't hold Wayland xdg popups (no input serial) —
  menus exec then insta-close. Grab the menu corpse offscreen
  (`menu.adjustSize(); menu.grab()`) or drive the action handlers directly.
- QTest right-click does NOT synthesize QContextMenuEvent; call the
  `contextMenuEvent`/`_on_context_menu` handlers with viewport coords.
- `mpv` in the bridge namespace is MpvController — mute via
  `mpv._mpv.mute = True` (assigning `mpv.mute` shadows nothing useful).
- Spectacle returns blank frames while the screen is locked; `win.grab()` +
  the red-fill `win.render()` probe distinguish "transparent hole" from
  "actively painted" without the compositor.
