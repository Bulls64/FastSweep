import base64
import faulthandler
import hashlib
import io
import threading
import urllib.error
from datetime import datetime, timedelta, timezone
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from fastsweep import backdrop, cleanup, dialogs, glyphs, history, i18n, icons, inventory, models, system, theme, uninstall, widgets, workers
from fastsweep import about, restore_page, restore_points, signing, update_ui, updater
from fastsweep import window as main_window


class HelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])
        # Réglages dans un dossier jetable : un test ne doit ni lire ni modifier ceux de l'utilisateur.
        # Settings in a throwaway folder: a test must neither read nor change the user's own.
        # Sous Windows, les réglages natifs sont dans le registre (setPath n'y change rien) : sans cela, les tests
        # liraient et effaceraient les vrais réglages de l'utilisateur. On impose donc un fichier INI jetable,
        # et un %LOCALAPPDATA% jetable pour l'historique et les sauvegardes.
        # On Windows, native settings live in the registry (setPath has no effect there): without this, the tests
        # would read and wipe the user's real settings. So force a throwaway INI file, and a throwaway
        # %LOCALAPPDATA% for the history and backups.
        # Garde-fou : un test qui se bloque affiche la pile de tous les threads puis arrête le processus, au lieu
        # de laisser un build (ou la CI) tourner indéfiniment sans rien dire.
        # Safety net: a test that hangs prints every thread's stack then stops the process, instead of
        # letting a build (or the CI) run forever without a word.
        faulthandler.dump_traceback_later(300, exit=True)
        from PySide6.QtCore import QSettings
        cls.settings_dir = tempfile.TemporaryDirectory()
        cls.data_dir = tempfile.TemporaryDirectory()
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, cls.settings_dir.name)
        ini = lambda: QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                                main_window.SETTINGS_ORG, main_window.SETTINGS_APP)
        cls.class_patches = [patch.object(main_window, "open_settings", ini),
                             patch.dict(os.environ, {"LOCALAPPDATA": cls.data_dir.name})]
        for item in cls.class_patches:
            item.start()

    @classmethod
    def tearDownClass(cls):
        faulthandler.cancel_dump_traceback_later()
        for item in reversed(cls.class_patches):
            item.stop()
        cls.settings_dir.cleanup()
        cls.data_dir.cleanup()

    def setUp(self):
        settings = main_window.open_settings()
        settings.clear()  # chaque test part des réglages par défaut / each test starts from default settings
        settings.sync()

    def test_normalize_name_ignores_punctuation_and_case(self):
        self.assertEqual(cleanup.normalize_name("Acme Tools: Pro!"), "acmetoolspro")

    def test_display_icon_path_removes_quotes_and_icon_index(self):
        self.assertEqual(
            inventory.display_icon_path('"C:\\Program Files\\Acme\\acme.exe",0'),
            "C:\\Program Files\\Acme\\acme.exe",
        )

    def test_display_icon_path_keeps_unquoted_executable_with_spaces(self):
        self.assertEqual(
            inventory.display_icon_path("C:\\Program Files\\Acme\\acme.exe,1"),
            "C:\\Program Files\\Acme\\acme.exe",
        )

    def test_windows_command_line_parser_preserves_quoted_arguments(self):
        self.assertEqual(
            uninstall.windows_argv('"C:\\Program Files\\Acme\\setup.exe" /quiet /log "C:\\Temp Files\\setup.log"'),
            ["C:\\Program Files\\Acme\\setup.exe", "/quiet", "/log", "C:\\Temp Files\\setup.log"],
        )

    def test_process_watcher_waits_for_uninstaller_child(self):
        child_script = "import time; time.sleep(0.65)"
        parent_script = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child_script!r}]); time.sleep(0.2)"
        process = subprocess.Popen([sys.executable, "-c", parent_script])
        started = time.monotonic()
        exit_code = uninstall.wait_for_process_tree(process, interval=0.05)
        self.assertEqual(exit_code, 0)
        self.assertGreaterEqual(time.monotonic() - started, 0.5)

    def test_process_watcher_catches_a_child_started_just_before_the_parent_ends(self):
        # Parent 100 : le 1er parcours ne voit pas encore l'enfant 200, puis le parent se termine.
        # Parent 100: the 1st scan does not see child 200 yet, then the parent ends.
        scans = iter([{100: 1}, {100: 1, 200: 100}])
        child_checks = []
        def still_running(pid, started):
            if pid == 200:
                child_checks.append(started)
                return len(child_checks) < 3  # l'enfant tourne encore un peu / the child runs a little longer
            return False
        process = type("Process", (), {"pid": 100, "wait": lambda self: 0})()
        with patch.object(uninstall, "_parent_map", side_effect=lambda: next(scans, {100: 1, 200: 100})), \
                patch.object(uninstall, "_create_time", side_effect=lambda pid: {100: 10.0, 200: 10.1}[pid]), \
                patch.object(uninstall, "_still_running", side_effect=still_running), \
                patch.object(uninstall.time, "sleep"):
            self.assertEqual(uninstall.wait_for_process_tree(process, interval=0), 0)
        self.assertGreaterEqual(len(child_checks), 3)  # attendu jusqu'à la fin de l'enfant / waited until the child ended

    def test_process_watcher_ignores_children_of_a_reused_process_id(self):
        # Le parent 100 se termine ; un processus étranger prend ensuite le numéro 100 et lance 300.
        # Parent 100 ends; an unrelated process then takes id 100 and starts 300.
        scans = iter([{100: 1}, {100: 1, 300: 100}])
        sleeps = []
        def sleep(_seconds):
            sleeps.append(1)
            if len(sleeps) > 5:
                raise AssertionError("le suivi attend un processus qui n'est pas le sien / waits for a process that is not its own")
        process = type("Process", (), {"pid": 100, "wait": lambda self: 0})()
        with patch.object(uninstall, "_parent_map", side_effect=lambda: next(scans, {100: 1, 300: 100})), \
                patch.object(uninstall, "_create_time", side_effect=lambda pid: {100: 10.0, 300: time.time() + 1000}[pid]), \
                patch.object(uninstall, "_still_running", side_effect=lambda pid, started: pid == 300), \
                patch.object(uninstall.time, "sleep", sleep):
            self.assertEqual(uninstall.wait_for_process_tree(process, interval=0), 0)

    def test_parent_map_falls_back_to_the_public_api(self):
        fake = [type("P", (), {"info": {"pid": 7, "ppid": 3}})()]
        with patch.object(uninstall.psutil, "_psplatform", None), \
                patch.object(uninstall.psutil, "process_iter", return_value=fake):
            self.assertEqual(uninstall._parent_map(), {7: 3})
        self.assertIn(os.getpid(), uninstall._parent_map())  # vrai instantané / real snapshot

    def test_file_scan_finds_exact_and_publisher_nested_residue(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local = root / "LocalAppData"
            (local / "Acme Cleaner").mkdir(parents=True)
            (local / "Acme Publisher" / "Acme Cleaner").mkdir(parents=True)
            (local / "Unrelated").mkdir()
            env = {
                "LOCALAPPDATA": str(local), "APPDATA": "", "PROGRAMDATA": "",
                "PROGRAMFILES": "", "PROGRAMFILES(X86)": "",
            }
            app = models.AppEntry(name="Acme Cleaner", publisher="Acme Publisher")
            with patch.dict(os.environ, env):
                candidates = cleanup.file_candidates(app)
            paths = {Path(candidate.path) for candidate in candidates}
            self.assertIn((local / "Acme Cleaner").resolve(), paths)
            self.assertIn((local / "Acme Publisher" / "Acme Cleaner").resolve(), paths)
            self.assertNotIn((local / "Unrelated").resolve(), paths)

    def test_operation_history_round_trips_locally(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "history.json"
            with patch.object(history, "operation_history_path", return_value=path):
                history.record_operation("Example", "uninstall", "success", {"exit_code": 0})
                events = history.load_operation_history()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["app"], "Example")
            self.assertEqual(events[0]["details"]["exit_code"], 0)

    def test_file_residue_dialog_requires_explicit_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            candidate = models.FileCandidate(str(Path(temporary)), "high", "exact-name")
            dialog = dialogs.FileResidueDialog(None, [candidate], True, "fr")
            self.assertFalse(dialog.accept_button.isEnabled())
            dialog.checks[0][0].setChecked(True)
            self.assertTrue(dialog.accept_button.isEnabled())
            self.assertEqual(dialog.selected, [candidate])
            dialog.close()

    def test_history_dialog_renders_saved_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "history.json"
            with patch.object(history, "operation_history_path", return_value=path):
                history.record_operation("Example", "files", "success", {"moved_to_recycle_bin": ["C:/Example"]})
                dialog = dialogs.HistoryDialog(None, True, "en")
                self.assertEqual(dialog.table.rowCount(), 1)
                self.assertEqual(dialog.table.item(0, 2).text(), "Example")
                dialog.close()

    def test_format_date_reads_registry_install_date(self):
        self.assertEqual(inventory.format_date("20260409"), "09/04/2026")
        self.assertEqual(inventory.format_date("unknown"), "unknown")

    def test_size_format_uses_localized_binary_units(self):
        self.assertEqual(inventory.format_size(1024), "1,0 Ko")
        self.assertEqual(inventory.format_size(1024 ** 2), "1,0 Mo")
        self.assertEqual(inventory.format_size(1024 ** 3), "1,0 Go")
        self.assertEqual(inventory.format_size(1024 ** 3, "en"), "1.0 GB")
        self.assertTrue(inventory.format_size(1024 ** 2, partial=True).startswith("≈ "))

    def test_directory_size_sums_files_without_following_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "first.bin").write_bytes(b"x" * 1024)
            nested = root / "nested"
            nested.mkdir()
            (nested / "second.bin").write_bytes(b"y" * 2048)
            size, complete = inventory.directory_size(str(root))
            self.assertEqual(size, 3072)
            self.assertTrue(complete)

    def test_store_logo_path_uses_manifest_logo(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "Assets"
            assets.mkdir()
            logo = assets / "StoreLogo.png"
            logo.write_bytes(b"image fixture")
            (root / "AppxManifest.xml").write_text(
                "<Package><Properties><Logo>Assets\\StoreLogo.png</Logo></Properties></Package>",
                encoding="utf-8",
            )
            self.assertEqual(inventory.store_logo_path(str(root)), str(logo.resolve()))

    def test_store_logo_path_falls_back_to_asset_variant(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "Assets"
            assets.mkdir()
            logo = assets / "Square44x44Logo.scale-200.png"
            logo.write_bytes(b"image fixture")
            self.assertEqual(inventory.store_logo_path(str(root)), str(logo.resolve()))

    def test_store_application_icon_loads_png_artwork(self):
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/cWQAAAAASUVORK5CYII=")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Assets").mkdir()
            (root / "Assets" / "StoreLogo.png").write_bytes(png)
            (root / "AppxManifest.xml").write_text(
                "<Package><Properties><Logo>Assets\\StoreLogo.png</Logo></Properties></Package>",
                encoding="utf-8",
            )
            icon = icons.application_icon(models.AppEntry(name="Test Store App", source="store", install_location=str(root)))
            self.assertFalse(icon.isNull())
            cached = icons.application_icon(models.AppEntry(name="Another app object", source="store", install_location=str(root)))
            self.assertIs(icon, cached)

    def test_window_loads_and_filters_both_app_sources(self):
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/cWQAAAAASUVORK5CYII=")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Assets").mkdir()
            (root / "Assets" / "StoreLogo.png").write_bytes(png)
            (root / "AppxManifest.xml").write_text(
                "<Package><Properties><Logo>Assets\\StoreLogo.png</Logo></Properties></Package>", encoding="utf-8"
            )
            classic = [
                models.AppEntry(name="Classic sample", publisher="Example", uninstall="uninstall.exe"),
                models.AppEntry(name="Another sample", publisher="Other publisher", uninstall="uninstall.exe"),
            ]
            store = [models.AppEntry(name="Store sample", publisher="Example Store", source="store", install_location=str(root))]
            with patch.object(main_window, "read_classic_apps", return_value=classic), patch.object(main_window, "read_store_apps", return_value=(store, "")):
                window = main_window.MainWindow()
                window.show()
                deadline = time.monotonic() + 5
                while (window._workers or window._size_workers) and time.monotonic() < deadline:
                    self.qt_app.processEvents()
                    QTest.qWait(20)
                self.assertFalse(window._workers, "inventory worker did not finish")
                self.assertFalse(window._size_workers, "size worker did not finish")
                self.assertEqual(window.table.rowCount(), 2)
                window.publisher_filter.setCurrentIndex(window.publisher_filter.findData("Example"))
                self.assertEqual(window.table.rowCount(), 1)
                self.assertEqual(window.table.item(0, main_window.COL_NAME).text(), "Classic sample")
                window.publisher_filter.setCurrentIndex(0)
                window.switch_mode("store")
                self.assertEqual(window.table.rowCount(), 1)
                self.assertEqual(window.table.item(0, main_window.COL_NAME).text(), "Store sample")
                self.assertEqual(window.table.columnCount(), 6)
                self.assertNotEqual(window.table.item(0, main_window.COL_SIZE).text(), "—")
                self.assertFalse(window.table.item(0, main_window.COL_NAME).icon().isNull())
                deadline = time.monotonic() + 2
                while window._workers and time.monotonic() < deadline:
                    self.qt_app.processEvents()
                    QTest.qWait(20)
                window.close()

    # --- Point 1 : noms non latins et entrées sans DisplayName -------------------------------
    # --- Point 1: non-Latin names and entries without DisplayName ----------------------------

    def test_normalize_name_keeps_non_latin_letters_and_strips_latin_accents(self):
        self.assertEqual(cleanup.normalize_name("微信"), "微信")
        self.assertEqual(cleanup.normalize_name("Яндекс Браузер"), "яндексбраузер")
        self.assertEqual(cleanup.normalize_name("Éditeur ©"), "editeur")
        self.assertEqual(cleanup.normalize_name("が"), "が")
        self.assertNotEqual(cleanup.normalize_name("が"), cleanup.normalize_name("か"))
        self.assertEqual(cleanup.normalize_name("™ © _ -"), "")
        self.assertEqual(cleanup.normalize_name("Acme™ Tools®"), "acmetools")

    def test_registry_scan_ignores_names_without_letters_or_digits(self):
        with FakeRegistry({}) as registry:
            self.assertEqual(cleanup.registry_candidates(models.AppEntry(name="™ ©")), [])
            self.assertEqual(registry.opened, [])

    def test_registry_scan_does_not_match_entries_without_display_name(self):
        uninstall_root = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
        keys = {
            "HKLM\\" + uninstall_root: {},
            "HKLM\\" + uninstall_root + r"\{11111111-AAAA}": {},
            "HKLM\\" + uninstall_root + r"\{22222222-BBBB}": {"DisplayName": ""},
            "HKLM\\" + uninstall_root + r"\WeChat": {"DisplayName": "微信"},
            "HKLM\\" + uninstall_root + r"\Other": {"DisplayName": "Яндекс"},
        }
        with FakeRegistry(keys):
            candidates = cleanup.registry_candidates(models.AppEntry(name="微信"))
        self.assertEqual(
            [(c.hive_label, c.path, c.confidence) for c in candidates],
            [("HKLM", uninstall_root + r"\WeChat", "high")],
        )

    # --- Point 2 : repli UAC quand le désinstalleur exige l'élévation ----------------------------
    # --- Point 2: UAC fallback when the uninstaller requires elevation ---------------------------

    def test_launcher_falls_back_to_uac_when_elevation_is_required(self):
        error = OSError(22, "The requested operation requires elevation")
        error.winerror = uninstall.ERROR_ELEVATION_REQUIRED
        elevated = object()
        with patch.object(uninstall.subprocess, "Popen", side_effect=error), \
                patch.object(uninstall, "shell_execute_elevated", return_value=elevated) as runas:
            process = uninstall.launch_uninstaller('"C:\\Program Files\\Acme\\unins000.exe" /SILENT /LOG="C:\\Temp Dir\\u.log"')
        self.assertIs(process, elevated)
        runas.assert_called_once_with("C:\\Program Files\\Acme\\unins000.exe", '/SILENT /LOG="C:\\Temp Dir\\u.log"')

    def test_launcher_does_not_elevate_for_other_errors(self):
        error = OSError(2, "File not found")
        error.winerror = 2
        with patch.object(uninstall.subprocess, "Popen", side_effect=error), \
                patch.object(uninstall, "shell_execute_elevated") as runas:
            with self.assertRaises(OSError):
                uninstall.launch_uninstaller('"C:\\Missing\\uninstall.exe" /S')
        runas.assert_not_called()

    def test_elevation_cancel_message_is_localized(self):
        error = uninstall.ELEVATION_CANCELLED_PREFIX + "unins000.exe"
        self.assertIn("unins000.exe", uninstall.describe_uninstall_error(error, "en"))
        self.assertIn("UAC", uninstall.describe_uninstall_error(error, "fr"))
        self.assertEqual(uninstall.describe_uninstall_error("other", "fr"), "other")

    def test_process_watcher_does_not_track_pid_zero(self):
        class NoPidProcess:
            pid = 0

            def wait(self):
                return 5

        self.assertEqual(uninstall.wait_for_process_tree(NoPidProcess(), interval=0.01), 5)

    # --- Point 3 : point de restauration réellement créé, un seul par file -------------------
    # --- Point 3: restore point really created, a single one per queue -----------------------

    def test_restore_point_requires_confirmation_marker(self):
        self.assertEqual(uninstall.restore_point_outcome(0, "FASTSWEEP_RESTORE_POINT_CREATED\n", ""), (True, ""))
        created, detail = uninstall.restore_point_outcome(0, "", "A new restore point cannot be created in the last 1440 minutes")
        self.assertFalse(created)
        self.assertIn("1440", detail)
        self.assertFalse(uninstall.restore_point_outcome(2, "", "Windows did not create a restore point.")[0])

    def run_queue(self, apps, restore_result):
        outcomes = {}
        worker = workers.UninstallQueueWorker(apps, True)
        worker.item_finished.connect(lambda index, outcome: outcomes.__setitem__(index, outcome))
        with patch.object(workers, "create_restore_point", return_value=restore_result) as restore, \
                patch.object(workers, "perform_uninstall", return_value={"restored": False, "source": "classic", "exit_code": 0}) as uninstall_mock:
            worker.run()
        return outcomes, restore, uninstall_mock

    def test_queue_creates_one_restore_point_for_all_apps(self):
        apps = [models.AppEntry(name=f"App {n}", uninstall="u.exe") for n in range(3)]
        outcomes, restore, uninstall_mock = self.run_queue(apps, (True, ""))
        restore.assert_called_once()
        self.assertEqual([call.args[1] for call in uninstall_mock.call_args_list], [False, False, False])
        self.assertTrue(all(outcomes[n]["result"]["restored"] for n in range(3)))

    def test_queue_stops_before_any_uninstall_when_restore_point_fails(self):
        apps = [models.AppEntry(name=f"App {n}", uninstall="u.exe") for n in range(2)]
        outcomes, _restore, uninstall_mock = self.run_queue(apps, (False, "System Protection is off"))
        uninstall_mock.assert_not_called()
        for n in range(2):
            self.assertTrue(outcomes[n]["error"].startswith(uninstall.RESTORE_POINT_ERROR_PREFIX))
        message = uninstall.describe_uninstall_error(outcomes[0]["error"], "en")
        self.assertIn("System Protection is off", message)

    def test_error_markers_are_translated(self):
        self.assertEqual(uninstall.describe_uninstall_error(uninstall.NO_UNINSTALLER_ERROR, "en"), i18n.STRINGS["en"]["no_uninstaller"])
        self.assertIn(i18n.STRINGS["en"]["ask_restore"], uninstall.describe_uninstall_error(uninstall.RESTORE_POINT_ERROR_PREFIX, "en"))

    def test_both_languages_define_the_same_texts(self):
        self.assertEqual(set(i18n.STRINGS["fr"]), set(i18n.STRINGS["en"]))

    # --- Point 4 : fermer la file annule au lieu de figer l'interface -------------------------
    # --- Point 4: closing the queue cancels instead of freezing the interface -----------------

    def test_closing_the_queue_while_running_cancels_pending_steps(self):
        from PySide6.QtWidgets import QDialog, QWidget
        parent = QWidget()
        dialog = dialogs.UninstallQueueDialog(parent, [models.AppEntry(name="A"), models.AppEntry(name="B")], False, True, "fr")
        dialog.show()
        with patch.object(dialog.worker, "isRunning", return_value=True):
            dialog.reject()
            self.assertTrue(dialog.isVisible())
            self.assertTrue(dialog.worker.cancel_pending)
            self.assertFalse(dialog.cancel_button.isEnabled())
        dialog.reject()
        self.assertFalse(dialog.isVisible())
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        parent.deleteLater()

    # --- Point 5 : tailles recalculées et calculées après l'affichage de la liste -------------
    # --- Point 5: sizes recomputed, and computed after the list is shown ----------------------

    def test_directory_size_is_recomputed_after_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.bin").write_bytes(b"x" * 1000)
            self.assertEqual(inventory.directory_size(str(root)), (1000, True))
            (root / "b.bin").write_bytes(b"y" * 500)
            self.assertEqual(inventory.directory_size(str(root)), (1500, True))

    def test_directory_size_can_be_stopped(self):
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / "a.bin").write_bytes(b"x")
            self.assertEqual(inventory.directory_size(temporary, lambda: True), (None, False))

    # --- Point 6 : noms Store, dossiers supplémentaires ---------------------------------------
    # --- Point 6: Store names, additional folders ---------------------------------------------

    def write_manifest(self, root: Path, display: str, publisher: str) -> None:
        (root / "AppxManifest.xml").write_text(
            '<Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"><Properties>'
            f"<DisplayName>{display}</DisplayName><PublisherDisplayName>{publisher}</PublisherDisplayName>"
            "</Properties></Package>", encoding="utf-8")

    def test_store_names_come_from_the_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.write_manifest(Path(temporary), "Calculatrice", "Microsoft Corporation")
            self.assertEqual(
                inventory.store_display_info(temporary, "Microsoft.WindowsCalculator_1_x64__8wekyb3d8bbwe", "Microsoft.WindowsCalculator"),
                ("Calculatrice", "Microsoft Corporation"),
            )

    def test_store_resource_names_are_resolved_or_left_empty(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.write_manifest(Path(temporary), "ms-resource:AppStoreName", "ms-resource:Publisher")
            seen = []

            def fake_load(source):
                seen.append(source)
                return "Calculatrice" if source.endswith("/Resources/AppStoreName}") else ""

            with patch.object(inventory, "load_indirect_string", side_effect=fake_load):
                result = inventory.store_display_info(temporary, "Pkg_1_x64__abc", "Pkg")
            self.assertEqual(result, ("Calculatrice", ""))
            self.assertIn("@{Pkg_1_x64__abc? ms-resource://Pkg/Resources/AppStoreName}", seen)

    def test_file_scan_includes_per_user_programs_and_start_menu(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local, roaming = root / "Local", root / "Roaming"
            (local / "Programs" / "Acme Cleaner").mkdir(parents=True)
            menu = roaming / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            menu.mkdir(parents=True)
            (menu / "Acme Cleaner.lnk").write_bytes(b"shortcut")
            env = {"LOCALAPPDATA": str(local), "APPDATA": str(roaming), "PROGRAMDATA": "",
                   "PROGRAMFILES": "", "PROGRAMFILES(X86)": ""}
            with patch.dict(os.environ, env):
                paths = {Path(c.path) for c in cleanup.file_candidates(models.AppEntry(name="Acme Cleaner"))}
            self.assertIn((local / "Programs" / "Acme Cleaner").resolve(), paths)
            self.assertIn((menu / "Acme Cleaner.lnk").resolve(), paths)

    # --- Code factorisé ------------------------------------------------------------------------
    # --- Shared (factored-out) code ------------------------------------------------------------

    def launched_args(self, command):
        with patch.object(uninstall.subprocess, "Popen", return_value="process") as popen:
            uninstall.launch_uninstaller(command)
        return popen.call_args.args[0]

    def test_msiexec_install_switch_becomes_uninstall_in_every_form(self):
        # Les options de msiexec ne sont pas sensibles à la casse ; /x est ce qu'il reçoit.
        # msiexec switches are case-insensitive; /x is what it receives.
        # Nommé sans dossier : le vrai msiexec de System32, jamais un homonyme. / Named without a folder: System32's.
        msiexec = system.system_tool("MsiExec.exe")
        self.assertEqual(self.launched_args("MsiExec.exe /I{ABC}"), [msiexec, "/x{ABC}"])
        self.assertEqual(self.launched_args("MsiExec /I{ABC}"), [msiexec, "/x{ABC}"])
        self.assertEqual(
            self.launched_args('"C:\\Windows\\System32\\msiexec.exe" /i {ABC} /qb'),
            ["C:\\Windows\\System32\\msiexec.exe", "/x", "{ABC}", "/qb"],
        )
        # Seul msiexec est réécrit : un autre programme garde son option /I.
        # Only msiexec is rewritten: another program keeps its /I switch.
        self.assertEqual(self.launched_args("C:\\Tools\\setup.exe /I"), ["C:\\Tools\\setup.exe", "/I"])

    def test_uninstall_status_treats_restart_codes_as_success(self):
        self.assertEqual([uninstall.uninstall_status(code) for code in (0, 1641, 3010, 1602)],
                         ["success", "success", "success", "partial"])

    def test_candidates_keep_the_strongest_match_and_sort_by_confidence(self):
        found = {}
        models.keep_strongest(found, "a", models.FileCandidate("B", "low", "similar-name"))
        models.keep_strongest(found, "a", models.FileCandidate("B", "high", "exact-name"))
        models.keep_strongest(found, "a", models.FileCandidate("B", "medium", "exact-name"))
        models.keep_strongest(found, "c", models.FileCandidate("a", "medium", "exact-name"))
        self.assertEqual(found["a"].confidence, "high")
        self.assertEqual([c.path for c in models.sort_by_confidence(found.values())], ["B", "a"])

    def test_registry_residue_dialog_lists_keys_unchecked(self):
        candidate = models.RegistryCandidate(0, r"SOFTWARE\Acme", "HKCU", "medium", "key-name")
        dialog = dialogs.ResidueDialog(None, [candidate], False, "en")
        checkbox = dialog.checks[0][0]
        self.assertIn(r"HKCU\SOFTWARE\Acme", checkbox.text())
        self.assertFalse(checkbox.isChecked())
        self.assertFalse(dialog.accept_button.isEnabled())
        dialog.close()

    def test_history_dialog_has_no_empty_rows_beyond_its_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "history.json"
            events = [{"timestamp": f"2026-01-01T00:00:{n % 60:02d}", "app": f"App {n}", "operation": "files",
                       "status": "success", "details": {}} for n in range(dialogs.HistoryDialog.MAX_ROWS + 20)]
            path.write_text(json.dumps(events), encoding="utf-8")
            with patch.object(history, "operation_history_path", return_value=path):
                dialog = dialogs.HistoryDialog(None, True, "en")
            table = dialog.table
            self.assertEqual(table.rowCount(), dialogs.HistoryDialog.MAX_ROWS)
            self.assertTrue(all(table.item(row, 2) is not None for row in range(table.rowCount())))
            dialog.close()

    # --- Interface WinUI 3 : thème, icônes, transparence, sélection ---------------------------------
    # --- WinUI 3 interface: theme, icons, transparency, selection -----------------------------------

    def test_stylesheets_exist_for_every_theme_and_mode(self):
        for dark in (False, True):
            for translucent in (False, True):
                sheet = theme.theme_stylesheet(dark, translucent)
                self.assertIn(theme.colors(dark)["accent"], sheet)
                window = theme.colors(dark, translucent)["window"]
                self.assertIn(f"QMainWindow#mainWindow {{ background: {window}; }}", sheet)
                # Opacité par défaut : fenêtre invisible en Acrylic, pleine sinon. / Default opacity: invisible window in Acrylic, solid otherwise.
                self.assertEqual(theme.qcolor(window).alpha(), 0 if translucent else 255)

    def test_translucent_fills_let_the_backdrop_show_through(self):
        # Calques nettement transparents en mode Acrylic, pleins en mode opaque.
        # Clearly see-through layers in Acrylic mode, solid in opaque mode.
        for dark in (False, True):
            see_through = theme.colors(dark, translucent=True)
            solid = theme.colors(dark, translucent=False)
            for token in ("layer", "card", "ctl", "footer"):
                self.assertLessEqual(theme.qcolor(see_through[token]).alpha(), 128, (dark, token))  # 50 % au plus / at most 50 %
                self.assertEqual(theme.qcolor(solid[token]).alpha(), 255, (dark, token))

    def test_qcolor_reads_hex_and_rgba(self):
        self.assertEqual(theme.qcolor("#0067C0").name(), "#0067c0")
        color = theme.qcolor("rgba(0,0,0,0.5)")
        self.assertEqual((color.red(), color.alpha()), (0, 128))

    def test_glyphs_render_as_icons_and_style_sheet_images(self):
        self.assertFalse(glyphs.glyph_icon("search", "#1A1A1A").isNull())
        path = Path(glyphs.glyph_png("check", "#FFFFFF"))
        self.assertTrue(path.is_file())
        self.assertTrue(path.with_name(path.stem + "@2x.png").is_file())

    def test_backdrop_stays_off_outside_windows_11(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QWidget
        widget = QWidget()
        with patch.object(backdrop, "is_windows", return_value=False):
            self.assertFalse(backdrop.prepare_translucency(widget))
        self.assertFalse(widget.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
        widget.deleteLater()

    def test_selection_delegate_paints_tables_and_lists(self):
        # Une liste n'expose pas columnCount() : ce cas plantait le dessin de la file.
        # A list does not expose columnCount(): this case crashed the queue's painting.
        from PySide6.QtGui import QImage, QPainter
        from PySide6.QtWidgets import QListWidget, QStyle, QStyleOptionViewItem, QTableWidget, QTableWidgetItem
        table = QTableWidget(1, 2)
        table.setItem(0, 0, QTableWidgetItem("a"))
        table.setItem(0, 1, QTableWidgetItem("b"))
        listing = QListWidget()
        listing.addItem("c")
        image = QImage(200, 50, QImage.Format.Format_ARGB32)
        painter = QPainter(image)
        try:
            for view, index in ((table, table.model().index(0, 0)), (table, table.model().index(0, 1)),
                                (listing, listing.model().index(0, 0))):
                delegate = widgets.SelectionDelegate(view, dark=True, row_height=40)
                option = QStyleOptionViewItem()
                option.rect = view.visualRect(index)
                option.state = QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_Enabled
                option.widget = view
                delegate.paint(painter, option, index)
                self.assertEqual(delegate.sizeHint(option, index).height(), 40)
        finally:
            painter.end()

    def test_window_switches_theme_and_keeps_its_icons(self):
        with patch.object(main_window, "read_classic_apps", return_value=[]), \
                patch.object(main_window, "read_store_apps", return_value=([], "")):
            window = main_window.MainWindow()
            before = window.theme_btn.accessibleName()
            self.assertEqual(window.theme_btn.text(), "")  # symbole seul / symbol only
            self.assertEqual(window.theme_btn.toolTip(), before)
            window.toggle_theme()
            self.assertEqual(window.theme_btn.text(), "")
            self.assertNotEqual(window.theme_btn.accessibleName(), before)
            self.assertEqual(window.theme_btn.toolTip(), window.theme_btn.accessibleName())
            for button in (window.classic_nav, window.uninstall_btn, window.sidebar_toggle, window.refresh_btn):
                self.assertFalse(button.icon().isNull())
            window.toggle_theme()
            deadline = time.monotonic() + 5
            while window._workers and time.monotonic() < deadline:
                self.qt_app.processEvents()
                QTest.qWait(20)
            window.close()

    def test_dialogs_put_their_buttons_in_the_footer(self):
        dialog = dialogs.FileResidueDialog(None, [models.FileCandidate("C:/A", "high", "exact-name")], False, "en")
        footer = dialog.findChild(dialogs.QFrame, "dialogFooter")
        self.assertIsNotNone(footer)
        self.assertIs(dialog.accept_button.parent(), footer)
        dialog.close()

    def test_dialog_backdrop_property_follows_translucency_support(self):
        # Le résultat dépend de la machine (Windows 11 : oui, sinon non) : on fixe la réponse de Windows.
        # The result depends on the machine (Windows 11: yes, otherwise no): Windows' answer is fixed here.
        candidate = [models.FileCandidate("C:/A", "high", "exact-name")]
        for supported in (False, True):
            with patch.object(dialogs, "prepare_translucency", return_value=supported):
                dialog = dialogs.FileResidueDialog(None, candidate, False, "en")
            self.assertEqual(bool(dialog.property("backdrop")), supported)
            self.assertEqual(dialog.translucent, supported)
            dialog.close()

    # --- Cases à cocher : sélection multiple pour enchaîner les désinstallations ---------------------
    # --- Checkboxes: multiple selection to chain uninstalls ------------------------------------------

    def checkbox_window(self):
        apps = [models.AppEntry(name=name, publisher="Pub", uninstall="u.exe", hive_label="HKLM",
                                subkey=f"SOFTWARE\\Uninstall\\{name}") for name in ("Alpha", "Bravo", "Charlie")]
        with patch.object(main_window, "read_classic_apps", side_effect=lambda: [models.AppEntry(**vars(a)) for a in apps]), \
                patch.object(main_window, "read_store_apps", return_value=([], "")):
            window = main_window.MainWindow()
            self.wait_for_workers(window)
        return window

    def wait_for_workers(self, window):
        deadline = time.monotonic() + 5
        while (window._workers or window._size_workers) and time.monotonic() < deadline:
            self.qt_app.processEvents()
            QTest.qWait(20)

    def row_of(self, window, name):
        return next(row for row in range(window.table.rowCount())
                    if window.table.item(row, main_window.COL_NAME).text() == name)

    def test_checkbox_column_is_first_and_unchecked(self):
        window = self.checkbox_window()
        self.assertEqual(window.table.rowCount(), 3)
        for row in range(3):
            item = window.table.item(row, main_window.COL_CHECK)
            self.assertTrue(item.flags() & Qt.ItemFlag.ItemIsUserCheckable)
            self.assertEqual(item.checkState(), Qt.CheckState.Unchecked)
        self.assertEqual(window.checked_apps(), [])
        self.assertFalse(window.uninstall_btn.isEnabled())
        window.close()

    def test_checking_apps_updates_counter_button_and_header(self):
        window = self.checkbox_window()
        window.table.item(self.row_of(window, "Alpha"), main_window.COL_CHECK).setCheckState(Qt.CheckState.Checked)
        window.table.item(self.row_of(window, "Charlie"), main_window.COL_CHECK).setCheckState(Qt.CheckState.Checked)
        self.assertEqual([app.name for app in window.checked_apps()], ["Alpha", "Charlie"])
        self.assertIn("2", window.uninstall_btn.text())
        self.assertIn("2", window.count_label.text())
        self.assertTrue(window.uninstall_btn.isEnabled())
        self.assertEqual(window.table.check_header.state, Qt.CheckState.PartiallyChecked)
        window.close()

    def test_header_box_checks_then_unchecks_every_visible_row(self):
        window = self.checkbox_window()
        window.table.check_header.toggled.emit()
        self.assertEqual(len(window.checked_apps()), 3)
        self.assertEqual(window.table.check_header.state, Qt.CheckState.Checked)
        window.table.check_header.toggled.emit()
        self.assertEqual(window.checked_apps(), [])
        self.assertEqual(window.table.check_header.state, Qt.CheckState.Unchecked)
        window.close()

    def test_space_key_checks_the_selected_rows(self):
        window = self.checkbox_window()
        window.table.selectRow(self.row_of(window, "Bravo"))
        QTest.keyClick(window.table, Qt.Key.Key_Space)
        self.assertEqual([app.name for app in window.checked_apps()], ["Bravo"])
        QTest.keyClick(window.table, Qt.Key.Key_Space)
        self.assertEqual(window.checked_apps(), [])
        window.close()

    def test_checks_survive_search_and_refresh(self):
        window = self.checkbox_window()
        window.table.item(self.row_of(window, "Bravo"), main_window.COL_CHECK).setCheckState(Qt.CheckState.Checked)
        window.search.setText("Alpha")
        window.filter_table()
        self.assertEqual(window.table.rowCount(), 1)
        self.assertEqual([app.name for app in window.checked_apps()], ["Bravo"])  # masquée mais cochée / hidden but checked
        window.search.setText("")
        window.filter_table()
        self.assertTrue(window.table.row_is_checked(self.row_of(window, "Bravo")))
        with patch.object(main_window, "read_classic_apps", return_value=[
                models.AppEntry(name="Bravo", hive_label="HKLM", subkey="SOFTWARE\\Uninstall\\Bravo")]), \
                patch.object(main_window, "read_store_apps", return_value=([], "")):
            window.load_apps()
            self.wait_for_workers(window)
        self.assertEqual([app.name for app in window.checked_apps()], ["Bravo"])  # nouvel objet, même clé / new object, same key
        window.close()

    def test_uninstall_uses_the_checked_apps(self):
        window = self.checkbox_window()
        window.table.selectRow(self.row_of(window, "Bravo"))  # sélectionnée mais pas cochée / selected but not checked
        for name in ("Alpha", "Charlie"):
            window.table.item(self.row_of(window, name), main_window.COL_CHECK).setCheckState(Qt.CheckState.Checked)
        prompts = []
        with patch.object(main_window.QMessageBox, "question",
                          side_effect=lambda *args: prompts.append(args[2]) or main_window.QMessageBox.StandardButton.No):
            window.uninstall_selection()
        self.assertEqual(len(prompts), 1)
        self.assertIn("Alpha", prompts[0])
        self.assertIn("Charlie", prompts[0])
        self.assertNotIn("Bravo", prompts[0])
        window.close()

    def test_app_key_is_stable_and_distinguishes_sources(self):
        classic = models.AppEntry(name="A", hive_label="HKCU", subkey="SOFTWARE\\Uninstall\\A")
        self.assertEqual(classic.key, models.AppEntry(name="A", hive_label="HKCU", subkey="SOFTWARE\\Uninstall\\A").key)
        store = models.AppEntry(name="A", source="store", package_full_name="Pkg_1_x64__abc")
        self.assertNotEqual(classic.key, store.key)

    # --- Renommage Clair → FastSweep : reprise des données --------------------------------------------
    # --- Rename Clair → FastSweep: carrying data over -------------------------------------------------

    def test_app_data_dir_uses_the_new_name(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": "C:/Users/x/AppData/Local"}):
            self.assertEqual(system.app_data_dir().name, "FastSweep")
            self.assertEqual(cleanup.registry_backup_root().name, "RegistryBackups")

    def test_legacy_data_is_copied_once_and_never_deleted(self):
        with tempfile.TemporaryDirectory() as temporary:
            local = Path(temporary)
            old = local / "Clair"
            (old / "RegistryBackups" / "cleanup-1").mkdir(parents=True)
            (old / "RegistryBackups" / "cleanup-1" / "01-key.reg").write_text("old backup", encoding="utf-8")
            (old / "history.json").write_text('[{"app": "Old"}]', encoding="utf-8")
            with patch.dict(os.environ, {"LOCALAPPDATA": str(local)}):
                self.assertTrue(system.migrate_legacy_data())
                new = local / "FastSweep"
                self.assertEqual((new / "history.json").read_text(encoding="utf-8"), '[{"app": "Old"}]')
                self.assertTrue((new / "RegistryBackups" / "cleanup-1" / "01-key.reg").is_file())
                self.assertTrue((old / "history.json").is_file())  # l'ancien dossier reste / the old folder stays
                # Deuxième lancement : rien n'est recopié, même si l'ancien dossier a changé.
                # Second launch: nothing is copied again, even if the old folder changed.
                (old / "history.json").write_text("changed", encoding="utf-8")
                self.assertFalse(system.migrate_legacy_data())
                self.assertEqual((new / "history.json").read_text(encoding="utf-8"), '[{"app": "Old"}]')

    def test_migration_never_overwrites_existing_new_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            local = Path(temporary)
            (local / "Clair").mkdir()
            (local / "Clair" / "history.json").write_text("old", encoding="utf-8")
            (local / "FastSweep").mkdir()
            (local / "FastSweep" / "history.json").write_text("new", encoding="utf-8")
            with patch.dict(os.environ, {"LOCALAPPDATA": str(local)}):
                system.migrate_legacy_data()
            self.assertEqual((local / "FastSweep" / "history.json").read_text(encoding="utf-8"), "new")

    def test_migration_without_an_old_folder_does_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"LOCALAPPDATA": temporary}):
                self.assertFalse(system.migrate_legacy_data())
            self.assertFalse((Path(temporary) / "FastSweep").exists())

    def test_settings_carry_over_from_the_old_name(self):
        from PySide6.QtCore import QSettings
        self.addCleanup(QSettings.setPath, QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                        self.settings_dir.name)  # rendre le dossier des tests / give back the tests' folder
        with tempfile.TemporaryDirectory() as temporary:
            QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, temporary)
            old = QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, *main_window.LEGACY_SETTINGS)
            old.setValue("language", "en")
            old.setValue("dark", False)
            old.sync()
            new = QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, "FastSweep", "FastSweep")
            main_window.migrate_legacy_settings(new)
            self.assertEqual(new.value("language"), "en")
            self.assertFalse(new.value("dark", True, type=bool))
            new.setValue("language", "fr")  # un choix déjà fait n'est pas écrasé / an existing choice is not overwritten
            main_window.migrate_legacy_settings(new)
            self.assertEqual(new.value("language"), "fr")

    def test_application_is_named_fastsweep_everywhere(self):
        self.assertEqual(i18n.APP_NAME, "FastSweep")
        for lang in ("fr", "en"):
            self.assertEqual(i18n.STRINGS[lang]["title"], "FastSweep")
            self.assertNotIn("Clair", i18n.STRINGS[lang]["about_description"])

    # --- Scripts de build : format attendu par Windows -------------------------------------------------
    # --- Build scripts: format expected by Windows ------------------------------------------------------

    def test_version_is_the_same_everywhere(self):
        from fastsweep import __version__
        project = Path(__file__).resolve().parents[1]
        info = (project / "version_info.txt").read_text(encoding="utf-8")
        numbers = ", ".join(__version__.split(".")) + ", 0"
        self.assertIn(f"filevers=({numbers})", info)
        self.assertIn(f"prodvers=({numbers})", info)
        self.assertIn(f"StringStruct('FileVersion', '{__version__}')", info)
        self.assertIn(f"StringStruct('ProductVersion', '{__version__}')", info)
        self.assertIn(f"version-{__version__}-", (project / "README.md").read_text(encoding="utf-8"))

    def test_build_scripts_use_windows_line_endings(self):
        # cmd.exe lit mal un .bat en fins de ligne LF (étiquettes et blocs ignorés) : le script s'arrête sans rien faire.
        # cmd.exe misreads a .bat with LF line endings (labels and blocks skipped): the script stops doing nothing.
        project = Path(__file__).resolve().parents[1]
        for name in ("build.bat", "build.ps1"):
            data = (project / name).read_bytes()
            self.assertEqual(data.count(b"\r\n"), data.count(b"\n"), f"{name} doit être en CRLF / must use CRLF")
        self.assertTrue((project / "build.ps1").read_bytes().startswith(b"\xef\xbb\xbf"), "BOM pour PowerShell 5 / BOM for PowerShell 5")


    # --- Points de restauration : lecture, protection du plus récent, suppression ---------------------
    # --- Restore points: reading, protection of the most recent one, deletion -------------------------

    @staticmethod
    def sample_points():
        utc = timezone.utc
        return [restore_points.RestorePoint(12, "Pilote", datetime(2026, 10, 1, 9, 0, tzinfo=utc), 10),
                restore_points.RestorePoint(13, "Windows Update", datetime(2026, 10, 15, 9, 0, tzinfo=utc), 0),
                restore_points.RestorePoint(14, "FastSweep uninstall safety point", datetime(2026, 10, 30, 18, 5, tzinfo=utc), 12)]

    def test_restore_point_dates_are_parsed_with_their_offset(self):
        parse = restore_points.parse_creation_time
        self.assertEqual(parse("20261031143005.123456-000"), datetime(2026, 10, 31, 14, 30, 5, 123456, tzinfo=timezone.utc))
        self.assertEqual(parse("20261031143005.000000+060").utcoffset(), timedelta(minutes=60))
        self.assertEqual(parse("2026-10-31T14:30:05Z"), datetime(2026, 10, 31, 14, 30, 5, tzinfo=timezone.utc))
        self.assertEqual(parse("/Date(0)/"), datetime(1970, 1, 1, tzinfo=timezone.utc))
        for bad in ("", None, "hier", "20261399999999.000000-000"):
            self.assertIsNone(parse(bad))

    def test_restore_point_list_parsing_sorts_newest_first_and_skips_bad_entries(self):
        output = "\ufeff" + json.dumps([
            {"Sequence": 3, "Description": "Ancien", "Type": 0, "Created": "20260101100000.000000-000"},
            {"Sequence": "x", "Description": "Illisible"},
            "pas un objet",
            {"Sequence": 7, "Description": "Récent é", "Type": 12, "Created": "20261030100000.000000-000"},
        ])
        points = restore_points.parse_restore_points(output)
        self.assertEqual([point.sequence for point in points], [7, 3])
        self.assertEqual(points[0].description, "Récent é")
        self.assertEqual(points[0].kind, 12)
        single = restore_points.parse_restore_points(json.dumps({"Sequence": 5, "Description": "Seul"}))
        self.assertEqual([(point.sequence, point.created, point.kind) for point in single], [(5, None, -1)])
        self.assertEqual(restore_points.parse_restore_points(""), [])
        self.assertEqual(restore_points.parse_restore_points("[]"), [])

    def test_list_restore_points_reports_powershell_errors(self):
        failed = subprocess.CompletedProcess([], 1, "", "Accès refusé")
        with patch.object(restore_points, "run_powershell", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "Accès refusé"):
                restore_points.list_restore_points()
        ok = subprocess.CompletedProcess([], 0, json.dumps([{"Sequence": 2}, {"Sequence": 1}]), "")
        with patch.object(restore_points, "run_powershell", return_value=ok) as run:
            self.assertEqual([point.sequence for point in restore_points.list_restore_points()], [2, 1])
        self.assertIn("Get-ComputerRestorePoint", run.call_args.args[0])

    def test_most_recent_restore_point_is_protected(self):
        points = self.sample_points()
        self.assertEqual(restore_points.protected_sequences(points), {14})
        self.assertEqual(restore_points.protected_sequences([]), set())
        # Horloge reculée : le dernier créé n'est pas le plus récent par date, les deux sont protégés.
        # Clock moved back: the last created is not the newest by date, both are protected.
        skewed = points + [restore_points.RestorePoint(15, "Après changement d'heure", datetime(2026, 9, 1, tzinfo=timezone.utc))]
        self.assertEqual(restore_points.protected_sequences(skewed), {14, 15})
        undated = [restore_points.RestorePoint(4, "a", None), restore_points.RestorePoint(9, "b", None)]
        self.assertEqual(restore_points.protected_sequences(undated), {9})

    def test_deleting_restore_points_never_removes_the_most_recent(self):
        removed = []
        report = restore_points.delete_restore_points([12, 13, 14, 99], list_points=self.sample_points, remove=removed.append)
        self.assertEqual(removed, [12, 13])
        self.assertEqual([point.sequence for point in report.deleted], [12, 13])
        self.assertEqual(report.protected, [14])
        self.assertEqual(report.missing, [99])
        self.assertEqual(report.errors, [])

    def test_deleting_restore_points_rereads_the_list_and_collects_errors(self):
        # Un point créé entre l'affichage et la suppression devient le plus récent : c'est lui qui est protégé.
        # A point created between display and deletion becomes the most recent: it is the one protected.
        fresh = self.sample_points() + [restore_points.RestorePoint(15, "Nouveau", datetime(2026, 10, 31, tzinfo=timezone.utc))]
        def remove(sequence):
            if sequence == 13:
                raise OSError(5, "Accès refusé")
        report = restore_points.delete_restore_points([13, 14, 15], list_points=lambda: fresh, remove=remove)
        self.assertEqual([point.sequence for point in report.deleted], [14])
        self.assertEqual(report.protected, [15])
        self.assertEqual([(point.sequence, "Accès refusé" in error) for point, error in report.errors], [(13, True)])

    def test_restore_point_script_takes_an_escaped_description(self):
        self.assertIn("-Description 'FastSweep uninstall safety point'", uninstall.restore_point_script())
        script = uninstall.restore_point_script("Point d'essai")
        self.assertIn("-Description 'Point d''essai'", script)
        self.assertNotIn("__DESCRIPTION__", script)
        done = subprocess.CompletedProcess([], 0, uninstall.RESTORE_POINT_CREATED, "")
        with patch.object(uninstall, "run_powershell", return_value=done) as run:
            self.assertEqual(uninstall.create_restore_point(uninstall.MANUAL_RESTORE_DESCRIPTION), (True, ""))
        self.assertIn(uninstall.MANUAL_RESTORE_DESCRIPTION, run.call_args.args[0])

    def restore_window(self, points=None):
        window = self.checkbox_window()
        points = self.sample_points() if points is None else points
        with patch.object(restore_page, "is_windows", return_value=True), \
                patch.object(restore_page, "list_restore_points", return_value=points):
            window.switch_mode("restore")
            self.wait_for_workers(window)
        return window, window.restore_page

    def point_row(self, page, sequence):
        return next(row for row in range(page.table.rowCount())
                    if page.table.item(row, restore_page.COL_SEQUENCE).text() == str(sequence))

    def test_restore_page_lists_points_newest_first_with_the_newest_locked(self):
        window, page = self.restore_window()
        self.assertIs(window.pages.currentWidget(), page)
        self.assertTrue(window.restore_nav.isChecked())
        self.assertFalse(window.classic_nav.isChecked())
        self.assertEqual(window.page_title.text(), window.t("restore_points"))
        self.assertEqual([page.table.item(row, restore_page.COL_SEQUENCE).text() for row in range(3)], ["14", "13", "12"])
        newest = page.table.item(self.point_row(page, 14), restore_page.COL_CHECK)
        self.assertFalse(newest.flags() & Qt.ItemFlag.ItemIsUserCheckable)
        self.assertFalse(newest.icon().isNull())  # cadenas / lock
        self.assertEqual(page.table.item(self.point_row(page, 14), restore_page.COL_STATUS).text(), window.t("rp_protected"))
        for sequence in (12, 13):
            item = page.table.item(self.point_row(page, sequence), restore_page.COL_CHECK)
            self.assertTrue(item.flags() & Qt.ItemFlag.ItemIsUserCheckable)
        self.assertFalse(page.delete_btn.isEnabled())
        window.close()

    def test_restore_page_check_all_and_space_skip_the_protected_point(self):
        window, page = self.restore_window()
        page.table.check_header.toggled.emit()
        self.assertEqual([point.sequence for point in page.checked_points()], [13, 12])
        self.assertEqual(page.table.check_header.state, Qt.CheckState.Checked)
        self.assertIn("2", page.delete_btn.text())
        self.assertTrue(page.delete_btn.isEnabled())
        page.table.check_header.toggled.emit()
        self.assertEqual(page.checked_points(), [])
        page.table.selectRow(self.point_row(page, 14))
        QTest.keyClick(page.table, Qt.Key.Key_Space)
        self.assertEqual(page.checked_points(), [])
        self.assertEqual(page.selected_points(), [])  # sélectionnable mais jamais supprimable / selectable but never deletable
        page.table.checked.add(14)  # même forcé, il reste exclu / even forced, it stays out
        self.assertEqual(page.checked_points(), [])
        window.close()

    def test_restore_page_deletes_checked_points_after_confirmation(self):
        window, page = self.restore_window()
        page.table.item(self.point_row(page, 12), restore_page.COL_CHECK).setCheckState(Qt.CheckState.Checked)
        report = restore_points.DeletionReport([self.sample_points()[0]], [], [], [])
        prompts, records = [], []
        with patch.object(window, "confirm_risky", side_effect=lambda title, text: prompts.append(text) or True), \
                patch.object(window, "record_history", side_effect=lambda *args: records.append(args)), \
                patch.object(restore_page, "delete_restore_points", return_value=report) as delete, \
                patch.object(restore_page.QMessageBox, "information") as info, \
                patch.object(restore_page, "is_windows", return_value=True), \
                patch.object(restore_page, "list_restore_points", return_value=self.sample_points()[1:]):
            window.delete_current()  # touche Suppr / Del key
            self.wait_for_workers(window)
        delete.assert_called_once_with([12])
        self.assertIn("Pilote", prompts[0])
        self.assertEqual(records[0][:3], ("Pilote", "restore_point", "success"))
        info.assert_called_once()
        self.assertEqual(page.table.rowCount(), 2)  # liste relue / list read again
        self.assertEqual(page.checked_points(), [])
        window.close()

    def test_restore_page_declined_confirmation_deletes_nothing(self):
        window, page = self.restore_window()
        page.table.item(self.point_row(page, 13), restore_page.COL_CHECK).setCheckState(Qt.CheckState.Checked)
        with patch.object(window, "confirm_risky", return_value=False), \
                patch.object(restore_page, "delete_restore_points") as delete:
            page.delete_checked()
        delete.assert_not_called()
        window.close()

    def test_restore_page_shows_load_errors_and_empty_state(self):
        window = self.checkbox_window()
        page = window.restore_page
        with patch.object(restore_page, "is_windows", return_value=True), \
                patch.object(restore_page, "list_restore_points", side_effect=RuntimeError("Accès refusé")):
            window.switch_mode("restore")
            self.wait_for_workers(window)
        self.assertTrue(page.empty_label.isVisibleTo(page))
        self.assertIn("Accès refusé", page.empty_label.text())
        with patch.object(restore_page, "is_windows", return_value=True), \
                patch.object(restore_page, "list_restore_points", return_value=[]):
            window.refresh_current()  # F5 sur cette page / F5 on this page
            self.wait_for_workers(window)
        self.assertEqual(page.empty_label.text(), window.t("rp_empty"))
        window.close()

    def test_restore_page_waits_for_a_running_task_before_loading(self):
        window = self.checkbox_window()
        page = window.restore_page
        window._busy = True
        window.set_busy(True)
        with patch.object(restore_page, "is_windows", return_value=True), \
                patch.object(restore_page, "list_restore_points", return_value=self.sample_points()):
            window.switch_mode("restore")
            self.assertTrue(page._pending_load)
            window._busy = False
            window.set_busy(False)
            deadline = time.monotonic() + 5
            while page.table.rowCount() == 0 and time.monotonic() < deadline:
                self.qt_app.processEvents()
                QTest.qWait(20)
        self.assertEqual(page.table.rowCount(), 3)
        self.assertNotEqual(page.status_label.text(), window.t("busy"))
        window.close()

    def test_returning_to_apps_page_and_switching_language(self):
        window, page = self.restore_window()
        window.toggle_language()
        self.assertEqual(page.table.horizontalHeaderItem(restore_page.COL_DATE).text(), window.t("rp_date"))
        self.assertEqual(window.page_title.text(), window.t("restore_points"))
        window.switch_mode("classic")
        self.assertIs(window.pages.currentWidget(), window.apps_page)
        self.assertTrue(window.classic_nav.isChecked())
        self.assertFalse(window.restore_nav.isChecked())
        self.assertEqual(window.page_title.text(), window.t("classic"))
        self.assertEqual(window.table.rowCount(), 3)
        window.toggle_language()
        window.close()


    # --- Optimisation : composants partagés et bugs corrigés ------------------------------------------
    # --- Optimization: shared components and fixed bugs ----------------------------------------------

    def test_powershell_output_is_forced_to_utf8(self):
        with patch.object(system, "run_hidden", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            system.run_powershell("Get-Thing", timeout=5)
        script = run.call_args.args[0][-1]
        self.assertTrue(script.startswith("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8"))
        self.assertTrue(script.endswith("Get-Thing"))

    def test_same_uninstall_key_in_64_and_32_bit_views_lists_both_installs(self):
        root64 = "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall"
        root32 = "HKLM\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall"
        keys = {root64: {}, root32: {},
                root64 + "\\7-Zip": {"DisplayName": "7-Zip 24.09 (x64)"},
                root32 + "\\7-Zip": {"DisplayName": "7-Zip 24.09"}}
        with FakeRegistry(keys):
            apps = inventory.read_classic_apps()
        self.assertEqual(sorted(app.name for app in apps), ["7-Zip 24.09", "7-Zip 24.09 (x64)"])
        self.assertEqual(len({app.key for app in apps}), 2)  # cochables séparément / checkable separately

    def test_refresh_during_a_task_announces_nothing(self):
        window = self.checkbox_window()
        window._busy = True
        window.status_label.setText("busy-marker")
        with patch.object(window, "run_background") as run:
            window.load_apps()
        run.assert_not_called()
        self.assertEqual(window.status_label.text(), "busy-marker")
        window._busy = False
        window.close()

    def test_elevation_is_refused_while_a_task_runs(self):
        window = self.checkbox_window()
        window._busy = True
        with patch.object(main_window, "relaunch_as_admin") as relaunch, \
                patch.object(main_window.QMessageBox, "information") as info:
            window.elevate_application()
        relaunch.assert_not_called()
        info.assert_called_once()
        window._busy = False
        window.close()

    def test_a_deselected_current_row_is_not_the_scan_target(self):
        window = self.checkbox_window()
        row = self.row_of(window, "Bravo")
        window.table.selectRow(row)
        self.assertEqual(window.selected.name, "Bravo")
        window.table.selectionModel().clearSelection()  # comme un Ctrl+clic / like a Ctrl+click
        self.assertEqual(window.table.currentRow(), row)
        self.assertIsNone(window.selected)
        self.assertFalse(window.scan_btn.isEnabled())
        window.close()

    def test_background_errors_always_have_a_message(self):
        worker = workers.TaskWorker(lambda: (_ for _ in ()).throw(KeyError()))
        messages = []
        worker.failed.connect(messages.append)
        worker.run()  # exécuté ici, sans thread / run here, without a thread
        self.assertEqual(messages, ["KeyError"])

    def test_history_export_is_disabled_without_events(self):
        with patch.object(dialogs, "load_operation_history", return_value=[]):
            dialog = dialogs.HistoryDialog(None, False, "fr")
        self.assertFalse(dialog.export_button.isEnabled())
        dialog.close()

    def test_check_table_skips_rows_without_a_box(self):
        table = widgets.CheckTable(2, False, key=lambda value: value)
        table.setRowCount(3)
        for row, (value, checkable) in enumerate((("a", True), ("locked", False), ("b", True))):
            table.setItem(row, 0, table.check_item(value, value, checkable=checkable))
        changes = []
        table.checks_changed.connect(lambda: changes.append(set(table.checked)))
        table.check_header.toggled.emit()
        self.assertEqual(table.checked, {"a", "b"})
        self.assertEqual(table.check_header.state, Qt.CheckState.Checked)
        table.selectAll()
        self.assertEqual(table.selected_objects(), ["a", "b"])
        table.set_rows_checked([1], True)  # ligne sans case : ignorée / row without a box: ignored
        self.assertNotIn("locked", table.checked)
        table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
        self.assertEqual(table.checked, {"b"})
        self.assertEqual(table.check_header.state, Qt.CheckState.PartiallyChecked)
        self.assertTrue(changes)
        table.deleteLater()

    def test_search_box_waits_for_a_pause_in_typing(self):
        box = widgets.SearchBox(delay_ms=30)
        fired = []
        box.search_changed.connect(lambda: fired.append(box.text()))
        for text in ("a", "ab", "abc"):
            box.setText(text)
        self.assertEqual(fired, [])
        QTest.qWait(120)
        self.assertEqual(fired, ["abc"])
        box.deleteLater()


    def test_maximized_window_keeps_the_panel_below_the_title_bar(self):
        window = self.checkbox_window()
        self.assertEqual(window.root_layout.contentsMargins().top(), 0)
        window.setWindowState(Qt.WindowState.WindowMaximized)
        self.qt_app.processEvents()
        self.assertEqual(window.root_layout.contentsMargins().top(), main_window.MAXIMIZED_TOP_INSET)
        window.setWindowState(Qt.WindowState.WindowFullScreen)
        self.qt_app.processEvents()
        self.assertEqual(window.root_layout.contentsMargins().top(), main_window.MAXIMIZED_TOP_INSET)
        window.setWindowState(Qt.WindowState.WindowNoState)
        self.qt_app.processEvents()
        self.assertEqual(window.root_layout.contentsMargins().top(), 0)
        window.close()


    # --- Mode silencieux, analyse automatique, nettoyage des points par règle --------------------
    # --- Silent mode, automatic leftover scan, rule-based restore point cleanup --------------------

    def test_silent_command_prefers_the_programs_own_then_msi_passive_mode(self):
        cmd = lambda **kw: uninstall.silent_uninstall_command(models.AppEntry(name="A", **kw))
        self.assertEqual(cmd(uninstall="u.exe", quiet_uninstall='"u.exe" /S'), '"u.exe" /S')
        self.assertEqual(cmd(uninstall="MsiExec.exe /X{AB-12}"), "MsiExec.exe /X{AB-12} /passive /norestart")
        self.assertEqual(cmd(uninstall='"C:\\Windows\\System32\\msiexec.exe" /I{AB}'),
                         '"C:\\Windows\\System32\\msiexec.exe" /I{AB} /passive /norestart')
        self.assertEqual(cmd(uninstall="msiexec /x {G} /qn"), "msiexec /x {G} /qn")  # niveau déjà choisi / level already set
        self.assertEqual(cmd(uninstall="C:\\Program Files\\App\\unins000.exe"), "")  # pas de devinette / no guessing
        self.assertEqual(cmd(uninstall="msiexecutor.exe /x"), "")

    def test_inventory_reads_the_quiet_uninstall_string(self):
        root = "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall"
        keys = {root: {}, root + "\\App": {"DisplayName": "App", "UninstallString": "u.exe", "QuietUninstallString": "u.exe /S"}}
        with FakeRegistry(keys):
            app = inventory.read_classic_apps()[0]
        self.assertEqual((app.uninstall, app.quiet_uninstall), ("u.exe", "u.exe /S"))

    def test_silent_uninstall_launches_the_quiet_command(self):
        app = models.AppEntry(name="A", uninstall="u.exe", quiet_uninstall="u.exe /S")
        with patch.object(uninstall, "launch_uninstaller", return_value=object()) as launch, \
                patch.object(uninstall, "wait_for_process_tree", return_value=0):
            loud = uninstall.perform_uninstall(app, False)
            quiet = uninstall.perform_uninstall(app, False, silent=True)
        self.assertEqual([call.args[0] for call in launch.call_args_list], ["u.exe", "u.exe /S"])
        self.assertEqual((loud["silent"], quiet["silent"]), (False, True))
        bare = models.AppEntry(name="B", uninstall="setup.exe /remove")  # rien de silencieux : l'assistant normal / nothing silent: the usual wizard
        with patch.object(uninstall, "launch_uninstaller", return_value=object()) as launch, \
                patch.object(uninstall, "wait_for_process_tree", return_value=0):
            self.assertFalse(uninstall.perform_uninstall(bare, False, silent=True)["silent"])
        launch.assert_called_once_with("setup.exe /remove")

    def test_queue_passes_silent_mode_to_every_uninstall(self):
        apps = [models.AppEntry(name=f"App {n}", uninstall="u.exe") for n in range(2)]
        worker = workers.UninstallQueueWorker(apps, False, silent=True)
        with patch.object(workers, "perform_uninstall", return_value={"restored": False, "exit_code": 0, "silent": True}) as run:
            worker.run()
        self.assertEqual([call.args[2] for call in run.call_args_list], [True, True])
        dialog = dialogs.UninstallQueueDialog(None, apps, False, False, "fr", silent=True)
        dialog.update_item(0, {"result": {"exit_code": 0, "silent": True}})
        self.assertIn(i18n.STRINGS["fr"]["queue_silent"], dialog.items.item(0).text())
        dialog.close()

    def test_tests_never_touch_the_real_settings(self):
        from PySide6.QtCore import QSettings
        settings = main_window.open_settings()
        self.assertEqual(settings.format(), QSettings.Format.IniFormat)
        self.assertTrue(Path(settings.fileName()).is_relative_to(Path(self.settings_dir.name)))
        self.assertTrue(system.app_data_dir().is_relative_to(Path(self.data_dir.name)))

    def test_uninstall_options_are_remembered(self):
        window = self.checkbox_window()
        self.assertFalse(any(check.isChecked() for check in (window.restore_check, window.silent_check, window.auto_scan_check)))
        window.silent_check.setChecked(True)
        window.auto_scan_check.setChecked(True)
        window.close()
        again = self.checkbox_window()
        self.assertEqual([again.restore_check.isChecked(), again.silent_check.isChecked(), again.auto_scan_check.isChecked()],
                         [False, True, True])
        again.close()

    def test_automatic_scan_skips_the_question_after_an_uninstall(self):
        window = self.checkbox_window()
        app = window.classic_apps[0]
        app.hive = 1
        with patch.object(main_window, "registry_key_exists", return_value=False), \
                patch.object(window, "scan_leftovers") as scan, \
                patch.object(main_window.QMessageBox, "question") as question:
            window.after_inventory_refresh(app, {"exit_code": 0})
            question.assert_called_once()  # sans l'option : la question habituelle / without the option: the usual question
            question.reset_mock()
            window.auto_scan_check.setChecked(True)
            window.after_inventory_refresh(app, {"exit_code": 0})
            question.assert_not_called()
        scan.assert_called_with([app], auto=True)
        window.close()

    def test_queue_offers_one_scan_for_every_removed_app(self):
        window = self.checkbox_window()
        apps = window.classic_apps
        for app in apps:
            app.hive = 1
        still_there = apps[1].subkey
        prompts = []
        with patch.object(main_window, "registry_key_exists", side_effect=lambda hive, key: key == still_there), \
                patch.object(window, "scan_leftovers") as scan, \
                patch.object(main_window.QMessageBox, "question",
                             side_effect=lambda *args: prompts.append(args[2]) or main_window.QMessageBox.StandardButton.Yes):
            window.offer_leftover_scan(apps)
        self.assertIn("2", prompts[0])
        scan.assert_called_once_with([apps[0], apps[2]])
        window.close()

    def test_automatic_scan_runs_both_steps_and_says_when_nothing_is_left(self):
        window = self.checkbox_window()
        apps = window.classic_apps[:2]
        with patch.object(main_window, "registry_candidates_for", return_value=[]) as registry, \
                patch.object(main_window, "file_candidates_for", return_value=[]) as files, \
                patch.object(main_window, "registry_key_exists", return_value=False), \
                patch.object(main_window.QMessageBox, "question") as question, \
                patch.object(main_window.QMessageBox, "information") as info, \
                patch.object(window, "record_history") as record:
            window.scan_leftovers(apps, auto=True)
            self.wait_for_workers(window)
        registry.assert_called_once_with(apps)
        files.assert_called_once_with(apps)
        question.assert_not_called()
        info.assert_called_once()
        self.assertEqual(info.call_args.args[2], window.t("scan_nothing"))
        self.assertEqual(record.call_args.args[0], "Alpha, Bravo")  # une entrée d'historique pour le groupe / one history entry for the group
        window.close()

    def test_grouped_scans_merge_duplicates_and_keep_the_strongest(self):
        a, b = models.AppEntry(name="A"), models.AppEntry(name="B")
        weak = models.RegistryCandidate(1, r"SOFTWARE\Shared", "HKCU", "low", "similar-display-name")
        strong = models.RegistryCandidate(1, r"SOFTWARE\Shared", "HKCU", "high", "display-name")
        other = models.RegistryCandidate(1, r"SOFTWARE\OnlyB", "HKCU", "medium", "key-name")
        with patch.object(cleanup, "registry_candidates", side_effect=lambda app: [weak] if app is a else [strong, other]):
            merged = cleanup.registry_candidates_for([a, b])
        self.assertEqual([(c.path, c.confidence) for c in merged], [(r"SOFTWARE\Shared", "high"), (r"SOFTWARE\OnlyB", "medium")])
        files = {a: [models.FileCandidate("C:\\X", "low", "similar-name")], b: [models.FileCandidate("c:\\x", "high", "exact-name")]}
        with patch.object(cleanup, "file_candidates", side_effect=lambda app: files[app]):
            self.assertEqual([(c.path, c.confidence) for c in cleanup.file_candidates_for([a, b])], [("c:\\x", "high")])

    def test_rule_selection_by_age_or_count_never_takes_the_protected_point(self):
        points = self.sample_points()  # 12 (01/10), 13 (15/10), 14 (30/10, protégé / protected)
        now = datetime(2026, 10, 31, tzinfo=timezone.utc)
        by_age = restore_points.select_by_rule(points, older_than_days=20, now=now)
        self.assertEqual([p.sequence for p in by_age], [12])
        self.assertEqual([p.sequence for p in restore_points.select_by_rule(points, older_than_days=0, now=now)], [13, 12])
        self.assertEqual([p.sequence for p in restore_points.select_by_rule(points, keep_latest=1)], [13, 12])
        self.assertEqual([p.sequence for p in restore_points.select_by_rule(points, keep_latest=0)], [13, 12])  # au moins 1 / at least 1
        self.assertEqual(restore_points.select_by_rule(points, keep_latest=5), [])
        undated = points + [restore_points.RestorePoint(5, "Sans date", None)]
        self.assertNotIn(5, [p.sequence for p in restore_points.select_by_rule(undated, older_than_days=0, now=now)])
        self.assertEqual(restore_points.select_by_rule(points), [])

    def test_rule_dialog_previews_and_switches_rules(self):
        dialog = restore_page.RestoreRuleDialog(None, self.sample_points(), False, "fr", "age", 3650)
        self.assertEqual(dialog.mode, "age")
        self.assertEqual(dialog.selected, [])
        self.assertFalse(dialog.apply_button.isEnabled())
        dialog.keep.setValue(2)  # régler le nombre choisit la règle / setting the number picks the rule
        self.assertEqual(dialog.mode, "keep")
        self.assertEqual([p.sequence for p in dialog.selected], [12])
        self.assertEqual(dialog.list.count(), 1)
        self.assertIn("1", dialog.apply_button.text())
        self.assertTrue(dialog.apply_button.isEnabled())
        dialog.close()

    def test_rule_cleanup_deletes_after_confirmation_and_remembers_the_rule(self):
        window, page = self.restore_window()
        def fake_exec(dialog):
            dialog.keep.setValue(1)
            return restore_page.QDialog.DialogCode.Accepted
        report = restore_points.DeletionReport(self.sample_points()[:2], [], [], [])
        with patch.object(restore_page.RestoreRuleDialog, "exec", fake_exec), \
                patch.object(window, "confirm_risky", return_value=True) as confirm, \
                patch.object(restore_page, "delete_restore_points", return_value=report) as delete, \
                patch.object(restore_page.QMessageBox, "information"), \
                patch.object(restore_page, "is_windows", return_value=True), \
                patch.object(restore_page, "list_restore_points", return_value=self.sample_points()[2:]):
            page.clean_by_rule()
            self.wait_for_workers(window)
        confirm.assert_called_once()
        delete.assert_called_once_with([13, 12])
        self.assertEqual((window.settings.value("rp_rule"), int(window.settings.value("rp_rule_keep"))), ("keep", 1))
        self.assertFalse(page.rule_btn.isEnabled())  # un seul point restant : rien à nettoyer / one point left: nothing to clean
        window.close()


    # --- Mise à jour depuis GitHub / Update from GitHub -------------------------------------------------

    class FakeResponse:
        """Réponse HTTP minimale : read(), geturl(), headers et gestionnaire de contexte.

        Minimal HTTP response: read(), geturl(), headers and context manager.
        """

        def __init__(self, data: bytes, url: str = "https://release-assets.githubusercontent.com/abc"):
            self.data, self.url = io.BytesIO(data), url
            self.headers = {"Content-Length": str(len(data))}

        def read(self, size=-1):
            return self.data.read(size)

        def geturl(self):
            return self.url

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    EXE = b"MZ" + b"\x90" * 5000
    ASSET_URL = updater.DOWNLOAD_PREFIX + "v1.0.2/FastSweep.exe"

    def release_json(self, tag="v1.0.2", assets="default", body="### v1.0.2\n- Nouveau"):
        if assets == "default":
            assets = [{"name": "FastSweep.exe", "state": "uploaded", "size": len(self.EXE), "browser_download_url": self.ASSET_URL,
                       "digest": "sha256:" + hashlib.sha256(self.EXE).hexdigest().upper()}]
        return {"tag_name": tag, "body": body, "html_url": "https://github.com/Bulls64/FastSweep/releases/tag/" + tag, "assets": assets}

    def with_release(self, data):
        return patch.object(updater, "open_url", return_value=self.FakeResponse(json.dumps(data).encode()))

    def info(self, **changes):
        base = dict(version="1.0.2", tag="v1.0.2", notes="- Nouveau", url=self.ASSET_URL, size=len(self.EXE),
                    sha256=hashlib.sha256(self.EXE).hexdigest(), page=updater.RELEASES_URL, newer=True)
        base.update(changes)
        return updater.UpdateInfo(**base)

    def test_versions_are_compared_numerically(self):
        self.assertEqual(updater.parse_version("v1.0.10"), (1, 0, 10))
        self.assertEqual(updater.parse_version("nope"), ())
        self.assertTrue(updater.is_newer("1.0.10", "1.0.9"))  # pas alphabétique / not alphabetical
        self.assertTrue(updater.is_newer("v1.1", "1.0.9"))
        self.assertFalse(updater.is_newer("1.0", "1.0.0"))
        self.assertFalse(updater.is_newer("1.0.1", "1.0.1"))
        self.assertFalse(updater.is_newer("1.0.0", "1.0.1"))
        self.assertFalse(updater.is_newer("", "1.0.1"))

    def test_check_latest_reads_the_release_and_its_checksum(self):
        with self.with_release(self.release_json()) as opened:
            info = updater.check_latest("1.0.1")
        self.assertEqual((info.version, info.newer, info.installable, info.size), ("1.0.2", True, True, len(self.EXE)))
        self.assertEqual(info.sha256, hashlib.sha256(self.EXE).hexdigest())  # normalisée en minuscules / lowercased
        self.assertIn("Nouveau", info.notes)
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, updater.API_LATEST)
        self.assertIn("FastSweep/", request.get_header("User-agent"))

    def test_check_latest_handles_up_to_date_and_missing_exe(self):
        with self.with_release(self.release_json("v1.0.1")):
            self.assertFalse(updater.check_latest("1.0.1").newer)
        with self.with_release(self.release_json(assets=[])):
            info = updater.check_latest("1.0.1")
        self.assertTrue(info.newer)
        self.assertFalse(info.installable)  # publiée mais l'exe n'est pas encore joint / published but the exe is not attached yet

    def test_check_latest_ignores_download_links_from_other_places(self):
        foreign = [{"name": "FastSweep.exe", "state": "uploaded", "size": 5, "browser_download_url": "https://evil.example/FastSweep.exe"}]
        with self.with_release(self.release_json(assets=foreign)):
            self.assertEqual(updater.check_latest("1.0.1").url, "")
        pending = [{"name": "FastSweep.exe", "state": "open", "size": 5, "browser_download_url": self.ASSET_URL}]
        with self.with_release(self.release_json(assets=pending)):  # envoi en cours / upload in progress
            self.assertEqual(updater.check_latest("1.0.1").url, "")
        with self.with_release(self.release_json(assets=[{"name": "FastSweep.exe", "browser_download_url": self.ASSET_URL}])):
            self.assertEqual(updater.check_latest("1.0.1").sha256, "")  # pas d'empreinte fournie / no checksum given

    def test_check_latest_reports_each_failure_with_its_own_key(self):
        def fails(error):
            with patch.object(updater, "open_url", side_effect=error):
                with self.assertRaises(updater.UpdateError) as caught:
                    updater.check_latest("1.0.1")
            return caught.exception.key
        http = lambda code: urllib.error.HTTPError(updater.API_LATEST, code, "x", {}, None)
        self.assertEqual(fails(http(403)), "update_err_rate")
        self.assertEqual(fails(http(429)), "update_err_rate")
        self.assertEqual(fails(http(404)), "update_err_none")
        self.assertEqual(fails(http(500)), "update_err_network")
        self.assertEqual(fails(urllib.error.URLError("no route")), "update_err_network")
        self.assertEqual(fails(TimeoutError("slow")), "update_err_network")
        for bad in (b"not json", b"[]", json.dumps({"tag_name": "latest"}).encode()):
            with patch.object(updater, "open_url", return_value=self.FakeResponse(bad)):
                with self.assertRaises(updater.UpdateError) as caught:
                    updater.check_latest("1.0.1")
            self.assertEqual(caught.exception.key, "update_err_data")

    def download(self, info=None, data=None, url="https://release-assets.githubusercontent.com/abc", **kwargs):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "FastSweep.exe.new"
            response = self.FakeResponse(self.EXE if data is None else data, url)
            error = None
            with patch.object(updater, "open_url", return_value=response):
                try:
                    updater.download(info or self.info(), target, **kwargs)
                except updater.UpdateError as exc:
                    error = exc.key
            leftovers = sorted(path.name for path in Path(temporary).iterdir())
            return error, leftovers, (target.read_bytes() if target.exists() else None)

    def test_download_keeps_the_file_only_when_everything_checks_out(self):
        seen = []
        error, leftovers, content = self.download(progress=lambda done, total: seen.append((done, total)))
        self.assertIsNone(error)
        self.assertEqual((leftovers, content), (["FastSweep.exe.new"], self.EXE))  # pas de .part / no .part
        self.assertEqual(seen[-1], (len(self.EXE), len(self.EXE)))

    def test_download_rejects_a_bad_file_and_leaves_nothing_behind(self):
        for label, kwargs, key in (
            ("somme", dict(info=self.info(sha256="0" * 64)), "update_err_hash"),
            ("taille", dict(info=self.info(size=len(self.EXE) + 1)), "update_err_size"),
            ("type", dict(data=b"PK" + b"\x00" * 5000, info=self.info(size=5002, sha256="")), "update_err_notexe"),
            ("hôte", dict(url="https://evil.example/FastSweep.exe"), "update_err_host"),
            ("http", dict(url="http://objects.githubusercontent.com/x"), "update_err_host"),
            ("absent", dict(info=self.info(url="")), "update_no_exe"),
            ("hors dépôt", dict(info=self.info(url="https://github.com/Other/Repo/releases/download/v1/FastSweep.exe")), "update_no_exe"),
        ):
            with self.subTest(label):
                error, leftovers, content = self.download(**kwargs)
                self.assertEqual(error, key)
                self.assertEqual((leftovers, content), ([], None))

    def test_download_can_be_cancelled(self):
        stop = threading.Event()
        stop.set()
        error, leftovers, _content = self.download(stop=stop)
        self.assertEqual((error, leftovers), ("update_err_cancelled", []))

    def make_install(self, temporary):
        exe = Path(temporary) / "FastSweep.exe"
        exe.write_bytes(b"MZ-old")
        staged = updater.staged_path(exe)
        staged.write_bytes(self.EXE)  # le fichier de la release de self.info() / the file of self.info()'s release
        return exe, staged

    def test_install_swaps_the_executable_and_keeps_the_old_one_aside(self):
        with tempfile.TemporaryDirectory() as temporary:
            exe, staged = self.make_install(temporary)
            updater.install(staged, exe, self.info())
            self.assertEqual(exe.read_bytes(), self.EXE)
            self.assertEqual(exe.with_name("FastSweep.exe.old").read_bytes(), b"MZ-old")
            self.assertFalse(staged.exists())

    def test_install_puts_the_old_version_back_when_it_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            exe, staged = self.make_install(temporary)
            with patch.object(updater.os, "replace", side_effect=PermissionError("verrouillé")):
                with self.assertRaises(updater.UpdateError) as caught:
                    updater.install(staged, exe, self.info())
            self.assertEqual(caught.exception.key, "update_err_write")
            self.assertEqual(exe.read_bytes(), b"MZ-old")  # l'application reste utilisable / the app stays usable
            self.assertTrue(staged.exists())

    def test_leftovers_are_cleaned_but_the_executable_is_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            exe, _staged = self.make_install(temporary)
            exe.with_name("FastSweep.exe.old").write_bytes(b"x")
            exe.with_name("FastSweep.exe.part").write_bytes(b"x")
            keep = Path(temporary) / "FastSweep.exe.bak"  # pas à nous : jamais touché / not ours: never touched
            keep.write_bytes(b"x")
            updater.cleanup_leftovers(exe)
            self.assertEqual(sorted(path.name for path in Path(temporary).iterdir()), ["FastSweep.exe", "FastSweep.exe.bak"])

    def test_restart_resets_the_pyinstaller_environment(self):
        with patch.object(updater.subprocess, "Popen") as popen:
            updater.restart(Path("C:/Apps/FastSweep.exe"))
        self.assertEqual(popen.call_args.args[0], [str(Path("C:/Apps/FastSweep.exe"))])
        self.assertEqual(popen.call_args.kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")

    def test_only_the_built_executable_can_replace_itself(self):
        self.assertEqual(updater.self_update_problem(), "update_from_source")  # lancé avec python / run with python
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(updater, "is_windows", return_value=True), \
                patch.object(updater.sys, "frozen", True, create=True), \
                patch.object(updater.sys, "executable", str(Path(temporary) / "FastSweep.exe")):
            self.assertEqual(updater.self_update_problem(), "")
            with patch.object(updater.os, "access", return_value=False):
                self.assertEqual(updater.self_update_problem(), "update_not_writable")

    def wait_until(self, condition, seconds=5):
        deadline = time.monotonic() + seconds
        while not condition() and time.monotonic() < deadline:
            self.qt_app.processEvents()
            QTest.qWait(10)
        return condition()

    def test_update_task_delivers_results_in_the_interface_thread(self):
        class Receiver(update_ui.QObject):
            def __init__(self):
                super().__init__()
                self.threads = []
            def got(self, *_args):
                self.threads.append(threading.get_ident())
        receiver, task = Receiver(), update_ui.UpdateTask()
        worker = []
        task.done.connect(receiver.got)
        task.progress.connect(receiver.got)
        task.start(lambda progress, stop: (worker.append(threading.get_ident()), progress(1, 2), "ok")[-1])
        self.assertTrue(self.wait_until(lambda: len(receiver.threads) == 2))
        self.assertEqual(set(receiver.threads), {threading.get_ident()})
        self.assertNotEqual(worker[0], threading.get_ident())

    def update_dialog(self, info=None, problem=""):
        patcher = patch.object(updater, "self_update_problem", return_value=problem)
        patcher.start()
        self.addCleanup(patcher.stop)
        return update_ui.UpdateDialog(None, False, "fr", main_window.open_settings(), info)

    def test_dialog_offers_the_install_when_a_newer_version_is_ready(self):
        dialog = self.update_dialog(self.info())
        self.assertIn("1.0.2", dialog.status_label.text())
        self.assertEqual(dialog.action_mode(), "install")
        self.assertEqual(dialog.action_button.text(), i18n.STRINGS["fr"]["update_btn_install"])
        self.assertFalse(dialog.action_button.isHidden())
        self.assertFalse(dialog.notes.isHidden())
        self.assertIn("Nouveau", dialog.notes.toPlainText())
        dialog.close()

    def test_dialog_hides_the_action_when_up_to_date(self):
        dialog = self.update_dialog(self.info(newer=False))
        self.assertEqual(dialog.status_label.text(), i18n.STRINGS["fr"]["update_uptodate"])
        self.assertTrue(dialog.action_button.isHidden())
        dialog.close()

    def test_dialog_sends_to_the_download_page_when_it_cannot_install(self):
        for info, problem, expected in ((self.info(url=""), "", "update_no_exe"),
                                        (self.info(), "update_from_source", "update_from_source"),
                                        (self.info(), "update_not_writable", "update_not_writable")):
            with self.subTest(expected):
                dialog = self.update_dialog(info, problem)
                self.assertEqual(dialog.action_mode(), "page")
                self.assertIn(i18n.STRINGS["fr"][expected], dialog.status_label.text())
                with patch.object(update_ui.QDesktopServices, "openUrl") as open_url:
                    dialog.run_action()
                self.assertEqual(open_url.call_args.args[0].toString(), updater.RELEASES_URL)
                dialog.close()

    def test_dialog_checks_on_opening_and_reports_errors_in_its_language(self):
        with patch.object(updater, "check_latest", return_value=self.info()):
            dialog = self.update_dialog(None)
            self.assertTrue(self.wait_until(lambda: dialog.info is not None))
        self.assertEqual(dialog.action_mode(), "install")
        dialog.close()
        with patch.object(updater, "check_latest", side_effect=updater.UpdateError("update_err_rate")):
            dialog = self.update_dialog(None)
            self.assertTrue(self.wait_until(lambda: i18n.STRINGS["fr"]["update_err_rate"] in dialog.status_label.text()))
        self.assertTrue(dialog.check_button.isEnabled())  # on peut réessayer / can retry
        dialog.close()

    def test_dialog_downloads_then_hands_over_the_staged_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            exe = Path(temporary) / "FastSweep.exe"
            staged = updater.staged_path(exe)
            def fake_download(info, target, progress, stop):
                progress(50, 100)
                return target
            with patch.object(updater, "current_executable", return_value=exe), \
                    patch.object(updater, "download", side_effect=fake_download) as download:
                dialog = self.update_dialog(self.info())
                dialog.run_action()
                self.assertTrue(self.wait_until(lambda: dialog.staged is not None))
            self.assertEqual(dialog.staged, staged)
            self.assertEqual(download.call_args.args[1], staged)
            self.assertEqual(dialog.result(), main_window.QDialog.DialogCode.Accepted)
            dialog.close()

    def test_dialog_cancel_stops_the_download_and_closing_ignores_late_results(self):
        started = threading.Event()
        def slow(info, target, progress, stop):
            started.set()
            stop.wait(5)
            raise updater.UpdateError("update_err_cancelled")
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(updater, "current_executable", return_value=Path(temporary) / "FastSweep.exe"), \
                patch.object(updater, "download", side_effect=slow):
            dialog = self.update_dialog(self.info())
            dialog.run_action()
            self.assertTrue(started.wait(5))
            self.assertEqual(dialog.action_mode(), "cancel")
            dialog.run_action()
            self.assertTrue(self.wait_until(lambda: i18n.STRINGS["fr"]["update_err_cancelled"] in dialog.status_label.text()))
            self.assertEqual(dialog.action_mode(), "install")  # on peut relancer / can start again
            self.assertIsNone(dialog.staged)
            dialog.close()

    def test_dialog_options_are_remembered_and_auto_install_follows_the_check(self):
        dialog = self.update_dialog(self.info(newer=False))
        self.assertTrue(dialog.auto_check.isChecked())
        self.assertFalse(dialog.auto_install.isChecked())  # jamais sans accord / never without consent
        dialog.auto_install.setChecked(True)
        dialog.auto_check.setChecked(False)
        self.assertFalse(dialog.auto_install.isEnabled())
        settings = main_window.open_settings()
        self.assertEqual((settings.value("update_auto_check", True, type=bool), settings.value("update_auto_install", False, type=bool)),
                         (False, True))
        dialog.close()

    def test_window_only_looks_for_updates_at_startup_when_asked(self):
        with patch.object(main_window.QTimer, "singleShot") as timer:
            plain = self.checkbox_window()
            plain.show()
            timer.assert_not_called()  # tests et usages sans réseau / tests and offline uses
            plain.close()
            with patch.object(main_window, "read_classic_apps", return_value=[]), \
                    patch.object(main_window, "read_store_apps", return_value=([], "")):
                real = main_window.MainWindow(check_updates=True)
                self.wait_for_workers(real)
                real.show()
            self.assertEqual(timer.call_args.args, (main_window.STARTUP_UPDATE_DELAY_MS, real.startup_update_check))
            real.close()

    def startup_window(self, info):
        window = self.checkbox_window()
        patcher = patch.object(updater, "check_latest", return_value=info)
        patcher.start()
        self.addCleanup(patcher.stop)
        return window

    def test_startup_check_is_silent_and_marks_the_sidebar_button(self):
        window = self.startup_window(self.info())
        self.assertFalse(window.settings_nav.property("highlight"))
        with patch.object(window, "show_updates") as show:
            window.startup_update_check()
            self.assertTrue(self.wait_until(lambda: window.update_info is not None))
        # L'entrée « Réglages » s'allume et sa section annonce la version. / "Settings" lights up and its section names the version.
        self.assertTrue(window.settings_nav.property("highlight"))
        self.assertIn("1.0.2", window.settings_nav.toolTip())
        self.assertIn("1.0.2", window.settings_page.updates_row[1].text())
        self.assertEqual(window.settings_page.check_now.text(), window.t("settings_update_show"))
        self.assertEqual(window.settings_page.check_now.objectName(), "primaryButton")
        show.assert_called_once()  # proposée à l'ouverture / offered on startup
        window.close()

    def test_startup_prompt_appears_once_per_version(self):
        window = self.startup_window(self.info())
        with patch.object(window, "show_updates") as show:
            for _ in range(2):
                window.startup_update_check()
                self.assertTrue(self.wait_until(lambda: window._update_task is None))
            self.assertEqual(show.call_count, 1)
            window.settings.setValue("update_dismissed", "")
            window.startup_update_check()
            self.assertTrue(self.wait_until(lambda: show.call_count == 2))
        window.close()

    def test_startup_check_respects_the_opt_out_and_stays_quiet_when_offline(self):
        window = self.startup_window(self.info())
        window.settings.setValue("update_auto_check", False)
        window.settings.setValue("whats_new_seen", main_window.__version__)  # nouveautés déjà vues / what's new already seen
        with patch.object(window, "show_updates") as show:
            window.startup_update_check()
            self.assertIsNone(window._update_task)  # rien à demander à GitHub / nothing to ask GitHub
        window.settings.setValue("update_auto_check", True)
        with patch.object(updater, "check_latest", side_effect=updater.UpdateError("update_err_network", "offline")), \
                patch.object(main_window.QMessageBox, "critical") as critical, \
                patch.object(main_window.QMessageBox, "warning") as warning:
            window.startup_update_check()
            self.assertTrue(self.wait_until(lambda: window._update_task is None))
        show.assert_not_called()
        critical.assert_not_called()
        warning.assert_not_called()
        self.assertIsNone(window.update_info)
        window.close()

    def test_automatic_install_downloads_then_applies_without_asking(self):
        window = self.startup_window(self.info())
        window.settings.setValue("update_auto_install", True)
        with tempfile.TemporaryDirectory() as temporary:
            exe = Path(temporary) / "FastSweep.exe"
            with patch.object(updater, "self_update_problem", return_value=""), \
                    patch.object(updater, "current_executable", return_value=exe), \
                    patch.object(updater, "download", side_effect=lambda info, target, progress, stop: target), \
                    patch.object(window, "apply_update") as apply, \
                    patch.object(window, "show_updates") as show:
                window.startup_update_check()
                self.assertTrue(self.wait_until(lambda: apply.called))
            apply.assert_called_once_with(updater.staged_path(exe))
            show.assert_not_called()
        window.close()

    def test_automatic_install_falls_back_to_the_prompt_when_it_cannot_install(self):
        window = self.startup_window(self.info())
        window.settings.setValue("update_auto_install", True)
        with patch.object(updater, "self_update_problem", return_value="update_from_source"), \
                patch.object(window, "apply_update") as apply, patch.object(window, "show_updates") as show:
            window.startup_update_check()
            self.assertTrue(self.wait_until(lambda: show.called))
        apply.assert_not_called()
        window.close()

    def test_apply_update_replaces_the_exe_records_history_and_restarts(self):
        window = self.checkbox_window()
        window.update_info = self.info()
        with tempfile.TemporaryDirectory() as temporary:
            exe, staged = self.make_install(temporary)
            with patch.object(updater, "current_executable", return_value=exe), \
                    patch.object(updater, "restart") as restart, \
                    patch.object(window, "record_history") as record, \
                    patch.object(window, "close") as close:
                window.apply_update(staged)
            self.assertEqual(exe.read_bytes(), self.EXE)
        restart.assert_called_once_with(exe)
        close.assert_called_once()
        self.assertEqual(record.call_args.args[:3], ("FastSweep", "update", "success"))
        self.assertEqual(record.call_args.args[3]["to"], "1.0.2")
        window.close()

    def test_apply_update_waits_for_a_running_operation_and_survives_failures(self):
        window = self.checkbox_window()
        window.update_info = self.info()
        with tempfile.TemporaryDirectory() as temporary:
            exe, staged = self.make_install(temporary)
            with patch.object(updater, "current_executable", return_value=exe), \
                    patch.object(updater, "restart") as restart, \
                    patch.object(main_window.QMessageBox, "information") as info, \
                    patch.object(main_window.QMessageBox, "critical") as critical, \
                    patch.object(main_window.QMessageBox, "warning") as warning, \
                    patch.object(window, "record_history") as record, \
                    patch.object(window, "close") as close:
                window._busy = True
                window.apply_update(staged)
                info.assert_called_once()
                self.assertEqual(exe.read_bytes(), b"MZ-old")  # rien touché pendant une opération / nothing touched during an operation
                window._busy = False
                with patch.object(updater.os, "replace", side_effect=PermissionError("verrouillé")):
                    window.apply_update(staged)
                critical.assert_called_once()
                self.assertEqual(exe.read_bytes(), b"MZ-old")  # ancienne version conservée / old version kept
                self.assertEqual(record.call_args.args[2], "failed")
                restart.assert_not_called()
                close.assert_not_called()
                restart.side_effect = OSError("introuvable")
                window.apply_update(staged)  # l'installation réussit, le relancement échoue / install works, restart fails
                warning.assert_called_once()
                close.assert_not_called()  # l'utilisateur garde sa fenêtre et un message / user keeps the window and a message
        window.close()

    BILINGUAL_NOTES = ("### v1.0.2\r\n\r\n**New**\r\n- Update from GitHub at startup.\r\n\r\n**Fixes**\r\n- The watcher no longer waits.\r\n\r\n"
                       "---\r\n\r\n### v1.0.2\r\n\r\n**Nouveautés**\r\n- Mise à jour depuis GitHub au démarrage.\r\n\r\n\r\n\r\n"
                       "**Corrections**\r\n- La surveillance n'attend plus.\r\n\r\n")

    def test_release_notes_keep_only_the_interface_language(self):
        french = updater.clean_notes(self.BILINGUAL_NOTES, "fr", "v1.0.2")
        english = updater.clean_notes(self.BILINGUAL_NOTES, "en", "v1.0.2")
        self.assertTrue(french.startswith("**Nouveautés**"))  # titre « v1.0.2 » redondant retiré / redundant heading removed
        self.assertNotIn("Fixes", french)
        self.assertTrue(english.startswith("**New**"))
        self.assertNotIn("Corrections", english)
        for text in (french, english):
            self.assertNotIn("\r", text)
            self.assertNotIn("\n\n\n", text)  # plus de lignes vides en série / no more runs of blank lines
            self.assertNotIn("---", text)

    def test_release_notes_stay_whole_when_the_language_is_unclear(self):
        single = "### v1.0.3\n\n- Bug fixes\n- Nouvelles traductions"
        self.assertEqual(updater.clean_notes(single, "fr", "v1.0.3"), "- Bug fixes\n- Nouvelles traductions")
        two_unknown = "Release 42\n\n---\n\n1.2.3"
        self.assertEqual(updater.clean_notes(two_unknown, "fr", "v1.0.3"), two_unknown)  # rien de reconnu : tout garder / nothing recognized: keep it all
        self.assertTrue(updater.clean_notes("### Version 2\n- x", "en", "v1.0.3").startswith("### Version 2"))  # pas le numéro : gardé / not the number: kept
        self.assertEqual(updater.clean_notes("", "fr", "v1"), "")

    def test_release_notes_are_rendered_compactly(self):
        dialog = self.update_dialog(self.info(notes=self.BILINGUAL_NOTES))
        document = dialog.notes.document()
        self.assertEqual(document.documentMargin(), update_ui.NOTES_MARGIN)
        self.assertEqual(document.indentWidth(), update_ui.NOTES_INDENT)
        text = dialog.notes.toPlainText()
        self.assertIn("Nouveautés", text)
        self.assertNotIn("Fixes", text)
        block = document.begin()
        while block.isValid():
            self.assertLessEqual(block.blockFormat().bottomMargin(), update_ui.BLOCK_BOTTOM)
            self.assertLessEqual(block.blockFormat().topMargin(), update_ui.HEADING_TOP)
            block = block.next()
        dialog.close()

    def test_update_history_entries_are_named_in_both_languages(self):
        for lang in ("fr", "en"):
            self.assertIn("history_update", i18n.STRINGS[lang])
        self.assertEqual(dialogs.HistoryDialog.OPERATION_KEYS["update"], "history_update")


    # --- Désinstallation d'une app du Store / Store app uninstall ----------------------------------------

    def store_app(self, name="2505FireCubeStudios.WinverUWP", full="2505FireCubeStudios.WinverUWP_2.1.4.0_x64__k45w5yt88e21j"):
        return models.AppEntry(name="Winver UWP", source="store", package_name=name, package_full_name=full)

    def test_store_uninstall_looks_up_by_name_never_with_an_abbreviated_parameter(self):
        script = uninstall.store_uninstall_script(self.store_app())
        self.assertNotIn("Get-AppxPackage -Package", script)  # devenait -PackageTypeFilter / became -PackageTypeFilter
        self.assertIn("Get-AppxPackage -Name '2505FireCubeStudios.WinverUWP'", script)
        self.assertIn("$_.PackageFullName -eq '2505FireCubeStudios.WinverUWP_2.1.4.0_x64__k45w5yt88e21j'", script)
        self.assertIn("Remove-AppxPackage -Package $package[0].PackageFullName", script)
        quoted = uninstall.store_uninstall_script(self.store_app("O'Brien.App", "O'Brien.App_1.0_x64__abc"))
        self.assertIn("-Name 'O''Brien.App'", quoted)  # apostrophe doublée / doubled quote
        self.assertIn("-eq 'O''Brien.App_1.0_x64__abc'", quoted)
        self.assertIn("@(Get-AppxPackage | Where-Object", uninstall.store_uninstall_script(self.store_app(name="")))

    def test_store_uninstall_runs_the_script_and_reports_powershell_errors(self):
        app = self.store_app()
        ok = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(uninstall, "run_powershell", return_value=ok) as run:
            result = uninstall.perform_uninstall(app, False)
        self.assertEqual(run.call_args.args[0], uninstall.store_uninstall_script(app))
        self.assertEqual((result["source"], result["exit_code"]), ("store", 0))
        failed = subprocess.CompletedProcess([], 1, "", "Remove-AppxPackage : Accès refusé")
        with patch.object(uninstall, "run_powershell", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "Accès refusé"):
                uninstall.perform_uninstall(app, False)

    @unittest.skipUnless(sys.platform == "win32", "PowerShell et Appx : Windows uniquement / Windows only")
    def test_store_uninstall_script_is_accepted_by_real_powershell(self):
        # Package inexistant : rien n'est désinstallé, mais PowerShell doit accepter tous les paramètres.
        # Missing package: nothing is uninstalled, but PowerShell must accept every parameter.
        app = self.store_app("FastSweep.Test.DoesNotExist", "FastSweep.Test.DoesNotExist_1.0.0.0_x64__0000000000000")
        result = system.run_powershell(uninstall.store_uninstall_script(app), timeout=120)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("PackageTypeFilter", result.stderr)
        self.assertIn("Package is no longer installed", result.stderr, result.stderr)

    # --- Page Réglages et opacité des fonds / Settings page and background opacity ----------------------

    def test_default_opacity_keeps_the_current_translucent_look(self):
        for dark in (False, True):
            current = theme.colors(dark, True, theme.DEFAULT_OPACITY)
            for token, value in theme._TRANSLUCENT[dark].items():
                self.assertAlmostEqual(theme.qcolor(current[token]).alphaF(), theme.qcolor(value).alphaF(), places=2)
            self.assertEqual(theme.qcolor(current["window"]).alpha(), 0)

    def test_opacity_goes_from_more_see_through_to_solid(self):
        for dark in (False, True):
            solid = theme.colors(dark)
            alphas = []
            for level in (0, 25, 50, 75, 100):
                alphas.append(theme.qcolor(theme.colors(dark, True, level)["card"]).alphaF())
            self.assertEqual(alphas, sorted(alphas))  # croissante / increasing
            self.assertLess(alphas[0], alphas[2])
            full = theme.colors(dark, True, 100)
            for token in ("layer", "card", "ctl", "footer", "window"):
                color, expected = theme.qcolor(full[token]), theme.qcolor(solid["mica" if token == "window" else token])
                self.assertEqual((color.alpha(), color.rgb()), (255, expected.rgb()))  # 100 % = couleurs pleines / solid colors
        self.assertEqual(theme.colors(True)["card"], "#2B2B2B")  # mode opaque inchangé / opaque mode unchanged

    def test_background_opacity_is_clamped_and_used_by_the_stylesheet(self):
        self.addCleanup(theme.set_background_opacity, theme.DEFAULT_OPACITY)
        self.assertEqual(theme.set_background_opacity(150), 100)
        self.assertEqual(theme.set_background_opacity(-3), 0)
        theme.set_background_opacity(80)
        self.assertIn(theme.colors(True, True, 80)["card"], theme.theme_stylesheet(True, True))
        self.assertEqual(theme.theme_stylesheet(True, False), theme._theme_stylesheet(True, False, theme.DEFAULT_OPACITY))

    def settings_window(self, translucent=False):
        self.addCleanup(theme.set_background_opacity, theme.DEFAULT_OPACITY)
        window = self.checkbox_window()
        window._translucent = translucent
        window.settings_page.load()
        window.switch_mode("settings")
        return window, window.settings_page

    def test_settings_page_opens_from_the_sidebar(self):
        window, page = self.settings_window()
        self.assertIs(window.pages.currentWidget(), page)
        self.assertTrue(window.settings_nav.isChecked())
        self.assertFalse(window.classic_nav.isChecked())
        self.assertEqual(window.page_title.text(), window.t("settings"))
        with patch.object(window, "load_apps") as load, patch.object(window, "uninstall_selection") as uninstall_mock:
            window.refresh_current()  # F5 et Suppr ne font rien ici / F5 and Del do nothing here
            window.delete_current()
        load.assert_not_called()
        uninstall_mock.assert_not_called()
        window.switch_mode("classic")
        self.assertIs(window.pages.currentWidget(), window.apps_page)
        self.assertFalse(window.settings_nav.isChecked())
        window.close()

    def test_settings_page_changes_theme_and_language_and_stays_in_sync(self):
        window, page = self.settings_window()
        page.theme_combo.setCurrentIndex(0 if window.dark else 1)
        dark = window.dark
        self.assertEqual(page.theme_combo.currentIndex(), 1 if dark else 0)
        window.toggle_theme()  # bouton du haut / top button
        self.assertEqual(page.theme_combo.currentIndex(), 1 if window.dark else 0)
        page.language_combo.setCurrentIndex(page.language_combo.findData("en"))
        self.assertEqual(window.lang, "en")
        self.assertEqual(window.page_title.text(), "Settings")
        window.toggle_language()
        self.assertEqual(page.language_combo.currentData(), "fr")
        self.assertEqual(page.theme_combo.itemText(0), i18n.STRINGS["fr"]["settings_theme_light"])
        window.close()

    def test_opacity_slider_is_disabled_without_acrylic(self):
        window, page = self.settings_window(translucent=False)
        self.assertFalse(page.opacity_slider.isEnabled())
        self.assertEqual(page.opacity_row[1].text(), window.t("settings_opacity_unavailable"))
        window.close()

    def test_opacity_slider_restyles_remembers_and_resets(self):
        window, page = self.settings_window(translucent=True)
        self.assertTrue(page.opacity_slider.isEnabled())
        self.assertEqual(page.opacity_slider.value(), theme.DEFAULT_OPACITY)
        self.assertFalse(page.opacity_reset.isEnabled())
        page.opacity_slider.setValue(20)
        self.assertEqual(page.opacity_value.text(), "20 %")
        self.assertTrue(self.wait_until(lambda: theme.background_opacity() == 20))  # après la courte pause / after the short pause
        self.assertIn(theme.colors(window.dark, True, 20)["card"], window.styleSheet())
        self.assertEqual(window.settings.value("background_opacity", type=int), 20)
        self.assertTrue(page.opacity_reset.isEnabled())
        page.opacity_reset.click()
        self.assertTrue(self.wait_until(lambda: theme.background_opacity() == theme.DEFAULT_OPACITY))
        window.close()

    def test_saved_opacity_is_applied_at_startup(self):
        self.addCleanup(theme.set_background_opacity, theme.DEFAULT_OPACITY)
        main_window.open_settings().setValue("background_opacity", 85)
        window = self.checkbox_window()
        self.assertEqual(theme.background_opacity(), 85)
        self.assertEqual(window.settings_page.opacity_slider.value(), 85)
        window.close()

    def test_settings_update_options_match_the_update_window(self):
        window, page = self.settings_window()
        page.auto_check.setChecked(False)
        self.assertFalse(page.auto_install.isEnabled())
        self.assertFalse(window.settings.value("update_auto_check", True, type=bool))
        with patch.object(main_window.UpdateDialog, "exec", return_value=0), \
                patch.object(updater, "check_latest", return_value=self.info(newer=False)):
            page.check_now.click()  # ouvre la fenêtre des mises à jour / opens the update window
            window.settings.setValue("update_auto_check", True)  # comme si la fenêtre l'avait changé / as if the window changed it
        window.settings_page.load()
        self.assertTrue(page.auto_check.isChecked())
        window.close()

    # --- Fenêtre « Nouveautés » / "What's new" window ------------------------------------------------------

    def current_info(self, **changes):
        base = dict(version=main_window.__version__, tag="v" + main_window.__version__, newer=False,
                    published="2026-11-02T10:08:36Z", notes=self.BILINGUAL_NOTES.replace("v1.0.2", "v" + main_window.__version__))
        base.update(changes)
        return self.info(**base)

    def test_release_publication_date_is_shown_locally(self):
        self.assertEqual(self.info(published="2026-11-02T10:08:36Z").published_date, "02/11/2026")
        self.assertEqual(self.info(published="").published_date, "")
        self.assertEqual(self.info(published="hier").published_date, "")

    def test_whats_new_shows_the_installed_version_notes(self):
        dialog = update_ui.WhatsNewDialog(None, False, "fr", main_window.open_settings(), self.current_info())
        title = i18n.STRINGS["fr"]["whats_new_title"].format(version=main_window.__version__)
        self.assertEqual((dialog.windowTitle(), dialog.heading.text()), (title, title))
        self.assertEqual(dialog.date_label.text(), "Publiée le 02/11/2026")
        self.assertIn("Nouveautés", dialog.notes.toPlainText())
        self.assertNotIn("Fixes", dialog.notes.toPlainText())  # langue de l'interface seulement / interface language only
        self.assertTrue(dialog.notice.isHidden())
        self.assertTrue(dialog.update_button.isHidden())
        with patch.object(update_ui.QDesktopServices, "openUrl") as open_url:
            dialog.github_button.click()
        self.assertEqual(open_url.call_args.args[0].toString(), updater.RELEASES_URL)
        dialog.close()

    def test_whats_new_offers_the_update_when_the_release_is_newer(self):
        dialog = update_ui.WhatsNewDialog(None, False, "en", main_window.open_settings(), self.info(version="9.0.0", tag="v9.0.0"))
        self.assertFalse(dialog.notice.isHidden())
        self.assertIn(main_window.__version__, dialog.notice.text())
        dialog.update_button.click()
        self.assertTrue(dialog.wants_update)
        self.assertEqual(dialog.result(), main_window.QDialog.DialogCode.Accepted)

    def test_whats_new_loads_on_demand_and_reports_errors(self):
        with patch.object(updater, "check_latest", return_value=self.current_info()):
            dialog = update_ui.WhatsNewDialog(None, False, "fr", main_window.open_settings())
            self.assertEqual(dialog.notes.toPlainText(), i18n.STRINGS["fr"]["whats_new_loading"])
            self.assertTrue(self.wait_until(lambda: dialog.info is not None))
        self.assertIn(main_window.__version__, dialog.windowTitle())
        dialog.close()
        with patch.object(updater, "check_latest", side_effect=updater.UpdateError("update_err_rate")):
            dialog = update_ui.WhatsNewDialog(None, False, "fr", main_window.open_settings())
            self.assertTrue(self.wait_until(lambda: i18n.STRINGS["fr"]["update_err_rate"] in dialog.notes.toPlainText()))
        self.assertTrue(dialog.github_button.isEnabled())
        dialog.close()

    def test_whats_new_option_is_remembered(self):
        dialog = update_ui.WhatsNewDialog(None, False, "fr", main_window.open_settings(), self.current_info())
        self.assertTrue(dialog.auto_show.isChecked())  # actif par défaut / on by default
        dialog.auto_show.setChecked(False)
        self.assertFalse(main_window.open_settings().value("whats_new_show", True, type=bool))
        dialog.close()

    def test_whats_new_appears_once_on_the_first_start_of_a_version(self):
        window = self.startup_window(self.current_info())
        with patch.object(window, "show_whats_new") as whats_new, patch.object(window, "show_updates") as updates:
            for _ in range(2):
                window.startup_update_check()
                self.assertTrue(self.wait_until(lambda: window._update_task is None))
        whats_new.assert_called_once()
        self.assertEqual(whats_new.call_args.args[0].version, main_window.__version__)
        updates.assert_not_called()
        self.assertEqual(window.settings.value("whats_new_seen"), main_window.__version__)
        window.close()

    def test_whats_new_respects_its_option_and_leaves_room_for_the_update_prompt(self):
        window = self.startup_window(self.current_info())
        window.settings.setValue("whats_new_show", False)
        with patch.object(window, "show_whats_new") as whats_new:
            window.startup_update_check()
            self.assertTrue(self.wait_until(lambda: window._update_task is None))
        whats_new.assert_not_called()
        window.close()
        # Une version plus récente existe : la proposition de mise à jour, pas les nouveautés (une fenêtre à la fois).
        # A newer version exists: the update offer, not what's new (one window at a time).
        window = self.startup_window(self.info(version="9.0.0", tag="v9.0.0"))
        with patch.object(window, "show_whats_new") as whats_new, patch.object(window, "show_updates") as updates:
            window.startup_update_check()
            self.assertTrue(self.wait_until(lambda: updates.called))
        whats_new.assert_not_called()
        self.assertEqual(window.settings.value("whats_new_seen", ""), "")  # reste à montrer / still to show
        window.close()

    def test_whats_new_alone_never_offers_an_update(self):
        window = self.startup_window(self.info(version="9.0.0", tag="v9.0.0"))
        window.settings.setValue("update_auto_check", False)
        with patch.object(window, "show_whats_new") as whats_new, patch.object(window, "show_updates") as updates:
            window.startup_update_check()  # requête faite pour les nouveautés / request made for what's new
            self.assertTrue(self.wait_until(lambda: window._update_task is None))
        whats_new.assert_not_called()
        updates.assert_not_called()
        window.close()

    def test_whats_new_can_lead_to_the_update_window(self):
        window = self.checkbox_window()
        def accept_update(dialog):
            dialog.wants_update = True
            dialog.info = self.info(version="9.0.0", tag="v9.0.0")
            return 1
        with patch.object(main_window.WhatsNewDialog, "exec", accept_update), \
                patch.object(window, "show_updates") as updates, \
                patch.object(updater, "check_latest", return_value=self.info(version="9.0.0", tag="v9.0.0")):
            window.show_whats_new(self.info(version="9.0.0", tag="v9.0.0"))
        updates.assert_called_once()
        self.assertEqual(window.update_info.version, "9.0.0")
        window.close()

    def test_settings_open_whats_new_and_remember_its_option(self):
        window = self.checkbox_window()
        page = window.settings_page
        self.assertTrue(page.whats_new_auto.isChecked())
        page.whats_new_auto.setChecked(False)
        self.assertFalse(window.settings.value("whats_new_show", True, type=bool))
        self.assertEqual(page.whats_new_btn.text(), window.t("settings_whats_new_btn"))
        with patch.object(main_window.WhatsNewDialog, "exec", return_value=0) as run, \
                patch.object(updater, "check_latest", return_value=self.current_info()):
            page.whats_new_btn.click()
        run.assert_called_once()
        window.close()

    def test_a_closed_window_ignores_a_late_github_reply(self):
        window = self.startup_window(self.current_info())
        release = threading.Event()
        def slow():
            release.wait(5)
            return self.current_info()
        with patch.object(updater, "check_latest", side_effect=slow), \
                patch.object(window, "show_whats_new") as whats_new, patch.object(window, "show_updates") as updates:
            window.startup_update_check()
            window.close()
            release.set()
            QTest.qWait(200)
            self.qt_app.processEvents()
        whats_new.assert_not_called()
        updates.assert_not_called()

    # --- Optimisation : défauts corrigés / Optimization: fixed defects ----------------------------------

    def test_startup_opens_nothing_over_an_open_window(self):
        window = self.startup_window(self.info(version="9.0.0", tag="v9.0.0"))
        with patch.object(main_window.QApplication, "activeModalWidget", return_value=object()), \
                patch.object(window, "show_whats_new") as whats_new, patch.object(window, "show_updates") as updates:
            window.startup_update_check()
            self.assertTrue(self.wait_until(lambda: window._update_task is None))
        whats_new.assert_not_called()
        updates.assert_not_called()
        self.assertEqual(window.update_info.version, "9.0.0")  # l'entrée « Réglages » s'allume quand même / the entry still lights up
        self.assertEqual(window.settings.value("update_dismissed", ""), "")  # proposition encore due / offer still due
        window.close()

    def test_update_window_waits_for_a_running_automatic_download(self):
        window = self.checkbox_window()
        window._auto_downloading = True
        with patch.object(main_window, "UpdateDialog") as dialog, \
                patch.object(main_window.QMessageBox, "information") as told:
            window.show_updates()
        dialog.assert_not_called()  # un second téléchargement écraserait le premier / a second download would clobber the first
        told.assert_called_once()
        window.drop_update_task()
        self.assertFalse(window._auto_downloading)
        window.close()

    def test_automatic_install_waits_until_fastsweep_is_idle(self):
        window = self.checkbox_window()
        staged = Path("FastSweep.exe.new")
        with patch.object(window, "apply_update") as apply, patch.object(main_window.QTimer, "singleShot") as later:
            window._busy = True
            window.auto_update_downloaded(staged)
            apply.assert_not_called()
            later.assert_called_once()
            window._busy = False
            later.call_args.args[1]()  # nouvel essai / retry
        apply.assert_called_once_with(staged)
        window.close()

    def test_switching_pages_refreshes_once(self):
        window = self.checkbox_window()
        with patch.object(window, "update_copy") as copy, patch.object(window, "populate_table") as populate:
            window.switch_mode("store")
            self.assertEqual((copy.call_count, populate.call_count), (1, 1))
            window.switch_mode("store")  # déjà affichée / already shown
            self.assertEqual((copy.call_count, populate.call_count), (1, 1))
            window.switch_mode("settings")
            window.switch_mode("store")  # retour sans changer de liste / back without changing the list
            self.assertEqual((copy.call_count, populate.call_count), (3, 1))
        self.assertEqual((window.active_page, window.mode), ("apps", "store"))
        self.assertTrue(window.store_nav.isChecked() and not window.settings_nav.isChecked())
        window.close()

    def test_a_pending_opacity_change_survives_a_refresh(self):
        window = self.checkbox_window()
        page = window.settings_page
        page.opacity_slider.setValue(80)  # le redessin attend une courte pause / repaint waits for a short pause
        self.assertTrue(page.opacity_timer.isActive())
        window.toggle_theme()  # recharge les contrôles entre-temps / reloads the controls meanwhile
        self.assertEqual((page.opacity_slider.value(), window.background_opacity), (80, 80))
        window.set_background_opacity(main_window.DEFAULT_OPACITY)
        window.close()

    def test_cleanup_never_offers_shared_folders_or_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            local = Path(temporary) / "LocalAppData"
            for name in ("Microsoft", "Programs", "Temp"):
                (local / name).mkdir(parents=True)
            env = {"LOCALAPPDATA": str(local), "APPDATA": "", "PROGRAMDATA": "", "PROGRAMFILES": "", "PROGRAMFILES(X86)": ""}
            with patch.dict(os.environ, env):
                for name in ("Microsoft", "Programs"):
                    paths = [c.path for c in cleanup.file_candidates(models.AppEntry(name=name, location=str(local / name)))]
                    self.assertEqual(paths, [], name)
        keys = {"HKLM\\SOFTWARE": {}, "HKLM\\SOFTWARE\\Microsoft": {}, "HKLM\\SOFTWARE\\Classes": {}}
        with FakeRegistry(keys):
            for name in ("Microsoft", "Classes"):
                self.assertEqual(cleanup.registry_candidates(models.AppEntry(name=name)), [], name)

    def test_unreadable_history_or_dates_do_not_crash(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "history.json"
            path.write_bytes(b"\xff\xfe\x00garbage")  # ni UTF-8 ni JSON / neither UTF-8 nor JSON
            with patch.object(history, "operation_history_path", return_value=path):
                self.assertEqual(history.load_operation_history(), [])
        self.assertIsNone(restore_points.parse_creation_time("/Date(99999999999999999)/"))
        self.assertIn(self.info(published="9999-12-31T23:59:59+14:00").published_date, ("", "31/12/9999"))  # hors limites selon le fuseau / out of range depending on the zone

    def test_download_ignores_an_invalid_content_length(self):
        class Response:
            headers = {"Content-Length": "lots"}
        self.assertEqual(updater._content_length(Response()), 0)
        Response.headers = {"Content-Length": "-5"}
        self.assertEqual(updater._content_length(Response()), 0)
        Response.headers = {"Content-Length": "1234"}
        self.assertEqual(updater._content_length(Response()), 1234)

    def test_cancelling_an_update_task_silences_it(self):
        task = update_ui.UpdateTask()
        received = []
        task.done.connect(received.append)
        task.cancel()
        self.assertTrue(task.stop.is_set())
        task.done.emit("late")
        self.assertEqual(received, [])
        task.cancel()  # deux fois sans erreur / twice without error

    # --- Audit de sécurité : six correctifs / Security audit: six fixes -----------------------------------

    def test_windows_tools_are_called_by_their_full_path(self):
        with patch.object(system, "run_hidden", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            system.run_powershell("Get-Thing", timeout=5)
        self.assertEqual(run.call_args.args[0][0], system.system_tool("WindowsPowerShell", "v1.0", "powershell.exe"))
        candidate = models.RegistryCandidate(0, r"SOFTWARE\Acme", "HKCU", "high", "key-name")
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(cleanup, "registry_backup_root", return_value=Path(temporary)), \
                patch.object(cleanup, "run_hidden", return_value=subprocess.CompletedProcess([], 1, "", "no")) as run:
            with self.assertRaises(OSError):
                cleanup.backup_registry_keys([candidate])
        self.assertEqual(run.call_args.args[0][0], system.system_tool("reg.exe"))
        self.assertTrue(system.system_tool("reg.exe").lower().endswith("system32\\reg.exe"))

    def test_bare_program_names_never_resolve_to_fastsweeps_folder(self):
        with tempfile.TemporaryDirectory() as own, tempfile.TemporaryDirectory() as other:
            for folder in (own, other):
                Path(folder, "tool.exe").write_bytes(b"MZ")
            with patch.object(system.sys, "executable", os.path.join(own, "FastSweep.exe")), \
                    patch.object(system, "system_directory", return_value=os.path.join(own, "missing", "System32")), \
                    patch.dict(os.environ, {"PATH": os.pathsep.join((own, ".", other))}):
                self.assertEqual(system.trusted_executable("tool"), os.path.join(other, "tool.exe"))
                self.assertEqual(system.trusted_executable("absent.exe"), system.system_tool("absent.exe"))
            self.assertEqual(system.trusted_executable(r"C:\Tools\setup.exe"), r"C:\Tools\setup.exe")

    def test_windows_dlls_load_from_system32_only(self):
        with patch.object(system.ctypes, "WinDLL", create=True) as load:
            system.system_dll("srclient.dll", use_last_error=True)
        load.assert_called_once_with("srclient.dll", use_last_error=True, winmode=system.LOAD_LIBRARY_SEARCH_SYSTEM32)

    def test_user_declared_uninstallers_are_flagged_when_running_as_administrator(self):
        window = self.checkbox_window()
        app = models.AppEntry(name="Fake Chrome", uninstall=r"C:\Users\me\payload.exe", hive_label="HKCU", subkey="x")
        with patch.object(main_window, "is_admin", return_value=True), \
                patch.object(window, "confirm_risky", return_value=False) as risky, \
                patch.object(main_window.QMessageBox, "question") as question, \
                patch.object(window, "run_background") as run:
            window.uninstall_selected(app)
        question.assert_not_called()
        self.assertIn(r"C:\Users\me\payload.exe", risky.call_args.args[1])  # la commande est montrée / command shown
        run.assert_not_called()
        machine = models.AppEntry(name="Real", uninstall="u.exe", hive_label="HKLM", subkey="y")
        with patch.object(main_window, "is_admin", return_value=True), \
                patch.object(main_window.QMessageBox, "question", return_value=main_window.QMessageBox.StandardButton.No) as question:
            window.uninstall_selected(machine)
        question.assert_called_once()  # HKLM : question habituelle / HKLM: usual question
        with patch.object(main_window, "is_admin", return_value=False):
            self.assertEqual(window.user_entry_warning([app]), "")  # sans droits, rien à craindre / no rights, nothing to fear
        window.close()

    def scan_env(self, root: Path) -> dict:
        return {"LOCALAPPDATA": str(root / "Local"), "APPDATA": "", "PROGRAMDATA": "", "PROGRAMFILES": "",
                "PROGRAMFILES(X86)": "", "USERPROFILE": str(root / "Profile"), "TEMP": "", "TMP": "",
                "PUBLIC": "", "ONEDRIVE": "", "PROGRAMW6432": "", "COMMONPROGRAMFILES": "", "COMMONPROGRAMFILES(X86)": "",
                "SYSTEMROOT": str(root / "Windows")}

    def test_file_scan_never_follows_a_link_named_after_the_app(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Local").mkdir()
            (root / "Precious").mkdir()
            os.symlink(root / "Precious", root / "Local" / "Acme Cleaner", target_is_directory=True)
            (root / "Local" / "Acme Publisher").mkdir()
            os.symlink(root / "Precious", root / "Local" / "Vendor", target_is_directory=True)
            with patch.dict(os.environ, self.scan_env(root)):
                paths = [c.path for c in cleanup.file_candidates(models.AppEntry(name="Acme Cleaner", publisher="Vendor"))]
        self.assertEqual(paths, [])

    def test_file_scan_never_offers_personal_or_system_folders(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for folder in ("Local", "Profile/Documents", "Windows/Acme Cleaner", "Local/Acme Cleaner"):
                (root / folder).mkdir(parents=True)
            with patch.dict(os.environ, self.scan_env(root)):
                for name, location in (("Documents", root / "Profile" / "Documents"), ("Profile", root / "Profile"),
                                       ("Acme Cleaner", root / "Windows" / "Acme Cleaner")):
                    found = [c.path for c in cleanup.file_candidates(models.AppEntry(name=name, location=str(location)))]
                    self.assertNotIn(str(location.resolve()), found, name)
                found = [c.path for c in cleanup.file_candidates(models.AppEntry(name="Acme Cleaner"))]
            self.assertEqual(found, [str((root / "Local" / "Acme Cleaner").resolve())])  # le vrai résidu reste proposé / the real leftover stays
            self.assertTrue(cleanup.is_protected_folder(Path(root.anchor), set()))  # racine d'un disque / drive root

    def test_recycle_refuses_locations_without_a_recycle_bin(self):
        self.assertFalse(cleanup.drive_has_recycle_bin(r"\\server\share\Acme"))
        with patch.object(cleanup, "is_windows", return_value=True), \
                patch.object(cleanup, "drive_has_recycle_bin", return_value=False):
            with self.assertRaises(OSError) as raised:
                cleanup.recycle_path(r"E:\Acme")
        self.assertEqual(str(raised.exception), cleanup.NOT_RECYCLABLE_ERROR)

    # --- Audit de sécurité : points 8, 9 et 11 / Security audit: points 8, 9 and 11 ----------------------

    def test_install_refuses_a_file_changed_after_its_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            exe, staged = self.make_install(temporary)
            staged.write_bytes(b"MZ" + b"\x00" * 5000)  # même taille, autre contenu / same size, other content
            with self.assertRaises(updater.UpdateError) as caught:
                updater.install(staged, exe, self.info())
            self.assertEqual(caught.exception.key, "update_err_changed")
            self.assertEqual(exe.read_bytes(), b"MZ-old")  # rien d'installé / nothing installed
            self.assertFalse(staged.exists())  # fichier suspect supprimé / suspicious file deleted

    def test_apply_update_needs_the_release_to_check_against(self):
        window = self.checkbox_window()
        window.update_info = None
        with tempfile.TemporaryDirectory() as temporary:
            exe, staged = self.make_install(temporary)
            with patch.object(updater, "current_executable", return_value=exe), \
                    patch.object(main_window.QMessageBox, "critical") as critical, \
                    patch.object(window, "record_history"), patch.object(updater, "restart") as restart:
                window.apply_update(staged)
            self.assertEqual(exe.read_bytes(), b"MZ-old")
        critical.assert_called_once()
        restart.assert_not_called()
        window.close()

    def test_downloads_and_github_replies_are_capped(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "FastSweep.exe.new"
            with self.assertRaises(updater.UpdateError) as caught:
                updater.download(self.info(size=updater.MAX_DOWNLOAD + 1), target)
            self.assertEqual(caught.exception.key, "update_err_too_big")
            # Taille inconnue : le flux est coupé au plafond. / Unknown size: the stream is cut at the cap.
            response = self.FakeResponse(self.EXE)
            response.headers = {}
            with patch.object(updater, "open_url", return_value=response), patch.object(updater, "MAX_DOWNLOAD", 1000), \
                    patch.object(updater, "CHUNK", 256):
                with self.assertRaises(updater.UpdateError) as caught:
                    updater.download(self.info(size=0, sha256=""), target)
            self.assertEqual(caught.exception.key, "update_err_too_big")
            self.assertEqual(list(Path(temporary).iterdir()), [])  # rien laissé / nothing left
        with patch.object(updater, "open_url", return_value=self.FakeResponse(b"{" + b" " * 64 + b"}")), \
                patch.object(updater, "MAX_JSON", 32):
            with self.assertRaises(updater.UpdateError) as caught:
                updater.check_latest()
        self.assertEqual(caught.exception.key, "update_err_data")

    # --- Signature des mises à jour / Update signing ----------------------------------------------------

    SIG_URL = updater.DOWNLOAD_PREFIX + "v1.0.2/FastSweep.exe.sig"

    def signing_key(self):
        """Clé de test et fonction de vérification qui remplace CNG hors Windows. / Test key and a check standing in for CNG."""
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import ec, utils
        except ImportError:
            self.skipTest("cryptography absent")
        private = ec.generate_private_key(ec.SECP256R1())
        numbers = private.public_key().public_numbers()
        public = (numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")).hex()

        def sign(version, sha256):
            r, s_ = utils.decode_dss_signature(private.sign(signing.message(version, sha256), ec.ECDSA(hashes.SHA256())))
            return base64.b64encode(r.to_bytes(32, "big") + s_.to_bytes(32, "big"))

        def check(key, digest, signature):
            point = ec.EllipticCurvePublicNumbers(int.from_bytes(key[:32], "big"), int.from_bytes(key[32:], "big"),
                                                  ec.SECP256R1()).public_key()
            der = utils.encode_dss_signature(int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big"))
            try:
                point.verify(der, digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
                return True
            except InvalidSignature:
                return False
        return private, public, sign, check

    def signed(self, public, check):
        """Active la vérification avec la clé de test (CNG réel sous Windows). / Enables checking with the test key."""
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch.object(signing, "PUBLIC_KEY", public))
        if sys.platform != "win32":
            stack.enter_context(patch.object(signing, "verify_digest", check))
        return stack

    def test_signatures_are_checked_against_the_version_and_the_hash(self):
        _private, public, sign, check = self.signing_key()
        sha = hashlib.sha256(self.EXE).hexdigest()
        with self.signed(public, check):
            good = signing.decode_signature(sign("1.0.2", sha))
            self.assertTrue(signing.verify("1.0.2", sha, good))
            self.assertTrue(signing.verify("1.0.2", sha.upper(), good))
            self.assertFalse(signing.verify("1.0.3", sha, good))  # autre version / other version
            self.assertFalse(signing.verify("1.0.2", "0" * 64, good))  # autre fichier / other file
            self.assertFalse(signing.verify("1.0.2", sha, bytes([good[0] ^ 1]) + good[1:]))
            self.assertFalse(signing.verify("1.0.2", "pas une empreinte", good))
        with patch.object(signing, "PUBLIC_KEY", "zz" * 64):
            self.assertFalse(signing.verify("1.0.2", sha, good))  # clé mal formée : refus / malformed key: refused
        for bad in (b"", b"pas du base64 !", base64.b64encode(b"x" * 63)):
            with self.assertRaises(ValueError):
                signing.decode_signature(bad)
        self.assertFalse(signing.enabled())  # aucune clé par défaut dans les tests / no key by default in tests

    @unittest.skipUnless(sys.platform == "win32", "CNG : Windows uniquement / Windows only")
    def test_windows_cng_checks_real_signatures(self):
        _private, public, sign, _check = self.signing_key()
        sha = hashlib.sha256(b"abc").hexdigest()
        raw = signing.decode_signature(sign("2.0", sha))
        digest = hashlib.sha256(signing.message("2.0", sha)).digest()
        key = bytes.fromhex(public)
        self.assertTrue(signing.verify_digest(key, digest, raw))
        self.assertFalse(signing.verify_digest(key, digest, raw[:-1] + bytes([raw[-1] ^ 1])))
        self.assertFalse(signing.verify_digest(b"\x01" * 64, digest, raw))  # point hors courbe / point off the curve

    def signed_download(self, info, signature, temporary):
        target = Path(temporary) / "FastSweep.exe.new"
        def answer(request):
            if request.full_url == self.SIG_URL:
                return self.FakeResponse(signature)
            return self.FakeResponse(self.EXE)
        error = None
        with patch.object(updater, "open_url", side_effect=answer):
            try:
                updater.download(info, target)
            except updater.UpdateError as exc:
                error = exc.key
        return error, sorted(path.name for path in Path(temporary).iterdir()), target

    def test_signed_updates_need_a_valid_signature(self):
        _private, public, sign, check = self.signing_key()
        sha = hashlib.sha256(self.EXE).hexdigest()
        with self.signed(public, check):
            data = self.release_json()
            with self.with_release(data):
                self.assertFalse(updater.check_latest("1.0.1").installable)  # signature pas encore jointe / not attached yet
            data["assets"].append({"name": "FastSweep.exe.sig", "state": "uploaded", "size": 89, "browser_download_url": self.SIG_URL})
            with self.with_release(data):
                info = updater.check_latest("1.0.1")
            self.assertEqual((info.signature_url, info.installable), (self.SIG_URL, True))
            for label, signature, changes, key in (
                ("bonne", sign("1.0.2", sha), {}, None),
                ("autre version", sign("1.0.1", sha), {}, "update_err_signature"),
                ("illisible", b"!!!", {}, "update_err_signature"),
                ("trop grande", b"A" * 5000, {}, "update_err_signature"),
                ("absente", sign("1.0.2", sha), dict(signature_url=""), "update_err_unsigned"),
            ):
                with self.subTest(label), tempfile.TemporaryDirectory() as temporary:
                    error, leftovers, target = self.signed_download(self.info(**{"signature_url": self.SIG_URL, **changes}), signature, temporary)
                    self.assertEqual(error, key)
                    if key is None:
                        self.assertEqual(leftovers, ["FastSweep.exe.new", "FastSweep.exe.new.sig"])
                        exe = Path(temporary) / "FastSweep.exe"
                        exe.write_bytes(b"MZ-old")
                        updater.install(target, exe, self.info(signature_url=self.SIG_URL))
                        self.assertEqual(exe.read_bytes(), self.EXE)
                        self.assertFalse(updater.signature_path(target).exists())  # retirée avec le fichier / removed with the file
                    else:
                        self.assertEqual(leftovers, [])  # rien gardé / nothing kept

    def test_install_checks_the_signature_again(self):
        _private, public, sign, check = self.signing_key()
        sha = hashlib.sha256(self.EXE).hexdigest()
        with self.signed(public, check):
            for label, signature in (("modifiée", sign("1.0.3", sha)), ("supprimée", None)):
                with self.subTest(label), tempfile.TemporaryDirectory() as temporary:
                    exe, staged = self.make_install(temporary)
                    if signature is not None:
                        updater.signature_path(staged).write_bytes(signature)
                    with self.assertRaises(updater.UpdateError) as caught:
                        updater.install(staged, exe, self.info())
                    self.assertEqual(caught.exception.key, "update_err_changed")
                    self.assertEqual(sorted(path.name for path in Path(temporary).iterdir()), ["FastSweep.exe"])
                    self.assertEqual(exe.read_bytes(), b"MZ-old")

    def test_the_release_tool_signs_what_fastsweep_checks(self):
        private, public, _sign, check = self.signing_key()
        from cryptography.hazmat.primitives import serialization
        import importlib.util
        spec = importlib.util.spec_from_file_location("release_signing", Path(__file__).resolve().parents[1] / "tools" / "release_signing.py")
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()).decode()
        self.assertEqual(tool.message("1.2.3", "AB" * 32), signing.message("1.2.3", "AB" * 32))  # même format / same format
        with tempfile.TemporaryDirectory() as temporary:
            module = Path(temporary) / "signing.py"
            module.write_text('PUBLIC_KEY = ""\n', encoding="utf-8")
            exe = Path(temporary) / "FastSweep.exe"
            exe.write_bytes(self.EXE)
            with patch.object(tool, "SIGNING_MODULE", module), patch.dict(os.environ, {"FASTSWEEP_SIGNING_KEY": pem}), \
                    patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit):
                    tool.sign(str(exe))  # pas de clé publique dans FastSweep : refus / no public key in FastSweep: refused
                module.write_text(f'PUBLIC_KEY = "{"ab" * 64}"\n', encoding="utf-8")
                with self.assertRaises(SystemExit):
                    tool.sign(str(exe))  # clé publique d'une autre clé / public key of another key
                module.write_text(f'PUBLIC_KEY = "{public}"\n', encoding="utf-8")
                with patch("sys.stdout", io.StringIO()):
                    tool.sign(str(exe))
            raw = signing.decode_signature(exe.with_name("FastSweep.exe.sig").read_bytes())
        with self.signed(public, check):
            from fastsweep import __version__
            self.assertTrue(signing.verify(__version__, hashlib.sha256(self.EXE).hexdigest(), raw))

    REG_HEADER = "Windows Registry Editor Version 5.00\r\n\r\n"

    def reg_bytes(self, *keys):
        text = self.REG_HEADER + "".join(f"[{key}]\r\n\"Value\"=\"1\"\r\n\r\n" for key in keys)
        return b"\xff\xfe" + text.encode("utf-16-le")  # comme reg.exe export / like reg.exe export

    def test_registry_backups_restored_as_administrator_must_be_trusted(self):
        user_only = self.reg_bytes(r"HKEY_CURRENT_USER\Software\Acme", r"-HKEY_CURRENT_USER\Software\Old")
        machine = self.reg_bytes(r"HKEY_CURRENT_USER\Software\Acme", r"HKEY_LOCAL_MACHINE\SOFTWARE\Acme")
        self.assertEqual(cleanup.reg_file_hives(machine), ["HKEY_CURRENT_USER", "HKEY_LOCAL_MACHINE"])
        with patch.object(cleanup, "trusted_backup_registered", return_value=False):
            self.assertTrue(cleanup.backup_is_trusted(user_only))  # rien qu'un programme sans droits ne puisse écrire / nothing a program without rights could not write
            self.assertFalse(cleanup.backup_is_trusted(machine))
            self.assertFalse(cleanup.backup_is_trusted(b""))
        with patch.object(cleanup, "trusted_backup_registered", side_effect=lambda digest: digest == hashlib.sha256(machine).hexdigest()):
            self.assertTrue(cleanup.backup_is_trusted(machine))  # faite par FastSweep en administrateur / made by FastSweep as administrator
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "01-Acme.reg"
            path.write_bytes(machine)
            done = subprocess.CompletedProcess([], 0, "", "")
            with patch.object(cleanup, "is_admin", return_value=True), \
                    patch.object(cleanup, "trusted_backup_registered", return_value=False), \
                    patch.object(cleanup, "run_hidden", return_value=done) as run:
                self.assertEqual(cleanup.restore_registry_backup(str(path)), (False, cleanup.UNTRUSTED_BACKUP_ERROR))
                run.assert_not_called()
            with patch.object(cleanup, "is_admin", return_value=False), \
                    patch.object(cleanup, "run_hidden", return_value=done) as run:
                self.assertEqual(cleanup.restore_registry_backup(str(path)), (True, ""))  # sans droits : aucun risque / without rights: no risk
            self.assertEqual(run.call_args.args[0][:2], [system.system_tool("reg.exe"), "import"])

    def test_backups_made_as_administrator_are_registered(self):
        candidate = models.RegistryCandidate(cleanup.winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Acme", "HKLM", "high", "key-name")
        def export(args, timeout):
            Path(args[3]).write_bytes(self.reg_bytes(r"HKEY_LOCAL_MACHINE\SOFTWARE\Acme"))
            return subprocess.CompletedProcess(args, 0, "", "")
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(cleanup, "registry_backup_root", return_value=Path(temporary)), \
                patch.object(cleanup, "run_hidden", side_effect=export), \
                patch.object(cleanup, "is_admin", return_value=True), \
                patch.object(cleanup, "register_trusted_backups") as register:
            folder = cleanup.backup_registry_keys([candidate])
            files = list(register.call_args.args[0])
            self.assertEqual(files, [folder / "01-Acme.reg"])

    # --- Fenêtre « À propos » / About window ------------------------------------------------------------

    def test_about_diagnostics_name_the_version_and_the_environment(self):
        text = about.diagnostics_text()
        self.assertIn(f"FastSweep {about.__version__}", text)
        for label in ("Windows:", "Python:", "Qt:", "Administrator:"):
            self.assertIn(label, text)
        self.assertEqual(about.REPO_URL, "https://github.com/Bulls64/FastSweep")
        self.assertEqual(about.ISSUES_URL, about.REPO_URL + "/issues/new")
        self.assertEqual(about.environment()["mode"], "source")  # les tests tournent avec python / tests run with python

    def test_about_section_shows_version_license_and_repository_in_both_languages(self):
        window = self.checkbox_window()
        section = window.settings_page.about
        for lang in ("fr", "en"):
            if window.lang != lang:
                window.toggle_language()
            texts = " | ".join(label.text() for label in section.findChildren(about.QLabel))
            for expected in (about.__version__, "MIT", about.REPO_URL, about.AUTHOR):
                self.assertIn(expected, texts)
            self.assertIn(i18n.STRINGS[lang]["about_tagline"], texts)
            self.assertNotIn("Clair", texts)
            self.assertEqual(window.settings_page.about_label.text(), i18n.STRINGS[lang]["about"])
        window.close()

    def test_about_buttons_open_the_repository_and_copy_the_details(self):
        window = self.checkbox_window()
        section = window.settings_page.about
        with patch.object(about.QDesktopServices, "openUrl") as open_url:
            section.repo_button.click()
            section.issue_button.click()
        self.assertEqual([call.args[0].toString() for call in open_url.call_args_list], [about.REPO_URL, about.ISSUES_URL])
        section.copy_button.click()
        self.assertEqual(about.QGuiApplication.clipboard().text(), about.diagnostics_text())
        self.assertEqual(section.copy_button.text(), window.t("about_copied"))
        section.restore_copy_label()
        self.assertEqual(section.copy_button.text(), window.t("about_copy"))
        window.close()

    def test_sidebar_no_longer_has_updates_and_about_entries(self):
        window = self.checkbox_window()
        texts = [button.text() for button in window.sidebar.findChildren(main_window.NavButton)]
        self.assertIn(window.t("settings"), texts)
        self.assertNotIn(window.t("about"), texts)
        self.assertFalse(hasattr(window, "update_btn") or hasattr(window, "about_btn"))
        with patch.object(main_window.UpdateDialog, "exec", return_value=0) as run, \
                patch.object(updater, "check_latest", return_value=self.info(newer=False)):
            window.switch_mode("settings")
            window.settings_page.check_now.click()  # les mises à jour s'ouvrent depuis les Réglages / updates open from Settings
        run.assert_called_once()
        window.close()

class FakeRegistry:
    """Remplace les appels winreg utilisés par registry_candidates par un arbre en mémoire.

    Les clés sont écrites avec leur préfixe de ruche, par ex. "HKLM\\SOFTWARE\\Vendor".

    Replace the winreg calls used by registry_candidates with an in-memory tree.

    Keys are written with their hive prefix, e.g. "HKLM\\SOFTWARE\\Vendor".
    """

    def __init__(self, keys: dict[str, dict[str, str]]):
        self.values = {path.casefold(): values for path, values in keys.items()}
        self.names = {path.casefold(): path for path in keys}
        self.opened: list[str] = []
        self.patches = []

    class Key:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def children(self, path):
        prefix = path.casefold() + "\\"
        return sorted(
            self.names[key][len(prefix):] for key in self.values
            if key.startswith(prefix) and "\\" not in key[len(prefix):]
        )

    def open_key(self, parent, sub_key, *_):
        if isinstance(parent, FakeRegistry.Key):
            path = f"{parent.path}\\{sub_key}"
        else:
            hive = {cleanup.winreg.HKEY_CURRENT_USER: "HKCU", cleanup.winreg.HKEY_LOCAL_MACHINE: "HKLM"}[parent]
            path = f"{hive}\\{sub_key}"
        self.opened.append(path)
        if path.casefold() not in self.values:
            raise OSError(2, "not found")
        return FakeRegistry.Key(path)

    def query_value(self, key, name):
        try:
            return self.values[key.path.casefold()][name], 1
        except KeyError:
            raise OSError(2, "value not found") from None

    def __enter__(self):
        self.patches = [
            patch.object(cleanup.winreg, "OpenKey", self.open_key),
            patch.object(cleanup.winreg, "QueryInfoKey", lambda key: (len(self.children(key.path)), 0, 0)),
            patch.object(cleanup.winreg, "EnumKey", lambda key, index: self.children(key.path)[index]),
            patch.object(cleanup.winreg, "QueryValueEx", self.query_value),
        ]
        for item in self.patches:
            item.start()
        return self

    def __exit__(self, *_):
        for item in reversed(self.patches):
            item.stop()
        return False


if __name__ == "__main__":
    unittest.main()
