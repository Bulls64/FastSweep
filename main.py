"""FastSweep — point d'entrée (``py main.py``).

FastSweep — entry point (``py main.py``).
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastsweep import __version__
from fastsweep.i18n import APP_NAME, STRINGS
from fastsweep.system import is_windows, migrate_legacy_data


def resource_path(relative: str) -> Path:
    """Chemin d'un fichier embarqué, aussi bien depuis les sources que depuis l'exécutable PyInstaller.

    Path of a bundled file, both from the sources and from the PyInstaller executable.
    """
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def main() -> int:
    if not is_windows():
        print(STRINGS["fr"]["unavailable"])
        return 1
    # Importés ici : ces modules ont besoin de winreg, qui n'existe que sous Windows.
    # Imported here: these modules need winreg, which only exists on Windows.
    from PySide6.QtGui import QFont, QIcon
    from PySide6.QtWidgets import QApplication

    from fastsweep.window import MainWindow

    migrate_legacy_data()  # historique et sauvegardes de l'ancien nom / history and backups of the former name
    app = QApplication(sys.argv)
    # Fusion : base neutre et prévisible pour les feuilles de style WinUI de FastSweep.
    # Fusion: a neutral, predictable base for FastSweep's WinUI style sheets.
    app.setStyle("Fusion")
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(APP_NAME)
    app.setFont(QFont("Segoe UI Variable", 10))
    icon = resource_path("assets/fastsweep.ico")
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
