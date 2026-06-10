"""Hotkey registry — single source of truth for in-app keyboard shortcuts.

Lives in its own module so:
- The main window doesn't carry a wall of `QShortcut(...)` boilerplate.
- A future Settings → Hotkeys page can iterate one canonical list, render
  one row per action, and rebind via QSettings without touching the
  main-window file.
- Tests can verify the registry data without spinning up a QWidget tree.

Wiring contract
~~~~~~~~~~~~~~~
``build_registry(main_window)`` returns the canonical list of action
dicts (see ``REGISTRY`` below for the shape). The main window calls
``install_shortcuts(parent)`` once at startup — that builds the
registry against ``parent``, creates a ``QShortcut`` per entry, and
returns them so the caller can hold the refs (otherwise Qt garbage-
collects the shortcuts and they stop firing).

User overrides
~~~~~~~~~~~~~~
``get_sequence(action_id)`` reads ``hotkeys/<action_id>`` from QSettings
and returns the user's binding if set, else the registry default. The
follow-up Settings UI will use ``set_sequence`` / ``reset`` / ``reset_all``
to mutate; this module stays UI-agnostic.

``find_conflict`` flags a sequence already bound to another action so
the Settings UI can warn before a save; the registry itself still just
stores whatever is saved.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

from PySide6.QtCore import QSettings
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QWidget

logger = logging.getLogger(__name__)


# QSettings group prefix. Each binding lives at ``hotkeys/<action_id>``
# as a plain QKeySequence portable string (e.g. ``"Ctrl+F"``). New
# namespace — no migration needed; prior to this module the shortcuts
# were never user-rebindable.
_GROUP = "hotkeys"


# Cached QSettings handle. Lazy so import-time has no side effects (the
# test conftest patches QStandardPaths before any Settings() is built).
_qs: Optional[QSettings] = None


def _settings() -> QSettings:
    global _qs
    if _qs is None:
        # Same org/app pair as jellytoast.settings.Settings. Hardcoded
        # here rather than imported to keep this module dependency-free
        # for tests that don't want the full Settings constructor
        # (which runs the legacy-org migration on first build).
        _qs = QSettings("jellytoast", "jellytoast")
    return _qs


def _reset_settings_cache() -> None:
    """Drop the cached QSettings handle. For tests that need a fresh
    handle pointing at a freshly-redirected QStandardPaths root."""
    global _qs
    _qs = None


def build_registry(main_window: Any) -> list[dict]:
    """Build the canonical list of shortcut entries against a parent.

    Each entry is a dict with these keys:

    - ``action_id`` (str): stable QSettings key suffix
    - ``default_seq`` (str): default QKeySequence portable string
    - ``label`` (str): human-readable, for the Settings UI
    - ``callable`` (Callable[[], None]): what to invoke when fired
    - ``context`` (str): ``"global"`` (default) or e.g. ``"search"``

    The callables close over ``main_window``, so the registry is built
    fresh per install call rather than cached at module-import time.

    On any unexpected error we return an empty list and log — better
    to lose hotkeys than refuse to start up over a wiring bug.
    """
    try:
        return _build_registry_impl(main_window)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("registry build failed: %s", e)
        return []


def _build_registry_impl(mw: Any) -> list[dict]:
    entries: list[dict] = [
        {
            "action_id": "all_music",
            "default_seq": "Ctrl+Shift+L",
            "label": "Open all music",
            "callable": lambda: mw._show_native_music_grid(),
            "context": "global",
        },
        # Search: two defaults bound to the same action. Ctrl+F is the
        # platform-standard "find" sequence; "/" is the vim/Slack
        # convention. Stored as two separate registry entries so the
        # Settings UI can let a user reassign each independently
        # (or drop one entirely).
        {
            "action_id": "search_find",
            "default_seq": "Ctrl+F",
            "label": "Focus search",
            "callable": lambda: mw._show_search_view(),
            "context": "global",
        },
        {
            "action_id": "search_slash",
            "default_seq": "/",
            "label": "Focus search (slash)",
            "callable": lambda: mw._show_search_view(),
            "context": "global",
        },
        {
            "action_id": "quit",
            "default_seq": "Ctrl+Q",
            # close() so the existing closeEvent logic (minimize-to-
            # tray vs. hard exit) decides what actually happens.
            "label": "Quit",
            "callable": lambda: mw.close(),
            "context": "global",
        },
    ]

    # JT_NATIVE_ALBUM is an opt-in debug/preview shortcut for opening
    # the currently-playing track's album in NowPlayingPage. Hidden
    # behind the env flag to avoid stealing Ctrl+Shift+A from users'
    # other muscle memory by default. Only registered when set.
    if os.getenv("JT_NATIVE_ALBUM"):
        entries.append(
            {
                "action_id": "native_album",
                "default_seq": "Ctrl+Shift+A",
                "label": "Open currently-playing album",
                "callable": lambda: mw._open_currently_playing_album(),
                "context": "global",
            }
        )

    return entries


def get_sequence(action_id: str, default_seq: str | None = None) -> QKeySequence:
    """Return the active QKeySequence for ``action_id``.

    If the user has set an override in QSettings, that wins. Otherwise
    falls back to ``default_seq`` (or the registry default if None is
    passed and the action_id is known — pulled from a synthesised
    registry without a main window, since defaults are static)."""
    raw = _settings().value(f"{_GROUP}/{action_id}", "", type=str)
    if raw:
        return QKeySequence(raw)
    if default_seq is None:
        # Resolve the registry default. None of the lambdas are
        # invoked here — we only read ``default_seq``.
        for entry in build_registry(_StubContext()):
            if entry["action_id"] == action_id:
                default_seq = entry["default_seq"]
                break
    return QKeySequence(default_seq or "")


def set_sequence(action_id: str, seq: QKeySequence | str) -> None:
    """Persist ``seq`` as the user's binding for ``action_id``.

    Accepts either a QKeySequence or a portable string. Empty strings
    fall through to ``reset(action_id)`` so the Settings UI can clear
    an override by saving an empty sequence."""
    if isinstance(seq, QKeySequence):
        raw = seq.toString(QKeySequence.SequenceFormat.PortableText)
    else:
        raw = str(seq)
    if not raw:
        reset(action_id)
        return
    _settings().setValue(f"{_GROUP}/{action_id}", raw)
    _settings().sync()


def reset(action_id: str) -> None:
    """Remove the user override for ``action_id``. Next ``get_sequence``
    will return the registry default."""
    _settings().remove(f"{_GROUP}/{action_id}")
    _settings().sync()


def reset_all() -> None:
    """Clear every ``hotkeys/*`` entry. Other QSettings groups are
    untouched — uses ``beginGroup``/``remove("")`` which scopes the
    wipe to this group only."""
    qs = _settings()
    qs.beginGroup(_GROUP)
    try:
        qs.remove("")  # within a group, "" means "the group itself"
    finally:
        qs.endGroup()
    qs.sync()


def registry_actions() -> list[dict]:
    """The registry's static metadata — ``action_id`` / ``default_seq``
    / ``label`` / ``context``, with no callables. For the Settings →
    Hotkeys page, which renders one editable row per action but never
    invokes them."""
    return [
        {k: entry[k] for k in ("action_id", "default_seq", "label", "context")}
        for entry in build_registry(_StubContext())
    ]


def _portable(seq: QKeySequence | str) -> str:
    """Normalise a sequence to its portable-text form so two spellings
    of the same chord compare equal."""
    if not isinstance(seq, QKeySequence):
        seq = QKeySequence(str(seq))
    return seq.toString(QKeySequence.SequenceFormat.PortableText)


def current_bindings() -> dict[str, str]:
    """Return ``{action_id: portable-string}`` for every registry
    action, user overrides applied. Used by the Settings UI's conflict
    check and by tests."""
    out: dict[str, str] = {}
    for entry in build_registry(_StubContext()):
        aid = entry["action_id"]
        out[aid] = _portable(get_sequence(aid, entry.get("default_seq")))
    return out


def find_conflict(
    action_id: str, seq: QKeySequence | str, *, bindings: dict[str, str] | None = None
) -> Optional[str]:
    """If ``seq`` is already bound to a *different* action, return that
    action's id; otherwise None. An empty sequence never conflicts.

    ``bindings`` lets a caller pass a snapshot (e.g. a Settings page
    that's mid-edit and hasn't persisted yet); when omitted the current
    persisted bindings are used."""
    norm = _portable(seq)
    if not norm:
        return None
    table = bindings if bindings is not None else current_bindings()
    for other_id, other_seq in table.items():
        if other_id == action_id:
            continue
        if other_seq and _portable(other_seq) == norm:
            return other_id
    return None


def install_shortcuts(parent: QWidget) -> list[QShortcut]:
    """Create QShortcuts for every registry entry against ``parent``.

    Returns the list of QShortcut objects. The caller MUST hold a
    reference (Qt garbage-collects child-less QShortcuts otherwise);
    binding them to an attribute on the main window is the normal
    pattern.

    Any per-entry wiring failure is logged and skipped — one bad
    shortcut shouldn't prevent the rest from registering."""
    shortcuts: list[QShortcut] = []
    try:
        registry = build_registry(parent)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("install failed at build: %s", e)
        return shortcuts

    for entry in registry:
        try:
            action_id = entry["action_id"]
            seq = get_sequence(action_id, entry.get("default_seq"))
            sc = QShortcut(seq, parent)
            sc.activated.connect(entry["callable"])
            shortcuts.append(sc)
        except Exception as e:  # pragma: no cover - defensive
            aid = entry.get("action_id", "<?>")
            logger.warning("wiring %r failed: %s", aid, e)
            continue
    return shortcuts


class _StubContext:
    """Stand-in main-window object for callsites that only need the
    registry's static metadata (action_id / default_seq / label /
    context). Every method access returns a no-op lambda so the
    registry lambdas can be safely *constructed* even though they're
    never invoked here."""

    def __getattr__(self, _name: str) -> Callable[..., None]:
        return lambda *_a, **_kw: None
