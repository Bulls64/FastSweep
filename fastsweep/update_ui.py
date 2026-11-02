"""Fenêtre « Mises à jour » et tâche d'arrière-plan réseau.

"Updates" window and the network background task.
"""
from __future__ import annotations

import threading
import warnings
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
# Fenêtre « Nouveautés » : affichée au premier démarrage d'une nouvelle version (désactivable), version déjà vue.
# "What's new" window: shown on the first start of a new version (can be turned off), version already seen.
SETTING_WHATS_NEW, SETTING_WHATS_NEW_SEEN = "whats_new_show", "whats_new_seen"


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

    def cancel(self) -> None:
        """Interrompt la tâche (téléchargement) et ignore ses résultats tardifs ; le thread démon finit seul.

        Interrupts the task (download) and ignores its late results; the daemon thread ends by itself.
        """
        self.stop.set()
        with warnings.catch_warnings():  # PySide avertit pour un signal sans connexion / PySide warns for a signal with no connection
            warnings.simplefilter("ignore", RuntimeWarning)
            for signal in (self.done, self.failed, self.progress):
                try:
                    signal.disconnect()
                except (RuntimeError, TypeError):
                    pass  # rien de connecté / nothing connected


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


def show_notes(browser: QTextBrowser, info: updater.UpdateInfo, lang: str, empty_text: str) -> bool:
    """Affiche les notes d'une release, nettoyées et compactes ; ``empty_text`` s'il n'y en a pas. Vrai si notes.

    Shows a release's notes, cleaned up and compact; ``empty_text`` when there are none. True if notes.
    """
    notes = updater.clean_notes(info.notes, lang, info.tag)
    if notes:
        browser.setMarkdown(notes)
        compact_document(browser.document())
    else:
        browser.setPlainText(empty_text)
    return bool(notes)


def store_update_options(settings, auto_check: QCheckBox, auto_install: QCheckBox) -> None:
    """Mémorise les deux options de mise à jour ; l'installation automatique suit la vérification.

    Remembers both update options; automatic install follows the check.
    """
    settings.setValue(SETTING_AUTO_CHECK, auto_check.isChecked())
    settings.setValue(SETTING_AUTO_INSTALL, auto_install.isChecked())
    auto_install.setEnabled(auto_check.isChecked())


def open_release_page(info: updater.UpdateInfo | None) -> None:
    """Page GitHub de la release (adresse déjà vérifiée par check_latest), sinon la liste des releases.

    GitHub page of the release (address already checked by check_latest), otherwise the list of releases.
    """
    QDesktopServices.openUrl(QUrl(info.page if info else updater.RELEASES_URL))


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
        store_update_options(self.settings, self.auto_check, self.auto_install)

    # --- États / States ----------------------------------------------------------------------------

    def set_busy(self, busy: bool) -> None:
        self.check_button.setEnabled(not busy)
        self.action_button.setEnabled(not busy and self.action_mode() != "")

    def action_mode(self) -> str:
        """Ce que fait le bouton principal : "install", "page" ou "" (rien à faire).

        What the main button does: "install", "page", "cancel" (download running) or "" (nothing to do).
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
            problem = "update_no_exe" if not info.installable else updater.self_update_problem()
            if problem:
                lines.append(strings[problem])
        else:
            lines.append(strings["update_uptodate"])
        self.status_label.setText("\n".join(lines))
        if info.newer:
            show_notes(self.notes, info, self.lang, strings["update_notes_none"])
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
            task.cancel()

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
            open_release_page(self.info)
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


class WhatsNewDialog(FluentDialog):
    """« Nouveautés de la vX » : notes de la dernière release publiée sur GitHub.

    "What's new in vX": notes of the latest release published on GitHub.
    ``info`` déjà connu (démarrage) ou cherché à l'ouverture. ``wants_update`` est vrai si l'utilisateur a demandé
    à voir la mise à jour (release plus récente que la version installée).
    ``info`` already known (startup) or looked up on opening. ``wants_update`` is true if the user asked to see
    the update (release newer than the installed version).
    """

    def __init__(self, parent: QWidget | None, dark: bool, lang: str, settings, info: updater.UpdateInfo | None = None):
        self.strings = strings = STRINGS[lang]
        self.lang = lang
        super().__init__(parent, strings["whats_new_title_plain"], dark)
        self.heading = self.body.itemAt(0).widget()  # titre du dialogue, complété avec la version / dialog title, completed with the version
        self.settings = settings
        self.info: updater.UpdateInfo | None = None
        self.task: UpdateTask | None = None
        self.wants_update = False
        self.setMinimumSize(620, 460)
        self.date_label = QLabel()
        self.date_label.setObjectName("muted")
        self.body.addWidget(self.date_label)
        self.notice = QLabel()  # release pas encore installée / release not installed yet
        self.notice.setWordWrap(True)
        self.notice.hide()
        self.body.addWidget(self.notice)
        self.notes = QTextBrowser()
        self.notes.setOpenLinks(False)  # notes venues du réseau : aucun lien suivi / network notes: no link followed
        self.notes.setAccessibleName(strings["whats_new_title_plain"])
        self.body.addWidget(self.notes, 1)
        self.auto_show = QCheckBox(strings["whats_new_auto"])
        self.auto_show.setChecked(settings.value(SETTING_WHATS_NEW, True, type=bool))
        self.auto_show.toggled.connect(lambda checked: settings.setValue(SETTING_WHATS_NEW, checked))
        self.body.addWidget(self.auto_show)
        self.update_button = self.add_footer_button(strings["whats_new_update"], "primaryButton", "download")
        self.update_button.setIcon(glyph_icon("download", colors(dark)["onAccent"]))
        self.update_button.hide()
        self.github_button = self.add_footer_button(strings["whats_new_github"], icon="export")
        close = self.add_footer_button(strings["close"])
        self.update_button.clicked.connect(self.see_update)
        self.github_button.clicked.connect(self.open_page)
        close.clicked.connect(self.reject)
        if info is None:
            self.load()
        else:
            self.show_info(info)

    def load(self) -> None:
        self.github_button.setEnabled(False)
        self.notes.setPlainText(self.strings["whats_new_loading"])
        task = self.task = UpdateTask(self)
        task.done.connect(self.show_info)
        task.failed.connect(self.show_error)
        task.start(lambda progress, stop: updater.check_latest())

    def show_info(self, info: updater.UpdateInfo) -> None:
        strings = self.strings
        self.task = None
        self.info = info
        title = strings["whats_new_title"].format(version=info.version)
        self.setWindowTitle(title)
        self.heading.setText(title)
        self.date_label.setText(strings["whats_new_published"].format(date=info.published_date) if info.published_date else "")
        self.date_label.setVisible(bool(info.published_date))
        show_notes(self.notes, info, self.lang, strings["update_notes_none"])
        self.notice.setText(strings["whats_new_not_installed"].format(current=__version__))
        self.notice.setVisible(info.newer)
        self.update_button.setVisible(info.newer)
        self.github_button.setEnabled(True)

    def show_error(self, key: str, detail: str) -> None:
        self.task = None
        self.notes.setPlainText(describe_error(self.strings, key, detail))
        self.github_button.setEnabled(True)  # la page des releases reste accessible / the releases page stays reachable

    def open_page(self) -> None:
        open_release_page(self.info)

    def see_update(self) -> None:
        self.wants_update = True
        self.accept()

    def reject(self) -> None:
        if self.task is not None:
            self.task.cancel()  # résultat tardif ignoré / late result ignored
            self.task = None
        super().reject()
