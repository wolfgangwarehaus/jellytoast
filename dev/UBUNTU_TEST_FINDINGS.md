# jellytoast 0.1.5 — Ubuntu QA pass (findings + evidence)

**Box:** Ubuntu **26.04 LTS** (Resolute Raccoon), GNOME **49**, **Wayland** session
(XWayland present at `:0`); single display.
**Server:** live Subsonic/Navidrome (`http://192.168.50.100:4533`, signed in as `avtips`),
bit-perfect mode on.
**App:** source run (`python3 -m jellytoast`, `jt-build-venv` PySide6 **6.11.1** +
python-mpv, host **libmpv.so.2** + **libxcb-cursor.so.0** present), in **both** a
Wayland (`platform=wayland`) and an **X11/xcb** (`QT_QPA_PLATFORM=xcb` over XWayland)
session. Isolated `JT_INSTANCE_KEY=jt-qa`, test bridge.
**Date:** 2026-06-28. Autonomous pass.

## TL;DR
**Clean pass. No app P1/P2.** Every surface renders correctly as the **near-opaque
fallback body** (the *correct* look on GNOME, which has no app-controllable compositor
blur) — legible in **dark AND light**, frosted **and** solid themes, across window
states, mini player, settings dialog, and the X11 session. **52/52** captured surfaces
assessed good (independent multi-agent visual review, adversarially verified — 0 issues).
MPRIS, autostart, the tray menu, platform-adaptive window decorations, keep-above, and the
fallback-body alphas were all confirmed at runtime. The smoke test is all-pass on live
data. The two historical `.deb` blockers (**#148** bundled-libmpv shadowing, **#149**
missing xcb closure) are **re-verified fixed**.

**One finding:** a **P3** packaging-completeness gap — the `.deb`'s explicit Qt runtime
closure omits `libglib2.0-0` + `libdbus-1-3`, which the bundled `libQt6XcbQpa.so.6`
directly needs and PyInstaller does **not** bundle. Zero real-world impact today (the
`libmpv2` Depends pulls both transitively) — but that transitive masking is exactly the
fragility `build_deb.sh` says it avoids. Trivial defensive fix (separate PR).

---

## A. Visual consistency — PASS (52/52 surfaces, dark + light, Wayland + X11)
GNOME has **no app-controllable window blur** (`blur.status() == UNSUPPORTED` on Wayland,
`UNSUPPORTED` on X11/xcb too — `REQUESTED_UNVERIFIABLE` is the KDE-X11 state, not GNOME's).
So the body is the **intended near-opaque panel**, not glass — and that is correct here.
Measured at runtime via `theme.body_color_for(theme, status)`:

| theme | UNSUPPORTED / X11 (fallback) | ACTIVE (glass, for reference) |
|---|---|---|
| frosted_dark | **(18,18,18,236)** ≈ 92.5% opaque ✓ | (18,18,18,172) ≈ 67% |
| frosted_light | **(244,244,246,240)** ≈ 94% opaque ✓ | (244,244,246,140) ≈ 55% |

The X11/GNOME body picks **236, not 172** — the exact §D concern ("frosted renders
see-through on X11") is handled correctly.

- **Every surface** (Albums, Artists, Songs, Genres, Suggestions, Radio, Downloads,
  Smart Playlists, Search, Now Playing) — legible near-opaque body, titles/subtitles/
  track-lists readable over the body **and** over busy album art; rounded top corners
  intact; accent (purple) + selection borders + focus highlight consistent. Captured in
  **frosted_dark, frosted_light, solid dark, solid light** (40 surface shots).
- **Now Playing** — album cover, synced lyrics (current line bright, rest dimmed), the
  album track-list with the current track accent-highlighted, the audio
  `VisualizerWidget` (intended soft glow). All correct.
- **Window states** — maximized + fullscreen fill the screen **edge-to-edge**, no blank /
  black / mis-draw, no black flash on fullscreen enter+exit; restores to normal cleanly.
- **Mini player** — compact card (art + title/subtitle + prev/play/next), rounded corners,
  body matches main, in dark **and** light.
- **Settings dialog** — correct body, rounded corners, all legible ("Signed in to
  Navidrome as avtips", server URL + green online dot), in dark **and** light.
- **Light theme** — the F6 icon-tint harness fix is in: light-theme top-bar glyphs render
  correctly (not the "invisible icons" artifact the Windows pass diagnosed).

**Capture method (GNOME-49 specific):** `gnome-screenshot` is **blocked** on this Wayland
session (the GNOME Shell screenshot interface is locked down on 49; the X11 fallback
hangs). Since GNOME applies **no** compositor blur, `win.grab()` is a faithful capture of
exactly the on-screen pixels (no glass to miss) — used for the Wayland gallery. The X11
gallery additionally used a **real composited** `import -window <wid>` capture of the
XWayland window (decorations included).

## B. Linux/GNOME-native — PASS (runtime-confirmed)

| Check | Result | Evidence |
|---|---|---|
| No-blur near-opaque fallback | ✓ | frosted_dark `(18,18,18,236)`, frosted_light `(244,244,246,240)` — legible, not glass alpha; gallery confirms |
| **MPRIS** metadata → GNOME media controls | ✓ | `org.mpris.MediaPlayer2.jellytoast` registered; on real playback `Metadata` carries `xesam:title/album/artist/albumArtist`, `mpris:length`, **`mpris:artUrl`** (Navidrome cover), `contentCreated`; `PlaybackStatus` tracks Playing/Paused/Stopped |
| **MPRIS** transport (media keys / playerctl) | ✓ | `PlayPause` toggles pause↔play, `Next`/`Previous` walk the queue (Hello→Lovesong→Remedy→back), `Stop` clears — all via D-Bus; `CanGoNext`/`CanPlay`/`CanPause`/`CanControl` correct |
| Tray icon + menu | ✓ (built) | `QSystemTrayIcon.isSystemTrayAvailable()=True`; SNI host present (`org.kde.StatusNotifierWatcher`); tray visible; **one** menu object (`tray.contextMenu() is menu`) with all 8 items (Play/Prev/Next/Stop/mini/open/quit) |
| **XDG autostart** | ✓ | `enable()` writes `~/.config/autostart/jellytoast.desktop` with **`X-GNOME-Autostart-enabled=true`** + correct `Exec=… -m jellytoast`, Icon, Categories; `disable()` removes it; `is_enabled()` tracks both states (restored to original = off) |
| Window decorations (platform-adaptive) | ✓ | **Wayland** → frameless custom chrome (`FramelessWindowHint`, `borderless=True linux_frameless=True`); **X11/xcb** → **server-side decorations** (native Mutter titlebar, `borderless=False`) — confirmed in the real `import` capture |
| Fullscreen hide/restore | ✓ | enter→`isFullScreen()=True`, exit→`False`; edge-to-edge, no black flash/blank |
| Mini keep-above | ✓ | mini player has `Qt.WindowStaysOnTopHint` (native on X11) |

## C. Features — PASS (smoke all-pass on live data)
`dev/smoke_test.py`: **all checks passed** — provider auth (subsonic, creds from dual
store), search (songs/albums/artists), library tabs (**219 genres**, albums, artists,
songs, 6 playlists), instant-mix (50), genre radio (30), **smart-shuffle anti-clustering
on LIVE data (smart 0.002 vs classic 0.016, 200 songs)**, smart-playlist resolve
(354/354 in-window), stream (**FLAC 206 / audio/flac**), cover serve (jpeg).
Additionally confirmed via the bridge against Navidrome:
- **Playback** — real mpv decode: stream URL loaded, `ao=pipewire`, `duration` present,
  DirectStream + **bit-perfect active**; play / pause / next / prev / stop all drive it.
- **Queue** — `play_now` (5) → `add_to_end` (+3 = 8) → `clear` (0); `move_item` reorder OK.
- **Keyboard nav** — Songs list focuses `_SongsListView`; 3×Down moves selection row 0→3.
- **Provider parity** — the entire pass ran against Subsonic/Navidrome (the configured
  provider); behavior matches the Jellyfin code paths the smoke test exercises.

## D. Historical Linux-fragile spots — re-verified
- **#148 bundled-libmpv shadows host** → **fixed.** No `libmpv*` in the PyInstaller
  bundle (`_internal`); `jellytoast.spec` excludes it on Linux; the `.deb` depends on the
  **system** `libmpv2 | libmpv1` (python-mpv dlopens the host `libmpv.so.2`). No
  GLIBCXX-shadowing surface.
- **#149 missing xcb closure** → **fixed.** `build_deb.sh` declares the full xcb/X11/xkb/
  font/EGL/GL closure (22 packages incl. `libxcb-cursor0`); the app boots under
  `QT_QPA_PLATFORM=xcb` (`platformName=xcb`). *(One residual closure gap — see the P3.)*
- **Frosted see-through on X11/GNOME** → **fixed.** X11 body = `(18,18,18,236)` near-opaque
  fallback, not the `172` glass alpha; legible.
- **Autostart ignored by GNOME** → **fixed.** Entry includes `X-GNOME-Autostart-enabled=true`.
- **Tray double-menu** → single menu object set + popped; SNI host path. *(Right-click
  single-vs-double needs a manual pointer eyeball — see "Couldn't fully verify".)*

---

## Finding — P3 (packaging completeness, fixable)
**The `.deb`'s explicit Qt runtime closure omits two libs the bundled Qt directly needs.**

`build_deb.sh` hand-writes `Depends` and states it declares "the **COMPLETE** Qt runtime
closure explicitly … so a future libmpv repackaging can't silently strip a lib Qt needs."
A recursive `readelf -d` of the **shipped** bundle's xcb plugin closure
(`PySide6/Qt/plugins/platforms/libqxcb.so` → `…/lib/libQt6XcbQpa.so.6` → Qt6Gui/Core)
finds these external (non-bundled) DT_NEEDED libs **not** in `Depends`:

| soname | Debian pkg | Priority | covered by a declared dep? |
|---|---|---|---|
| `libglib-2.0.so.0` | `libglib2.0-0(t64)` | important | **no** (only via `libmpv2`, transitively) |
| `libdbus-1.so.3` | `libdbus-1-3` | important | **no** (only via `libmpv2`, transitively) |
| `libgcc_s.so.1` | `libgcc-s1` | required | yes — pulled by `libgl1`/`libegl1` |
| `libstdc++.so.6` | `libstdc++6` | important | yes — pulled by `libgl1`/`libegl1` |
| `libz.so.1` | `zlib1g` | required | yes — pulled by `libgl1`/`libegl1`/`libfontconfig1` |
| `libzstd.so.1` | `libzstd1` | required | yes — pulled by `libgl1`/`libegl1` |

`libgthread-2.0.so.0` (Qt6XcbQpa) *is* bundled by PyInstaller, but `libglib-2.0.so.0`
is **not** — so glib comes from the system, undeclared. The bottom four are
Priority `required`/`important` and are also transitively pulled by the declared
`libgl1`/`libegl1`, so they're conventionally fine to omit.

**Real-world impact today: none.** `libmpv2 | libmpv1` is a hard Depends, and `libmpv2`
pulls `libglib2.0-0t64` **and** `libdbus-1-3` transitively — so the `.deb` installs and
launches everywhere right now. **But** that's precisely the "happens to pull it
transitively today" fragility the comment says it's guarding against for every other lib:
the Qt closure should stand on its own, independent of libmpv's packaging.

**Severity:** P3 (defensive correctness; no user-visible failure). **Fix:** add
`libglib2.0-0, libdbus-1-3` to the `Depends` line (+ update the closure comment). Done in
a separate fix PR.

---

## Couldn't fully verify on this box
- **True standalone Xorg session.** This box is **Wayland-only** (no Xorg server); X11 was
  exercised via **XWayland** (`QT_QPA_PLATFORM=xcb` over the live `:0`), which genuinely
  covers the xcb platform-plugin path, X11 server-side decorations, and the X11 blur
  fallback. A real Xorg-login decoration/keep-above nuance would need the CachyOS box or
  an actual Xorg session.
- **`.deb` minimal-container smoke** (the definitive #149/closure gate that would catch
  the libglib/libdbus gap as a real failure or prove transitive coverage) needs **Docker**,
  which this box doesn't have. Verified analytically against the actual shipped bundle
  instead (`packaging/deb/smoke_test_deb.sh` remains the CI/container gate).
- **Tray right-click single-vs-double menu** — needs a manual pointer right-click on a
  visible tray icon; Wayland blocks reliable synthetic clicks. Code uses one menu object;
  the SNI/DBusMenu path (active here) does not re-fire Qt's `Context` activation, so single
  is expected.
- **Offline download → cache → play round-trip** and **live cast transport** — not
  exercised (would write real download cache / needs a cast device on the LAN). Subsystems
  present; the Downloads view renders clean; `CastManager` exposes discovery (0 devices on
  this LAN at test time).

## Methodology note (so this reproduces)
MPRIS transport **must** be tested with `dbus-send --print-reply` (or playerctl / real
clients, which wait for the reply). **Fire-and-forget `dbus-send` (no `--print-reply`) is
unreliable** against the dbus-next service — the message can be dropped before the asyncio
loop dispatches it, making transport look dead when it isn't. A minimal dbus-next + Qt
repro confirmed the cross-thread emit works with `--print-reply`. (Adversarial
verification caught this; the first read falsely looked like a P1 MPRIS regression.)
