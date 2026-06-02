"""Contract tests for the fetch/paging state machine extracted into
``modules.library_paginator._PaginatorMixin``.

The fetch machine (the album-doubling / tail-growth race code) was moved
verbatim out of ``library_grid.py`` into a ``_PaginatorMixin`` mixed into
``LibraryGrid``. The behavioural safety net lives in
``test_library_grid_double_load.py`` + ``test_library_grid_stale_cache_tail.py``
(unchanged logic, only their ``run_async`` monkeypatch target moved to this
module). These contract tests pin the *shape* so a future edit can't quietly
give the mixin state, drop a moved method, or move a Signal off the class.
"""

from __future__ import annotations

from PySide6.QtCore import Signal

from modules.library_grid import LibraryGrid
from modules.library_paginator import _PaginatorMixin

# Methods that MUST live on the mixin (the fetch/paging machine).
MOVED = [
    "load_items",
    "_render_offline_items",
    "_on_offline_mode_changed",
    "_on_cold_fetch",
    "_save_cache_async",
    "set_sort",
    "_maybe_load_more",
    "_silent_buffered_fill",
    "_on_silent_buffer_page",
    "_load_next_page",
    "_on_page_loaded",
    "_on_items_loaded",
    "_on_refresh_loaded",
    "_probe_tail_growth",
    "_on_tail_probe",
    "_silent_rebuild_tick",
    "_on_silent_rebuild_page",
    "_items_signature",
    "_clear",
]

# Methods that MUST stay on the grid (cover / alphabet / widget concerns the
# fetch machine calls back into).
STAYED = [
    "showEvent",
    "set_view_mode",
    "_rearm_failed_covers",
    "_load_visible_covers",
    "_fire_cover_load",
    "_on_alphabet_jump",
]


def test_grid_mixes_in_paginator_with_single_qt_base():
    assert issubclass(LibraryGrid, _PaginatorMixin)
    assert _PaginatorMixin.__bases__ == (object,)
    mro = [c.__name__ for c in LibraryGrid.__mro__]
    assert mro.index("_PaginatorMixin") < mro.index("QWidget")


def test_mixin_owns_no_state_or_initializer():
    # A stateful mixin would fight LibraryGrid.__init__ / break the race
    # guards' assumption that all paging state is on the grid instance.
    assert "__init__" not in _PaginatorMixin.__dict__


def test_moved_methods_live_on_the_mixin():
    for name in MOVED:
        assert name in _PaginatorMixin.__dict__, f"{name} should be on _PaginatorMixin"
        assert callable(getattr(LibraryGrid, name))


def test_stayed_methods_remain_on_the_grid():
    for name in STAYED:
        assert name in LibraryGrid.__dict__, f"{name} should stay on LibraryGrid"


def test_signals_stay_on_the_grid_not_the_mixin():
    # Signals require QObject ancestry — they must NOT move to the plain mixin,
    # or the moved methods' self._items_loaded.emit(...) would break.
    for sig in ("_items_loaded", "_refresh_loaded"):
        assert isinstance(LibraryGrid.__dict__.get(sig), Signal)
        assert sig not in _PaginatorMixin.__dict__
