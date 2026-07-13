# Steam Deck flatpak QA — mission brief (for a Claude running ON the Deck)

You are a Claude Code session running on **august's Steam Deck, in desktop
mode**, in the `~/jellytoast` repo checkout on `main` (`git pull` first).
This is the Deck leg of the 0.2.0 QA program — the Mac and Windows legs ran
the same way (see `dev/MAC_TEST_FINDINGS_0.2.0.md` /
`WINDOWS_TEST_FINDINGS_0.2.0.md` for the house findings style). Your PR
carries the checklist matrix — tick it as you go and report findings there.

**jellytoast** is a PySide6/Qt6 music client for Jellyfin +
Subsonic/Navidrome. Branding is lowercase **jellytoast**.

## Your mission

Fresh-install the **re-rolled 0.2.0 flatpak** (it now carries the #230
Qt-skew fix — blur requests actually reach KWin) and run the full feature
sweep on Deck hardware: functional rows, visual judgement from real
screenshots, bug hunting, and the Deck-specific items no other platform can
test. **Do NOT merge the PR; no Co-Authored-By / attribution trailers on any
commit** (august's standing rule — commits are authored by August only).

## Setup

```bash
cd ~/jellytoast && git pull
# Bridge-client venv (host side; the Deck rootfs is read-only, ~/.local is fine):
python3 -m venv ~/.local/jt-qa-venv && ~/.local/jt-qa-venv/bin/pip install PySide6
```

## Install — the user path (part of the test)

1. Remove any prior install + stale overrides so this is a TRUE fresh install:
   ```bash
   flatpak override --user --unset-env=JT_BLUR_FORCE io.github.wolfgangwarehaus.jellytoast 2>/dev/null
   flatpak override --user --reset io.github.wolfgangwarehaus.jellytoast 2>/dev/null
   flatpak uninstall -y io.github.wolfgangwarehaus.jellytoast 2>/dev/null
   ```
2. Download https://github.com/wolfgangwarehaus/jellytoast/releases/latest/download/jellytoast.flatpak
3. Install by OPENING IT (Discover) — note whether the spurious
   "install failed" toast appears (known Discover sideload quirk; document,
   don't chase). Fallback: `flatpak install --user -y ./jellytoast.flatpak`.
4. **Verify the fix is inside before sweeping** — the wheel's bundled Qt must
   be ABSENT and the KWin grant present:
   ```bash
   flatpak run --command=sh io.github.wolfgangwarehaus.jellytoast -c \
     'ls /app/lib/python*/site-packages/PySide6/Qt/lib/libQt6Gui.so.6 2>&1'
   # expected: No such file (wheel Qt gone → runtime Qt drives KF6)
   flatpak info --show-permissions io.github.wolfgangwarehaus.jellytoast | grep -i kwin
   # expected: org.kde.KWin=talk
   ```
   If either check fails you have a stale asset — STOP and say so on the PR.

## Driving the app

Launch with the test bridge:
```bash
flatpak run --env=JT_TEST_BRIDGE=1 --env=TMPDIR=/tmp io.github.wolfgangwarehaus.jellytoast &
```
Client (host venv): `TMPDIR=/tmp ~/.local/jt-qa-venv/bin/python dev/jt_ctl.py ping`

**Socket plumbing caveat:** the sandbox's /tmp and XDG_RUNTIME_DIR are
namespaced, so the host-side client may not see the QLocalServer socket. If
ping fails, run the CLIENT inside the same sandbox instead:
```bash
flatpak run --filesystem=home --env=TMPDIR=/tmp --command=python3 \
  io.github.wolfgangwarehaus.jellytoast ~/jellytoast/dev/jt_ctl.py ping
```
(One of the two works — the 0.1.9 flatpak QA drove the bridge this way.)
If neither connects after honest effort, fall back to UI-driven testing and
say so in the findings — don't burn the session on plumbing.

Screenshots: `spectacle -b -n -o <path>` full-screen, then READ the PNGs
yourself — you are the visual judge (frost quality, legibility at the Deck's
1280×800 + 1.25 scale, layout).

## The sweep — tick the PR matrix

Run the PR's §B rows (same 17 as the Mac/Windows legs) plus the Deck-specific
§C rows. Highlights and Deck notes:

- **Row 15 is the headline**: blur must be genuinely ACTIVE — desktop visibly
  BLURRED through the window body (not clear glass, not the opaque fallback).
  Cross-check via bridge: `eval "__import__('jellytoast.blur',fromlist=['x']).status().name"`
  and `.reason()`. Screenshot dark + light.
- Sign-in: use the demo button if no server creds are configured on the Deck
  (august's Navidrome may be reachable on LAN — prefer it if signed in
  already from the earlier session).
- Offline/downloads land under `~/.var/app/<app-id>/` — confirm a download
  plays and note the on-disk location works within sandbox limits.
- MPRIS row: Plasma's media widget should show jellytoast
  (`--own-name=org.mpris.MediaPlayer2.jellytoast`); media keys = Deck's
  desktop-mode equivalents, note what's testable.
- Tray: StatusNotifier icon in the Plasma tray; "hide to tray on close" works.
- Start-at-login toggle: writes `~/.config/autostart` via the narrow
  filesystem grant — flip it, verify the .desktop file appears, flip back.
- Keep system volume LOW but nonzero for playback rows.

## §C — Deck-specific rows (in the PR matrix)

- Rendering at 1280×800 with 125% scale: no clipped dialogs, legible text.
- Performance: CPU % (via `top`) while playing FLAC with visualizer OFF and
  ON; note fan behaviour subjectively. This is a handheld — egregious idle
  CPU is a finding.
- Suspend/resume mid-playback (Deck power button): app survives, audio
  resumes or pauses gracefully — no crash, no zombie frost.
- STRETCH (only if all else is done): add jellytoast as a non-Steam app and
  try Gaming Mode launch — document what happens, no fix expected.

## Deliverables

1. `dev/STEAMDECK_TEST_FINDINGS_0.2.0.md` in the house findings style —
   commit it to THIS PR's branch (plain commit, August author, NO trailers).
2. Tick the PR matrix (edit the PR body checkboxes / flip ⬜→✅ or ❌).
3. `gh pr comment` a findings summary.
4. Leave the app installed; remove the qa venv is optional.
