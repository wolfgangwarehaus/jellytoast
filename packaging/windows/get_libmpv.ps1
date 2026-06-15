# Download the pinned libmpv build and stage libmpv-2.dll where the
# PyInstaller spec expects it (packaging/windows/libmpv/libmpv-2.dll).
#
# Source: shinchiro's mpv-winbuild-cmake releases — the de-facto
# upstream for Windows libmpv (what mpv.io links to). Pinned by URL +
# SHA-256 so a release build is reproducible; bump both together.
#
# Run from the repo root:  pwsh packaging/windows/get_libmpv.ps1

$ErrorActionPreference = "Stop"

# Plain x86_64 (not -v3) so older CPUs stay supported.
$Url = "https://github.com/shinchiro/mpv-winbuild-cmake/releases/download/20260610/mpv-dev-x86_64-20260610-git-304426c.7z"
$Sha256 = "8cbb25ea784f01afbb3f904217cab1317430a8bcfd5680fd827a866367f71cc9"

$Dest = Join-Path $PSScriptRoot "libmpv"
$Archive = Join-Path $env:TEMP "libmpv-dev.7z"

Invoke-WebRequest -Uri $Url -OutFile $Archive

$actual = (Get-FileHash -Algorithm SHA256 $Archive).Hash.ToLower()
if ($actual -ne $Sha256.ToLower()) {
    throw "libmpv archive SHA-256 mismatch: expected $Sha256, got $actual"
}

New-Item -ItemType Directory -Force -Path $Dest | Out-Null
# 7z.exe ships on the GitHub windows runners.
& 7z e $Archive -o"$Dest" libmpv-2.dll -y
if (-not (Test-Path (Join-Path $Dest "libmpv-2.dll"))) {
    throw "libmpv-2.dll not found in the archive"
}
Write-Host "libmpv-2.dll staged at $Dest"
