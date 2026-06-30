# 0.1.5 QA — Ubuntu platform brief (.deb / X11 / GNOME)

Read `QA_SESSION_COMMON.md` first. The point of THIS box is the **non-KDE Linux**
path: the **no-blur near-opaque fallback**, MPRIS, XDG autostart, and a **clean
`.deb` install on a current Ubuntu** (the dependency-closure bugs only show on a
fresh box). Screenshot: harness auto-detects `gnome-screenshot` → `scrot` →
`import`; on GNOME, `gnome-screenshot -f {path}`. Launch: `python3 -m jellytoast`
(or the installed `jellytoast`).

Test in BOTH a **Wayland** and an **X11/Xorg** GNOME session if you can (log out
→ pick the session at the login screen) — blur status + window decorations
differ.

## B. Linux/Ubuntu-native checks
- [ ] **No-blur fallback body** (`theme.py` `body_color_for`, `_faux_frost.py`):
      Frosted theme on GNOME/X11 → body is a **legible near-opaque dark panel**
      (~92%), NOT see-through-broken. Faux-frost adds soft bloom + grain. Text
      fully legible. `JT_BLUR_FORCE=unsupported` forces this on any box to check.
- [ ] **MPRIS** (`media_controls/_mpris.py`): `playerctl play/pause/next/previous`
      work; GNOME's top-bar/lock-screen media controls show title + art; hardware
      media keys work. `dbus-send --session --print-reply /org/mpris/MediaPlayer2
      org.mpris.MediaPlayer2.Player.PlayPause` toggles.
- [ ] **Tray** (`tray.py`): icon visible (needs a StatusNotifier/AppIndicator host
      — GNOME may need the AppIndicator extension); left-click toggles mini;
      **right-click = ONE frosted menu** (no double on Linux); items work.
- [ ] **XDG autostart** (`autostart/_linux.py`): Settings → Launch on login →
      `~/.config/autostart/jellytoast.desktop` appears with
      `X-GNOME-Autostart-enabled=true`; disable → set false; survives reboot.
- [ ] **Window decorations**: under GNOME/Mutter the window has server-side
      titlebar + edges; fullscreen hides it and restores on exit (no black flash).
- [ ] **Keep-above**: on X11 the mini player stays above via Qt's native
      `WindowStaysOnTopHint` (keep_above backend is a no-op on X11). On a
      non-KWin **Wayland** compositor it may not stay above — note if so.
- [ ] **.deb install on current Ubuntu** (`packaging/deb/`): build →
      `sudo apt install ./dist/jellytoast_*.deb` → installs to `/opt/jellytoast`,
      `/usr/bin/jellytoast` symlink, .desktop + icon registered → launches from
      the app menu. Then the two closure bugs below.

## D. Re-verify these historically-Linux-fragile spots
- [ ] **#148 bundled-libmpv shadows host on Ubuntu 24.04/26.04** ("GLIBCXX_3.4.32
      not found"): install the `.deb` on a box **newer than the build host** →
      launches; `ldd /usr/bin/jellytoast | grep libmpv` resolves to the host
      `/usr/lib/.../libmpv.so.2`, not `/opt/jellytoast/_internal`.
- [ ] **#149 missing `libxcb-cursor0` in Depends** → on a **clean/minimal** Ubuntu
      X11 session the app must launch (not abort "Could not load the Qt platform
      plugin xcb"). `dpkg -s libxcb-cursor0` → installed; the full xcb DT_NEEDED
      closure is in `Depends`.
- [ ] **Frosted renders see-through on X11/GNOME** — body must use the near-opaque
      fallback (236), not the glass alpha (172). Confirm legible; `blur.status()`
      is UNSUPPORTED (GNOME) / REQUESTED_UNVERIFIABLE (KDE X11).
- [ ] **Autostart ignored by GNOME** — entry includes `X-GNOME-Autostart-enabled`;
      shows in GNOME Startup Applications per the setting.
- [ ] **Tray double-menu** — confirm single on right-click (Linux gate).

## Notes / gotchas
- Blur status: X11 = REQUESTED_UNVERIFIABLE → fallback body; GNOME Wayland =
  UNSUPPORTED → fallback body; only KDE Wayland with Blur on = ACTIVE glass.
- Fresh minimal Ubuntu: no libnotify → notifications silently no-op; no
  PipeWire/Pulse → audio device enum falls back to auto; all Qt xcb libs must be
  pulled by the `.deb` `Depends`.
- Headless smoke under Xvfb: `xvfb-run -a env QT_QPA_PLATFORM=xcb timeout 15
  jellytoast` boots the xcb plugin closure (CI uses this).
- Env: `JT_BLUR_FORCE=unsupported|active|unverifiable`, `JT_OPAQUE=1`.
  `JT_WIN_GLASS_ALPHA`/`JT_MAC_GLASS_ALPHA` are ignored on Linux.
