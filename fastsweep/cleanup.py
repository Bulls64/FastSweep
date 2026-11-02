"""Analyses et nettoyage des résidus : clés de registre (sauvegardées d'abord) et fichiers (Corbeille).

Leftover scans and cleanup: registry keys (backed up first) and files (Recycle Bin).
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import unicodedata
import winreg
from ctypes import wintypes
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from .inventory import FILE_ATTRIBUTE_REPARSE_POINT, SOFTWARE_ROOTS, UNINSTALL_ROOTS, registry_value
from .models import AppEntry, FileCandidate, RegistryCandidate, keep_strongest, sort_by_confidence
from .system import app_data_dir, is_windows, run_hidden


MIN_REGISTRY_MATCH_LENGTH = 2

# Dossiers et clés partagés par Windows et de nombreux programmes, sous les racines analysées : jamais proposés,
# même si une application porte exactement ce nom (« Microsoft », « Programs », « Common Files »…).
# Folders and keys shared by Windows and many programs, under the scanned roots: never offered,
# even when an application bears exactly that name ("Microsoft", "Programs", "Common Files"…).
SHARED_NAMES = frozenset({
    "microsoft", "windows", "windowsnt", "windowsapps", "packages", "programs", "temp", "commonfiles",
    "microsoftnet", "internetexplorer", "windowsdefender", "windowsmail", "windowsmediaplayer",
    "windowsphotoviewer", "windowssidebar", "windowspowershell", "modifiablewindowsapps", "crashdumps",
    "classes", "clients", "policies", "registeredapplications", "wow6432node", "odbc",
})


def registry_candidates(app: AppEntry) -> list[RegistryCandidate]:
    target = normalize_name(app.name)
    # Un nom composé uniquement de symboles serait normalisé en "" et correspondrait à toute clé dont le nom ou
    # le DisplayName est lui aussi normalisé en "" (par exemple les entrées Uninstall sans DisplayName).
    # A name made only of symbols would normalize to "" and match every key whose name or
    # DisplayName also normalizes to "" (for example Uninstall entries without DisplayName).
    if len(target) < MIN_REGISTRY_MATCH_LENGTH:
        return []
    publisher = normalize_name(app.publisher)
    found: dict[str, RegistryCandidate] = {}
    roots = [(hive, root, label, True) for hive, root, label in UNINSTALL_ROOTS]
    roots.extend((hive, root, label, False) for hive, root, label in SOFTWARE_ROOTS)

    def add(hive: int, path: str, label: str, confidence: str, reason: str) -> None:
        if normalize_name(path.rsplit("\\", 1)[-1]) in SHARED_NAMES:
            return
        candidate = RegistryCandidate(hive, path, label, confidence, reason)
        keep_strongest(found, candidate.identity, candidate)

    for hive, root, label, is_uninstall_root in roots:
        try:
            with winreg.OpenKey(hive, root) as parent:
                for index in range(winreg.QueryInfoKey(parent)[0]):
                    try:
                        name = winreg.EnumKey(parent, index)
                        key_name = normalize_name(name)
                        path = root + "\\" + name
                        if is_uninstall_root:
                            with winreg.OpenKey(parent, name) as key:
                                display_normalized = normalize_name(registry_value(key, "DisplayName"))
                            if display_normalized and display_normalized == target:
                                add(hive, path, label, "high", "display-name")
                            elif key_name == target:
                                add(hive, path, label, "medium", "key-name")
                            elif len(target) >= 7 and display_normalized and (target in display_normalized or display_normalized in target):
                                add(hive, path, label, "low", "similar-display-name")
                        elif key_name == target:
                            add(hive, path, label, "medium", "key-name")
                        elif publisher and key_name == publisher:
                            try:
                                with winreg.OpenKey(parent, name) as vendor:
                                    for child_index in range(winreg.QueryInfoKey(vendor)[0]):
                                        child = winreg.EnumKey(vendor, child_index)
                                        if normalize_name(child) == target:
                                            add(hive, path + "\\" + child, label, "high", "publisher-product")
                            except OSError:
                                pass
                    except OSError:
                        continue
        except OSError:
            continue
    return sort_by_confidence(found.values())


def registry_candidates_for(apps: list[AppEntry]) -> list[RegistryCandidate]:
    """Résidus de registre de plusieurs applications, sans doublon (la correspondance la plus forte l'emporte).

    Registry leftovers of several apps, without duplicates (the strongest match wins).
    """
    found: dict[str, RegistryCandidate] = {}
    for app in apps:
        for candidate in registry_candidates(app):
            keep_strongest(found, candidate.identity, candidate)
    return sort_by_confidence(found.values())


@lru_cache(maxsize=8192)
def normalize_name(value: str) -> str:
    """Comparable form of a name: casefolded, punctuation removed, letters and digits of any script kept.

    Accents on Latin letters are dropped (« Éditeur » == « Editeur »), while marks on other scripts are
    kept so that, for example, Japanese voiced kana are not merged with their unvoiced form.
    """
    chars: list[str] = []
    for char in unicodedata.normalize("NFD", value.casefold()):
        if unicodedata.combining(char) and chars and chars[-1].isascii():
            continue
        chars.append(char)
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFC", "".join(chars)))


def registry_backup_root() -> Path:
    return app_data_dir() / "RegistryBackups"


def backup_registry_keys(candidates: list[RegistryCandidate]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    folder = registry_backup_root() / f"cleanup-{stamp}"
    folder.mkdir(parents=True, exist_ok=False)
    manifest = []
    try:
        for number, candidate in enumerate(candidates, start=1):
            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", candidate.path.split("\\")[-1]).strip("._") or "registry-key"
            target = folder / f"{number:02d}-{safe_name}.reg"
            registry_path = f"{candidate.hive_label}\\{candidate.path}"
            result = run_hidden(["reg.exe", "export", registry_path, str(target), "/y"], timeout=30)
            if result.returncode != 0 or not target.is_file():
                raise OSError(result.stderr.strip() or f"reg.exe export failed for {candidate.path}")
            manifest.append({"file": target.name, "registry_path": registry_path})
        (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return folder
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def restore_registry_backup(path: str) -> tuple[bool, str]:
    result = run_hidden(["reg.exe", "import", path], timeout=60)
    return result.returncode == 0, result.stderr.strip()


def file_candidates(app: AppEntry) -> list[FileCandidate]:
    """Trouve les résidus probablement propres à l'application sans parcourir récursivement les disques de l'utilisateur ou du système.

    Find likely app-owned leftovers without recursively crawling user or system disks.
    """
    target = normalize_name(app.name)
    if len(target) < 4:
        return []
    publisher = normalize_name(app.publisher)
    found: dict[str, FileCandidate] = {}

    def add(path: Path, confidence: str, reason: str) -> None:
        try:
            resolved = path.resolve(strict=True)
            if resolved.name in {"", ".", ".."} or any(part.casefold() == "windowsapps" for part in resolved.parts):
                return
            if not (resolved.is_dir() or resolved.is_file()) or path_is_reparse_point(resolved):
                return
            if normalize_name(resolved.name if resolved.is_dir() else resolved.stem) in SHARED_NAMES:
                return
            keep_strongest(found, str(resolved).casefold(), FileCandidate(str(resolved), confidence, reason))
        except (OSError, ValueError):
            return

    # Le dossier d'installation enregistré est le signal le plus fort, mais on ne propose qu'un
    # dossier propre au produit ; jamais un parent partagé comme Program Files.
    # The registered install directory is the strongest signal, but only offer a
    # product-specific folder; never offer a shared parent such as Program Files.
    if app.location:
        install = Path(os.path.expandvars(app.location))
        if normalize_name(install.name) == target:
            add(install, "high", "install-location")

    local, roaming, common = (os.environ.get(name, "") for name in ("LOCALAPPDATA", "APPDATA", "PROGRAMDATA"))
    start_menu = Path("Microsoft") / "Windows" / "Start Menu" / "Programs"
    env_roots = (
        local,
        str(Path(local) / "Programs") if local else "",  # dossier d'installation habituel par utilisateur / usual per-user install folder
        roaming,
        str(Path(roaming) / start_menu) if roaming else "",  # raccourcis résiduels / leftover shortcuts
        common,
        str(Path(common) / start_menu) if common else "",
        os.environ.get("PROGRAMFILES", ""),
        os.environ.get("PROGRAMFILES(X86)", ""),
    )
    roots = [Path(os.path.expandvars(root)) for root in env_roots if root]
    for root in roots:
        if not root.is_dir():
            continue
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue  # une entrée illisible n'arrête pas l'analyse du dossier / one unreadable entry does not stop the scan
                    name = normalize_name(entry.name if is_dir else Path(entry.name).stem)
                    if name == target:
                        add(Path(entry.path), "high", "exact-name")
                    elif len(target) >= 7 and name.startswith(target) and len(name) - len(target) <= 12:
                        add(Path(entry.path), "low", "similar-name")
                    # Inspecter un niveau de produit sous un dossier d'éditeur dont le nom correspond exactement
                    # à l'éditeur ; ne jamais proposer le dossier de l'éditeur lui-même.
                    # Inspect one product level under a vendor directory with an exact
                    # publisher match; never offer the vendor directory itself.
                    elif publisher and is_dir and name == publisher:
                        try:
                            with os.scandir(entry.path) as products:
                                for product in products:
                                    if normalize_name(product.name) == target:
                                        add(Path(product.path), "medium", "exact-name")
                        except OSError:
                            continue
        except OSError:
            continue
    return sort_by_confidence(found.values())


def file_candidates_for(apps: list[AppEntry]) -> list[FileCandidate]:
    """Fichiers résiduels de plusieurs applications, sans doublon.

    Leftover files of several apps, without duplicates.
    """
    found: dict[str, FileCandidate] = {}
    for app in apps:
        for candidate in file_candidates(app):
            keep_strongest(found, candidate.path.casefold(), candidate)
    return sort_by_confidence(found.values())


def path_is_reparse_point(path: Path) -> bool:
    try:
        if hasattr(path, "is_junction") and path.is_junction():
            return True
        return bool(path.stat(follow_symlinks=False).st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT)
    except (OSError, AttributeError):
        return False


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR), ("fFlags", wintypes.WORD), ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", wintypes.LPVOID), ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def recycle_path(path: str) -> None:
    """Déplace un fichier ou un dossier vers la Corbeille Windows, sans jamais le supprimer définitivement.

    Move a file or folder to the Windows Recycle Bin, never permanently delete it.
    """
    if not is_windows():
        raise OSError("Recycle Bin operations are only available on Windows.")
    buffer = ctypes.create_unicode_buffer(path + "\0\0")
    operation = SHFILEOPSTRUCTW(
        hwnd=None, wFunc=3, pFrom=ctypes.cast(buffer, wintypes.LPCWSTR), pTo=None,
        fFlags=0x0040 | 0x0010 | 0x0004 | 0x0400, fAnyOperationsAborted=False,
        hNameMappings=None, lpszProgressTitle=None,
    )
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    file_operation = shell32.SHFileOperationW
    file_operation.argtypes = (ctypes.POINTER(SHFILEOPSTRUCTW),)
    file_operation.restype = ctypes.c_int
    result = file_operation(ctypes.byref(operation))
    if result != 0 or operation.fAnyOperationsAborted:
        raise OSError(f"SHFileOperation error 0x{result:X}" if result else "Operation aborted")


def delete_registry_tree(hive: int, path: str) -> None:
    # La suppression récursive via winreg évite d'invoquer un shell et reste sous la clé de désinstallation choisie.
    # Recursive deletion via winreg avoids invoking a shell and stays under the selected uninstall key.
    with winreg.OpenKey(hive, path, 0, winreg.KEY_READ | winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY) as key:
        while winreg.QueryInfoKey(key)[0] > 0:
            child = winreg.EnumKey(key, 0)
            delete_registry_tree(hive, path + "\\" + child)
    winreg.DeleteKeyEx(hive, path, winreg.KEY_WOW64_64KEY, 0)


def registry_key_exists(hive: int, path: str) -> bool:
    try:
        with winreg.OpenKey(hive, path, 0, winreg.KEY_READ):
            return True
    except OSError:
        return False
