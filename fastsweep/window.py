"""Fenêtre principale.

Main window.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QEvent, QSettings, QSize, Qt
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .backdrop import ACRYLIC, apply_backdrop, prepare_translucency, set_dark_title_bar
from .cleanup import (
    backup_registry_keys,
    delete_registry_tree,
    file_candidates_for,
    recycle_path,
    registry_backup_root,
    registry_candidates_for,
    registry_key_exists,
    restore_registry_backup,
)
from .dialogs import FileResidueDialog, HistoryDialog, ResidueDialog, UninstallQueueDialog
from .glyphs import bolt_pixmap, glyph_icon
from .history import record_operation
from .i18n import APP_NAME, STRINGS
from .icons import application_icon
from .inventory import display_icon_path, format_date, format_size, read_classic_apps, read_store_apps
from .models import AppEntry, FileCandidate, RegistryCandidate
from .restore_page import RestorePointsPage
from .system import is_admin, is_windows, relaunch_as_admin
from .theme import colors, theme_stylesheet
from .uninstall import (
    REBOOT_EXIT_CODES,
    RESTORE_POINT_ERROR_PREFIX,
    describe_uninstall_error,
    perform_uninstall,
    silent_uninstall_command,
    uninstall_status,
)
from .widgets import CheckTable, NavButton, SearchBox, SortableItem
from .workers import SizeWorker, TaskWorker


# Colonnes du tableau ; la première porte les cases à cocher.
# Table columns; the first one holds the checkboxes.
COL_CHECK, COL_NAME, COL_PUBLISHER, COL_VERSION, COL_DATE, COL_SIZE = range(6)

# Fenêtre agrandie ou plein écran : Windows la déborde de l'épaisseur de son cadre et le haut du contenu
# passe sous l'en-tête. Ce retrait (en pixels logiques, donc mis à l'échelle avec le DPI) le redescend.
# Maximized or full-screen window: Windows pushes it out by its frame thickness and the top of the content
# slips under the title bar. This inset (in logical pixels, so scaled with the DPI) brings it back down.
MAXIMIZED_TOP_INSET = 8


SETTINGS_ORG, SETTINGS_APP = "FastSweep", "FastSweep"
LEGACY_SETTINGS = ("Clair", "ClairUninstaller")  # ancien nom / former name


def open_settings() -> QSettings:
    """Réglages de FastSweep (registre HKCU sous Windows) ; les tests la remplacent par un fichier jetable.

    FastSweep's settings (HKCU registry on Windows); the tests replace it with a throwaway file.
    """
    return QSettings(SETTINGS_ORG, SETTINGS_APP)


def migrate_legacy_settings(settings: QSettings) -> None:
    """Reprend la langue et le thème enregistrés sous l'ancien nom, tant que rien n'est enregistré sous le nouveau.

    Brings over the language and theme saved under the former name, as long as nothing is saved under the new one.
    """
    if settings.contains("language") or settings.contains("dark"):
        return
    legacy = QSettings(settings.format(), settings.scope(), *LEGACY_SETTINGS)  # même stockage / same storage
    for key in ("language", "dark"):
        if legacy.contains(key):
            settings.setValue(key, legacy.value(key))


# Options de désinstallation mémorisées : clé de réglage → valeur par défaut.
# Remembered uninstall options: settings key → default value.
OPTION_DEFAULTS = {"restore_point": False, "silent": False, "auto_scan": False}


@dataclass
class LeftoverScan:
    """Une analyse de résidus en cours : les applications visées, le mode automatique et ce qui a été trouvé.

    A leftover scan in progress: the target apps, automatic mode and what was found.
    """

    apps: list[AppEntry]
    auto: bool = False
    found: bool = False  # des clés de registre ont été proposées / registry keys were offered
    label: str = field(init=False)

    def __post_init__(self) -> None:
        self.label = ", ".join(app.name for app in self.apps)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = open_settings()
        migrate_legacy_settings(self.settings)
        self.lang = self.settings.value("language", "fr")
        if self.lang not in STRINGS:
            self.lang = "fr"
        self.dark = self.settings.value("dark", True, type=bool)
        self.mode = "classic"  # liste d'applications affichée / application list shown
        self.restore_active = False  # page des points de restauration ouverte / restore points page open
        self.sidebar_collapsed = False
        self.selected: AppEntry | None = None
        self.classic_apps: list[AppEntry] = []
        self.store_apps: list[AppEntry] = []
        self._workers: list[TaskWorker] = []
        self._size_workers: list[SizeWorker] = []
        self._size_items: dict[int, QTableWidgetItem] = {}
        self._busy = False
        self.setObjectName("mainWindow")
        # À décider avant la création de la fenêtre native (fond transparent pour l'Acrylic).
        # Decided before the native window exists (transparent background for Acrylic).
        self._translucent = prepare_translucency(self)
        self._backdrop_checked = False
        self.setMinimumSize(950, 650)
        self.resize(1160, 760)
        self.build_ui()
        self.apply_theme()
        self.load_apps()

    def t(self, key: str) -> str:
        return STRINGS[self.lang][key]

    def build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        root_layout = self.root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)  # retrait du haut : update_top_inset() / top inset: update_top_inset()
        root_layout.setSpacing(0)

        # Volet de navigation, posé directement sur le fond Acrylic.
        # Navigation pane, laid directly on the Acrylic backdrop.
        self.sidebar = QFrame()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(248)
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(8, 16, 8, 12)
        side.setSpacing(2)
        brand = QHBoxLayout()
        brand.setContentsMargins(12, 4, 12, 0)
        brand.setSpacing(12)
        self.brand_mark = mark = QLabel()  # éclair posé par refresh_icons() / bolt set by refresh_icons()
        mark.setObjectName("brandMark")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(36, 36)
        brand.addWidget(mark)
        name_box = QVBoxLayout()
        name_box.setSpacing(0)
        self.brand_title = QLabel()
        self.brand_title.setObjectName("brandTitle")
        self.brand_subtitle = QLabel()
        self.brand_subtitle.setObjectName("caption")
        self.brand_subtitle.setWordWrap(True)  # selon la police disponible / depending on the available font
        name_box.addWidget(self.brand_title)
        name_box.addWidget(self.brand_subtitle)
        brand.addLayout(name_box)
        brand.addStretch(1)
        side.addLayout(brand)
        side.addSpacing(24)
        self.nav_label = QLabel()
        self.nav_label.setObjectName("sectionLabel")
        side.addWidget(self.nav_label)
        self.classic_nav = NavButton()
        self.store_nav = NavButton()
        self.classic_nav.setChecked(True)
        self.classic_nav.clicked.connect(lambda: self.switch_mode("classic"))
        self.store_nav.clicked.connect(lambda: self.switch_mode("store"))
        side.addWidget(self.classic_nav)
        side.addWidget(self.store_nav)
        side.addSpacing(16)
        self.system_label = QLabel()
        self.system_label.setObjectName("sectionLabel")
        side.addWidget(self.system_label)
        self.restore_nav = NavButton()
        self.restore_nav.clicked.connect(lambda: self.switch_mode("restore"))
        side.addWidget(self.restore_nav)
        side.addStretch(1)
        self.admin_label = QLabel()
        self.admin_label.setObjectName("caption")
        self.admin_label.setWordWrap(True)
        self.admin_label.setContentsMargins(12, 0, 12, 8)
        side.addWidget(self.admin_label)
        divider = QFrame()
        divider.setObjectName("navDivider")
        divider.setFixedHeight(1)
        side.addWidget(divider)
        side.addSpacing(6)
        self.restore_backup_btn = NavButton()
        self.history_btn = NavButton()
        self.about_btn = NavButton()
        for button, slot in ((self.restore_backup_btn, self.restore_backup), (self.history_btn, self.show_history),
                             (self.about_btn, self.show_about)):
            button.setCheckable(False)  # actions, pas des pages / actions, not pages
            button.clicked.connect(slot)
            side.addWidget(button)
        root_layout.addWidget(self.sidebar)

        # Panneau de contenu : calque arrondi en haut à gauche, comme une NavigationView WinUI.
        # Content panel: layer rounded at the top left, like a WinUI NavigationView.
        main = QWidget()
        main.setObjectName("panel")
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(32, 24, 32, 20)
        main_layout.setSpacing(16)
        top = QHBoxLayout()
        top.setSpacing(12)
        self.sidebar_toggle = QPushButton()
        self.sidebar_toggle.setObjectName("subtleButton")
        self.sidebar_toggle.setFixedSize(36, 36)
        self.sidebar_toggle.setIconSize(QSize(18, 18))
        self.sidebar_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sidebar_toggle.clicked.connect(self.toggle_sidebar)
        top.addWidget(self.sidebar_toggle, 0, Qt.AlignmentFlag.AlignTop)
        title_col = QVBoxLayout()
        title_col.setSpacing(0)
        self.page_title = QLabel()
        self.page_title.setObjectName("pageTitle")
        self.page_hint = QLabel()
        self.page_hint.setObjectName("muted")
        title_col.addWidget(self.page_title)
        title_col.addWidget(self.page_hint)
        top.addLayout(title_col)
        top.addStretch(1)
        self.theme_btn = QPushButton()
        self.theme_btn.setFixedSize(32, 32)  # bouton carré, symbole seul / square button, symbol only
        self.theme_btn.setIconSize(QSize(16, 16))
        self.theme_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.theme_btn.clicked.connect(self.toggle_theme)
        self.language_btn = QPushButton()
        self.language_btn.clicked.connect(self.toggle_language)
        self.elevate_btn = QPushButton()
        self.elevate_btn.clicked.connect(self.elevate_application)
        for button in (self.theme_btn, self.language_btn, self.elevate_btn):
            top.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)
        main_layout.addLayout(top)

        # Deux pages sous la barre du haut : la liste d'applications et les points de restauration.
        # Two pages under the top bar: the application list and the restore points.
        self.pages = QStackedWidget()
        self.apps_page = QWidget()
        apps_layout = QVBoxLayout(self.apps_page)
        apps_layout.setContentsMargins(0, 0, 0, 0)
        apps_layout.setSpacing(16)
        self.pages.addWidget(self.apps_page)
        self.restore_page = RestorePointsPage(self)
        self.pages.addWidget(self.restore_page)
        main_layout.addWidget(self.pages, 1)

        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self.search = SearchBox()
        self.search.search_changed.connect(self.populate_table)
        search_row.addWidget(self.search, 1)
        self.publisher_filter = QComboBox()
        self.publisher_filter.addItem("", "")  # « Tous les éditeurs », texte posé par update_copy() / "All publishers", text set by update_copy()
        self.publisher_filter.setMinimumWidth(190)
        self.publisher_filter.currentIndexChanged.connect(self.filter_table)
        search_row.addWidget(self.publisher_filter)
        self.refresh_btn = QPushButton()
        self.refresh_btn.clicked.connect(self.load_apps)
        search_row.addWidget(self.refresh_btn)
        apps_layout.addLayout(search_row)

        self.card = QFrame()
        self.card.setObjectName("card")
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(4, 0, 4, 4)
        card_layout.setSpacing(0)
        self.table = CheckTable(6, self.dark, key=lambda app: app.key, primary_column=COL_NAME)
        self.table.setObjectName("appTable")
        self.table.setIconSize(QSize(24, 24))
        header = self.table.horizontalHeader()
        for column in (COL_NAME, COL_PUBLISHER):  # le nom et l'éditeur se partagent la largeur libre / name and publisher take the free width
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        header.setSortIndicator(COL_NAME, Qt.SortOrder.AscendingOrder)  # A → Z au démarrage / A → Z at startup
        self.table.setSortingEnabled(True)
        self.table.itemSelectionChanged.connect(self.on_selection_changed)
        self.table.checks_changed.connect(self.update_check_ui)
        self.table.cellDoubleClicked.connect(self.show_details)
        card_layout.addWidget(self.table, 1)
        self.empty_label = QLabel()
        self.empty_label.setObjectName("emptyState")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.hide()
        card_layout.addWidget(self.empty_label, 1)
        apps_layout.addWidget(self.card, 1)

        footer = QHBoxLayout()
        self.count_label = QLabel()
        self.count_label.setObjectName("muted")
        self.count_label.setWordWrap(True)  # passe à la ligne plutôt que d'élargir la fenêtre / wraps rather than widening the window
        footer.addWidget(self.count_label, 1)  # occupe la place libre, les options restent à droite / takes the free space, options stay right
        footer.setSpacing(20)
        self.restore_check, self.silent_check, self.auto_scan_check = QCheckBox(), QCheckBox(), QCheckBox()
        for check, key in ((self.restore_check, "restore_point"), (self.silent_check, "silent"), (self.auto_scan_check, "auto_scan")):
            check.setChecked(self.settings.value(key, OPTION_DEFAULTS[key], type=bool))
            check.toggled.connect(lambda checked, key=key: self.settings.setValue(key, checked))  # mémorisé / remembered
            footer.addWidget(check)
        apps_layout.addLayout(footer)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.status_label = QLabel(self.t("status_ready"))
        self.status_label.setObjectName("muted")
        action_row.addWidget(self.status_label)
        action_row.addStretch(1)
        self.scan_btn = QPushButton()
        self.scan_btn.clicked.connect(lambda: self.scan_leftovers())
        self.uninstall_btn = QPushButton()
        self.uninstall_btn.setObjectName("primaryButton")
        self.uninstall_btn.clicked.connect(self.uninstall_selection)
        action_row.addWidget(self.scan_btn)
        action_row.addWidget(self.uninstall_btn)
        apps_layout.addLayout(action_row)
        root_layout.addWidget(main, 1)
        self.setCentralWidget(root)
        self.search_shortcut = QShortcut(QKeySequence("Ctrl+F"), self)
        self.search_shortcut.activated.connect(self.focus_search)
        self.refresh_shortcut = QShortcut(QKeySequence("F5"), self)
        self.refresh_shortcut.activated.connect(self.refresh_current)
        self.delete_shortcut = QShortcut(QKeySequence("Delete"), self)
        self.delete_shortcut.activated.connect(self.delete_current)
        self.update_copy()

    def update_copy(self) -> None:
        self.setWindowTitle(f"{APP_NAME} — {self.t('subtitle')}")
        self.brand_title.setText(self.t("title"))
        self.brand_subtitle.setText(self.t("subtitle"))
        self.nav_label.setText(self.t("all"))
        self.classic_nav.setText(self.t("classic"))
        self.store_nav.setText(self.t("store"))
        self.system_label.setText(self.t("system_section"))
        self.restore_nav.setText(self.t("restore_points"))
        self.about_btn.setText(self.t("about"))
        self.restore_backup_btn.setText(self.t("restore_backup"))
        self.history_btn.setText(self.t("history"))
        if self.restore_active:
            self.page_title.setText(self.t("restore_points"))
            self.page_hint.setText(self.t("restore_points_hint"))
        else:
            self.page_title.setText(self.t("classic" if self.mode == "classic" else "store"))
            self.page_hint.setText(self.t("classic_hint" if self.mode == "classic" else "store_hint"))
        self.search.setPlaceholderText(self.t("search"))
        self.publisher_filter.setItemText(0, self.t("filter_publisher"))
        self.refresh_btn.setText(self.t("refresh"))
        self.table.setHorizontalHeaderLabels(["", self.t("name"), self.t("publisher"), self.t("version"), self.t("date"), self.t("size")])
        self.table.horizontalHeaderItem(COL_CHECK).setToolTip(self.t("check_all"))
        self.table.horizontalHeaderItem(COL_SIZE).setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        for check, key in ((self.restore_check, "restore"), (self.silent_check, "silent"), (self.auto_scan_check, "auto_scan")):
            check.setText(self.t(key))
            check.setToolTip(self.t(key + "_tip"))
        self.scan_btn.setText(self.t("scan"))
        self.uninstall_btn.setText(self.t("queue_uninstall"))
        theme_label = self.t("theme_light" if self.dark else "theme_dark")
        self.theme_btn.setToolTip(theme_label)  # le texte reste dispo en infobulle et pour les lecteurs d'écran
        self.theme_btn.setAccessibleName(theme_label)  # the text stays available as tooltip and for screen readers
        self.language_btn.setText("EN" if self.lang == "fr" else "FR")
        self.update_sidebar_toggle()
        admin = is_admin()
        self.admin_label.setText(self.t("admin_ok") if admin else self.t("admin"))
        self.elevate_btn.setText(self.t("elevate"))
        self.elevate_btn.setVisible(not admin)
        self.table.setAccessibleName(self.t("all"))
        self.table.setAccessibleDescription(self.t("multi_select_hint"))
        self.uninstall_btn.setToolTip(self.t("multi_select_hint"))
        for widget, key in ((self.search, "search"), (self.refresh_btn, "refresh"), (self.uninstall_btn, "queue_uninstall"),
                            (self.scan_btn, "scan"), (self.restore_backup_btn, "restore_backup"),
                            (self.history_btn, "history"), (self.publisher_filter, "filter_publisher")):
            widget.setAccessibleName(self.t(key))
        self.update_check_ui()  # compteur et libellé du bouton / counter and button label
        self.restore_page.update_copy()

    def update_sidebar_toggle(self) -> None:
        label = self.t("show_sidebar" if self.sidebar_collapsed else "hide_sidebar")
        self.sidebar_toggle.setToolTip(label)
        self.sidebar_toggle.setAccessibleName(label)

    def refresh_icons(self) -> None:
        """Icônes au trait dans les couleurs du thème courant.

        Stroke icons in the current theme's colors.
        """
        c = colors(self.dark)
        for button, name in ((self.classic_nav, "apps"), (self.store_nav, "store"), (self.restore_nav, "timeline")):
            button.setIcon(glyph_icon(name, c["text"], c["accent"]))  # accent quand la page est active / accent when active
            button.set_accent(QColor(c["accent"]))
        for button, name in ((self.restore_backup_btn, "undo"), (self.history_btn, "history"), (self.about_btn, "info"),
                             (self.refresh_btn, "refresh"), (self.scan_btn, "search"), (self.elevate_btn, "shield"),
                             (self.theme_btn, "sun" if self.dark else "moon")):
            button.setIcon(glyph_icon(name, c["text"]))
        self.sidebar_toggle.setIcon(glyph_icon("menu", c["text"], size=18))
        self.brand_mark.setPixmap(bolt_pixmap(c["onAccent"], 30))
        self.uninstall_btn.setIcon(glyph_icon("trash", c["onAccent"]))
        self.search.set_theme(self.dark)
        self.table.set_theme(self.dark)
        self.restore_page.set_theme(self.dark)

    def apply_theme(self) -> None:
        self.setStyleSheet(theme_stylesheet(self.dark, self._translucent))
        if self.windowHandle() is not None:  # fenêtre native déjà créée / native window already created
            set_dark_title_bar(self, self.dark)
        self.refresh_icons()
        self.update_copy()

    def update_top_inset(self) -> None:
        """Retrait du haut selon l'état de la fenêtre (aucun en taille normale).

        Top inset according to the window state (none at normal size).
        """
        full = bool(self.windowState() & (Qt.WindowState.WindowMaximized | Qt.WindowState.WindowFullScreen))
        self.root_layout.setContentsMargins(0, MAXIMIZED_TOP_INSET if full else 0, 0, 0)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self.update_top_inset()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.update_top_inset()  # fenêtre ouverte directement agrandie / window opened already maximized
        if self._backdrop_checked:
            return
        self._backdrop_checked = True
        # Acrylic au premier affichage (on voit, flouté, ce qui est derrière la fenêtre) ; si Windows le refuse, thème opaque.
        # Acrylic on first show (what lies behind the window shows through, blurred); if Windows refuses it, opaque theme.
        if self._translucent and not apply_backdrop(self, ACRYLIC, self.dark):
            self._translucent = False
            self.setStyleSheet(theme_stylesheet(self.dark, False))
        set_dark_title_bar(self, self.dark)

    def toggle_theme(self) -> None:
        self.dark = not self.dark
        self.settings.setValue("dark", self.dark)
        self.apply_theme()

    def toggle_sidebar(self) -> None:
        self.sidebar_collapsed = not self.sidebar_collapsed
        self.sidebar.setVisible(not self.sidebar_collapsed)
        self.update_sidebar_toggle()

    def toggle_language(self) -> None:
        self.lang = "en" if self.lang == "fr" else "fr"
        self.settings.setValue("language", self.lang)
        self.update_copy()
        self.populate_table()

    def switch_mode(self, mode: str) -> None:
        """Ouvre une page : « classic » ou « store » (liste d'applications) ou « restore » (points de restauration).

        Opens a page: "classic" or "store" (application list) or "restore" (restore points).
        """
        restore = mode == "restore"
        self.classic_nav.setChecked(not restore and mode == "classic")
        self.store_nav.setChecked(not restore and mode == "store")
        self.restore_nav.setChecked(restore)
        if restore or self.restore_active:
            self.restore_active = restore
            self.pages.setCurrentWidget(self.restore_page if restore else self.apps_page)
            self.update_copy()
            if restore:
                self.restore_page.ensure_loaded()
                return
        if self.mode == mode:
            return
        self.mode = mode
        self.selected = None
        self.update_copy()
        self.refresh_publisher_filter()
        self.populate_table()

    def focus_search(self) -> None:
        search = self.restore_page.search if self.restore_active else self.search
        search.setFocus()
        search.selectAll()

    def refresh_current(self) -> None:
        """F5 : actualise la page affichée. / F5: refreshes the page on screen."""
        if self.restore_active:
            self.restore_page.load()
        else:
            self.load_apps()

    def delete_current(self) -> None:
        """Suppr : désinstalle les applications ou supprime les points cochés. / Del: uninstalls apps or deletes checked points."""
        if self.restore_active:
            self.restore_page.delete_checked()
        else:
            self.uninstall_selection()

    def current_apps(self) -> list[AppEntry]:
        return self.classic_apps if self.mode == "classic" else self.store_apps

    def run_background(self, task, on_success, on_error=None) -> None:
        if self._busy:
            return
        self._busy = True
        self.set_busy(True)
        worker = TaskWorker(task)
        self._workers.append(worker)

        def completed(result):
            self._busy = False
            self.set_busy(False)
            on_success(result)

        def failed(message):
            self._busy = False
            self.set_busy(False)
            if on_error:
                on_error(message)
            else:
                self.status_label.setText(message[:180])
                QMessageBox.critical(self, APP_NAME, message)

        worker.completed.connect(completed)
        worker.failed.connect(failed)
        worker.finished.connect(self.forget_worker)
        worker.start()

    def forget_worker(self) -> None:
        """Retire un thread terminé (l'émetteur du signal) des listes qui le gardent en vie.

        Drop a finished thread (the signal's sender) from the lists that keep it alive.
        """
        worker = self.sender()
        for workers in (self._workers, self._size_workers):
            if worker in workers:
                workers.remove(worker)

    def set_busy(self, busy: bool) -> None:
        self.refresh_btn.setEnabled(not busy)
        self.publisher_filter.setEnabled(not busy)
        self.update_action_buttons()
        self.restore_page.set_busy(busy)
        if busy:
            self.status_label.setText(self.t("busy"))

    def update_action_buttons(self) -> None:
        self.uninstall_btn.setEnabled(not self._busy and bool(self.checked_apps() or self.selected_apps()))
        self.scan_btn.setEnabled(not self._busy and self.selected is not None and self.selected.source == "classic")

    def closeEvent(self, event) -> None:
        if any(worker.isRunning() for worker in self._workers):
            QMessageBox.information(self, APP_NAME, self.t("wait_to_close"))
            event.ignore()
            return
        # Les estimations de taille ne sont qu'un confort : les arrêter plutôt que de faire attendre l'utilisateur.
        # Size estimates are only a convenience: stop them instead of making the user wait.
        self.stop_size_workers()
        for worker in list(self._size_workers):
            worker.wait()
        event.accept()

    def load_apps(self, on_complete=None) -> None:
        if not is_windows():
            QMessageBox.warning(self, APP_NAME, self.t("unavailable"))
            return
        if self._busy:  # F5 pendant une tâche : ne rien annoncer qui n'aura pas lieu / F5 during a task: announce nothing
            return
        self.status_label.setText(self.t("loading"))
        self.selected = None
        def load():
            classic = read_classic_apps()
            store, error = read_store_apps()
            for app in classic:
                size_path = app.location
                if not size_path and app.icon_path:
                    icon_path = Path(display_icon_path(app.icon_path))
                    if icon_path.is_file():
                        size_path = str(icon_path.parent)
                if app.size_bytes is None:
                    app.size_path, app.size_source = (size_path, "pending") if size_path else ("", "unavailable")
            for app in store:
                app.size_path = app.install_location
                app.size_source = "pending" if app.install_location else "unavailable"
            return classic, store, error
        def loaded(result):
            self.classic_apps, self.store_apps, error = result
            # Garder cochées les applications encore installées (une désinstallée disparaît d'elle-même).
            # Keep still-installed apps checked (an uninstalled one drops out by itself).
            self.table.checked &= {app.key for app in self.classic_apps + self.store_apps}
            self.refresh_publisher_filter()
            self.populate_table()
            self.start_size_worker()
            self.status_label.setText(self.t("status_ready") if not error else error[:160])
            if on_complete:
                on_complete()
        self.run_background(load, loaded)

    def stop_size_workers(self) -> None:
        for worker in self._size_workers:
            worker.stop_requested = True

    def start_size_worker(self) -> None:
        """Mesure les dossiers d'installation en arrière-plan ; la liste est déjà utilisable entre-temps.

        Measure install folders in the background; the list is already usable meanwhile.
        """
        self.stop_size_workers()
        pending = [app for app in self.classic_apps + self.store_apps if app.size_source == "pending"]
        if not pending:
            return
        worker = SizeWorker(pending)
        self._size_workers.append(worker)
        worker.size_ready.connect(self.size_computed)
        worker.finished.connect(self.forget_worker)
        worker.start()

    def size_computed(self, app: AppEntry, size: int | None, complete: bool) -> None:
        # Une application d'une liste remplacée par une actualisation n'est simplement plus dans _size_items.
        # An app from a list replaced by a refresh is simply not in _size_items any more.
        app.size_bytes = size
        app.size_source = ("folder" if complete else "partial") if size is not None else "unavailable"
        item = self._size_items.get(id(app))
        if item is None:
            return
        try:
            self.apply_size_item(item, app)
        except RuntimeError:
            self._size_items.pop(id(app), None)  # la table a été reconstruite entre-temps / the table was rebuilt in the meantime

    def size_text(self, app: AppEntry) -> str:
        if app.size_source == "pending":
            return self.t("size_pending")
        return format_size(app.size_bytes, self.lang, app.size_source == "partial")

    def apply_size_item(self, item: QTableWidgetItem, app: AppEntry) -> None:
        item.setText(self.size_text(app))
        item.setData(Qt.ItemDataRole.UserRole + 1, app.size_bytes)  # tri numérique ; None tant que le calcul est en attente / numeric sort; None while pending
        if app.size_bytes is None:
            item.setToolTip("")
        else:
            item.setToolTip(self.t("size_partial_tip" if app.size_source == "partial" else "size_tip"))

    def refresh_publisher_filter(self) -> None:
        selected = self.publisher_filter.currentData()
        was_blocked = self.publisher_filter.blockSignals(True)
        self.publisher_filter.clear()
        self.publisher_filter.addItem(self.t("filter_publisher"), "")
        publishers = sorted({app.publisher for app in self.current_apps() if app.publisher}, key=str.casefold)
        for publisher in publishers:
            self.publisher_filter.addItem(publisher, publisher)
        index = self.publisher_filter.findData(selected)
        self.publisher_filter.setCurrentIndex(max(index, 0))
        self.publisher_filter.blockSignals(was_blocked)

    def populate_table(self) -> None:
        apps = self.current_apps()
        query = self.search.text().casefold().strip()
        publisher = self.publisher_filter.currentData()
        visible = []
        for app in apps:
            if publisher and app.publisher != publisher:
                continue
            searchable = (app.name, app.publisher, app.version, app.location, app.install_location, app.package_full_name, app.package_name)
            if query and not any(query in value.casefold() for value in searchable):
                continue
            visible.append(app)

        was_blocked = self.table.blockSignals(True)
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setSortingEnabled(False)
            self._size_items = {}
            self.table.setRowCount(len(visible))
            unknown_publisher, unknown_version = self.t("publisher_unknown"), self.t("version_unknown")
            for row, app in enumerate(visible):
                check_item = self.table.check_item(app, self.t("check_app").format(name=app.name))
                name_item = SortableItem(app.name)
                name_item.setData(Qt.ItemDataRole.UserRole, app)
                name_item.setIcon(application_icon(app))
                name_item.setToolTip(app.package_full_name if app.source == "store" else app.subkey)
                date_item = SortableItem(format_date(app.date))
                date_item.setData(Qt.ItemDataRole.UserRole + 1, app.date)  # tri sur AAAAMMJJ / sort on YYYYMMDD
                size_item = SortableItem()
                size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.apply_size_item(size_item, app)
                self._size_items[id(app)] = size_item
                row_items = (check_item, name_item, SortableItem(app.publisher or unknown_publisher),
                             SortableItem(app.version or unknown_version), date_item, size_item)
                for col, item in enumerate(row_items):
                    self.table.setItem(row, col, item)
                self.table.setRowHeight(row, 48)
        finally:
            self.table.setSortingEnabled(True)
            self.table.setUpdatesEnabled(True)
            self.table.blockSignals(was_blocked)
        self.table.setVisible(bool(visible))
        self.empty_label.setText(self.t("empty"))
        self.empty_label.setVisible(not visible)
        self.selected = None
        self.table.sync_checks()  # case d'en-tête, compteur et bouton / header box, counter and button

    def filter_table(self) -> None:
        self.search.timer.stop()
        self.populate_table()

    def on_selection_changed(self) -> None:
        row = self.table.currentRow()
        # Ctrl+clic peut désélectionner la ligne courante : elle ne compte alors plus. / Ctrl+click can deselect the current row.
        selected = row >= 0 and self.table.selectionModel().isRowSelected(row)
        item = self.table.item(row, COL_NAME) if selected else None
        self.selected = item.data(Qt.ItemDataRole.UserRole) if item else None
        self.update_action_buttons()

    def selected_apps(self) -> list[AppEntry]:
        return self.table.selected_objects()

    def checked_apps(self) -> list[AppEntry]:
        """Applications cochées de la page courante, y compris celles masquées par la recherche ou le filtre.

        Checked apps of the current page, including those hidden by the search or the filter.
        """
        return [app for app in self.current_apps() if app.key in self.table.checked]

    def update_check_ui(self) -> None:
        """Compteur et libellé du bouton selon les cases cochées. / Counter and button label according to the checked boxes."""
        visible, checked = self.table.rowCount(), len(self.checked_apps())
        self.count_label.setText(self.t("count_checked").format(count=visible, checked=checked) if checked
                                 else self.t("count").format(count=visible))
        self.uninstall_btn.setText(self.t("queue_uninstall_count").format(count=checked) if checked
                                   else self.t("queue_uninstall"))
        self.update_action_buttons()

    def uninstall_selection(self) -> None:
        if self._busy:
            return
        apps = self.checked_apps() or self.selected_apps()  # les cases cochées d'abord / checked boxes first
        if not apps:
            QMessageBox.information(self, APP_NAME, self.t("select"))
            return
        if len(apps) == 1:
            self.uninstall_selected(apps[0])
            return
        prompt = self.t("queue_confirm").format(count=len(apps)) + "\n\n" + "\n".join(f"• {app.name}" for app in apps[:12])
        if len(apps) > 12:
            prompt += f"\n… (+{len(apps) - 12})"
        if QMessageBox.question(self, self.t("confirm_title"), prompt) != QMessageBox.StandardButton.Yes:
            return
        dialog = UninstallQueueDialog(self, apps, self.restore_check.isChecked(), self.dark, self.lang,
                                      self.silent_check.isChecked())
        dialog.start_queue()
        dialog.exec()
        dialog.worker.wait()
        successes = failed = canceled = 0
        finished: list[AppEntry] = []  # désinstalleurs allés au bout / uninstallers that ran to the end
        for app, outcome in zip(apps, dialog.results):
            if outcome is None or outcome.get("canceled"):
                canceled += 1
            elif "error" in outcome:
                failed += 1
                self.record_history(app.name, "uninstall", "failed", {"error": outcome["error"], "queued": True})
            else:
                result = outcome["result"]
                status = uninstall_status(result["exit_code"])
                if status == "success":
                    successes += 1
                else:
                    failed += 1
                finished.append(app)
                self.record_history(app.name, "uninstall", status, {
                    "source": app.source, "exit_code": result["exit_code"], "restore_point": result["restored"],
                    "silent": result.get("silent", False), "queued": True,
                })
        summary = self.t("queue_summary").format(success=successes, failed=failed, canceled=canceled)
        self.status_label.setText(summary)
        QMessageBox.information(self, self.t("queue_title"), summary)
        self.load_apps(lambda: self.offer_leftover_scan(finished))

    def uninstall_selected(self, app: AppEntry | None = None) -> None:
        if self._busy:
            return
        app = app or self.selected
        if app is None:
            QMessageBox.information(self, APP_NAME, self.t("select"))
            return
        if app.source == "classic" and not app.uninstall:  # le dire avant de demander confirmation / say it before asking for confirmation
            QMessageBox.warning(self, APP_NAME, self.t("no_uninstaller"))
            return
        prompt = self.t("store_confirm" if app.source == "store" else "confirm").format(name=app.name)
        if QMessageBox.question(self, self.t("confirm_title"), prompt) != QMessageBox.StandardButton.Yes:
            return
        make_restore, silent = self.restore_check.isChecked(), self.silent_check.isChecked()
        self.status_label.setText(self.t("uninstall_started"))
        self.run_background(
            lambda: perform_uninstall(app, make_restore, silent),
            lambda result: self.uninstall_finished(app, result),
            lambda error: self.uninstall_failed(app, error),
        )

    def uninstall_finished(self, app: AppEntry, result: dict) -> None:
        status = uninstall_status(result["exit_code"])
        self.record_history(app.name, "uninstall", status, {
            "source": app.source, "exit_code": result["exit_code"], "restore_point": result["restored"],
            "silent": result.get("silent", False),
        })
        if result["restored"]:
            QMessageBox.information(self, self.t("restore_title"), self.t("restore_ok"))
        self.status_label.setText(self.t("uninstall_exit").format(code=result["exit_code"]))
        self.load_apps(lambda: self.after_inventory_refresh(app, result))

    def uninstall_failed(self, app: AppEntry, error: str) -> None:
        self.record_history(app.name, "uninstall", "failed", {"error": error})
        message = describe_uninstall_error(error, self.lang)
        if message == error:  # pas un des marqueurs d'erreur propres à FastSweep / not one of FastSweep's own error markers
            message = self.t("uninstall_error").format(error=error)
        title = self.t("restore_title") if error.startswith(RESTORE_POINT_ERROR_PREFIX) else APP_NAME
        QMessageBox.critical(self, title, message)

    def after_inventory_refresh(self, app: AppEntry, result: dict) -> None:
        if app.source == "store":
            if result["exit_code"] == 0:
                QMessageBox.information(self, APP_NAME, self.t("store_ok"))
            return
        if app.hive is None or registry_key_exists(app.hive, app.subkey):
            QMessageBox.information(self, self.t("confirm_title"), self.t("still_installed"))
            return
        if result["exit_code"] in REBOOT_EXIT_CODES:
            QMessageBox.information(self, self.t("confirm_title"), self.t("reboot_required").format(code=result["exit_code"]))
        if self.auto_scan_check.isChecked():
            self.scan_leftovers([app], auto=True)
        elif QMessageBox.question(self, self.t("scan_title"), self.t("scan_ready")) == QMessageBox.StandardButton.Yes:
            self.scan_leftovers([app])

    def offer_leftover_scan(self, apps: list[AppEntry]) -> None:
        """Après une file : une seule analyse pour toutes les applications classiques réellement désinstallées.

        After a queue: a single scan for every desktop app that was really uninstalled.
        """
        gone = [app for app in apps if app.source == "classic" and app.hive is not None
                and not registry_key_exists(app.hive, app.subkey)]
        if not gone:
            return
        if self.auto_scan_check.isChecked():
            self.scan_leftovers(gone, auto=True)
        elif QMessageBox.question(self, self.t("scan_title"),
                                  self.t("scan_ready_many").format(count=len(gone))) == QMessageBox.StandardButton.Yes:
            self.scan_leftovers(gone)

    def scan_leftovers(self, apps: list[AppEntry] | None = None, auto: bool = False) -> None:
        """Analyse les résidus d'une ou plusieurs applications (par défaut : la ligne sélectionnée).

        Scan the leftovers of one or several apps (by default: the selected row).
        En mode automatique, aucune question n'est posée entre les étapes ; chaque suppression reste à confirmer.
        In automatic mode, no question is asked between steps; every deletion still needs confirming.
        """
        if apps is None:
            apps = [self.selected] if self.selected else []
        apps = [app for app in apps if app.source == "classic"]
        if not apps:
            QMessageBox.information(self, APP_NAME, self.t("select"))
            return
        apps = [app for app in apps if app.hive is None or not registry_key_exists(app.hive, app.subkey)]
        if not apps:
            QMessageBox.information(self, self.t("scan_title"), self.t("scan_first"))
            return
        scan = LeftoverScan(apps, auto)
        self.status_label.setText(self.t("busy"))
        self.run_background(
            lambda: registry_candidates_for(scan.apps),
            lambda matches: self.show_registry_candidates(scan, matches),
        )

    def show_registry_candidates(self, scan: LeftoverScan, matches: list[RegistryCandidate]) -> None:
        if not matches:
            if not scan.auto:  # en automatique, le bilan vient à la fin / in automatic mode, the summary comes at the end
                QMessageBox.information(self, self.t("scan_title"), self.t("scan_none"))
            self.next_file_scan(scan)
            return
        scan.found = True
        dialog = ResidueDialog(self, matches, self.dark, self.lang)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.selected:
            self.next_file_scan(scan)
            return
        prompt = f"{self.t('registry_warning')}\n\n{self.t('delete_selected')} ({len(dialog.selected)})?"
        if not self.confirm_risky(self.t("scan_title"), prompt):
            return
        candidates = list(dialog.selected)
        def clean():
            backup_folder = backup_registry_keys(candidates)
            errors = []
            for candidate in candidates:
                try:
                    delete_registry_tree(candidate.hive, candidate.path)
                except OSError as exc:
                    errors.append(f"{candidate.path}: {exc}")
            return backup_folder, errors, [candidate.path for candidate in candidates]
        self.run_background(
            clean,
            lambda result: self.registry_cleanup_finished(scan, result),
            lambda error: self.registry_cleanup_failed(scan, error),
        )

    def registry_cleanup_finished(self, scan: LeftoverScan, result: tuple[Path, list[str], list[str]]) -> None:
        backup_folder, errors, paths = result
        self.record_history(scan.label, "registry", "partial" if errors else "success", {
            "backup_folder": str(backup_folder), "keys": paths, "errors": errors,
        })
        if errors:
            QMessageBox.warning(self, self.t("scan_title"), self.t("delete_error").format(error="\n".join(errors)) + f"\n\n{backup_folder}")
        else:
            QMessageBox.information(self, self.t("backup_registry"), self.t("backup_done").format(path=backup_folder))
        self.next_file_scan(scan)

    def registry_cleanup_failed(self, scan: LeftoverScan, error: str) -> None:
        self.record_history(scan.label, "registry", "failed", {"error": error})
        QMessageBox.critical(self, self.t("backup_registry"), self.t("backup_failed") + "\n\n" + error)

    def next_file_scan(self, scan: LeftoverScan) -> None:
        if scan.auto or QMessageBox.question(self, self.t("file_scan_title"), self.t("file_ask")) == QMessageBox.StandardButton.Yes:
            self.run_background(
                lambda: file_candidates_for(scan.apps),
                lambda candidates: self.show_file_candidates(scan, candidates),
            )
        else:
            self.status_label.setText(self.t("status_ready"))

    def show_file_candidates(self, scan: LeftoverScan, candidates: list[FileCandidate]) -> None:
        if not candidates:
            self.record_history(scan.label, "files", "success", {"items": [], "message": "no-candidates"})
            nothing = scan.auto and not scan.found
            QMessageBox.information(self, self.t("scan_title" if nothing else "file_scan_title"),
                                    self.t("scan_nothing" if nothing else "file_scan_none"))
            self.status_label.setText(self.t("status_ready"))
            return
        dialog = FileResidueDialog(self, candidates, self.dark, self.lang)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.selected:
            return
        prompt = f"{self.t('file_scan_warning')}\n\n{self.t('file_recycle')} ({len(dialog.selected)})?"
        if not self.confirm_risky(self.t("file_scan_title"), prompt):
            return
        selected = list(dialog.selected)
        def recycle_selected():
            moved, errors = [], []
            for candidate in selected:
                try:
                    recycle_path(candidate.path)
                    moved.append(candidate.path)
                except OSError as exc:
                    errors.append(f"{candidate.path}: {exc}")
            return moved, errors
        self.run_background(recycle_selected, lambda result: self.file_cleanup_finished(scan, result))

    def file_cleanup_finished(self, scan: LeftoverScan, result: tuple[list[str], list[str]]) -> None:
        moved, errors = result
        self.record_history(scan.label, "files", "partial" if errors else "success", {"moved_to_recycle_bin": moved, "errors": errors})
        if errors:
            QMessageBox.warning(self, self.t("file_scan_title"), self.t("file_error").format(error="\n".join(errors)))
        else:
            QMessageBox.information(self, self.t("file_scan_title"), self.t("file_done"))

    def confirm_risky(self, title: str, text: str) -> bool:
        """Avertissement Oui / Annuler, Annuler étant le bouton par défaut.

        Warning with Yes / Cancel, Cancel being the default button.
        """
        buttons = QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        answer = QMessageBox.warning(self, title, text, buttons, QMessageBox.StandardButton.Cancel)
        return answer == QMessageBox.StandardButton.Yes

    def record_history(self, app: str, operation: str, status: str, details: dict) -> None:
        try:
            record_operation(app, operation, status, details)
        except OSError as exc:
            self.status_label.setText(str(exc)[:180])

    def show_history(self) -> None:
        HistoryDialog(self, self.dark, self.lang).exec()

    def restore_backup(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, self.t("restore_backup"), str(registry_backup_root()), "Registry backup (*.reg)")
        if not path:
            return
        if not self.confirm_risky(self.t("backup_registry"), self.t("backup_confirm")):
            return
        def restored(result: tuple[bool, str]) -> None:
            ok, detail = result
            if ok:
                QMessageBox.information(self, self.t("backup_registry"), self.t("backup_restored"))
            else:
                QMessageBox.critical(self, self.t("backup_registry"), self.t("backup_restore_failed").format(error=detail))
        self.run_background(lambda: restore_registry_backup(path), restored)

    def elevate_application(self) -> None:
        if self._busy:  # la fenêtre refuserait de se fermer : pas de seconde instance / the window would refuse to close
            QMessageBox.information(self, APP_NAME, self.t("wait_to_close"))
            return
        if relaunch_as_admin():
            self.close()
        else:
            QMessageBox.information(self, APP_NAME, self.t("admin"))

    def show_details(self, row: int, column: int) -> None:
        if column == COL_CHECK:  # un double clic sur la case ne fait que cocher / a double click on the box only checks
            return
        item = self.table.item(row, COL_NAME)
        if item:
            app = item.data(Qt.ItemDataRole.UserRole)
            location = app.location or app.install_location or (app.hive_label + "\\" + app.subkey if app.source == "classic" else "—")
            details = [
                app.name,
                f"{self.t('publisher')}: {app.publisher or self.t('publisher_unknown')}",
                f"{self.t('version')}: {app.version or self.t('version_unknown')}",
                f"{self.t('size')}: {self.size_text(app)}",
                f"{self.t('installed_location')}: {location or '—'}",
                f"{self.t('package_id')}: {app.package_full_name or '—'}",
                f"{self.t('uninstall_command')}: {app.uninstall or '—'}",
                f"{self.t('silent_command')}: {silent_uninstall_command(app) or '—'}",
            ]
            box = QMessageBox(self)
            box.setWindowTitle(self.t("detail"))
            box.setText("\n\n".join(details))
            box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            box.setStyleSheet(theme_stylesheet(self.dark))
            box.exec()

    def show_about(self) -> None:
        QMessageBox.about(self, self.t("about"), self.t("about_text"))
