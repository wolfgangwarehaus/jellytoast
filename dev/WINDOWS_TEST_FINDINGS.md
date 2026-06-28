# jellytoast 0.1.5 — Windows 11 QA pass (findings + evidence)

**Box:** Windows 11, build **26200**, single display @ **125%** scale, non-admin.
**Server:** live Subsonic/Navidrome (`http://192.168.50.100:4533`, signed in as `avtips`).
**App:** source run (`python -m jellytoast`, venv PySide6 6.11.1, libmpv-2.dll present) **and** a frozen PyInstaller onedir build.
**Driver:** `dev/qa_harness.py` over the test bridge (isolated `JT_INSTANCE_KEY=jt-qa`), full composited-screen capture, dark + light, + per-surface bridge introspection + a frozen-exe launch test.
**Date:** 2026-06-28. Autonomous pass.

## TL;DR
**Clean pass. No app P1/P2.** Acrylic frosted glass reads correctly as translucent glass on **all 25 surfaces in BOTH dark and light**, every Windows-native code path is present (with unit tests) and the runtime-observable ones were confirmed live, the frozen install boots with bundled libmpv and no console window, single-instance works in the frozen shape, and the smoke test is all-pass on live data.

Three "scary" things the gallery showed turned out to be **harness artifacts, not app bugs** (two now fixed in the harness, one explained). A handful of minor cosmetic **P3 candidates** remain for a human eyeball. Some checks couldn't be done on this box (single display, non-admin, auto-hidden taskbar) — listed at the bottom.

---

## A. Visual consistency — PASS (25/25 `glass_good`, dark + light)
Acrylic is genuinely active here: `blur/_dwm.is_supported()=True`, build `26200`, `_transparency_enabled()=True`, and the desktop wallpaper bleeds through the body in every shot. Dark carries the heavier veil (`_ACRYLIC_TINT_DARK=0xBE…`), light the lighter frost (`0x99…`) — both legible, never see-through-broken, never opaque-flat.

- **Every surface** (Albums, Artists, Songs, Genres, Suggestions, Radio, Downloads, Smart Playlists, Search, Now Playing) — translucent frosted glass, dark **and** light; titles/subtitles/track-lists legible over body **and** busy album art; rounded top corners intact; accent (purple) + focus rings + selection borders consistent.
- **Window states** — maximized + fullscreen fill the work area edge-to-edge (square corners correct for those states), no blank/black/mis-draw on the state sweep.
- **Mini player** — frosted card (album art + title/subtitle + prev/pause/next), rounded corners, frost over the desktop; matches the main body. ✓
- **Settings dialog** — frosted acrylic panel, album art ghosts faintly through, all text legible ("Signed in to Navidrome as avtips", server URL + green online dot), accent consistent, rounded corners, modal scrim — in **dark and light**. ✓
- **Now-playing visualizer** — `VisualizerWidget` renders as a soft purple ambient glow (intended; see artifact #4 below).

## B. Windows-native — PASS (code present + runtime-confirmed)
All paths statically verified present (with existing unit tests where noted); runtime-observable ones confirmed live:

| Check | Result | Evidence |
|---|---|---|
| Acrylic apply() honest status | ✓ | returns real `SetWindowCompositionAttribute`/HRESULT, not unconditional True (`blur/_dwm.py`) |
| SMTC media keys + flyout card | ✓ | **`SMTC registered (hwnd=262768)`** at runtime (uses `winrt`, present) |
| SMTC Next/Prev grey at boundaries | ✓ | single-track disables Next, empty disables both — `test_media_controls_windows.py` |
| Taskbar overlay badge | ✓ | real `ITaskbarList3.SetOverlayIcon` exercised live: play/pause HICONs built (non-null), clears on stop, **never caches HICON(0)** (retry-on-state-change) |
| Toasts replace-in-place + identity | ✓ (static) | Tag+Group; runtime AUMID resolve (unpackaged + MSIX) `notifications/_windows.py` |
| Sleep prevention | ✓ | `inhibit()` succeeds live; `ES_CONTINUOUS\|ES_SYSTEM_REQUIRED`, **no `ES_DISPLAY_REQUIRED`** (screen free to sleep); `test_power.py` |
| Single-instance + foreground | ✓ | frozen build: 2nd instance **exits 0**, 1st survives; `force_foreground` via AttachThreadInput |
| AUMID + Start-menu identity | ✓ | **`start-menu shortcut + AUMID stamp OK`** at runtime; `.lnk → …\.venv\Scripts\jellytoast.exe`; MSIX-skip path present |
| No console window (frozen) | ✓ | `jellytoast.exe` PE **Subsystem=2 (GUI)**; `console=False` in spec |
| Launch-on-login | ✓ | live: `enable()` writes `HKCU\…\Run = "…\jellytoast.exe"` (launcher, **no console flash**), `disable()` removes, `is_enabled()` correct (restored to original) |
| libmpv (frozen) | ✓ | `_internal/libmpv-2.dll` (112 MB) bundled; source run decodes FLAC + cover art |
| Frameless chrome / native sizing frame | ✓ (runtime flag + static) | `chrome: … win_frameless=True native_border=False platform=windows`; WM_NCCALCSIZE clamp + 1px auto-hide sliver + hittest present (`win_frameless.py`) |

## C. Features — PASS (smoke all-pass on live data)
`dev/smoke_test.py` (after the encoding fix below): **all checks passed** — provider auth, search (songs/albums/artists), library tabs (**219 genres**), instant-mix, genre radio, **smart-shuffle anti-clustering on LIVE data (smart 0.000 vs classic 0.013, 200 songs)**, smart-playlist resolve (354/354 in-window), stream (FLAC **206**), cover serve. Playback paused/loaded correctly; libmpv FLAC decode + embedded cover.

## D. Historical Windows-fragile spots — all re-verified
- Acrylic apply() unconditional-True → **now honest** (code + tests).
- SMTC always-enabled at boundaries → **greys out** (unit-tested).
- Taskbar HICON(0) cached → **never caches NULL, retries** (code + exercised live).
- Dark Acrylic veil too light → **dark heavier** (`0xBE` vs `0x99`) (code + visual).
- Popup double-veil → **elevated surfaces request `0x01` alpha** (code).
- Qt vs native maximize mismatch → **both clamp to work area** (code + visual edge-to-edge).
- **Top/left edge resize jitter (QTBUG-40578)** — native sizing frame is present, but per this box's prior conclusion the residual left/top vibration is a **known unfixable Qt limitation**; needs a human hand to judge feel. Not re-investigated (per that standing conclusion).

---

## Harness artifacts (NOT app bugs)
The gallery surfaced three alarming-looking things that turned out to be the QA rig, not jellytoast. Caught by adversarial re-verification (live bridge introspection + the real-app code path).

1. **Light-theme toolbar icons looked invisible** — `qa_harness.py set_theme()` emitted `theme_changed` after `ui_helpers.refresh_theme()` but **without `icons.refresh_theme()`**, so the toolbar re-issued its glyphs from the *previous* theme's tint (off-by-one lag → dark-grey `#a8a8a8` glyphs on light frost). The real app deliberately calls `icons.refresh_theme()` before the emit (`settings_dialog._on_theme_changed`, with a comment "values must already be fresh when the first slot fires"). Sampling the live button pixmaps proved the lag with the harness flow and **no lag** with the real flow. **FIXED** in the harness; re-capture shows correct dark icons in light theme. *(Affects every platform's harness light captures.)*
2. **Smoke test "crashed"** — `dev/smoke_test.py` prints a `─` (U+2500) section header that throws `UnicodeEncodeError` on Windows' default cp1252 stream before any check runs, so the harness captured a traceback instead of results. **FIXED** (force UTF-8 in the harness subprocess + `smoke_test.py` stdout reconfigure); re-run = all-pass.
3. **Stale "Albums" title on non-album views** — the harness navigates via the internal `_show_genres_view()` / `_show_songs_view()` methods, which (by design) do **not** call `set_active_tab`; only `_on_tab_requested` (the dropdown a real user clicks) does. Confirmed live: direct `_show_genres_view()` → label stays "Albums"; `_on_tab_requested('Genres')` → label "Genres". Real users see correct titles. *(Harness nav design; noted, not fixed.)*
4. **Now-playing "garbled lavender blob"** — the `VisualizerWidget` clipped by the window's bottom edge in the surface capture; un-clipped it's a soft ambient glow (intended).
5. *(minor)* Harness leaves the Settings dialog open after the `--dialogs` pass (its `activeModalWidget()` close didn't catch it across runs) — a rig-cleanup nit.

## P3 candidates (real, minor/cosmetic — want a human eyeball)
- **Missing-cover fallback inconsistency** — on the Suggestions/home "LATEST" shelf, art-less tiles render as blank dark squares with **no fallback glyph**, while the Artists grid shows a star placeholder. Cosmetic.
- **Songs view album column** reads noticeably dimmer than the title/artist columns (legible, but the hierarchy step is a touch strong).
- **Smart-playlists empty state** says click "+ New smart playlist" — verify that create affordance is discoverable on the surface.
- *(carryover from macOS pass, still open cross-platform)* now-playing-bar long-album subtitle ellipsis (not reproduced here — subtitles were short); `disk_cache` songs.json write failure (did not surface here, not investigated on Windows).

## Couldn't test on this box
- **Single 125% display** → multi-monitor maximize + **150% / 175% badge crispness** untested.
- **Taskbar auto-hidden** → taskbar badge *on the button* not captured (the COM path was exercised and succeeded).
- **Non-admin** → MSIX install / WACK not run (Store 0.1.2 already live); tested the **PyInstaller onedir** frozen shape instead.
- **Live toast visual** (track-change) not captured (intrusive); static + runtime AUMID-stamp verified.
- **Top/left resize-jitter feel** needs a human hand (QTBUG-40578, see §D).
