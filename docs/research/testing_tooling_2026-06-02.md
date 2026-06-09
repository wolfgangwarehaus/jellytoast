# Research: testing techniques & tooling for jellytoast (KDE Wayland + PySide6)

> **📍 Status — 2026-06-02 reference note:** the tooling this note recommends
> shipped — the `JT_TEST_BRIDGE` eval socket (`modules/test_bridge.py`) plus the
> `dev/jt_ctl.py` / `dev/jt_drive.py` drivers. Kept as the methodology + tooling
> reference; the live-exercise results it enabled are in
> `docs/live_shakedown_report.md`.

## 1. TL;DR — recommended stack for THIS app

jellytoast is a native PySide6/Qt6 QWidget app on KWin 6.6.5 Wayland with a 85-signal `PlayerBus`, an MPRIS2 surface, objectNames on every widget, a dev-only `JT_TEST_BRIDGE` eval socket, and ~2400 offscreen pytest tests. The single most important reframing from this research: **the "synthetic input is flaky on Wayland" problem applies ONLY to external OS-level injectors (ydotool/libei feeding the compositor) — it does NOT apply to in-process Qt event delivery.** Qt Test "sends internal Qt events. That means there are no side-effects on the machine" ([Qt docs](https://doc.qt.io/qt-6/qttestlib-tutorial3-example.html)), so `QTest`/`qtbot`/the bridge drive real widget interactions deterministically and headlessly. That collapses most of the testing problem onto a path that does not touch KWin at all.

Recommended layered stack, in priority order:

- **Tier 0 — in-process logic/model/signal/widget tests under `QT_QPA_PLATFORM=offscreen` (your existing CI tier, plus three additions):**
  - Add **pytest-qt** (`qtbot.waitSignal`/`waitUntil`/`assertNotEmitted`) to replace the hand-rolled `processEvents()` loops — pin `pytest-qt==4.5.0`, which resolves cleanly against your installed PySide6 6.11.1 ([PyPI](https://pypi.org/project/pytest-qt/)).
  - Add **QAbstractItemModelTester** (already importable via `PySide6.QtTest`) wrapped around `_TracksModel`, `LibraryPaginator`, and the library/songs list models to catch begin/endInsertRows contract violations the hand-written reorder-math tests can't ([docs](https://doc.qt.io/qtforpython-6/PySide6/QtTest/QAbstractItemModelTester.html)).
  - Add **Hypothesis** `RuleBasedStateMachine` over `QueueManager`/`_TracksModel` to fuzz reorder/shuffle/repeat invariants ([docs](https://hypothesis.readthedocs.io/en/latest/stateful.html)).
- **Tier 1 — in-process visual goldens under offscreen:** `QWidget.render()` (already used in `test_visualizer_widget.py`/`test_qss_audit.py`) → **pytest-image-snapshot 0.5.3** for the auto-baseline workflow, diffed with **pixelmatch** AA-aware thresholding ([PyPI](https://pypi.org/project/pytest-image-snapshot/)).
- **Tier 2 — live E2E via the existing `JT_TEST_BRIDGE` + `dev/jt_ctl.py`:** formalize a launch→drive→assert→kill harness. This is the project-native equivalent of Spix; no new dependency.
- **Tier 3 — robustness instrumentation (cheap, high-signal):** `faulthandler.enable()` + a `qInstallMessageHandler` that escalates cross-thread warnings; **psutil** soak-monitoring (already installed); `coredumpctl gdb` + `py-bt` for the embedded-libmpv SIGSEGV class.
- **Tier 4 — real-input smoke (last resort only):** ydotool, with geometry-driven click correction, reserved for proving a genuine click reaches the right widget through the compositor.

The opinionated core: **drive almost everything in-process (QTest/bridge), and treat ydotool as a thin OS-integration smoke layer, not the workhorse.** The research's own live verification confirms button clicks, line-edit typing, QMenu action clicks, and QListView row clicks all deliver correctly under offscreen.

## 2. Real-input automation on KDE Wayland — the verdict

**Verdict ranking for genuine OS-level input: KWin private EIS > ydotool > XDG portal >> AT-SPI/xdotool/wtype/wlrctl.** But the meta-verdict is: minimize how much you depend on any of them, because in-process QTest/bridge driving is more reliable than all of them.

### ydotool — the pragmatic default, with measured caveats
ydotool 1.0.4 is installed, `ydotoold` is running as august (PID 1130), and a live `mousemove` returned exit 0. It works because it injects via the kernel `uinput` subsystem, below the compositor, independent of any Wayland protocol KWin lacks ([repo](https://github.com/ReimuNotMoe/ydotool)).

Three fact-checked caveats materially change how you must use it:

- **Absolute coordinates are NOT trustworthy and MUST be calibrated.** Upstream issues [#195](https://github.com/ReimuNotMoe/ydotool/issues/195) (jumps to 0,0), [#158](https://github.com/ReimuNotMoe/ydotool/issues/158) (lands at half/double — workaround "divide by 2"), and [#250](https://github.com/ReimuNotMoe/ydotool/issues/250) (always top-left corner) are real. **Sharpening from the fact-check:** the half/double offset is primarily a uinput-virtual-device-resolution vs. screen-resolution mapping problem (issue #158's reporter hit the 2× offset on Sway with NO scaling), with fractional HiDPI compounding it. A non-integer scale means the divisor cannot be derived analytically — **measure it with a round-trip** (move to a known target, read back actual cursor position) before trusting any absolute click. Never hardcode pixels: query the target widget's `mapToGlobal()` via the bridge.
- **`type` is raw-scancode / US-QWERTY only.** It ignores the configured layout (issues [#43](https://github.com/ReimuNotMoe/ydotool/issues/43), [#22](https://github.com/ReimuNotMoe/ydotool/issues/22)) and there is a *KDE Plasma 6.2+ layout regression* ([#254](https://github.com/ReimuNotMoe/ydotool/issues/254)). It also performs no window functions and injects to whatever has focus ([#140](https://github.com/ReimuNotMoe/ydotool/issues/140)) — so focus is a precondition you must establish yourself. **Always read the field value back programmatically** (bridge `widget.text()`), never trust a screenshot; "typed-wrong" and "typed-into-nothing" both look plausible. Prefer setting field values via the bridge over synthesizing keystrokes.
- **The uinput access mechanism is fragile-by-dependency.** August is NOT in the `input` group; `/dev/uinput` is `root:input`. The fact-check determined the actual mechanism: a udev `uaccess` tag (shipped by kdeconnect/sunshine rules) + systemd-logind dynamic ACL granting `user:august:rw-` because his seat0 session is `State=active`, with `ydotoold` launched as a systemd *user* service (`/usr/lib/systemd/user/ydotool.service`). **Consequence: this breaks for SSH-only / non-active-seat / lingering sessions** (no active seat → no ACL). Unattended runs over SSH will silently lose input capability. Document this.

On maintenance: ydotool's last *release* is v1.0.4 (2023-01-30), but it has *commits* as recent as 2025-12-22 (mousemove buffer-overflow fix) — maintained at source, dormant on releases; distros ship the release ([releases API](https://api.github.com/repos/ReimuNotMoe/ydotool/releases/latest)). kdotool is actively maintained both ways (v0.2.3, 2026-04-03).

### KWin private EIS — the pixel-accurate escape hatch, now partly verified
`org.kde.KWin.EIS.RemoteDesktop.connectToEIS` is present on this build and **the fact-check went further than prior research and called it live, twice, on the real session: `busctl --user call ... connectToEIS i 0` returned exit 0 with a real libei fd (`hi 5 1`, `hi 5 2`), with NO permission dialog.** So the dialog-free fd handout is now empirically confirmed on KWin 6.6.5. Two honest limits remain: (1) the "no dialog" property is kwin-mcp's framing, not a KDE guarantee — KDE's intended path is portal-mediated ([KWin MR !5496](https://invent.kde.org/plasma/kwin/-/merge_requests/5496)), so it could change between versions; (2) **actually driving click/drag/scroll/key events through that fd into jellytoast was NOT exercised** — getting the fd is necessary but not sufficient, and you'd need a libei client (Python bindings are thin). Verdict: a worthwhile *spike* if ydotool calibration proves too fragile for pixel-accurate drags (track reorder, mini-player drag), borrowing kwin-mcp's client code ([kwin-mcp](https://github.com/isac322/kwin-mcp)).

### AT-SPI — not in scope here
AT-SPI accessibility-driven automation was not part of the gathered findings and is not recommended over the verified paths above; Qt's AT-SPI bridge interaction with offscreen/Wayland is unvalidated for this app and offers no advantage over in-process QTest for a fully-objectNamed widget tree.

### kdotool — the window-management companion (not input)
Worth installing as scaffolding: it find/raise/activate's the jellytoast window and reads exact geometry (to aim ydotool and correct HiDPI), and asserts minimize-to-tray/keep-above/borderless-chrome. It does **not** do input — its own docs point you to ydotool for that ([repo](https://github.com/jinliu/kdotool)).

### XDG RemoteDesktop portal — avoid for automation
Fully present on this KWin, but `Start` triggers a permission dialog — bad for unattended loops. The private EIS path is the no-dialog variant of the same machinery; prefer it ([portal docs](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html)).

## 3. Qt/PySide6 in-process testing — the workhorse tier

This is where most of jellytoast's testing value lives, and it is all deterministic and headless.

**pytest-qt (`qtbot`).** Currently absent (only pytest/xdist/randomly/cov in `pyproject`). Add `pytest-qt==4.5.0` — it declares no upper bound on the Qt binding and resolves cleanly against PySide6 6.11.1 ([changelog](https://pytest-qt.readthedocs.io/en/latest/changelog.html)). Highest-leverage uses:
- Replace manual `processEvents()` loops (`test_visualizer`, `test_qss_audit`) with `with qtbot.waitSignal(bus.track_changed, timeout=2000): trigger()`.
- Use `assertNotEmitted` and `waitSignals(order='strict')` to **regression-guard the race/dup bugs already fixed** (double `load_items`, double-scrobble guard, `_load_gen`) so they stay fixed under pytest-randomly + `-n auto`.
- Enable pytest-qt's Qt-message-as-failure gate with a curated `qt_log_failure_ignore` allowlist for benign Wayland/KWin noise — this is the test-time mirror of the runtime `qInstallMessageHandler`. Budget one session to triage the initial warning backlog before flipping the gate.

**Fixture-coexistence warning (corrected).** Your `conftest.py` already defines a session-scoped fixture **named exactly `qapp`** (line 88) — the *same name* as pytest-qt's built-in. The real risk is **NOT a double-QApplication-init crash** (both fixtures guard via `instance()` checks and reuse, only `warnings.warn` on class mismatch). The real risk is **fixture-name shadowing**: per pytest resolution order, your conftest `qapp` overrides the plugin's, so `qtbot` would receive your bare `QApplication([])` instead of pytest-qt's managed instance. **Fix: rename your fixture (e.g. `qt_app`) or override `qapp_cls` instead**, and let pytest-qt own `qapp`/`qtbot` ([pytest-qt qapplication docs](https://pytest-qt.readthedocs.io/en/latest/qapplication.html)). Validate on a throwaway branch.

**QTest / `PySide6.QtTest`.** Already used in `test_top_bar_library_dropdown.py`. The verified headless hit-test recipes you can reuse directly:
- View row: `QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=view.visualRect(index).center())`
- Menu action: `QTest.mouseClick(menu, ..., pos=menu.actionGeometry(action).center())`

Two corrections to apply:
- For **hover/drag**, construct `QMouseEvent`s and `QApplication.sendEvent()` directly — `QTest.mousePress`/`mouseClick` mishandle release in some cases ([pytest-qt #428](https://github.com/pytest-dev/pytest-qt/issues/428)).
- **`QTest.mouseMove` is the one non-platform-independent primitive:** the QWidget overload uses `QCursor::setPos()` (async, WM-influenced) per the [Qt Wiki](https://wiki.qt.io/Writing_good_tests). Key/click primitives are internal-event and platform-independent; mouse-MOVE is not. Prefer the QWindow overload or direct event sends for moves.

**Offscreen focus/activation (corrected — better than prior research feared).** The earlier worry that "windows don't genuinely activate under offscreen" is **refuted on your Qt 6.11.1**: empirically `qWaitForWindowActive()` returns True, `isActiveWindow()`/`activateWindow()` work, multi-window activation contention resolves, `hasFocus`/`focusWidget`/Tab focus-chain all work, and even `QToolTip` worked. So you CAN rely on activation/focus headlessly on this Qt version — but verify per-test for any focus-sensitive case, since this is Qt-version-dependent and historically flaky. Note: the "offscreen only fully supported on X11" line is a stale ~2013 note, not current QTest docs, and pytest-qt #512 is an Xvfb-CI issue, not an offscreen one.

**QAbstractItemModelTester.** Attach to `_TracksModel`/`LibraryPaginator`/list models during real insert/remove/reorder/reset paths. It catches the *structural* contract bugs (bad `parent()`/`index()`, missing begin/end rows) that crash QListView lazily during scroll — complementary to, not a replacement for, your hand-written reorder-math tests, which catch the semantics it can't ([docs](https://doc.qt.io/qtforpython-6/PySide6/QtTest/QAbstractItemModelTester.html)).

**The bridge as project-native Spix.** Spix's QWidget path was confirmed (from source) to use `QApplication::postEvent` of synthesized QEvents — same *class* as QTest, headless-friendly ([QtWidgetsEvents.cpp](https://github.com/faaxm/spix/blob/master/libs/Scenes/QtWidgets/src/QtWidgetsEvents.cpp)). But it's C++-only with no PySide6 bindings, and your `JT_TEST_BRIDGE` already provides the in-process remote-control value. **Keep the bridge; borrow only Spix's object-path selector idea** for richer find-by-role/text queries in the bridge namespace.

## 4. Visual regression for a blur/HiDPI app

The structural fact that shapes everything: **`QWidget.render()`/`grab()` capture only the widget's own painted pixels — they are PROVABLY BLIND to the frosted blur**, because blur is a KWin compositor effect (`modules/blur` calls `KWindowEffects::enableBlurBehind`). This mirrors your own "window-only grab shows translucent body as flat black" finding. So split into two tiers that test what the other structurally cannot:

**Tier A — in-process render() goldens (CI, blur-blind, deterministic).** Reuse the exact `widget.render(QImage)` primitive already in `test_visualizer_widget.py`. Wrap with **pytest-image-snapshot 0.5.3** for auto-baseline-on-first-run + `--image-snapshot-update` ([PyPI](https://pypi.org/project/pytest-image-snapshot/)). **Version correction:** the suspicion that 0.5.3's "2026-06-02" release date was a date-pickup artifact is **refuted** — the PyPI JSON API shows three distinct microsecond-precise same-day timestamps (0.5.0 → 0.5.1 → 0.5.3 in a ~22-min window), a genuine same-day release burst; pin `==0.5.3` ([PyPI JSON](https://pypi.org/pypi/pytest-image-snapshot/json)).

Three non-negotiable rules for this tier:
- **Never byte-equality.** Offscreen Qt6 font anti-aliasing is non-deterministic across machines/font configs and will flake across the 3.11/3.12/3.13 CI matrix. Diff with **pixelmatch** (`includeAA=false`, threshold ~0.1) so AA pixels are ignored by its YIQ-perceptual model ([pixelmatch](https://github.com/mapbox/pixelmatch)). Confirm the chosen Python port's kwarg names before adopting.
- **Pin DPR, normalize dimensions.** Your documented Wayland DPR drift (`screen_dpr()` varies across launches at fractional scale) would make captures different sizes run-to-run, and pixelmatch hard-requires identical dimensions. Render at a fixed logical size with `devicePixelRatio=1.0` — the visual-tier analog of the `server_px = LOGICAL × 3` cover fix ([Qt HiDPI](https://doc.qt.io/qt-6/highdpi.html)).
- **Mask volatile regions.** now-playing surfaces mix stable chrome with changing data (cover by `image_id`, progress, clock, visualizer). Pre-blank those rects in BOTH images (pixelmatch has no named-mask). This is how the handoff's open favorite-heart / cast-banner eyeball items become assertable.

Optionally add **scikit-image SSIM** (not installed; numpy 2.4.6 is present) as a forgiving second metric for blur/gradient surfaces — gate on a per-region SSIM floor, but **pair with a mean-color assertion** since SSIM tolerates flat color shifts (a wrong-but-flat accent recolor scores high).

**Tier B — full-composite blur goldens (LOCAL, at-the-computer only).** `spectacle -f -b -n` is the only path on this box that captures the real composited blur/corners/translucency (grim is non-functional; `spectacle -a` window-grab renders translucent body as black) ([Spectacle](https://apps.kde.org/spectacle/)). Drive state via the bridge → full composite → crop to an app sub-region via KWin geometry + `magick -crop`. Keep these OUT of CI — they're machine/wallpaper-specific. Launch→capture→kill to dodge the single-instance stray-window footgun.

**Parametrize a small theme matrix** (dark/light × a couple accents × Frosted/Transparent). Accent and theme mode live-apply via `PlayerBus.theme_changed` + `_reapply_accent()`; one golden per surface under-covers and misses exactly the `_reapply_accent`/signal-connects-in-init regressions you've been bitten by.

## 5. Robustness / chaos / leak detection

jellytoast's failure modes are overwhelmingly timing/lifecycle (pool-worker GC SIGSEGV, cast-loop teardown, detached `QGraphicsOpacityEffect`, signaler-set retention) — they only surface after many cycles. Instrument first, then soak.

**Do this NOW (one-liners, currently absent):**
- `faulthandler.enable()` at the top of `jellytoast.main` — converts mystery process death into an attributable Python+C stack. Note: faulthandler has known reentrancy edge cases in heavily-threaded processes ([cpython #116008](https://github.com/python/cpython/issues/116008)); smoke-test `all_threads` dumps before relying on it in a long soak.
- A `qInstallMessageHandler` (currently absent anywhere in the tree) that pattern-matches the cross-thread warnings ("Timers cannot be started from another thread", "Cannot create children for a parent in a different thread") and `os.abort()`s under a `JT_STRICT_QT_MSGS` flag. These warnings fire BEFORE the SIGSEGV — they're the early siren for your entire crash class. Keep it allocation-light/thread-safe; record + `os.abort()` rather than raising on a non-GUI thread ([qInstallMessageHandler](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QObject.html)).

**Soak harness on the EXISTING bridge.** Launch with `JT_TEST_BRIDGE=1`, fire a repeating churn cycle via `jt_ctl` (play→seek→next→enqueue→open mini-player→cast connect/disconnect→navigate→sign-out/in), and sample **psutil** (7.2.2, already installed) `num_fds`/`num_threads`/`uss` every N seconds. Health = metrics **plateau** over a stable cycle, not absolute growth (RSS naturally rises then plateaus; `uss` is the truer leak signal; `num_threads` ratcheting per cycle is the real smell — cast/pool teardown should return to baseline) ([psutil](https://psutil.readthedocs.io/)). Prime fd suspects: QLocalServer sockets, cast-proxy (port 8943), DLNA/snapcast asyncio threads, embedded libmpv, the visualizer `pw-record` tap, QNAM.

**objgraph as second-stage drill-down** (not installed; `pip install objgraph`). Only after psutil flags growth: `show_growth()` across a churn cycle, then `find_backref_chain` on the leaked type. Caveat: it sees Python wrappers, not the C++ QObject heap — a C++ object kept alive only by a Qt parent won't show ([objgraph](https://mg.pov.lt/objgraph/)).

**Hypothesis stateful fuzzing** of `QueueManager`/`_TracksModel` first (pure-model, headless), with a `checkErrors` `@invariant` that fails the step if any Qt warning/in-slot exception occurred (the Schrödinger pattern — re-implement it, it's not pip-installable). Reset models per example and drain async_io, or you reintroduce the singleton/threadpool leaks conftest fights. Only escalate to bridge-driven *live* rules behind a slow/opt-in marker once the model machine is green ([Hypothesis stateful](https://hypothesis.readthedocs.io/en/latest/stateful.html)).

**Native-crash post-mortem: `coredumpctl gdb` + `py-bt`.** valgrind is absent (and is impractically slow/noisy on CPython anyway), so this is the realistic route. It matters because **mpv is embedded as in-process libmpv** (`mpv.MPV(...)`, torn down via `_mpv.terminate()` in `player_backend.py`), NOT a subprocess — an mpv C-level crash or cross-thread `~QObject` takes the whole Python process down with no traceback. Needs debuginfo packages (python-debug, qt6-base debug, mpv debug); gdb/py-bt availability on this box was NOT verified — confirm before relying on it. Pair with the proven stress-loop method (`-n auto` 50–100×, read the `[Thread (pooled)]` faulthandler stack; timing not order, so a green fixed-seed run proves nothing) ([wiki.qt.io profiling](https://wiki.qt.io/Profiling_and_Memory_Checking_Tools)).

**Chaos / fault injection (Toxiproxy or tc-netem).** Degrade the music-server, cast-proxy (8943), and scrobble links to verify the documented-but-not-fault-verified seams: offline-fallback, dual-store auth recovery, Navidrome boot-ping false-positive, Chromecast/Tailscale re-resolve. **Critically: netem-throttle the mpv stream URL to confirm playback I/O never blocks the GUI thread** (the embedded-libmpv risk). Caveats: HTTPS/self-signed/Tailscale complicates a transparent TCP proxy — easiest against plain-HTTP or by toxic-ing the cast-proxy hop; Toxiproxy does NOT simulate DNS/mDNS failures (use firewall rules for Chromecast discovery) ([Toxiproxy](https://github.com/Shopify/toxiproxy)).

**Monkey-storming with ydotool: last resort, nested compositor only.** Random click/scroll/key storms over the window bounds, with the oracle being faulthandler + qInstallMessageHandler + psutil (ydotool can only tell you "it crashed"). Run inside `cage`/`kwin_wayland --nested` so it can't hijack august's real cursor. Prefer bridge-driven random *valid* action sequences (deterministic, reproducible, window-targeted) for nearly all fuzzing.

## 6. What NOT to bother with — and why

- **wtype and wlrctl — hard incompatible with KWin.** Both require wlroots-only protocols (virtual-keyboard / wlr_virtual_pointer / wlr_foreign_toplevel) KWin does not implement; wtype fails with "Compositor does not support the virtual keyboard protocol" (confirmed by [KDE bug 502882](https://www.mail-archive.com/kde-bugs-dist@kde.org/msg1046107.html) and community testing). Zero investment.
- **xdotool / XWayland.** XTEST does not reach native-Wayland surfaces, and forcing `QT_QPA_PLATFORM=xcb` to make it work would disable the exact Wayland subsystems you want under test (KWin keep-above rule, borderless SSD, blur). Not installed; avoid.
- **XDG RemoteDesktop *and* Screenshot portals for automation.** Both have interactive permission dialogs / no guaranteed non-interactive capture — wrong for unattended loops. `spectacle -f -b -n` is the reliable scripted capture path; revisit the Screenshot portal only for a future Flatpak/sandboxed build ([Screenshot portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.Screenshot.html)).
- **Squish and Spix as dependencies.** Squish is commercial and its Qt-Wayland-compositor support ships only in the embedded source package; Spix is C++-only with no PySide6 bindings. Your homegrown bridge already provides their core value — borrow the selector idea, not the dependency ([Spix](https://github.com/faaxm/spix)).
- **needle / pytest-needle.** Selenium/web-only; jellytoast has no QWebEngineView (the native-UI pivot removed it). Ruled out to prevent it surfacing from generic "python visual regression" searches.
- **valgrind.** Absent on this box, and notoriously slow + noisy on CPython (needs a suppression file). `coredumpctl gdb` + `py-bt` is the practical native route.
- **odiff / pytest-image-diff.** odiff (extra non-Python binary) is overkill until a golden corpus reaches hundreds of variants; pytest-image-diff has had no release since ~Mar 2023 vs. pytest-image-snapshot shipping actively. Default to pytest-image-snapshot + pixelmatch.
- **dotool.** Not installed; offers no pointer advantage over ydotool. One correction worth noting: its `mouseto` uses *normalized percentages* (0.0–1.0 over a fixed 0..10000 EV_ABS range), making it resolution/scale-INDEPENDENT by design — verifiably DIFFERENT from ydotool's pixel-absolute model, not "assumed similar" ([dotool source](https://sr.ht/~geb/dotool/)). So IF real-input text/positioning becomes important AND ydotool calibration stays painful, dotool's percentage model is plausibly more HiDPI-robust — but first prefer setting field values via the bridge.

## 7. Prioritized adoption checklist for jellytoast

Ordered by leverage-per-effort. Do all of this in the MAIN session (background agents can't get interactive write permission), launch→kill any GUI instance.

1. **(1 line, zero risk) `faulthandler.enable()`** in `jellytoast.main` right after QApplication. Smoke-test `all_threads` dumps don't destabilize the QThreadPool/asyncio-cast-loop process.
2. **(small) `qInstallMessageHandler`** that records cross-thread timer/child/parent warnings and `os.abort()`s under a `JT_STRICT_QT_MSGS` env switch (consistent with your `JT_*` convention).
3. **(small) Add `pytest-qt==4.5.0`** to `[project.optional-dependencies].dev`. FIRST rename the conftest `qapp` fixture → `qt_app` (or override `qapp_cls`) to avoid shadowing pytest-qt's `qapp`. Validate on a throwaway branch. Then convert the `processEvents()` loops in `test_visualizer`/`test_qss_audit` to `qtbot.waitSignal`/`waitUntil`.
4. **(small) Enable the pytest-qt Qt-message-as-failure gate** with a curated `qt_log_failure_ignore` allowlist; triage the initial warning backlog in one focused session before flipping it on.
5. **(small) Wrap custom models in `QAbstractItemModelTester`** (`_TracksModel`, `LibraryPaginator`, library/songs models) driven through real insert/remove/reorder/reset paths. Pure-headless, fits `-n auto`.
6. **(medium) Soak harness** on the existing bridge: launch `JT_TEST_BRIDGE=1`, repeating churn cycle via `jt_ctl`, psutil `num_fds`/`num_threads`/`uss` sampling, plateau-vs-ratchet judgment, with a watchdog that avoids the stray-second-window single-instance footgun.
7. **(medium) Add `assertNotEmitted` / `waitSignals(order='strict')` regression guards** for the double-`load_items`, double-scrobble, and `_load_gen` bugs so pytest-randomly + `-n auto` keep them fixed.
8. **(medium) In-process visual goldens**: `render()` + `pytest-image-snapshot==0.5.3` + `pixelmatch` (AA-ignored, threshold ~0.1), DPR pinned to 1.0 at fixed logical size, volatile regions masked. Start with cast dialog, EQ editor, library tiles, A-Z rail.
9. **(medium) Hypothesis `RuleBasedStateMachine`** over `QueueManager`/`_TracksModel` with a `checkErrors` invariant; pure-model first, per-example reset + async_io drain.
10. **(larger / at-computer) Live blur tier**: bridge-staged state → `spectacle -f -b -n` → KWin-geometry crop → pixelmatch, across the dark/light × accent × Frosted/Transparent matrix. Local-only, never CI; surface diffs to august via file send for the eyeball sign-off the handoff calls for.
11. **(larger / opt-in) Chaos**: Toxiproxy/tc-netem on server + cast-proxy + scrobble; netem-throttle the mpv stream URL to prove playback I/O is off the GUI thread.
12. **(as-needed) Native-crash kit**: install python/qt6/mpv debuginfo, adopt `coredumpctl gdb` + `py-bt`, codify the `-n auto` 50–100× stress-loop ritual for the cast/visualizer/smartplaylist suites.
13. **(only if pixel-accurate real input is needed) Calibrate ydotool** via round-trip measurement (move→read-back), or spike the KWin EIS libei-client path (fd handout already verified dialog-free; driving events is the unproven remainder).

Cross-cutting discipline: keep a **strict headless/live partition** for "tested" claims. Logic/models/signals/MPRIS/widget interactions → offscreen CI. Frosted blur / KWin keep-above / borderless SSD chrome → live Plasma 6 Wayland via `spectacle -f -b -n` only. Never mark blur, cast protocols, or keep-above as verified from an offscreen run.