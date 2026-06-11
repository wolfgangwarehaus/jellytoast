# Flatpak build manifest (AT-5) — research, 2026-06-11

> **Status:** Research complete — promotes AT-5 from "needs research" to
> **ready to fire** (draft + local `flatpak-builder` verification; NB
> `flatpak-builder` is not currently installed on the dev box).
> Supersedes `docs/research/flatpak_packaging.md` (2026-05-26) where they
> conflict; the stale draft on branch `auto/flatpak-manifest` (commit
> `cce329a`, 2026-06-01) is a usable skeleton but carries the **old app-id**
> (`io.github.augustvontrips66.*`), pre-rename `modules.*` paths, mpv 0.40.0,
> and one invalid finish-arg (see §libmpv / permission table).

## Summary

**Go** — nothing the app does is fundamentally Flatpak-incompatible. The
recommended header, verified against what is live today:

```yaml
id: io.github.wolfgangwarehaus.jellytoast
runtime: org.kde.Platform
runtime-version: '6.10'
sdk: org.kde.Sdk
base: io.qt.PySide.BaseApp
base-version: '6.10'
command: jellytoast
```

- **Runtime 6.10** is the newest *published* KDE branch (announced 2025-10-30,
  fd-SDK 25.08, Qt 6.10) and is what Haruna ships on Flathub **today**. A
  `qt6.11` branch appeared in `KDE/flatpak-kde-runtime` git **yesterday
  (2026-06-10)** but has no release announcement and **no PySide BaseApp 6.11
  branch exists** — re-check at submission time; if BaseApp 6.11 has landed by
  then, bump both numbers and nothing else changes.
- **PySide6 comes from `io.qt.PySide.BaseApp`** (built from source against the
  runtime's Qt), *not* the PyPI wheel — this is exactly what kills the pipx
  blur-loss pitfall: the BaseApp's PySide6 sees the runtime's KF6 plugins.
- **Python deps**: `flatpak-pip-generator` (still the upstream tool), with
  `--prefer-wheels` for the binary packages.
- **libmpv**: built from source (mpv v0.41.0), copying Haruna's module chain
  verbatim — there is no mpv shared-module or baseapp.
- The **one genuinely uncertain Flathub item** is the KWin trio
  (`--talk-name=org.kde.KWin`, `--filesystem=xdg-data/kwin:create`,
  `--filesystem=xdg-config/kwin:create`). All three are hard linter errors
  requiring an exceptions-file PR. The talk-name has granted precedents; the
  two filesystem grants have **zero precedent** in the current exceptions
  file. Recommended default: ship least-privilege (grants OFF, features
  self-degrade), as the stale draft already does.
- ⚠️ **Flathub exception-PR policy**: the linter docs state that LLM-written
  exception requests result in permanent denial. If august pursues the KWin
  grants, **he must write that PR himself.**

## Runtime & SDK

Confirmed state, June 2026:

| Branch | Status | Evidence |
|---|---|---|
| 6.8 | **EOL** (marked Dec 2025) | KDE Discuss EOL-list thread |
| 6.9 | Supported | announced 2025-04-14, fd-SDK 24.08 |
| **6.10** | **Supported, newest published** | announced 2025-10-30, fd-SDK 25.08; `org.kde.haruna` ships on it now |
| 6.11 | **In progress** — git branch created 2026-06-10, unannounced | `KDE/flatpak-kde-runtime` branches page |

KDE's policy: latest **two** Qt minor branches supported, ~10–12 months each,
previous−2 marked EOL one month after a new minor. Flathub rejects
submissions on EOL runtimes. So 6.10 is safe now and through the v0.1.0
window; 6.9 (the May-26 note's pick) is **superseded** — it will go EOL ~one
month after 6.11 publishes, which is imminent.

**PySide BaseApp** (`io.qt.PySide.BaseApp`): maintained branches 6.7
(discouraged), 6.8, 6.9, **6.10**. Rules from its README:

- `base-version` must match `runtime-version`.
- `cleanup-commands: [/app/cleanup-BaseApp.sh]` is **required**.
- `BASEAPP_REMOVE_WEBENGINE=1` / `BASEAPP_REMOVE_PYWEBENGINE=1` strip
  QtWebEngine (jellytoast's native-UI pivot removed QWebEngineView — set both;
  the stale draft already does).
- `BASEAPP_DISABLE_NUMPY` exists — **do not set it**: the BaseApp ships numpy
  by default, which covers the visualizer FFT dep for free (drop numpy from
  the pip-generator list, or keep it harmlessly pinned — verify at build time
  which version the BaseApp ships vs the `numpy>=1.24` floor).

**Real Flathub PySide6/BaseApp apps** (found via code search of the flathub
org): `net.davidotek.pupgui2` (ProtonUp-Qt — the flagship example; BaseApp
6.9, `BASEAPP_REMOVE_WEBENGINE=1`, per-dep `python3-*.json` modules, installs
itself via `pip3 install --no-index --no-build-isolation --no-deps --prefix=${FLATPAK_DEST} .`),
`io.github.tapscodes.MuseAmp`, `com.markopejic.downloader`. None pip-install
the PySide6 wheel; the wheel-bundles-its-own-Qt route is exactly the
known-broken pattern from the pipx blur-loss finding and should not be
considered.

## Python dependency strategy

**Tool: `flatpak-pip-generator`** — unchanged home at
`flatpak/flatpak-builder-tools/pip` (plus an unofficial PyPI mirror, last
released 2025-04-26). Still the upstream-documented path; no built-in
flatpak-builder pypi source type exists.

Current behavior (verified against the README this week):

- Default artifact preference: universal wheels (`none-any.whl`) → sdists;
  **platform wheels ignored unless** `--prefer-wheels=module1,module2,...`
  (requires `--runtime` so it can compute platform tags).
- For the binary deps: `--prefer-wheels=cryptography,cffi,zeroconf` avoids
  needing the Rust SDK extension (cryptography sdist needs Rust; zeroconf
  sdist needs Cython). Alternative if august prefers sdists:
  `sdk-extensions: [org.freedesktop.Sdk.Extension.rust-stable]` (the pattern
  Delfin uses).
- `--checker-data` emits x-checker-data for the External Data Checker —
  include it.
- Offline-build requirement is satisfied by construction: the generated JSON
  pins exact URLs + sha256 for every artifact; Flathub's builders mirror and
  build offline. The app module itself then installs with
  `pip3 install --no-index ...` (ProtonUp-Qt pattern above).

Generator input (current `pyproject.toml`, PySide6 excluded — BaseApp; numpy
likely excluded — BaseApp ships it):

```
flatpak-pip-generator --runtime org.kde.Sdk//6.10 \
    --prefer-wheels=cryptography,cffi,zeroconf \
    --checker-data --output packaging/flatpak/python3-modules \
    python-mpv "pychromecast>=14,<16" "zeroconf>=0.149.5" ifaddr \
    dbus-next "pyatv>=0.17,<1.0" python-xlib "requests>=2.32.4" \
    keyring "cryptography>=43.0.1" "async-upnp-client>=0.47,<1.0" \
    "soco>=0.31,<1" "snapcast>=2.3.8"
```

Two additions vs the May-26 note's list: **`ifaddr`** (now a declared direct
dep — Tailscale interface enumeration) and **`python-xlib`** (startup-
notification cleanup). The security floors (`zeroconf>=0.149.5`,
`requests>=2.32.4`, `cryptography>=43.0.1`) should be passed through so the
generated lock doesn't resolve below them. Commit the generated JSON next to
the manifest; regenerate on dep bumps.

## libmpv

- **Not in `org.kde.Platform`**; no shared-module or BaseApp for mpv exists
  (flathub `shared-modules` carries luajit only — Delfin references
  `shared-modules/luajit/luajit.json`). Every mpv consumer builds it.
- **Current pins, copied from `org.kde.haruna` (live on runtime 6.10 today)**:
  - mpv **v0.41.0**, sha256
    `ee21092a5ee427353392360929dc64645c54479aefdb5babc5cfbb5fad626209`
    (Delfin pins the identical archive — high confidence)
  - luajit2 `v2.1-20260606` (commit `a08100e7598451d4fd3a89a9826980f7c64117e7`)
  - libplacebo `v7.360.1` (commit `cee9b076f2c63104ccfd497fa79c39a867293ec4`)
  - libass `0.17.4`, uchardet `0.0.8` (sha256
    `e97a60cfc00a1c147a674b097bb1422abd9fa78a2d9ce3f3fdcc2e78a34ac5f0`),
    libXpresent `1.0.2`
  - config-opts: `-Dlibmpv=true -Dcplayer=false -Dbuild-date=false
    -Dlua=enabled -Dalsa=disabled -Dmanpage-build=disabled` (Haruna's set;
    note **`-Dalsa=disabled`** — the new ALSA-direct output path from suite
    2857 will be Pulse/PipeWire-only inside Flatpak, flag in release notes)
  - ffmpeg comes from the KDE runtime — no module needed (Haruna relies on
    this on 6.10).
- `python-mpv>=1.0.5` ctypes-loads `libmpv.so.2` from `/app/lib` — in the
  sandbox's default library path, no placement hack needed (the Windows
  dll-placement pain has no Linux analog here).
- **License**: default mpv build is GPLv2+; jellytoast is GPL-2.0-or-later —
  fully compatible, nothing special needed in the manifest. (The LGPL
  `-Dgpl=false` build exists for proprietary consumers; irrelevant here.)
- The stale draft's libmpv module is structurally right; update v0.40.0 →
  v0.41.0 and fill the pins above.

## Permission table

Linter rule names verified against `flatpak-builder-lint` source
(`checks/finish_args.py`) this week.

| finish-arg | Feature served | Flathub status |
|---|---|---|
| `--share=network` | Jellyfin/Subsonic HTTP, streaming, mDNS/SSDP discovery, cast proxy :8943 | **standard** |
| `--socket=wayland` + `--socket=fallback-x11` + `--share=ipc` | Qt display | **standard** (linter forbids x11-without-ipc; fallback-x11 + ipc is the blessed combo) |
| `--device=dri` | libmpv GPU decode/render, Qt | **standard** |
| `--socket=pulseaudio` | mpv audio out (pipewire-pulse answers) | **standard** |
| `--filesystem=xdg-run/pipewire-0:ro` | native PipeWire (visualizer tap, future direct PW out) | **standard** (Haruna ships it; xdg-run isn't in the linter's flagged xdg-dirs). NB the stale draft's `--socket=pipewire` **does not exist** — delete that line |
| `--talk-name=org.freedesktop.secrets` | keyring SecretService | **standard** |
| `--talk-name=org.kde.kwalletd6` | KWallet-direct keyring backend | **standard** (dual-store blob fallback already covers a denied wallet) |
| `--own-name=org.mpris.MediaPlayer2.jellytoast` | MPRIS | **standard** (Haruna: identical pattern) |
| `--talk-name=org.kde.StatusNotifierWatcher` | tray (SNI) | **standard** |
| `--talk-name=org.freedesktop.Notifications` | notifications | **standard**; consider the Notification portal instead (portals are auto-granted, no arg needed) |
| `--talk-name=org.kde.KWin` | drag_repaint effect load/unload, keep_above rule reconfigure, blur D-Bus probe | **needs-justification** — hard error `finish-args-kwin-talk-name`; granted precedents exist (Nyrna, GPU Screen Recorder, Kando, CrossMacro — all "talk to a KWin script") |
| `--filesystem=xdg-data/kwin:create` | drag_repaint effect install (`~/.local/share/kwin/effects/jellytoast_dragrepaint/`) | **likely-rejected / unprecedented** — error `finish-args-unnecessary-xdg-data-kwin-create-access`; **no app in the current exceptions file has it** |
| `--filesystem=xdg-config/kwin:create` (or the real targets `kwinrc`/`kwinrulesrc`) | kwinrc `[Plugins]` enable key, kwinrulesrc keep-above/noborder rules, blur-demotion read | **likely-rejected / unprecedented** — same rule class; also note the files are `kwinrc`/`kwinrulesrc` directly under `xdg-config`, *not* under an `xdg-config/kwin/` dir, so the grant would have to be `--filesystem=xdg-config/kwinrc` + `--filesystem=xdg-config/kwinrulesrc` — even more eyebrow-raising |
| `--filesystem=xdg-config/autostart:create` | launch-on-login (`autostart/_linux.py` writes `~/.config/autostart/jellytoast.desktop`) | **likely-rejected** — error `finish-args-autostart-filesystem-access`; exceptions only for autostart-manager apps. **Use the Background/Autostart portal** (`org.freedesktop.portal.Background.RequestBackground`) — portal access is automatic, zero finish-args; needs a small `flatpak-prep` refactor of `autostart/_linux.py` (raw D-Bus via dbus-next is fine) |
| `--talk-name=org.freedesktop.Avahi` | — | **not needed**: python-zeroconf implements mDNS in userspace UDP multicast; pychromecast/DLNA/AirPlay discovery all work under plain `--share=network` |

Sandbox-blocker audit (honest list):

- **Cast proxy port 8943**: fine — `--share=network` gives full bind/listen;
  Flatpak has no separate listen permission. The ufw concern is host-firewall
  only; `ufw` appears solely as a docstring note (`cast_proxy.py:64`) and a
  pre-filled hint *string* in `settings_dialog.py:3339` — never executed.
- **spectacle / ydotool**: zero hits in `jellytoast/` — dev-tooling only,
  confirmed non-issues.
- **Visualizer** (`pw-record`/`parec` subprocess): binaries absent in the
  sandbox → engine stays inert (already guarded by `shutil.which`). Gated
  behind `JT_VISUALIZER=1`, so acceptable to no-op for v1; option B is
  bundling pipewire's CLI tools as a module later.
- **Offline downloads**: `offline/locations.py` uses `AppDataLocation` →
  lands in `~/.var/app/<id>/data/` inside the sandbox; cast-proxy blob
  serving reads the same path. No grant needed — **except** the
  user-configurable external download location, which under Flatpak needs
  either the FileChooser portal + persisted document-portal access or a user
  override via Flatseal. Flag in docs.
- **XDG desktop-file copy**: `autostart/_linux.py` also reads
  `~/.local/share/applications/jellytoast.desktop` as a template — absent in
  the sandbox; the synth-entry fallback path handles it, but the synthesized
  `Exec=` would point at the sandboxed python, not `flatpak run`. The portal
  refactor solves this wholesale.

## KWin integration under the sandbox

Three subsystems touch host KWin; all are **best-effort by design** and
degrade to silent no-ops. Mechanics, per the actual code:

1. **`drag_repaint/_kwin.py`** — copies package-data effect files to
   `$XDG_DATA_HOME/kwin/effects/jellytoast_dragrepaint/`, writes
   `kwinrc [Plugins] jellytoast_dragrepaintEnabled` via `kwriteconfig6`, and
   calls `org.kde.KWin /Effects loadEffect` via `qdbus`.
2. **`keep_above/_kwin.py`** — writes rule groups into `kwinrulesrc` via
   `kwriteconfig6`/`kreadconfig6`, then `org.kde.KWin /KWin reconfigure` via
   `qdbus`. Powers the mini-player always-on-top **and the noborder rules
   that make every jellytoast window frameless on KDE Wayland** — this is
   front-of-house chrome, not just the mini player.
3. **`blur/_kwin.py`** — ctypes-loads `libKF6WindowSystem` for
   `enableBlurBehind`/`isEffectAvailable`, plus a `kreadconfig6` read of
   `kwinrc [Plugins] blurEnabled` to demote the stale capability bit.

Expected behavior in the sandbox, least-privilege build:

- **Blur largely works** — this is the headline win vs pipx: `org.kde.Platform`
  ships KF6, so the ctypes `libKF6WindowSystem` load and the QtWayland
  blur-behind protocol path both function (the request travels over the
  Wayland socket; no D-Bus or filesystem needed). Only the kwinrc
  *demotion read* degrades: without host `kwinrc` access, a user who turned
  the Blur effect off gets the translucent-but-unblurred body (the known
  stale-capability-bit failure from the portable-blur research). A read-only
  `--filesystem=xdg-config/kwinrc:ro` would fix it but hits the same
  unprecedented-exception wall; probably not worth it.
- **drag_repaint and keep_above self-disable — probably.** `is_supported()`
  gates on `kwriteconfig6`/`qdbus` on PATH. **Unverified nuance:** the KDE
  runtime may actually ship those binaries (kconfig/qttools are KF6/Qt
  components). If it does, the writes succeed but land in the *sandbox's*
  `~/.var/app/<id>/config/kwinrc` (harmless garbage host KWin never reads),
  and the `qdbus org.kde.KWin` calls fail on the filtered session bus. Net
  effect is still a no-op, but messier; a cheap hardening is an
  `os.environ.get("container") == "flatpak"` / `/.flatpak-info` check in both
  `is_supported()` implementations. Confirm binary presence during the first
  local build.
- **Cost of the no-op**: no drag-blur-artifact fix (KWin bug 455526 line
  artifact returns while dragging), no mini-player always-on-top, and **all
  windows get server-side decorations** (no noborder rules) — visually the
  Flatpak looks like the `native_window_border` opt-out mode. That last item
  is the biggest real degradation and wasn't called out in the May-26 note.
- **If august wants the features**: Path A = `--talk-name=org.kde.KWin`
  (precedented, plausible) + the two filesystem grants (unprecedented,
  uncertain) + refactor `_kwin.py` to drive D-Bus via dbus-next and write the
  INI files directly instead of shelling out to host binaries (the
  `flatpak-spawn --host` route requires `--talk-name=org.freedesktop.Flatpak`,
  which is its own linter error `finish-args-flatpak-spawn-access` —
  *worse*, not better, than the direct grants). Each exception needs a
  human-written PR to `flathub-infra/flatpak-builder-lint` exceptions.json.

## Open questions for august

1. **Path A vs Path B for KWin** (grants + exception PRs + dbus-next refactor
   vs self-disable). New information since May-26: the filesystem grants are
   genuinely unprecedented in the exceptions file; the noborder/SSD
   degradation makes Path B uglier than previously described; and any
   exception PR must be hand-written by you (LLM-authored = permanent
   denial). A middle path exists: request only `--talk-name=org.kde.KWin`
   and accept that effect-install/rules still can't work — probably not
   worth it alone.
2. **6.10 now vs waiting for 6.11** — qt6.11 branch landed in KDE git
   2026-06-10; if the runtime + BaseApp 6.11 publish before the Flathub
   submission, bump both. Pure timing call.
3. **Autostart portal refactor** (`flatpak-prep` issue): refactor
   `autostart/_linux.py` to try `org.freedesktop.portal.Background` first
   (works everywhere, also fixes non-Flatpak edge cases) — pre-requisite for
   autostart working in the Flatpak at all.
4. **Visualizer in-sandbox**: no-op for v1 (current recommendation) or bundle
   pipewire CLI tools?
5. **External download location** under Flatpak: document "use Flatseal /
   keep default" for v1, or invest in document-portal persistence?
6. **Screenshots** remain the hard submission blocker (metainfo.xml has the
   block commented out with a TODO; Flathub validates at least one
   screenshot URL) — already on the not-autonomous list.

## Sources

- Flathub runtimes doc — https://docs.flathub.org/docs/for-app-authors/runtimes
- KDE Flatpak Runtime Update Policy — https://community.kde.org/Policies/Flatpak_Runtime_Update_Policy
- KDE Discuss: org.kde.Sdk 6.10 announcement (2025-10-30) — https://discuss.kde.org/t/flatpak-org-kde-sdk-6-10-runtime-is-now-available/41158
- KDE Discuss: org.kde.Sdk 6.9 announcement (2025-04-14) — https://discuss.kde.org/t/flatpak-org-kde-sdk-6-9-runtime-is-now-available/32852
- KDE Discuss: EOL runtimes list (6.8 EOL, 6.9/6.10 supported as of Dec 2025) — https://discuss.kde.org/t/end-of-life-kde-runtimes-list/42566
- KDE/flatpak-kde-runtime branches (qt6.11 created 2026-06-10) — https://github.com/KDE/flatpak-kde-runtime/branches/all
- PySide BaseApp (branch matrix, BASEAPP_* env vars, cleanup script) — https://github.com/flathub/io.qt.PySide.BaseApp
- KDE: Publishing your Python app as a Flatpak — https://develop.kde.org/docs/getting-started/python/python-flatpak/
- flatpak-pip-generator (current flags incl. `--prefer-wheels`) — https://github.com/flatpak/flatpak-builder-tools/tree/master/pip
- flatpak-pip-generator PyPI mirror (2025-04-26) — https://pypi.org/project/flatpak-pip-generator/
- Flathub requirements (permission philosophy, EOL-runtime rejection) — https://docs.flathub.org/docs/for-app-authors/requirements
- Flathub linter doc (incl. LLM exception-PR ban) — https://docs.flathub.org/docs/for-app-authors/linter
- flatpak-builder-lint finish_args.py (rule names, verified directly) — https://github.com/flathub-infra/flatpak-builder-lint/blob/master/flatpak_builder_lint/checks/finish_args.py
- flatpak-builder-lint exceptions.json (KWin talk-name precedents; no xdg-data/kwin precedent) — https://github.com/flathub-infra/flatpak-builder-lint/blob/master/flatpak_builder_lint/staticfiles/exceptions.json
- Haruna Flathub manifest (runtime 6.10, mpv 0.41.0 chain, MPRIS/pipewire args) — https://github.com/flathub/org.kde.haruna
- Delfin Flathub manifest (mpv 0.41.0, rust-stable extension, luajit shared-module) — https://github.com/flathub/cafe.avery.Delfin
- ProtonUp-Qt Flathub manifest (PySide BaseApp + pip-generator reference app) — https://github.com/flathub/net.davidotek.pupgui2
- Repo: `pyproject.toml`, `jellytoast/drag_repaint/_kwin.py`, `jellytoast/keep_above/_kwin.py`, `jellytoast/blur/_kwin.py`, `jellytoast/autostart/_linux.py`, `jellytoast/cast_proxy.py:64-68`, `jellytoast/visualizer.py:264-352`, `jellytoast/media_controls/_mpris.py:36`, `jellytoast/offline/locations.py`, `jellytoast/settings_dialog.py:3339`, branch `auto/flatpak-manifest` @ `cce329a`
