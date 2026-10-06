"""packaging/macos/merge_universal.sh — the universal .dmg merge.

Runs the REAL script on fake .app trees with tiny stand-ins for macOS's
``file`` / ``lipo`` / ``otool`` (each fake binary's first line says which
arches it carries; ``DEP:<arch>:<name>`` lines are its per-arch library
references), so the merge logic is exercised on Linux CI.

Regression: Homebrew stopped building Intel bottles (2026-10), so the x86_64
leg froze on libx265.216 while arm64 moved to libx265.217 and the merge
refused the build. A soname-only skew whose slices all resolve is now
accepted (both thin copies ship); anything else one-sided still fails.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "packaging" / "macos" / "merge_universal.sh"

_FILE = """#!/usr/bin/env bash
# file -b PATH
p="${@: -1}"
if head -n1 "$p" 2>/dev/null | grep -q '^ARCH:'; then echo "Mach-O stub"; else echo "data"; fi
"""

_LIPO = """#!/usr/bin/env bash
if [ "$1" = "-archs" ]; then
    head -n1 "$2" | sed 's/^ARCH://'
    exit 0
fi
if [ "$1" = "-create" ]; then
    a="$2"; b="$3"; out="$5"
    {
        echo "ARCH:$(head -n1 "$a" | sed 's/^ARCH://') $(head -n1 "$b" | sed 's/^ARCH://')"
        tail -n +2 "$a"; tail -n +2 "$b"
    } > "$out"
    exit 0
fi
exit 1
"""

_OTOOL = """#!/usr/bin/env bash
# otool -arch ARCH -L PATH
arch="$2"; p="$4"
echo "$p:"
grep "^DEP:$arch:" "$p" | while IFS=: read -r _ _ name; do
    printf '\\t@rpath/%s (compatibility version 0.0.0)\\n' "$name"
done
"""


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _stub_bin(tmp: Path) -> Path:
    bin_dir = tmp / "stubs"
    bin_dir.mkdir()
    for name, body in (("file", _FILE), ("lipo", _LIPO), ("otool", _OTOOL)):
        p = bin_dir / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def _app(root: Path, arch: str, x265: str, avcodec_dep: str | None = None, extra: dict = None):
    app = root / f"{arch}.app"
    _write(app / "Contents/MacOS/jellytoast", f"ARCH:{arch}\n")
    dep = avcodec_dep or x265
    _write(app / "Contents/Frameworks/libavcodec.62.dylib", f"ARCH:{arch}\nDEP:{arch}:{dep}\n")
    _write(app / f"Contents/Frameworks/{x265}", f"ARCH:{arch}\n")
    _write(app / "Contents/Resources/base_library.zip", "zip\n")
    (app / "Contents/Resources").mkdir(parents=True, exist_ok=True)
    os.symlink(f"../Frameworks/{x265}", app / f"Contents/Resources/{x265}")
    for rel, text in (extra or {}).items():
        _write(app / rel, text)
    return app


def _merge(tmp: Path, arm: Path, x86: Path):
    out = tmp / "out" / "jellytoast.app"
    env = dict(os.environ, PATH=f"{_stub_bin(tmp)}:{os.environ['PATH']}")
    proc = subprocess.run(
        ["bash", str(SCRIPT), str(arm), str(x86), str(out)],
        env=env,
        capture_output=True,
        text=True,
    )
    return proc, out


@pytest.mark.skipif(os.name != "posix", reason="bash script")
class TestMergeUniversal:
    def test_identical_versions_fuse(self, tmp_path):
        arm = _app(tmp_path, "arm64", "libx265.217.dylib")
        x86 = _app(tmp_path, "x86_64", "libx265.217.dylib")
        proc, out = _merge(tmp_path, arm, x86)
        assert proc.returncode == 0, proc.stderr
        fw = out / "Contents/Frameworks"
        assert (fw / "libx265.217.dylib").read_text().startswith("ARCH:arm64 x86_64")
        assert "soname skew" not in proc.stdout

    def test_soname_skew_ships_both_thin_copies(self, tmp_path):
        arm = _app(tmp_path, "arm64", "libx265.217.dylib")
        x86 = _app(tmp_path, "x86_64", "libx265.216.dylib")
        proc, out = _merge(tmp_path, arm, x86)
        assert proc.returncode == 0, proc.stderr
        fw = out / "Contents/Frameworks"
        assert (fw / "libx265.217.dylib").read_text().startswith("ARCH:arm64\n")
        assert (fw / "libx265.216.dylib").read_text().startswith("ARCH:x86_64\n")
        # The consumer is fused and each slice names its own soname.
        avcodec = (fw / "libavcodec.62.dylib").read_text()
        assert avcodec.startswith("ARCH:arm64 x86_64")
        assert "DEP:arm64:libx265.217.dylib" in avcodec
        assert "DEP:x86_64:libx265.216.dylib" in avcodec
        # PyInstaller's Resources -> Frameworks cross-links exist for both.
        res = out / "Contents/Resources"
        assert os.readlink(res / "libx265.216.dylib") == "../Frameworks/libx265.216.dylib"
        assert os.readlink(res / "libx265.217.dylib") == "../Frameworks/libx265.217.dylib"
        assert "soname skew accepted" in proc.stdout

    def test_skew_that_does_not_resolve_fails(self, tmp_path):
        # The Intel avcodec slice wants a soname neither tree ships.
        arm = _app(tmp_path, "arm64", "libx265.217.dylib")
        x86 = _app(tmp_path, "x86_64", "libx265.216.dylib", avcodec_dep="libx265.215.dylib")
        proc, out = _merge(tmp_path, arm, x86)
        assert proc.returncode != 0
        assert "libx265.215.dylib" in proc.stderr and "not in the bundle" in proc.stderr
        assert not out.exists()

    def test_unrelated_one_sided_mach_o_still_fails(self, tmp_path):
        arm = _app(
            tmp_path,
            "arm64",
            "libx265.217.dylib",
            extra={"Contents/Frameworks/libonlyarm.dylib": "ARCH:arm64\n"},
        )
        x86 = _app(tmp_path, "x86_64", "libx265.217.dylib")
        proc, out = _merge(tmp_path, arm, x86)
        assert proc.returncode != 0
        assert "libonlyarm.dylib" in proc.stderr
        assert "refusing to ship a half-thin bundle" in proc.stderr
        assert not out.exists()
