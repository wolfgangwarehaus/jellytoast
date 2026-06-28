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
| Windows 11 | laptop | rig ready; brief + PR to delegate | — |
| Ubuntu (.deb/X11) | Ubuntu box | rig ready; brief + PR to delegate | — |

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

### Windows
_(pending)_

### Ubuntu
_(pending)_

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
