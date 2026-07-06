# Licensing

jellytoast's source code is licensed **GPL-2.0-or-later** — see
[`LICENSE`](LICENSE) (GPLv2) and [`COPYING`](COPYING) (GPLv3, the version
binary distributions are conveyed under, since bundled Qt/PySide6 is
LGPL-3.0). Third-party notices for bundled components:
[`packaging/THIRD-PARTY-NOTICES.md`](packaging/THIRD-PARTY-NOTICES.md).

## App Store distribution exception

Application stores (Apple's App Store, the Microsoft Store, and similar)
wrap downloads in their own terms — device limits, storefront EULAs, DRM —
that the GPL forbids an ordinary distributor from adding. To keep
store distribution unambiguously permitted, jellytoast grants the
following additional permission (adopted 2026-07-06):

> **App Store distribution exception.** As an additional permission under
> section 7 of the GNU General Public License version 3, you are permitted to
> convey the Program (and works based on it) through an application store or
> distribution channel, even if that store imposes terms or conditions that are
> incompatible with the GNU General Public License, provided that the complete
> corresponding source code of the conveyed work remains available to all
> recipients under the GNU General Public License (version 2 or later), with or
> without this additional permission, through a channel that does not impose
> those incompatible terms.

That condition is satisfied by this repository: the complete source is
public here under GPL-2.0-or-later. The exception rides on GPLv3 via the
project's "or later" clause, so the license identifier stays
`GPL-2.0-or-later`, unchanged.

Store builds bundle only LGPL-licensed native media libraries (libmpv
built with `-Dgpl=false`; FFmpeg without `--enable-gpl`) so that no
third-party GPL code is conveyed under store terms this exception cannot
cover.

## Contributions

By contributing first-party code to jellytoast you agree to license it
under GPL-2.0-or-later **including the App Store distribution exception
above**, so store distribution remains possible after your contribution.
