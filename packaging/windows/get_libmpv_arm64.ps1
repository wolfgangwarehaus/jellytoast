# Download the pinned aarch64 (Arm64) libmpv build and stage libmpv-2.dll
# where the PyInstaller spec expects it
# (packaging/windows/libmpv/libmpv-2.dll).
#
# This is the Arm64 sibling of get_libmpv.ps1. The Windows arm64 build leg
# (release.yml, runner windows-11-arm) calls THIS script; the x64 leg calls
# get_libmpv.ps1. Both stage into the SAME path, so the leg that runs decides
# which architecture's libmpv-2.dll the spec bundles.
#
# Source: shinchiro's mpv-winbuild-cmake — the same upstream the x64 leg pins,
# which publishes an aarch64 dev asset (mpv-dev-aarch64-<date>-git-<hash>.7z)
# alongside the x86_64 one, built from the identical mpv/FFmpeg tree. Keeping
# both arches on one upstream/date means one licensing story and one bump.
#
# LICENSING NOTE: shinchiro's default build (this asset) links a GPL FFmpeg +
# LuaJIT, so it is conveyed as GPL — same as the x64 leg. The frozen bundle
# already ships COPYING (GPLv3) + THIRD-PARTY-NOTICES for exactly this reason,
# so the arm64 leg needs no extra compliance work. (If a pure-LGPL Windows
# bundle is ever wanted, swap BOTH legs to an mpv-dev-lgpl-* asset together —
# don't mix a GPL x64 dll with an LGPL arm64 dll under one installer.)
#
# UPSTREAM CAVEAT: mpv upstream marks the aarch64 Windows build "untested" and
# builds it with OpenGL disabled. jellytoast plays AUDIO only (no mpv video
# window / GL rendering), so this is fine — the CI smoke test boots the frozen
# app and does `import mpv`, which exercises the dll load path on the arm64
# runner.
#
# Run from the repo root:  pwsh packaging/windows/get_libmpv_arm64.ps1

$ErrorActionPreference = "Stop"

# Same shinchiro release as get_libmpv.ps1, mirrored into
# wolfgangwarehaus/jellytoast-deps ($Url; $UpstreamUrl is the origin and the
# fallback). dev/mirror_libmpv.sh re-pins both scripts together and checks the
# contained libmpv-2.dll is a genuine ARM64 PE (machine 0xAA64).
$Url = "https://github.com/wolfgangwarehaus/jellytoast-deps/releases/download/libmpv-windows-20261018/mpv-dev-aarch64-20261018-git-01e2533f31.7z"
$UpstreamUrl = "https://github.com/shinchiro/mpv-winbuild-cmake/releases/download/20261018/mpv-dev-aarch64-20261018-git-01e2533f31.7z"
$Sha256 = "062b07022d1fadefd341485602658a51d973c216bb4282859e10daa09ad55478"

$Dest = Join-Path $PSScriptRoot "libmpv"
$Archive = Join-Path $env:TEMP "libmpv-dev-aarch64.7z"

# Our copy first; upstream only if the mirror is unreachable. Either way
# the SHA-256 check below decides.
try {
    Invoke-WebRequest -Uri $Url -OutFile $Archive
} catch {
    Write-Warning "libmpv (aarch64) mirror download failed ($_); trying upstream"
    Invoke-WebRequest -Uri $UpstreamUrl -OutFile $Archive
}

$actual = (Get-FileHash -Algorithm SHA256 $Archive).Hash.ToLower()
if ($actual -ne $Sha256.ToLower()) {
    throw "libmpv (aarch64) archive SHA-256 mismatch: expected $Sha256, got $actual"
}

New-Item -ItemType Directory -Force -Path $Dest | Out-Null
# 7z.exe ships on the GitHub windows runners (incl. windows-11-arm).
& 7z e $Archive -o"$Dest" libmpv-2.dll -y
if ($LASTEXITCODE -ne 0) {
    throw "7z extraction failed (exit $LASTEXITCODE)"
}
if (-not (Test-Path (Join-Path $Dest "libmpv-2.dll"))) {
    throw "libmpv-2.dll not found in the aarch64 archive"
}
Write-Host "libmpv-2.dll (aarch64) staged at $Dest"
