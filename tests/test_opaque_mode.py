"""Settings → Display "Opaque background" toggle (persistent JT_OPAQUE).

``blur.opaque_mode_active()`` is True via the JT_OPAQUE env switch OR the
persisted ``settings.opaque_mode``. When on, ``status()`` reports UNSUPPORTED so
frosted bodies + popups fall back to their opaque alpha (and ``apply()`` skips
requesting blur). 2026-06-07.
"""

import modules.blur as blur


def test_opaque_via_env(monkeypatch):
    monkeypatch.setenv("JT_OPAQUE", "1")
    assert blur.opaque_mode_active() is True
    assert blur.status() is blur.BlurStatus.UNSUPPORTED


def test_opaque_via_setting(monkeypatch):
    monkeypatch.delenv("JT_OPAQUE", raising=False)

    class _S:
        opaque_mode = True

    monkeypatch.setattr("modules.settings.get_settings", lambda: _S())
    assert blur.opaque_mode_active() is True
    assert blur.status() is blur.BlurStatus.UNSUPPORTED


def test_not_opaque_by_default(monkeypatch):
    monkeypatch.delenv("JT_OPAQUE", raising=False)

    class _S:
        opaque_mode = False

    monkeypatch.setattr("modules.settings.get_settings", lambda: _S())
    assert blur.opaque_mode_active() is False
