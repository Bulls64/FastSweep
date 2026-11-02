"""Fenêtre « À propos » : version, licence, dépôt GitHub, environnement, et copie des informations pour un rapport de bogue.

"About" window: version, license, GitHub repository, environment, and copying the details for a bug report.
"""
from __future__ import annotations

import platform
import sys

import PySide6
from PySide6.QtCore import QTimer, Qt, QUrl, qVersion
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from . import __version__
from .dialogs import FluentDialog
from .glyphs import bolt_pixmap, glyph_icon
from .i18n import APP_NAME, STRINGS
from .system import app_data_dir, is_admin
from .theme import colors
from .updater import REPO

AUTHOR = "Bulls64"
LICENSE_NAME = "MIT"
REPO_URL = f"https://github.com/{REPO}"
ISSUES_URL = f"{REPO_URL}/issues/new"
MARK_SIZE = 64
ROW_HEIGHT = 24


def windows_version() -> str:
    """« Windows 11 (build 22631) » ou la plate-forme décrite par Python ailleurs.

    "Windows 11 (build 22631)" or the platform described by Python elsewhere.
    """
    getter = getattr(sys, "getwindowsversion", None)
    if getter is None:
        return platform.platform()
    build = getter().build
    return f"Windows {11 if build >= 22000 else 10} (build {build})"


def environment() -> dict[str, str]:
    """Versions utiles à un rapport de bogue : Python, PySide6/Qt, Windows, mode d'exécution et droits.

    Versions useful to a bug report: Python, PySide6/Qt, Windows, run mode and rights.
    """
    return {
        "python": platform.python_version(),
        "qt": f"PySide6 {PySide6.__version__} · Qt {qVersion()}",
        "windows": windows_version(),
        "mode": "exe" if getattr(sys, "frozen", False) else "source",
        "admin": "yes" if is_admin() else "no",
        "data": str(app_data_dir()),
    }


def diagnostics_text() -> str:
    """Bloc de texte à coller dans un ticket GitHub (en anglais, quelle que soit la langue de l'interface).

    Text block to paste into a GitHub issue (in English, whatever the interface language).
    """
    env = environment()
    return "\n".join((
        f"{APP_NAME} {__version__} ({env['mode']})",
        f"Windows: {env['windows']}",
        f"Python: {env['python']}",
        f"Qt: {env['qt']}",
        f"Administrator: {env['admin']}",
    ))


class AboutDialog(FluentDialog):
    def __init__(self, parent: QWidget | None, dark: bool, lang: str):
        self.strings = strings = STRINGS[lang]
        super().__init__(parent, strings["about_title"], dark)
        self.setMinimumWidth(720)
        self.body.itemAt(0).widget().hide()  # le titre standard : remplacé par l'en-tête ci-dessous / the standard title: replaced by the header below
        c = colors(dark)

        header = QHBoxLayout()
        header.setSpacing(16)
        mark = QLabel()
        mark.setObjectName("brandMark")
        mark.setFixedSize(MARK_SIZE, MARK_SIZE)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setPixmap(bolt_pixmap(c["onAccent"], 46))
        header.addWidget(mark, 0, Qt.AlignmentFlag.AlignTop)
        names = QVBoxLayout()
        names.setSpacing(0)
        title = QLabel(APP_NAME)
        title.setObjectName("pageTitle")
        self.version_label = QLabel(strings["about_version"].format(version=__version__))
        self.version_label.setObjectName("brandTitle")
        tagline = QLabel(strings["about_tagline"])
        tagline.setObjectName("muted")
        tagline.setWordWrap(True)
        for label in (title, self.version_label, tagline):
            names.addWidget(label)
        header.addLayout(names, 1)
        self.body.addLayout(header)

        description = QLabel(strings["about_description"])
        description.setWordWrap(True)
        self.body.addWidget(description)

        card = QFrame()
        card.setObjectName("card")
        grid = QGridLayout(card)
        grid.setContentsMargins(16, 12, 16, 12)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        env = environment()
        rows = (
            ("about_run", strings["about_mode_" + env["mode"]]),
            ("about_license", f"{LICENSE_NAME} — © 2026 {AUTHOR}"),
            ("about_repo", REPO_URL),
            ("about_windows", env["windows"] + (" · " + strings["about_admin"] if env["admin"] == "yes" else "")),
            ("about_python", f"{env['python']} · {env['qt']}"),
            ("about_data", env["data"]),
        )
        for row, (key, value) in enumerate(rows):
            name = QLabel(strings[key])
            name.setObjectName("muted")
            text = QLabel(value)
            text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)  # copiable à la souris / mouse-copyable
            # Hauteur minimale explicite : sans elle, la hauteur calculée avant l'application de la feuille de style
            # rogne le bas des lettres (j, p, g…). Pas de retour à la ligne : les valeurs sont courtes.
            # Explicit minimum height: without it, the height computed before the style sheet applies clips the
            # bottom of letters (j, p, g…). No word wrap: the values are short.
            name.setMinimumHeight(ROW_HEIGHT)
            text.setMinimumHeight(ROW_HEIGHT)
            grid.addWidget(name, row, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(text, row, 1)
        self.body.addWidget(card)

        credits = QLabel(strings["about_credits"])
        credits.setObjectName("muted")
        credits.setWordWrap(True)
        self.body.addWidget(credits)

        self.repo_button = self.add_footer_button(strings["about_open_repo"], "primaryButton", "export")
        self.issue_button = self.add_footer_button(strings["about_report"])
        self.copy_button = self.add_footer_button(strings["about_copy"])
        close = self.add_footer_button(strings["close"])
        self.repo_button.setIcon(glyph_icon("export", c["onAccent"]))
        self.repo_button.clicked.connect(lambda: self.open_url(REPO_URL))
        self.issue_button.clicked.connect(lambda: self.open_url(ISSUES_URL))
        self.copy_button.clicked.connect(self.copy_details)
        close.clicked.connect(self.accept)

    @staticmethod
    def open_url(url: str) -> None:
        """Ouvre une adresse du dépôt FastSweep dans le navigateur (jamais une adresse venue du réseau).

        Opens an address of the FastSweep repository in the browser (never an address coming from the network).
        """
        QDesktopServices.openUrl(QUrl(url))

    def copy_details(self) -> None:
        QGuiApplication.clipboard().setText(diagnostics_text())
        self.copy_button.setText(self.strings["about_copied"])
        QTimer.singleShot(1800, self.restore_copy_label)

    def restore_copy_label(self) -> None:
        try:
            self.copy_button.setText(self.strings["about_copy"])
        except RuntimeError:
            pass  # fenêtre déjà fermée / window already closed
