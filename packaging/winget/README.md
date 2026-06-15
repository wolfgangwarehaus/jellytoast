# winget submission

These manifests go to [microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs)
under `manifests/w/wolfgangwarehaus/jellytoast/<version>/` once the GitHub
release with the Inno installer is **published** (winget validation fetches
the URL, so a draft release 404s).

Before submitting, fill `InstallerSha256` in the installer manifest:

    sha256sum dist/jellytoast-<version>-windows-x64-setup.exe

Easiest path — let wingetcreate do the whole thing (it fills hashes and
opens the PR):

    wingetcreate new https://github.com/wolfgangwarehaus/jellytoast/releases/download/v0.1.0/jellytoast-0.1.0-windows-x64-setup.exe

After the first version is merged, updates are one-liners:

    wingetcreate update wolfgangwarehaus.jellytoast -u <new-installer-url> -v <new-version> --submit

Users then install with:

    winget install wolfgangwarehaus.jellytoast
