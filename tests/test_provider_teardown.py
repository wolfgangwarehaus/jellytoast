"""Provider teardown (#46): reset_provider() closes the outgoing provider
so a PER-INSTANCE resource (SubsonicProvider's requests.Session) is
released instead of leaked on every kind/server switch. JellyfinProvider's
API is a shared singleton (get_api) and must NOT be closed — it inherits
the base no-op.
"""

import modules.providers as providers_mod
from modules.providers import reset_provider


def test_reset_closes_outgoing_provider(monkeypatch):
    closed = []

    class _P:
        def close(self):
            closed.append(1)

    monkeypatch.setattr(providers_mod, "_PROVIDER", _P())
    reset_provider()
    assert closed == [1]
    assert providers_mod._PROVIDER is None


def test_reset_survives_close_error(monkeypatch):
    class _P:
        def close(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(providers_mod, "_PROVIDER", _P())
    reset_provider()  # best-effort — must not raise
    assert providers_mod._PROVIDER is None


def test_reset_with_no_provider_is_safe(monkeypatch):
    monkeypatch.setattr(providers_mod, "_PROVIDER", None)
    reset_provider()
    assert providers_mod._PROVIDER is None


def test_subsonic_close_closes_session():
    from modules.providers.subsonic import SubsonicProvider

    p = SubsonicProvider.__new__(SubsonicProvider)
    closed = []

    class _S:
        def close(self):
            closed.append(1)

    p.session = _S()
    p.close()
    assert closed == [1]


def test_jellyfin_inherits_noop_close():
    # The api is a shared singleton (get_api); close() must not touch it.
    from modules.providers.jellyfin import JellyfinProvider

    p = JellyfinProvider.__new__(JellyfinProvider)
    p.close()  # inherited base no-op — must not raise / not close the api
