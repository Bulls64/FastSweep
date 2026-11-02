"""Page « Points de restauration » : liste, cases à cocher et suppression, le plus récent restant protégé.

"Restore points" page: list, checkboxes and deletion, the most recent one staying protected.
"""
from __future__ import annotations

from PySide6.QtCore import QSize, QTimer, Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .dialogs import FluentDialog
from .glyphs import glyph_icon
from .i18n import APP_NAME, STRINGS
from .restore_points import (
    TYPE_KEYS,
    RestorePoint,
    delete_restore_points,
    list_restore_points,
    protected_sequences,
    select_by_rule,
)
from .system import is_admin, is_windows
from .theme import colors
from .uninstall import MANUAL_RESTORE_DESCRIPTION, create_restore_point
from .widgets import CheckTable, InfoBar, SearchBox, SelectionDelegate, SortableItem

# Colonnes du tableau ; la première porte les cases à cocher (ou le cadenas du point protégé).
# Table columns; the first one holds the checkboxes (or the protected point's lock).
COL_CHECK, COL_DATE, COL_DESCRIPTION, COL_TYPE, COL_SEQUENCE, COL_STATUS = range(6)
MAX_LISTED = 12  # points énumérés dans la confirmation / points listed in the confirmation


def format_point_date(point: RestorePoint) -> str:
    """Date et heure locales, ou « — » si Windows n'en donne pas.

    Local date and time, or "—" when Windows gives none.
    """
    return point.created.astimezone().strftime("%d/%m/%Y %H:%M") if point.created else "—"


# Règle de nettoyage mémorisée : réglage → valeur par défaut. / Remembered cleanup rule: setting → default value.
RULE_DEFAULTS = {"rp_rule": "age", "rp_rule_days": 30, "rp_rule_keep": 3}


class RestoreRuleDialog(FluentDialog):
    """Nettoyage par règle : par âge ou en ne gardant que les N plus récents, avec la liste de ce qui partirait.

    Rule-based cleanup: by age or keeping only the N most recent, with the list of what would go.
    ``selected`` contient les points à supprimer une fois le dialogue accepté.
    ``selected`` holds the points to delete once the dialog is accepted.
    """

    def __init__(self, parent: QWidget | None, points: list[RestorePoint], dark: bool, lang: str,
                 mode: str = "age", days: int = 30, keep: int = 3):
        self.strings = STRINGS[lang]
        super().__init__(parent, self.strings["rp_rule_title"], dark)
        self.points = points
        self.selected: list[RestorePoint] = []
        self.setMinimumSize(620, 480)
        self.age_radio = QRadioButton(self.strings["rp_rule_age_before"])
        self.keep_radio = QRadioButton(self.strings["rp_rule_keep_before"])
        group = QButtonGroup(self)
        group.addButton(self.age_radio)
        group.addButton(self.keep_radio)
        self.days = QSpinBox()
        self.days.setFixedWidth(92)
        self.days.setRange(1, 3650)
        self.days.setValue(int(days))
        self.keep = QSpinBox()
        self.keep.setFixedWidth(92)
        self.keep.setRange(1, 100)
        self.keep.setValue(int(keep))
        for radio, spin, after in ((self.age_radio, self.days, "rp_rule_age_after"),
                                   (self.keep_radio, self.keep, "rp_rule_keep_after")):
            row = QHBoxLayout()
            row.setSpacing(8)
            row.addWidget(radio)
            row.addWidget(spin)
            row.addWidget(QLabel(self.strings[after]))
            row.addStretch(1)
            self.body.addLayout(row)
            spin.setAccessibleName(f"{radio.text()} … {self.strings[after]}")
            # Régler un nombre choisit la règle qui va avec. / Setting a number picks the matching rule.
            spin.valueChanged.connect(lambda _value, radio=radio: radio.setChecked(True))
            spin.valueChanged.connect(self.refresh)
            radio.toggled.connect(self.refresh)
        self.body.addWidget(InfoBar(self.strings["rp_rule_info"], dark, "info"))
        self.preview = QLabel()
        self.body.addWidget(self.preview)
        self.list = QListWidget()
        self.list.setItemDelegate(SelectionDelegate(self.list, dark, row_height=32))
        self.body.addWidget(self.list, 1)
        self.apply_button = self.add_footer_button("", "dangerButton", "trash")
        self.apply_button.clicked.connect(self.accept)
        close = self.add_footer_button(self.strings["close"])
        close.clicked.connect(self.reject)
        (self.keep_radio if mode == "keep" else self.age_radio).setChecked(True)
        self.refresh()

    @property
    def mode(self) -> str:
        return "keep" if self.keep_radio.isChecked() else "age"

    def refresh(self) -> None:
        if self.mode == "keep":
            self.selected = select_by_rule(self.points, keep_latest=self.keep.value())
        else:
            self.selected = select_by_rule(self.points, older_than_days=self.days.value())
        self.list.clear()
        self.list.addItems([f"{format_point_date(point)} — {point.description or self.strings['rp_no_description']}"
                            for point in self.selected])
        count = len(self.selected)
        self.preview.setText(self.strings["rp_rule_preview"].format(count=count) if count else self.strings["rp_rule_none"])
        self.apply_button.setText(self.strings["rp_rule_apply"].format(count=count))
        self.apply_button.setEnabled(bool(count))


class RestorePointsPage(QWidget):
    """Contenu de la page, affiché à la place de la liste d'applications ; s'appuie sur la fenêtre principale (``host``).

    Page content, shown instead of the application list; relies on the main window (``host``).
    ``host`` fournit t(), dark, run_background(), confirm_risky() et record_history().
    ``host`` provides t(), dark, run_background(), confirm_risky() and record_history().
    """

    def __init__(self, host):
        super().__init__()
        self.host = host
        self.points: list[RestorePoint] = []
        self._protected: set[int] = set()
        self._loaded = False
        self._pending_load = False
        self._busy = False
        self.error = ""
        self.build_ui()

    def t(self, key: str) -> str:
        return self.host.t(key)

    def build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self.search = SearchBox()
        self.search.search_changed.connect(self.populate_table)
        search_row.addWidget(self.search, 1)
        self.create_btn = QPushButton()
        self.create_btn.clicked.connect(self.create_point)
        self.rule_btn = QPushButton()
        self.rule_btn.clicked.connect(self.clean_by_rule)
        search_row.addWidget(self.rule_btn)
        self.refresh_btn = QPushButton()
        self.refresh_btn.clicked.connect(self.load)
        search_row.addWidget(self.create_btn)
        search_row.addWidget(self.refresh_btn)
        layout.addLayout(search_row)

        self.info_bar = InfoBar("", self.host.dark, "info")
        layout.addWidget(self.info_bar)
        self.admin_bar = InfoBar("", self.host.dark, "warning")
        layout.addWidget(self.admin_bar)

        self.card = QFrame()
        self.card.setObjectName("card")
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(4, 0, 4, 4)
        card_layout.setSpacing(0)
        # Clé mémorisée : le numéro de séquence ; le point protégé n'a pas de case. / Stored key: the sequence number; no box for the protected point.
        self.table = CheckTable(6, self.host.dark, key=lambda point: point.sequence, primary_column=COL_DESCRIPTION)
        self.table.setObjectName("restoreTable")
        self.table.setIconSize(QSize(16, 16))
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_DESCRIPTION, QHeaderView.ResizeMode.Stretch)
        header.setSortIndicator(COL_DATE, Qt.SortOrder.DescendingOrder)  # le plus récent en haut / newest on top
        self.table.setSortingEnabled(True)
        self.table.checks_changed.connect(self.update_check_ui)
        self.table.itemSelectionChanged.connect(self.update_action_buttons)
        card_layout.addWidget(self.table, 1)
        self.empty_label = QLabel()
        self.empty_label.setObjectName("emptyState")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.hide()
        card_layout.addWidget(self.empty_label, 1)
        layout.addWidget(self.card, 1)

        footer = QHBoxLayout()
        self.count_label = QLabel()
        self.count_label.setObjectName("muted")
        footer.addWidget(self.count_label)
        footer.addStretch(1)
        layout.addLayout(footer)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.status_label = QLabel()
        self.status_label.setObjectName("muted")
        action_row.addWidget(self.status_label)
        action_row.addStretch(1)
        self.delete_btn = QPushButton()
        self.delete_btn.setObjectName("dangerButton")
        self.delete_btn.clicked.connect(self.delete_checked)
        action_row.addWidget(self.delete_btn)
        layout.addLayout(action_row)
        self.update_copy()

    # --- Textes et thème / Texts and theme ---------------------------------------------------------

    def update_copy(self) -> None:
        self.search.setPlaceholderText(self.t("rp_search"))
        self.search.setAccessibleName(self.t("rp_search"))
        self.create_btn.setText(self.t("rp_create"))
        self.rule_btn.setText(self.t("rp_rule"))
        self.rule_btn.setToolTip(self.t("rp_rule_tip"))
        self.refresh_btn.setText(self.t("refresh"))
        self.info_bar.set_text(self.t("rp_info"))
        self.admin_bar.set_text(self.t("rp_admin"))
        self.admin_bar.setVisible(not is_admin())
        self.table.setHorizontalHeaderLabels(["", self.t("rp_date"), self.t("rp_description"), self.t("rp_type"),
                                              self.t("rp_sequence"), self.t("rp_status")])
        self.table.horizontalHeaderItem(COL_CHECK).setToolTip(self.t("check_all"))
        self.table.horizontalHeaderItem(COL_SEQUENCE).setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.table.setAccessibleName(self.t("restore_points"))
        self.delete_btn.setAccessibleName(self.t("rp_delete"))
        if not self._busy:
            self.status_label.setText(self.t("status_ready"))
        if self._loaded:
            self.populate_table()  # dates, types et statut traduits / translated dates, types and status
        else:
            self.update_check_ui()

    def set_theme(self, dark: bool) -> None:
        c = colors(dark)
        self.refresh_btn.setIcon(glyph_icon("refresh", c["text"]))
        self.create_btn.setIcon(glyph_icon("plus", c["text"]))
        self.rule_btn.setIcon(glyph_icon("history", c["text"]))
        self.delete_btn.setIcon(glyph_icon("trash", c["onDanger"]))
        self.search.set_theme(dark)
        self.info_bar.set_theme(dark)
        self.admin_bar.set_theme(dark)
        self.table.set_theme(dark)
        lock = self.lock_icon()
        for row in range(self.table.rowCount()):
            if not self.table.is_checkable(row):  # seul le point protégé n'a pas de case / only the protected point has no box
                self.table.item(row, COL_CHECK).setIcon(lock)

    # --- Chargement / Loading ----------------------------------------------------------------------

    def ensure_loaded(self) -> None:
        """Première lecture quand la page est ouverte (pas au démarrage : PowerShell prend quelques secondes).

        First read when the page is opened (not at startup: PowerShell takes a few seconds).
        """
        if not self._loaded:
            self.load()

    def load(self) -> None:
        if not is_windows():
            self._loaded = True
            self.error = self.t("unavailable")
            self.populate_table()
            return
        if self._busy:  # une autre opération est en cours : charger dès qu'elle se termine / another task runs: load when it ends
            self._pending_load = True
            return
        self._pending_load = False
        self.status_label.setText(self.t("rp_loading"))
        self.host.run_background(list_restore_points, self.loaded, self.load_failed)

    def loaded(self, points: list[RestorePoint]) -> None:
        self._loaded = True
        self.error = ""
        self.points = sorted(points, key=lambda point: point.sequence, reverse=True)  # du plus récent au plus ancien / newest first
        self._protected = protected_sequences(self.points)
        # Ne garder cochés que les points encore présents et supprimables. / Keep only points still present and deletable.
        self.table.checked &= {point.sequence for point in self.points} - self._protected
        self.populate_table()
        self.status_label.setText(self.t("status_ready"))

    def load_failed(self, error: str) -> None:
        self._loaded = True
        self.points, self._protected = [], set()
        self.table.checked.clear()
        self.error = self.t("rp_load_error").format(error=error.strip()[:400])
        self.populate_table()
        self.status_label.setText(self.t("status_ready"))

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.refresh_btn.setEnabled(not busy)
        self.create_btn.setEnabled(not busy and is_windows())
        self.update_action_buttons()
        if busy:
            self.status_label.setText(self.t("busy"))
            return
        if self.status_label.text() == self.t("busy"):  # le rappel de la tâche peut le remplacer / the task's callback may replace it
            self.status_label.setText(self.t("status_ready"))
        if self._pending_load:
            QTimer.singleShot(0, self.load)  # après le rappel de la tâche finie / after the finished task's callback

    # --- Tableau / Table ---------------------------------------------------------------------------

    def type_label(self, point: RestorePoint) -> str:
        key = TYPE_KEYS.get(point.kind)
        return self.t(key) if key else self.t("rp_type_unknown").format(code=point.kind)

    def visible_points(self) -> list[RestorePoint]:
        query = self.search.text().casefold().strip()
        if not query:
            return list(self.points)
        return [point for point in self.points
                if any(query in value.casefold() for value in
                       (point.description, format_point_date(point), self.type_label(point), str(point.sequence)))]

    def lock_icon(self):
        return glyph_icon("lock", colors(self.host.dark)["text2"])

    def populate_table(self) -> None:
        visible = self.visible_points()
        lock = self.lock_icon()
        with self.table.rebuilding(len(visible)):
            for row, point in enumerate(visible):
                protected = point.sequence in self._protected
                description = point.description or self.t("rp_no_description")
                accessible = self.t("rp_protected") if protected else self.t("check_point").format(name=description)
                check_item = self.table.check_item(point, accessible, checkable=not protected)
                check_item.setData(Qt.ItemDataRole.UserRole + 1, 0 if protected else 1)
                if protected:
                    # Pas de case : un cadenas, et la ligne reste sélectionnable pour la lire. / No box: a lock; the row stays selectable.
                    check_item.setIcon(lock)
                    check_item.setToolTip(self.t("rp_protected_tip"))
                date_item = SortableItem(format_point_date(point))
                date_item.setData(Qt.ItemDataRole.UserRole + 1, point.created.timestamp() if point.created else float(point.sequence))
                description_item = SortableItem(description)
                description_item.setData(Qt.ItemDataRole.UserRole, point)
                description_item.setToolTip(description)
                sequence_item = SortableItem(str(point.sequence))
                sequence_item.setData(Qt.ItemDataRole.UserRole + 1, point.sequence)
                sequence_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                status_item = SortableItem(self.t("rp_protected") if protected else "")
                if protected:
                    status_item.setToolTip(self.t("rp_protected_tip"))
                for col, item in enumerate((check_item, date_item, description_item, SortableItem(self.type_label(point)),
                                            sequence_item, status_item)):
                    self.table.setItem(row, col, item)
                self.table.setRowHeight(row, 44)
            if self.error:
                self.empty_label.setText(self.error)
            else:
                self.empty_label.setText(self.t("rp_no_match") if self.points else self.t("rp_empty"))
            self.empty_label.setVisible(not visible and self._loaded)

    def checked_points(self) -> list[RestorePoint]:
        """Points cochés, y compris ceux masqués par la recherche ; jamais le plus récent.

        Checked points, including those hidden by the search; never the most recent one.
        """
        return [point for point in self.points if point.sequence in self.table.checked and point.sequence not in self._protected]

    def selected_points(self) -> list[RestorePoint]:
        return [point for point in self.table.selected_objects() if point.sequence not in self._protected]

    def update_check_ui(self) -> None:
        visible, checked = self.table.rowCount(), len(self.checked_points())
        self.count_label.setText(self.t("rp_count_checked").format(count=visible, checked=checked) if checked
                                 else self.t("rp_count").format(count=visible))
        self.delete_btn.setText(self.t("rp_delete_count").format(count=checked) if checked else self.t("rp_delete"))
        self.update_action_buttons()

    def update_action_buttons(self) -> None:
        self.rule_btn.setEnabled(not self._busy and len(self.points) > 1)  # il faut plus que le point protégé / more than the protected point
        self.delete_btn.setEnabled(not self._busy and bool(self.checked_points() or self.selected_points()))

    # --- Actions -----------------------------------------------------------------------------------

    def delete_checked(self) -> None:
        if self._busy:
            return
        points = self.checked_points() or self.selected_points()  # les cases cochées d'abord / checked boxes first
        if not points:
            QMessageBox.information(self, APP_NAME, self.t("rp_select"))
            return
        self.delete_points(points)

    def clean_by_rule(self) -> None:
        """Ouvre le nettoyage par règle (dernière règle mémorisée) puis supprime, après confirmation, ce qu'elle retient.

        Opens the rule-based cleanup (last remembered rule), then deletes what it picks, after confirmation.
        """
        if self._busy or len(self.points) < 2:
            return
        settings = self.host.settings
        values = {key: settings.value(key, default, type=type(default)) for key, default in RULE_DEFAULTS.items()}
        dialog = RestoreRuleDialog(self, self.points, self.host.dark, self.host.lang,
                                   values["rp_rule"], values["rp_rule_days"], values["rp_rule_keep"])
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        settings.setValue("rp_rule", dialog.mode)
        settings.setValue("rp_rule_days", dialog.days.value())
        settings.setValue("rp_rule_keep", dialog.keep.value())
        if dialog.selected:
            self.delete_points(dialog.selected)

    def delete_points(self, points: list[RestorePoint]) -> None:
        """Confirmation puis suppression ; le moteur refuse de toute façon le plus récent.

        Confirmation then deletion; the backend refuses the most recent one anyway.
        """
        listed = "\n".join(f"• {format_point_date(point)} — {point.description or self.t('rp_no_description')}"
                           for point in points[:MAX_LISTED])
        if len(points) > MAX_LISTED:
            listed += f"\n… (+{len(points) - MAX_LISTED})"
        prompt = self.t("rp_confirm").format(count=len(points)) + "\n\n" + listed
        if not self.host.confirm_risky(self.t("rp_confirm_title"), prompt):
            return
        sequences = [point.sequence for point in points]
        self.host.run_background(lambda: delete_restore_points(sequences), self.deleted, self.action_failed)

    def deleted(self, report) -> None:
        for point in report.deleted:
            self.host.record_history(point.description or f"#{point.sequence}", "restore_point", "success",
                                     {"sequence": point.sequence, "created": format_point_date(point)})
        for point, error in report.errors:
            self.host.record_history(point.description or f"#{point.sequence}", "restore_point", "failed",
                                     {"sequence": point.sequence, "created": format_point_date(point), "error": error})
        lines = [self.t("rp_deleted").format(count=len(report.deleted))]
        if report.errors:
            lines.append(self.t("rp_errors").format(count=len(report.errors), errors="\n".join(
                f"• {point.description or point.sequence}: {error}" for point, error in report.errors[:MAX_LISTED])))
        if report.protected:
            lines.append(self.t("rp_kept_newest"))
        if report.missing:
            lines.append(self.t("rp_missing").format(count=len(report.missing)))
        self.table.checked -= {point.sequence for point in report.deleted} | set(report.missing)
        message = "\n\n".join(lines)
        self.status_label.setText(lines[0])
        if report.errors:
            QMessageBox.warning(self, self.t("restore_points"), message)
        else:
            QMessageBox.information(self, self.t("restore_points"), message)
        self.load()

    def create_point(self) -> None:
        if self._busy or not is_windows():
            return
        self.status_label.setText(self.t("rp_creating"))
        self.host.run_background(lambda: create_restore_point(MANUAL_RESTORE_DESCRIPTION), self.created, self.action_failed)

    def created(self, result: tuple[bool, str]) -> None:
        ok, detail = result
        self.host.record_history(MANUAL_RESTORE_DESCRIPTION, "restore_create", "success" if ok else "failed",
                                 {} if ok else {"error": detail})
        if ok:
            self.status_label.setText(self.t("rp_created"))
            QMessageBox.information(self, self.t("restore_points"), self.t("rp_created"))
        else:
            QMessageBox.critical(self, self.t("restore_points"), self.t("rp_create_failed").format(error=detail or "—"))
        self.load()

    def action_failed(self, error: str) -> None:
        self.status_label.setText(error[:180])
        QMessageBox.critical(self, self.t("restore_points"), error)
        self.load()
