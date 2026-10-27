"""i18n (#232) — language resolution + translator install.

Covers the boot-time layer only: which language ``jellytoast.i18n``
resolves (Settings override → system locale → English fallback) and that
installing the Spanish catalog actually makes ``translate()`` return
Spanish for a string the login surface uses. Widget-level rendering is
covered implicitly — Qt resolves ``self.tr`` through the same installed
translators at construction time.
"""

from jellytoast import i18n


def _uninstall_all(app):
    # Leave no translator behind for later tests — a leaked catalog would
    # flip every subsequently-built widget to Spanish.
    for tr in i18n._translators:
        app.removeTranslator(tr)
    i18n._translators.clear()


class TestResolvedLanguage:
    def test_override_wins(self, isolated_settings):
        isolated_settings.language = "es"
        assert i18n.resolved_language() == "es"

    def test_explicit_english_means_source(self, isolated_settings):
        # "en" is the source language — no catalog, so it resolves to ""
        # (nothing to install) even though it was an explicit pick.
        isolated_settings.language = "en"
        assert i18n.resolved_language() == ""

    def test_override_normalized(self, isolated_settings):
        isolated_settings.language = "  ES "
        assert i18n.resolved_language() == "es"

    def test_empty_falls_to_system(self, isolated_settings, monkeypatch):
        isolated_settings.language = ""
        # Pin the system locale so the assert doesn't depend on the host.
        from PySide6.QtCore import QLocale

        monkeypatch.setattr(QLocale, "uiLanguages", lambda self: ["de-DE", "en-US"])
        assert i18n.resolved_language() == "de"


class TestInstall:
    def test_spanish_catalog_translates(self, qapp, isolated_settings):
        isolated_settings.language = "es"
        try:
            assert i18n.install(qapp) == "es"
            from PySide6.QtCore import QCoreApplication

            assert QCoreApplication.translate("LoginView", "Sign in") == "Iniciar sesión"
            # .format-style placeholders survive translation.
            assert (
                QCoreApplication.translate("LoginView", "Alternate URLs · {0}").format(3)
                == "URLs alternativas · 3"
            )
        finally:
            _uninstall_all(qapp)

    def test_unshipped_language_stays_english(self, qapp, isolated_settings):
        # A locale we have no catalog for must be a clean no-op, not a
        # crash or a half-installed translator.
        isolated_settings.language = "fi"
        try:
            assert i18n.install(qapp) == ""
            assert not i18n._translators
        finally:
            _uninstall_all(qapp)

    def test_english_installs_nothing(self, qapp, isolated_settings):
        isolated_settings.language = "en"
        try:
            assert i18n.install(qapp) == ""
            assert not i18n._translators
        finally:
            _uninstall_all(qapp)

    def test_shipped_list_matches_catalogs(self):
        # Every advertised language must actually ship a compiled catalog —
        # the Settings dropdown builds from SHIPPED_LANGUAGES, so a missing
        # .qm would offer users a language that silently does nothing.
        import importlib.resources as res

        for code, _en, _native in i18n.SHIPPED_LANGUAGES:
            qm = res.files("jellytoast.i18n").joinpath(f"jellytoast_{code}.qm")
            assert qm.is_file(), f"no compiled catalog for {code!r}"


def test_top_bar_page_titles_are_translated(qapp, isolated_settings):
    """QA 2026-10-27: the centre title read "Albums" / "Suggestions" in a
    Spanish UI. It shows the translation; the host and the view menu
    still speak in the English page keys."""
    from PySide6.QtWidgets import QWidget

    from jellytoast.top_bar import JtTopBar

    isolated_settings.language = "es"
    try:
        assert i18n.install(qapp) == "es"
        host = QWidget()
        top = JtTopBar(host, titlebar_mode=True)
        top.set_collection("music")
        top.set_active_tab("Smart playlists")
        assert top.view_btn.text() == "Listas inteligentes"
        assert top._view_title == "Smart playlists"  # the key is unchanged
        top.set_now_playing_mode(True)
        assert top.view_btn.text() == "En reproducción"
        host.deleteLater()
    finally:
        _uninstall_all(qapp)
