# Download the pinned libmpv build and stage libmpv-2.dll where the
# PyInstaller spec expects it (packaging/windows/libmpv/libmpv-2.dll).
#
# Source: shinchiro's mpv-winbuild-cmake releases — the de-facto
# upstream for Windows libmpv (what mpv.io links to). Pinned by SHA-256 so a
# release build is reproducible.
#
# $Url is OUR copy in wolfgangwarehaus/jellytoast-deps: shinchiro keeps only
# the last ~30 daily releases, so an upstream pin 404s about a month later
# (the 0.2.3 cut hit that). $UpstreamUrl is where the copy came from (and the
# fallback). Re-pin both scripts with one command:
#     dev/mirror_libmpv.sh <shinchiro tag>
#
# Run from the repo root:  pwsh packaging/windows/get_libmpv.ps1

$ErrorActionPreference = "Stop"

# Plain x86_64 (not -v3) so older CPUs stay supported.
$Url = "https://github.com/wolfgangwarehaus/jellytoast-deps/releases/download/libmpv-windows-20261018/mpv-dev-x86_64-20261018-git-01e2533f31.7z"
$UpstreamUrl = "https://github.com/shinchiro/mpv-winbuild-cmake/releases/download/20261018/mpv-dev-x86_64-20261018-git-01e2533f31.7z"
$Sha256 = "d7f5621f42b6e161fe032df914a10bdece28a89df3e57c6c3e16a5a250929b5e"

$Dest = Join-Path $PSScriptRoot "libmpv"
$Archive = Join-Path $env:TEMP "libmpv-dev.7z"

# Our copy first; upstream only if the mirror is unreachable. Either way
# the SHA-256 check below decides.
try {
    Invoke-WebRequest -Uri $Url -OutFile $Archive
} catch {
    Write-Warning "libmpv mirror download failed ($_); trying upstream"
    Invoke-WebRequest -Uri $UpstreamUrl -OutFile $Archive
}

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
