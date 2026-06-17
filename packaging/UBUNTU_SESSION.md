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
- [ ] ~~Launch from the app grid; **audio plays**~~ — **BLOCKED: launch fails.**
      App aborts at startup with a "Missing dependency — jellytoast" dialog
      ("jellytoast requires libmpv…") and `sys.exit(1)`. **Root cause is NOT a
      missing system libmpv** (libmpv2 is installed & loadable). See
      **Findings → BUG-1** below. Blocks the rest of Phase 1 + all of Phases 2–4
      (the app never reaches its UI). Fix tracked in a separate PR off `main`.
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
loads it by path — not the cause.)*

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
- [ ] `pipx install jellytoast` then run `jellytoast`
- [ ] Launches + plays. *(Kubuntu/KDE only:* pipx's PyPI Qt can't see the system
      KF6 KWindowSystem plugin → blur unsupported; fix via `QT_PLUGIN_PATH` or
      `pipx install --system-site-packages`. On GNOME this is moot — no blur.)

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
- [ ] **Audio output picker** works (PipeWire on 24.04, PulseAudio on 22.04);
      switching devices works.
- [ ] **MPRIS / media keys** — keyboard play/pause/next + GNOME's media widget
      control the app.
- [ ] **System tray** — if the icon is missing, install
      `gnome-shell-extension-appindicator`; confirm tray menu + close-to-tray.
- [ ] **Autostart** — Settings → "Launch at login" writes
      `~/.config/autostart/jellytoast.desktop`; survives a reboot.
- [ ] **Credentials persist** across restart (gnome-keyring / Secret Service);
      no boot hang waiting on the wallet.
- [ ] **Window chrome** on Wayland looks intentional (decorations, no boot
      flash); then sanity-check X11 (`QT_QPA_PLATFORM=xcb jellytoast`).
- [ ] **Blur degrades cleanly** to opaque — no black/see-through panels.
- [ ] **Mini-player** opens; note whether always-on-top holds on GNOME/Wayland.
- [ ] **HiDPI / fractional scaling** — icons + text stay crisp at the GNOME scale.
- [ ] **Cast discovery** — if devices don't appear, check `ufw` (default-deny
      silently kills AirPlay/DLNA discovery — Chromecast surviving is the tell;
      the in-app ⓘ pre-fills the `ufw allow from <LAN>/24` rule).

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
