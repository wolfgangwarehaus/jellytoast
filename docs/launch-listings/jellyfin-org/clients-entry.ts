// Insert into src/data/clients.ts on the `master` branch of jellyfin/jellyfin.org,
// in the third-party clients array, alphabetical by `id` (after "jellyamp", before
// "supersonic"). Schema verified 2026-06-22 against clients.ts + platform.ts on master:
//   - ClientType / DeviceType / LicenseType are enums defined inline in clients.ts.
//   - Platform is imported from src/data/platform.ts (Platform.Linux renders "Generic Linux").
//   - link objects are { id, name, url }.
//   - store/download links go in primaryLinks; the source repo + site go in secondaryLinks.
//   - OMIT `recommended` — it's reserved for first-party / void-filling clients.
// Inclusion bar (met): first-rate Jellyfin support (Jellyfin + Subsonic at parity), no piracy,
// follows Jellyfin branding, not tied to one hosted instance, clear GPL licensing.
{
  id: 'jellytoast',
  name: 'jellytoast',
  description:
    'A native PySide6/Qt desktop music player (not Electron) for Jellyfin and Subsonic/OpenSubsonic (Navidrome) servers — bit-perfect gapless mpv playback, offline downloads, multi-protocol casting (Chromecast/AirPlay 2/Sonos/DLNA/Snapcast), a floating mini player, synced lyrics, and ListenBrainz scrobbling.',
  clientType: ClientType.ThirdParty,
  deviceTypes: [DeviceType.Desktop],
  licenseType: LicenseType.OpenSource,
  platforms: [Platform.Linux, Platform.Windows],
  primaryLinks: [
    {
      id: 'microsoft-store',
      name: 'Microsoft Store',
      url: 'https://apps.microsoft.com/detail/9PNLTPXGHN79',
    },
    {
      id: 'gh-releases',
      name: 'GitHub Releases',
      url: 'https://github.com/wolfgangwarehaus/jellytoast/releases/latest',
    },
  ],
  secondaryLinks: [
    {
      id: 'github',
      name: 'GitHub',
      url: 'https://github.com/wolfgangwarehaus/jellytoast',
    },
    {
      id: 'website',
      name: 'Website',
      url: 'https://wolfgangwarehaus.com/jellytoast',
    },
  ],
},
