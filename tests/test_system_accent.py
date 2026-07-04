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
    from jellytoast import icons as _icons
    from jellytoast import ui_helpers as _uih
    from jellytoast.system_accent import apply_accent_now

    prev_accent = isolated_settings.accent_color
    try:
        apply_accent_now("#ff8800")
        assert ct.get_current("ACCENT") == "#ff8800"
        # the accent cascade fired off ACCENT alone
        assert ct.get_current("ACCENT_DEEP").startswith("#")
        assert isolated_settings.accent_color == "#ff8800"
    finally:
        # apply_accent_now mutates the ui_helpers/icons module constants
        # via refresh_theme; reset_all alone leaves them at #ff8800 for
        # the rest of the worker (the order-dependent
        # test_no_override_for_token_leaves_default flake). Restore the
        # setting, then re-derive the constants from it.
        ct.reset_all()
        isolated_settings.accent_color = prev_accent
        _uih.refresh_theme()
        _icons.refresh_theme()


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

    from jellytoast import system_accent
    from jellytoast.system_accent import SystemAccentFollower

    calls: list[tuple] = []

    class _FakeBus:
        def connect(self, *args):
            calls.append(args)
            return True

    monkeypatch.setattr(QDBusConnection, "sessionBus", staticmethod(_FakeBus))
    # Pin the PORTAL branch: on a Windows/macOS host _subscribe routes to the
    # native backend and never touches QtDBus.
    monkeypatch.setattr(system_accent, "_IS_WINDOWS", False)
    monkeypatch.setattr(system_accent, "_IS_MACOS", False)
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
    # The family dropdown persists the built-in family as the "jellytoast"
    # sentinel — regression: the gate treated it as a preset and went dead.
    isolated_settings.theme_family = "jellytoast"
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


class TestWindowsAccentBackend:
    def test_abgr_to_hex(self):
        from jellytoast.system_accent import _abgr_to_hex

        # DWM AccentColor is 0xAABBGGRR: blue=0xd7, green=0x77, red=0x00
        assert _abgr_to_hex(0xFFD77700) == "#0077d7"
        assert _abgr_to_hex(0x00000000) == "#000000"
        assert _abgr_to_hex(0xFFFFFFFF) == "#ffffff"
        assert _abgr_to_hex("junk") is None
        assert _abgr_to_hex(-1) is None
        assert _abgr_to_hex(2**33) is None

    def test_read_dispatches_to_windows_reader(self, monkeypatch):
        from jellytoast import system_accent as sa

        monkeypatch.setattr(sa, "_IS_WINDOWS", True)
        # on a real Mac the macOS reader wins the dispatch otherwise
        monkeypatch.setattr(sa, "_IS_MACOS", False)
        monkeypatch.setattr(sa, "_read_windows_accent", lambda: "#0077d7")
        assert sa.read_system_accent() == "#0077d7"

    def test_windows_subscribe_installs_native_filter(
        self, qapp, isolated_settings, monkeypatch
    ):
        from jellytoast import system_accent as sa

        monkeypatch.setattr(sa, "_IS_WINDOWS", True)
        monkeypatch.setattr(sa, "_IS_MACOS", False)
        f = sa.SystemAccentFollower()
        f._subscribe()
        assert f._subscribed is True
        assert getattr(f, "_win_filter", None) is not None
        # teardown: don't leave an app-wide filter behind for other tests
        qapp.removeNativeEventFilter(f._win_filter)

    def test_windows_change_handler_gates_like_portal(
        self, qapp, isolated_settings, monkeypatch
    ):
        from jellytoast import system_accent as sa

        synced = {"n": 0}
        monkeypatch.setattr(
            sa.SystemAccentFollower,
            "_sync_now",
            lambda self: synced.__setitem__("n", synced["n"] + 1),
        )
        f = sa.SystemAccentFollower()
        isolated_settings.follow_system_accent = True
        isolated_settings.theme_family = "catppuccin"  # preset → gated off
        f._on_windows_accent_changed()
        assert synced["n"] == 0
        isolated_settings.theme_family = ""  # built-in → drives
        f._on_windows_accent_changed()
        assert synced["n"] == 1


class TestMacOSAccentBackend:
    def test_read_macos_accent_converts_srgb(self, monkeypatch):
        import sys
        import types

        # Fake AppKit: controlAccentColor → sRGB 0.0/0.47/0.84 (Big Sur blue-ish)
        class _RGB:
            def redComponent(self):
                return 0.0

            def greenComponent(self):
                return 0.47843

            def blueComponent(self):
                return 0.84314

        class _Color:
            def colorUsingColorSpace_(self, _space):
                return _RGB()

        appkit = types.ModuleType("AppKit")
        appkit.NSColor = types.SimpleNamespace(controlAccentColor=lambda: _Color())
        appkit.NSColorSpace = types.SimpleNamespace(sRGBColorSpace=lambda: object())
        monkeypatch.setitem(sys.modules, "AppKit", appkit)

        from jellytoast.system_accent import _read_macos_accent

        assert _read_macos_accent() == "#007ad7"

    def test_read_macos_accent_none_on_missing_appkit(self, monkeypatch):
        import sys

        # No AppKit importable → None, never raises.
        monkeypatch.setitem(sys.modules, "AppKit", None)
        from jellytoast.system_accent import _read_macos_accent

        assert _read_macos_accent() is None

    def test_read_dispatches_to_macos_reader(self, monkeypatch):
        from jellytoast import system_accent as sa

        monkeypatch.setattr(sa, "_IS_MACOS", True)
        monkeypatch.setattr(sa, "_read_macos_accent", lambda: "#007ad7")
        assert sa.read_system_accent() == "#007ad7"

    def test_macos_change_handler_gates_like_portal(
        self, qapp, isolated_settings, monkeypatch
    ):
        from jellytoast import system_accent as sa

        synced = {"n": 0}
        monkeypatch.setattr(
            sa.SystemAccentFollower,
            "_sync_now",
            lambda self: synced.__setitem__("n", synced["n"] + 1),
        )
        f = sa.SystemAccentFollower()
        isolated_settings.follow_system_accent = True
        isolated_settings.theme_family = "catppuccin"  # preset → gated off
        f._on_macos_accent_changed()
        assert synced["n"] == 0
        isolated_settings.theme_family = ""  # built-in → drives
        f._on_macos_accent_changed()
        assert synced["n"] == 1

    def test_macos_sync_now_reads_inline_not_on_worker(
        self, qapp, isolated_settings, monkeypatch
    ):
        """macOS must NOT push the AppKit read to a worker thread."""
        from jellytoast import system_accent as sa

        monkeypatch.setattr(sa, "_IS_MACOS", True)
        monkeypatch.setattr(sa, "read_system_accent", lambda: "#112233")
        applied = {}
        monkeypatch.setattr(sa, "apply_accent_now", lambda h: applied.setdefault("h", h))

        def _boom(*a, **k):
            raise AssertionError("run_async must not be used on macOS _sync_now")

        import jellytoast.async_io as aio

        monkeypatch.setattr(aio, "run_async", _boom)
        sa.SystemAccentFollower()._sync_now()
        assert applied["h"] == "#112233"


def test_resync_system_accent_dispatches(qapp, isolated_settings, monkeypatch):
    """The shared resync helper reads + applies the OS accent (mac inline;
    Linux/Windows via worker). Pin the mac-inline path for determinism."""
    from jellytoast import system_accent as sa

    monkeypatch.setattr(sa, "_IS_MACOS", True)
    monkeypatch.setattr(sa, "read_system_accent", lambda: "#0088cc")
    applied = {}
    monkeypatch.setattr(sa, "apply_accent_now", lambda h: applied.setdefault("h", h))
    sa.resync_system_accent()
    assert applied["h"] == "#0088cc"
