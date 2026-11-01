"""Utilitaires de session Windows : test de plateforme, droits administrateur, élévation, outils console masqués.

Windows session helpers: platform check, administrator rights, elevation, hidden console tools.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path


def is_windows() -> bool:
    return sys.platform == "win32"


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    executable = sys.executable
    arguments = sys.argv[1:] if getattr(sys, "frozen", False) else [os.path.abspath(sys.argv[0]), *sys.argv[1:]]
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, subprocess.list2cmdline(arguments), os.getcwd(), 1)
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
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", POWERSHELL_UTF8 + script],
        timeout,
    )
