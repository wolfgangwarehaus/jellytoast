"""Regression for NowPlayingPage._on_track_context_menu (review P0-3).

In source-order display the right-clicked row index is an ``original_items``
index, but ``queue_remove_at`` → ``QueueManager.remove_at`` expects a
PLAY-ORDER index. A shuffled-but-unmodified album stays in source-order
display (shuffle permutes ``play_order`` without setting ``is_modified``),
so the two indices diverge and "Remove from queue" used to delete the wrong
track. The handler now maps the row's Id to its play-order index — mirroring
``_on_row_clicked`` — before emitting.

Driven on a bare page (``__new__`` to skip the heavy widget build) with the
QMenu stubbed, so no Qt painting, no audio, no exec dialog.
"""

from jellytoast.now_playing_page import NowPlayingPage


class _Sig:
    def __init__(self):
        self.emitted = []

    def emit(self, *args):
        self.emitted.append(args)


class _Bus:
    def __init__(self):
        self.queue_remove_at = _Sig()
        self.queue_add_next = _Sig()
        self.queue_add_end = _Sig()


class _QueueMgr:
    def __init__(self, original_items, queue):
        self.original_items = original_items
        self.queue = queue


class _FakeMenu:
    """Stand-in for ui_helpers.opaque_menu(...). addAction returns a unique
    sentinel; exec returns the action whose label matches ``choose``."""

    def __init__(self, choose):
        self._choose = choose
        self._chosen = None

    def addAction(self, label):
        act = object()
        if label == self._choose:
            self._chosen = act
        return act

    def addSeparator(self):
        pass

    def exec(self, _pos):
        return self._chosen


def _page(kind, original_items, queue):
    page = NowPlayingPage.__new__(NowPlayingPage)
    page._preview_id = ""
    page._preview_tracks = []
    page._displayed_items_kind = kind
    page._list_container = None
    page.queue_mgr = _QueueMgr(original_items, queue)
    page.bus = _Bus()
    return page


def _patch_menu(monkeypatch, choose):
    def _fake_opaque_menu(_parent):
        return _FakeMenu(choose)

    # opaque_menu is imported lazily inside the handler via
    # `from jellytoast.ui_helpers import opaque_menu`, so patch it at source.
    import jellytoast.ui_helpers as uih

    monkeypatch.setattr(uih, "opaque_menu", _fake_opaque_menu)


def test_remove_on_shuffled_album_maps_source_to_play_order(qapp, monkeypatch):
    # Source order [a, b, c]; play order shuffled to [c, a, b]. Right-click
    # the displayed row 2 (== original_items[2] == "c"); "c" lives at
    # play-order index 0, so remove must emit 0, NOT 2.
    original = [{"Id": "a"}, {"Id": "b"}, {"Id": "c"}]
    queue = [{"Id": "c"}, {"Id": "a"}, {"Id": "b"}]
    page = _page("source", original, queue)
    _patch_menu(monkeypatch, "Remove from queue")

    NowPlayingPage._on_track_context_menu(page, 2, None)

    assert page.bus.queue_remove_at.emitted == [(0,)]


def test_remove_in_play_mode_emits_index_directly(qapp, monkeypatch):
    # Play-order display: the displayed index IS the play-order index.
    queue = [{"Id": "c"}, {"Id": "a"}, {"Id": "b"}]
    page = _page("play", queue, queue)
    _patch_menu(monkeypatch, "Remove from queue")

    NowPlayingPage._on_track_context_menu(page, 2, None)

    assert page.bus.queue_remove_at.emitted == [(2,)]


def test_remove_unknown_id_is_noop(qapp, monkeypatch):
    # Source row resolves to an Id not present in the play queue → no emit
    # (better a no-op than removing the wrong track).
    original = [{"Id": "a"}, {"Id": "b"}, {"Id": "z"}]
    queue = [{"Id": "a"}, {"Id": "b"}]  # "z" not in the play queue
    page = _page("source", original, queue)
    _patch_menu(monkeypatch, "Remove from queue")

    NowPlayingPage._on_track_context_menu(page, 2, None)

    assert page.bus.queue_remove_at.emitted == []
