# Manual test plan — pending verifications

Running list of things shipped (or about to ship) that need august's
eyes at the keyboard. Tick items off as you verify them.

Priority tag = same scheme as `docs/TODO.md` (P0-P4). Test items
inherit their feature's priority.

Last updated: 2026-05-18.

---

## P0

### §1 Offline Phase 5 — full disconnect pass

**All sub-sections previously verified 2026-05-17 ✓** (see git log).
Listed for retest if regressions suspected.

#### §1.4 Scrobble reconnect-flush
Requires ListenBrainz configured.
1. Play a track online so a scrobble is queued. Disconnect mid-track.
2. Confirm `scrobble_queue.json` has at least one pending entry.
3. Reconnect. Expected: `ScrobbleManager._on_connectivity_changed`
   fires `flush_pending()`. Queue file should empty.

### §1.5 Offline Phase 6 — Wi-Fi-only (auto branch, 2026-05-18)

Tests `auto/offline-phase6-wifi-only` before merge.
1. Open Settings → Downloads. Confirm new checkbox "Only download
   on Wi-Fi" appears below "Stream from server even when a track is
   downloaded".
2. Note copy below it explains "auto-detection in a future update".
3. Toggle on → restart app → toggle persists.
4. With downloads queued: from a Python shell, run
   `from modules import offline; offline.mark_metered(True)` and
   confirm queue stops dispatching (currently-running download
   still completes — pause is "stop popping", not kill).
5. `offline.mark_metered(False)` → queue resumes.
6. Toggle wifi-only off while metered + queued → queue starts
   dispatching again.

### §1.6 Offline Phase 6 — DownloadsView UI (auto branch, 2026-05-18)

Tests `auto/offline-phase6-downloads-ui` before merge.

**Pause / Resume button:**
1. Open Settings → Downloads. Confirm "Pause downloads" button
   appears directly under the storage label (above the offline-mode
   checkbox).
2. Queue an album. Hit Pause → button flips to "Resume downloads",
   currently-downloading track finishes, queued tracks idle.
3. Restart app. Pause state persists (button reads "Resume
   downloads" on first open).
4. Resume → queued tracks start downloading again.

**Per-row Re-sync:**
1. With at least one downloaded album in the list, the Remove
   button is preceded by a Re-sync button (smaller, ghost styling).
2. Click Re-sync on a complete row → sub-line shows
   "Album · Re-syncing…" in accent color, both buttons disabled.
3. After a moment (provider round-trip), sub-line returns to
   "Album · {size}" if no drift; "Album · Stale · {size}" if blob
   fields drifted; "Album · Re-sync failed" on error.

**Stale badge:**
1. Run `offline.repair()` from a Python shell after manually
   editing a downloaded item's `DateModified` server-side (or simulate
   via `index.mark_stale("<item_id>")`).
2. DownloadsView row sub-line should read "Track · Stale · {size}"
   in WARN_FG (yellow) — same color as failed downloads, because
   the badge exists to nudge a re-sync.

### §1.7 Downloads — notification toggle (auto branch, 2026-05-18)

Tests `auto/downloads-notify-toggle` before merge. Slice C of the
downloads-progress feature; backend gating already lives in
`manager._emit_drain_complete` (slice A).

1. Open Settings → Downloads. Confirm new checkbox "Notify me when
   downloads finish" appears below "Only download on Wi-Fi" with
   caption note about the system notification channel.
2. Confirm checkbox starts checked (default True).
3. Toggle off → restart app → checkbox persists unchecked.
4. With checkbox ON, queue a small album. After the queue
   completes, system notification appears on your DE's notification
   channel (KDE: persists in history; GNOME: bottom-of-screen toast).
5. With checkbox OFF, repeat — no notification.

### §1.8 Downloads — full progress UI + library walk + clear-all (2026-05-18)

Tests the full downloads arc shipped same-day (slices A/B/C +
library sync + scroll fix + tray fix + various polishes).

**Aggregate progress block (slice B):**
1. Open Settings → Downloads at idle — no aggregate visible.
2. Queue an album (right-click → Download). Within ~1 s an
   aggregate block appears between the storage label and the Pause
   button: `Downloading 1 of N · K%` then `X MB/s · Y left` then a
   4 px accent progress bar.
3. While downloading, hit Pause. Counts read "Paused · 1 of N
   waiting · K%"; tail is empty; bar tints to TEXT_DIM.
4. Resume → variant reverts. Once the queue drains the block hides
   entirely and a desktop notification fires ("Downloads complete —
   N tracks downloaded.").

**Library walk (slice + sync):**
5. Click "Download entire library". Confirm dialog appears; click
   Yes. Button reads "Walking library…" while paginating + counting.
6. Once enumeration finishes (a few seconds for a few hundred
   albums) the aggregate appears with a STABLE right-hand total:
   "Downloading X of T · K%" where T = sum of `ChildCount` across
   all albums. T does NOT climb as new tracks dispatch.
7. Pause button now reads "Pause library download" (was "Pause
   downloads") — confirms bulk-walk awareness.
8. Pause mid-walk → close the app → reopen. Pause button still
   reads "Resume library download" (persisted flag); the same T is
   shown in the aggregate immediately. Click Resume → queue
   continues.
9. Let the walk drain to zero. Notification fires; the
   "Pause library download" rebrand reverts to "Pause downloads"
   for the next ad-hoc queue.

**Clear all downloads:**
10. With at least one downloaded item, "Clear all downloads"
    button is visible. With zero downloads it hides.
11. Click → confirm dialog → Yes. All rows on the standalone
    Downloads page disappear; storage drops to 0 B; aggregate
    hides; pause / resume / library-walk flags all clear; restart
    confirms no "Resume downloads" ghost button.

**Standalone Downloads nav entry:**
12. Top bar → tab dropdown → "Downloads". Main content swaps to
    a page titled "Downloads" with the full per-item list (every
    user-requested node, kind sub-line, Re-sync + Remove buttons).
    Live updates fire on every download_progress signal.

**Aggregate text doesn't clip:**
13. With a fast network, the speed reads "49.1 MB/s · Y left" with
    nothing truncated. Compact one-line check rows below stack
    tightly (toggle on left, tiny caption on the right).

### §2 Refresh album art — verified 2026-05-17 ✓

---

## P1

### §3 CastManager UI wiring for new backends (after shipping)

Backends shipped 2026-05-17 (DLNA/Sonos/Snapcast); CastManager device
wiring still dormant per 2026-05-18 audit. Test after wiring lands.

- Open Cast dialog → each enabled protocol's section is visible
  (sections for disabled protocols stay collapsed).
- DLNA: known DLNA renderer on LAN appears in its section.
- Sonos: Sonos zone appears (untested without hardware).
- Snapcast: snapserver's groups + clients appear if `cast_snapcast_enabled`.
- Mutual exclusion: picking a target in one section clears any picked
  target in the others.
- Section state survives dialog close+reopen.

### §4 Internet radio UI (after shipping)

Backend shipped (Subsonic CRUD + Jellyfin local + ICY observer). UI
pending per 2026-05-18 audit.

- Subsonic: stations from `getInternetRadioStations` appear in the
  Radio tab.
- Jellyfin: local `radio/stations` list survives restart.
- Click a station → mpv loads the stream, ICY title appears in the
  NP bar (`radio_title_changed` already firing).
- NP surface for radio: no scrubber, just elapsed + LIVE pip.
- Cast a station to Chromecast → cast_proxy serves it.
- Stream goes down mid-play → graceful error, not a crash.

### §5 Seeded radio (after RadioFeeder ships)

Provider methods shipped on both backends. RadioFeeder + right-click
affordances pending per 2026-05-18 audit.

- Right-click a song → "Start radio from here" → queue fills with
  similar tracks.
- Right-click an artist → "Artist radio" → same.
- Queue gets to within 5 tracks of empty → automatic extension fires
  (25 new tracks appended, `radio_played_ids` deduped).
- Played-set respected: same track doesn't reappear within a radio
  run.
- Cap at 200: trimming oldest *played* first.
- Repeats: same artist doesn't cluster (skip-heavy reseed).
- User manually adds a track mid-radio → radio continues; header
  flips to "QUEUE — X Radio".

### §6 EQ — shipped 2026-05-17

- Off → on with default flat preset → no audible difference.
- Move a band slider → audible change in real time, no glitch /
  re-buffer.
- A/B against bypass to confirm chain order.
- Preset switch → all bands snap to new values.
- Save a custom preset, restart app, preset persists.
- High-res audio (24/96 if available) → still works.
- Cast active → EQ greys out, tooltip explains why.

### §7 Smart playlists editor (after shipping)

Evaluator + multi-rule logic shipped 2026-05-17. Editor UI + preset
recipes pending.

- Build a "Recently added" rule → preview pane updates live as
  rules change.
- Save → playlist appears in Playlists view, distinguished from
  static.
- Play from a smart playlist → snapshot at play time (queue stays
  static even if rules would re-evaluate).
- Provider switch (Jellyfin → Subsonic) → rules with unsupported
  operators grey out, rest still work.
- Navidrome `.nsp` server-native playlists surface read-only (v2).

### §8 Visualizer rendering widget (after shipping)

FFT + worker thread + bus signal shipped 2026-05-17. Paint widget +
real mpv audio tap pending.

- `JT_VISUALIZER=1`, install `pip install -e ".[visualizer]"`, set
  NP left pane to "visualizer".
- Spectrum bars react to audio (FFT working — but currently the tap
  is stub zeros until mpv lavfi-complex wiring ships).
- Pause → bars idle, don't freeze in last state.
- Cast active → "Casting to <device>" placeholder, not frozen frame.
- 60fps when window has focus; throttles to 30fps when minimized.

### §9 Cast-proxy demo (after recording)

Set up Tailscale-only Navidrome, Chromecast on LAN, laptop offline.
Should record cleanly in 20-30s. Pairs with Flathub screenshot set.

---

## P2

### §10 Sleep timer with fade-to-stop — shipped 2026-05-17
- Set 15 min timer → playback fades to silence over the configured
  `playback/sleep_fade_duration_ms` (default 8 s) and pauses.
- Verify fade ramp is audibly smooth (50 ms ticks).
- Cancel mid-fade → original volume restored.
- "End of current track" → playback ends with the last track.
- Pause → timer pauses too.
- Cast active mid-fade → fade falls through to immediate pause
  (mpv volume isn't what's playing).
- Restart app mid-timer → timer NOT restored (session-scoped).

### §11 Smart shuffle — shipped 2026-05-17
- Settings → Playback: enable "Smart shuffle".
- Shuffle a 50-track multi-artist album → verify the same artist
  doesn't cluster across the first 20 tracks.
- Library with <16 tracks → falls back to classic `random.shuffle`.
- Toggle off → behaves identically to classic random.

### §12 Tag editing UI (after shipping)

Backend shipped 2026-05-17 (Jellyfin only). UI pending per
2026-05-18 audit.

- Right-click a track → "Edit tags…" → form shows current values.
- Edit a field, save → server reflects change immediately.
- Cover art upload via drag-drop → cover updates on library + NP page.
- Subsonic / Navidrome sign-in: edit affordances hidden cleanly via
  `provider.can_edit_metadata`.
- Bug guard: edit a field that was previously set, save, **then trigger
  a Jellyfin library refresh** — edit persists (LockedFields workaround
  for Jellyfin bug #10724).

### §13 Downloads "Repair downloads" + retry-failed — shipped 2026-05-17
- Settings → Downloads: trigger a Repair pass.
- Delete a downloaded file from disk, run Repair → corresponding blob
  row is dropped from `downloads.db`, item flips to failed.
- Corrupt a file's byte count → Repair recomputes it.
- Disconnect, queue a download that fails, reconnect → retry happens
  after the configured backoff (30s, 60s, 120s, ...).

### §14 Server-side scrobble badge — shipped 2026-05-18 (verified by audit)

`_ScrobbleBadge` lives at `now_playing_bar.py:1046`. Verify visually:
- Sign in to Navidrome that has ListenBrainz linked server-side.
- NP bar shows a small "Scrobble" indicator near the title.
- Tooltip carries service destinations.
- In-app ListenBrainz toggle is locked off (already true).

### §15 Multi-server hostnames (after login UI ships)

Backend shipped (`server_hostnames`, alternate-probe, `host_switched`).
Login UI affordance + NP toast pending.

- Add primary + Tailscale alternate URLs.
- Disconnect from primary's network → connectivity tracker tries
  the alternate before declaring unreachable.
- Reconnect → switches back to primary on first success.
- Terminal: `[jellytoast] host_switched: tailscale`.

### §16 Crossfade (after shipping)
- Enable via `JT_CROSSFADE=1` first; then via Settings (when UI
  exposes it).
- Cross-album track change → audible fade between A's tail and B's
  head.
- Same-album adjacent tracks → smart-album-continuity kicks in, no
  crossfade, gapless preserved.
- Skip during fade → cuts cleanly.
- Pause during fade → both instances pause.
- Cast active → setting greys out.

### §17 Hotkey rebinding UI (after shipping)

Registry shipped; UI read-only per 2026-05-18 audit.

- Settings → Hotkeys: change a binding via `QKeySequenceEdit`.
- Save → binding takes effect immediately (no restart).
- Conflict warning when binding overlaps existing.
- Reset to default works per-row + globally.
- System media keys (Play/Next/Prev) reserved — can't be rebound.

### §18 Theme modes (after light + audit pass)

`light` not yet defined in `theme.py`; live-apply per-surface not
yet wired per 2026-05-18 audit.

- Switch dark → light → no restart needed (live-apply).
- Auto-switch follows OS preference change on KDE
  (`colorSchemeChanged`).
- Spot-check every surface in light mode: NP bar, mini player,
  Settings, library, search, downloads.
- Accent color still live-applies in both themes.

### §19 Per-type cast toggles + discovery timing — shipped 2026-05-17
- Settings → Casting: disable Chromecast → cast dialog hides the
  Chromecast section; no mDNS chatter.
- Discovery timing radio: "on demand" → no scan on launch; opening
  the cast menu fires the scan.
- "Startup" → scan starts a few seconds after boot.

### §20 Cast dialog collapsible sections — shipped 2026-05-17
- Open Cast dialog → each protocol's section state matches last
  saved (collapsed/expanded).
- Toggle a section → state persists across dialog close+reopen.
- Empty sections default to collapsed (DLNA/Sonos/Snapcast currently
  empty until UI wiring lands).
- Selecting a device in one section clears any selection in another.

### §21 ReplayGain mode UI toggle — shipped (verified by 2026-05-18 audit)

`_rg_combo` at `settings_dialog.py:731`. Verify:
- Settings → Playback: combo offers no / track / album.
- Pick "track" → mpv `replaygain` property updates.
- Persist across restart.

---

## P3 — baseline release sanity checks

Walk through before cutting any release. Worth keeping the list as
a starting point for an eventual smoke-test script.

- Sign-in: Jellyfin + Subsonic both succeed cold.
- Sign-out clears credentials AND swaps provider singleton refs
  (`memory/feedback_provider_singleton_refs.md`).
- Library Albums / Playlists / Artists / Songs / Genres load.
- Internet radio: a station plays + ICY title surfaces (once UI ships).
- Search returns results in all three buckets.
- Now-playing bar updates cover from `np.image_id` (not `item_id` —
  `memory/feedback_now_playing_cover_pipeline.md`).
- Mini player opens, stays on top (KWin rule), closes cleanly.
- Tray Quit hard-shuts (no minimize loop —
  `memory/known_issue_tray_quit_closeevent.md`).
- Live-accent change applies without restart on every native surface.
- HiDPI: drag window across monitors with different scale factors
  — covers re-request at the new physical size.

### §22 Keyboard-nav pickup (2026-05-14, still pending)
Per `memory/project_keyboard_nav_pickup_untested.md`:
- Cast dialog: no focus ring on open, Esc closes
- Settings dialog: Esc closes; Esc on open combo closes only the popup
- Top-bar View/Sort dropdowns: arrow nav starts on the current item

### §23 LG-compat AirPlay 2 patch
With an LG webOS TV on the network:
- TV appears in Cast dialog
- Selecting it does not pyatv-crash

### §24 Scrobble end-to-end (after ListenBrainz config)
- Play a track ≥ 30s past `min(d//2, 240s)` → `[scrobble] sent
  listen …`; refresh listenbrainz.org/user/<you>.
- Pause + seek backward + skip past threshold → no scrobble.
- Now-playing pings: one per track start.

### §25 Auth-failure auto-drop to LoginView
Per 2026-05-16 known issue (still untested live):
- Navidrome: change password server-side. Relaunch jellytoast.
- App should drop to LoginView, not hang on a perpetual loading state.

### §26 Stylesheet parse warning hunt (P3 quick fix)
Per 2026-05-17: `Could not parse stylesheet of object QPushButton(...)`
during offline disconnect test. After the autonomous fix lands,
verify the terminal stays silent through:
- Browsing while offline-mode toggles flip.
- Opening/closing the Cast dialog.
- Toggling cast-type per-protocol checkboxes.
