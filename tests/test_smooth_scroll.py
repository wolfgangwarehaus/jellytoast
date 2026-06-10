"""SmoothScrollFilter target-cache invariant.

The filter coalesces successive wheel notches by caching a per-bar
animation TARGET and adding each notch to that target (not to the bar's
live, mid-tween value). The flip side is that any programmatic jump must
call ``invalidate(bar)`` — otherwise the next notch computes
``new_target = stale_target + delta`` and animates the bar back to where
it was before the jump. That exact omission shipped the A-Z rail
"snap-back" regression (2026-05-25), and the module had no test guarding
it. These pin the contract.

Headless: a plain QScrollBar plus ``_animate`` / ``invalidate`` populate
and clear the ``_anims`` cache synchronously, independent of whether the
QPropertyAnimation actually ticks — no visible window or event loop
needed.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QScrollBar

from jellytoast.smooth_scroll import SmoothScrollFilter


def _bar(value: int = 500, lo: int = 0, hi: int = 1000) -> QScrollBar:
    bar = QScrollBar(Qt.Orientation.Vertical)
    bar.setMinimum(lo)
    bar.setMaximum(hi)
    bar.setValue(value)
    return bar


class TestTargetCache:
    def test_first_notch_targets_bar_value_plus_delta(self, qapp):
        sf = SmoothScrollFilter()
        bar = _bar(value=500)
        sf._animate(bar, -90)
        anim, target = sf._anims[bar]
        assert target == 410
        assert int(anim.endValue()) == 410

    def test_second_notch_coalesces_from_cached_target_not_live_value(self, qapp):
        # The coalescing invariant: the second notch adds to the CACHED
        # target (410), ignoring the bar's mid-tween live value (480), so a
        # fast wheel spin glides to the summed target rather than fighting
        # the in-flight tween.
        sf = SmoothScrollFilter()
        bar = _bar(value=500)
        sf._animate(bar, -90)  # target 410
        bar.setValue(480)  # bar mid-tween
        sf._animate(bar, -90)  # → 410 - 90 = 320 (NOT 480 - 90)
        _anim, target = sf._anims[bar]
        assert target == 320

    def test_target_clamped_to_minimum(self, qapp):
        sf = SmoothScrollFilter()
        bar = _bar(value=50)
        sf._animate(bar, -90)  # 50 - 90 = -40 → clamp to 0
        _anim, target = sf._anims[bar]
        assert target == 0

    def test_target_clamped_to_maximum(self, qapp):
        sf = SmoothScrollFilter()
        bar = _bar(value=980)
        sf._animate(bar, 90)  # 980 + 90 = 1070 → clamp to 1000
        _anim, target = sf._anims[bar]
        assert target == 1000


class TestInvalidate:
    def test_invalidate_drops_cached_target(self, qapp):
        sf = SmoothScrollFilter()
        bar = _bar(value=500)
        sf._animate(bar, -90)
        assert bar in sf._anims
        sf.invalidate(bar)
        assert bar not in sf._anims

    def test_notch_after_invalidate_recomputes_from_bar_value(self, qapp):
        # The A-Z snap-back regression in one test: a programmatic jump
        # moves the bar (500 → 900) and invalidates; the next wheel notch
        # MUST compute from the new live value (900 - 90 = 810), not the
        # stale pre-jump cached target (which would give 410 - 90 = 320 and
        # animate the bar back up).
        sf = SmoothScrollFilter()
        bar = _bar(value=500)
        sf._animate(bar, -90)  # stale cached target 410
        bar.setValue(900)  # programmatic A-Z jump
        sf.invalidate(bar)  # the fix: forget the stale target
        sf._animate(bar, -90)
        _anim, target = sf._anims[bar]
        assert target == 810

    def test_invalidate_unknown_bar_is_safe(self, qapp):
        sf = SmoothScrollFilter()
        bar = _bar()
        sf.invalidate(bar)  # never animated — no-op, must not raise
        assert bar not in sf._anims
