"""App localization — Qt translation loading (#232).

Standard Qt/PySide6 layer: user-facing strings are wrapped in ``self.tr(...)``
(QObject subclasses) or ``QCoreApplication.translate("Context", ...)``;
``dev/update_translations.sh`` extracts them into ``jellytoast/i18n/*.ts``
via ``pyside6-lupdate`` and compiles shipped ``.qm`` catalogs via
``pyside6-lrelease``. This module resolves which language to use and installs
the translators on the QApplication — call :func:`install` in ``main()``
BEFORE any UI is constructed (Qt translates at widget-construction time; the
v1 language switch is restart-applied, same precedent as Square corners).

Language resolution: the Settings override (``settings.language``, a BCP-47ish
code like ``"es"``) wins; empty means follow the system locale. English is the
source language — no catalog needed, missing translations simply fall through
to the English source strings, so partially-translated releases stay usable.

Two translators install per non-English language:
  * Qt's own base catalog (``qtbase_<lang>.qm`` from the PySide6 wheel) — the
    stock dialogs/menus Qt renders itself.
  * The app catalog (``jellytoast_<lang>.qm``) shipped as package data in
    ``jellytoast/i18n/``.
"""

from __future__ import annotations

import logging
from typing import List, Tuple

logger = logging.getLogger(__name__)

# Languages with a shipped app catalog (code, English label, native label).
# Keep in sync with jellytoast/i18n/jellytoast_<code>.ts — the Settings
# dropdown builds from this list.
SHIPPED_LANGUAGES: List[Tuple[str, str, str]] = [
    ("es", "Spanish", "Español"),
]

# Keep strong refs — a GC'd QTranslator silently uninstalls itself.
_translators: list = []


def resolved_language() -> str:
    """The language code that will be used: the Settings override, else the
    system UI language, normalized to a bare lowercase code ("es"). Returns
    "" for English/unknown (no catalogs installed)."""
    from jellytoast.settings import get_settings

    override = (get_settings().language or "").strip().lower()
    if override:
        return "" if override == "en" else override
    try:
        from PySide6.QtCore import QLocale

        # First UI language, e.g. "es-MX" → "es".
        ui = QLocale().uiLanguages()
        code = (ui[0] if ui else "").split("-")[0].lower()
    except Exception:
        return ""
    if code in ("", "c", "en"):
        return ""
    return code


def install(app) -> str:
    """Install translators for the resolved language on ``app``. Returns the
    language code actually installed ("" = English/source). Never raises —
    a missing/corrupt catalog degrades to English."""
    lang = resolved_language()
    if not lang:
        return ""
    shipped = {code for code, _, _ in SHIPPED_LANGUAGES}
    if lang not in shipped:
        logger.info("i18n: no catalog for %r — staying on English", lang)
        return ""
    try:
        from PySide6.QtCore import QLibraryInfo, QTranslator

        # Qt's own base strings (stock dialog buttons etc.).
        qt_tr = QTranslator(app)
        qt_dir = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        if qt_tr.load(f"qtbase_{lang}", qt_dir):
            app.installTranslator(qt_tr)
            _translators.append(qt_tr)

        # The app catalog, from package data (works installed, frozen, and
        # from a source checkout alike).
        import importlib.resources as res

        app_tr = QTranslator(app)
        qm = res.files("jellytoast.i18n").joinpath(f"jellytoast_{lang}.qm")
        with res.as_file(qm) as path:
            if app_tr.load(str(path)):
                app.installTranslator(app_tr)
                _translators.append(app_tr)
                logger.info("i18n: %s catalog installed", lang)
                return lang
        logger.warning("i18n: failed to load catalog for %r", lang)
    except Exception:
        logger.exception("i18n: translator install failed — staying on English")
    return ""
