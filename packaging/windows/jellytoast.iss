; Inno Setup script — wraps the PyInstaller onedir bundles into ONE
; dual-architecture Windows installer (native x64 + native Arm64) published
; on GitHub Releases. The winget manifest in packaging/winget/ points at this
; exe.
;
; The installer CARRIES BOTH arch trees and installs the one matching the
; running machine:
;   dist-x64\jellytoast\    → installed on x64 Windows
;   dist-arm64\jellytoast\  → installed on Arm64 Windows 11
; CI builds each tree on its own runner (x64 on windows-latest, arm64 on
; windows-11-arm), collects both under dist-<arch>\, then runs iscc ONCE
; (see .github/workflows/release.yml build-windows).
;
; Requires Inno Setup 6.3+ (arm64 / x64compatible identifiers + the
; IsX64Compatible/IsArm64 support functions landed in 6.3.0, 2024-06-09).
; The GitHub windows runner ships 6.x >= 6.3.
;
; Build (CI):
;   iscc /DAppVersion=0.2.0 packaging\windows\jellytoast.iss
;
; Per-user install (no admin prompt): jellytoast is a user app, writes
; only user config, and per-user is what winget expects from new
; submissions by default.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{9720458D-B2BB-45E0-80B8-085E227A8D79}
AppName=jellytoast
; lowercase everywhere — the brand is "jellytoast", never "JellyToast"
AppVersion={#AppVersion}
AppPublisher=wolfgangwarehaus
AppPublisherURL=https://github.com/wolfgangwarehaus/jellytoast
AppSupportURL=https://github.com/wolfgangwarehaus/jellytoast/issues
AppUpdatesURL=https://github.com/wolfgangwarehaus/jellytoast/releases
DefaultDirName={autopf}\jellytoast
DefaultGroupName=jellytoast
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
; One installer for both arches — no -x64 in the name anymore.
OutputBaseFilename=jellytoast-{#AppVersion}-windows-setup
OutputDir=..\..\dist
SetupIconFile=jellytoast.ico
UninstallDisplayIcon={app}\jellytoast.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Allow BOTH x64 and native Arm64 Windows 11. x64compatible also matches
; Arm64 (x64 emulation) — the per-file Check: functions below pick the
; NATIVE tree per machine so an Arm64 box gets the arm64 dll, not the
; emulated x64 one.
ArchitecturesAllowed=x64compatible or arm64
; Install into the 64-bit Program Files / registry view on both x64 and
; Arm64 (native 64-bit app on each).
ArchitecturesInstallIn64BitMode=x64compatible or arm64
LicenseFile=..\..\LICENSE

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; A PyInstaller onedir upgrade must never OVERLAY the previous tree: files the
; new version dropped would otherwise survive as stale orphans (0.2.0 QA found
; 0.1.x's whole cryptography/ package lingering under _internal after the
; DPAPI switch removed it — dead weight at best, importable version-skew at
; worst). Purge the bundle dir before laying the fresh tree down. User
; data/config live under %APPDATA%, not {app}, so only shipped files go.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
; Two PyInstaller onedir outputs, each with its arch's libmpv-2.dll bundled.
; Exactly one is installed, chosen by the Check: functions in [Code]. solidbreak
; puts each tree in its own solid block so the skipped arch isn't decompressed.
; x64 tree — x64 Windows only (NOT Arm64; Arm64 gets the native tree below).
Source: "..\..\dist-x64\jellytoast\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs solidbreak; Check: IsX64Install
; Native Arm64 tree — Arm64 Windows 11 only.
Source: "..\..\dist-arm64\jellytoast\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs solidbreak; Check: IsArm64

[Icons]
Name: "{group}\jellytoast"; Filename: "{app}\jellytoast.exe"
Name: "{group}\{cm:UninstallProgram,jellytoast}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\jellytoast"; Filename: "{app}\jellytoast.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\jellytoast.exe"; Description: "{cm:LaunchProgram,jellytoast}"; Flags: nowait postinstall skipifsilent

[Code]
function IsX64Install: Boolean;
begin
  { Install the x64 tree only on x64 Windows. IsX64Compatible is also True on
    Arm64 (it can run x64 under emulation), so exclude Arm64 explicitly — there
    the native arm64 tree wins. }
  Result := IsX64Compatible and not IsArm64;
end;
