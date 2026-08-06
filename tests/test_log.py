"""jellytoast.log — the opt-in rotating file log.

install() is process-global (it mutates the ROOT logger), so every test here
resets the module's idempotency latch and detaches whatever handlers it added;
the log dir is monkeypatched into tmp_path so the suite never writes the real
app data dir. Without both, a stray file handler would follow the rest of the
suite around and pytest-randomly would surface it as a phantom failure.
"""

from __future__ import annotations

import logging

import pytest

from jellytoast import log as jlog

# Grabbed before the autouse fixture ever patches it — the one test that wants
# the REAL resolver can't use monkeypatch.undo() (the fixture shares this
# test's monkeypatch instance, so undoing would also unwind its reset).
_REAL_LOG_DIR = jlog.log_dir


@pytest.fixture(autouse=True)
def _fresh_install(tmp_path, monkeypatch):
    """Point the log dir at tmp_path and undo install()'s global effects."""
    monkeypatch.setattr(jlog, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(jlog, "_installed", False)
    monkeypatch.setattr(jlog, "_file_path", None)
    # app.py's module-level basicConfig may already have attached a console
    # handler; install() re-levels it, so snapshot and restore the levels too.
    root = logging.getLogger()
    before = list(root.handlers)
    before_levels = [(h, h.level) for h in before]
    before_level = root.level
    yield
    for h in root.handlers[:]:
        if h not in before:
            root.removeHandler(h)
            h.close()
    for h, lvl in before_levels:
        h.setLevel(lvl)
    root.setLevel(before_level)


def test_install_creates_rotating_file_log(tmp_path):
    assert jlog.install() is True
    path = jlog.log_file_path()
    assert path is not None
    assert path.parent == tmp_path / "logs"
    assert path.name == "jellytoast.log"
    logging.getLogger("jellytoast.test").info("hello from the suite")
    for h in logging.getLogger().handlers:
        h.flush()
    assert "hello from the suite" in path.read_text(encoding="utf-8")


def test_install_attaches_exactly_one_file_handler():
    from logging.handlers import RotatingFileHandler

    root = logging.getLogger()
    before = sum(isinstance(h, RotatingFileHandler) for h in root.handlers)
    jlog.install()
    after = sum(isinstance(h, RotatingFileHandler) for h in root.handlers)
    assert after == before + 1


def test_install_is_idempotent():
    root = logging.getLogger()
    jlog.install()
    n1 = len(root.handlers)
    jlog.install()  # second call must not stack handlers
    assert len(root.handlers) == n1


def test_console_handler_drops_to_warning():
    # app.py's basicConfig leaves an INFO console handler on the root; install()
    # re-levels THAT one rather than adding a second (which would double-print
    # every warning). Either way the terminal ends up quiet.
    root = logging.getLogger()
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    root.addHandler(console)
    jlog.install()
    streams = [h for h in root.handlers if type(h) is logging.StreamHandler]
    assert streams
    assert all(h.level == logging.WARNING for h in streams)


def test_jt_log_env_sets_debug(monkeypatch):
    monkeypatch.setenv("JT_LOG", "debug")
    jlog.install()
    assert logging.getLogger().level == logging.DEBUG


def test_legacy_jt_log_level_env_still_honored(monkeypatch):
    # The QA docs + install_doctor tell people to use JT_LOG_LEVEL; keep it
    # working, and let it lower the console too so `JT_LOG_LEVEL=DEBUG
    # jellytoast` still prints to the terminal as documented.
    monkeypatch.delenv("JT_LOG", raising=False)
    monkeypatch.setenv("JT_LOG_LEVEL", "DEBUG")
    jlog.install()
    root = logging.getLogger()
    assert root.level == logging.DEBUG
    streams = [h for h in root.handlers if type(h) is logging.StreamHandler]
    assert streams and all(h.level == logging.DEBUG for h in streams)


def test_unrecognised_level_falls_back_to_info(monkeypatch):
    monkeypatch.setenv("JT_LOG", "chatty")
    jlog.install()
    assert logging.getLogger().level == logging.INFO


def test_unwritable_dir_degrades_without_raising(monkeypatch):
    def _boom():
        raise OSError("no state dir here")

    monkeypatch.setattr(jlog, "log_dir", _boom)
    assert jlog.install() is False
    assert jlog.log_file_path() is None


def test_rotation_caps_file_size(monkeypatch):
    monkeypatch.setattr(jlog, "_MAX_BYTES", 2_000)
    jlog.install()
    lg = logging.getLogger("jellytoast.rotate")
    for i in range(200):
        lg.info("line %04d %s", i, "x" * 40)
    path = jlog.log_file_path()
    assert path.stat().st_size <= 2_100  # rolled over, never unbounded
    assert path.with_name(path.name + ".1").exists()


def test_log_dir_lands_under_the_app_tree(qapp):
    # Not the tmp_path stub — the real resolver. It must never hand back a
    # bare platform root, or logs would land outside jellytoast's own tree.
    d = _REAL_LOG_DIR()
    assert d.name == "logs"
    assert "jellytoast" in str(d).lower()


def test_open_logs_dir_is_false_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(jlog, "log_dir", lambda: tmp_path / "nope")
    assert jlog.open_logs_dir() is False
