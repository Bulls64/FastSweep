"""Fenêtre « Mises à jour » et tâche d'arrière-plan réseau.

"Updates" window and the network background task.
"""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QTextCursor, QTextDocument, QTextFormat
from PySide6.QtWidgets import QCheckBox, QLabel, QProgressBar, QTextBrowser, QWidget

from . import __version__, updater
from .dialogs import FluentDialog
from .glyphs import glyph_icon
from .i18n import STRINGS
from .inventory import format_size
from .theme import colors

# Réglages mémorisés (clé → valeur par défaut) ; la vérification au démarrage est active, l'installation sans demande non.
# Remembered settings (key → default); the startup check is on, installing without asking is off.
SETTING_AUTO_CHECK, SETTING_AUTO_INSTALL, SETTING_DISMISSED = "update_auto_check", "update_auto_install", "update_dismissed"


class UpdateTask(QObject):
    """Exécute une fonction réseau dans un thread démon et rend le résultat au thread de l'interface.

    Runs a network function in a daemon thread and hands the result back to the interface thread.
    Un thread démon ne bloque jamais la fermeture de FastSweep, même en plein téléchargement.
    A daemon thread never blocks FastSweep from closing, even in the middle of a download.
    """

    done = Signal(object)
    failed = Signal(str, str)  # clé de texte, détail / text key, detail
    progress = Signal(int, int)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.stop = threading.Event()

    def start(self, function) -> None:
        """``function(progress, stop)`` : ``progress(fait, total)`` et un Event ``stop`` lui sont fournis.

        ``function(progress, stop)``: it is given ``progress(done, total)`` and a ``stop`` Event.
        """
        def emit(signal, *args) -> None:
            try:
                signal.emit(*args)
            except RuntimeError:
                pass  # la fenêtre a été fermée entre-temps / the window was closed meanwhile

        def run() -> None:
            try:
                result = function(lambda done, total: emit(self.progress, done, total), self.stop)
            except updater.UpdateError as exc:
                emit(self.failed, exc.key, exc.detail)
            except Exception as exc:  # jamais de message vide / never an empty message
                emit(self.failed, "update_err_network", str(exc) or type(exc).__name__)
            else:
                emit(self.done, result)

        threading.Thread(target=run, daemon=True, name="FastSweepUpdate").start()


# Espacements compacts des notes de version, en pixels (Qt met par défaut de larges marges autour de chaque bloc).
# Compact spacing for release notes, in pixels (Qt puts wide margins around every block by default).
NOTES_MARGIN, NOTES_INDENT, HEADING_TOP, HEADING_BOTTOM, BLOCK_BOTTOM, ITEM_BOTTOM, AFTER_LIST = 6, 18, 8, 2, 4, 1, 6


def compact_document(document: QTextDocument) -> None:
    """Resserre un document Markdown : marges réduites, titres à peine plus grands que le texte, listes peu retirées.

    Tightens a Markdown document: smaller margins, headings barely bigger than the text, lists barely indented.
    """
    document.setDocumentMargin(NOTES_MARGIN)
    document.setIndentWidth(NOTES_INDENT)
    block, first, after_list = document.begin(), True, False
    while block.isValid():
        cursor = QTextCursor(block)
        layout = block.blockFormat()
        heading = layout.headingLevel() > 0
        in_list = block.textList() is not None
        # Un titre se détache un peu ; un paragraphe qui suit une liste aussi. / A heading stands out a little; so does a paragraph after a list.
        layout.setTopMargin(0 if first else HEADING_TOP if heading else AFTER_LIST if after_list and not in_list else 0)
        layout.setBottomMargin(HEADING_BOTTOM if heading else (ITEM_BOTTOM if block.textList() else BLOCK_BOTTOM))
        cursor.setBlockFormat(layout)
        if heading:  # gras à la taille du texte plutôt qu'un grand titre / bold at text size rather than a big title
            cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
            text_format = cursor.charFormat()
            text_format.setProperty(QTextFormat.Property.FontSizeAdjustment, 0)
            cursor.mergeCharFormat(text_format)
        block, first, after_list = block.next(), False, in_list


def describe_error(strings: dict, key: str, detail: str) -> str:
    """Message d'erreur de mise à jour dans la langue de l'interface.

    Update error message in the interface language.
    """
    return strings.get(key, strings["update_err_network"]).format(detail=detail or "—")


class UpdateDialog(FluentDialog):
    """Cherche une mise à jour, montre ses nouveautés, la télécharge puis rend le fichier prêt à installer dans ``staged``.

    Looks for an update, shows what's new, downloads it, then leaves the file ready to install in ``staged``.
    ``info`` est le résultat d'une recherche déjà faite (au démarrage) ; sinon une recherche part à l'ouverture.
    ``info`` is the result of a search already made (at startup); otherwise a search starts on opening.
    """

    def __init__(self, parent: QWidget | None, dark: bool, lang: str, settings, info: updater.UpdateInfo | None = None):
        self.strings = STRINGS[lang]
        self.lang = lang
        super().__init__(parent, self.strings["update_title"], dark)
        self.settings = settings
        self.info = info
        self.staged: Path | None = None
        self.task: UpdateTask | None = None
        self._on_done = None  # que faire du résultat de la tâche en cours / what to do with the current task's result
        self.downloading = False
        self.setMinimumSize(640, 460)
        strings = self.strings

        self.version_label = QLabel(strings["update_current"].format(version=__version__))
        self.version_label.setObjectName("muted")
        self.body.addWidget(self.version_label)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.body.addWidget(self.status_label)
        self.notes = QTextBrowser()
        self.notes.setOpenLinks(False)  # les notes viennent du réseau : aucun lien n'est suivi / notes come from the network: no link is followed
        self.notes.setAccessibleName(strings["update_notes"])
        self.body.addWidget(self.notes, 1)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.hide()
        self.body.addWidget(self.progress)

        self.auto_check = QCheckBox(strings["update_auto_check"])
        self.auto_check.setChecked(settings.value(SETTING_AUTO_CHECK, True, type=bool))
        self.auto_install = QCheckBox(strings["update_auto_install"])
        self.auto_install.setToolTip(strings["update_auto_install_tip"])
        self.auto_install.setChecked(settings.value(SETTING_AUTO_INSTALL, False, type=bool))
        self.auto_install.setEnabled(self.auto_check.isChecked())
        self.auto_check.toggled.connect(self.save_options)
        self.auto_install.toggled.connect(self.save_options)
        self.body.addWidget(self.auto_check)
        self.body.addWidget(self.auto_install)

        self.action_button = self.add_footer_button("", "primaryButton", "download")
        self.check_button = self.add_footer_button(strings["update_btn_check"])
        close = self.add_footer_button(strings["close"])
        self.action_button.clicked.connect(self.run_action)
        self.check_button.clicked.connect(self.check)
        close.clicked.connect(self.reject)
        self.action_button.setIcon(glyph_icon("download", colors(dark)["onAccent"]))
        if info is None:
            self.check()
        else:
            self.show_info(info)

    def save_options(self) -> None:
        self.settings.setValue(SETTING_AUTO_CHECK, self.auto_check.isChecked())
        self.settings.setValue(SETTING_AUTO_INSTALL, self.auto_install.isChecked())
        self.auto_install.setEnabled(self.auto_check.isChecked())  # l'installation automatique suit la vérification / auto install follows the check

    # --- États / States ----------------------------------------------------------------------------

    def set_busy(self, busy: bool) -> None:
        self.check_button.setEnabled(not busy)
        self.action_button.setEnabled(not busy and self.action_mode() != "")

    def action_mode(self) -> str:
        """Ce que fait le bouton principal : "install", "page" ou "" (rien à faire).

        What the main button does: "install", "page" or "" (nothing to do).
        """
        info = self.info
        if self.downloading:
            return "cancel"
        if info is None or not info.newer:
            return ""
        if info.installable and not updater.self_update_problem():
            return "install"
        return "page"

    def show_info(self, info: updater.UpdateInfo) -> None:
        strings = self.strings
        self.info = info
        self.task = None
        lines = []
        if info.newer:
            lines.append(strings["update_available"].format(version=info.version))
            if not info.installable:
                lines.append(strings["update_no_exe"])
            elif updater.self_update_problem():
                lines.append(strings[updater.self_update_problem()])
        else:
            lines.append(strings["update_uptodate"])
        self.status_label.setText("\n".join(lines))
        notes = updater.clean_notes(info.notes, self.lang, info.tag) if info.newer else ""
        if notes:
            self.notes.setMarkdown(notes)
            compact_document(self.notes.document())
        else:
            self.notes.setPlainText(strings["update_notes_none"] if info.newer else "")
        self.notes.setVisible(info.newer)
        self.progress.hide()
        self.refresh_buttons()

    def refresh_buttons(self) -> None:
        strings = self.strings
        mode = self.action_mode()
        self.action_button.setText({"install": strings["update_btn_install"], "page": strings["update_btn_page"],
                                    "cancel": strings["update_btn_cancel"]}.get(mode, strings["update_btn_install"]))
        self.action_button.setVisible(mode != "")
        self.action_button.setEnabled(mode != "")
        self.check_button.setEnabled(not self.downloading)

    def show_error(self, key: str, detail: str) -> None:
        self.task = None
        self.downloading = False
        self.progress.hide()
        self.status_label.setText(describe_error(self.strings, key, detail))
        self.refresh_buttons()

    # --- Actions -----------------------------------------------------------------------------------

    def check(self) -> None:
        self.status_label.setText(self.strings["update_checking"])
        self.notes.hide()
        self.set_busy(True)
        self.start_task(lambda progress, stop: updater.check_latest(), self.show_info)

    def start_task(self, function, on_done) -> None:
        # Des méthodes du dialogue (et non des lambdas) : Qt les exécute dans le thread de l'interface même quand
        # le signal part du thread réseau. L'ancienne tâche est coupée pour que ses résultats tardifs soient ignorés.
        # Dialog methods (not lambdas): Qt runs them in the interface thread even when the signal comes from the
        # network thread. The old task is cut off so that its late results are ignored.
        self.drop_task()
        task = self.task = UpdateTask(self)
        self._on_done = on_done
        task.done.connect(self.on_task_done)
        task.failed.connect(self.show_error)
        task.progress.connect(self.show_progress)
        task.start(function)

    def drop_task(self) -> None:
        task, self.task, self._on_done = self.task, None, None
        if task is not None:
            task.stop.set()
            for signal in (task.done, task.failed, task.progress):
                try:
                    signal.disconnect()
                except (RuntimeError, TypeError):
                    pass

    def on_task_done(self, result) -> None:
        callback, self._on_done = self._on_done, None
        if callback is not None:
            callback(result)

    def run_action(self) -> None:
        mode = self.action_mode()
        if mode == "cancel":
            if self.task is not None:
                self.task.stop.set()  # l'erreur « annulé » arrive ensuite / the "cancelled" error follows
        elif mode == "page":
            QDesktopServices.openUrl(QUrl(self.info.page if self.info else updater.RELEASES_URL))
        elif mode == "install":
            self.download()

    def download(self) -> None:
        info, exe = self.info, updater.current_executable()
        if info is None or exe is None:
            return
        self.downloading = True
        self.progress.setRange(0, 0)  # indéterminée jusqu'au premier octet / indeterminate until the first byte
        self.progress.show()
        self.status_label.setText(self.strings["update_installing"].format(version=info.version))
        self.refresh_buttons()
        target = updater.staged_path(exe)
        self.start_task(lambda progress, stop: updater.download(info, target, progress, stop), self.downloaded)

    def show_progress(self, done: int, total: int) -> None:
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(min(done, total))
        self.status_label.setText(self.strings["update_downloading"].format(
            done=format_size(done, self.lang), total=format_size(total, self.lang) if total else "—"))

    def downloaded(self, path: Path) -> None:
        self.downloading = False
        self.task = None
        self.staged = path
        self.accept()  # l'installation et le redémarrage sont gérés par la fenêtre principale / the main window handles install and restart

    def reject(self) -> None:
        self.drop_task()  # fermer interrompt un téléchargement en cours ; ses résultats tardifs sont ignorés / closing interrupts a running download; its late results are ignored
        super().reject()
