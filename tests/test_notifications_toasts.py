"""The notification `tag` replace-plumbing and the now-playing trigger.

The live Windows toast surface is verified on-device; here we assert the
backend-agnostic logic that runs on Linux CI: the facade forwards the
tag, the Linux backend turns it into the daemon replace-hint, the Windows
backend degrades to a clean no-op without its package, and the
now-playing notifier respects the setting and only fires on real changes.

Imports happen at call time + cross-module patches go through the package
path, so this file is robust to the reload-based fixture in
test_notifications.py running adjacent under pytest-randomly.
"""

import types


class _NP:
    def __init__(self, item_id="1", title="T", subtitle="A", album="Alb"):
        self.item_id = item_id
        self.title = title
        self.subtitle = subtitle
        self.album = album


def _arm_track_change(monkeypatch, on):
    from jellytoast import settings as settings_mod

    monkeypatch.setattr(
        settings_mod,
        "get_settings",
        lambda: types.SimpleNamespace(notify_on_track_change=on),
    )


def _arm_capture(monkeypatch):
    calls = []

    def fake(title, body="", icon=None, app_name="jellytoast", tag=None):
        calls.append((title, body, tag))

    # Patch the package attribute the notifier imports at call time.
    monkeypatch.setattr("jellytoast.notifications.notify", fake)
    return calls


class TestFacadeTag:
    def test_forwards_tag_to_backend(self, monkeypatch):
        import jellytoast.notifications as notif

        captured = {}
        monkeypatch.setattr(
            notif,
            "_backend",
            types.SimpleNamespace(
                is_supported=lambda: True,
                notify=lambda *a: captured.update(args=a),
            ),
        )
        notif.notify("T", "B", tag="grp")
        assert captured["args"] == ("T", "B", None, "jellytoast", "grp")


class TestLinuxReplaceHint:
    def test_tag_becomes_synchronous_hint(self, monkeypatch):
        from jellytoast.notifications import _linux

        monkeypatch.setattr(_linux, "_notify_send_bin", lambda: "/usr/bin/notify-send")
        captured = {}
        monkeypatch.setattr(
            _linux.subprocess, "run", lambda cmd, **kw: captured.update(cmd=cmd)
        )
        _linux.notify("Title", "Body", tag="np")
        cmd = captured["cmd"]
        assert "--hint" in cmd
        assert (
            cmd[cmd.index("--hint") + 1] == "string:x-canonical-private-synchronous:np"
        )

    def test_no_tag_no_hint(self, monkeypatch):
        from jellytoast.notifications import _linux

        monkeypatch.setattr(_linux, "_notify_send_bin", lambda: "/usr/bin/notify-send")
        captured = {}
        monkeypatch.setattr(
            _linux.subprocess, "run", lambda cmd, **kw: captured.update(cmd=cmd)
        )
        _linux.notify("Title", "Body")
        assert "--hint" not in captured["cmd"]


class TestWindowsBackendNoop:
    def test_unsupported_and_noop_without_package(self):
        from jellytoast.notifications import _windows

        # windows_toasts isn't installed on Linux → toaster is None.
        assert _windows.is_supported() is False
        _windows.notify("t", "b", tag="x")  # must not raise


class TestNowPlayingNotifier:
    def test_no_toast_when_disabled(self, qapp, monkeypatch):
        from jellytoast.notifications import nowplaying

        _arm_track_change(monkeypatch, False)
        calls = _arm_capture(monkeypatch)
        nowplaying.NowPlayingNotifier()._on_started(_NP())
        assert calls == []

    def test_toast_on_track_change(self, qapp, monkeypatch):
        from jellytoast.notifications import nowplaying

        _arm_track_change(monkeypatch, True)
        calls = _arm_capture(monkeypatch)
        nowplaying.NowPlayingNotifier()._on_started(
            _NP(item_id="1", title="Song", subtitle="Artist", album="Album")
        )
        assert len(calls) == 1
        title, body, tag = calls[0]
        assert title == "Song"
        assert body == "Artist — Album"
        assert tag == nowplaying._TAG

    def test_body_omits_missing_parts(self, qapp, monkeypatch):
        from jellytoast.notifications import nowplaying

        _arm_track_change(monkeypatch, True)
        calls = _arm_capture(monkeypatch)
        nowplaying.NowPlayingNotifier()._on_started(
            _NP(item_id="1", title="Song", subtitle="Artist", album="")
        )
        assert calls[0][1] == "Artist"

    def test_dedup_same_track(self, qapp, monkeypatch):
        from jellytoast.notifications import nowplaying

        _arm_track_change(monkeypatch, True)
        calls = _arm_capture(monkeypatch)
        n = nowplaying.NowPlayingNotifier()
        np = _NP(item_id="x")
        n._on_started(np)
        n._on_started(np)
        assert len(calls) == 1

    def test_new_track_retoasts(self, qapp, monkeypatch):
        from jellytoast.notifications import nowplaying

        _arm_track_change(monkeypatch, True)
        calls = _arm_capture(monkeypatch)
        n = nowplaying.NowPlayingNotifier()
        n._on_started(_NP(item_id="a"))
        n._on_started(_NP(item_id="b"))
        assert len(calls) == 2
