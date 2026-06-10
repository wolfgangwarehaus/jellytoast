"""JT_OPAQUE dev diagnostic (env-only).

``blur.opaque_mode_active()`` is True only via the ``JT_OPAQUE=1`` env switch.
When on, ``status()`` reports UNSUPPORTED so frosted bodies + popups fall back
to their opaque alpha (and ``apply()`` skips requesting blur). There is no
user-facing setting — the old "Opaque background" Settings toggle was removed
(it broke the window's rounded corners by dropping translucency, and the
near-opaque no-blur fallback already covers the real need). 2026-06-07.
"""

import jellytoast.blur as blur


def test_opaque_via_env(monkeypatch):
    monkeypatch.setenv("JT_OPAQUE", "1")
    assert blur.opaque_mode_active() is True
    assert blur.status() is blur.BlurStatus.UNSUPPORTED


def test_not_opaque_by_default(monkeypatch):
    monkeypatch.delenv("JT_OPAQUE", raising=False)
    assert blur.opaque_mode_active() is False


def test_setting_no_longer_forces_opaque(monkeypatch):
    """A stale ``opaque_mode`` setting must NOT re-enable opaque chrome — the
    diagnostic is env-only now, so a user who once toggled it reverts to the
    automatic near-opaque fallback (which keeps rounded corners)."""
    monkeypatch.delenv("JT_OPAQUE", raising=False)

    class _S:
        opaque_mode = True  # stale value from before the toggle was removed

    monkeypatch.setattr("jellytoast.settings.get_settings", lambda: _S())
    assert blur.opaque_mode_active() is False
