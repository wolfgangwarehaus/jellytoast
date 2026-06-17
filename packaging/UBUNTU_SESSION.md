# Ubuntu session — fresh-install verification

**Open this on the Ubuntu box in Claude Code and work top to bottom**, ticking
boxes as you go. jellytoast's primary Ubuntu artifact is the **`.deb`** (built
on 22.04 for a low glibc floor; depends on the system libmpv). It's also
installable via **pipx** (PyPI) and eventually **Flatpak**. This verifies all
three plus cross-desktop behavior on **GNOME** (Ubuntu's default), which differs
from the KDE/Arch dev box in real ways.

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
host-provided dep closure from the Linux bundle; fix direction proven locally,
pending a full `.deb` rebuild on CI). CI's frozen smoke test never caught BUG-1
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
- [x] Launches — reaches the login flow (`boot-auth: url=empty`),
      `libmpv.so.2.5.0` mapped into the process (mpv backend live). **Plays:**
      not exercised — needs a Jellyfin/Navidrome server + credentials.
      *(GNOME: blur caveat moot — no blur; confirmed in Phase 4.)*

## Phase 3 — Flatpak local build test → ⛔ SKIP (Flathub is parked)
- [x] **Skip this phase.** Flathub is parked: as of 2026-05-28 its policy bars
      *AI-assisted* code (not just AI submissions), and a prior auto-submission
      (flathub/flathub#9022) was already closed as a violation — one strike on
      the account. jellytoast's history is 74% Claude-co-authored, so there's no
      quiet-submission path. The only future route is a transparent, pre-cleared
      "mature, well-maintained project" exception request on Flathub Discourse —
      a deliberate human step, not part of this verification pass. Local
      `flatpak-builder` testing was only prep for that submission, so there's
      nothing to do here. Linux coverage stands on the `.deb` + pipx (above) and
      AUR. Don't spend time on Flatpak.

## Phase 4 — GNOME / Ubuntu cross-desktop QA
*(Run against the **pipx** build — the `.deb` is blocked by BUG-1. Verified
programmatically via D-Bus/process introspection where possible; items needing
real playback, a test server, or visual inspection are marked as such.)*
- [ ] **Audio output picker** — backend stack confirmed **PipeWire +
      WirePlumber**; the picker UI itself isn't exercised without playback
      (needs a server). *Pending server.*
- [x] **MPRIS / media keys** — `org.mpris.MediaPlayer2.jellytoast` **registered**
      on the session bus (Identity "jellytoast"); GNOME's media widget + media
      keys bind to MPRIS, so control is wired. *(Actual play/pause/next with real
      media not exercised — needs a server. `playerctl` not installed.)*
- [x] **System tray** — jellytoast **registers a StatusNotifierItem**
      (`:1.283@/StatusNotifierItem`, its connection also owns the MPRIS name).
      Ubuntu ships **`ubuntu-appindicators@ubuntu.com`** enabled by default, so
      the tray icon appears here without extra setup (vanilla GNOME would need
      the AppIndicator extension, per Context). *(Menu / close-to-tray not
      click-tested — needs UI interaction.)*
- [ ] **Autostart** — keyring/Secret Service present; the "Launch at login"
      toggle is a Settings-UI action, not exercised headlessly. No
      `~/.config/autostart/jellytoast.desktop` yet (expected). *Pending UI.*
- [ ] **Credentials persist** — **Secret Service available**
      (`org.freedesktop.secrets` + `org.gnome.keyring`); the app's
      `credentials.py` keyring **warm-up thread** runs at boot (seen in a
      traceback), so integration is wired and no boot hang observed. Actual
      persist-across-restart needs a real login. *Pending server.*
- [~] **Window chrome** — **Wayland:** app runs on the native `wayland` Qt
      plugin; the BUG-1 error dialog rendered with normal GNOME CSD decorations
      (looked intentional), but the *real* window needs a screenshot to confirm.
      **X11:** ✗ **`QT_QPA_PLATFORM=xcb` crashes** — `libxcb-cursor0` is not
      installed and Qt 6.5+ hard-requires it for the xcb plugin (`Could not load
      the Qt platform plugin "xcb"`). See **Findings → BUG-2**. *Visual Wayland
      check pending screenshot.*
- [x] **Blur degrades cleanly** — **confirmed by runtime log:** `Frosted theme:
      GNOME has no app-controllable window blur — using a near-opaque body
      (unsupported).` So it falls back to near-opaque rather than breaking.
      *(No black/see-through visually confirmed pending screenshot.)*
- [ ] **Mini-player** — needs UI interaction after login (+ screenshot to judge
      always-on-top on GNOME/Wayland). *Pending.*
- [ ] **HiDPI / fractional scaling** — display currently at **1× scale**
      (`scaling-factor 0`; fractional scaling available but inactive), so there's
      nothing to stress without changing the display scale + a screenshot.
      *Pending.*
- [ ] **Cast discovery** — needs real LAN devices; `ufw` status not yet checked.
      *Pending.*

### Findings — Phase 4

**BUG-2 (X11/xcb won't start — missing `libxcb-cursor0`).** Launching with
`QT_QPA_PLATFORM=xcb` aborts: `From 6.5.0, xcb-cursor0 or libxcb-cursor0 is
needed to load the Qt xcb platform plugin … Could not load the Qt platform
plugin "xcb"` → `Fatal Python error: Aborted`. `libxcb-cursor0` is absent on
this box. Wayland is unaffected (uses the `wayland` plugin). The `.deb` should
add **`libxcb-cursor0`** to `Depends` (it bundles the xcb plugin but not this
runtime dlopen dep) so X11/XWayland sessions work; pipx users need it installed
system-wide. Lower severity than BUG-1 (Wayland is the Ubuntu default). *Fix:
TBD — confirm with august whether to open a 2nd PR (deb `Depends`) .*

**Good news:** MPRIS, tray (SNI), Secret Service, and blur→opaque degradation
all work correctly on GNOME/Wayland via the pipx build — the cross-desktop
fallbacks the Context section worried about behave as intended.

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
