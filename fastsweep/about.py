"""Section « À propos » des Réglages : version, licence, dépôt GitHub, environnement, et copie des informations
pour un rapport de bogue.

"About" section of the Settings: version, license, GitHub repository, environment, and copying the details
for a bug report.
"""
from __future__ import annotations

import platform
import sys

import PySide6
from PySide6.QtCore import QTimer, Qt, QUrl, qVersion
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from . import __version__
from .glyphs import bolt_pixmap, glyph_icon
from .i18n import APP_NAME
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


class AboutSection(QWidget):
    """Section « À propos » de la page Réglages : identité, détails de l'environnement et liens vers le dépôt.

    "About" section of the Settings page: identity, environment details and links to the repository.
    ``host`` fournit t() et dark. / ``host`` provides t() and dark.
    """

    def __init__(self, host):
        super().__init__()
        self.host = host
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(16)
        self.mark = QLabel()
        self.mark.setObjectName("brandMark")
        self.mark.setFixedSize(MARK_SIZE, MARK_SIZE)
        self.mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self.mark, 0, Qt.AlignmentFlag.AlignTop)
        names = QVBoxLayout()
        names.setSpacing(0)
        title = QLabel(APP_NAME)
        title.setObjectName("brandTitle")
        self.version_label = QLabel()
        self.tagline = QLabel()
        self.tagline.setObjectName("muted")
        self.tagline.setWordWrap(True)
        for label in (title, self.version_label, self.tagline):
            names.addWidget(label)
        header.addLayout(names, 1)
        layout.addLayout(header)

        self.description = QLabel()
        self.description.setWordWrap(True)
        layout.addWidget(self.description)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(1, 1)
        self.rows: list[tuple[QLabel, QLabel, str]] = []
        for row, key in enumerate(("about_run", "about_license", "about_repo", "about_windows", "about_python", "about_data")):
            name = QLabel()
            name.setObjectName("muted")
            value = QLabel()
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)  # copiable à la souris / mouse-copyable
            # Hauteur minimale explicite : sans elle, le bas des lettres (j, p, g…) est rogné. / Explicit height: avoids clipped descenders.
            name.setMinimumHeight(ROW_HEIGHT)
            value.setMinimumHeight(ROW_HEIGHT)
            grid.addWidget(name, row, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(value, row, 1)
            self.rows.append((name, value, key))
        layout.addLayout(grid)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.repo_button, self.issue_button, self.copy_button = QPushButton(), QPushButton(), QPushButton()
        self.repo_button.clicked.connect(lambda: self.open_url(REPO_URL))
        self.issue_button.clicked.connect(lambda: self.open_url(ISSUES_URL))
        self.copy_button.clicked.connect(self.copy_details)
        for button in (self.repo_button, self.issue_button, self.copy_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.credits = QLabel()
        self.credits.setObjectName("muted")
        self.credits.setWordWrap(True)
        layout.addWidget(self.credits)
        self.update_copy()
        self.set_theme(host.dark)

    def t(self, key: str) -> str:
        return self.host.t(key)

    def update_copy(self) -> None:
        env = environment()
        values = {
            "about_run": self.t("about_mode_" + env["mode"]),
            "about_license": f"{LICENSE_NAME} — © 2026 {AUTHOR}",
            "about_repo": REPO_URL,
            "about_windows": env["windows"] + (" · " + self.t("about_admin") if env["admin"] == "yes" else ""),
            "about_python": f"{env['python']} · {env['qt']}",
            "about_data": env["data"],
        }
        for name, value, key in self.rows:
            name.setText(self.t(key))
            value.setText(values[key])
        self.version_label.setText(self.t("about_version").format(version=__version__))
        self.tagline.setText(self.t("about_tagline"))
        self.description.setText(self.t("about_description"))
        self.credits.setText(self.t("about_credits"))
        self.repo_button.setText(self.t("about_open_repo"))
        self.issue_button.setText(self.t("about_report"))
        self.copy_button.setText(self.t("about_copy"))

    def set_theme(self, dark: bool) -> None:
        c = colors(dark)
        self.mark.setPixmap(bolt_pixmap(c["onAccent"], 46))
        self.repo_button.setIcon(glyph_icon("export", c["text"]))

    @staticmethod
    def open_url(url: str) -> None:
        """Ouvre une adresse du dépôt FastSweep dans le navigateur (jamais une adresse venue du réseau).

        Opens an address of the FastSweep repository in the browser (never an address coming from the network).
        """
        QDesktopServices.openUrl(QUrl(url))

    def copy_details(self) -> None:
        QGuiApplication.clipboard().setText(diagnostics_text())
        self.copy_button.setText(self.t("about_copied"))
        QTimer.singleShot(1800, self.restore_copy_label)

    def restore_copy_label(self) -> None:
        try:
            self.copy_button.setText(self.t("about_copy"))
        except RuntimeError:
            pass  # page déjà détruite / page already destroyed
