# Third-party notices

jellytoast is licensed GPL-2.0-or-later (see `LICENSE`). The binary
distributions bundle third-party components under their own licenses,
listed here with the written offer for corresponding source that
GPL-2.0 §3(b) requires.

## libmpv / FFmpeg (all binary bundles that vendor them)

Several distributions bundle libmpv (which statically or dynamically
links FFmpeg and, depending on the build, x264 and other GPL/LGPL
components):

- **Windows installer + portable zip** — `libmpv-2.dll`, taken
  unmodified from the **shinchiro `mpv-winbuild-cmake`** release
  artifacts (<https://github.com/shinchiro/mpv-winbuild-cmake/releases>),
  fetched by `packaging/windows/get_libmpv.ps1`.
- **Linux AppImage** — the build host's distro libmpv plus its FFmpeg /
  codec closure, vendored by `packaging/appimage/build_appimage.sh`
  (source: the distribution packages of the CI build image, and
  upstream mpv/FFmpeg below).
- **macOS `.dmg` (Intel + Apple Silicon)** — libmpv and its dylib
  closure staged by `packaging/macos/get_libmpv.sh` from the build
  host's package manager (source: those packages, and upstream below).
- **Mac App Store `.pkg`** — an LGPL-configured, no-Lua libmpv built
  from source by `packaging/macos/mas/build_libmpv_lgpl.sh` (FFmpeg
  built without `--enable-gpl`).

**Written offer for source:** the complete corresponding source for
every bundled libmpv and the components it links is publicly available
at:

- mpv — <https://github.com/mpv-player/mpv>
- FFmpeg — <https://github.com/FFmpeg/FFmpeg>
- the exact Windows build recipe + pinned source revisions —
  <https://github.com/shinchiro/mpv-winbuild-cmake>
- the exact MAS build recipe — `packaging/macos/mas/build_libmpv_lgpl.sh`
  in this repository

For three years from the date of this release, you may also request the
corresponding source by opening an issue at
<https://github.com/wolfgangwarehaus/jellytoast/issues>.

On Linux, the `.deb` does **not** bundle libmpv — it depends on the
system `libmpv2`/`libmpv1`, whose source is provided by your distribution.

## Qt / PySide6

The bundled Qt 6 / PySide6 are used under the **LGPL-3.0**. Corresponding
source: <https://code.qt.io/> and <https://code.qt.io/pyside/pyside-setup>.

## Python and bundled Python packages

CPython is under the **PSF License**. The bundled pure-Python
dependencies retain their own licenses (MIT / Apache-2.0 / BSD / etc.);
a full machine-readable manifest can be generated with
`pip-licenses --with-license-file` against the release's pinned set
(see `pyproject.toml`).
