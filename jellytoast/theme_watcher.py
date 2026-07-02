"""Follow the pywal / wallust palette live (0.1.7 P2a).

``PywalFollower`` watches ``~/.cache/wal/colors.json`` — the file pywal (and
wallust, in pywal-compat mode) rewrites on every wallpaper change — and applies
it through the imported-preset path (``external_theme.parse_pywal_json`` →
``theme_presets.apply_imported_preset``), so a wallpaper change re-themes
jellytoast in one ``theme_changed`` emit.

Watching gotchas this encodes (from the 0.1.7 theming research):
- pywal REPLACES the file (write-temp + rename), which drops a watched inode —
  after every change the path must be re-added to the watcher.
- The parent directory is watched too, so the very first ``wal`` run (file
  didn't exist yet) is still caught.
- Rapid rewrites (template fan-out) are coalesced behind a ~150 ms debounce
  timer, owned by this GUI-thread QObject (QTimer thread affinity).

The follower always watches; the handler gates on the LIVE
``settings.follow_pywal`` value (same pattern as ``SystemAccentFollower``), so
toggling the setting needs no watcher lifecycle management. The file read runs
on an ``async_io`` worker; parse + apply land back on the GUI thread.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer

_DEBOUNCE_MS = 150


def pywal_colors_path() -> str:
    """``~/.cache/wal/colors.json``, honouring ``XDG_CACHE_HOME``."""
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(cache, "wal", "colors.json")


def pywal_apply_once(on_missing=None) -> None:
    """Read + apply the current pywal palette (launch re-read / the Settings
    toggle turning on). Best-effort: no file / a bad file is a no-op (or
    ``on_missing(reason)`` when provided, for the dialog to show feedback)."""
    from jellytoast.async_io import run_async

    path = pywal_colors_path()

    def _read() -> str | None:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return None

    def _got(text: str | None) -> None:
        if text is None:
            if on_missing is not None:
                on_missing(f"No pywal palette found ({path})")
            return
        _apply_text(text, on_missing)

    run_async(_read, on_result=_got, on_error=lambda _e: None)


def _apply_text(text: str, on_error=None) -> None:
    """Parse + apply a colors.json payload. GUI thread only."""
    try:
        from jellytoast.external_theme import Base16ParseError, parse_pywal_json
        from jellytoast.settings import get_settings
        from jellytoast.theme_presets import apply_imported_preset

        try:
            preset = parse_pywal_json(text)
        except Base16ParseError as exc:
            if on_error is not None:
                on_error(str(exc))
            return
        s = get_settings()
        apply_imported_preset(preset, s.frosted)
        s.imported_scheme_path = ""  # source is pywal now, not a folder file
    except Exception:
        pass


class ThemeFolderFollower(QObject):
    """Re-applies the active imported scheme when its source file in the
    watched ``~/.config/jellytoast/themes/`` folder changes — the matugen /
    template ricing loop. No toggle: following is implicit while the active
    theme came from the folder (``settings.imported_scheme_path``)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_fs_event)
        self._watcher.directoryChanged.connect(self._on_fs_event)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._fire)

    def start(self) -> None:
        from jellytoast.external_theme import themes_folder_path

        # Create the drop-folder so it's discoverable (and watchable) from the
        # first launch — it's inside jellytoast's own config dir.
        try:
            os.makedirs(themes_folder_path(), exist_ok=True)
        except OSError:
            pass
        self._arm_watch()

    def _arm_watch(self) -> None:
        from jellytoast.external_theme import themes_folder_path
        from jellytoast.settings import get_settings

        try:
            folder = themes_folder_path()
            active = get_settings().imported_scheme_path
            existing = set(self._watcher.files()) | set(self._watcher.directories())
            if os.path.isdir(folder) and folder not in existing:
                self._watcher.addPath(folder)
            # Watch the active file directly too — editors that write in place
            # only emit fileChanged; rename-replacers emit directoryChanged.
            if active and os.path.isfile(active) and active not in existing:
                self._watcher.addPath(active)
        except Exception:
            pass

    def _on_fs_event(self, _path: str) -> None:
        self._arm_watch()  # re-add after rename-replace
        from jellytoast.settings import get_settings

        if get_settings().imported_scheme_path:
            self._debounce.start()

    def _fire(self) -> None:
        from jellytoast.async_io import run_async
        from jellytoast.settings import get_settings

        path = get_settings().imported_scheme_path
        if not path:
            return

        def _read() -> str | None:
            try:
                with open(path, encoding="utf-8") as f:
                    return f.read()
            except OSError:
                return None

        def _got(text: str | None) -> None:
            if text is None:
                return  # file deleted mid-edit — keep the persisted palette
            try:
                from jellytoast.external_theme import (
                    Base16ParseError,
                    ensure_readable,
                    parse_base16_yaml,
                )
                from jellytoast.settings import get_settings as _gs
                from jellytoast.theme_presets import apply_imported_preset

                s = _gs()
                if s.imported_scheme_path != path or s.theme_family != "imported":
                    return  # the active scheme changed while we were reading
                try:
                    preset = ensure_readable(parse_base16_yaml(text))
                except Base16ParseError:
                    return  # half-written template render — wait for the next event
                apply_imported_preset(preset, s.frosted)
                s.imported_scheme_path = path  # defensive re-pin (apply is path-agnostic)
            except Exception:
                pass

        run_async(_read, on_result=_got, on_error=lambda _e: None)


class PywalFollower(QObject):
    """Keeps the theme in sync with pywal while ``settings.follow_pywal`` is on:
    applies once at :meth:`start` and again on every colors.json rewrite."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_fs_event)
        self._watcher.directoryChanged.connect(self._on_fs_event)
        self._debounce = QTimer(self)  # owned by this GUI-thread object
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._fire)

    def start(self) -> None:
        from jellytoast.settings import get_settings

        self._arm_watch()
        if get_settings().follow_pywal:
            pywal_apply_once()

    def _arm_watch(self) -> None:
        """(Re-)watch the file + its parent dir. The rename-replace pywal does
        drops the file from the watcher on every change; the dir watch catches
        both that and the file being created for the first time."""
        path = pywal_colors_path()
        parent = os.path.dirname(path)
        try:
            existing = set(self._watcher.files()) | set(self._watcher.directories())
            if os.path.isdir(parent) and parent not in existing:
                self._watcher.addPath(parent)
            if os.path.isfile(path) and path not in existing:
                self._watcher.addPath(path)
        except Exception:
            pass

    def _on_fs_event(self, _path: str) -> None:
        self._arm_watch()  # re-add after the inode swap, even while gated off
        from jellytoast.settings import get_settings

        if get_settings().follow_pywal:
            self._debounce.start()  # restart = coalesce rapid rewrites

    def _fire(self) -> None:
        pywal_apply_once()
