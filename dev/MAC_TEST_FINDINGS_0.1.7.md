# jellytoast 0.1.7 (PR #219) — macOS QA pass (findings + evidence)

**Box:** Intel MacBookPro12,1 (Broadwell Iris 6100), macOS Sequoia **15.7.7** via **OCLP 2.4.1** root patches, Retina 2560×1600.
**Server:** live Subsonic/Navidrome (`http://<lan-ip>:4533`, signed in as `<qa-user>`).
**App:** source run (`.venv/bin/python -m jellytoast`, PySide6), driven via the test bridge (`JT_TEST_BRIDGE=1` + `dev/jt_ctl.py`) against the real Settings widgets.
**Date:** 2026-07-02 → 2026-07-03. Autonomous pass + august's hands for OCLP/System Settings.

## TL;DR
**Clean pass — mac column fully verified, PR merged (7bb77b2).** Accent-follow works end-to-end (incl. a real live System Settings flip), glass caps land pixel-exact AND read correctly on real vibrancy (110 jellytoast / 150 preset / slider-verbatim; dark, light, and Dracula all eyeballed), 0.1.7 theming (presets, base16 import, watched folder live re-theme) works live, and the suite went from 41–50 failures + crashed workers to **3152 passed, 16 skipped in 2:21** after keyring/CFPreferences isolation (one real production fix fell out: a SIGSEGV guard in `blur/_macos.apply()`).

The mid-pass "vibrancy is dead, this hardware can't blur" scare was **not the app and not really the hardware**: OCLP's update-prep had silently deleted the graphics accelerator kext (full post-mortem in the July 3 addendum). Re-running OCLP Post-Install Root Patch fixed it; the numeric verification methodology below survived the whole detour unchanged. Test tooling: `dev/mac_test_artifacts/{frost_test,pixel_means}.py`.

---

## Original pass notes (July 2–3, PR comment form)

Ran on the real Intel MacBook Pro (macOS Sequoia 15), from source on this branch, driven through the test bridge against the real Settings widgets.

### Accent follow — 3/4 ✅, 1 needs a 10-second manual flip
- **Enable toggle**: real `_follow_accent_check.click()` → app accent snapped `#967de1 → #007aff` (system blue) instantly. ✅
- **Preset untouched**: on Dracula (kept), a system accent change left the preset accent at `#bd93f9`, follow-toggle preserved, `follow_accent_active()` correctly gated False. ✅
- **Applied at launch**: seeded a stale `#bd93f9` on the builtin family, restarted → boot re-read applied `#62ba46` (the then-current system green). ✅
- **Live change — NEEDS MANUAL CHECK**: I changed the accent via `defaults write NSGlobalDomain AppleAccentColor` + posting `AppleColorPreferencesChangedNotification`. The app's observer **fires** and `_sync_now` re-reads — the plumbing is verified — but `NSColor.controlAccentColor` in the app process kept returning the stale color: AppKit's accent cache only re-resolves on whatever internal invalidation a *real* System Settings change delivers (a fresh process read the new color fine; `NSUserDefaults` in-process saw the new int too). So the one unverifiable link is AppKit-cache-refresh ordering on a genuine change. **august: with the app running and Follow enabled, flip the accent in System Settings once** — if the app follows, check the box; if it applies the *previous* color (one-behind), the fix is re-reading on a 0-delay timer hop after the notification.

### Glass caps — numerically ✅, visual eyeball pending
Effective `body_color_for(...)` verified in-process (blur `ACTIVE`):
- jellytoast Frosted Default: `(18,18,18,110)` — the baked `JT_MAC_GLASS_ALPHA=110`, unchanged.
- Dracula Frosted Default: `(40,42,54,150)` — provisional mac preset cap applies.
- Slider → 230 on Dracula: `(40,42,54,230)` honored verbatim — dead-slider-above-cap fix works on macOS too.
- Reset-to-default: back to `(40,42,54,150)`.
- **Could not eyeball**: `screencapture` has no screen-recording permission in this remote session, so whether 150 *reads* right vs Acrylic-128 is unjudged — needs one look on real glass.

### Reduce Transparency interaction (worth knowing, works as designed)
This Mac had **Reduce Transparency ON** → `blur.status()` = UNSUPPORTED and frosted bodies fall back to near-opaque `(40,42,54,236)` per the HIG guard in `_macos.apply()`. Verified the fallback numbers, then re-tested with it off. Note: the app's `_reduce_transparency()` and accent reads are cached per-process — synthetic `defaults` writes don't propagate until restart (real System Settings toggles post the accessibility notification the observer handles).

### 0.1.7 themes ✅
- Preset picker: Dracula applied live through the real combo + keep path (`#bd93f9` accent, `#282a36` bg).
- base16 import: garbage yaml and mislabeled hex both refused with `Base16ParseError`; a valid 16-slot scheme parses (UI message path covered by the suite).
- Watched folder: dropped `qa-live-test.yaml` in `~/.config/jellytoast/themes/` → appears in the picker as "QA Live Test", applies (`#7aa2f7`/`#1a1b26`), and **editing base0D in the file re-themed live to `#e0af68` within ~2s**. ✅

### Test suite — was 41–50 failed + crashed workers, now `3152 passed, 16 skipped` (matches the Windows box)
Commit `df4453b`, mirroring the Windows isolation pass:
- **Tests were hitting the real macOS Keychain** (no keyring isolation; the 5× retry in `_keyring_get_token` turned a denied prompt into a prompt loop on the desktop). Null backend forced in conftest.
- **QSettings NativeFormat on macOS is CFPreferences** — async cfprefsd writes meant sync() left the store file nonexistent (test_settings_migration) and, worse, **test writes/clears leaked into the user's REAL `com.jellytoast.jellytoast` domain** (cfprefsd keys off the uid, not `$HOME`). Extended the forced-INI redirect to darwin.
- Case-colliding migration scenarios skipped on darwin (default APFS is case-insensitive).
- Platform-pinned the tests that asserted Linux-host behavior (Windows accent dispatch, off-platform blur degrade, `POPUP_OPAQUE_FILL`'s by-design macOS 0.97 bump).
- **One real production fix fell out**: `blur/_macos.apply()` called `widget.winId()` unguarded via `_ns_view` — force-creating the native window on a never-shown widget (the exact contract violation `_dwm` fixed on this branch), and under a non-cocoa QPA (offscreen = CI + the release smoke probe) wrapping that id as an NSView is a **hard SIGSEGV** no try/except catches. This was crashing xdist workers all over the suite. Now guards `windowHandle()` + `platformName() == "cocoa"`.

### ⚠️ Collateral damage on this box (pre-fix runs, now impossible to repeat)
Before the isolation fix landed, the earlier `pytest -n auto` runs **cleared the real preferences domain and overwrote the real Keychain token** (the stored token is now an 8-char test value; `boot-auth: is_auth=False`). Recovered what I could:
- Server URL restored to `http://<lan-ip>:8096` (the QA server, reachable — found via dev docs + LAN probe).
- Covers cache and downloads DB were untouched.
- **august: one re-login needed** (Settings → server) — user id + token are gone. Sorry; the conftest fix makes this class of damage impossible going forward.
- Everything I changed during QA was restored: builtin family + default accent, follow off, system accent key deleted, Reduce Transparency back ON, test scheme file removed, no app instances left running.

### Small notes (P3, non-blocking)
- Reopening Settings after theme/accent churn logged `RuntimeError: Internal C++ object (_AccentSwatch) already deleted` twice from `_refresh_accent_swatches` (settings_dialog.py:4913 → :75) — a stale swatch surviving a page rebuild; cosmetic, worth a guard.
- Switching back to the builtin family with Follow ON does **not** immediately re-sync the accent (it kept the preset's `#bd93f9` until restart). Boot re-read covers it, but a `_sync_now()` on family-switch-to-builtin would close the gap.
- Colors-page "reset" writes the registry default `rgba(67,67,67,0.65)` for `POPUP_OPAQUE_FILL` — on macOS the live value is the intentional 0.97 bump, so a reset briefly under-opaques popups until the next theme refresh.

### July 2 follow-ups — the "needs august's eyes" items are now ✅
- **Live accent flip (the one unverifiable link above)**: verified with a *real* System Settings accent change while the app ran with Follow on — the app followed live, no one-behind. Closed.
- Re-verified on the current server config (now Navidrome/subsonic `http://<lan-ip>:4533`, user <qa-user>, after the re-login): preset glass caps numerically (jt 110 / preset 150 / slider verbatim), base16 import refusal, watched-folder live re-theme.
- Suite still green: `3152 passed, 16 skipped` (`df4453b`, pushed).

### July 3 addendum — the frost saga is CLOSED: dead vibrancy is this box's hardware, not the PR

The July 2 root-cause hypothesis (stuck Increase Contrast / Reduce Transparency latched by WindowServer at login, fix = reboot with clean settings) is **falsified**. Rebooted July 3 with all three stores verifiably clean (System Settings UI, `com.apple.universalaccess` plist, live `NSWorkspace` API — the desync is gone, nothing re-applied them at login, so no perf-script login item is involved). Behind-window vibrancy is still dead system-wide:

- Pure-AppKit `NSVisualEffectView` (behindWindow, state=active) over a synthetic green window: dead-flat neutral grey at every material — hud 0.149, popover 0.173, menu 0.196, sidebar 0.220, underWindowBackground 0.220 mean RGB; zero green leak.
- **Control Center's own panel is fully opaque** over the same green backdrop. Apple's flagship frost doesn't sample either → WindowServer backdrop sampling is dead for everyone, not just Qt/jellytoast.
- Plain window-alpha compositing is fine: a 50%-alpha NSWindow and a `WA_TranslucentBackground` PySide6 window both mix with the green exactly as predicted. Only backdrop *sampling* is broken.

Why: this is a MacBookPro12,1 (Broadwell Iris 6100) on Sequoia via **OCLP 2.4.1 root patches** — `AppleIntelBDWGraphicsMTLDriver.bundle` is the **Monterey 12.5-vintage driver** shimmed in (plus MetallibSupportPkg). Backdrop sampling just doesn't survive that combination. Not the toggles, not the PR, not fixable from userspace. The *aesthetic* vibrancy judgment (does preset-150 read right on real glass) **cannot be made on this machine** — needs any Mac with stock graphics.

What I could and did verify here, quantitatively — the mac glass pipeline end-to-end in the live window (runtime introspection + pixel math):

- Live main window has everything the sibling-below backend promises: `WA_TranslucentBackground` set, surface alpha buffer 8, NSWindow non-opaque with clear bg, `NSVisualEffectView` (material 21 underWindowBackground, state active) sibling-below `QNSView`, Qt backing layer non-opaque.
- Body compositing is pixel-exact against the (dead-flat 0.220-grey) effect view: slider 110 → 0.157 measured / 0.156 predicted; 150 → 0.133 / 0.132; 167 → 0.122 / 0.122. So the 110-vs-150 cap difference **does land on screen** correctly on macOS.
- Slider-wins-over-cap precedence confirmed live: august's explicit 167 renders at 167 (not capped to 110) — matching the numeric suite results from the 28th. (Settings restored to 167 afterwards.)

One design note that fell out (P3): `blur.status()` on this box reports ACTIVE — *technically* true (the effect view is installed and composited) — but sampling is dead, so the frosted body rides a flat grey instead of real frost, and the faux-frost album-art fallback never engages because status can't see dead sampling. A KWin-style pixel-verify probe would catch OCLP-class machines and give them the (nicer) faux-frost. Legible either way; nothing renders broken.

Also closed today, via the test bridge against the real widgets:
- **Font picker (0.1.6 sanity)**: live-change ✅ (combo → American Typewriter applied app-wide instantly), **auto-revert** ✅ (10s countdown restored font + combo + setting untouched), **Keep** ✅ (survived past the countdown, persisted to settings). Restored to System default after.
- **Album grid / cover art** ✅ — loads over the Navidrome library (screenshots on file).

### Mac wrap-up
Every mac checklist item is verified except one that this machine physically cannot judge: whether preset-150 *reads* right on real vibrancy. Backdrop sampling is dead at the WindowServer level on this OCLP box (even Control Center is opaque), so that call needs a stock-graphics Mac — or ship the numerically-verified caps (110/150/slider-verbatim, pixel-exact on screen) and tune by eye post-merge if a real-glass Mac ever materializes. Everything app-side is proven correct. Mac side: **done**. Squash-merge whenever the Windows column agrees.

### July 3 (afternoon) — RESOLVED: the "hardware can't blur" conclusion above was WRONG
Real root cause: June 28 OCLP auto-agent ("Preparing host for macOS update to 26.5.1") **deleted `AppleIntelBDWGraphics.kext`** from `/Library/Extensions`; the update never installed so the kext was never restored. No accelerator → no Metal → WindowServer drops backdrop sampling system-wide. OCLP + Broadwell blurs fine with patches intact — the earlier "needs a stock-graphics Mac" verdict (and the stuck-toggles and MacTweaks theories before it) are all corrected in the PR thread.

Post-fix (august re-ran OpenCore-Patcher Post-Install Root Patch + reboot), re-verified:
- `AppleIntelBDWGraphics` 18.0.8 loaded, Metal 2 back in `system_profiler` (line was absent before).
- Green-backdrop probe: fx region mean RGB 0.082/0.651/0.086 (backdrop 0.992 green) vs dead-flat 0.149 pre-fix — sampling alive.
- **Real-glass eyeball done**: A/B at 110 vs 150 (relaunch per value, screenshots in session scratchpad). 150 reads correctly — deeper than 110, real frost, content legible. No cap tune needed; ship 110/150/slider-verbatim as-is.
- Settings restored: slider 167, follow-accent on, app left running. Recurrence guard: consider disabling automatic macOS updates (softwareupdated staging the Tahoe upgrade is what triggered the kext deletion).

Closure comment: https://github.com/wolfgangwarehaus/jellytoast/pull/219#issuecomment-4877472351. No repo issues needed closing (#197 mac-blur P3s already closed; nothing else related).

### July 3 (final) — post-repatch retests, checklist ticked, MERGED
- Suite on the healthy graphics stack: **3152 passed, 16 skipped in 2:21** (`pytest -n auto`).
- **`frosted_light` eyeballed on real vibrancy for the first time** (June 28 evidence was dark-only): bright glass, wallpaper wash pulls through, dark text legible. ✅
- **Dracula frosted eyeballed live** (previously numeric-only): cap-150 body reads visibly deeper than builtin, accent stamps controls, vibrancy shows. Applied/reverted via `theme_presets.apply_theme_family` over the bridge; user config restored. ✅
- Tray menu not popped programmatically (native NSMenu run-loop risk) — it rides pure system vibrancy (#197), which is now proven working; one manual glance suffices.
- Checklist comment 4869868589: all 16 boxes ticked. **PR #219 squash-merged (`7bb77b2`)**, branch deleted, main carries three P3 follow-ups on top (`5e7c5f2`).
- Standing box hazard: automatic macOS updates re-staging the Tahoe upgrade can make OCLP's update-prep delete the accelerator kext again — disable auto-updates or expect a repeat.

