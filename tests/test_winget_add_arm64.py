"""dev/winget_add_arm64.py: komac emits only x64 entries for the universal
setup.exe; the patch copies each as arm64 (same URL / hash / scope)."""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "winget_add_arm64", Path(__file__).parent.parent / "dev" / "winget_add_arm64.py"
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

KOMAC = """PackageIdentifier: wolfgangwarehaus.jellytoast
PackageVersion: 0.2.3
InstallerType: inno
Installers:
- Architecture: x64
  Scope: user
  InstallerUrl: https://example/jellytoast-0.2.3-windows-setup.exe
  InstallerSha256: AAA
  InstallerSwitches:
    Custom: /CURRENTUSER
- Architecture: x64
  Scope: machine
  InstallerUrl: https://example/jellytoast-0.2.3-windows-setup.exe
  InstallerSha256: AAA
  InstallerSwitches:
    Custom: /ALLUSERS
ManifestType: installer
ManifestVersion: 1.12.0
"""


def test_duplicates_each_x64_entry_as_arm64():
    out = mod.add_arm64(KOMAC)
    assert out.count("- Architecture: x64") == 2
    assert out.count("- Architecture: arm64") == 2
    arm = out[out.index("- Architecture: arm64"):out.index("ManifestType:")]
    assert "Scope: user" in arm and "Scope: machine" in arm and "/ALLUSERS" in arm
    assert out.endswith("ManifestType: installer\nManifestVersion: 1.12.0\n")


def test_noop_when_arm64_present():
    assert mod.add_arm64(mod.add_arm64(KOMAC)) is None


def test_crlf_manifest_keeps_crlf():
    # komac 2.17 (the 0.2.4 release) writes CRLF line endings.
    crlf = KOMAC.replace("\n", "\r\n")
    out = mod.add_arm64(crlf)
    assert out is not None
    assert out.count("- Architecture: arm64") == KOMAC.count("- Architecture: x64")
    assert "\r\n" in out and "\n" not in out.replace("\r\n", "")
    assert mod.add_arm64(out) is None  # already patched → no-op
