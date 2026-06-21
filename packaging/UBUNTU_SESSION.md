# Ubuntu session — fresh-install verification

**Open this on the Ubuntu box in Claude Code and work top to bottom**, ticking
boxes as you go. jellytoast's primary Ubuntu artifact is the **`.deb`** (built
on 22.04 for a low glibc floor; depends on the system libmpv). It's also
installable via **pipx** (PyPI). This verifies both plus cross-desktop
behavior on **GNOME** (Ubuntu's default), which differs from the KDE/Arch
dev box in real ways.

---

## Context — what's expected to differ from the KDE dev box
- **No frosted blur on GNOME.** Blur is KDE-Wayland-only (KWin). On GNOME it
  must degrade to an **opaque** body — verify it doesn't break (no black or
  see-through panels).
- **Borderless chrome** uses a KWin rule on KDE; off KDE expect normal/CSD
  window decorations. Verify the window looks intentional, not broken.
- **System tray** — GNOME 45+ has no built-in tray; the icon won't appear
  without the AppIndicator extension.
- **Mini-player always-on-top** uses a KWin rule on KDE; off KDE it falls back
  to Qt's `WindowStaysOnTopHint` (Wayland may not honor it — note the behavior).

## Phase 1 — install via the `.deb` (primary path)
- [x] Download `jellytoast_0.1.0_amd64.deb` from the v0.1.0 GitHub release.
      *(SHA256 verified against the release `SHA256SUMS`.)*
- [x] `sudo apt install ./jellytoast_0.1.0_amd64.deb` — *installed via `pkexec`
      (`sudo` has no TTY in this session; `pkexec` uses GNOME's polkit dialog).*
- [x] Confirm the **libmpv dependency resolves** — Depends is
      `libmpv2 | libmpv1` (libmpv2 on 24.04 Noble, libmpv1 on 22.04). This is
      the known smoke test: the deb is built on 22.04, so verify it installs
      clean on **24.04** too.
      → **Resolved to `libmpv2 0.41.0-2ubuntu4` on Ubuntu 26.04** (this box is
      26.04 — two LTS newer than the build floor; an even harder smoke test than
      24.04). `dpkg` install + dependency resolution: clean.
- [ ] ~~Launch from the app grid; **audio plays**~~ — **BLOCKED via the `.deb`.**
      App aborts at startup with a "Missing dependency — jellytoast" dialog
      ("jellytoast requires libmpv…") and `sys.exit(1)`. **Root cause is NOT a
      missing system libmpv** (libmpv2 is installed & loadable). See
      **Findings → BUG-1** below. **Fix: PR #148** (off `main`). Phase 4 QA was
      done instead via the **pipx** build (Phase 2), which dodges BUG-1.
- [x] Record the Ubuntu version + session type (GNOME/Wayland vs X11).
      → **Ubuntu 26.04 LTS (Resolute Raccoon)**, session: **GNOME / Wayland**
      (`XDG_CURRENT_DESKTOP=ubuntu:GNOME`, `XDG_SESSION_TYPE=wayland`). Audio
      stack: **PipeWire + WirePlumber** (no `pactl`/pulseaudio-utils installed).

### Findings — Phase 1

**BUG-1 (launch-blocking, all distros newer than the 22.04 builder).**
The `.deb` is a PyInstaller one-dir bundle under `/opt/jellytoast`. python-mpv
(`>=1.0.5`) loads libmpv on Linux strictly via
`ctypes.CDLL(ctypes.util.find_library("mpv"))` — no hardcoded fallback. On 26.04
`find_library("mpv")` correctly returns `libmpv.so.2`, **but the `CDLL` load
fails** because PyInstaller puts the bundle's `_internal/` dir first on the
loader path, and the bundle ships the **22.04 build host's copies** of libmpv's
own dependency closure (libstdc++, libmount, glib, ffmpeg, …). The host's
`libmpv.so.2 → ffmpeg 8` needs newer symbols than those stale bundled libs
provide. Exact errors (reproduced with
`LD_LIBRARY_PATH=/opt/jellytoast/_internal python3 -c "import ctypes; ctypes.CDLL('libmpv.so.2')"`):

```
OSError: /opt/jellytoast/_internal/libstdc++.so.6: version `GLIBCXX_3.4.32' not found
         (required by /usr/lib/x86_64-linux-gnu/libavcodec.so.62)
OSError: /opt/jellytoast/_internal/libmount.so.1: version `MOUNT_2_40' not found
         (required by /usr/lib/x86_64-linux-gnu/libgio-2.0.so.0)
```

`import mpv` raises `OSError` → `MPV_AVAILABLE=False` → the dialog. The bundle
ships **154 libraries that overlap libmpv.so.2's dependency closure**, including
two ffmpeg generations (`libavcodec.so.58` *and* `.so.61`) and the full glib
stack. **Confirmed fix direction:** when those bundled copies are removed so the
deps resolve from the host (guaranteed present — the deb `Depends: libmpv2`),
`CDLL('libmpv.so.2')` → `LOADED OK`. Proper fix = stop bundling libmpv's host-
provided dependency closure in `packaging/pyinstaller/jellytoast.spec` (Linux).
The spec header *intends* "libmpv intentionally NOT bundled," but nothing strips
what PyInstaller's auto-scan drags in. *(Note: the stray bundled
`_internal/libmpv.so.1` from the 22.04 build is dead weight — python-mpv never
loads it by path — not the cause.)* **→ Fixed in PR #148** (strips libmpv's
host-provided dep closure from the Linux bundle; merged + shipped in v0.1.1. The
fix direction was proven locally; an end-to-end launch of the rebuilt `.deb` on a
newer distro is still pending — round-2 checklist in `UBUNTU_SESSION_2.md`). CI's frozen smoke test never caught BUG-1
because it boots the bundle on the **same 22.04** it was built on, where the
bundled libs still match the system.

**Also observed (non-blocking):** GIO module load warnings at every launch
(`libdconfsettings.so … undefined symbol g_assertion_message_cmpint`,
`libgvfsdbus.so …`) — same root cause (bundled stale glib shadowing the host).
And the error dialog rendered with **normal GNOME CSD decorations** (titlebar +
min/max/close), i.e. off-KDE chrome looks intentional — but this is only the
dialog; real window-chrome QA (Phase 4) is blocked until BUG-1 is fixed.

## Phase 2 — install via pipx (PyPI path)
> **Note:** pipx installs the **PyPI wheel** into a venv on the **system
> Python** — it is *not* a PyInstaller bundle, so it has no `_internal/` libs to
> shadow the host's. python-mpv resolves the host `libmpv.so.2` directly (which
> we verified loads fine: `CDLL('libmpv.so.2') → OK`). So pipx is expected to
> **dodge BUG-1**, making it the viable path to run the Phase 4 GNOME QA while
> the `.deb` is broken. *(Requires `apt install pipx` — needs polkit/sudo.)*
- [x] `pipx install jellytoast` then run `jellytoast` — installed `jellytoast
      0.1.0` (venv on system Python 3.14). Verified the venv's python-mpv loads
      the **host** `libmpv.so.2` (`import mpv OK`), so it **dodges BUG-1**.
- [x] Launches **and plays** — logged into a Navidrome server, started a track,
      and confirmed a live PipeWire output stream (`media.name: "… - mpv"`,
      state **running @ 44.1 kHz**). mpv → PipeWire audio path verified.
      *(GNOME: blur caveat moot — no blur; confirmed in Phase 4.)*

## Phase 3 — Flatpak → ⛔ RETIRED (not a channel)
- [x] **Nothing to do.** jellytoast does not ship Flatpak or Flathub (retired —
      see `docs/TODO.md`). Linux coverage stands on the `.deb` + pipx (above) +
      AUR, with the AppImage planned for 0.1.3.

## Phase 4 — GNOME / Ubuntu cross-desktop QA
*(Run against the **pipx** build — the `.deb` is blocked by BUG-1. Verified
programmatically via D-Bus/process introspection where possible; items needing
real playback, a test server, or visual inspection are marked as such.)*
- [~] **Audio output picker** — playback confirmed through **PipeWire +
      WirePlumber** (live `running` mpv stream). Sink enumeration works, but this
      box has **only one sink** (Built-in Audio Analog Stereo), so device-
      *switching* couldn't be exercised. *Needs a 2nd output to fully test.*
- [x] **MPRIS / media keys** — `org.mpris.MediaPlayer2.jellytoast` registered
      (Identity "jellytoast") **and control verified**: an MPRIS `Play` over
      D-Bus flipped status `Paused → Playing` and started the PipeWire stream
      (exactly what a media key / GNOME's media widget does). Full metadata
      populated (title/album/artist + cover-art URL).
- [x] **System tray** — jellytoast **registers a StatusNotifierItem**
      (`:1.283@/StatusNotifierItem`, its connection also owns the MPRIS name).
      Ubuntu ships **`ubuntu-appindicators@ubuntu.com`** enabled by default, so
      the tray icon appears here without extra setup (vanilla GNOME would need
      the AppIndicator extension, per Context). *(Menu / close-to-tray not
      click-tested — needs UI interaction.)*
- [x] **Autostart** — toggling Settings → "Launch at login" **wrote
      `~/.config/autostart/jellytoast.desktop`** with `X-GNOME-Autostart-
      enabled=true` and a correct `Exec`/`Path`. Valid GNOME-enabled entry →
      launches at login (reboot-survival implied; not separately rebooted).
- [x] **Credentials persist** — **verified end-to-end.** Login stores a secret
      via `keyring.backends.SecretService.Keyring` under `jellytoast/access_token`.
      After a full app restart, jellytoast **auto-reauthenticated and reopened 3
      established TCP connections to the Navidrome server** (no re-login prompt),
      and booted cleanly with **no hang on the wallet**.
- [x] **Window chrome** — **Wayland: ✓ confirmed by screenshot.** Clean GNOME
      CSD decorations (dark titlebar, centred "jellytoast", min/max/close),
      integrated with the app's dark theme — intentional, not broken/borderless.
      **X11:** ✗ `QT_QPA_PLATFORM=xcb` crashes — `libxcb-cursor0` missing (Qt
      6.5+ requires it). See **BUG-2** → **fixed in PR #149** (deb `Depends`).
- [x] **Blur degrades cleanly** — **confirmed by log + screenshot.** Log:
      `Frosted theme: GNOME has no app-controllable window blur — using a
      near-opaque body`. Screenshot shows a **solid near-black body** (subtle
      decorative radial), **no black-break, no see-through** — clean fallback.
- [x] **Mini-player** — opens and **looks great** (clean rounded dark card,
      album art + transport, by screenshot). **Always-on-top does NOT hold on
      GNOME/Wayland** — the expected fallback: KWin's on-top rule is KDE-only and
      Wayland ignores Qt's `WindowStaysOnTopHint` (per the Context note). Working
      as designed for off-KDE; not a bug.
- [n/a] **HiDPI / fractional scaling** — display is at **1× scale**
      (`scaling-factor 0`; fractional scaling available but inactive). Nothing to
      stress without raising the display scale; not exercised this session.
- [x] **Cast discovery + casting** — **both work despite `ufw` being active +
      enabled.** Cast to a **Chromecast** succeeded (`pychromecast … Launching
      app CC1AD845`, volume set) — Chromecast survives ufw's default-deny, as the
      doc predicts. **Caveat — BUG-3:** AirPlay 2 (pyatv) discovery prep failed
      once with an import-lock deadlock (falls back to AirPlay-1 zeroconf); see
      Findings.

### Findings — Phase 4

**BUG-2 (X11/xcb won't start — missing `libxcb-cursor0`).** Launching with
`QT_QPA_PLATFORM=xcb` aborts: `From 6.5.0, xcb-cursor0 or libxcb-cursor0 is
needed to load the Qt xcb platform plugin … Could not load the Qt platform
plugin "xcb"` → `Fatal Python error: Aborted`. `libxcb-cursor0` is absent on
this box. Wayland is unaffected (uses the `wayland` plugin). The `.deb` should
add **`libxcb-cursor0`** to `Depends` (it bundles the xcb plugin but not this
runtime dlopen dep) so X11/XWayland sessions work; pipx users need it installed
system-wide. Lower severity than BUG-1 (Wayland is the Ubuntu default).
**→ Fixed in PR #149** (adds `libxcb-cursor0` to the deb's `Depends`; merged + shipped in v0.1.1).

**BUG-3 (AirPlay 2 discovery — import-lock deadlock on Python 3.14, degraded).**
`cast_manager/_airplay.py:discover_airplay()` runs `_probe()` on a pool worker,
which does a cold `from jellytoast import airplay2` (→ pyatv → aiohttp). On
**Python 3.14** (the pipx build's interpreter) that worker-thread cold import
races CPython's import system and trips the new deadlock detector:
`deadlock detected by _ModuleLock('aiohttp.http_exceptions')`. It's caught and
falls back to the AirPlay-1 zeroconf path, so impact is **degraded AirPlay 2
discovery, not fatal** — and likely **3.14-specific** (the `.deb` bundles Python
**3.12**, so the primary path probably isn't affected; needs confirming). Fix
direction: serialize the discovery gateways' cold imports. **→ Fixed in PR #151**
(merged + shipped in v0.1.1) — one shared `cold_import_lock` in `async_io`, held (double-checked) around the
cold import in all four lazy gateways (`airplay2.is_available`,
`cast_manager._ensure_chromecast`, `cast.sonos._ensure_soco`,
`cast.dlna.codec._ensure_async_upnp`). Reproduced 40/40, serialized 0/60, and
all 228 cast/airplay/dlna/sonos tests pass against the patched code.

**Good news:** playback (mpv→PipeWire), MPRIS + control, tray (SNI), Secret
Service credential persistence (survives restart, auto-reconnects), autostart,
mini-player rendering, Chromecast casting, and blur→opaque degradation all work
correctly on GNOME/Wayland via the pipx build — the cross-desktop fallbacks the
Context section worried about behave as intended.

## Pushing results back
Auth is set up via `gh auth login` (HTTPS + git credential helper), so
`git push` and `gh` work non-interactively. Conventions:
- **`git pull --rebase` first** — the KDE/Arch box may have pushed to this branch.
- **Checklist results + findings** → commit to **this** branch
  (`docs/ubuntu-session-checklist`) and `git push`; that updates **PR #147**.
- **Code fixes** for any GNOME/Ubuntu bug you find → DON'T pile them on this
  branch. Branch off main: `git switch main && git pull && git switch -c fix/<thing>`,
  commit, push, and `gh pr create`.
- **Do NOT merge any PR** without august's explicit OK. `main` is branch-protected
  (4 required CI checks, squash-only) — open the PR and stop there.
- End commit messages with the Claude Code co-author trailer (this repo's convention).

## Report back
- [ ] Update `docs/TODO.md` (clears the "Mint/Ubuntu deb smoke test" item +
      records any GNOME-specific gaps), push, and tell the KDE/Linux session so
      memory captures the outcome.
