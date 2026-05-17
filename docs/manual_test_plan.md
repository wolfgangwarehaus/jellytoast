# Manual test plan — pending verifications

A running list of things shipped (or about to ship) that need
august's eyes at the keyboard. Tick items off as you verify them.

Priority tag = same scheme as `docs/TODO.md` (P0-P4). Test items
inherit their feature's priority.

Last updated: 2026-05-17.

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

### §2 Refresh album art — **verified 2026-05-17 ✓**

Change cover art server-side, hit Settings → Refresh album art,
confirm the visible tile updates without restart. Live-verified by
august.

---

## P1

### §3 CastManager UI wiring for new backends (after shipping)

Backends shipped 2026-05-17 (DLNA/Sonos/Snapcast); discovery + push
wiring + cast-dialog fanout pending.

- Open Cast dialog → each enabled protocol's section is visible
  (sections for disabled protocols stay collapsed).
- DLNA: known DLNA renderer on LAN appears in its section.
- Sonos: Sonos zone appears (untested without hardware).
- Snapcast: snapserver's groups + clients appear if cast/snapcast_enabled.
- Mutual exclusion: picking a target in one section clears any picked
  target in the others.
- Section state survives dialog close+reopen.

### §4 Internet radio UI (after shipping)
- Subsonic: stations from `getInternetRadioStations` appear in the
  Radio tab.
- Jellyfin: local `radio/stations` list survives restart.
- Click a station → mpv loads the stream, ICY title appears in the
  NP bar.
- NP surface for radio: no scrubber, just elapsed + stop.
- Cast a station to Chromecast → cast_proxy serves it.
- Stream goes down mid-play → graceful error, not a crash.

### §5 Artist / album / track seeded radio (after shipping)
- Right-click a song → "Start radio from here" → queue fills with
  similar tracks.
- Right-click an artist → "Artist radio" → same.
- Queue gets to within N tracks of empty → automatic extension fires.
- Repeats: same artist doesn't cluster.
- User manually adds a track mid-radio → radio continues after the
  manual addition.

### §6 EQ (after shipping)
- Off → on with default flat preset → no audible difference.
- Move a band slider → audible change in real time, no glitch /
  re-buffer.
- A/B against bypass to confirm chain order.
- Preset switch → all bands snap to new values.
- Save a custom preset, restart app, preset persists.
- High-res audio (24/96 if available) → still works.
- Cast active → EQ greys out, tooltip explains why.

### §7 Smart playlists editor (after shipping)
Evaluator + multi-rule logic shipped 2026-05-17. Editor UI pending.
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
FFT pipeline + worker thread + bus signal shipped 2026-05-17. Widget
pending.
- `JT_VISUALIZER=1`, install `pip install -e ".[visualizer]"`, set
  NP left pane to "visualizer".
- Spectrum bars react to audio (FFT working).
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
- Library with <16 tracks → falls back to classic `random.shuffle`
  (spread penalty starves picks on tiny libs).
- Toggle off → behaves identically to classic random.

### §12 Tag editing UI (after shipping)
Backend shipped 2026-05-17 (Jellyfin only). UI pending.
- Right-click a track → "Edit tags…" → form shows current values.
- Edit a field, save → server reflects change immediately.
- Cover art upload via drag-drop → cover updates on library + NP page.
- Subsonic / Navidrome sign-in: edit affordances hidden cleanly via
  `provider.can_edit_metadata`.
- Bug guard: edit a field that was previously set, save, **then trigger
  a Jellyfin library refresh** — the edit must persist (LockedFields
  workaround for Jellyfin bug #10724).

### §13 Downloads "Repair downloads" + retry-failed — shipped 2026-05-17
- Settings → Downloads: trigger a Repair pass.
- Delete a downloaded file from disk, run Repair → corresponding blob
  row is dropped from `downloads.db`, item flips to failed.
- Corrupt a file's byte count → Repair recomputes it.
- Disconnect, queue a download that fails, reconnect → retry happens
  after the configured backoff (30s, 60s, 120s, ...). Settings
  exposes the next-retry timestamp via `get_retry_state(item_id)`.

### §14 Server-side scrobble badge (after shipping)
- Sign in to Navidrome that has ListenBrainz linked server-side.
- NP bar shows a small "Scrobbled by Navidrome" indicator.
- In-app ListenBrainz toggle is locked off (already true).

### §15 Multi-server hostnames (after shipping)
- Add primary + Tailscale alternate URLs.
- Disconnect from primary's network → connectivity tracker tries
  the alternate before declaring unreachable.
- Reconnect → switches back to primary on first success.
- Terminal: `[jellytoast] host_switched: tailscale`.

### §16 Crossfade (after shipping)
- Enable via `JT_CROSSFADE=1` first; then via Settings.
- Cross-album track change → audible fade between A's tail and B's
  head.
- Same-album adjacent tracks → smart-album-continuity kicks in, no
  crossfade, gapless preserved.
- Skip during fade → cuts cleanly.
- Pause during fade → both instances pause.
- Cast active → setting greys out.

### §17 Hotkey rebinding (after shipping)
- Settings → Hotkeys: change a binding via QKeySequenceEdit.
- Save → binding takes effect immediately (no restart).
- Conflict warning when binding overlaps existing.
- Reset to default works per-row + globally.
- System media keys (Play/Next/Prev) reserved — can't be rebound.

### §18 Theme modes (after light + audit pass)
- Switch dark → light → no restart needed (live-apply).
- Auto-switch follows OS preference change on KDE (`colorSchemeChanged`).
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
- Selecting a device in one section clears any selection in another
  (mutual-exclusion across sections).

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

### §21 Keyboard-nav pickup (2026-05-14)
Per `memory/project_keyboard_nav_pickup_untested.md`:
- Cast dialog: no focus ring on open, Esc closes
- Settings dialog: Esc closes; Esc on open combo closes only the popup
- Top-bar View/Sort dropdowns: arrow nav starts on the current item

### §22 LG-compat AirPlay 2 patch
With an LG webOS TV on the network:
- TV appears in Cast dialog
- Selecting it does not pyatv-crash

### §23 Scrobble end-to-end (after ListenBrainz config)
- Play a track ≥ 30s past `min(d//2, 240s)` → `[scrobble] sent
  listen …`; refresh listenbrainz.org/user/<you>.
- Pause + seek backward + skip past threshold → no scrobble.
- Now-playing pings: one per track start.

### §24 Auth-failure auto-drop to LoginView
Per 2026-05-16 known issue (still untested live):
- Navidrome: change password server-side. Relaunch jellytoast.
- App should drop to LoginView, not hang on a perpetual loading state.
