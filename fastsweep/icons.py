"""Icônes des applications (programmes classiques et logos du Store), mises en cache.

Application icons (desktop programs and Store logos), cached.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QFileInfo
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QFileIconProvider, QStyle

from .inventory import display_icon_path, store_logo_path
from .models import AppEntry

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".ico"})


def application_icon(app: AppEntry) -> QIcon:
    path = store_logo_path(app.install_location) if app.source == "store" else display_icon_path(app.icon_path)
    return cached_application_icon(app.source, path, app.install_location)


@lru_cache(maxsize=2048)
def cached_application_icon(source: str, icon_path: str, install_location: str) -> QIcon:
    path = icon_path or (install_location if source == "store" else "")
    if path and Path(path).exists():
        # Charger le visuel du package comme une image. QFileIconProvider renvoie l'icône du type de
        # fichier pour les PNG sous Windows, ce qui masque le vrai logo du Store.
        # Load package artwork as an image. QFileIconProvider returns the file-type
        # icon for PNGs on Windows, which hides the actual Store logo.
        if source == "store" and Path(path).suffix.casefold() in IMAGE_SUFFIXES:
            icon = QIcon(path)
            if not icon.isNull():
                return icon
        icon = QFileIconProvider().icon(QFileInfo(path))
        if not icon.isNull():
            return icon
    return QApplication.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)
