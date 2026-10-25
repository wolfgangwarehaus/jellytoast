# jellytoast on Linux phones

On a phone shell (Plasma Mobile or Phosh) jellytoast opens its touch interface:
swipe gestures, a mini player, Now Playing with lyrics and the queue, casting,
downloads for offline listening. It needs **jellytoast 0.2.4 or newer**; 0.2.3
and older only have the desktop window.

Tested (2026-10-25) on aarch64, the OnePlus 6T's architecture, under emulation:
signing in, loading the library and its art, and decoding audio through the
distribution's own libmpv. Not yet tried on a real phone.

## Nura (formerly postmarketOS) and Alpine

Nura v26.06 is built on Alpine 3.24, which packages everything jellytoast
needs: PySide6 6.11, libmpv, python-mpv and pychromecast. jellytoast isn't in
the Alpine repositories yet, so build the package once on the phone:

```bash
doas apk add alpine-sdk git
abuild-keygen -a -i -n            # a signing key for packages you build
git clone https://github.com/wolfgangwarehaus/jellytoast.git
cd jellytoast/packaging/alpine
abuild checksum                    # fetches the release, records its checksum
abuild -r                          # builds and checks it
doas apk add --repository ~/packages/packaging jellytoast
```

To update after a new release, in `jellytoast/packaging/alpine`:

```bash
git pull && abuild checksum && abuild -r
doas apk upgrade --repository ~/packages/packaging jellytoast
```

- **DLNA, Sonos and AirPlay speakers** aren't offered: their Python libraries
  aren't packaged in Alpine. Chromecast and Google speaker groups work.
- **Nura Duranium** (the immutable edition) keeps the system read-only and
  installs apps as Flatpaks; `apk` is for the regular edition. jellytoast has no
  ARM Flatpak yet, so use the regular edition for now.

## Arch Linux ARM

The AUR package is architecture-independent and every dependency is in Arch
Linux ARM's aarch64 repositories, so it installs the same way as on a PC:

```bash
yay -S jellytoast                  # or: git clone the AUR repo, makepkg -si
```

Optional extras from the AUR: `python-async-upnp-client` (DLNA) and
`python-soco` (Sonos).

## Tips

- Plasma Mobile and Phosh both open the touch interface automatically. On a
  plain desktop session you can force it with `JT_UI=quick jellytoast`, or
  choose the classic window with `JT_UI=widgets`.
- Closing jellytoast while music plays keeps it playing in the background;
  tap the app or the media controls to bring it back.
- In docked mode (a screen and keyboard attached), jellytoast switches to the
  desktop layout the next time it starts.
