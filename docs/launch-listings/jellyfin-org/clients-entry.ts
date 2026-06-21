// Insert into src/data/clients.ts in the third-party clients array, in
// alphabetical order by `id` (jellytoast sorts after "jellyamp", before
// "supersonic"). VERIFY the field names against the LIVE clients.ts at
// submission time — model on a neighbouring native desktop music client
// (e.g. Feishin / Jellyamp); the schema drifts.
//
// Corrections vs the auto-draft: name is lowercase "jellytoast" (branding
// rule), and there is no Flathub link (jellytoast does not ship
// Flatpak/Flathub — see docs/TODO.md).
{
  id: 'jellytoast',
  name: 'jellytoast',
  description:
    'A native PySide6/Qt desktop music player for Jellyfin and Subsonic/OpenSubsonic (Navidrome) servers — bit-perfect gapless playback, casting, offline downloads, synced lyrics, and ListenBrainz scrobbling.',
  clientType: ClientType.ThirdParty,
  deviceTypes: [DeviceType.Desktop],
  licenseType: LicenseType.OpenSource,
  platforms: [Platform.Linux, Platform.Windows],
  primaryLinks: [
    {
      id: 'gh-releases',
      name: 'GitHub Releases',
      url: 'https://github.com/wolfgangwarehaus/jellytoast/releases',
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
