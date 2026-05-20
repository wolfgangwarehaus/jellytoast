# Smaller P2 parity items — bundled design

> **📍 Status — 2026-05-20:** Mixed. Sleep timer and smart shuffle
> shipped 2026-05-17. Still open from this bundle: live-apply theme
> modes, the crossfade Settings UI, and hotkey rebinding — all on the
> P2 list in `docs/TODO.md`. Kept for rationale.

Status: research / pre-build. Six P2 features that don't each warrant a separate research doc but together cover a sprint's worth of work. Each item gets a self-contained block so we can implement them in any order.

## 1. Overview

The items, in order of appearance:

1. [Multi-server hostnames](#2-multi-server-hostnames) — primary + alternate URLs per server, LAN vs Tailscale style.
2. [Server-side scrobbling indicator UI](#3-server-side-scrobbling-indicator-ui) — surface what `navidrome_detect` already knows.
3. [Smart shuffle](#4-smart-shuffle) — weighted shuffle with recency + artist-spread penalties.
4. [Hotkey rebinding](#5-hotkey-rebinding) — make Settings → Hotkeys editable.
5. [Theme modes beyond `frosted_dark`](#6-theme-modes) — light, dark, system-auto + live-apply.
6. [Sleep timer](#7-sleep-timer) — deferred stop/pause with optional fade-out.

Recommended sequencing is in §8. TL;DR: ship the sleep timer and server-side scrobble badge first — both are small, both are very visible. Light mode is the big one and should be staged carefully.

---

## 2. Multi-server hostnames

### Problem / why
Tailscale users live with two URLs per server: `http://192.168.1.50:4533` at home, `https://music.tail-xyz.ts.net` everywhere else. Today we keep one `server/url` and the user retypes it on every network change — or gives up and just sets the tailnet URL, paying the Tailscale-routing tax even at home. Supersonic addresses this explicitly: "Primary and alternate server hostnames, e.g. for internal and external URLs" (see Sources). Pair with our existing cast-proxy `auto` rule (SPEC §3) and the connectivity tracker, and it's a clear UX win for self-hosters.

### Implementation approach
- **Settings model.** Add `server/hostnames` storing a JSON list `[{"label": "LAN", "url": "http://192.168.1.50:4533", "priority": 1}, {"label": "Tailscale", "url": "https://music.tail-xyz.ts.net", "priority": 2}]`. Keep `server/url` writing through to the head of the list for backwards compat; the credential keys (`server/username`, encrypted token) stay single-valued — same user across all aliases, same cert trust policy.
- **Resolver.** New helper `modules/providers/host_resolver.py` (or inline in `connectivity.py`) holds the "current active URL." Iterates the priority list once at boot, probes `/ping.view` (Subsonic) / `/System/Info/Public` (Jellyfin) with a short timeout, picks the first responder. The active URL is what every provider call uses.
- **Connectivity tracker hook.** In `modules/offline/connectivity.py`, before `note_network_failure` increments past `_UNREACHABLE_THRESHOLD - 1`, try the next alternate. Only flip to unreachable once *every* alternate has failed in the rolling window. Emit a new `PlayerBus.host_switched(label: str)` signal so the bar can flash a small toast ("Now using Tailscale").
- **Login UI.** `modules/login_view.py` `_build_field` row for Server URL grows a small "+ Add alternate URL" affordance below. Each alternate gets a label field + URL field + drag-to-reorder priority. Keep the primary URL as the existing input so first-time login is unchanged.

### Multi-platform considerations
Pure Python. Same code on Linux / Windows / macOS. The probe is a plain HTTP GET via `modules.async_io.get_qnam()` — already cross-platform.

### Provider abstraction
The active URL is plumbed once into the provider singleton at boot and after each switch via `reset_provider()` (see `feedback_provider_singleton_refs.md`). The provider itself is hostname-agnostic. No interface change.

### Edge cases
- **Same user, different permissions.** Some servers gate features by network. Out of scope; document that aliases must point to equivalent accounts on the same backend.
- **Certificate trust.** A self-signed LAN URL paired with a public Let's Encrypt one. Honour `requests` defaults per-URL; don't pin a global verify flag across the alternate list.
- **Probe loop on a flaky network.** Cap one probe per `note_network_failure` burst; don't dial all alternates on every miss.
- **User edits while playback is mid-track.** Don't switch active host until the next provider call boundary — pulling the rug mid-stream tanks playback.

### Effort
**M.** Settings model + resolver + connectivity hook is ~half a day. The login UI redesign (drag-to-reorder list of URL rows with delete buttons) is the rest. Add `host_switched` toast on the now-playing bar last.

---

## 3. Server-side scrobbling indicator UI

### Problem / why
Phase 2 of the scrobble subsystem (`architecture_scrobble.md`) auto-detects Navidrome's per-user Last.fm / ListenBrainz linkage and disables in-app scrobbling for those services to avoid double-counts. The detection result lives in `settings.server_scrobbles_lastfm` / `..._listenbrainz` and is surfaced via a banner in Settings → Scrobbling. But the *now-playing* surfaces — where the user is actually thinking about scrobble state — stay silent. A user on Navidrome who isn't in the Settings dialog has no way to know their plays *are* being counted (just not by us).

### Implementation approach
- **Now-playing page.** Below the album/title block in `modules/now_playing_page.py`, add a small dim label: "Scrobbled by Navidrome → Last.fm" (or → ListenBrainz, or → both). Tooltip: "Your server logs plays automatically. In-app scrobbling is disabled to avoid duplicates." Style: `TYPE_CAPTION` at `TEXT_FAINT`.
- **Now-playing bar.** Tighter on space. Add a single small icon (broadcast / signal glyph from `modules/icons.py`) right of the track title, only visible when at least one server-side service is detected. Tooltip carries the same copy.
- **Live update.** New `PlayerBus.scrobble_status_changed = Signal()` (no payload — subscribers just re-read settings). Emitted when login completes a fresh `navidrome_detect.detect()`, and again when the user toggles in-app scrobbling. Both surfaces re-read on receipt.
- **Reuse settings.** No new keys — `server_scrobbles_lastfm`, `server_scrobbles_listenbrainz`, `server_is_navidrome`, `server_scrobble_check_done` are all already wired.

### Multi-platform considerations
None. Pure widget.

### Provider abstraction
None. The detector is Navidrome-specific by design; this just consumes its existing output.

### Edge cases
- **Pre-detection / detection-failed states.** If `server_scrobble_check_done` is False, hide the badge — we don't know yet. If detection ran but found nothing, hide it. Only show when we've affirmatively detected a linked service.
- **In-app scrobbling enabled despite detection.** User can override the guard in Settings. In that case, show *both* states: "Scrobbled by Navidrome + jellytoast" with a warning-tone tooltip that they're double-scrobbling.
- **Multi-account future.** If/when the multi-server work lands, scrobble status is per active host. Re-emit on `host_switched`.

### Effort
**S.** A label + a signal + two surfaces. Half a day including the tooltip wordsmithing.

---

## 4. Smart shuffle

### Problem / why
`random.shuffle` in `modules/queue_manager.py` (`_apply_shuffle`, lines 363-387) is Fisher-Yates — truly random. Truly random produces the runs-and-clusters the human brain reads as "broken." Spotify rewrote their shuffle in 2014 precisely because true random felt unfair. Even Foobar2000 users mod it. Our queue manager is the cleanest place to fix it: shuffle is already a permutation of `play_order`, so swapping in a smarter permutation is contained.

### Implementation approach
- **Algorithm.** Score-based selection instead of one-shot permutation. Build the shuffled order by greedy weighted pick:
  - For each candidate `c` remaining in the bag, weight = `1.0`.
  - Subtract `recency_penalty` (looks at last N picks; if `c` or another track by the same artist appears in the tail, dock its weight). N = 8 is a good starting cap.
  - Subtract `artist_penalty` (steeper if same artist played within last 3 picks, milder within last 6).
  - Floor at 0.05 so nothing is fully blacklisted in a small library.
  - Sample weighted-random from the bag. Repeat until empty.
- **Setting.** `playback/smart_shuffle` (bool, default `False` — keep classic `random.shuffle` as the simple, predictable default). The toggle goes in Settings → Playback.
- **History.** Rolling deque of last 32 `(track_id, artist_id)` tuples on the QueueManager, fed by `_on_playback_started`. Persisted? No — session-only is fine; a fresh launch with a fresh deque is a feature, not a bug.
- **Refactor.** Split `_apply_shuffle` into `_shuffle_classic` and `_shuffle_smart`. `_on_shuffle_changed` dispatches on `settings.smart_shuffle`. Anchored-head behaviour (current track stays at position 0) is preserved in both modes.

### Multi-platform considerations
None.

### Provider abstraction
None — operates on `original_items`, which is already provider-agnostic.

### Edge cases
- **Tiny libraries.** With <10 tracks, artist penalty would starve picks. Detect and fall back to classic shuffle below a threshold (say, len < 2 × N).
- **Artist-less tracks.** Missing `artist_id` is treated as a single anonymous bucket — penalty still applies, so a queue of all "Unknown Artist" tracks doesn't go fully chaotic.
- **Same-album play-throughs.** Some users *want* album-grouped shuffle. Out of scope here; that's a "shuffle by album, not by track" toggle if we add it later.
- **Toggling mid-queue.** Re-shuffle preserves the current track at the head (existing `keep_at_start` path) under both modes.

### Effort
**S-M.** Algorithm itself is ~30 lines. Setting + UI toggle + the deque wiring + threshold guard is the other half. Solid afternoon's work.

---

## 5. Hotkey rebinding

### Problem / why
Settings → Hotkeys exists as a read-only cheat sheet right now (`_build_hotkeys`, settings_dialog.py:1128-1151). Users who already type `Ctrl+K` for search in five other apps have to remember `Ctrl+F` for jellytoast. Letting them rebind is a small ergonomic win and brings parity with most desktop players. The Qt primitive — `QKeySequenceEdit` — does all the heavy lifting: focus the widget, press keys, the widget recognises modifiers + finalises after a key release plus a short debounce.

### Implementation approach
- **Storage.** `hotkeys/<action_id>` keys in QSettings: `hotkeys/search`, `hotkeys/all_music`, `hotkeys/quit`, `hotkeys/native_album`. Defaults defined in a single `HOTKEY_DEFAULTS` dict at the top of `modules/settings.py` so new actions get exactly one place to add to.
- **Action registry.** Move the `QShortcut(QKeySequence(...))` calls out of `jellytoast.py` (lines 549-570) into `modules/hotkeys.py`. Expose a `register(action_id, callable, parent)` helper that reads the current sequence from settings, builds the `QShortcut`, and keeps a registry so we can swap sequences at runtime.
- **Live re-bind.** New `PlayerBus.hotkeys_changed = Signal(str)` (action_id). `hotkeys.py` listens, finds the existing QShortcut for that action, calls `setKey(QKeySequence(...))`. No restart.
- **Settings UI.** `_build_hotkeys` becomes editable: each row is `QLabel | QKeySequenceEdit | "Reset" button`. The bottom of the page gets a "Reset all to defaults" button. On `editingFinished`, validate against the conflict map (see below), then save + emit.
- **Conflict detection.** Build a `{sequence_str: action_id}` map at save time. On a new binding, if `sequence_str` is already claimed by a *different* action, pop a small inline warning ("This shortcut is also used for X. Save anyway?") with Save / Cancel buttons. Don't silently steal.
- **Reserved sequences.** Hard-coded blacklist: `Media Play`, `Media Next`, `Media Previous`, `Media Stop`. Those are surfaced read-only in the page footer with a "Routed via MPRIS — managed by your desktop" caption (the existing footer text, kept).

### Multi-platform considerations
- **System-level media keys** are out of scope for this work — they go through MPRIS on Linux (`modules/media_controls/_mpris.py`) and we have Windows / macOS scaffolding in place but no implementation. The rebinding UI explicitly shows them as read-only.
- **Within-app shortcuts** are pure `QShortcut` and work uniformly on Linux / Windows / macOS. macOS users will expect `Cmd` mapping for some shortcuts; `QKeySequence` handles `Meta`/`Ctrl` swap natively (`QKeySequence::StandardKey` patterns) — we can opt-in to standards (`StandardKey::Find` → `Cmd+F` on mac, `Ctrl+F` elsewhere) for the common ones.

### Provider abstraction
None.

### Edge cases
- **Sequence collides with a child widget's local shortcut.** E.g. `Ctrl+A` in a text field. Qt's `ShortcutContext` (default = WindowShortcut) gives focused inputs first dibs — usually fine, but document the rule.
- **User binds an unproductive sequence.** Single modifier, no key. `QKeySequenceEdit` allows partial sequences; validate `keySequence().count() >= 1` and reject empty.
- **Multi-key chords.** `QKeySequenceEdit` supports up to 4-key chords. Cap at 2 in our policy — chords beyond two are a power-user UX hole; we can lift later.
- **Restore-defaults regression.** A per-row Reset is fine; a global "Reset all" needs a confirm dialog so users don't nuke a careful rebind setup by accident.

### Effort
**M.** The action registry refactor (pulling shortcuts out of `jellytoast.py`) is the bulk; the widget per row is mechanical. Plan a half-day for the refactor, half-day for the UI + conflict logic.

---

## 6. Theme modes

### Problem / why
`settings.theme_mode` already accepts `frosted_dark | dark | transparent | light` (settings.py:1058-1065) and three of those are defined in `modules/theme.py`. But `light` isn't defined, and `theme_changed` only re-builds GLOBAL_STYLE — it does *not* re-style widgets whose stylesheets were baked at construction. Every surface that ever wrote `rgba(255,255,255,0.06)` directly into a setStyleSheet call is frozen dark. **15 files contain literal `rgba(255,255,255,...)` strings; 95 occurrences total.** That's the audit cost of a light mode.

### Implementation approach
This breaks into two independent pieces. Do them in order.

**Phase A — finish live-apply for the existing themes.** Today `refresh_theme()` rebuilds `GLOBAL_STYLE` (ui_helpers.py:244-280), but anything with a per-widget `setStyleSheet(f"...")` keeps the old colors. The fix is a `_reapply_theme()` method on each major surface that re-sets every stylesheet from the current `ui_helpers` constants, called on `PlayerBus.theme_changed`. We already do this for accent (`architecture_live_accent.md`); extend the same per-surface methods to also re-pull color tokens, not just the accent triple. Caveat: paintEvent body fills (`BODY_COLOR`, `MINI_BODY_COLOR`, `DIALOG_BODY_COLOR`) are read at paint time, so those refresh automatically once the constants are mutated — only QSS strings need the manual reapply.

**Phase B — add the `light` theme.**
- **Theme entry.** New `LIGHT = Theme(name="light", label="Light", ...)` in `modules/theme.py`. Backgrounds: `bg=#f7f7f8`, `bg_panel=#ffffff`, `bg_card=rgba(0,0,0,0.04)`. Text: `text=#1a1a1a`, `text_dim=rgba(0,0,0,0.65)`, `text_faint=rgba(0,0,0,0.40)`. Borders flip to `rgba(0,0,0,0.08)`. The accent presets stay — they're darkened palette swatches that work on both backgrounds.
- **Hardcoded rgba audit.** Every `rgba(255,255,255,...)` in QSS source must become a `TEXT_DIM` / `BORDER` / `BG_CARD` reference. The 95 occurrences live mostly in: `now_playing_bar.py`, `now_playing_page.py`, `library_grid.py`, `settings_dialog.py`, `top_bar.py`, `tray.py`, `search_view.py`, `songs_view.py`, `artist_page.py`. Some are legitimately decorative (e.g. opacity-on-hover overlays) — those map to a new `OVERLAY_HOVER` token derived from the theme's foreground rather than a hardcoded white.
- **System-auto mode.** New `theme_mode = "auto"`. On boot and on `QStyleHints::colorSchemeChanged` (Qt 6.5+), map `Qt::ColorScheme::Dark` → `frosted_dark`, `Light` → `light`. Subscribe in `jellytoast.py` post-show init and call `refresh_theme()` + `PlayerBus.theme_changed.emit()`. On Linux Qt listens to DBus signals for KDE / GNOME / freedesktop already; macOS works natively; Windows 11 works since Qt 6.5 but is more involved on Windows 10.
- **Settings UI.** Display page gets a radio group: Dark / Light / Transparent / Auto (system). Current "Frosted dark" stays the default. Restart notice disappears once Phase A is done.

### Multi-platform considerations
- **Wayland (primary target).** Qt 6.5+ DBus listeners catch `org.kde.kdeglobals` / `org.freedesktop.appearance` — auto-mode works without us doing anything beyond subscribing to the signal.
- **Windows 11.** Works in Qt 6.5+. Windows 10 is partial. Document.
- **macOS.** Native. Works.
- **Light mode + KWin blur.** No interaction — we don't request blur today. If we add it later we'll need a light-mode-specific tint.

### Provider abstraction
None.

### Edge cases
- **Album-cover dominant-color blending.** Light backgrounds may make album art borders disappear. Add a 1px `border` to the cover tiles in light mode.
- **Mini-player translucency.** Light + transparent reads as a milky-white over the desktop — usable but tested only on KDE Plasma's default wallpapers. Hold transparent as dark-only if it looks wrong.
- **System-auto + accent override.** Accent (`accent_color`) is independent of mode; the user's chosen accent should carry across both modes. Already true — `get_active_theme()` applies the override on top of the base.
- **Per-paint-event surfaces (mini player rounded body).** These read the module-level `MINI_BODY_COLOR` tuple. Make sure mutation is atomic so a mid-paint reload doesn't read a half-updated tuple. (Tuples are immutable; reassigning the global binding is atomic — fine.)

### Effort
**L.** Phase A alone is M; Phase B with the audit is L. Realistic estimate: 2-3 sessions. Most of the time goes into the rgba audit and finding which occurrences are "should be a token" vs "is genuinely a hardcoded overlay."

---

## 7. Sleep timer

### Problem / why
Every mobile music client has it. Most desktop ones don't — that gap is our opportunity. The user is winding down, wants their music to stop in 30 minutes without thinking about it. Add to that "end of current track" — the Lyrion / foobar2000 "stop after current" feature people genuinely miss.

### Implementation approach
- **Module.** New `modules/sleep_timer.py`. State held on the module: `_qtimer: QTimer | None`, `_target_mode: "pause" | "fade"`, `_target_ms: int`, `_started_at_ms: int`. No persistence — session-scoped.
- **Public API.** `start(duration_ms, mode)`, `start_end_of_track(mode)`, `extend(ms)`, `cancel()`, `remaining_ms() -> int | None`, `is_active() -> bool`.
- **Bus.** New signals on `PlayerBus`: `sleep_timer_started(remaining_ms: int)`, `sleep_timer_tick(remaining_ms: int)` (1 Hz while active), `sleep_timer_cancelled()`, `sleep_timer_fired()`. The bar / page / mini player all subscribe.
- **Fire action.** Two modes:
  - **Pause** — emit `PlayerBus.pause_requested` and let `queue_manager` handle. No fade.
  - **Fade-out-and-stop** — over 5 seconds, drive `player_backend` volume from current → 0, then `pause_requested`, then restore the saved volume so the next manual play isn't silent. mpv property mutation handles this cleanly.
  - User pick on first use, remembered in `playback/sleep_fade_default` (bool).
- **End-of-track mode.** Hook `PlayerBus.playback_started`. When the active track changes after `start_end_of_track`, fire immediately on the *current* track ending (i.e. before the next `playback_started` arrives). Subscribe to `queue_manager`'s `track_ended` signal if it exists, otherwise to the bar's "ended" code path.
- **UI — now-playing bar.** Small clock-face icon in the right cluster (`_build_right_cluster`-equivalent block around `now_playing_bar.py:1718`). Click → popup menu: 15 / 30 / 60 / 90 minutes / End of current track / Custom… / Cancel. While active, the icon shows accent-coloured + a tiny remaining-time chip ("23m") next to it. Click again while active to extend / cancel.
- **Settings.** Optional, low-priority: `playback/sleep_fade_default` (bool, default False). No persisted timer duration — session-only.

### Multi-platform considerations
Pure Python + Qt. No system integration. Works everywhere.

### Provider abstraction
None.

### Edge cases
- **User pauses manually before timer fires.** Pause the timer too (so the remaining time doesn't burn while playback is paused) and resume on next play. This matches user intent: "30 minutes of music," not "30 minutes of wall clock."
- **User changes tracks during end-of-track mode.** The timer fires at the end of *whichever* track is currently playing when the user activated end-of-track, or the new one? Pick the latter — the active track is what end-of-track is bound to. Re-bind to the new active track on each `playback_started`.
- **Timer fires while no track is playing.** Cancel silently. Don't lock the UI into a "paused" state if nothing was playing.
- **App quit with timer active.** Discard. Session-scoped means session-scoped.
- **Fade-out collision with cast.** Cast volume is driven separately (cast_manager). If casting, fade the cast device volume, not the local mpv volume. Simple if-cast-active branch.

### Effort
**S-M.** The QTimer-and-signals piece is half a day; the bar UI with the popup menu and live countdown badge is the other half. Fade-out is +2 hours including the cast branch.

---

## 8. Recommended sequencing

Order matters because the smaller items unblock real-world feel-good wins while the bigger items can stage in the background.

1. **Server-side scrobbling indicator (S).** Already-detected state, no new APIs, ~half a day. Hidden value: gives every Navidrome user who's already on the build a "yes my plays count" confidence boost.
2. **Sleep timer (S-M).** Visible, novel-for-desktop, lands as a clear differentiator. Ties off a long-standing competitive gap. No deep refactor.
3. **Smart shuffle (S-M).** Backend-only swap behind a setting; users opt in. Low risk; gives us a "smart" toggle to brag about in the AppStream description.
4. **Hotkey rebinding (M).** Touches `jellytoast.py` for the refactor — do this when the surrounding code is quiet. Has a discrete, reviewable boundary (new `modules/hotkeys.py`).
5. **Multi-server hostnames (M).** Requires changes to login UI, connectivity tracker, and provider plumbing. Test on a real Tailscale + LAN setup before merging. Bigger commit but contained.
6. **Theme modes / light mode (L).** The 95-occurrence rgba audit is the long pole. Do Phase A (live-apply for existing themes) in its own PR, then Phase B (light theme + audit) in a second. System-auto comes last and is the easy capstone since Qt 6.5+ does the detection for us.

### Two highest-ROI picks
**Sleep timer** and **server-side scrobbling indicator UI.** Both are S/S-M effort. Both are immediately visible the next time the user launches. The scrobble badge is genuinely cost-free — the detection logic already runs. The sleep timer is a feature mobile users assume exists and desktop users assume they have to live without; shipping it is a quiet flex.

---

## 9. Sources

### Qt / PySide6
- [QKeySequenceEdit — Qt for Python](https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QKeySequenceEdit.html)
- [QShortcut — Qt for Python](https://doc.qt.io/qtforpython-6/PySide6/QtGui/QShortcut.html)
- [QStyleHints (colorScheme + colorSchemeChanged)](https://doc.qt.io/qt-6/qstylehints.html)
- [QGuiApplication — Qt 6](https://doc.qt.io/qt-6/qguiapplication.html)
- [Dark Mode on Windows 11 with Qt 6.5 — Qt blog](https://www.qt.io/blog/dark-mode-on-windows-11-with-qt-6.5)
- [Qt forum: detect system dark/light mode](https://forum.qt.io/topic/147263/qt-detect-system-dark-light-mode)

### Competitive references
- [Supersonic — github.com/dweymouth/supersonic](https://github.com/dweymouth/supersonic) (primary + alternate hostnames; multi-server support)
- [Plexamp sleep timer + fade interaction — Plex forum](https://forums.plex.tv/t/bug-stop-playback-using-timer-plex-amp-with-fades/834688)
- [Lyrion / Logitech Media Server "Stop after current track"](https://forums.lyrion.org/forum/user-forums/general-discussion/77135-stop-after-current-track)
- [foobar2000 components — stop-after-current built in](https://www.foobar2000.org/components)
- [Spotify shuffle algorithm rewrite (Engineering blog)](https://engineering.atspotify.com/2025/11/shuffle-making-random-feel-more-human)
- [How Spotify's shuffle algorithm works — Medium](https://medium.com/immensity/how-spotifys-shuffle-algorithm-works-19e963e75171)
- [A Better Playlist Shuffle Algorithm Is Possible — Hackaday](https://hackaday.com/2023/02/19/a-better-playlist-shuffle-algorithm-is-possible/)
- [An algorithm for shuffling playlists — Ruud van Asseldonk](https://ruudvanasseldonk.com/2023/an-algorithm-for-shuffling-playlists)

### Navidrome / scrobbling
- [Navidrome scrobbling docs](https://www.navidrome.org/docs/usage/features/scrobbling/)
- [Navidrome external integrations docs](https://www.navidrome.org/docs/usage/integration/external-services/)

### Internal references
- `docs/SPEC.md` §3 (cast routing rule pattern)
- `memory/architecture_scrobble.md` (scrobble subsystem already-built state)
- `memory/architecture_live_accent.md` (`PlayerBus.theme_changed` per-surface re-apply contract)
- `memory/feedback_typography_tokens.md` (typography token guidance — same shape applies to color tokens)
- `modules/offline/connectivity.py` (host-failure threshold + bus emit; extension point for alternates)
- `modules/queue_manager.py` `_apply_shuffle` (lines 363-387)
- `modules/scrobble/navidrome_detect.py` (detection result schema)
- `modules/settings_dialog.py` `_build_hotkeys` (lines 1128-1151, currently read-only)
