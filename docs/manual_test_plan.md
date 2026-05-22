# Manual test plan — pending verifications

Things that need august's eyes at the keyboard. Tick items off as you
verify them. Organized by status: what's ready to verify now, what's
already been verified, what's blocked because there's no UI yet, and
the baseline release smoke test.

As of the 2026-05-21 priority reset, working through this plan is a
first-class priority — the manual bug-testing pass is what gets the
project dialled in before any packaging push (see `docs/TODO.md`).

Last updated: 2026-05-21.

---

## Ready to verify now

Features that have shipped with working UI but haven't been confirmed
by hand. Step-by-step checks below where they help.

### §1 Smart playlists editor + live preview

Evaluator, editor UI, live preview, and the right-click entry have all
shipped. Verify against a real server (Jellyfin and Subsonic both):

1. Open the smart-playlist editor. Build a "Recently added" rule —
   the preview pane should update live as you add/change rules.
2. Save → the playlist appears in the Playlists view, visually
   distinguished from a static playlist.
3. Play from a smart playlist → the queue snapshots at play time and
   stays static even if the rules would re-evaluate.
4. Switch provider (Jellyfin → Subsonic) → rules using operators the
   provider can't support grey out; the rest still evaluate.
5. Right-click an album/artist/genre in the library → "Create smart
   playlist" → the editor opens pre-seeded from that context.

Note: Navidrome `.nsp` server-native playlists surfacing read-only is
still a v2 item, not shipped.

### §2 Start-radio right-click entries

Album, artist, genre, and track all have a "Start radio" right-click
entry, and the RadioFeeder auto-extends the queue.

1. Right-click a song → "Start radio" → the queue fills with similar
   tracks.
2. Right-click an artist → same; right-click a genre → same.
3. Play down to within ~5 tracks of empty → automatic extension
   fires (new tracks appended, `radio_played_ids` deduped).
4. Same track shouldn't reappear within a radio run; same artist
   shouldn't cluster.
5. Queue caps at 200 — oldest *played* tracks trimmed first.
6. Manually add a track mid-radio → radio continues; the queue
   header flips to "QUEUE — X Radio".

### §3 Internet radio (Radio tab)

1. Subsonic: stations from `getInternetRadioStations` appear in the
   Radio tab.
2. Jellyfin: the local `radio/stations` list survives a restart.
3. Click a station → mpv loads the stream; the ICY title shows in
   the NP bar.
4. Radio NP surface: no scrubber, just elapsed time + a LIVE pip.
5. Cast a station to a Chromecast → cast_proxy serves the stream.
6. Stream goes down mid-play → graceful error, not a crash.

### §4 Audio visualizer

1. Launch with `JT_VISUALIZER=1` after `pip install -e ".[visualizer]"`,
   set the NP left pane to "visualizer".
2. Spectrum bars react to real audio.
3. Pause → bars idle (don't freeze in the last frame).
4. Cast active → "Casting to <device>" placeholder, not a frozen
   frame.
5. 60fps when the window has focus; throttles to 30fps when
   minimized.

### §5 Cast dialog — all 5 protocol sections

Discovery and the cast dialog are wired for all five protocols.

1. Open the Cast dialog → each enabled protocol has its own section
   (Chromecast / AirPlay / DLNA / Sonos / Snapcast); disabled
   protocols' sections stay hidden.
2. Section collapsed/expanded state matches the last saved state and
   survives close+reopen.
3. Picking a device in one section clears any selection in another
   (mutual exclusion across sections).
4. Chromecast + AirPlay: real devices on the LAN appear and play.
5. DLNA / Sonos / Snapcast: code is wired but **no hardware is
   available** — these can't be fully verified. If a renderer/zone/
   snapserver shows up on the network, confirm it appears in its
   section; otherwise this stays untested against hardware.

### §6 Downloads — Phase 6 behaviors

The full downloads arc has shipped: aggregate progress block,
standalone Downloads view, Wi-Fi-only, pause/resume, per-row re-sync,
stale badge, retry, finish notification.

**Aggregate progress block:**
1. Settings → Downloads at idle — no aggregate visible.
2. Queue an album → within ~1s an aggregate block appears:
   `Downloading 1 of N · K%`, then `X MB/s · Y left`, then a 4px
   accent progress bar.
3. Hit Pause → counts read "Paused · 1 of N waiting · K%"; bar tints
   to TEXT_DIM. Resume → reverts.
4. Queue drains → block hides; a desktop notification fires
   ("Downloads complete — N tracks downloaded.").

**Library walk:**
5. "Download entire library" → confirm dialog → Yes. Button reads
   "Walking library…" while paginating.
6. After enumeration the aggregate shows a STABLE total T (sum of
   `ChildCount` across all albums); T does not climb as tracks
   dispatch.
7. The Pause button rebrands to "Pause library download".
8. Pause mid-walk → close → reopen → button still reads "Resume
   library download" (persisted); same T shown. Resume → continues.
9. Walk drains → notification fires; the rebrand reverts to "Pause
   downloads".

**Wi-Fi-only:**
10. Settings → Downloads has "Only download on Wi-Fi" with copy
    noting auto-detection is future work. Toggle persists across
    restart.
11. With downloads queued, `offline.mark_metered(True)` from a Python
    shell stops the queue dispatching (a running download finishes);
    `mark_metered(False)` resumes it.

**Finish notification toggle:**
12. "Notify me when downloads finish" checkbox is present, starts
    checked, persists across restart. ON → notification on queue
    complete; OFF → none.

**Per-row Re-sync + stale badge:**
13. A downloaded row has a Re-sync button before Remove. Click it →
    sub-line shows "Album · Re-syncing…" in accent, both buttons
    disabled, then returns to "Album · {size}" (or "· Stale ·" /
    "· Re-sync failed").
14. After `index.mark_stale("<item_id>")` the row sub-line reads
    "Track · Stale · {size}" in WARN_FG (yellow).

**Clear all + standalone view:**
15. "Clear all downloads" → confirm → Yes: all rows vanish, storage
    drops to 0 B, all pause/walk flags clear, no ghost "Resume"
    button after restart.
16. Top bar → tab dropdown → "Downloads" → a page titled "Downloads"
    with the full per-item list; live updates on every
    download_progress signal.

### §7 Smart-rule schema v2 — date-based rules

Merged and verified 2026-05-20. `date_added` / `last_played` rule
fields with `in the last` / `before` / `after` operators. Re-check on
any change to the provider date-field mapping or the rule evaluator:

1. New smart playlist → the rule field dropdown offers **Date added**
   and **Last played**.
2. **Date added → in the last → 30 days** → preview fills with
   recently-added tracks. On a large Jellyfin library this takes a few
   seconds — the fetch pages and stops at the cutoff (a single
   unbounded fetch used to time out).
3. **Last played → before / after → a date** (calendar picker) →
   Jellyfin populates; Subsonic correctly matches nothing (no
   per-track last-played timestamp there).
4. The "Recently added" preset populates on both backends.
5. Older saved smart playlists (year / play_count rules) still load.

### §8 Sleep timer + smart shuffle UI

Both engines were built earlier but had no UI; this wires them up.

1. Now-playing bar shows a moon button between the mini-player and
   cast icons. Click it → menu of 15 / 30 / 45 min, 1 hour, 1 h 30
   min, plus "Stop after current track".
2. Pick a duration → moon goes accent-tinted; hover the button → the
   tooltip counts down ("Sleep timer — 29:58 left").
3. Re-open the menu while armed → the chosen duration is checked and
   a "Cancel timer (mm:ss left)" row appears. Cancel → moon returns
   to its idle colour, tooltip back to "Sleep timer".
4. Let a short timer elapse → playback fades out and pauses (the
   fade duration is the Settings value); moon clears.
5. "Stop after current track" → playback pauses cleanly at the end
   of the current song.
6. Settings → Playback has a "Smart shuffle" checkbox. Enable it,
   turn Shuffle on, and shuffle a large library (16+ tracks) → the
   same artist no longer clusters back-to-back the way plain random
   does with the box unchecked.

---

## Verified already

Compressed history so it isn't lost. These were ticked off in earlier
audits/sessions:

- Offline Phase 5 full disconnect pass (2026-05-17), including
  scrobble reconnect-flush.
- Refresh album art (2026-05-17).
- 10-band EQ (2026-05-17): flat-preset no-op, live band changes,
  preset switching, custom-preset persistence, greys out when
  casting.
- Sleep-timer fade math (2026-05-17): smooth fade ramp, cancel
  restores volume, end-of-track mode, pause-pauses-timer,
  session-scoped (not restored on restart). The fade *engine* is
  verified; see Blocked list below for the missing start UI.
- Smart shuffle engine (2026-05-17): anti-clustering, <16-track
  classic fallback. Engine only — see Blocked list.
- Downloads Repair + retry-failed (2026-05-17).
- Per-type cast toggles + discovery timing radio (2026-05-17).
- Cast dialog collapsible sections (2026-05-17).
- Server-side scrobble badge (2026-05-18 audit).
- ReplayGain mode combo (2026-05-18 audit): no/track/album, mpv
  property updates, persists.

---

## Blocked — no UI to test yet

These have backend code but nothing user-facing to drive them, so
they cannot be hand-tested. One line each on what's missing:

- **Crossfade** — only reachable via `JT_CROSSFADE=1`; no Settings
  control.
- **Hotkey rebinding** — the registry exists; the Settings page is
  read-only, no `QKeySequenceEdit` rebinding.
- **Tag editing** — Jellyfin backend exists; there is no "Edit
  tags…" UI.
- **Multi-server hostnames** — `server_hostnames` / alternate-probe
  backend exists; the login screen has no UI to add alternate URLs.
- **Light theme** — `light` is not defined in `theme.py`; accent
  live-applies but mode-swap needs a restart. The light-theme
  surface spot-check is blocked until the theme exists.

Last.fm scrobbling is also dormant (empty API key) — only
ListenBrainz is testable today.

---

## Release sanity checks (P3)

Walk through before cutting any release. A good starting point for an
eventual smoke-test script.

- Sign-in: Jellyfin + Subsonic both succeed cold.
- Sign-out clears credentials AND swaps provider singleton refs
  (`memory/feedback_provider_singleton_refs.md`).
- Library Albums / Playlists / Artists / Songs / Genres load.
- Internet radio: a station plays + ICY title surfaces.
- Search returns results in all three buckets.
- Now-playing bar updates cover from `np.image_id` (not `item_id` —
  `memory/feedback_now_playing_cover_pipeline.md`).
- Mini player opens, stays on top (KWin rule), closes cleanly.
- Tray Quit hard-shuts (no minimize loop —
  `memory/known_issue_tray_quit_closeevent.md`).
- Live-accent change applies without restart on every native
  surface.
- HiDPI: drag the window across monitors with different scale
  factors — covers re-request at the new physical size.

### Keyboard-nav pickup

Per `memory/project_keyboard_nav_pickup_untested.md`:
- Cast dialog: no focus ring on open, Esc closes.
- Settings dialog: Esc closes; Esc on an open combo closes only the
  popup.
- Top-bar View/Sort dropdowns: arrow nav starts on the current item.

### LG-compat AirPlay 2 patch

With an LG webOS TV on the network:
- TV appears in the Cast dialog.
- Selecting it does not pyatv-crash.

### Scrobble end-to-end (ListenBrainz)

- Play a track ≥ 30s past `min(d//2, 240s)` → `[scrobble] sent
  listen …`; refresh listenbrainz.org/user/<you>.
- Pause + seek backward + skip past threshold → no scrobble.
- Now-playing pings: one per track start.

### Auth-failure auto-drop to LoginView

Per the 2026-05-16 known issue:
- Navidrome: change the password server-side, relaunch jellytoast.
- The app should drop to LoginView, not hang on a perpetual loading
  state.

### Stylesheet parse warning hunt

Confirm the terminal stays silent (no "Could not parse stylesheet"
warnings) through:
- Browsing while offline-mode toggles flip.
- Opening/closing the Cast dialog.
- Toggling cast-type per-protocol checkboxes.

### Cast-proxy demo recording

Set up a Tailscale-only Navidrome, a Chromecast on the LAN, laptop
offline. Should record cleanly in 20-30s. Pairs with the Flathub
screenshot set.
