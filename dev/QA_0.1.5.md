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
| macOS (Intel Sequoia) | august's MBP | **testing live** — native blur verified working; pass in progress | pending `/tmp/jt_mac_findings.md` |
| KDE Plasma (Wayland) | CachyOS primary | rig ready; **next up** | — |
| Windows 11 | laptop | rig ready; brief + PR to delegate | — |
| Ubuntu (.deb/X11) | Ubuntu box | rig ready; brief + PR to delegate | — |

## macOS — already fixed on `feat/macos-native-blur` (queued for 0.1.5)
- Native NSVisualEffectView blur via **sibling-below** (fixes blank/mis-draw on
  resize + activation that had it disabled).
- Frosted body over vibrancy tuned to `JT_MAC_GLASS_ALPHA=110` (Plasma-matched).
- Tray right-click **double-menu** fixed (`tray.py`, gated to `IS_LINUX`).

## Findings — fill in per platform (P1 blocker / P2 should-fix / P3 polish)

### macOS
_(from the Mac session — paste/triage here)_

### KDE Plasma
_(pending)_

### Windows
_(pending)_

### Ubuntu
_(pending)_

## 0.1.5 fix list (cross-platform, triaged)
_(populated as findings land; one line per fix → PR/commit)_

## Release gate
- [ ] All four platforms' P1s fixed + re-verified on hardware.
- [ ] P2s triaged (fix-now vs defer to 0.1.6).
- [ ] macOS blur branch merged to main (if shipping in 0.1.5).
- [ ] CHANGELOG `[Unreleased]` updated with the user-facing wins.
- [ ] `dev/cut_release.sh 0.1.5 --push` → draft release → publish.
