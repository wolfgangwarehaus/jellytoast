# Licensing

jellytoast is licensed **GPL-2.0-or-later** (see [`LICENSE`](../LICENSE)). The
SPDX expression in `pyproject.toml` and the `<project_license>` in the
AppStream metainfo both match.

This note records the one licensing fact that is **not obvious from the
code** and that a future maintainer must not break.

## ⚠️ The "or-later" is load-bearing — do not relicense to GPL-2.0-only

The Qt binding, **PySide6 / shiboken6**, is offered under
`LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only`.

- LGPL-3.0 and GPL-3.0 are **incompatible** with a GPL-2.0-**only**
  program.
- They are compatible with jellytoast **only because jellytoast is
  GPL-2.0-or-*later*** — the "or later" lets a conveyed combined work be
  taken to GPL-3.0, under which the LGPL-3.0/GPL-3.0 binding is fine.

**Consequence:** relicensing jellytoast to GPL-2.0-**only** would silently
make it incompatible with the Qt binding it cannot run without. Keep the
`-or-later`.

## Dependency licenses (all GPL-2-or-later compatible)

| Dependency | License |
| --- | --- |
| pyatv, soco, pychromecast, keyring, dbus-next, ifaddr | MIT |
| jeepney, comtypes, windows-toasts, winrt-* (PyWinRT set), pyobjc (macos extra) | MIT |
| requests, async-upnp-client | Apache-2.0 |
| aiohttp | Apache-2.0 |
| cryptography | Apache-2.0 OR BSD-3-Clause |
| numpy | BSD-3-Clause |
| zeroconf | LGPL-2.1-or-later |
| python-xlib | LGPL-2.1-or-later |
| python-mpv (bindings) | GPL-2.0-or-later OR LGPL-2.1-or-later |
| PySide6 / shiboken6 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only (see above) |

No proprietary or copyleft-incompatible dependency is present.

## Distribution note (bundles)

A pip install links these dynamically and does not redistribute them. A
**self-contained bundle vendors them** (the AppImage, the PyInstaller
.deb/.exe), so a bundle should ship their notices — generate a
`THIRD-PARTY-LICENSES` file at packaging time, e.g.:

```bash
pip install pip-licenses
pip-licenses --format=plain-vertical --with-license-file --no-license-path \
  > THIRD-PARTY-LICENSES
```
