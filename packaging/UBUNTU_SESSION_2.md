# Ubuntu session — round 2: get the `.deb` flawless (+ round-1 scraps)

**Open this on the Ubuntu box in Claude Code and work top to bottom.** Round 1
verified the app on GNOME/Wayland via **pipx** and produced the fixes below —
but the **`.deb` itself was never successfully launched** (BUG-1 blocked it), so
all the GNOME QA ran on the pipx build, not the package. Round 2's headline goal:
**prove the *fixed* `.deb` works flawlessly** across Ubuntu versions and session
types, then close the handful of items round 1 couldn't exercise.

Round-1 fixes now on `main` (need real-`.deb` validation):
- **#148** — stop bundling libmpv's host dep closure (BUG-1: `.deb` aborted at
  launch on Ubuntu >22.04 with `GLIBCXX_3.4.32`/`MOUNT_2_40` OSErrors → no audio).
- **#149** — `.deb` `Depends: libxcb-cursor0` (BUG-2: X11/xcb session crashed).
- **#151** — serialize cast-discovery cold-imports (BUG-3: AirPlay 2 import-lock
  deadlock on Python 3.14).
- **#152 / #153** — GNOME faux-frost + frameless chrome (verified by screenshot
  on pipx in round 1; re-confirm via the `.deb`).

> **Session gotcha (carry-over):** `sudo` has no TTY in the Claude session — use
> **`pkexec`** (GNOME polkit dialog) for apt/dpkg, or run those lines yourself.

---

## Get a fixed `.deb` to test
**v0.1.1 is released** (2026-06-17) and its assets include
`jellytoast_0.1.1_amd64.deb`, built from post-fix `main` — so it carries
#148/#149/#151/#152/#153. (v0.1.0 is the pre-fix release; its `.deb` is the
broken one and was never published to that release.)

- **Preferred — download the released v0.1.1 `.deb`** (the real shipping artifact):
  ```bash
  gh release download v0.1.1 --pattern 'jellytoast_*_amd64.deb'
  ```
- **Or build one from current `main`** (fallback — e.g. to test a fix not yet
  in a release):
  ```bash
  pip install . pyinstaller
  pyinstaller packaging/pyinstaller/jellytoast.spec --noconfirm
  bash packaging/deb/build_deb.sh 0.1.1-test    # → dist/jellytoast_0.1.1-test_amd64.deb
  ```

## Phase 1 — the `.deb` works flawlessly  ⭐ main goal
Exercise on the versions that were broken (this box is **26.04**; also try
**24.04** if available):
- [ ] **Installs clean:** `pkexec apt install ./jellytoast_*_amd64.deb` — the
      `libmpv2 | libmpv1` dep resolves (libmpv2 on 24.04/26.04).
- [ ] **Launches + audio plays (BUG-1 / #148):** app opens with **no** "Missing
      dependency — jellytoast" dialog; `MPV_AVAILABLE=True`; a track plays through
      PipeWire. Confirm the old `libstdc++`/`libmount` symbol OSErrors are GONE.
- [ ] **No glib/GIO noise:** check stderr has no `libdconfsettings.so … undefined
      symbol` warnings (same bundled-stale-glib root cause — should be cleared by
      the closure strip).
- [ ] **X11 session (BUG-2 / #149):** log into an Xorg session (or
      `QT_QPA_PLATFORM=xcb jellytoast`) — launches with **no** `libxcb-cursor0`
      "could not load the Qt xcb platform plugin" crash.
- [ ] **Wayland session (GNOME default):** launches; window chrome = the
      frameless custom chrome (#153); body shows the faux-frost fallback (#152) —
      verify these **from the `.deb`** this time (round 1 only saw them on pipx).
- [ ] **Functionality smoke** (proves #148's strip didn't drop anything needed):
      cast (Chromecast), an offline download, lyrics, and the mini-player all
      load and work.
- [ ] **Clean uninstall:** `pkexec apt remove jellytoast`.

## Phase 2 — round-1 scraps (couldn't be exercised last time)
- [ ] **AirPlay 2 on the `.deb`'s Python 3.12 (#151):** BUG-3 was Python-**3.14**
      -specific (pipx's interpreter); the `.deb` bundles **3.12**. Confirm AirPlay 2
      discovery runs with no import-lock deadlock on the `.deb` — validates the
      fix holds and the package path is clean.
- [ ] **Audio device-switching:** round 1 had only one sink. If a 2nd output is
      available, switch the output device in-app and confirm the stream moves.
- [ ] **HiDPI / fractional scaling:** round 1 ran at 1× scale. Raise the GNOME
      display scale (150 % / 200 % or fractional) and confirm icons + text stay
      crisp — no blur, no clipped chrome.
- [ ] **Tray menu / close-to-tray:** round 1 confirmed the StatusNotifierItem
      *registers*; now click-test the tray menu actions and close-to-tray.
- [ ] **Autostart reboot-survival:** round 1 wrote a valid
      `~/.config/autostart/jellytoast.desktop`; actually **reboot** and confirm
      jellytoast starts at login, then toggle it off + reboot to confirm it doesn't.

## Phase 3 — report back
- [ ] Update `docs/TODO.md` — clear the `.deb` smoke-test item now that the
      package is proven, and record any new gaps. Push, and tell the KDE session
      so memory captures the outcome.

## Pushing results back (same conventions as round 1)
- `git pull --rebase` first (the KDE box may have pushed).
- Checklist results → commit to **this** branch (`packaging/ubuntu-round-2`) and
  push → updates this PR.
- Any **code fix** → its own branch off `main` (`git switch main && git pull &&
  git switch -c fix/<thing>`) + its own PR. Don't pile fixes onto this branch.
- **Do NOT merge** any PR without august's explicit OK (`main` is branch-protected,
  squash-only). End commits with the Claude Code co-author trailer.
