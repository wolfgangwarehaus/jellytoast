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
- [ ] Download `jellytoast_0.1.0_amd64.deb` from the v0.1.0 GitHub release.
- [ ] `sudo apt install ./jellytoast_0.1.0_amd64.deb`
- [ ] Confirm the **libmpv dependency resolves** — Depends is
      `libmpv2 | libmpv1` (libmpv2 on 24.04 Noble, libmpv1 on 22.04). This is
      the known smoke test: the deb is built on 22.04, so verify it installs
      clean on **24.04** too.
- [ ] Launch from the app grid; **audio plays** (system libmpv → ffmpeg).
- [ ] Record the Ubuntu version + session type (GNOME/Wayland vs X11).

## Phase 2 — install via pipx (PyPI path)
- [ ] `pipx install jellytoast` then run `jellytoast`
- [ ] Launches + plays. *(Kubuntu/KDE only:* pipx's PyPI Qt can't see the system
      KF6 KWindowSystem plugin → blur unsupported; fix via `QT_PLUGIN_PATH` or
      `pipx install --system-site-packages`. On GNOME this is moot — no blur.)

## Phase 3 (optional) — Flatpak local build test (Flathub prep)
- [ ] `flatpak-builder` a local build from `packaging/flatpak/` (runbook in
      `packaging/flatpak/README.md`) and run it. Confirms the manifest before
      the human-only Flathub submission. (Flathub auto-closes AI-authored PRs —
      submit by hand.)

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

## Report back
- [ ] Update `docs/TODO.md` (clears the "Mint/Ubuntu deb smoke test" item +
      records any GNOME-specific gaps) and tell the KDE/Linux session so memory
      captures the outcome.
