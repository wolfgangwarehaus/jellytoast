# Alpine / Nura package

`APKBUILD` builds jellytoast for Alpine and for Nura (formerly postmarketOS),
which is built on Alpine. Install steps for users are in
[`docs/linux-phones.md`](../../docs/linux-phones.md).

- `dev/cut_release.sh` bumps `pkgver` (and resets `pkgrel`) with every release.
  The checksum can't be known until the release tag exists, so builds run
  `abuild checksum` first.
- Every hard dependency is in Alpine's main / community repos from v3.24.
  DLNA (async-upnp-client), Sonos (soco) and AirPlay (pyatv) aren't packaged in
  Alpine; jellytoast imports them lazily and hides those speakers without them.
- Verified 2026-10-25 on Alpine 3.24 aarch64 under emulation: `abuild -r`
  builds and passes `check()`, `apk add` installs it, and the installed app
  signs in, loads the library and art, and decodes audio through libmpv.
- To get it into Alpine's own repositories (so `apk add jellytoast` works on
  Nura without building), submit this APKBUILD to
  [aports](https://gitlab.alpinelinux.org/alpine/aports) under `testing/`.
