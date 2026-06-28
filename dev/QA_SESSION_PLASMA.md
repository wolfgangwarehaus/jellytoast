# 0.1.5 QA — KDE Plasma (Wayland) platform brief

Read `QA_SESSION_COMMON.md` first. This is the **primary dev platform**
(CachyOS/Arch, KDE Plasma 6 Wayland) and the visual reference the other OSes get
measured against — so be thorough on the blur/chrome. Tools needed:
`kwindowsystem` (KF6), `kwriteconfig6`/`kreadconfig6`, `qdbus`, `spectacle`,
`jeepney`. Screenshot: **`spectacle -f -b -n -o {path}`** (full composite,
background, no-notify) — the harness auto-detects this. Launch:
`TMPDIR=/tmp python3 -m jellytoast`.

## B. KDE-native checks
- [ ] **KWin blur** (`blur/_kwin.py`): main window + mini player + dialogs read
      as **true translucent glass** with the desktop blurred behind. Toggle
      System Settings → Desktop Effects → **Blur off** → bodies fall back to
      near-opaque (~92%); turn it back on → glass returns. (`blur.status()`
      ACTIVE ↔ UNSUPPORTED.)
- [ ] **Borderless chrome**: with "Use native window border" OFF, main + mini +
      Settings show **no titlebar** (KWin noborder rule), rounded 8px corners,
      top bar acts as the titlebar. Toggle native border ON → titlebar appears.
- [ ] **Drag-repaint** (`drag_repaint/`): drag the main window continuously while
      a cover/video shows — **no stale translucent rectangle** at the drag-start
      position (KWin NVIDIA blur bug 455526/457727). Most relevant on NVIDIA.
- [ ] **Keep-above**: mini player stays **above** the main window when you click
      the main; toggle "Always on top" off → it falls behind; on → returns.
      (KWin rule in `~/.config/kwinrulesrc`, not Qt hint — Qt's
      `WindowStaysOnTopHint` is a no-op on Wayland.)
- [ ] **Smooth scroll**: mouse-wheel scrolling the library animates smoothly (no
      notch-jumps); trackpad is direct. Rapid spins coalesce (no stacked anims).
- [ ] **Popup/combobox translucency**: volume popup, dropdowns, menus render
      frosted over the blur; with blur off they go near-opaque (not see-through).
- [ ] **Titlebar double-click**: double-click the top bar → your configured KWin
      action fires (maximize/etc., from kwinrc); vertical-max fallback if unbound.
- [ ] **Screen color picker**: Settings → EQ (track playing) → eyedropper → KDE
      portal picker opens, click a pixel → hex returns (jeepney D-Bus round-trip).
- [ ] **Tray**: left-click toggles mini; **right-click shows ONE frosted menu**
      (no double); all items work; now-playing label updates live; Quit stops
      audio + closes everything.

## D. Re-verify these historically-KDE-fragile spots
- [ ] **NVIDIA stale-blur line on drag** — fixed by the scripted effect; confirm
      dead (drag while playing). `drag_repaint/effect/.../main.js`.
- [ ] **Blur drops on undecorated window during move** — mini/Settings use SSD +
      noborder rule (not Qt frameless) so blur persists while dragging; confirm.
- [ ] **`WindowStaysOnTopHint` ignored on Wayland** — keep-above is via KWin rule;
      confirm mini stays above; rule not duplicated on relaunch (idempotent UUID).
- [ ] **QSettings destructor flush unreliable on tray Quit** — change a setting,
      Quit from tray, relaunch → it persisted (explicit `flush()` after writes).
- [ ] **kwalletd6 slow on cold boot (8–10s)** — app launches fast on first boot;
      credentials load in background, never block startup.
- [ ] **Combobox/tooltip quirks under translucent parent** — dropdowns still
      function; custom tooltip pill renders (not Qt's broken native QTipLabel).

## Notes / gotchas
- Blur detection dual-confirms kwinrc `[Plugins] blurEnabled` + the KWin D-Bus
  report. Window rules (keep-above, noborder) are idempotent via UUIDs in
  QSettings and re-applied on launch.
- Rounded corners (8px) are painted by the app on frameless surfaces (KWin
  strips chrome entirely via noborder).
- `JT_BLUR_FORCE=unsupported|active|unverifiable` pins blur status to eyeball
  the fallback body without touching the compositor. `JT_OPAQUE=1` = fully
  opaque chrome.
- Test drag-repaint on the **NVIDIA** path specifically if this box has one.
