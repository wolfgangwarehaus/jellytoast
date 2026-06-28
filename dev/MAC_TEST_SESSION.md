# macOS test session — mission brief (for a Claude running ON the Mac)

You are a Claude Code session running on **august's real Intel MacBook Pro
(macOS Sequoia 15)**, in the `jellytoast` repo, on branch
**`feat/macos-native-blur`**. The primary-machine session (on Linux) prepared
this brief because local memory doesn't sync across machines — everything you
need is here.

**jellytoast** is a native PySide6/Qt6 **music-only** client for Jellyfin +
Subsonic/Navidrome. Branding is always lowercase **jellytoast**. Run with
`python3 -m jellytoast`.

## Your mission
Do an **extensive testing pass on real hardware**: (1) **visual consistency**
of the new native macOS blur across every surface, (2) **macOS-native feature**
checks, (3) **bug hunting**. Capture evidence, review it yourself (you can read
the PNGs), and write up findings. **Do NOT merge anything** — this branch needs
august's explicit approval to merge; your job is to test and report.

## What just shipped on this branch (verify it holds)
1. **Native NSVisualEffectView blur via "sibling-below"** (`jellytoast/blur/_macos.py`).
   The effect view sits *below* Qt's content view (not a content-view swap),
   which fixed the old **blank/mis-drawn window on resize / focus-change**. This
   is the #1 thing to stress: resize, maximize, fullscreen, and click-away-then-
   back must NEVER blank or mis-draw the window, mini player, or dialogs.
2. **Frosted-body transparency tuned to `JT_MAC_GLASS_ALPHA=110`** (default baked
   in `jellytoast/theme.py`), matched by eye to the KDE Plasma blur. Watch that
   text stays legible over the lighter glass (over busy album art especially).
3. **Tray right-click double-menu fixed** (`jellytoast/tray.py`) — right-click
   should now show a SINGLE translucent menu, not an opaque native twin.

## Setup
```bash
cd ~/jellytoast            # or wherever the checkout lives
source .venv/bin/activate
git pull                  # get this branch's latest
```

## Run the automated gallery
The harness drives the live app via the test bridge and `screencapture`s the
**real composited blur** (the bridge's own `win.grab()` is blur-blind — never
use it to judge frost). It sweeps every surface in dark + light, plus window
states, the mini player, and the smoke test.

```bash
# 1) launch with the bridge — TMPDIR=/tmp is load-bearing (server+client must
#    share it to resolve the same socket):
TMPDIR=/tmp JT_TEST_BRIDGE=1 python3 -m jellytoast &
#    wait until the window is up + the library has loaded, then:

# 2) run the harness in the SAME shell:
TMPDIR=/tmp python3 dev/mac_test_harness.py          # add --dialogs for modal capture
```
Output lands in `/tmp/jt_mac_gallery/` — `manifest.md` plus one PNG per surface.
**Read every PNG** and assess against the checklist below. The harness saves +
restores `theme_mode`, but if you Ctrl-C it mid-run, restore it (see its
docstring).

If a surface logs `nav … failed`, the navigation call in `SURFACES`
(`dev/mac_test_harness.py`) is slightly off for this build — open
`jellytoast/nav_controller.py`, find the right `_show_*` method, fix the call,
re-run. You're fast on real hardware; iterate.

## Driving the app by hand (the bridge)
For anything the harness doesn't cover, drive it directly. One-shot:
```bash
TMPDIR=/tmp python3 dev/jt_ctl.py eval "get_now_playing().title"
TMPDIR=/tmp python3 dev/jt_ctl.py exec "bus.next_track.emit()"
```
Bridge eval/exec namespace: `app, win, bus, mini, settings, QApplication,
QTest, Qt, QPoint, get_now_playing, get_settings, get_provider, cast, qm, mpv`.
Useful calls: `win._show_songs_view()`, `win._show_native_music_grid("album")`,
`win._open_settings()`, `mini.show()`, `bus.pause_toggled.emit()`,
`win.showFullScreen()`. Capture a real-blur shot any time with
`screencapture -x /tmp/shot.png`.

## Checklist

### A. Visual consistency (the blur work — primary)
- [ ] Every surface's body reads as **frosted glass over the desktop**, not an
      opaque panel and not see-through-broken. (Albums, Artists, Songs, Genres,
      Suggestions, Radio, Downloads, Smart Playlists, Search, Now Playing.)
- [ ] **No blank / black / mis-draw** after: window resize (drag a corner),
      maximize, fullscreen enter+exit, and click-to-another-app then back.
- [ ] **Mini player** body matches the main window's glass (historical mismatch
      point) and keeps its **rounded corners**; resize it — corners + frost hold.
- [ ] **Dialogs** (Settings, Cast, Smart-Playlist editor) show frost + rounded
      corners, no blank.
- [ ] **Menus / tooltips / the tray menu** are frosted (not opaque-native), and
      the **volume popup** reads right.
- [ ] **Dark AND light** themes both look correct (compare the two gallery sets).
- [ ] **Text legibility**: titles, the now-playing bar, and the A–Z rail stay
      readable over the lighter `110` glass and over busy album art. If marginal,
      note it — the fallback lever is a lighter vibrancy *material*, not a thinner
      body.
- [ ] Accent color is consistent across surfaces; scrollbars/hairlines look right.

### B. macOS-native
- [ ] **Tray icon**: left-click and right-click each show ONE frosted menu (no
      double). Every item works: Play/Pause, Previous, Next, Stop, Show/Hide Mini
      Player, Open jellytoast, Quit. Now-playing label updates while playing.
- [ ] **Traffic-light titlebar**: the transparent-titlebar inset looks right;
      entering fullscreen drops the inset and content fills; exiting restores it.
- [ ] **Media keys / Now Playing**: F7/F8/F9 and Control-Center "Now Playing"
      control jellytoast; title/artist/artwork show there.
- [ ] **Mini player** stays **always-on-top**; its position **persists** across an
      app restart (don't land jammed under the menu bar at 0,0).
- [ ] **Single-instance**: relaunching focuses the existing window, no 2nd copy.
- [ ] **Quit** (tray + ⌘Q) fully exits — no lingering mini surface, audio stops.

### C. Features (verify on real data)
- [ ] Playback: play / pause / next / prev / seek / stop; volume; no audio glitch.
- [ ] Queue: add, reorder, clear. Smart-shuffle anti-clustering (the smoke test
      checks this on live data — confirm it PASSed in `smoke.txt`).
- [ ] Search returns results; artist page opens; genres + albums load.
- [ ] Offline: download a track, toggle offline, play it from cache.
- [ ] Casting (network-dependent): device discovery, cast, transport control.
- [ ] Keyboard nav: Tab between sections, arrows within a grid/list, Enter/Space
      operate. Focus rings appear on keyboard, not on mouse click.
- [ ] Provider parity: if a Subsonic/Navidrome server is configured, spot-check a
      couple of surfaces there too (features must behave identically).

### D. Re-verify historically macOS-fragile spots
- [ ] Window blank-on-resize / on-activation — **fixed by the blur rewrite**;
      confirm dead.
- [ ] Frameless surface resize (mini, dialogs) — no artifacts.
- [ ] Fullscreen titlebar-inset toggle — correct both directions.
- [ ] Tray double-menu — **fixed**; confirm single.

## Reporting
Write findings to `/tmp/jt_mac_findings.md` (don't commit it) as a triaged list:
each issue = surface + what's wrong + a screenshot path + severity (P1 blocker /
P2 should-fix / P3 polish). Note what you verified GOOD too. Then summarize back
to august. He decides what to fix and when to merge.

## House rules (encode these — they're august's standing preferences)
- **Don't leave GUI instances running** after you finish — close the app + mini
  player (his launches are namespace-isolated; strays cause confusion).
- **Restore real settings** you change (the harness restores `theme_mode`; if you
  flip anything else by hand, put it back).
- **Lowercase "jellytoast"** everywhere.
- **Do NOT merge** — branch + report only; august gives the explicit go.
