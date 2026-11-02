"""Exécution des désinstalleurs : points de restauration, analyse de la commande, repli UAC, suivi des processus.

Running uninstallers: restore points, command parsing, UAC fallback, process tracking.
"""
from __future__ import annotations

import ctypes
import ntpath
import os
import re
import subprocess
import time
from ctypes import wintypes

import psutil

from .i18n import STRINGS
from .models import AppEntry
from .system import run_powershell

# Codes d'erreur Windows
# Windows error codes
ERROR_ELEVATION_REQUIRED = 740
ERROR_CANCELLED = 1223

# Codes de sortie MSI : 1641 = redémarrage lancé, 3010 = redémarrage requis. Les deux signifient un succès.
# MSI exit codes: 1641 = restart started, 3010 = restart required. Both mean success.
REBOOT_EXIT_CODES = frozenset({1641, 3010})
SUCCESS_EXIT_CODES = frozenset({0}) | REBOOT_EXIT_CODES

# Marqueurs portés par les chaînes d'erreur des threads de travail, traduits par describe_uninstall_error.
# Markers carried in error strings from worker threads, translated by describe_uninstall_error.
ELEVATION_CANCELLED_PREFIX = "elevation-cancelled:"
RESTORE_POINT_ERROR_PREFIX = "restore-point:"
NO_UNINSTALLER_ERROR = "no-uninstaller"

RESTORE_POINT_CREATED = "FASTSWEEP_RESTORE_POINT_CREATED"
RESTORE_POINT_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
function Get-LastRestorePoint {
    @(Get-ComputerRestorePoint -ErrorAction SilentlyContinue) |
        Sort-Object SequenceNumber | Select-Object -Last 1 -ExpandProperty SequenceNumber
}
# Par défaut, Windows ignore silencieusement un nouveau point de restauration si un autre a été créé
# dans les dernières 24 heures (Checkpoint-Computer n'écrit qu'un avertissement). On lève cette limite
# pour cet appel uniquement, puis on remet le réglage précédent.
# By default Windows silently skips a new restore point when one was created in the last
# 24 hours (Checkpoint-Computer only writes a warning). Lift that limit for this one call
# and put the previous setting back afterwards.
$key = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore'
$name = 'SystemRestorePointCreationFrequency'
$previous = (Get-ItemProperty -Path $key -Name $name -ErrorAction SilentlyContinue).$name
$before = Get-LastRestorePoint
Set-ItemProperty -Path $key -Name $name -Value 0 -Type DWord
try {
    Checkpoint-Computer -Description '__DESCRIPTION__' -RestorePointType MODIFY_SETTINGS -WarningAction SilentlyContinue -WarningVariable restoreWarnings
} finally {
    if ($null -eq $previous) {
        Remove-ItemProperty -Path $key -Name $name -ErrorAction SilentlyContinue
    } else {
        Set-ItemProperty -Path $key -Name $name -Value $previous -Type DWord
    }
}
$after = Get-LastRestorePoint
if ($null -ne $after -and $after -ne $before) { '__CREATED__' }
else { [Console]::Error.WriteLine(('Windows did not create a restore point. ' + ($restoreWarnings -join ' ')).Trim()); exit 2 }
""".replace("__CREATED__", RESTORE_POINT_CREATED)


UNINSTALL_RESTORE_DESCRIPTION = "FastSweep uninstall safety point"
MANUAL_RESTORE_DESCRIPTION = "FastSweep manual restore point"


def restore_point_script(description: str = UNINSTALL_RESTORE_DESCRIPTION) -> str:
    """Script de création avec la description donnée (apostrophes doublées pour PowerShell).

    Creation script with the given description (single quotes doubled for PowerShell).
    """
    return RESTORE_POINT_SCRIPT.replace("__DESCRIPTION__", description.replace("'", "''"))


def create_restore_point(description: str = UNINSTALL_RESTORE_DESCRIPTION) -> tuple[bool, str]:
    """Crée un point de restauration et confirme que Windows en a réellement ajouté un.

    Create a restore point and confirm that Windows really added one.
    """
    try:
        result = run_powershell(restore_point_script(description), timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return restore_point_outcome(result.returncode, result.stdout, result.stderr)


def restore_point_outcome(returncode: int, stdout: str, stderr: str) -> tuple[bool, str]:
    """Seule une exécution qui a affiché le marqueur de confirmation compte comme point de restauration créé.

    Only a run that printed the confirmation marker counts as a created restore point.
    """
    created = returncode == 0 and RESTORE_POINT_CREATED in stdout
    return created, "" if created else (stderr.strip() or stdout.strip())


def windows_argv(command_line: str) -> list[str]:
    argc = ctypes.c_int()
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    parse = shell32.CommandLineToArgvW
    parse.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int))
    parse.restype = ctypes.POINTER(wintypes.LPWSTR)
    pointer = parse(command_line, ctypes.byref(argc))
    if not pointer:
        raise OSError("Windows could not parse the uninstall command.")
    try:
        return [pointer[index] for index in range(argc.value)]
    finally:
        local_free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
        local_free.argtypes = (wintypes.HLOCAL,)
        local_free.restype = wintypes.HLOCAL
        local_free(pointer)


# msiexec en tête de commande, avec ou sans chemin ni guillemets. / msiexec at the start of the command, with or without path or quotes.
_MSIEXEC = re.compile(r'^\s*"?(?:[^"]*[\\/])?msiexec(?:\.exe)?"?(?=\s|$)', re.IGNORECASE)
# Options MSI qui fixent déjà le niveau d'interface. / MSI options that already set the UI level.
_MSI_UI_LEVEL = re.compile(r"(?i)(?:^|\s)[/-](?:q[nbrf]?[+!-]?|passive|quiet)(?=\s|$)")


def silent_uninstall_command(app: AppEntry) -> str:
    """Commande sans assistant pour cette application, ou "" s'il n'y en a pas.

    Command without a wizard for this app, or "" when there is none.
    La valeur QuietUninstallString du programme d'abord ; sinon, pour Windows Installer, le mode passif
    (barre de progression seule, l'invite UAC reste possible, pas de redémarrage forcé). Les autres
    désinstalleurs n'ont pas d'option silencieuse commune : on ne devine pas.
    The program's QuietUninstallString first; otherwise, for Windows Installer, passive mode
    (progress bar only, the UAC prompt can still appear, no forced restart). Other uninstallers have
    no common silent option: no guessing.
    """
    if app.quiet_uninstall:
        return app.quiet_uninstall
    command = app.uninstall.strip()
    if not _MSIEXEC.match(command):
        return ""
    if not _MSI_UI_LEVEL.search(command):
        command += " /passive /norestart"
    return command


def launch_uninstaller(command: str):
    expanded = os.path.expandvars(command.strip())
    # Les chemins d'exécutables contiennent souvent des espaces non protégés ("C:\\Program Files\\…\\unins000.exe /S") :
    # couper après le premier « .exe » plutôt qu'au premier espace.
    # Executable paths often contain unquoted spaces ("C:\\Program Files\\…\\unins000.exe /S"):
    # cut after the first ".exe" rather than at the first space.
    match = re.match(r'^"([^"]+\.exe)"\s*(.*)$', expanded, re.IGNORECASE) or re.match(
        r"^(.+?\.exe)(?:\s+(.*))?$", expanded, re.IGNORECASE)
    if match:
        executable, tail = match.group(1), (match.group(2) or "").strip()
    else:  # par ex. « MsiExec /I{GUID} » ou un fichier .bat / e.g. "MsiExec /I{GUID}" or a .bat file
        parts = windows_argv(expanded)
        if not parts:
            raise ValueError("The uninstall command is empty.")
        executable, tail = parts[0], subprocess.list2cmdline(parts[1:])
    if ntpath.basename(executable).casefold() in {"msiexec", "msiexec.exe"}:  # toujours un chemin Windows / always a Windows path
        tail = re.sub(r"(?i)^/i\b", "/x", tail, count=1)  # /I réparerait ou réinstallerait au lieu de désinstaller / /I would repair or reinstall instead
    args = windows_argv("FastSweepArgs " + tail)[1:] if tail else []
    return start_uninstaller_process(executable, args, tail)


def start_uninstaller_process(executable: str, args: list[str], parameters: str):
    """Lance directement le désinstalleur ; si Windows indique qu'il faut une élévation, passe par l'invite UAC.

    CreateProcess (utilisé par subprocess) ne peut pas afficher l'invite UAC et échoue avec l'erreur 740 pour
    les désinstalleurs dont le manifeste exige les droits administrateur (Inno Setup, NSIS, …).
    ``parameters`` est le texte d'arguments d'origine, transmis tel quel au processus élevé.

    Start the uninstaller directly; if Windows says it needs elevation, ask through the UAC prompt.

    CreateProcess (used by subprocess) cannot show the UAC prompt and fails with error 740 for
    uninstallers whose manifest requires administrator rights (Inno Setup, NSIS, …).
    ``parameters`` is the original argument text, passed as is to the elevated process.
    """
    try:
        return subprocess.Popen([executable, *args], shell=False)
    except OSError as exc:
        if getattr(exc, "winerror", None) != ERROR_ELEVATION_REQUIRED:
            raise
    return shell_execute_elevated(executable, parameters)


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
    ]


class ElevatedProcess:
    """Enveloppe minimale façon Popen (pid, wait) autour d'un processus lancé via l'invite UAC.

    Minimal Popen-like wrapper (pid, wait) around a process started through the UAC prompt.
    """

    def __init__(self, handle: int):
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.GetProcessId.argtypes = (wintypes.HANDLE,)
        self._kernel32.GetProcessId.restype = wintypes.DWORD
        self._kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self._kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self._kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        self._kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._handle = handle
        self.pid = int(self._kernel32.GetProcessId(handle))
        self.returncode: int | None = None

    def wait(self) -> int:
        if self.returncode is not None:
            return self.returncode
        self._kernel32.WaitForSingleObject(self._handle, 0xFFFFFFFF)
        code = wintypes.DWORD()
        try:
            if not self._kernel32.GetExitCodeProcess(self._handle, ctypes.byref(code)):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self._kernel32.CloseHandle(self._handle)
        self.returncode = int(code.value)
        return self.returncode


def shell_execute_elevated(executable: str, parameters: str) -> ElevatedProcess:
    """Exécute un programme avec le verbe « runas » (invite UAC) et garde un handle pour l'attendre.

    Run an executable with the "runas" verb (UAC prompt) and keep a handle to wait for it.
    """
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
    ole32.CoInitializeEx.restype = ctypes.c_long
    execute = shell32.ShellExecuteExW
    execute.argtypes = (ctypes.POINTER(SHELLEXECUTEINFOW),)
    execute.restype = wintypes.BOOL
    # ShellExecuteEx attend COM sur le thread appelant ; ceci s'exécute dans un thread de travail.
    # ShellExecuteEx expects COM on the calling thread; this runs in a worker thread.
    com_result = ole32.CoInitializeEx(None, 0x2 | 0x4)  # COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE
    try:
        info = SHELLEXECUTEINFOW()
        info.cbSize = ctypes.sizeof(info)
        info.fMask = 0x00000040 | 0x00000100  # SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
        info.lpVerb = "runas"
        info.lpFile = executable
        info.lpParameters = parameters or None
        info.lpDirectory = None
        info.nShow = 1  # SW_SHOWNORMAL
        if not execute(ctypes.byref(info)):
            error = ctypes.get_last_error()
            if error == ERROR_CANCELLED:
                raise RuntimeError(ELEVATION_CANCELLED_PREFIX + ntpath.basename(executable))
            raise ctypes.WinError(error)
    finally:
        if com_result >= 0:
            ole32.CoUninitialize()
    if not info.hProcess:
        raise OSError("Windows did not return a handle for the elevated uninstaller.")
    return ElevatedProcess(info.hProcess)


def describe_uninstall_error(error: str, lang: str) -> str:
    """Transforme les marqueurs d'erreur internes en un message dans la langue de l'interface.

    Turn internal error markers into a message in the interface language.
    """
    strings = STRINGS[lang]
    if error.startswith(ELEVATION_CANCELLED_PREFIX):
        return strings["elevation_cancelled"].format(name=error.removeprefix(ELEVATION_CANCELLED_PREFIX))
    if error.startswith(RESTORE_POINT_ERROR_PREFIX):
        detail = error.removeprefix(RESTORE_POINT_ERROR_PREFIX).strip()
        return strings["restore_failed"] + "\n\n" + (detail or strings["ask_restore"])
    if error == NO_UNINSTALLER_ERROR:
        return strings["no_uninstaller"]
    return error


def _ps_literal(value: str) -> str:
    """Chaîne PowerShell entre apostrophes (les apostrophes internes sont doublées). / Single-quoted PowerShell string."""
    return "'" + value.replace("'", "''") + "'"


def store_uninstall_script(app: AppEntry) -> str:
    """Script PowerShell qui retire un package du Store pour le compte courant.

    PowerShell script that removes a Store package for the current account.
    Get-AppxPackage n'a pas de paramètre -Package : PowerShell complétait cette abréviation en -PackageTypeFilter et
    rejetait le nom complet du package. On cherche par -Name (le nom court), puis on retient le nom complet exact ;
    Remove-AppxPackage, lui, prend bien -Package avec le nom complet.
    Get-AppxPackage has no -Package parameter: PowerShell expanded that abbreviation to -PackageTypeFilter and
    rejected the package full name. Look up by -Name (the short name), then keep the exact full name;
    Remove-AppxPackage does take -Package with the full name.
    """
    full_name = _ps_literal(app.package_full_name)
    lookup = f"Get-AppxPackage -Name {_ps_literal(app.package_name)}" if app.package_name else "Get-AppxPackage"
    return (
        "$ErrorActionPreference = 'Stop'\n"
        f"$package = @({lookup} | Where-Object {{ $_.PackageFullName -eq {full_name} }})\n"
        "if ($package.Count -eq 0) { throw 'Package is no longer installed for this account.' }\n"
        "Remove-AppxPackage -Package $package[0].PackageFullName -ErrorAction Stop\n"
    )


def perform_uninstall(app: AppEntry, create_restore: bool, silent: bool = False) -> dict:
    """Désinstalle une application ; ``silent`` utilise la commande sans assistant quand elle existe.

    Uninstall an app; ``silent`` uses the command without a wizard when there is one.
    """
    restored = False
    if create_restore:
        restored, detail = create_restore_point()
        if not restored:
            raise RuntimeError(RESTORE_POINT_ERROR_PREFIX + detail)
    if app.source == "store":
        result = run_powershell(store_uninstall_script(app), timeout=180)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "Package removal failed.")
        return {"restored": restored, "source": "store", "exit_code": 0, "silent": True}  # le Store n'a pas d'assistant / no wizard
    if not app.uninstall:
        raise RuntimeError(NO_UNINSTALLER_ERROR)
    quiet = silent_uninstall_command(app) if silent else ""
    process = launch_uninstaller(quiet or app.uninstall)
    return {"restored": restored, "source": "classic", "exit_code": wait_for_process_tree(process), "silent": bool(quiet)}


def uninstall_status(exit_code: int) -> str:
    """Statut d'historique d'un désinstalleur terminé.

    History status of a finished uninstaller.
    """
    return "success" if exit_code in SUCCESS_EXIT_CODES else "partial"


def _parent_map() -> dict[int, int]:
    """pid → pid du parent pour tous les processus, en un seul instantané quand psutil le permet.

    pid → parent pid for every process, in a single snapshot when psutil allows it.
    Sous Windows, psutil.Process.ppid() relit toute la liste des processus à chaque appel : l'appeler pour chaque
    processus rend un parcours si lent (≈ 0,3 s) qu'un enfant lancé pendant ce temps pouvait passer inaperçu.
    On Windows, psutil.Process.ppid() reads the whole process list on every call: calling it for each process
    makes a scan so slow (≈ 0.3 s) that a child started meanwhile could go unnoticed.
    """
    snapshot = getattr(getattr(psutil, "_psplatform", None), "ppid_map", None)
    if callable(snapshot):
        try:
            return dict(snapshot())
        except Exception:
            pass  # repli public ci-dessous / public fallback below
    return {proc.info["pid"]: proc.info["ppid"] for proc in psutil.process_iter(("pid", "ppid"))}


def _create_time(pid: int) -> float:
    return psutil.Process(pid).create_time()


def wait_for_process_tree(process, interval: float = 0.35) -> int:
    """Attend le désinstalleur et les processus qu'il a lancés (beaucoup de désinstalleurs se relancent eux-mêmes).

    Wait for the uninstaller and the processes it launched (many uninstallers relaunch themselves).
    """
    if not process.pid:
        # Le PID 0 est le processus System Idle : ne jamais le suivre (il ne se « terminerait » jamais).
        # PID 0 is the System Idle Process: never track it (it would never "finish").
        return process.wait()
    try:
        tracked = {process.pid: _create_time(process.pid)}  # pid -> heure de démarrage / pid -> start time
    except psutil.Error:
        return process.wait()

    died: dict[int, float] = {}  # pid -> heure où on l'a vu terminé / pid -> time we saw it finished

    def discover() -> None:
        """Ajoute les descendants des processus suivis, jusqu'à ne plus en trouver de nouveaux.

        Adds the descendants of the tracked processes, until no new one turns up.
        Windows réutilise vite les numéros de processus : un processus quelconque qui prend le numéro d'un suivi
        déjà terminé a des enfants qui ne sont pas les nôtres. Un enfant d'un parent terminé n'est donc retenu que
        s'il a démarré avant qu'on constate la fin de ce parent.
        Windows reuses process ids quickly: an unrelated process that takes the id of an already finished tracked
        one has children that are not ours. A child of a finished parent is therefore only kept if it started
        before we saw that parent finish.
        """
        try:
            parents = _parent_map()
        except (psutil.Error, OSError):
            return
        added = True
        while added:  # petits-enfants compris / grandchildren included
            added = False
            for pid, ppid in parents.items():
                parent_started = tracked.get(ppid)
                if parent_started is None or pid in tracked:
                    continue
                try:
                    child_started = _create_time(pid)
                except psutil.Error:
                    continue
                if ppid in died and child_started > died[ppid]:
                    continue  # né après la fin du parent : numéro réutilisé / born after the parent ended: reused id
                if child_started >= parent_started:  # pas un ancien processus ayant réutilisé le pid du parent / not an older process that reused the parent's pid
                    tracked[pid] = child_started
                    added = True

    def running() -> bool:
        alive = False
        for pid, started in tracked.items():
            if _still_running(pid, started):
                alive = True
            else:
                died.setdefault(pid, time.time())
        return alive

    while True:
        discover()
        # Tout semble fini : un enfant lancé juste avant la fin de son parent a pu échapper au parcours précédent.
        # Un dernier parcours, fait après avoir vu le parent terminé, le trouve forcément s'il tourne encore.
        # Everything looks finished: a child started just before its parent ended may have escaped the previous scan.
        # One last scan, made after seeing the parent finished, is bound to find it if it is still running.
        if not running():
            discover()
            if not running():
                return process.wait()
        time.sleep(interval)


def _still_running(pid: int, started: float) -> bool:
    try:
        candidate = psutil.Process(pid)
        return abs(candidate.create_time() - started) < 0.01 and candidate.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False
