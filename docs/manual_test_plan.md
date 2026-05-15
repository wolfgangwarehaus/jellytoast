# Manual test plan — pending verifications

A running list of things shipped (or about to ship) that need
august's eyes at the keyboard. Tick items off as you verify them.

Priority tag = same scheme as `docs/TODO.md` (P0-P4). Test items
inherit their feature's priority.

Last updated: 2026-05-15.

---

## P0

### §1 Offline Phase 5 — full disconnect pass

The connectivity tracker, auto-offline, scrobble reconnect-flush,
chip states, library/search/artist filters all wired but only smoke-
exercised via the Settings toggle.

#### §1.1 Threshold + auto-offline (real disconnect)
1. Settings → Downloads: "Automatic offline mode" ON, "Offline mode"
   OFF. Confirm chip is hidden.
2. Kill the network (`sudo ip link set <iface> down` or toggle Wi-Fi).
3. Browse Albums / scroll covers. Expected terminal output:
   - `[jellytoast] connectivity → unreachable` after 3 failed requests
   - `[jellytoast] offline mode → on` immediately after
   - Chip appears reading "Offline"
4. Bring network back. Expected:
   - `[jellytoast] connectivity → reachable` on the first success
   - `[jellytoast] offline mode → off` (auto-set, lifts on reconnect)
   - Chip disappears

#### §1.2 User-set offline survives a blip
1. Settings → Downloads: toggle "Offline mode" ON manually.
2. Disconnect / reconnect network. Expected: offline mode stays on
   across the cycle (`_offline_source == "user"`).
3. Click chip → "Connecting…" → online.

#### §1.3 Persistence across restart
1. Toggle offline mode ON.
2. Quit + relaunch. Expected: chip is present at boot, library shows
   downloads only.

#### §1.4 Scrobble reconnect-flush
Requires ListenBrainz configured.
1. Play a track online so a scrobble is queued. Disconnect mid-track.
2. Confirm `scrobble_queue.json` has at least one pending entry.
3. Reconnect. Expected: `ScrobbleManager._on_connectivity_changed`
   fires `flush_pending()`. Queue file should empty.

#### §1.5 Bug fix verification — search "air"
Run after merging `auto/search-air-fix`:
- Search "a" → returns songs ✓ (regression check)
- Search "air" → returns the Air album tile (via AlbumArtist match)
- Search "air" → returns an Air artist tile (synthesized from
  album AlbumArtists)
- Toggle offline ↔ online with search field populated → re-fires
  cleanly

#### §1.6 Bug fix verification — artist page offline
Run after merging `auto/artist-page-offline-fix`:
- Click an artist link offline → page shows the downloaded album
  with a synthesized header, NOT "Couldn't load artist"
- Toggle offline ↔ online while on the artist page → page reloads
  from the new source

---

## P1

### §2 Internet radio (after shipping)
- Subsonic: stations from `getInternetRadioStations` appear in the
  Radio tab.
- Jellyfin: local `radio/stations` list survives restart.
- Click a station → mpv loads the stream, ICY title appears in the
  NP bar.
- NP surface for radio: no scrubber, just elapsed + stop.
- Cast a station to Chromecast → cast_proxy serves it.
- Stream goes down mid-play → graceful error, not a crash.

### §3 Artist / album / track seeded radio (after shipping)
- Right-click a song → "Start radio from here" → queue fills with
  similar tracks.
- Right-click an artist → "Artist radio" → same.
- Queue gets to within N tracks of empty → automatic extension fires.
- Repeats: same artist doesn't cluster.
- User manually adds a track mid-radio → radio continues after the
  manual addition.

### §4 EQ (after shipping)
- Off → on with default flat preset → no audible difference.
- Move a band slider → audible change in real time, no glitch /
  re-buffer.
- A/B against bypass to confirm chain order.
- Preset switch → all bands snap to new values.
- Save a custom preset, restart app, preset persists.
- High-res audio (24/96 if available) → still works.
- Cast active → EQ greys out, tooltip explains why.

### §5 Smart playlists (after shipping)
- Build a "Recently added" rule → preview pane updates live as
  rules change.
- Save → playlist appears in Playlists view, distinguished from
  static.
- Play from a smart playlist → snapshot at play time (queue stays
  static even if rules would re-evaluate).
- Provider switch (Jellyfin → Subsonic) → rules with unsupported
  operators grey out, rest still work.
- Navidrome `.nsp` server-native playlists surface read-only (v2).

### §6 Cast-proxy demo (after recording)
Set up Tailscale-only Navidrome, Chromecast on LAN, laptop offline.
Should record cleanly in 20-30s.

---

## P2

### §7 Server-side scrobble badge
- Sign in to Navidrome that has ListenBrainz linked server-side.
- NP bar shows a small "Scrobbled by Navidrome" indicator.
- In-app ListenBrainz toggle is locked off (already true).

### §8 Sleep timer
- Set 15 min timer → playback pauses (or fades) at minute 15.
- "End of current track" → playback ends with the last track.
- Pause → timer pauses too.
- Cancel + extend → no surprises.
- Restart app mid-timer → timer NOT restored (session-scoped).

### §9 Smart shuffle
- Toggle on. Shuffle a 50-track album → verify the same artist
  doesn't cluster across the first 20 tracks.
- Toggle off → falls back to `random.shuffle` (the simple option).

### §10 Multi-server hostnames
- Add primary + Tailscale alternate URLs.
- Disconnect from primary's network → connectivity tracker tries
  the alternate before declaring unreachable.
- Reconnect → switches back to primary on first success.
- Terminal: `[jellytoast] host_switched: tailscale`.

### §11 Crossfade
- Enable via `JT_CROSSFADE=1` first; then via Settings.
- Cross-album track change → audible fade between A's tail and B's
  head.
- Same-album adjacent tracks → smart-album-continuity kicks in, no
  crossfade, gapless preserved.
- Skip during fade → cuts cleanly.
- Pause during fade → both instances pause.
- Cast active → setting greys out.

### §12 Visualizers
- Toggle NP left pane to visualizer mode.
- Spectrum bars react to audio (FFT working).
- Pause → bars idle, don't freeze in last state.
- Cast active → "Casting to <device>" placeholder, not frozen frame.
- 60fps when window has focus; throttles to 30fps when minimized.

### §13 Hotkey rebinding
- Settings → Hotkeys: change a binding via QKeySequenceEdit.
- Save → binding takes effect immediately (no restart).
- Conflict warning when binding overlaps existing.
- Reset to default works per-row + globally.
- System media keys (Play/Next/Prev) reserved — can't be rebound.

### §14 Tag editing (Jellyfin admin only)
- Right-click a track → "Edit tags…" → form shows current values.
- Edit a field, save → server reflects change immediately.
- Cover art upload via drag-drop → cover updates on library + NP page.
- Subsonic / Navidrome sign-in: edit affordances hidden cleanly.
- Bug guard: artifact should not get corrupted if user clears a
  field that was previously set (the Jellyfin bug #10724 mitigation).

### §15 Theme modes
After light theme + audit pass:
- Switch dark → light → no restart needed (live-apply).
- Auto-switch follows OS preference change on KDE (`colorSchemeChanged`).
- Spot-check every surface in light mode: NP bar, mini player,
  Settings, library, search, downloads.
- Accent color still live-applies in both themes.

---

## P3 — baseline release sanity checks

Walk through before cutting any release. Worth keeping the list as
a starting point for an eventual smoke-test script.

- Sign-in: Jellyfin + Subsonic both succeed cold.
- Sign-out clears credentials AND swaps provider singleton refs
  (`memory/feedback_provider_singleton_refs.md`).
- Library Albums / Playlists / Artists / Songs / Genres load.
- Search returns results in all three buckets.
- Now-playing bar updates cover from `np.image_id` (not `item_id` —
  `memory/feedback_now_playing_cover_pipeline.md`).
- Mini player opens, stays on top (KWin rule), closes cleanly.
- Tray Quit hard-shuts (no minimize loop —
  `memory/known_issue_tray_quit_closeevent.md`).
- Live-accent change applies without restart on every native surface.
- HiDPI: drag window across monitors with different scale factors
  — covers re-request at the new physical size.

### §16 Keyboard-nav pickup (2026-05-14)
Per `memory/project_keyboard_nav_pickup_untested.md`:
- Cast dialog: no focus ring on open, Esc closes
- Settings dialog: Esc closes; Esc on open combo closes only the popup
- Top-bar View/Sort dropdowns: arrow nav starts on the current item

### §17 LG-compat AirPlay 2 patch
With an LG webOS TV on the network:
- TV appears in Cast dialog
- Selecting it does not pyatv-crash

### §18 Scrobble end-to-end (after ListenBrainz config)
- Play a track ≥ 30s past `min(d//2, 240s)` → `[scrobble] sent
  listen …`; refresh listenbrainz.org/user/<you>.
- Pause + seek backward + skip past threshold → no scrobble.
- Now-playing pings: one per track start.
