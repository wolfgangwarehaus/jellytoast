"""The suite must never write into the developer's real cache dir — the
crash / diagnostic logs live there and get attached to bug reports."""

import os
from pathlib import Path


def _real_home() -> Path:
    # xdist workers redirect $HOME, so ask the user database, not ~.
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError):  # Windows: no pwd; USERPROFILE is untouched
        return Path(os.environ.get("USERPROFILE", os.path.expanduser("~")))


def test_diag_logs_go_to_a_test_cache():
    from jellytoast.app import _diag_log_dir

    real = (_real_home() / ".cache").resolve()
    d = Path(_diag_log_dir()).resolve()
    assert os.environ.get("XDG_CACHE_HOME")
    assert real not in d.parents, d


def test_null_keyring_read_does_not_retry(monkeypatch):
    """No keyring backend → one read, no 5 × 100 ms retry stall (it turned
    Settings rebuilds in the suite into a minutes-long hang)."""
    import keyring
    from keyring.backends import null

    from jellytoast import credentials

    calls = []
    monkeypatch.setattr(keyring, "get_keyring", lambda: null.Keyring())
    monkeypatch.setattr(keyring, "get_password", lambda *a: calls.append(a))
    monkeypatch.setattr("time.sleep", lambda _s: calls.append("sleep"))
    assert credentials._keyring_get_token() is None
    assert calls.count("sleep") == 0 and len(calls) == 1
