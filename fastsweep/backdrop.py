"""Transparence Windows 11 : fonds Acrylic (fenêtre principale et dialogues) ou Mica via DWM, barre de titre sombre.

Windows 11 transparency: Acrylic (main window and dialogs) or Mica backdrops through DWM, dark title bar.

Ailleurs (Windows 10, Linux, échec de DWM), l'interface reste opaque avec les mêmes couleurs.
Elsewhere (Windows 10, Linux, DWM failure), the interface stays opaque with the same colors.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

from .system import is_windows, system_dll

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_SYSTEMBACKDROP_TYPE = 38  # Windows 11 22H2 (build 22621) et plus / and later
ACRYLIC = 3  # DWMSBT_TRANSIENTWINDOW (2 = Mica, inutilisé / unused)
MIN_BACKDROP_BUILD = 22621


class MARGINS(ctypes.Structure):
    _fields_ = [("cxLeftWidth", ctypes.c_int), ("cxRightWidth", ctypes.c_int),
                ("cyTopHeight", ctypes.c_int), ("cyBottomHeight", ctypes.c_int)]


def backdrop_available() -> bool:
    """Vrai si Windows sait dessiner Mica/Acrylic derrière une fenêtre ordinaire.

    True when Windows can draw Mica/Acrylic behind an ordinary window.
    """
    return is_windows() and sys.getwindowsversion().build >= MIN_BACKDROP_BUILD


def prepare_translucency(widget: QWidget) -> bool:
    """À appeler avant l'affichage : rend le fond de la fenêtre transparent pour laisser voir le fond DWM.

    Call before the window is shown: makes its background transparent so the DWM backdrop shows through.
    """
    if not backdrop_available():
        return False
    widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    return True


def _set_attribute(hwnd: int, attribute: int, value: int) -> bool:
    try:
        dwmapi = system_dll("dwmapi")
        data = ctypes.c_int(value)
        result = dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), wintypes.DWORD(attribute),
                                              ctypes.byref(data), ctypes.sizeof(data))
        return result == 0
    except (AttributeError, OSError):
        return False


def set_dark_title_bar(widget: QWidget, dark: bool) -> None:
    """Barre de titre (et teinte du Mica) en sombre ou en clair ; sans effet hors Windows.

    Dark or light title bar (and Mica tint); no effect outside Windows.
    """
    if is_windows():
        _set_attribute(int(widget.winId()), DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0)


def apply_backdrop(widget: QWidget, kind: int, dark: bool) -> bool:
    """Active Mica ou Acrylic sur une fenêtre préparée ; faux si Windows refuse (garder alors le thème opaque).

    Turns on Mica or Acrylic for a prepared window; False if Windows refuses (then keep the opaque theme).
    """
    if not backdrop_available():
        return False
    hwnd = int(widget.winId())
    set_dark_title_bar(widget, dark)
    try:
        # Étendre le cadre à toute la zone cliente pour que le fond DWM la couvre.
        # Extend the frame over the whole client area so the DWM backdrop covers it.
        margins = MARGINS(-1, -1, -1, -1)
        system_dll("dwmapi").DwmExtendFrameIntoClientArea(wintypes.HWND(hwnd), ctypes.byref(margins))
    except (AttributeError, OSError):
        return False
    return _set_attribute(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, kind)
