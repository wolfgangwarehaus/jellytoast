# 0.1.5 QA — Windows 11 platform brief

Read `QA_SESSION_COMMON.md` first. This box has the **largest native surface**
and the most divergence from Linux, so it's the heaviest native pass. Screenshot
(harness auto-detects a PowerShell `CopyFromScreen` of the whole virtual
screen). Launch from source for stderr: `python -X faulthandler -m jellytoast`
(the **frozen `jellytoast.exe` has NO stderr** — diagnostics go to the log;
`JT_LOG_LEVEL=DEBUG` for verbose). For the bridge, set `$env:JT_TEST_BRIDGE=1`
before launching.

Test BOTH where feasible: **source run** (`python -m jellytoast`) AND a **frozen
install** (MSIX from the Store, and/or the Inno `.exe`) — several bugs only show
in the packaged shape (AUMID, toasts, libmpv, no-console).

## B. Windows-native checks
- [ ] **Acrylic blur** (`blur/_dwm.py`): on Win11 (22000+) the Frosted theme
      shows live frosted-glass blur of the wallpaper. Dark reads a heavier veil
      than light. `JT_WIN_GLASS_ALPHA=N` tunes the body; `JT_NO_WIN_BLUR`
      switches to Mica. Win10 → near-opaque (no backdrop).
- [ ] **Frameless chrome + native sizing frame** (`win_frameless.py`): resize
      from **top/left** edges is smooth + atomic (no content-trails-origin
      jitter); maximize via **Win+Up / Aero Snap** fills the work area exactly,
      never covers the taskbar; with **auto-hide taskbar**, the 1px pop-out sliver
      survives at the maximized edge. Multi-monitor: maximize on the 2nd monitor
      uses that monitor's work area.
- [ ] **SMTC media keys + volume flyout** (`media_controls/_windows.py`): hardware
      Play/Pause/Next/Prev work; the Windows volume flyout shows the now-playing
      card (title/artist/album/thumbnail); **Next/Prev grey out at queue
      boundaries** (single-track disables Next; empty disables both).
- [ ] **Taskbar overlay badge** (`taskbar.py`): a crisp play/pause glyph overlays
      the taskbar button; updates with playback; clears on stop; **crisp at 125 /
      150 / 175% scale**.
- [ ] **Toast notifications** (`notifications/_windows.py`): track change → Action
      Center toast with art; subsequent tracks **replace in place** (Tag+Group,
      no stack); toast shows "jellytoast" name + icon (AUMID resolves) under BOTH
      unpackaged and MSIX.
- [ ] **Sleep prevention** (`power/_windows.py`): playing audio keeps the system
      awake (screen may still sleep); pausing restores normal sleep.
- [ ] **Single-instance + foreground** (`single_instance.py`): launching a 2nd
      copy exits immediately and brings the **first window to the actual
      foreground** (not just a taskbar flash); across pip/pipx/MSIX.
- [ ] **AUMID + taskbar/Start identity** (`windows_shortcut.py`): Start → jellytoast
      shows the brand icon (not a generic Python doc icon); taskbar button groups
      under "jellytoast" across all launch shapes.
- [ ] **No console window**: the frozen exe opens no console; source run does.
- [ ] **Launch-on-login** (`autostart/_windows.py`): Settings toggle adds/removes
      the `HKCU\...\Run` value and appears in Task Manager → Startup; no console
      flash at login (targets the gui launcher / `pythonw`).
- [ ] **libmpv-2.dll**: audio plays immediately from the frozen install (DLL
      bundled next to the exe); all formats (FLAC/MP3/…) work.

## D. Re-verify these historically-Windows-fragile spots
- [ ] **Top/left edge resize jitter** (QTBUG-40578) — fixed by the native sizing
      frame; confirm no jitter/trailing. `win_frameless.py`.
- [ ] **SMTC Next/Prev always enabled at boundaries** — confirm they grey out.
- [ ] **Acrylic apply() returned unconditional True** — Acrylic + Mica now both
      propagate real status (visual blur identical either way).
- [ ] **Taskbar badge HICON(0) cached** — a failed badge load retries on next
      state change (never caches a NULL/blank); `SetOverlayIcon` never gets NULL.
- [ ] **Dark Acrylic veil too light** — dark reads heavier than light (0xBE vs
      0x99 tint).
- [ ] **Popup double-veil** (Acrylic tint + QSS frost) — elevated surfaces request
      near-zero Acrylic alpha so QSS frost is the single tint source.
- [ ] **Qt vs native maximize mismatch** — both `showMaximized()` and Win+Up fill
      the work area identically (`WM_NCCALCSIZE` clamps client to work area).

## Notes / env knobs
`JT_WIN_GLASS_ALPHA=N` (body), `JT_WIN_POPUP_BLUR_ALPHA=N` (popup tint),
`JT_NO_WIN_BLUR` (Mica fallback), `JT_NO_WIN_CHROME` (disable frameless + sizing
frame), `JT_NO_START_MENU_SHORTCUT`, `JT_NO_AUDIO_SILENCE` (for audio tests).
SMTC hwnd needs a real HWND → resolved post-show. Taskbar button is created
lazily on the shell's `TaskbarButtonCreated` message; the filter re-applies on
Explorer restart. Under MSIX the OS supplies the AUMID — the manual stamp is
skipped to avoid conflict.
