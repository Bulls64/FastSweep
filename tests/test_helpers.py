import base64
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
from fastsweep import restore_page, restore_points
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
        self.assertEqual(self.launched_args("MsiExec.exe /I{ABC}"), ["MsiExec.exe", "/x{ABC}"])
        self.assertEqual(self.launched_args("MsiExec /I{ABC}"), ["MsiExec", "/x{ABC}"])
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
                self.assertIn("QMainWindow#mainWindow { background: transparent; }" if translucent else
                              f"QMainWindow#mainWindow {{ background: {theme.colors(dark)['mica']}; }}", sheet)

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
            self.assertNotIn("Clair", i18n.STRINGS[lang]["about_text"])

    # --- Scripts de build : format attendu par Windows -------------------------------------------------
    # --- Build scripts: format expected by Windows ------------------------------------------------------

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
