"""Thème WinUI 3 (Fluent) clair et sombre : couleurs et feuilles de style Qt.

WinUI 3 (Fluent) light and dark theme: colors and Qt style sheets.

En mode translucide, les fonds sont semi-transparents au-dessus du Mica / de l'Acrylic dessiné par Windows ;
en mode opaque (Windows 10, Linux), les mêmes teintes sont pleines.
In translucent mode, fills are semi-transparent over the Mica / Acrylic drawn by Windows;
in opaque mode (Windows 10, Linux), the same tints are solid.
"""
from __future__ import annotations

import re
from functools import lru_cache

from PySide6.QtGui import QColor

from .glyphs import glyph_png

FONT_FAMILY = "'Segoe UI Variable Text', 'Segoe UI', sans-serif"

_BASE = {
    False: dict(  # clair / light
        mica="#F3F3F3", layer="#F9F9F9", layerStroke="#E5E5E5", card="#FFFFFF", cardStroke="#E5E5E5",
        text="#1A1A1A", text2="#5D5D5D", textDisabled="#A0A0A0",
        ctl="#FFFFFF", ctlHover="#F9F9F9", ctlPressed="#F3F3F3", ctlStroke="#E5E5E5", ctlBottom="#C6C6C6",
        subtleHover="rgba(0,0,0,0.037)", subtlePressed="rgba(0,0,0,0.024)",
        sel="rgba(0,0,0,0.055)", navSel="rgba(0,0,0,0.055)",
        accent="#0067C0", accentHover="#1975C5", accentPressed="#3183CA", onAccent="#FFFFFF",
        accentDisabled="#C5C5C5", danger="#C42B1C", dangerHover="#B3261E", onDanger="#FFFFFF",
        divider="#EAEAEA", cbBorder="#767676", track="#DCDCDC", scrollHandle="rgba(0,0,0,0.35)",
        infoBg="#FFF4CE", infoStroke="#EBDDA8", warnIcon="#9D5D00", footer="#F3F3F3", popup="#FFFFFF",
    ),
    True: dict(  # sombre / dark
        mica="#202020", layer="#272727", layerStroke="#333333", card="#2B2B2B", cardStroke="#3A3A3A",
        text="#FFFFFF", text2="#C8C8C8", textDisabled="#787878",
        ctl="#2D2D2D", ctlHover="#323232", ctlPressed="#272727", ctlStroke="#3B3B3B", ctlBottom="#3B3B3B",
        subtleHover="rgba(255,255,255,0.06)", subtlePressed="rgba(255,255,255,0.04)",
        sel="rgba(255,255,255,0.075)", navSel="rgba(255,255,255,0.06)",
        accent="#60CDFF", accentHover="#5BBDEA", accentPressed="#56AAD3", onAccent="#000000",
        accentDisabled="#434343", danger="#C42B1C", dangerHover="#B3261E", onDanger="#FFFFFF",
        divider="#3A3A3A", cbBorder="#A6A6A6", track="#4D4D4D", scrollHandle="rgba(255,255,255,0.4)",
        infoBg="#433519", infoStroke="#5E4B1E", warnIcon="#FCE100", footer="#202020", popup="#2C2C2C",
    ),
}

# Surcharges translucides, posées sur l'Acrylic (ou le Mica) dessiné par Windows.
# Translucent overrides, laid over the Acrylic (or Mica) drawn by Windows.
# Valeurs plus légères que celles de WinUI pour laisser voir davantage l'arrière-plan (réglage du degré de transparence).
# Lighter than WinUI's values so more of the background shows through (this is where transparency is tuned).
_TRANSLUCENT = {
    False: dict(layer="rgba(255,255,255,0.22)", card="rgba(255,255,255,0.42)", ctl="rgba(255,255,255,0.5)",
                ctlHover="rgba(255,255,255,0.62)", ctlPressed="rgba(255,255,255,0.3)", footer="rgba(243,243,243,0.32)"),
    True: dict(layer="rgba(58,58,58,0.14)", card="rgba(255,255,255,0.035)", ctl="rgba(255,255,255,0.05)",
               ctlHover="rgba(255,255,255,0.08)", ctlPressed="rgba(255,255,255,0.03)", footer="rgba(32,32,32,0.3)"),
}


def colors(dark: bool, translucent: bool = False) -> dict[str, str]:
    """Jeton de couleur → valeur CSS (hex ou rgba).

    Color token → CSS value (hex or rgba).
    """
    values = dict(_BASE[dark])
    if translucent:
        values.update(_TRANSLUCENT[dark])
    return values


def qcolor(value: str) -> QColor:
    """QColor depuis « #RRGGBB » ou « rgba(r,g,b,a) » (alpha entre 0 et 1).

    QColor from "#RRGGBB" or "rgba(r,g,b,a)" (alpha between 0 and 1).
    """
    match = re.fullmatch(r"rgba\((\d+),(\d+),(\d+),([\d.]+)\)", value.replace(" ", ""))
    if match:
        red, green, blue, alpha = match.groups()
        return QColor(int(red), int(green), int(blue), round(float(alpha) * 255))
    return QColor(value)


@lru_cache(maxsize=4)
def theme_stylesheet(dark: bool, translucent: bool = False) -> str:
    c = colors(dark, translucent)
    window_bg = "transparent" if translucent else c["mica"]
    check = glyph_png("check", c["onAccent"], 12, 2.2)
    chevron = glyph_png("chev-down", c["text2"], 12, 1.8)
    chevron_up = glyph_png("chev-up", c["text2"], 12, 1.8)
    return f"""
        * {{ font-family: {FONT_FAMILY}; font-size: 14px; }}
        QWidget {{ color: {c["text"]}; }}
        QMainWindow#mainWindow {{ background: {window_bg}; }}
        QMessageBox {{ background: {c["mica"]}; }}
        QDialog {{ background: {c["layer"] if not translucent else c["mica"]}; }}
        QDialog[backdrop="true"] {{ background: transparent; }}
        QToolTip {{ background: {c["popup"]}; color: {c["text"]}; border: 1px solid {c["cardStroke"]}; padding: 4px 8px; }}

        #panel {{ background: {c["layer"]}; border-top: 1px solid {c["layerStroke"]};
                  border-left: 1px solid {c["layerStroke"]}; border-top-left-radius: 8px; }}
        #brandMark {{ background: {c["accent"]}; border-radius: 8px; }}
        #brandTitle {{ font-weight: 600; }}
        #pageTitle {{ font-size: 28px; font-weight: 600; }}
        #muted {{ color: {c["text2"]}; }}
        #caption {{ color: {c["text2"]}; font-size: 12px; }}
        #navDivider {{ background: {c["divider"]}; border: none; margin: 0 4px; }}
        #sectionLabel {{ color: {c["text2"]}; font-size: 12px; font-weight: 600; padding: 0 12px 6px 12px; }}
        #emptyState {{ color: {c["text2"]}; font-size: 15px; padding: 40px; }}
        QLabel#dialogTitle {{ font-size: 20px; font-weight: 600; }}

        QPushButton {{ background: {c["ctl"]}; color: {c["text"]}; border: 1px solid {c["ctlStroke"]};
                       border-bottom-color: {c["ctlBottom"]}; border-radius: 4px; padding: 5px 12px; min-height: 20px; }}
        QPushButton:hover {{ background: {c["ctlHover"]}; }}
        QPushButton:pressed {{ background: {c["ctlPressed"]}; color: {c["text2"]}; }}
        QPushButton:disabled {{ color: {c["textDisabled"]}; }}
        QPushButton#primaryButton {{ background: {c["accent"]}; color: {c["onAccent"]}; border: 1px solid {c["accent"]}; }}
        QPushButton#primaryButton:hover {{ background: {c["accentHover"]}; border-color: {c["accentHover"]}; }}
        QPushButton#primaryButton:pressed {{ background: {c["accentPressed"]}; border-color: {c["accentPressed"]}; }}
        QPushButton#primaryButton:disabled {{ background: {c["accentDisabled"]}; border-color: {c["accentDisabled"]};
                                              color: {c["textDisabled"] if dark else "#FFFFFF"}; }}
        QPushButton#dangerButton {{ background: {c["danger"]}; color: {c["onDanger"]}; border: 1px solid {c["danger"]}; }}
        QPushButton#dangerButton:hover {{ background: {c["dangerHover"]}; border-color: {c["dangerHover"]}; }}
        QPushButton#dangerButton:disabled {{ background: {c["accentDisabled"]}; border-color: {c["accentDisabled"]};
                                             color: {c["textDisabled"] if dark else "#FFFFFF"}; }}
        QPushButton#subtleButton, QPushButton#navButton {{ background: transparent; border: none; }}
        QPushButton#subtleButton:hover, QPushButton#navButton:hover {{ background: {c["subtleHover"]}; }}
        QPushButton#subtleButton:pressed, QPushButton#navButton:pressed {{ background: {c["subtlePressed"]}; color: {c["text"]}; }}
        QPushButton#navButton {{ text-align: left; padding: 10px 12px; }}
        QPushButton#navButton[highlight="true"] {{ color: {c["accent"]}; font-weight: 600; }}
        QPushButton#navButton:checked {{ background: {c["navSel"]}; font-weight: 600; }}

        QLineEdit {{ background: {c["ctl"]}; border: 1px solid {c["ctlStroke"]}; border-bottom: 1px solid {c["ctlBottom"]};
                     border-radius: 4px; padding: 5px 8px 5px 12px; selection-background-color: {c["accent"]};
                     selection-color: {c["onAccent"]}; }}
        QLineEdit:hover {{ background: {c["ctlHover"]}; }}
        QLineEdit:focus {{ border-bottom: 2px solid {c["accent"]}; padding-bottom: 4px; }}
        QComboBox {{ background: {c["ctl"]}; border: 1px solid {c["ctlStroke"]}; border-bottom-color: {c["ctlBottom"]};
                     border-radius: 4px; padding: 5px 12px; min-height: 20px; }}
        QComboBox:hover {{ background: {c["ctlHover"]}; }}
        QComboBox::drop-down {{ border: none; width: 32px; }}
        QComboBox::down-arrow {{ image: url("{chevron}"); width: 12px; height: 12px; }}
        QComboBox QAbstractItemView {{ background: {c["popup"]}; border: 1px solid {c["cardStroke"]}; padding: 4px;
                                       outline: 0; selection-background-color: {c["sel"]}; selection-color: {c["text"]}; }}

        QCheckBox {{ spacing: 8px; }}
        QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 4px; border: 1px solid {c["cbBorder"]};
                                background: {c["ctl"]}; }}
        QCheckBox::indicator:hover {{ background: {c["ctlHover"]}; }}
        QCheckBox::indicator:checked {{ background: {c["accent"]}; border: 1px solid {c["accent"]}; image: url("{check}"); }}
        QRadioButton {{ spacing: 8px; }}
        QRadioButton::indicator {{ width: 18px; height: 18px; border-radius: 10px; border: 1px solid {c["cbBorder"]};
                                   background: {c["ctl"]}; }}
        QRadioButton::indicator:hover {{ background: {c["ctlHover"]}; }}
        QRadioButton::indicator:checked {{ width: 10px; height: 10px; border: 5px solid {c["accent"]};
                                           background: {c["onAccent"]}; }}
        QSpinBox {{ background: {c["ctl"]}; border: 1px solid {c["ctlStroke"]}; border-bottom: 1px solid {c["ctlBottom"]};
                    border-radius: 4px; padding: 4px 4px 4px 10px; min-width: 56px; }}
        QSpinBox:hover {{ background: {c["ctlHover"]}; }}
        QSpinBox:focus {{ border-bottom: 2px solid {c["accent"]}; }}
        QSpinBox::up-button, QSpinBox::down-button {{ border: none; background: transparent; width: 20px; }}
        QSpinBox::up-arrow {{ image: url("{chevron_up}"); width: 10px; height: 10px; }}
        QSpinBox::down-arrow {{ image: url("{chevron}"); width: 10px; height: 10px; }}

        QTextBrowser {{ background: {c["card"]}; border: 1px solid {c["cardStroke"]}; border-radius: 4px; padding: 8px; }}
        #card {{ background: {c["card"]}; border: 1px solid {c["cardStroke"]}; border-radius: 8px; }}
        QTableWidget {{ background: transparent; border: none; outline: 0; gridline-color: transparent;
                        selection-background-color: transparent; selection-color: {c["text"]}; }}
        QTableWidget::item {{ border: none; padding: 0 12px; }}
        QTableWidget::indicator {{ width: 18px; height: 18px; border-radius: 4px; border: 1px solid {c["cbBorder"]};
                                   background: {c["ctl"]}; }}
        QTableWidget::indicator:hover {{ background: {c["ctlHover"]}; }}
        QTableWidget::indicator:checked {{ background: {c["accent"]}; border: 1px solid {c["accent"]}; image: url("{check}"); }}
        QHeaderView, QTableCornerButton::section {{ background: transparent; border: none; }}
        QHeaderView::section {{ background: transparent; color: {c["text2"]}; border: none; border-bottom: 1px solid {c["divider"]};
                                padding: 0 12px; min-height: 40px; font-size: 12px; font-weight: 600; }}
        QListWidget {{ background: {c["card"]}; border: 1px solid {c["cardStroke"]}; border-radius: 4px; outline: 0; padding: 2px;
                       selection-background-color: transparent; selection-color: {c["text"]}; }}
        QListWidget::item {{ border: none; padding: 0 12px; }}
        QScrollArea {{ background: {c["card"]}; border: 1px solid {c["cardStroke"]}; border-radius: 4px; }}
        #scrollContent {{ background: transparent; }}

        QProgressBar {{ background: {c["track"]}; border: none; border-radius: 2px; max-height: 4px; min-height: 4px; }}
        QProgressBar::chunk {{ background: {c["accent"]}; border-radius: 2px; }}
        #infoBar {{ background: {c["infoBg"]}; border: 1px solid {c["infoStroke"]}; border-radius: 4px; }}
        #infoBar[severity="info"] {{ background: {c["card"]}; border-color: {c["cardStroke"]}; }}
        #dialogFooter {{ background: {c["footer"]}; border-top: 1px solid {c["divider"]}; }}

        QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px; }}
        QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 2px; }}
        QScrollBar::handle {{ background: {c["scrollHandle"]}; border-radius: 3px; }}
        QScrollBar::handle:vertical {{ min-height: 32px; margin: 0 3px; }}
        QScrollBar::handle:horizontal {{ min-width: 32px; margin: 3px 0; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
        QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
    """
