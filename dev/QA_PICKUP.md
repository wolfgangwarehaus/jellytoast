# Picking up a QA PR (Windows / Mac box)

How to go from "check the open PR" to driving the live app and posting
findings, without re-deriving the rigging. Platform briefs with WHAT to test:
`QA_SESSION_WINDOWS.md`, `MAC_TEST_SESSION.md`. This doc is the HOW.

## 1. Pick up the PR

```
gh pr list                      # find it (QA PRs carry a checklist in the body)
gh pr checkout <N>
pip install -e ".[dev]"        # ALWAYS re-run: stale editable metadata makes the
                               # app report an old version (wrong update banner,
                               # failing version-consistency test). [dev] pulls
                               # pytest-xdist / pytest-randomly / dbus-next /
                               # ruff — without them the suite can't run as CI does
```

The PR body is the test plan. Read its comments too — later pushes often add
sections (e.g. a `needs:mac` addendum).

## 2. Launch with the test bridge

```powershell
# Windows
$env:JT_TEST_BRIDGE=1; & .\.venv\Scripts\python.exe -m jellytoast
```
```bash
# Mac / Linux (TMPDIR pin is load-bearing for the socket)
TMPDIR=/tmp JT_TEST_BRIDGE=1 python3 -m jellytoast &
```

Drive it with `python dev/jt_ctl.py ping|eval|exec "<python>"` — `win` is the
main window, `app` the QApplication. Full-surface screenshot walks:
`dev/qa_harness.py`.

## 3. Driving gotchas (cost hours; don't rediscover)

- **Each bridge RPC gets a FRESH namespace.** Multi-step state must live in one
  `exec`, or be stashed on a persistent object (`app._qa = {...}`).
- **Appearance changes arm a 10s keep/revert prompt** (theme family, font, …).
  Between two bridge calls it WILL auto-revert and you'll chase a phantom bug.
  Do pick + keep atomically in one exec:
  `import jellytoast.appearance_confirm as ac; win._settings_dlg._family_combo.setCurrentIndex(i); ac._active._do_keep()`
- **Prefer the real widgets** (`win._open_settings('Display')`, then
  `dlg._follow_accent_check.click()`, `dlg._glass_slider.setValue(n)`) over
  setting settings directly — the handlers are part of what's under test.
- **Screenshots need a background driver** (Windows): a foreground terminal
  can't be lifted off the top; run the capture script as a background task and
  foreground the app via `jellytoast.single_instance.force_foreground(win)`.
- **Verify numerically, then eyeball.** e.g. glass depth:
  `theme.body_color_for(theme.get_active_theme(), blur.status(), 'main')`
  gives the effective RGBA; the screenshot confirms the look.
- **Windows: QSettings is the registry.** If writes silently don't stick,
  check `get_settings()._s.status()` — `AccessError` is latched for the
  process; restart the app. A `QSettings()` with no organizationName is a
  black hole on Windows (tests hit this before the conftest fix).
- **Windows: the system accent can be changed programmatically** for
  accent-follow tests: write `HKCU\...\DWM\AccentColor` + call dwmapi
  ordinal 131 (`DwmSetColorizationParameters`) — DWM repaints and broadcasts
  the real `WM_DWMCOLORIZATIONCOLORCHANGED`. Save the params (ordinal 127)
  first and restore after.
- **Run `pytest -n auto -q` from the repo root** (some tests are
  cwd-sensitive), and not while you're mid-QA in the live app.

## 4. Share findings

- Findings + box facts go in a **PR comment prefixed `[windows-instance]` /
  `[mac-instance]` / `[linux-instance]`** — include effective values observed
  (alphas, hexes), not just pass/fail.
- Check off your platform's boxes by editing the PR body / comment
  (`gh pr view <N> --json body`, edit, `gh pr edit <N> --body-file …`).
- Test-infra fixes for your own platform: commit to the PR branch directly
  (this box owns its platform's fixes); flag anything cross-platform in the
  comment instead.
- Squash-merge only when every platform's section is checked (or split).
