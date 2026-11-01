"""Icônes vectorielles au trait (style Fluent), dessinées en SVG puis converties en QIcon ou en PNG.

Stroke vector icons (Fluent style), drawn as SVG then turned into a QIcon or a PNG.
"""
from __future__ import annotations

import tempfile
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

# Tracés sur une grille de 20 × 20, comme dans la maquette.
# Paths on a 20 × 20 grid, as in the mockup.
ICONS = {
    "menu": '<path d="M3 5h14M3 10h14M3 15h14"/>',
    "search": '<circle cx="9" cy="9" r="5"/><path d="M13 13l4 4"/>',
    "chev-down": '<path d="M5 8l5 5 5-5"/>',
    "chev-up": '<path d="M5 12l5-5 5 5"/>',
    "refresh": '<path d="M16 10a6 6 0 1 1-2-4.5M16 3v4h-4"/>',
    "moon": '<path d="M16 12.5A7 7 0 0 1 7.5 4 7 7 0 1 0 16 12.5z"/>',
    "sun": ('<circle cx="10" cy="10" r="3.5"/><path d="M10 2v2M10 16v2M2 10h2M16 10h2M4.3 4.3l1.4 1.4'
            'M14.3 14.3l1.4 1.4M4.3 15.7l1.4-1.4M14.3 5.7l1.4-1.4"/>'),
    "shield": '<path d="M10 2l6 2v5c0 4-3 7-6 9-3-2-6-5-6-9V4z"/>',
    "history": '<circle cx="10" cy="10" r="7"/><path d="M10 6v4l3 2"/>',
    "info": '<circle cx="10" cy="10" r="7"/><path d="M10 9v5M10 6.5v.01"/>',
    "undo": '<path d="M7 4L3 8l4 4M3 8h8a5 5 0 0 1 0 10H8"/>',
    "apps": ('<rect x="3" y="3" width="6" height="6" rx="1"/><rect x="11" y="3" width="6" height="6" rx="1"/>'
             '<rect x="3" y="11" width="6" height="6" rx="1"/><rect x="11" y="11" width="6" height="6" rx="1"/>'),
    "store": '<path d="M4 7h12l-1 10H5zM7 7a3 3 0 0 1 6 0"/>',
    "trash": '<path d="M4 6h12M8 6V4h4v2M6 6l1 11h6l1-11"/>',
    "warn": '<path d="M10 3l8 14H2zM10 8v4M10 14.5v.01"/>',
    "check": '<path d="M4.5 10.5l3.5 3.5 7.5-8"/>',
    "export": '<path d="M10 3v9M6 7l4-4 4 4M4 13v3h12v-3"/>',
    "timeline": ('<path d="M6 3v14"/><circle cx="6" cy="5" r="1.6"/><circle cx="6" cy="10" r="1.6"/>'
                 '<circle cx="6" cy="15" r="1.6"/><path d="M10 5h7M10 10h7M10 15h5"/>'),
    "lock": '<rect x="4.5" y="9" width="11" height="8" rx="1.5"/><path d="M7 9V6.5a3 3 0 0 1 6 0V9"/>',
    "plus": '<path d="M10 4v12M4 10h12"/>',
}

# Éclair très simple (une ligne brisée pleine) sur la grille 20 × 20 ; sert au logo du volet et à l'icône de l'application.
# Very simple lightning bolt (a filled zigzag) on the 20 × 20 grid; used for the sidebar logo and the application icon.
BOLT_POINTS = ((11.5, 2.0), (5.0, 11.0), (9.5, 11.0), (8.5, 18.0), (15.0, 9.0), (10.5, 9.0))


def bolt_pixmap(color: str, size: int) -> QPixmap:
    """Éclair plein de la couleur donnée, net aussi sur écran à haute densité.

    Filled bolt of the given color, sharp on high-density screens too.
    """
    points = " ".join(f"{x},{y}" for x, y in BOLT_POINTS)
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><polygon points="{points}" fill="{color}"/></svg>'
    pixmap = QPixmap.fromImage(_render_svg(svg.encode(), size * 2))
    pixmap.setDevicePixelRatio(2)
    return pixmap


def _svg(name: str, color: str, stroke: float) -> bytes:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="none" stroke="{color}" '
            f'stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round">{ICONS[name]}</svg>').encode()


def _render_svg(svg: bytes, pixels: int) -> QImage:
    image = QImage(pixels, pixels, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(QByteArray(svg)).render(painter, QRectF(0, 0, pixels, pixels))
    painter.end()
    return image


def _render(name: str, color: str, pixels: int, stroke: float) -> QImage:
    return _render_svg(_svg(name, color, stroke), pixels)


@lru_cache(maxsize=256)
def glyph_icon(name: str, color: str, checked_color: str = "", size: int = 16, stroke: float = 1.5) -> QIcon:
    """Icône nette en 1× et 2× ; ``checked_color`` colore l'état coché (bouton de navigation sélectionné).

    Crisp icon at 1× and 2×; ``checked_color`` colors the checked state (selected navigation button).
    """
    icon = QIcon()
    for scale in (1, 2):
        icon.addPixmap(QPixmap.fromImage(_render(name, color, size * scale, stroke)), QIcon.Mode.Normal, QIcon.State.Off)
        if checked_color:
            icon.addPixmap(QPixmap.fromImage(_render(name, checked_color, size * scale, stroke)),
                           QIcon.Mode.Normal, QIcon.State.On)
    return icon


@lru_cache(maxsize=64)
def glyph_png(name: str, color: str, size: int = 12, stroke: float = 2.0) -> str:
    """Chemin d'un PNG (et de sa variante @2x) pour les règles ``image: url(...)`` des feuilles de style.

    Path of a PNG (and its @2x variant) for the ``image: url(...)`` rules of style sheets.
    """
    folder = Path(tempfile.gettempdir()) / "FastSweep" / "glyphs"
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{name}-{color.lstrip('#').lower()}-{size}"
    _render(name, color, size, stroke).save(str(folder / f"{stem}.png"))
    _render(name, color, size * 2, stroke).save(str(folder / f"{stem}@2x.png"))
    return (folder / f"{stem}.png").as_posix()  # Qt veut des « / » / Qt wants "/" separators
