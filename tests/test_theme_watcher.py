"""pywal / wallust follow (0.1.7 P2a): colors.json adapter + the follower's
gating and debounce plumbing. No real pywal or filesystem events — the parser
gets fabricated payloads and the follower's callbacks are driven directly."""

from __future__ import annotations

import json

import pytest

from jellytoast.external_theme import (
    Base16ParseError,
    contrast_ratio,
    ensure_readable,
    parse_pywal_json,
)


def _wal_payload(bg="#0e0e15", fg="#c5c8c9", wallpaper="/walls/forest.png"):
    return {
        "wallpaper": wallpaper,
        "special": {"background": bg, "foreground": fg, "cursor": fg},
        "colors": {f"color{i}": f"#{i:02x}{i * 3 % 256:02x}{i * 7 % 256:02x}" for i in range(16)},
    }


class TestParsePywalJson:
    def test_happy_path_maps_slots(self):
        d = _wal_payload()
        p = parse_pywal_json(json.dumps(d))
        assert p.base16["base00"] == "#0e0e15"
        assert p.base16["base05"] == "#c5c8c9"
        assert p.base16["base0D"] == d["colors"]["color4"]  # accent slot
        assert p.accent_slot == "base0D"
        assert p.variant == "dark"  # inferred from the near-black bg
        assert p.name == "pywal — forest.png"
        # every base16 slot present → the shared mapper can consume it
        assert all(f"base0{c}" in p.base16 for c in "0123456789ABCDEF")

    def test_light_wallpaper_infers_light(self):
        d = _wal_payload(bg="#f4ecd8", fg="#403c34", wallpaper="")
        p = parse_pywal_json(json.dumps(d))
        assert p.variant == "light"
        assert p.name == "pywal"

    def test_hex_without_hash_normalizes(self):
        d = _wal_payload()
        d["special"]["background"] = "0E0E15"
        p = parse_pywal_json(json.dumps(d))
        assert p.base16["base00"] == "#0e0e15"

    @pytest.mark.parametrize(
        "text",
        [
            "not json at all",
            "{}",
            json.dumps({"special": {}, "colors": {}}),
            json.dumps({"special": {"background": "zzz", "foreground": "#fff000"},
                        "colors": {f"color{i}": "#102030" for i in range(16)}}),
        ],
    )
    def test_malformed_raises(self, text):
        with pytest.raises(Base16ParseError):
            parse_pywal_json(text)

    def test_unreadable_palette_gets_clamped(self):
        # bg ≈ fg (a washed-out wallpaper) → fg must be clamped to readable
        d = _wal_payload(bg="#303030", fg="#343434")
        p = parse_pywal_json(json.dumps(d))
        assert contrast_ratio(p.base16["base05"], p.base16["base00"]) >= 3.0


class TestEnsureReadable:
    def test_readable_scheme_is_untouched(self):
        d = _wal_payload()
        p = parse_pywal_json(json.dumps(d))
        assert ensure_readable(p) is p


class TestPywalFollower:
    def test_fs_event_gated_off_does_not_debounce(self, qapp, isolated_settings):
        from jellytoast.theme_watcher import PywalFollower

        isolated_settings.follow_pywal = False
        f = PywalFollower()
        f._on_fs_event("/whatever")
        assert not f._debounce.isActive()

    def test_fs_event_gated_on_debounces_and_fires_apply(
        self, qapp, isolated_settings, monkeypatch
    ):
        from jellytoast import theme_watcher

        isolated_settings.follow_pywal = True
        applied = []
        monkeypatch.setattr(
            theme_watcher, "pywal_apply_once", lambda **k: applied.append(1)
        )
        f = theme_watcher.PywalFollower()
        f._on_fs_event("/whatever")
        assert f._debounce.isActive()
        f._debounce.stop()
        f._fire()  # what the debounce timeout runs
        assert applied == [1]

    def test_apply_text_applies_via_imported_preset(
        self, qapp, isolated_settings, monkeypatch
    ):
        from jellytoast import theme_watcher

        seen = {}

        def _fake_apply(preset, frosted):
            seen["name"] = preset.name
            seen["frosted"] = frosted

        import jellytoast.theme_presets as tp

        monkeypatch.setattr(tp, "apply_imported_preset", _fake_apply)
        isolated_settings.frosted = True
        theme_watcher._apply_text(json.dumps(_wal_payload()))
        assert seen["name"].startswith("pywal")
        assert seen["frosted"] is True

    def test_apply_text_bad_payload_reports_not_raises(self, qapp):
        from jellytoast import theme_watcher

        errs = []
        theme_watcher._apply_text("garbage", on_error=errs.append)
        assert errs and "colors.json" in errs[0]


class TestBase16SanityGuards:
    def test_mislabeled_variant_is_corrected_by_luminance(self):
        from jellytoast.external_theme import parse_base16_yaml

        light_bg_scheme = "variant: dark\n" + "\n".join(
            f"base0{c}: {v}"
            for c, v in zip(
                "0123456789ABCDEF",
                ["#fdf6e3", "#eee8d5", "#d5cdb4", "#839496", "#657b83", "#586e75",
                 "#073642", "#002b36", "#dc322f", "#cb4b16", "#b58900", "#859900",
                 "#2aa198", "#268bd2", "#6c71c4", "#d33682"],
                strict=True,
            )
        )
        p = parse_base16_yaml(light_bg_scheme)
        assert p.variant == "light"

    def test_ambiguous_luminance_keeps_declared_variant(self):
        from jellytoast.external_theme import parse_base16_yaml

        mid_scheme = "variant: light\n" + "\n".join(
            f"base0{c}: {v}"
            for c, v in zip(
                "0123456789ABCDEF",
                ["#8a8a8a", "#949494", "#9e9e9e", "#a8a8a8", "#3a3a3a", "#111111",
                 "#0a0a0a", "#000000", "#aa0000", "#aa5500", "#aaaa00", "#00aa00",
                 "#00aaaa", "#0055aa", "#5500aa", "#aa0055"],
                strict=True,
            )
        )
        p = parse_base16_yaml(mid_scheme)
        assert p.variant == "light"  # bg lum ≈ 138 — within the dead zone

    def test_unreadable_import_is_rejected(self):
        from jellytoast.external_theme import parse_base16_yaml

        bad = "\n".join(
            f"base0{c}: #303030" if c != "5" else "base05: #343434"
            for c in "0123456789ABCDEF"
        )
        with pytest.raises(Base16ParseError, match="too similar"):
            parse_base16_yaml(bad)
