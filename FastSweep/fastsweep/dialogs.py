"""Boîtes de dialogue : file de désinstallation, résidus de registre et de fichiers, historique.

Dialogs: uninstall queue, registry and file leftovers, history.
"""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .backdrop import ACRYLIC, apply_backdrop, prepare_translucency, set_dark_title_bar
from .glyphs import glyph_icon
from .history import load_operation_history
from .i18n import STRINGS
from .models import AppEntry, FileCandidate, RegistryCandidate
from .theme import colors, theme_stylesheet
from .uninstall import RESTORE_POINT_ERROR_PREFIX, describe_uninstall_error
from .widgets import FluentTable, InfoBar, SelectionDelegate
from .workers import UninstallQueueWorker


class FluentDialog(QDialog):
    """Dialogue façon ContentDialog WinUI : titre, contenu, pied de page teinté ; fond Acrylic sous Windows 11.

    WinUI ContentDialog-style dialog: title, content, tinted footer; Acrylic backdrop on Windows 11.
    """

    def __init__(self, parent: QWidget | None, title: str, dark: bool):
        super().__init__(parent)
        self.dark = dark
        self.translucent = prepare_translucency(self)
        self._backdrop_checked = False
        self.setProperty("backdrop", self.translucent)
        self.setWindowTitle(title)
        self.setStyleSheet(theme_stylesheet(dark, self.translucent))
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        content = QWidget()
        self.body = QVBoxLayout(content)
        self.body.setContentsMargins(24, 24, 24, 24)
        self.body.setSpacing(12)
        heading = QLabel(title)
        heading.setObjectName("dialogTitle")
        heading.setWordWrap(True)
        self.body.addWidget(heading)
        outer.addWidget(content, 1)
        footer = QFrame()
        footer.setObjectName("dialogFooter")
        self.footer = QHBoxLayout(footer)
        self.footer.setContentsMargins(24, 24, 24, 24)
        self.footer.setSpacing(8)
        outer.addWidget(footer)

    def add_footer_button(self, text: str, kind: str = "", icon: str = "") -> QPushButton:
        """Boutons du pied de page, de même largeur comme dans WinUI.

        Footer buttons, all the same width as in WinUI.
        """
        button = QPushButton(text)
        if kind:
            button.setObjectName(kind)
        if icon:
            on_fill = colors(self.dark)["onDanger" if kind == "dangerButton" else "text"]
            button.setIcon(glyph_icon(icon, on_fill))
        self.footer.addWidget(button, 1)
        return button

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._backdrop_checked:
            return
        self._backdrop_checked = True
        # Acrylic au premier affichage ; si Windows le refuse, thème opaque.
        # Acrylic on first show; if Windows refuses it, opaque theme.
        if self.translucent and not apply_backdrop(self, ACRYLIC, self.dark):
            self.translucent = False
            self.setProperty("backdrop", False)
            self.setStyleSheet(theme_stylesheet(self.dark, False))
        set_dark_title_bar(self, self.dark)


class UninstallQueueDialog(FluentDialog):
    def __init__(self, parent: QWidget, apps: list[AppEntry], make_restore: bool, dark: bool, lang: str,
                 silent: bool = False):
        self.strings = STRINGS[lang]
        super().__init__(parent, self.strings["queue_title"], dark)
        self.lang = lang
        self.apps = apps
        self.worker = UninstallQueueWorker(apps, make_restore, silent)
        self.results: list[dict | None] = [None] * len(apps)
        self.setMinimumWidth(520)
        self.progress_label = QLabel(self.strings["status_ready"])
        self.progress_label.setWordWrap(True)
        self.body.addWidget(self.progress_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, len(apps))
        self.progress_bar.setTextVisible(False)
        self.body.addWidget(self.progress_bar)
        self.items = QListWidget()
        self.items.setItemDelegate(SelectionDelegate(self.items, dark, row_height=40))
        self.items.addItems([f"{app.name} — {self.strings['queue_waiting']}" for app in apps])
        self.body.addWidget(self.items, 1)
        self.cancel_button = self.add_footer_button(self.strings["queue_cancel_pending"])
        self.cancel_button.clicked.connect(self.cancel_queued)
        self.worker.progress.connect(self.update_progress)
        self.worker.item_finished.connect(self.update_item)
        self.worker.completed.connect(self.queue_completed)

    def start_queue(self) -> None:
        self.worker.start()

    def cancel_queued(self) -> None:
        self.worker.cancel_pending = True
        self.cancel_button.setEnabled(False)
        if self.worker.isRunning():
            self.progress_label.setText(self.strings["queue_finishing"])

    def reject(self) -> None:
        # Échap et le bouton de fermeture de la barre de titre (QDialog.closeEvent appelle reject) arrivent tous deux ici.
        # Ne jamais quitter tant qu'un désinstalleur tourne : annuler ce qui est en attente et rester
        # réactif jusqu'à la fin de l'étape en cours.
        # Esc and the title-bar close button (QDialog.closeEvent calls reject) both land here.
        # Never leave while an uninstaller still runs: cancel what is pending and stay
        # responsive until the current step ends.
        if self.worker.isRunning():
            if not self.worker.cancel_pending:
                self.cancel_queued()
            return
        super().reject()

    def update_progress(self, current: int, total: int, name: str) -> None:
        if current == 0:
            self.progress_label.setText(self.strings["queue_restore"])
            return
        self.progress_bar.setValue(current - 1)
        self.progress_label.setText(self.strings["queue_progress"].format(current=current, total=total, name=name))
        self.items.setCurrentRow(current - 1)

    def update_item(self, index: int, outcome: dict) -> None:
        self.results[index] = outcome
        if outcome.get("canceled"):
            status = self.strings["queue_canceled"]
        elif "error" in outcome:
            message = describe_uninstall_error(outcome["error"], self.lang)
            status = self.strings["queue_failed"].format(error=message[:100])
            if outcome["error"].startswith(RESTORE_POINT_ERROR_PREFIX):
                self.progress_label.setText(message)
        else:
            status = self.strings["queue_done"].format(code=outcome["result"]["exit_code"])
            if outcome["result"].get("silent") and self.apps[index].source == "classic":
                status += " · " + self.strings["queue_silent"]
        self.items.item(index).setText(f"{self.apps[index].name} — {status}")

    def queue_completed(self, canceled: bool) -> None:
        self.progress_bar.setValue(self.progress_bar.maximum())
        if canceled or self.worker.cancel_pending:
            self.progress_label.setText(self.strings["queue_cancelled_summary"])
        self.cancel_button.setText(self.strings["close"])
        self.cancel_button.setEnabled(True)
        self.cancel_button.clicked.disconnect()
        self.cancel_button.clicked.connect(self.accept)


class CandidateDialog(FluentDialog):
    """Liste de cases à cocher des résidus candidats ; rien n'est sélectionné tant que l'utilisateur n'a pas coché.

    Checklist of leftover candidates; nothing is selected until the user ticks it.
    """

    TITLE_KEY = FOUND_KEY = WARNING_KEY = ACCEPT_KEY = ""
    SEVERITY = "warning"
    REASON_KEYS: dict[str, str] = {}

    def __init__(self, parent: QWidget | None, candidates: list, dark: bool, lang: str):
        self.strings = STRINGS[lang]
        super().__init__(parent, self.strings[self.TITLE_KEY], dark)
        self.selected: list = []
        self.setMinimumSize(720, 480)
        found = QLabel(self.strings[self.FOUND_KEY].format(count=len(candidates)))
        found.setWordWrap(True)
        self.body.addWidget(found)
        self.body.addWidget(InfoBar(self.strings[self.WARNING_KEY], dark, self.SEVERITY))
        content = QWidget()
        content.setObjectName("scrollContent")
        rows = QVBoxLayout(content)
        rows.setContentsMargins(12, 8, 12, 8)
        rows.setSpacing(4)
        self.checks: list[tuple[QCheckBox, object]] = []
        for candidate in candidates:
            confidence = self.strings[f"confidence_{candidate.confidence}"]
            reason = self.strings[self.REASON_KEYS[candidate.reason]]
            label, tooltip = self.describe(candidate, confidence, reason)
            checkbox = QCheckBox(label)
            checkbox.setMinimumHeight(32)
            checkbox.setToolTip(tooltip)
            checkbox.setAccessibleDescription(f"{confidence}. {reason}. {self.location(candidate)}")
            checkbox.toggled.connect(self.update_selection)
            rows.addWidget(checkbox)
            self.checks.append((checkbox, candidate))
        rows.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.viewport().setAutoFillBackground(False)
        scroll.setWidget(content)
        self.body.addWidget(scroll, 1)
        # Action principale à gauche, comme dans un ContentDialog.
        # Primary action on the left, as in a ContentDialog.
        self.accept_button = self.add_footer_button(self.strings[self.ACCEPT_KEY], "dangerButton", "trash")
        self.accept_button.setEnabled(False)
        self.accept_button.clicked.connect(self.accept)
        close = self.add_footer_button(self.strings["close"])
        close.clicked.connect(self.reject)

    @staticmethod
    def location(candidate) -> str:
        return candidate.path

    def describe(self, candidate, confidence: str, reason: str) -> tuple[str, str]:
        """Texte et infobulle de la case à cocher.

        Checkbox text and tooltip.
        """
        raise NotImplementedError

    def update_selection(self) -> None:
        self.selected = [candidate for checkbox, candidate in self.checks if checkbox.isChecked()]
        self.accept_button.setEnabled(bool(self.selected))


class ResidueDialog(CandidateDialog):
    TITLE_KEY, FOUND_KEY, WARNING_KEY, ACCEPT_KEY = "scan_title", "scan_found", "registry_warning", "delete_selected"
    REASON_KEYS = {"display-name": "reason_display", "key-name": "reason_key",
                   "similar-display-name": "reason_similar", "publisher-product": "reason_publisher"}

    @staticmethod
    def location(candidate: RegistryCandidate) -> str:
        return f"{candidate.hive_label}\\{candidate.path}"

    def describe(self, candidate: RegistryCandidate, confidence: str, reason: str) -> tuple[str, str]:
        return f"{confidence}  ·  {self.location(candidate)}", reason


class FileResidueDialog(CandidateDialog):
    TITLE_KEY, FOUND_KEY, WARNING_KEY, ACCEPT_KEY = "file_scan_title", "file_scan_found", "file_scan_warning", "file_recycle"
    SEVERITY = "info"  # la Corbeille permet de restaurer / the Recycle Bin allows restoring
    REASON_KEYS = {"install-location": "file_reason_install", "exact-name": "file_reason_exact", "similar-name": "file_reason_close"}

    def describe(self, candidate: FileCandidate, confidence: str, reason: str) -> tuple[str, str]:
        return f"{confidence} · {reason} · {candidate.path}", candidate.path


class HistoryDialog(FluentDialog):
    MAX_ROWS = 500
    OPERATION_KEYS = {"uninstall": "history_uninstall", "registry": "history_registry", "files": "history_files",
                      "restore_point": "history_restore_point", "restore_create": "history_restore_create"}
    STATUS_KEYS = {"success": "history_success", "partial": "history_partial", "failed": "history_failed"}

    def __init__(self, parent: QWidget | None, dark: bool, lang: str):
        self.strings = strings = STRINGS[lang]
        super().__init__(parent, strings["history_title"], dark)
        self.events = load_operation_history()
        self.resize(900, 560)
        if not self.events:
            empty = QLabel(strings["history_empty"])
            empty.setObjectName("emptyState")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.body.addWidget(empty, 1)
        else:
            shown = self.events[-self.MAX_ROWS:]
            card = QFrame()
            card.setObjectName("card")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(4, 0, 4, 4)
            self.table = FluentTable(5, dark)
            self.table.setRowCount(len(shown))
            self.table.setAccessibleName(strings["history_title"])
            self.table.setHorizontalHeaderLabels([strings[key] for key in (
                "history_time", "history_operation", "name", "history_status", "history_details")])
            self.table.verticalHeader().setDefaultSectionSize(36)
            self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
            for row, event in enumerate(reversed(shown)):
                operation, status = event.get("operation", ""), event.get("status", "")
                values = (
                    event.get("timestamp", ""),
                    strings.get(self.OPERATION_KEYS.get(operation, ""), operation),
                    event.get("app", ""),
                    strings.get(self.STATUS_KEYS.get(status, ""), status),
                    json.dumps(event.get("details", {}), ensure_ascii=False),
                )
                for col, value in enumerate(values):
                    item = QTableWidgetItem(str(value))
                    item.setToolTip(str(value))
                    self.table.setItem(row, col, item)
            self.table.setSortingEnabled(True)
            self.table.sortItems(0, Qt.SortOrder.DescendingOrder)
            card_layout.addWidget(self.table)
            self.body.addWidget(card, 1)
        self.footer.addStretch(1)
        self.export_button = QPushButton(strings["history_export"])
        self.export_button.setIcon(glyph_icon("export", colors(dark)["text"]))
        self.export_button.setIconSize(QSize(16, 16))
        self.export_button.clicked.connect(self.export_history)
        self.export_button.setEnabled(bool(self.events))  # rien à exporter / nothing to export
        close = QPushButton(strings["close"])
        close.clicked.connect(self.accept)
        self.footer.addWidget(self.export_button)
        self.footer.addWidget(close)

    def export_history(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, self.strings["history_export"], "FastSweep-history.json", "JSON (*.json)")
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(self.events, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, self.strings["history_title"], str(exc))
