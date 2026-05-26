# Flatpak packaging — runtime, vendoring, permissions

> **📍 Status — 2026-05-26:** Research note — not yet implemented.
> AT-5 (`io.github.augustvontrips66.jellytoast.yaml`) is blocked on
> these decisions; this note picks them so the manifest can be
> drafted in a single pass. Build-verification is still gated on
> spinning up the `flatpak-builder` toolchain locally.

Owner: august.
Last updated: 2026-05-26.

## 1. KDE runtime + SDK version

**Recommendation: `org.kde.Platform` + `org.kde.Sdk` `6.9`, paired
with `io.qt.PySide.BaseApp` branch `6.9`.**

What's available on Flathub right now:

| Branch | KDE Frameworks | Qt | Status |
|---|---|---|---|
| `6.6` | KF 6.x | Qt 6.6 | end-of-life ([Flathub Discourse, 2026](https://discourse.flathub.org/t/org-kde-platform-branch-6-6-is-end-of-life-need-to-update-manually/8047)) |
| `6.7` | KF 6.x | Qt 6.7 | discouraged on the PySide BaseApp matrix |
| `6.8` | KF 6.x | Qt 6.8 | live but the BaseApp marks it "discouraged" |
| `6.9` | KF 6.x | Qt 6.9 | live, recommended |
| `6.10` | KF 6.x | Qt 6.10 | live (newest) |

`pyproject.toml` only pins `PySide6>=6.6.0`, so any of 6.7 / 6.8 /
6.9 / 6.10 nominally satisfies the install. The decision is which
runtime is *welcome on Flathub for new submissions* and which has a
ready BaseApp.

`6.9` wins: Flathub's submission rule is "must be the latest runtime
at time of submission" ([Flathub requirements](https://docs.flathub.org/docs/for-app-authors/requirements));
6.10 is the actual newest, 6.9 is one branch behind but maps to what
most Plasma 6 distros currently ship against. `io.qt.PySide.BaseApp`
[publishes a 6.9 branch](https://github.com/flathub/io.qt.PySide.BaseApp)
with PySide6 prebuilt — using it drops PySide6 (200+ MB) from the
build modules. 6.8 is tempting (it was the "current LTS-ish" branch
through 2025) but the BaseApp matrix marks 6.8 discouraged.

**Concretely** — manifest stanza:

```yaml
runtime: org.kde.Platform
runtime-version: '6.9'
sdk: org.kde.Sdk
base: io.qt.PySide.BaseApp
base-version: '6.9'
command: jellytoast
```

If august prefers to chase 6.10 instead, swap both `6.9`s for `6.10`
and nothing else changes — the open question at the bottom flags this.

## 2. Python dependency vendoring strategy

**Recommendation: `flatpak-pip-generator`.**

The candidates: `flatpak-pip-generator` (standalone script in
[`flatpak/flatpak-builder-tools`](https://github.com/flatpak/flatpak-builder-tools/tree/master/pip)
that walks a requirements list and emits a JSON of pinned tarball
URLs + SHA-256s) vs a built-in `pypi-dependencies` flatpak-builder
action. The latter does **not** exist as a built-in source type as of
2026 — flatpak-pip-generator is still the upstream-recommended path
([Flatpak Python docs](https://docs.flatpak.org/en/latest/python.html),
[KDE python-flatpak guide](https://develop.kde.org/docs/getting-started/python/python-flatpak/)).
Pick pip-generator: offline-reproducible by construction (every URL
mirrored on Flathub build infra, every tarball hashed), trivial to
regenerate, Flathub linter understands the pattern.

**Top-level deps the generator must walk** (from `pyproject.toml`):

```
PySide6>=6.6.0                  # SKIP — provided by io.qt.PySide.BaseApp
python-mpv>=1.0.5
pychromecast>=14.0.0,<16
zeroconf>=0.80.0
dbus-next>=0.2.3                # linux only — always present in Flatpak
pyatv>=0.17                     # linux only — always present in Flatpak
requests>=2.28.0
keyring>=23.0
cryptography>=41.0
```

Plus the extras we want bundled by default in the Flatpak (the manifest
should include them even though pip extras are opt-in, so users get
DLNA / Sonos / Snapcast / visualizer without a CLI override):

```
numpy>=1.24                     # [visualizer]
async-upnp-client>=0.47.0,<1.0  # [dlna]
soco>=0.31,<1                   # [sonos]
snapcast>=2.3.8                 # [snapcast]
```

Invocation pattern (run from the repo root, committed output goes
under `packaging/flatpak/`):

```
flatpak-pip-generator \
    --runtime org.kde.Sdk//6.9 \
    --output python3-modules \
    python-mpv pychromecast zeroconf dbus-next pyatv requests \
    keyring cryptography numpy async-upnp-client soco snapcast
```

The manifest includes the resulting JSON with
`modules: [python3-modules.json, jellytoast]`.

## 3. `modules/drag_repaint/` sandbox path

The effect-install code in `modules/drag_repaint/_kwin.py` builds its
destination from `XDG_DATA_HOME` (or `~/.local/share` if unset):

```python
def _data_home() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    return Path(xdg) if xdg else Path.home() / ".local" / "share"

def _dest_dir() -> Path:
    return _data_home() / "kwin" / "effects" / _EFFECT_ID
```

Inside a Flatpak sandbox, `$XDG_DATA_HOME` is rewritten to
`~/.var/app/io.github.augustvontrips66.jellytoast/data` and the host's
`~/.local/share/kwin/effects/` is **not** writable by default. KWin
itself runs on the host and reads from the host's
`~/.local/share/kwin/effects/`, so writing to the sandboxed location
would install the effect somewhere KWin will never look.

**The right grant is `--filesystem=xdg-data/kwin:create`** — Flatpak's
shorthand for "let me read+write `~/.local/share/kwin/`, and create the
directory if it doesn't exist." This maps the host directory in over
the sandboxed one for that subtree only, so `_data_home() /
"kwin/effects/" / _EFFECT_ID` resolves to the same host path the
non-Flatpak install uses, and the KWin scripted-effect loader (running
host-side) picks it up identically.

Sandbox-specific concern: `is_supported()` requires `kwriteconfig6` /
`qdbus` on PATH. Inside the sandbox they're absent — `shutil.which`
returns None and the subsystem no-ops, which is wrong here because
KWin is running on the host. Two fixes:

- **Short-term (v1):** grant `--talk-name=org.kde.KWin` and wrap the
  binary calls in `flatpak-spawn --host kwriteconfig6 …` /
  `flatpak-spawn --host qdbus6 …`. `flatpak-spawn` is the canonical
  host-binary escape hatch.
- **Cleaner long-term:** drop the binaries entirely — write the
  `kwinrc` Plugins key as plain INI ourselves (need
  `--filesystem=xdg-config/kwin:create`) and reload via raw D-Bus on
  `org.kde.KWin /Effects` using `dbus-next`, which is already a dep.
  File this as a follow-up issue tagged `flatpak-prep`.

The effect's JSON + JS ship as `package-data` under `modules.drag_repaint`
(see `pyproject.toml`), so they're already in `/app` at install time —
no extra manifest install steps.

**Tentative answer to august's specific question:**
`--filesystem=xdg-data/kwin:create` is correct for the destination
write; **also** add `--filesystem=xdg-config/kwin:create` for the
`kwinrc` Plugins-key write; and either grant `--talk-name=org.kde.KWin`
+ wrap external binaries in `flatpak-spawn --host`, or refactor
`_kwin.py` to drive KWin via dbus-next directly (preferred — file an
issue tagged `flatpak-prep`).

## 4. Other system permissions

| Permission | Why | Notes |
|---|---|---|
| `--socket=wayland` | PySide6 / QtWayland | Mandatory on Wayland-first builds |
| `--socket=fallback-x11` | XWayland fallback | Cheap, covers non-Wayland sessions |
| `--share=ipc` | X11 SHM extension | Pairs with fallback-x11 |
| `--socket=pulseaudio` | mpv → PipeWire (PulseAudio shim) | PipeWire's `pipewire-pulse` answers on the same socket |
| `--device=dri` | GPU access for libmpv decode + Qt rendering | Don't grant `--device=all` if avoidable |
| `--share=network` | REST API, streaming, mDNS, cast control | Mandatory |
| `--talk-name=org.freedesktop.secrets` | `keyring` SecretService backend (`modules/settings.py:6`) | Reaches KWallet / GNOME Keyring |
| `--talk-name=org.kde.kwalletd6` | Direct KWallet fallback | Some keyring backends use this name instead of org.freedesktop.secrets |
| `--talk-name=org.kde.KWin` | drag-repaint effect install (§3) | Only if using flatpak-spawn approach |
| `--filesystem=xdg-data/kwin:create` | drag-repaint effect destination (§3) | |
| `--filesystem=xdg-config/kwin:create` | `kwinrc` Plugins enable key (§3) | |
| `--own-name=org.mpris.MediaPlayer2.jellytoast` | MPRIS bus name (`modules/media_controls/_mpris.py:42`) | The own-name (not talk-name) — we publish, not consume |
| `--talk-name=org.kde.StatusNotifierWatcher` | Tray icon (`modules/tray.py`) on KDE | Plasma's tray uses SNW, not the legacy X11 tray protocol |
| `--talk-name=org.freedesktop.Notifications` | Tray-driven notifications | Cheap, future-proof; not used today but harmless |

**Cast proxy on port 8943** — `modules/cast_proxy.py` binds an HTTP
listener on `_PROXY_PORT = 8943` and serves stream bytes to cast
devices over the LAN. `--share=network` is sufficient; Flatpak does
not separate "listen" from "connect" for network sandboxes. The
ufw-rule note in the existing `cast_proxy` docstring still applies —
that's a host-firewall concern, not a Flatpak permission.

**Local file:// blobs for offline cast.** `cast_proxy` also serves
downloaded blobs off disk (per architecture notes). Whatever path the
downloads cache writes to needs read access from the same sandbox; if
downloads live under `XDG_DATA_HOME/jellytoast/downloads`, that's
already inside the sandbox and no extra grant is needed.

**Visualizer.** `modules/visualizer.py` shells out to `pw-record` or
`parec` (PipeWire CLI) on the host. Inside the sandbox neither exists
by default. Options: (a) bundle `pipewire-utils` / `pulseaudio-utils`
into the build modules (small), or (b) ship `flatpak-spawn --host
pw-record` wrappers. The visualizer is gated behind `JT_VISUALIZER=1`
so it's acceptable for v1 to no-op inside Flatpak and log "install
pipewire-utils for visualizer"; revisit if visualizer becomes
default-on.

## 5. libmpv

**`libmpv` is NOT included in `org.kde.Platform`** — confirmed via
the [mpv-player#12027 thread](https://github.com/mpv-player/mpv/issues/12027)
and [media-kit#1055](https://github.com/media-kit/media-kit/issues/1055).
It must be a build module. Common pitfall: mpv 0.36+ builds
`libmpv.so.2`, but some Python wrappers still hunt for
`libmpv.so.1` — `python-mpv>=1.0.5` (our pin) handles both names,
so we're safe.

Sketch of the build module (newest mpv release at write-time):

```yaml
- name: mpv
  buildsystem: meson
  config-opts:
    - -Dbuild-date=false
    - -Dlibmpv=true
    - -Dcplayer=false       # we drive via libmpv only
    - -Dmanpage-build=disabled
    - -Dhtml-build=disabled
    - -Dlua=disabled         # don't need scripting
    - -Dpulse=enabled
    - -Dpipewire=enabled
    - -Djavascript=disabled
  sources:
    - type: archive
      url: https://github.com/mpv-player/mpv/archive/refs/tags/v0.40.0.tar.gz
      sha256: <fill at manifest-draft time>
```

ffmpeg comes in via the KDE runtime's freedesktop-SDK base, so mpv's
ffmpeg dependency is satisfied without a separate module. Confirm at
build time — if the runtime's ffmpeg is older than what libmpv needs,
add an ffmpeg module first.

## 6. Other binary deps' system-lib needs

- `cryptography` — needs OpenSSL at build time; the freedesktop SDK
  base under `org.kde.Sdk` ships it. Modern manylinux wheels work.
- `keyring`, `pychromecast`, `zeroconf`, `pyatv`, `async-upnp-client`,
  `soco`, `snapcast`, `dbus-next`, `numpy` — all pure-Python or
  manylinux wheels, no extra system libs. `pyatv` pulls in
  `cryptography`, `protobuf`, `aiohttp` transitively; pip-generator
  picks them up automatically.

## 7. Open questions for august

1. **6.9 vs 6.10 runtime.** Note recommends 6.9 for compatibility
   breadth. august may prefer to ride the newest branch — confirm.
2. **drag-repaint refactor scope.** Wrapping `kwriteconfig` / `qdbus`
   in `flatpak-spawn --host` is mechanical but leaks the sandbox.
   Refactoring `_kwin.py` to drive KWin via `dbus-next` (already in
   the dep set) is cleaner. Both work; which to pick depends on how
   much we want to touch the drag-repaint subsystem before it's
   battle-tested. Default: ship flatpak-spawn for v1, file an issue
   for the dbus-next refactor.
3. **Visualizer in the sandbox.** Bundle `pipewire-utils` (adds ~5 MB
   to the Flatpak) or no-op the visualizer inside Flatpak for v1?
   The current `JT_VISUALIZER=1` gate means most users never trigger
   it.
4. **Flathub appstream id.** Current `packaging/io.github.augustvontrips66.*`
   prefix assumes the `augustvontrips66` GitHub user namespace. Confirm
   that's the namespace we want to land on Flathub long-term — once a
   Flatpak ships with that id, renaming is migration pain.
5. **Cast proxy + firewall in Flathub copy.** The manifest can't open
   firewall ports for the user. If the cast-proxy port being blocked
   becomes a common failure mode, the AppData (`metainfo.xml`) needs
   a release note flagging it. Out of manifest scope but worth noting.

## 8. Sources

- [Flathub for-app-authors requirements](https://docs.flathub.org/docs/for-app-authors/requirements)
- [Available Runtimes — Flatpak docs](https://docs.flatpak.org/en/latest/available-runtimes.html)
- [KDE Discuss — org.kde.Sdk 6.8 runtime announcement](https://discuss.kde.org/t/flatpak-org-kde-sdk-6-8-runtime-is-now-available/24717)
- [Flathub Discourse — 6.6 end-of-life](https://discourse.flathub.org/t/org-kde-platform-branch-6-6-is-end-of-life-need-to-update-manually/8047)
- [io.qt.PySide.BaseApp on Flathub](https://github.com/flathub/io.qt.PySide.BaseApp)
- [KDE — Publishing your Python app as a Flatpak](https://develop.kde.org/docs/getting-started/python/python-flatpak/)
- [Flatpak docs — Python](https://docs.flatpak.org/en/latest/python.html)
- [flatpak-builder-tools / pip](https://github.com/flatpak/flatpak-builder-tools/tree/master/pip)
- [Albin Larsson — Python Dependencies and Flatpak](https://byabbe.se/2022/11/07/python-dependencies-and-flatpak)
- [mpv-player#12027 — libmpv shared object in Flathub](https://github.com/mpv-player/mpv/issues/12027)
- [media-kit#1055 — libmpv.so.1 vs .so.2 in Flatpak](https://github.com/media-kit/media-kit/issues/1055)
- Repo: `pyproject.toml` (deps + package-data), `modules/drag_repaint/_kwin.py` (effect-install destination math), `modules/cast_proxy.py:38-45` (`_PROXY_PORT`), `modules/media_controls/_mpris.py:42` (`SERVICE_NAME`), `modules/tray.py` (StatusNotifierItem), `modules/visualizer.py:264-352` (pw-record / parec shell-outs).
