"""XDG-portal accent reader — the (ddd)-variant → hex parse (no live D-Bus)."""

from __future__ import annotations

from jellytoast.system_accent import _accent_from_variant


def test_parses_ddd_variant_and_bare_triple(qapp):
    # jeepney's usual variant shape ("(ddd)", (r,g,b))
    assert _accent_from_variant(("(ddd)", (1.0, 0.0, 0.0))) == "#ff0000"
    # and a bare triple
    assert _accent_from_variant((0.0, 1.0, 0.0)) == "#00ff00"
    # mid values round-trip through the 0..1 → 0..255 scale
    assert _accent_from_variant((0.5, 0.5, 0.5)).lower() in ("#808080", "#7f7f7f")


def test_unset_or_malformed_returns_none(qapp):
    assert _accent_from_variant(("(ddd)", (-1.0, -1.0, -1.0))) is None  # portal "unset"
    assert _accent_from_variant((1.5, 0.0, 0.0)) is None  # out of range
    assert _accent_from_variant(None) is None
    assert _accent_from_variant(("(ddd)", (0.5, 0.5))) is None  # wrong arity
    assert _accent_from_variant("nope") is None


def test_apply_accent_now_sets_accent_and_cascades(qapp, isolated_settings):
    import jellytoast.color_tokens as ct
    from jellytoast.system_accent import apply_accent_now

    try:
        apply_accent_now("#ff8800")
        assert ct.get_current("ACCENT") == "#ff8800"
        # the accent cascade fired off ACCENT alone
        assert ct.get_current("ACCENT_DEEP").startswith("#")
        assert isolated_settings.accent_color == "#ff8800"
    finally:
        ct.reset_all()


def test_follower_start_is_safe_without_portal(qapp, isolated_settings):
    # No session portal in the test env — start() must never raise, and it
    # degrades to "not subscribed" gracefully.
    from jellytoast.system_accent import SystemAccentFollower

    isolated_settings.follow_system_accent = False
    f = SystemAccentFollower()
    f.start()  # must not raise
    f._on_setting_changed(_APPEARANCE_NS, "accent-color")  # handler is null-safe


def test_subscribe_uses_six_arg_qtdbus_form(qapp, isolated_settings, monkeypatch):
    """Regression: QtDBus needs connect(service, path, iface, name, receiver,
    SLOT(sig)) — the 6-arg form. The old 5-arg bound-method call raised
    TypeError, was swallowed, and left the live accent watch UNSUBSCRIBED (only
    the launch re-read worked). This pins the shape so a revert fails loudly."""
    from PySide6.QtDBus import QDBusConnection

    from jellytoast.system_accent import SystemAccentFollower

    calls: list[tuple] = []

    class _FakeBus:
        def connect(self, *args):
            calls.append(args)
            return True

    monkeypatch.setattr(QDBusConnection, "sessionBus", staticmethod(_FakeBus))
    f = SystemAccentFollower()
    f._subscribe()

    assert f._subscribed is True
    assert len(calls) == 1
    args = calls[0]
    assert len(args) == 6, f"QtDBus connect must be the 6-arg form, got {len(args)}"
    assert args[3] == "SettingChanged"
    assert args[4] is f  # receiver is the follower itself
    assert "_on_setting_changed" in str(args[5])  # the SLOT() signature string


_APPEARANCE_NS = "org.freedesktop.appearance"


def test_follow_accent_active_requires_builtin_family(qapp, isolated_settings):
    """Preset / imported families supply their own accent — the follower must
    only drive the accent on the built-in (empty) family, or the OS accent
    silently mismatches the preset palette while its toggle is hidden."""
    from jellytoast.system_accent import follow_accent_active

    isolated_settings.follow_system_accent = True
    isolated_settings.theme_family = ""
    assert follow_accent_active() is True

    isolated_settings.theme_family = "catppuccin"
    assert follow_accent_active() is False
    isolated_settings.theme_family = "imported"
    assert follow_accent_active() is False

    isolated_settings.theme_family = ""
    isolated_settings.follow_system_accent = False
    assert follow_accent_active() is False


def test_follower_is_gated_off_while_preset_family_active(
    qapp, isolated_settings, monkeypatch
):
    from jellytoast import system_accent as sa

    isolated_settings.follow_system_accent = True
    isolated_settings.theme_family = "catppuccin"

    synced = {"n": 0}
    monkeypatch.setattr(
        sa.SystemAccentFollower,
        "_sync_now",
        lambda self: synced.__setitem__("n", synced["n"] + 1),
    )
    monkeypatch.setattr(sa.SystemAccentFollower, "_subscribe", lambda self: None)

    f = sa.SystemAccentFollower()
    f.start()  # launch re-read must not fire on a preset family
    assert synced["n"] == 0
    f._on_setting_changed(_APPEARANCE_NS, "accent-color")  # nor the live watch
    assert synced["n"] == 0

    # back on the built-in family, the same stored toggle resumes driving
    isolated_settings.theme_family = ""
    f._on_setting_changed(_APPEARANCE_NS, "accent-color")
    assert synced["n"] == 1
