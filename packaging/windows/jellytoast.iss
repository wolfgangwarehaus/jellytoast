; Inno Setup script — wraps the PyInstaller onedir bundle
; (dist/jellytoast, built from packaging/pyinstaller/jellytoast.spec)
; into the Windows installer published on GitHub Releases. The winget
; manifest in packaging/winget/ points at this exe.
;
; Build (CI: .github/workflows/release.yml; iscc ships on the
; windows-latest runner):
;   iscc /DAppVersion=0.1.0 packaging\windows\jellytoast.iss
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
OutputBaseFilename=jellytoast-{#AppVersion}-windows-x64-setup
OutputDir=..\..\dist
SetupIconFile=jellytoast.ico
UninstallDisplayIcon={app}\jellytoast.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\..\LICENSE

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The whole PyInstaller onedir output, libmpv-2.dll included.
Source: "..\..\dist\jellytoast\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\jellytoast"; Filename: "{app}\jellytoast.exe"
Name: "{group}\{cm:UninstallProgram,jellytoast}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\jellytoast"; Filename: "{app}\jellytoast.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\jellytoast.exe"; Description: "{cm:LaunchProgram,jellytoast}"; Flags: nowait postinstall skipifsilent
