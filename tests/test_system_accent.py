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


_APPEARANCE_NS = "org.freedesktop.appearance"
