# Third-party notices

jellytoast is licensed GPL-2.0-or-later (see `LICENSE`). The binary
distributions bundle third-party components under their own licenses,
listed here with the written offer for corresponding source that
GPL-2.0 §3(b) requires.

## libmpv / FFmpeg (Windows installer + portable zip)

The Windows builds bundle `libmpv-2.dll`, a GPL build of mpv that
statically links FFmpeg (and, depending on the build, x264 and other
GPL/LGPL components). The DLL is taken, unmodified, from the
**shinchiro `mpv-winbuild-cmake`** release artifacts
(<https://github.com/shinchiro/mpv-winbuild-cmake/releases>), fetched by
`packaging/windows/get_libmpv.ps1`.

**Written offer for source:** the complete corresponding source for the
bundled `libmpv-2.dll` and the components it statically links is the
mpv / FFmpeg / x264 source at the versions used by the shinchiro build
referenced in `get_libmpv.ps1`. It is publicly available at:

- mpv — <https://github.com/mpv-player/mpv>
- FFmpeg — <https://github.com/FFmpeg/FFmpeg>
- the exact build recipe + pinned source revisions —
  <https://github.com/shinchiro/mpv-winbuild-cmake>

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
