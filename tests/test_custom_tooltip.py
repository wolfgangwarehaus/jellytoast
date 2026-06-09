"""Tests for the custom frosted tooltip (modules/custom_tooltip).

Qt's reused QTipLabel kept an opaque box behind the text after a live
theme swap and couldn't be repositioned on Wayland, so we replaced it
with a top-level translucent widget driven by an app-wide event filter.
These pin the contract: the filter consumes QEvent.ToolTip and drives our
popup, honours the show_tooltips setting, and the live sleep-timer path
(refresh_text) updates an on-screen popup without a full re-show.
"""

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtGui import QHelpEvent
from PySide6.QtWidgets import QWidget

from modules import custom_tooltip
from modules.custom_tooltip import ToolTipFilter, ToolTipPopup


@pytest.fixture
def tooltips_on(monkeypatch):
    """Force the show_tooltips setting on without touching real config."""
    monkeypatch.setattr(
        custom_tooltip, "get_settings", lambda: SimpleNamespace(show_tooltips=True)
    )


@pytest.fixture
def fresh_popup(qapp):
    """Reset the ToolTipPopup singleton around each test so visibility /
    target state never leaks between cases (the popup is a process-wide
    singleton, the qapp is session-scoped)."""
    prev = ToolTipPopup._instance
    ToolTipPopup._instance = None
    yield
    inst = ToolTipPopup._instance
    if inst is not None:
        inst.hide()
        inst.deleteLater()
    ToolTipPopup._instance = prev


def _tooltip_event(widget):
    gp = widget.mapToGlobal(widget.rect().center())
    return QHelpEvent(QEvent.Type.ToolTip, widget.rect().center(), gp)


class TestToolTipFilter:
    def test_consumes_event_and_shows_popup(self, qapp, tooltips_on, fresh_popup):
        """A widget with a tooltip: the filter shows our popup and returns
        True so Qt's native QTipLabel never appears."""
        w = QWidget()
        w.setToolTip("Hello")
        flt = ToolTipFilter(qapp)

        consumed = flt.eventFilter(w, _tooltip_event(w))

        assert consumed is True
        assert ToolTipPopup._instance is not None
        assert ToolTipPopup._instance._label.text() == "Hello"

    def test_suppresses_when_setting_off(self, qapp, monkeypatch, fresh_popup):
        """show_tooltips off: consume the event (return True) but show
        nothing — no popup is even constructed."""
        monkeypatch.setattr(
            custom_tooltip,
            "get_settings",
            lambda: SimpleNamespace(show_tooltips=False),
        )
        w = QWidget()
        w.setToolTip("Hidden")
        flt = ToolTipFilter(qapp)

        consumed = flt.eventFilter(w, _tooltip_event(w))

        assert consumed is True
        assert ToolTipPopup._instance is None

    def test_empty_tooltip_falls_through(self, qapp, tooltips_on, fresh_popup):
        """No tooltip text → don't consume, let Qt's path handle it
        (returns False), and build no popup."""
        w = QWidget()  # no setToolTip
        flt = ToolTipFilter(qapp)

        consumed = flt.eventFilter(w, _tooltip_event(w))

        assert consumed is False
        assert ToolTipPopup._instance is None

    def test_leave_hides_matching_target(self, qapp, tooltips_on, fresh_popup):
        """Leaving the widget the popup is shown for hides it; a Leave on a
        different widget leaves it alone."""
        w = QWidget()
        w.setToolTip("Bye")
        other = QWidget()
        flt = ToolTipFilter(qapp)
        flt.eventFilter(w, _tooltip_event(w))
        popup = ToolTipPopup._instance
        assert popup is not None and popup.isVisible()

        # Leave on an unrelated widget — no-op.
        flt.eventFilter(other, QEvent(QEvent.Type.Leave))
        assert popup.isVisible()

        # Leave on the target — hides.
        flt.eventFilter(w, QEvent(QEvent.Type.Leave))
        assert not popup.isVisible()


class TestToolTipPopup:
    def test_is_toplevel_translucent_tooltip(self, qapp, fresh_popup):
        from PySide6.QtCore import Qt

        popup = ToolTipPopup.instance()
        assert bool(popup.windowFlags() & Qt.WindowType.ToolTip)
        assert popup.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def test_refresh_text_updates_visible_popup(self, qapp, fresh_popup):
        """The sleep-timer countdown path: refresh_text updates an already-
        visible popup's text in place (same instance, still visible)."""
        w = QWidget()
        popup = ToolTipPopup.instance()
        popup.show_under(w, "10:00 left")
        assert popup.isVisible()

        popup.refresh_text(w, "09:59 left")

        assert popup._label.text() == "09:59 left"
        assert popup.isVisible()
        assert popup._target is w

    def test_refresh_text_shows_when_not_visible(self, qapp, fresh_popup):
        """First tick (popup not yet up): refresh_text falls back to a full
        show rather than no-op'ing."""
        w = QWidget()
        popup = ToolTipPopup.instance()
        assert not popup.isVisible()

        popup.refresh_text(w, "Sleep timer — 5:00 left")

        assert popup.isVisible()
        assert popup._label.text() == "Sleep timer — 5:00 left"

    def test_reset_drops_singleton_for_fresh_rebuild(self, qapp, fresh_popup):
        """On a live theme swap the popup is rebuilt so the next hover gets a
        fresh ARGB surface (no stale opaque corners behind the pill). reset()
        must drop the singleton; instance() then builds a NEW one."""
        w = QWidget()
        first = ToolTipPopup.instance()
        first.show_under(w, "before swap")

        ToolTipPopup.reset()
        assert ToolTipPopup._instance is None

        second = ToolTipPopup.instance()
        assert second is not first

    def test_hide_for_ignores_other_target(self, qapp, fresh_popup):
        w = QWidget()
        other = QWidget()
        popup = ToolTipPopup.instance()
        popup.show_under(w, "x")

        popup.hide_for(other)  # different target — no-op
        assert popup.isVisible()
        popup.hide_for(w)
        assert not popup.isVisible()
