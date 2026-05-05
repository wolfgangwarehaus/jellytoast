"""Provider abstraction layer.

Every backend (Jellyfin, Subsonic / Navidrome, future) implements
``MediaProvider`` from ``modules.providers.base``. The host calls
``get_provider()`` to obtain the active provider singleton. Provider
selection is driven by the ``provider_kind`` setting, defaulting to
``jellyfin`` for backward compatibility with existing installs.

Phase 1 (this commit): scaffolding + the auth tier. The full data
model (browse / search / stream) still flows through ``modules.jellyfin_api``
for now — the abstraction at this layer is the auth boundary, which is
where provider-switching matters first.
"""

from modules.providers.base import (
    MediaProvider, ServerInfo, AuthResult,
)
from modules.providers.jellyfin import JellyfinProvider
from modules.providers.subsonic import SubsonicProvider


_PROVIDER: "MediaProvider | None" = None


def get_provider() -> MediaProvider:
    """Return the active provider singleton. Reads
    ``Settings.provider_kind`` to decide which one to instantiate on
    first call. Subsequent calls return the same instance. Provider
    type changes (e.g. switching servers from Jellyfin to Navidrome)
    require a ``reset_provider()`` call so the next ``get_provider()``
    re-reads the setting."""
    global _PROVIDER
    if _PROVIDER is not None:
        return _PROVIDER
    from modules.settings import get_settings
    kind = (get_settings().provider_kind or "jellyfin").lower()
    if kind == "subsonic":
        _PROVIDER = SubsonicProvider()
    else:
        _PROVIDER = JellyfinProvider()
    return _PROVIDER


def reset_provider():
    """Drop the cached provider singleton so the next ``get_provider()``
    call re-reads ``provider_kind`` and rebuilds. Used by the
    server-change path so a future kind toggle takes effect."""
    global _PROVIDER
    _PROVIDER = None


__all__ = [
    "MediaProvider", "ServerInfo", "AuthResult",
    "JellyfinProvider", "SubsonicProvider",
    "get_provider", "reset_provider",
]
