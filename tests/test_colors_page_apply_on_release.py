"""Custom-colors sliders apply on release, not mid-drag (lag fix, 2026-06-07).

``ct.apply_override`` fires a global ``theme_changed`` that re-stamps every
widget in the app (a full ``app.setStyleSheet`` re-polish — the GUI-thread
lock the user felt as "chug" / freeze while dragging).

Fix: the sliders run with tracking OFF, so a mouse drag emits ``sliderMoved``
(→ a cheap, this-swatch-only preview via ``_on_slider_preview``) but
``valueChanged`` only once, on release (→ ``_on_value_committed`` → a debounced
apply). So the app-wide apply can never fire mid-drag. Keyboard / wheel steps
still commit via ``valueChanged``.

`qapp` (conftest.py) provides the QApplication the widgets need.
"""

from jellytoast import color_tokens as ct
from jellytoast.settings_colors_page import _ColorTokenRow


def _a_token():
    """First registered color token — enough to build one editor row."""
    return next(iter(ct.TOKENS.values()))


def test_sliders_run_with_tracking_off(qapp):
    row = _ColorTokenRow(_a_token())
    for s in (row._h_slider, row._s_slider, row._v_slider):
        assert not s.hasTracking(), (
            "tracking must be off so a drag emits valueChanged only on release"
        )


def test_drag_preview_never_applies(qapp, monkeypatch):
    calls = []
    monkeypatch.setattr(ct, "apply_override", lambda *a, **k: calls.append(a))
    row = _ColorTokenRow(_a_token())

    # A drag tick (sliderMoved) previews the swatch but must not apply or even
    # arm the debounce — that is what keeps the drag smooth.
    row._on_slider_preview(123)

    assert calls == [], "no app-wide apply may fire during a drag"
    assert not row._settle.isActive(), "a drag tick must not arm the debounce"


def test_commit_arms_debounce_and_applies(qapp, monkeypatch):
    calls = []
    monkeypatch.setattr(ct, "apply_override", lambda *a, **k: calls.append(a))
    row = _ColorTokenRow(_a_token())

    # valueChanged (release / keyboard / wheel) arms the settle timer …
    row._h_slider.setValue((row._h_slider.value() + 37) % 361)
    assert row._settle.isActive(), "a commit must arm the debounce"
    assert calls == [], "apply is debounced, not synchronous"

    # … and the settle timer firing applies exactly once.
    row._on_settled()
    assert len(calls) == 1, "settle applies the override once"


def test_unchanged_reload_is_skipped(qapp):
    # The reload-storm guard: re-loading a row whose token value didn't change
    # is a no-op (no slider churn), which is what kills the on-release storm.
    row = _ColorTokenRow(_a_token())
    before = (row._h_slider.value(), row._s_slider.value(), row._v_slider.value())
    row._load_from_current()  # token hasn't changed since construction
    after = (row._h_slider.value(), row._s_slider.value(), row._v_slider.value())
    assert before == after
