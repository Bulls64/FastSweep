"""Page « Réglages » : apparence (thème, langue, opacité des fonds), mises à jour et « À propos ».

"Settings" page: appearance (theme, language, background opacity), updates and "About".
"""
from __future__ import annotations

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .about import AboutSection
from .theme import DEFAULT_OPACITY
from .update_ui import SETTING_AUTO_CHECK, SETTING_AUTO_INSTALL, SETTING_WHATS_NEW

SETTING_OPACITY = "background_opacity"
OPACITY_DELAY_MS = 120  # regroupe les mouvements du curseur avant de redessiner / groups slider moves before repainting
CONTROL_WIDTH = 220


def percent(value: int) -> str:
    return f"{value}\u00a0%"  # espace insécable : jamais « 50 » et « % » sur deux lignes / non-breaking space


class SettingsPage(QWidget):
    """Réglages de FastSweep ; ``host`` (la fenêtre principale) applique les changements et les mémorise.

    FastSweep settings; ``host`` (the main window) applies the changes and remembers them.
    """

    def __init__(self, host):
        super().__init__()
        self.host = host
        self.opacity_timer = QTimer(self)
        self.opacity_timer.setSingleShot(True)
        self.opacity_timer.setInterval(OPACITY_DELAY_MS)
        self.opacity_timer.timeout.connect(self.apply_opacity)
        self.build_ui()

    def t(self, key: str) -> str:
        return self.host.t(key)

    def build_ui(self) -> None:
        # La page défile : avec « À propos », elle dépasse la hauteur d'une petite fenêtre.
        # The page scrolls: with "About", it is taller than a small window.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("settingsScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.viewport().setAutoFillBackground(False)
        content = QWidget()
        content.setObjectName("scrollContent")
        self.scroll.setWidget(content)
        outer.addWidget(self.scroll)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 12, 12)  # place pour la barre de défilement / room for the scroll bar
        layout.setSpacing(8)

        self.appearance_label = self.section(layout)
        appearance = self.card(layout)
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["", ""])  # textes posés par update_copy() / texts set by update_copy()
        self.theme_combo.currentIndexChanged.connect(lambda index: self.host.set_dark(index == 1))
        self.theme_row = self.row(appearance, self.theme_combo)
        self.language_combo = QComboBox()
        self.language_combo.addItem("Français", "fr")
        self.language_combo.addItem("English", "en")
        self.language_combo.currentIndexChanged.connect(lambda index: self.host.set_language(self.language_combo.itemData(index)))
        self.language_row = self.row(appearance, self.language_combo)

        opacity = QWidget()
        opacity_layout = QHBoxLayout(opacity)
        opacity_layout.setContentsMargins(0, 0, 0, 0)
        opacity_layout.setSpacing(12)
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setSingleStep(5)
        self.opacity_slider.setPageStep(10)
        self.opacity_slider.setFixedWidth(CONTROL_WIDTH)
        self.opacity_slider.valueChanged.connect(self.opacity_changed)
        self.opacity_value = QLabel()
        self.opacity_value.setFixedWidth(44)
        self.opacity_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.opacity_reset = QPushButton()
        self.opacity_reset.setObjectName("subtleButton")
        self.opacity_reset.clicked.connect(lambda: self.opacity_slider.setValue(DEFAULT_OPACITY))
        opacity_layout.addWidget(self.opacity_reset)
        opacity_layout.addWidget(self.opacity_slider)
        opacity_layout.addWidget(self.opacity_value)
        self.opacity_row = self.row(appearance, opacity)

        self.updates_label = self.section(layout)
        updates = self.card(layout)
        self.auto_check = QCheckBox()
        self.auto_install = QCheckBox()
        self.auto_check.toggled.connect(self.save_update_options)
        self.auto_install.toggled.connect(self.save_update_options)
        self.check_now = QPushButton()
        self.check_now.clicked.connect(self.host.show_updates)  # recherche, nouveautés, téléchargement / lookup, notes, download
        self.updates_row = self.row(updates, self.check_now)
        self.add_checks(updates, self.auto_check, self.auto_install)
        self.whats_new_btn = QPushButton()
        self.whats_new_btn.clicked.connect(lambda: self.host.show_whats_new())
        self.whats_new_row = self.row(updates, self.whats_new_btn)
        self.whats_new_auto = QCheckBox()
        self.whats_new_auto.toggled.connect(lambda checked: self.host.settings.setValue(SETTING_WHATS_NEW, checked))
        self.add_checks(updates, self.whats_new_auto)

        self.about_label = self.section(layout)
        about = self.card(layout)
        self.about = AboutSection(self.host)
        about.addWidget(self.about)
        layout.addStretch(1)
        self.update_copy()

    # --- Construction / Building -------------------------------------------------------------------

    @staticmethod
    def add_checks(card: QVBoxLayout, *checks: QCheckBox) -> None:
        for check in checks:
            line = QHBoxLayout()
            line.setContentsMargins(16, 4, 16, 4)
            line.addWidget(check)
            card.addLayout(line)
        card.addSpacing(8)

    @staticmethod
    def section(layout: QVBoxLayout) -> QLabel:
        label = QLabel()
        label.setObjectName("sectionLabel")
        label.setContentsMargins(4, 12, 0, 4)
        layout.addWidget(label)
        return label

    @staticmethod
    def card(layout: QVBoxLayout) -> QVBoxLayout:
        frame = QFrame()
        frame.setObjectName("card")
        inner = QVBoxLayout(frame)
        inner.setContentsMargins(0, 4, 0, 4)
        inner.setSpacing(0)
        layout.addWidget(frame)
        return inner

    @staticmethod
    def row(card: QVBoxLayout, control: QWidget) -> tuple[QLabel, QLabel]:
        """Ligne de réglage WinUI : titre et description à gauche, contrôle à droite.

        WinUI settings row: title and description on the left, control on the right.
        """
        line = QHBoxLayout()
        line.setContentsMargins(16, 12, 16, 12)
        line.setSpacing(24)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        title, description = QLabel(), QLabel()
        description.setObjectName("muted")
        description.setWordWrap(True)
        texts.addWidget(title)
        texts.addWidget(description)
        line.addLayout(texts, 1)
        if isinstance(control, QComboBox):
            control.setFixedWidth(CONTROL_WIDTH)
        line.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
        card.addLayout(line)
        return title, description

    # --- Valeurs / Values --------------------------------------------------------------------------

    def load(self) -> None:
        """Remet les contrôles sur les valeurs en vigueur, sans rien déclencher.

        Puts the controls back on the current values, without triggering anything.
        """
        settings = self.host.settings
        for widget, value in ((self.theme_combo, 1 if self.host.dark else 0),
                              (self.language_combo, max(self.language_combo.findData(self.host.lang), 0)),
                              (self.opacity_slider, self.host.background_opacity),
                              (self.auto_check, settings.value(SETTING_AUTO_CHECK, True, type=bool)),
                              (self.auto_install, settings.value(SETTING_AUTO_INSTALL, False, type=bool)),
                              (self.whats_new_auto, settings.value(SETTING_WHATS_NEW, True, type=bool))):
            blocked = widget.blockSignals(True)
            if isinstance(widget, QComboBox):
                widget.setCurrentIndex(value)
            elif isinstance(widget, QSlider):
                widget.setValue(value)
            else:
                widget.setChecked(value)
            widget.blockSignals(blocked)
        self.opacity_value.setText(percent(self.opacity_slider.value()))
        self.auto_install.setEnabled(self.auto_check.isChecked())
        self.refresh_opacity_state()

    def refresh_opacity_state(self) -> None:
        """Le curseur n'agit que sur les fonds translucides : sans Acrylic, il est grisé et dit pourquoi.

        The slider only affects translucent fills: without Acrylic it is greyed out and says why.
        """
        available = self.host.translucent
        for widget in (self.opacity_slider, self.opacity_value, self.opacity_reset):
            widget.setEnabled(available)
        self.opacity_reset.setEnabled(available and self.opacity_slider.value() != DEFAULT_OPACITY)
        self.opacity_row[1].setText(self.t("settings_opacity_desc" if available else "settings_opacity_unavailable"))

    def update_copy(self) -> None:
        """Textes dans la langue courante, et contrôles recalés sur les valeurs en vigueur (thème changé ailleurs…).

        Texts in the current language, and controls put back on the current values (theme changed elsewhere…).
        """
        self.load()
        self.appearance_label.setText(self.t("settings_appearance"))
        self.updates_label.setText(self.t("settings_updates"))
        self.about_label.setText(self.t("about"))
        for (title, description), title_key, description_key in (
                (self.theme_row, "settings_theme", "settings_theme_desc"),
                (self.language_row, "settings_language", "settings_language_desc"),
                (self.whats_new_row, "settings_whats_new_title", "settings_whats_new_desc")):
            title.setText(self.t(title_key))
            description.setText(self.t(description_key))
        self.opacity_row[0].setText(self.t("settings_opacity"))
        blocked = self.theme_combo.blockSignals(True)
        self.theme_combo.setItemText(0, self.t("settings_theme_light"))
        self.theme_combo.setItemText(1, self.t("settings_theme_dark"))
        self.theme_combo.blockSignals(blocked)
        self.opacity_reset.setText(self.t("settings_opacity_reset"))
        self.opacity_slider.setAccessibleName(self.t("settings_opacity"))
        self.theme_combo.setAccessibleName(self.t("settings_theme"))
        self.language_combo.setAccessibleName(self.t("settings_language"))
        self.auto_check.setText(self.t("update_auto_check"))
        self.auto_install.setText(self.t("update_auto_install"))
        self.auto_install.setToolTip(self.t("update_auto_install_tip"))
        self.whats_new_btn.setText(self.t("settings_whats_new_btn"))
        self.whats_new_auto.setText(self.t("whats_new_auto"))
        self.refresh_opacity_state()
        self.refresh_update_status()
        self.about.update_copy()

    def refresh_update_status(self) -> None:
        """Version installée et version disponible ; le bouton devient l'action principale quand il y a du nouveau.

        Installed and available version; the button becomes the main action when there is something new.
        """
        info = self.host.update_info
        title, description = self.updates_row
        title.setText(self.t("settings_updates_title"))
        status = self.t("update_available").format(version=info.version) if info else self.t("settings_updates_desc")
        description.setText(f"{self.t('update_current').format(version=__version__)} · {status}")
        self.check_now.setText(self.t("settings_update_show" if info else "settings_check_now"))
        self.check_now.setObjectName("primaryButton" if info else "")
        self.check_now.style().unpolish(self.check_now)
        self.check_now.style().polish(self.check_now)

    def set_theme(self, dark: bool) -> None:
        self.about.set_theme(dark)

    # --- Actions -----------------------------------------------------------------------------------

    def opacity_changed(self, value: int) -> None:
        self.opacity_value.setText(percent(value))
        self.opacity_reset.setEnabled(self.host.translucent and value != DEFAULT_OPACITY)
        self.opacity_timer.start()  # redessin après une courte pause / repaint after a short pause

    def apply_opacity(self) -> None:
        self.host.set_background_opacity(self.opacity_slider.value())

    def save_update_options(self) -> None:
        self.host.settings.setValue(SETTING_AUTO_CHECK, self.auto_check.isChecked())
        self.host.settings.setValue(SETTING_AUTO_INSTALL, self.auto_install.isChecked())
        self.auto_install.setEnabled(self.auto_check.isChecked())
