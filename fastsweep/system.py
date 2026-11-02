"""Utilitaires de session Windows : test de plateforme, droits administrateur, élévation, outils console masqués.

Windows session helpers: platform check, administrator rights, elevation, hidden console tools.
"""
from __future__ import annotations

import ctypes
import ntpath
import os
import shutil
import subprocess
import sys
from pathlib import Path


def is_windows() -> bool:
    return sys.platform == "win32"


# Windows cherche une DLL nommée sans chemin d'abord dans le dossier de l'exécutable : une DLL piégée posée à côté
# de FastSweep.exe (Téléchargements…) serait chargée, en administrateur le cas échéant. Seul System32 est accepté.
# Windows looks for a DLL named without a path in the executable's folder first: a rigged DLL placed next to
# FastSweep.exe (Downloads…) would be loaded, as administrator if need be. Only System32 is accepted.
LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x00000800


def system_dll(name: str, use_last_error: bool = False):
    """DLL Windows chargée depuis System32 uniquement (AttributeError hors Windows).

    Windows DLL loaded from System32 only (AttributeError outside Windows).
    """
    return ctypes.WinDLL(name, use_last_error=use_last_error, winmode=LOAD_LIBRARY_SEARCH_SYSTEM32)


def system_directory() -> str:
    """Dossier System32 donné par Windows (pas par une variable d'environnement modifiable).

    System32 folder as given by Windows (not by a changeable environment variable).
    """
    if is_windows():
        try:
            buffer = ctypes.create_unicode_buffer(260)
            if system_dll("kernel32").GetSystemDirectoryW(buffer, len(buffer)):
                return buffer.value
        except (AttributeError, OSError):
            pass
    return ntpath.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32")


def system_tool(*parts: str) -> str:
    """Chemin complet d'un outil de System32 : jamais un homonyme du dossier de FastSweep ou du dossier courant.

    Full path of a System32 tool: never a namesake from FastSweep's folder or the current folder.
    """
    return ntpath.join(system_directory(), *parts)


def trusted_executable(name: str) -> str:
    """Chemin complet d'un programme nommé sans dossier (« MsiExec.exe », « rundll32 »…).

    Full path of a program named without a folder ("MsiExec.exe", "rundll32"…).
    Windows chercherait d'abord dans le dossier de FastSweep puis dans le dossier courant ; on cherche seulement
    dans System32, Windows puis les dossiers absolus du PATH, sauf celui de FastSweep. Introuvable : System32,
    où le lancement échouera proprement plutôt que d'exécuter un homonyme.
    Windows would look in FastSweep's folder then the current folder first; only System32, Windows, then the
    absolute PATH folders except FastSweep's are searched. Not found: System32, where launching fails cleanly
    instead of running a namesake.
    """
    if ntpath.dirname(name):
        return name
    file_name = name if ntpath.splitext(name)[1] else name + ".exe"
    own_folder = os.path.normcase(os.path.dirname(os.path.abspath(sys.executable)))
    folders = [system_directory(), ntpath.dirname(system_directory())]
    folders += [folder for folder in os.environ.get("PATH", "").split(os.pathsep)
                if os.path.isabs(folder) and os.path.normcase(folder.rstrip("\\/")) != own_folder]
    for folder in folders:
        candidate = os.path.join(folder, file_name)
        if os.path.isfile(candidate):
            return candidate
    return system_tool(file_name)


def is_admin() -> bool:
    try:
        return bool(system_dll("shell32").IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    executable = sys.executable
    arguments = sys.argv[1:] if getattr(sys, "frozen", False) else [os.path.abspath(sys.argv[0]), *sys.argv[1:]]
    result = system_dll("shell32").ShellExecuteW(None, "runas", executable, subprocess.list2cmdline(arguments), os.getcwd(), 1)
    return result > 32


APP_DIR_NAME = "FastSweep"
LEGACY_DIR_NAME = "Clair"  # ancien nom de l'application / former name of the application
MIGRATION_MARKER = ".migrated-from-clair"


def app_data_dir() -> Path:
    """%LOCALAPPDATA%\\FastSweep : les sauvegardes du registre et l'historique des opérations s'y trouvent.

    %LOCALAPPDATA%\\FastSweep: registry backups and operation history live here.
    """
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / APP_DIR_NAME


def _copy_if_missing(source: str, target: str) -> None:
    if not Path(target).exists():
        shutil.copy2(source, target)


def migrate_legacy_data() -> bool:
    """Reprend l'historique et les sauvegardes du registre de l'ancien dossier ``Clair`` (copie, rien n'est supprimé).

    Brings over the history and registry backups of the former ``Clair`` folder (copy only, nothing is deleted).
    Ne fait rien si le nouveau dossier a déjà été migré ; n'écrase jamais un fichier existant.
    Does nothing once the new folder has been migrated; never overwrites an existing file.
    """
    new = app_data_dir()
    old = new.parent / LEGACY_DIR_NAME
    marker = new / MIGRATION_MARKER
    if not old.is_dir() or marker.exists():
        return False
    try:
        shutil.copytree(old, new, dirs_exist_ok=True, copy_function=_copy_if_missing)
        marker.write_text(f"copied from {old}\n", encoding="utf-8")
    except OSError:
        return False
    return True


def run_hidden(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    """Exécute un outil console sans faire apparaître de fenêtre et capture sa sortie en texte.

    Run a console tool without flashing a window and capture its output as text.
    """
    return subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


# Sans cela, Windows PowerShell écrit dans la page de code OEM (cp850 en France) : les accents des messages
# d'erreur (« Accès refusé ») et des chemins seraient illisibles une fois décodés en UTF-8.
# Without this, Windows PowerShell writes in the OEM code page (cp850 in France): accents in error
# messages ("Accès refusé") and paths would be garbled once decoded as UTF-8.
POWERSHELL_UTF8 = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"


def run_powershell(script: str, timeout: float) -> subprocess.CompletedProcess:
    return run_hidden(
        [system_tool("WindowsPowerShell", "v1.0", "powershell.exe"), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", POWERSHELL_UTF8 + script],
        timeout,
    )
