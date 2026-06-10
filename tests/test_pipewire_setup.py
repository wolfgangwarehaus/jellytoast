"""Tests for the T4 audiophile PipeWire conf installer
(``jellytoast.pipewire_setup``).

The helper writes one conf file under
``~/.config/pipewire/pipewire.conf.d/`` and removes it cleanly. The
critical invariant is that ``uninstall()`` refuses to touch a file the
user wrote by hand at the same path — the ID-stamp header in our file
is what distinguishes "ours" from "theirs".

Every test routes ``Path.home()`` through ``tmp_path`` so the real user
config never sees a write.
"""

from pathlib import Path

import pytest

from jellytoast import pipewire_setup as pws


@pytest.fixture
def tmp_home(tmp_path: Path) -> Path:
    """A throwaway home dir. Tests pass it into ``install`` / ``uninstall``
    / ``is_installed`` via the ``home=`` parameter so the helper never
    touches the real ~/.config/pipewire on the test runner."""
    return tmp_path


def test_conf_path_under_user_config(tmp_home: Path):
    p = pws.conf_path(home=tmp_home)
    assert p == tmp_home / ".config/pipewire/pipewire.conf.d/10-jellytoast-bitperfect.conf"


def test_is_installed_false_when_missing(tmp_home: Path):
    assert pws.is_installed(home=tmp_home) is False


def test_install_writes_file_with_header_and_body(tmp_home: Path):
    pws.install(home=tmp_home)
    p = pws.conf_path(home=tmp_home)
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    # ID-stamp header is the uninstall safety check.
    assert pws.CONF_HEADER in text
    # Recipe properties from research §4.2 — both must land verbatim
    # or PipeWire won't actually rate-match.
    assert "default.clock.allowed-rates" in text
    assert "resample.quality = 14" in text


def test_install_creates_parent_dirs(tmp_home: Path):
    """First-install path is the most common — fresh user with no
    existing pipewire user config. ``mkdir(parents=True)`` must
    materialize the whole chain."""
    assert not (tmp_home / ".config" / "pipewire").exists()
    pws.install(home=tmp_home)
    assert (tmp_home / ".config/pipewire/pipewire.conf.d").is_dir()


def test_install_is_idempotent(tmp_home: Path):
    """Clicking the button twice in a row is a no-op the second time —
    same contents on disk, no error raised."""
    pws.install(home=tmp_home)
    first = pws.conf_path(home=tmp_home).read_text(encoding="utf-8")
    pws.install(home=tmp_home)
    second = pws.conf_path(home=tmp_home).read_text(encoding="utf-8")
    assert first == second


def test_is_installed_true_after_install(tmp_home: Path):
    pws.install(home=tmp_home)
    assert pws.is_installed(home=tmp_home) is True


def test_uninstall_removes_our_file(tmp_home: Path):
    pws.install(home=tmp_home)
    p = pws.conf_path(home=tmp_home)
    assert p.exists()

    assert pws.uninstall(home=tmp_home) is True
    assert not p.exists()
    assert pws.is_installed(home=tmp_home) is False


def test_uninstall_noop_when_file_missing(tmp_home: Path):
    """Returns False (not an exception) so the UI can present a calm
    state regardless of whether install ever happened."""
    assert pws.uninstall(home=tmp_home) is False


def test_uninstall_refuses_to_touch_user_authored_file(tmp_home: Path):
    """The safety invariant. A user with the same filename but their
    own contents must NOT lose their work to jellytoast's uninstall
    path — we only own files carrying our ID-stamp header."""
    p = pws.conf_path(home=tmp_home)
    p.parent.mkdir(parents=True, exist_ok=True)
    user_contents = (
        "# my own pipewire tweaks — not jellytoast's\n"
        "context.properties = {\n"
        "    default.clock.rate = 96000\n"
        "}\n"
    )
    p.write_text(user_contents, encoding="utf-8")

    assert pws.uninstall(home=tmp_home) is False
    # File is still on disk, untouched.
    assert p.exists()
    assert p.read_text(encoding="utf-8") == user_contents


def test_is_installed_false_for_user_authored_file(tmp_home: Path):
    """Same invariant viewed from the read side — ``is_installed``
    reports False for a file at the same path that lacks our ID-stamp,
    so the Settings button reads "Install" (not "Remove") and the user
    can opt in without overwriting their own file."""
    p = pws.conf_path(home=tmp_home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# my own conf\n", encoding="utf-8")

    assert pws.is_installed(home=tmp_home) is False


def test_install_then_uninstall_then_reinstall(tmp_home: Path):
    """Round-trip — Install → Remove → Install lands cleanly each time
    and the disk state matches expectations on every step."""
    pws.install(home=tmp_home)
    assert pws.is_installed(home=tmp_home)
    pws.uninstall(home=tmp_home)
    assert not pws.is_installed(home=tmp_home)
    pws.install(home=tmp_home)
    assert pws.is_installed(home=tmp_home)
