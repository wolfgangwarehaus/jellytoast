"""Main-window blur is shaped to the rounded body AT REST and square during an
active interaction (the 2026-06-05 hybrid — kills the square blur halo that
poked past the rounded corners as a "pointy" artifact).

These pin the corner radius the window hands the blur backend in each state;
the Wayland rendering + the resize/maximize strip-prevention are verified live
(the compositor blur can't be captured in a unit test). Called unbound on a
stub self, matching the window's other structural tests.
"""

from __future__ import annotations

from jellytoast.app import JellytoastWindow
from jellytoast.design_tokens import RADIUS_WINDOW


def _record_radius(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        "jellytoast.blur.apply",
        lambda w, enabled, radius: recorded.append((enabled, radius)) or True,
    )
    monkeypatch.setattr(
        "jellytoast.theme.get_active_theme",
        lambda: type("T", (), {"blur": True})(),
    )
    return recorded


class _StubWin:
    def __init__(self, edge_flush):
        self._edge_flush = edge_flush

    def _is_edge_flush(self):
        return self._edge_flush


def test_apply_blur_rounds_at_rest(monkeypatch):
    recorded = _record_radius(monkeypatch)
    JellytoastWindow._apply_blur(_StubWin(edge_flush=False))
    assert recorded == [(True, RADIUS_WINDOW)]


def test_apply_blur_squares_when_edge_flush(monkeypatch):
    # Maximized / vertical-max: body is squared flush to the edge, so the blur
    # region must square too (matches the painted body).
    recorded = _record_radius(monkeypatch)
    JellytoastWindow._apply_blur(_StubWin(edge_flush=True))
    assert recorded == [(True, 0)]


def test_apply_blur_whole_is_always_square(monkeypatch):
    # The live-during-resize path ignores edge-flush — always the square,
    # auto-tracking empty region that can't desync a lagging surface.
    recorded = _record_radius(monkeypatch)
    JellytoastWindow._apply_blur_whole(_StubWin(edge_flush=False))
    assert recorded == [(True, 0)]
