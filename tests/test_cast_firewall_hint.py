"""Casting-page firewall hint: LAN-subnet derivation (``_lan_cidrs``) and the
copy-paste help text.

A firewall lets the discovery query out but drops the device's reply, so
AirPlay/DLNA never appear while Chromecast survives. The hint hands the user an
allow rule pre-filled with their own subnet, so these tests pin (a) that the
subnet is derived from the right interfaces and (b) that the help text drops it
into the OS-appropriate command.
"""

from __future__ import annotations


class _FakeIP:
    def __init__(self, ip, prefix, is_v4=True):
        self.ip = ip
        self.network_prefix = prefix
        self.is_IPv4 = is_v4


class _FakeAdapter:
    def __init__(self, ips):
        self.ips = ips


def _patch_adapters(monkeypatch, adapters):
    import ifaddr

    monkeypatch.setattr(ifaddr, "get_adapters", lambda: adapters)


class TestLanCidrs:
    def test_maps_host_ip_to_network_excludes_overlay_and_local(self, monkeypatch):
        from jellytoast.cast_manager import _lan_cidrs

        _patch_adapters(
            monkeypatch,
            [
                _FakeAdapter([_FakeIP("192.168.50.210", 24)]),
                _FakeAdapter([_FakeIP("100.71.43.71", 32)]),  # Tailscale CGNAT
                _FakeAdapter([_FakeIP("127.0.0.1", 8)]),  # loopback
                _FakeAdapter([_FakeIP("169.254.1.2", 16)]),  # link-local
            ],
        )
        assert _lan_cidrs() == ["192.168.50.0/24"]

    def test_dedupes_same_network_keeps_distinct(self, monkeypatch):
        from jellytoast.cast_manager import _lan_cidrs

        _patch_adapters(
            monkeypatch,
            [
                _FakeAdapter(
                    [_FakeIP("192.168.50.210", 24), _FakeIP("192.168.50.50", 24)]
                ),
                _FakeAdapter([_FakeIP("10.0.0.7", 24)]),
            ],
        )
        assert _lan_cidrs() == ["192.168.50.0/24", "10.0.0.0/24"]

    def test_skips_non_ipv4(self, monkeypatch):
        from jellytoast.cast_manager import _lan_cidrs

        _patch_adapters(
            monkeypatch,
            [
                _FakeAdapter([_FakeIP("fe80::1", 64, is_v4=False)]),
                _FakeAdapter([_FakeIP("192.168.1.5", 24)]),
            ],
        )
        assert _lan_cidrs() == ["192.168.1.0/24"]

    def test_default_prefix_when_missing(self, monkeypatch):
        from jellytoast.cast_manager import _lan_cidrs

        _patch_adapters(monkeypatch, [_FakeAdapter([_FakeIP("192.168.7.9", None)])])
        assert _lan_cidrs() == ["192.168.7.0/24"]

    def test_empty_on_enumeration_error(self, monkeypatch):
        import ifaddr

        from jellytoast.cast_manager import _lan_cidrs

        def _boom():
            raise OSError("nope")

        monkeypatch.setattr(ifaddr, "get_adapters", _boom)
        assert _lan_cidrs() == []


class TestFirewallHelpText:
    # _firewall_help_text doesn't touch self, so call it unbound with a dummy
    # self — avoids constructing the whole (Qt-heavy) SettingsDialog.
    def test_linux_text_has_subnet_and_ufw_rule(self, monkeypatch):
        import jellytoast.cast_manager as cm
        from jellytoast.settings_dialog import SettingsDialog

        monkeypatch.setattr(cm, "_lan_cidrs", lambda: ["192.168.50.0/24"])
        monkeypatch.setattr("jellytoast.platform_compat.IS_WINDOWS", False)
        monkeypatch.setattr("jellytoast.platform_compat.IS_MACOS", False)
        text = SettingsDialog._firewall_help_text(object())
        assert "sudo ufw allow from 192.168.50.0/24" in text
        assert "192.168.50.0/24" in text  # also in the firewalld rich-rule
        assert "5353" in text and "1900" in text  # port reference line

    def test_falls_back_when_subnet_unknown(self, monkeypatch):
        import jellytoast.cast_manager as cm
        from jellytoast.settings_dialog import SettingsDialog

        monkeypatch.setattr(cm, "_lan_cidrs", lambda: [])
        monkeypatch.setattr("jellytoast.platform_compat.IS_WINDOWS", False)
        monkeypatch.setattr("jellytoast.platform_compat.IS_MACOS", False)
        text = SettingsDialog._firewall_help_text(object())
        assert "your local subnet" in text

    def test_windows_text_is_os_specific(self, monkeypatch):
        import jellytoast.cast_manager as cm
        from jellytoast.settings_dialog import SettingsDialog

        monkeypatch.setattr(cm, "_lan_cidrs", lambda: ["192.168.50.0/24"])
        monkeypatch.setattr("jellytoast.platform_compat.IS_WINDOWS", True)
        text = SettingsDialog._firewall_help_text(object())
        assert "Windows" in text
        assert "ufw" not in text  # no Linux command on Windows
