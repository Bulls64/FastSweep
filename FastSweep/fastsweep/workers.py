"""Threads d'arrière-plan : tâches génériques, estimations de taille, file de désinstallation.

Background threads: generic tasks, size estimates, uninstall queue.
"""
from __future__ import annotations

import os

from PySide6.QtCore import QThread, Signal

from .inventory import directory_size
from .models import AppEntry
from .uninstall import RESTORE_POINT_ERROR_PREFIX, create_restore_point, perform_uninstall


class TaskWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, task):
        super().__init__()
        self.task = task

    def run(self) -> None:
        try:
            self.completed.emit(self.task())
        except Exception as exc:
            self.failed.emit(str(exc) or type(exc).__name__)  # jamais de message vide / never an empty message


class SizeWorker(QThread):
    """Calcule les tailles de dossiers après l'affichage de la liste, une application à la fois, et s'arrête sur demande.

    Compute folder sizes after the list is shown, one app at a time, and stop on request.
    """

    size_ready = Signal(object, object, bool)

    def __init__(self, apps: list[AppEntry]):
        super().__init__()
        self.apps = apps
        self.stop_requested = False

    def run(self) -> None:
        computed: dict[str, tuple[int | None, bool]] = {}
        for app in self.apps:
            if self.stop_requested:
                return
            key = os.path.normcase(app.size_path)
            if key not in computed:
                computed[key] = directory_size(app.size_path, lambda: self.stop_requested)
            if self.stop_requested:
                return
            size, complete = computed[key]
            self.size_ready.emit(app, size, complete)


class UninstallQueueWorker(QThread):
    progress = Signal(int, int, str)
    item_finished = Signal(int, object)
    completed = Signal(bool)

    def __init__(self, apps: list[AppEntry], make_restore: bool, silent: bool = False):
        super().__init__()
        self.apps = apps
        self.make_restore = make_restore
        self.silent = silent
        self.cancel_pending = False

    def run(self) -> None:
        canceled = False
        restored = False
        if self.make_restore:
            # Un seul point de restauration protège toute la file : il est pris avant le premier changement.
            # One restore point protects the whole queue: it is taken before the first change.
            self.progress.emit(0, len(self.apps), "")
            restored, detail = create_restore_point()
            if not restored:
                for index in range(len(self.apps)):
                    self.item_finished.emit(index, {"error": RESTORE_POINT_ERROR_PREFIX + detail})
                self.completed.emit(False)
                return
        for index, app in enumerate(self.apps):
            if self.cancel_pending:
                canceled = True
                for pending_index in range(index, len(self.apps)):
                    self.item_finished.emit(pending_index, {"canceled": True})
                break
            self.progress.emit(index + 1, len(self.apps), app.name)
            try:
                result = perform_uninstall(app, False, self.silent)  # lève NO_UNINSTALLER_ERROR si nécessaire / raises NO_UNINSTALLER_ERROR when needed
                result["restored"] = restored
                self.item_finished.emit(index, {"result": result})
            except Exception as exc:
                self.item_finished.emit(index, {"error": str(exc)})
        self.completed.emit(canceled)
