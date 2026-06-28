# jellytoast 0.1.5 — cross-platform QA campaign

**0.1.5 = a major bug squish + refinement pass across all platforms.** Each box
runs the SAME extensive pass (`dev/qa_harness.py` + its platform brief); findings
converge here; fixes land on `main`; then `dev/cut_release.sh 0.1.5 --push`.

## How it runs
- **Harness:** `dev/qa_harness.py` — drives the live app via the test bridge,
  captures the real composited screen per surface (dark + light), runs the smoke
  test. Same everywhere; capture backend auto-detected per OS.
- **Briefs:** `QA_SESSION_COMMON.md` (shared) + `QA_SESSION_{PLASMA,WINDOWS,UBUNTU}.md`
  (per-platform native + historical checks). macOS uses `MAC_TEST_SESSION.md` +
  `mac_test_harness.py` on the `feat/macos-native-blur` branch (it also tests the
  new native blur, which is macOS-only).
- **Branch:** `test/cross-platform-qa` (off main) for Plasma/Windows/Ubuntu;
  macOS blur on `feat/macos-native-blur`. Delegation across machines is via
  labelled PRs (`needs:windows`, `needs:ubuntu`); Plasma + the merge-back happen
  on the primary box.

## Status (as of 2026-06-28)
| Platform | Box | State | Findings |
|---|---|---|---|
| macOS (Intel Sequoia) | august's MBP | **DONE** — blur verified on real pixels; P2s + P3s fixed on `feat/macos-native-blur` | #195 / #196 / #197 (all resolved) |
| KDE Plasma (Wayland) | CachyOS primary | **DONE** — 25/25 surfaces glass, no P1/P2; clean | A–Z rail faint (P3, already fixed on blur branch) |
| Windows 11 | laptop | **DONE** — 25/25 surfaces glass (dark+light), all native paths verified, frozen install OK, smoke all-pass; no app P1/P2 | clean (3 harness artifacts, 2 fixed; minor P3s) |
| Ubuntu (.deb/X11) | Ubuntu box | **DONE** — 52/52 surfaces good (dark+light, Wayland+X11), all native paths verified, smoke all-pass; no app P1/P2 | clean (one P3: .deb Qt closure omits libglib2.0-0/libdbus-1-3) |

## macOS — already fixed on `feat/macos-native-blur` (queued for 0.1.5)
- Native NSVisualEffectView blur via **sibling-below** (fixes blank/mis-draw on
  resize + activation that had it disabled).
- Frosted body over vibrancy tuned to `JT_MAC_GLASS_ALPHA=110` (Plasma-matched).
- Tray right-click **double-menu** fixed (`tray.py`, gated to `IS_LINUX`).

## Findings — fill in per platform (P1 blocker / P2 should-fix / P3 polish)

### macOS — pass complete (real Intel Sequoia 15.7.7, Subsonic/Navidrome)
**Verified GOOD:** native frost reads as glass over the desktop in dark AND light
(24/25 surfaces); the #1 historical blank/mis-draw on resize·maximize·fullscreen·
activation is **dead**; mini matches main; tray single menu; single-instance;
clean quit; playback + Control Center metadata; smoke all-pass (incl. smart-shuffle
on live data). Full report + evidence in PR #196 (`dev/MAC_TEST_FINDINGS.md`).
- **P2** see-through on a live *Reduce Transparency* toggle → fixed (#195, `87465f3`).
- **P2** tray left-click double-action on macOS → fixed (#195, `87465f3`).
- **P3 ×8** (corner-radius reset, destroyed-leak, orphan rollback, CGColor log spam,
  subview-warning note, tray QSS no-op note, theme docstring nits, A–Z rail contrast)
  → fixed (#197, `c282b32`). Issue closed.

### KDE Plasma — pass complete (CachyOS/Arch, KWin Wayland; autonomous, 2026-06-28)
Driven via an isolated bridge instance (`JT_INSTANCE_KEY=jt-qa`) alongside the
running app — no disruption to the live instance.
**Verified GOOD (25/25 shots `glass_good`, all legible, all corners intact):**
KWin blur confirmed ACTIVE (`status()=active`, body `(18,18,18,172)` = 67% glass);
every surface (albums/artists/songs/genres/suggestions/radio/downloads/smart-
playlists/search/now-playing) reads as proper translucent frosted glass in **dark
AND light** (light faithful — the F6 harness fix landed); maximized + fullscreen
fill edge-to-edge with no blank margins; mini player + Settings dialog frosted with
rounded corners; **smoke all-pass** (provider auth, 219 genres, smart-shuffle
anti-clustering `0.001 vs 0.016` on live data, FLAC 206 stream, cover serve).
No opaque / see-through / blank / mis-draw anywhere.
- **P3** A–Z fast-scroll rail letters read **faint** (this branch is at the old
  `0.30` ink) — **independently confirms the macOS finding**; the `0.45` bump is
  already committed on `feat/macos-native-blur` (`library_grid.py`, cross-platform)
  and reaches main when that branch merges.
- Note: mini player opened centered-over-main rather than bottom-right (its persisted
  position) — placement, not a render bug. Two instances share QSettings, so the
  theme sweep briefly flipped the live instance's theme too; restored to `auto`.

### Windows — pass complete (Win11 build 26200, 125% single display, live Subsonic/Navidrome; autonomous, 2026-06-28)
Driven via an isolated bridge instance (`JT_INSTANCE_KEY=jt-qa`); full report + evidence in `dev/WINDOWS_TEST_FINDINGS.md`.
**Verified GOOD:** Acrylic blur ACTIVE (`is_supported()`=True, build 26200, transparency on); **25/25 surfaces read as translucent frosted glass in dark AND light** (wallpaper bleed everywhere, dark heavier `0xBE` vs light `0x99`, all legible, corners intact, no opaque/see-through/blank/mis-draw); mini player + Settings dialog frosted (dark+light); maximized/fullscreen fill the work area edge-to-edge. Native: **SMTC registered** (hwnd), boundary-greying unit-tested; **taskbar badge** COM path exercised (play/pause HICONs, no NULL caching); **AUMID + Start-menu stamp OK** at runtime; **autostart** HKCU Run toggle writes the no-console launcher; **sleep inhibit** succeeds (system-only, screen free); **frozen PyInstaller onedir** boots (libmpv bundled in `_internal`), **no console** (PE Subsystem=2), **single-instance** correct (2nd exits 0, 1st survives). **Smoke all-pass** on live data (provider auth, 219 genres, smart-shuffle anti-clustering `0.000 vs 0.013`, FLAC 206 stream, cover serve). All §D historical bugs re-verified fixed.
- **No app P1/P2.** Three gallery scares were **harness artifacts, not app bugs**: (1) light-theme toolbar icons looked invisible — harness `set_theme()` omitted `icons.refresh_theme()` before the emit (off-by-one icon-tint lag; real app calls it) → **FIXED in `dev/qa_harness.py`**; (2) smoke "crashed" on Windows cp1252 (`─` UnicodeEncodeError) → **FIXED** (UTF-8) ; (3) stale "Albums" title on non-album views — harness navigates via internal `_show_*` which bypass `set_active_tab` (real dropdown nav is correct). The now-playing "blob" = the intended `VisualizerWidget` clipped by the window bottom.
- **P3** (cosmetic, want a human eyeball): missing-cover tiles on the Suggestions shelf show no fallback glyph (Artists grid uses a star); Songs album column reads a touch dim; verify the Smart-playlists "+ New" empty-state affordance.
- Couldn't test on this box: multi-monitor maximize + 150/175% badge crispness (single 125% display), taskbar-badge on-button visual (taskbar auto-hidden), MSIX/WACK (non-admin; tested the onedir frozen shape instead), live track-change toast visual.

### Ubuntu — pass complete (Ubuntu 26.04 / GNOME 49, Wayland + X11/xcb, live Subsonic/Navidrome; autonomous, 2026-06-28)
Driven via an isolated bridge instance (`JT_INSTANCE_KEY=jt-qa`); full report + evidence in `dev/UBUNTU_TEST_FINDINGS.md`.
**Verified GOOD:** GNOME has no app-controllable blur → the body is the **intended near-opaque
fallback** (`blur.status()=UNSUPPORTED` on both Wayland and X11/xcb), and **52/52 captured surfaces
read correctly in dark AND light** (frosted + solid), all legible, corners intact, accent consistent,
no opaque-broken/see-through/blank/mis-draw (independent multi-agent visual review, adversarially
verified — 0 issues). Fallback alphas measured: frosted_dark `(18,18,18,236)` ≈92.5%, frosted_light
`(244,244,246,240)` ≈94% — **X11 picks 236, not the 172 glass alpha** (the historical see-through bug).
**MPRIS** fully works: service registered, metadata carries title/album/artist/**artUrl** (GNOME media
controls show track+art), and transport `PlayPause/Next/Previous/Stop` all drive playback. **Autostart**
writes `~/.config/autostart/jellytoast.desktop` with `X-GNOME-Autostart-enabled=true` (enable/disable/
is_enabled all correct). **Decorations** are platform-adaptive: Wayland frameless custom chrome, **X11
server-side titlebar** (confirmed in a real composited `import` capture); fullscreen edge-to-edge, no
black flash. **Tray** builds one menu (8 items), SNI host present. **Mini** keep-above set. **Smoke
all-pass** on live data (auth, 219 genres, smart-shuffle `0.002 vs 0.016`, FLAC 206 stream, cover serve,
smart-playlist 354/354). Queue add/reorder/clear + keyboard arrow-nav confirmed. mpv decode via
pipewire, DirectStream + bit-perfect.
- **§D historical bugs re-verified fixed:** #148 (libmpv NOT bundled → system `libmpv2|libmpv1` dep),
  #149 (full 22-pkg xcb closure declared; boots under xcb), X11 see-through (→236 fallback), GNOME
  autostart flag, tray single-menu object.
- **P3** (packaging completeness): the `.deb`'s explicit Qt closure omits `libglib2.0-0` + `libdbus-1-3`,
  which the bundled `libQt6XcbQpa.so.6` directly DT_NEEDEDs and PyInstaller does **not** bundle. **No
  real-world impact** today (the `libmpv2` Depends pulls both transitively) — but that transitive masking
  is exactly the fragility `build_deb.sh` says it guards against, so the Qt closure should declare them.
  Trivial defensive fix in a separate PR.
- **Couldn't fully verify here:** a true standalone **Xorg** session (box is Wayland-only → X11 tested
  via XWayland); the **Docker** minimal-container `.deb` smoke (no Docker on this box → closure verified
  analytically); tray right-click single-vs-double (Wayland blocks synthetic clicks); offline
  download→cache→play round-trip + live cast transport. `gnome-screenshot` is blocked on GNOME-49
  Wayland → captured via `win.grab()` (faithful: no compositor blur) + `import -window` for the X11 shot.
- **Methodology note:** MPRIS transport must be tested with `dbus-send --print-reply` (or real clients);
  fire-and-forget `dbus-send` is unreliable against dbus-next and falsely looks like dead transport.

## 0.1.5 fix list (cross-platform, triaged)
- [x] macOS native blur (sibling-below) + 110 body + tray fix — `feat/macos-native-blur`.
- [x] macOS P2s: live Reduce-Transparency see-through; tray left-click — #195.
- [x] macOS P3s ×8 incl. A–Z rail contrast (cross-platform) — #197 / `c282b32`.
- [x] **A–Z rail contrast** — Plasma CONFIRMS faint at 0.30; the 0.45 bump rides
      the blur branch to main. Still wants a Windows/Ubuntu eyeball after merge.
- [ ] **Now-playing bar: long album subtitle truncates/overlaps** without a clean
      ellipsis (cross-platform, cosmetic; from macOS pass F4). Needs eyes.
- [ ] **`disk_cache` write failure for the songs view** (`view_cache/songs.json.tmp
      -> songs.json [Errno 2]`) — cache not persisting; real, cross-platform
      (from macOS pass F7). Investigate the atomic-rename / missing dir.

## Release gate
- [ ] All four platforms' P1s fixed + re-verified on hardware.
- [ ] P2s triaged (fix-now vs defer to 0.1.6).
- [ ] macOS blur branch merged to main (if shipping in 0.1.5).
- [ ] CHANGELOG `[Unreleased]` updated with the user-facing wins.
- [ ] `dev/cut_release.sh 0.1.5 --push` → draft release → publish.
