# jellytoast 0.1.5 QA — shared brief (read this first, then your platform brief)

You are a Claude Code session running on one of august's machines to do an
**extensive testing pass** feeding the **0.1.5 release** — a cross-platform bug
squish + refinement. This doc is the shared half (how to drive + the
platform-agnostic checklist + house rules). Your **platform brief**
(`QA_SESSION_{PLASMA,WINDOWS,UBUNTU}.md`) adds the native + historical-bug
checks specific to your OS. Local memory does NOT sync across machines —
everything you need is in these two docs + the repo.

**jellytoast** is a native PySide6/Qt6 **music-only** client for Jellyfin +
Subsonic/Navidrome. Branding is always lowercase **jellytoast**. Run with
`python3 -m jellytoast`. Branch: **`test/cross-platform-qa`** (off main = what
0.1.5 ships; macOS-only blur work lives on a separate branch).

## Mission
(1) **Visual consistency** of every surface, (2) **platform-native feature**
checks, (3) **bug hunting**. Capture evidence, review it yourself (you can read
the PNGs), write triaged findings. **Do NOT merge** — test + report; august
decides fixes and timing.

## Setup
```bash
cd ~/jellytoast            # or wherever the checkout lives (Windows: your repo path)
git checkout test/cross-platform-qa && git pull
# create/activate the venv as you normally launch the app on this box
```

## Run the automated gallery
`dev/qa_harness.py` drives the live app via the test bridge and captures the
**real composited screen** (so blur/vibrancy/Acrylic is in the shot — the
bridge's `win.grab()` is blur-blind; never use it to judge frost). It
auto-detects the screenshot tool for your OS, sweeps every surface in dark +
light, plus window states, the mini player, and the smoke test.

```bash
# macOS / Linux — TMPDIR=/tmp is load-bearing (bridge server + client must share it):
TMPDIR=/tmp JT_TEST_BRIDGE=1 python3 -m jellytoast &      # wait for the window + library to load
TMPDIR=/tmp python3 dev/qa_harness.py                     # add --dialogs for modal capture

# Windows (PowerShell), one shell:
$env:JT_TEST_BRIDGE=1; python -m jellytoast               # in one terminal
python dev\qa_harness.py                                  # in another
```
Output → `jt_qa_gallery/` (under TMPDIR, or `/tmp`): `manifest.md` + one PNG per
surface. **Read every PNG** and assess against the checklist. The harness
saves+restores `theme_mode`; if you Ctrl-C mid-run, restore it (see its
docstring). If a surface logs `nav … failed`, the call in `SURFACES`
(`dev/qa_harness.py`) is slightly off for this build — open
`jellytoast/nav_controller.py`, fix it, re-run.

## Driving by hand (the bridge)
```bash
TMPDIR=/tmp python3 dev/jt_ctl.py eval "get_now_playing().title"
TMPDIR=/tmp python3 dev/jt_ctl.py exec "bus.next_track.emit()"
```
Bridge eval/exec namespace: `app, win, bus, mini, settings, QApplication,
QTest, Qt, QPoint, get_now_playing, get_settings, get_provider, cast, qm, mpv`.
Handy: `win._show_songs_view()`, `win._show_native_music_grid("album")`,
`win._open_settings()`, `mini.show()`, `bus.pause_toggled.emit()`,
`win.showFullScreen()`. Capture any moment with your platform's screenshot
command (see your platform brief).

## Checklist — A. Visual consistency (every platform)
- [ ] Every surface's body reads correctly for this platform's blur status —
      glass over the desktop where blur is real, a legible **near-opaque panel**
      where it isn't (never see-through-broken). Surfaces: Albums, Artists,
      Songs, Genres, Suggestions, Radio, Downloads, Smart Playlists, Search,
      Now Playing.
- [ ] **No blank / black / mis-draw** after resize, maximize, fullscreen
      enter+exit, and click-to-another-app-then-back.
- [ ] **Mini player** body matches the main window; rounded corners hold; resize
      it — corners + body survive.
- [ ] **Dialogs** (Settings, Cast, Smart-Playlist editor) — correct body, rounded
      corners, no blank.
- [ ] **Menus / tooltips / volume popup / dropdowns** render correctly (frost
      where blur is real, near-opaque fallback otherwise; not double-veiled).
- [ ] **Dark AND light** themes both correct (compare the two gallery sets).
- [ ] **Text legibility** everywhere — titles, now-playing bar, A–Z rail — over
      the body and over busy album art.
- [ ] **Accent color** consistent across all surfaces; scrollbars/hairlines right.

## Checklist — C. Features (verify on real data)
- [ ] Playback: play / pause / next / prev / seek / stop; volume; no audio glitch.
- [ ] Queue: add, reorder, clear. Smart-shuffle anti-clustering — confirm the
      smoke test PASSed it in `smoke.txt`.
- [ ] Search returns results; artist page opens; genres + albums load.
- [ ] Offline: download a track, toggle offline, play it from cache.
- [ ] Casting (network-dependent): device discovery, cast, transport control.
- [ ] Keyboard nav: Tab between sections, arrows within grids/lists, Enter/Space
      operate; focus rings appear on keyboard, not on mouse click.
- [ ] Provider parity: if a Subsonic/Navidrome server is configured, spot-check a
      couple of surfaces there too — behavior must be identical to Jellyfin.

→ Now do **section B (native)** and **section D (historical bugs)** from your
platform brief.

## Reporting
Append your findings to **`dev/QA_0.1.5.md`** (the shared tracker) under your
platform's section — or, if you can't push, write `/tmp/jt_qa_findings_<os>.md`
and hand it back. Each issue = surface + what's wrong + screenshot path +
severity (**P1** blocker / **P2** should-fix / **P3** polish). Note what you
verified GOOD too. Then summarize back to august.

## House rules (august's standing preferences — encode these)
- **Don't merge** — branch + report only; august gives the explicit go.
- **Don't leave GUI instances running** when you finish — close the app + mini
  player.
- **Restore real settings** you change (the harness restores `theme_mode`; put
  back anything else you flip — these write to real config).
- **Lowercase "jellytoast"** everywhere.
- If you change code to fix a nav call or a small bug, keep it on
  `test/cross-platform-qa`; note it in `QA_0.1.5.md`.
