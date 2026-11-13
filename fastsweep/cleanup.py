"""Analyses et nettoyage des résidus : clés de registre (sauvegardées d'abord) et fichiers (Corbeille).

Leftover scans and cleanup: registry keys (backed up first) and files (Recycle Bin).
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import unicodedata
import uuid
import winreg
from ctypes import wintypes
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from .inventory import FILE_ATTRIBUTE_REPARSE_POINT, SOFTWARE_ROOTS, UNINSTALL_ROOTS, registry_value
from .models import AppEntry, FileCandidate, RegistryCandidate, keep_strongest, sort_by_confidence
from .system import app_data_dir, is_admin, is_windows, run_hidden, system_dll, system_tool


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
            result = run_hidden([system_tool("reg.exe"), "export", registry_path, str(target), "/y"], timeout=30)
            if result.returncode != 0 or not target.is_file():
                raise OSError(result.stderr.strip() or f"reg.exe export failed for {candidate.path}")
            manifest.append({"file": target.name, "registry_path": registry_path})
        (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        if is_admin():
            register_trusted_backups(folder / entry["file"] for entry in manifest)
        return folder
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


# Le dossier des sauvegardes s'écrit sans droits : un programme malveillant pourrait y modifier un fichier .reg pour
# qu'une restauration faite en administrateur écrive dans HKLM. Les empreintes des sauvegardes faites en
# administrateur sont donc notées dans HKLM, que seul un administrateur peut modifier.
# The backup folder is writable without rights: a malicious program could change a .reg file there so that a
# restore made as administrator writes to HKLM. The fingerprints of backups made as administrator are therefore
# recorded in HKLM, which only an administrator can change.
TRUSTED_BACKUPS_KEY = r"SOFTWARE\FastSweep\RegistryBackups"
UNTRUSTED_BACKUP_ERROR = "untrusted-backup"  # traduit par l'interface / translated by the interface


def file_sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def register_trusted_backups(files) -> None:
    """Note dans HKLM l'empreinte de sauvegardes faites en administrateur (au mieux : un échec n'empêche rien).

    Records in HKLM the fingerprint of backups made as administrator (best effort: a failure prevents nothing).
    """
    try:
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, TRUSTED_BACKUPS_KEY, 0,
                                winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY) as key:
            for path in files:
                winreg.SetValueEx(key, file_sha256(path), 0, winreg.REG_SZ, Path(path).name)
    except (AttributeError, OSError):
        pass


def trusted_backup_registered(digest: str) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, TRUSTED_BACKUPS_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            winreg.QueryValueEx(key, digest)
            return True
    except (AttributeError, OSError):
        return False


def reg_file_hives(data: bytes) -> list[str]:
    """Ruches que vise un fichier .reg (une par en-tête de clé, « [HKEY_…\\…] » ou « [-HKEY_…] »).

    Hives a .reg file targets (one per key header, "[HKEY_…\\…]" or "[-HKEY_…]").
    """
    text = data.decode("utf-16", errors="replace") if data[:2] in (b"\xff\xfe", b"\xfe\xff") \
        else data.decode("utf-8", errors="replace")
    hives = []
    for line in text.splitlines():
        stripped = line.strip().lstrip("\ufeff")
        if stripped.startswith("["):
            hives.append(stripped.lstrip("[-").strip().split("\\")[0].split("]")[0].strip().upper())
    return hives


def backup_is_trusted(data: bytes) -> bool:
    """Vrai si l'import ne peut rien écrire qu'un programme sans droits ne pourrait écrire lui-même (HKCU seul),
    ou si cette sauvegarde exacte a été faite par FastSweep en administrateur.

    True if the import can write nothing a program without rights could not write itself (HKCU only),
    or if this exact backup was made by FastSweep as administrator.
    """
    hives = reg_file_hives(data)
    if hives and all(hive in ("HKEY_CURRENT_USER", "HKCU") for hive in hives):
        return True
    return trusted_backup_registered(hashlib.sha256(data).hexdigest())


def restore_registry_backup(path: str) -> tuple[bool, str]:
    """Importe une sauvegarde .reg ; en administrateur, seulement si elle est digne de confiance (voir plus haut).

    Imports a .reg backup; as administrator, only if it is trustworthy (see above).
    """
    if is_admin():
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            return False, str(exc)
        if not backup_is_trusted(data):
            return False, UNTRUSTED_BACKUP_ERROR
    result = run_hidden([system_tool("reg.exe"), "import", path], timeout=60)
    return result.returncode == 0, result.stderr.strip()


# Dossiers personnels de Windows (Known Folders), où qu'ils aient été déplacés. / Windows personal folders, wherever moved.
KNOWN_FOLDER_IDS = (
    "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",  # Bureau / Desktop
    "FDD39AD0-238F-46AF-ADB4-6C85480369C7",  # Documents
    "374DE290-123F-4565-9164-39C4925E467B",  # Téléchargements / Downloads
    "33E28130-4E1E-4676-835A-98395C3BC3BB",  # Images / Pictures
    "4BD8D571-6D19-48D3-BE97-422220080E43",  # Musique / Music
    "18989B1D-99B5-455B-841C-AB7C74E4DDFC",  # Vidéos / Videos
)
# Sous-dossiers du profil qui ne sont jamais les résidus d'une application. / Profile subfolders that are never an app's leftovers.
PROFILE_FOLDERS = ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos", "OneDrive", "AppData",
                   "AppData\\LocalLow", "Favorites", "Contacts", "Links", "Saved Games", "Searches")


def known_folder(folder_id: str) -> str:
    """Chemin d'un Known Folder Windows, "" s'il est inconnu ou hors Windows.

    Path of a Windows Known Folder, "" when unknown or outside Windows.
    """
    if not is_windows():
        return ""

    class GUID(ctypes.Structure):
        _fields_ = [("data", ctypes.c_ubyte * 16)]

    try:
        shell32, ole32 = system_dll("shell32"), system_dll("ole32")
        guid = GUID()
        ctypes.memmove(guid.data, uuid.UUID(folder_id).bytes_le, 16)
        path = ctypes.c_wchar_p()
        if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path)) != 0:
            return ""
        try:
            return path.value or ""
        finally:
            ole32.CoTaskMemFree(path)
    except (AttributeError, OSError, ValueError):
        return ""


def _comparable(path: str) -> str:
    return os.path.normcase(os.path.realpath(path)).rstrip("\\/")


def protected_folders() -> set[str]:
    """Dossiers que l'analyse ne propose jamais, ni aucun de leurs parents : profil et dossiers personnels,
    dossiers des programmes et des données, racines analysées elles-mêmes.

    Folders the scan never offers, nor any of their parents: profile and personal folders, program and data
    folders, the scanned roots themselves.
    """
    env = os.environ.get
    profile = env("USERPROFILE", "")
    paths = [env(name, "") for name in ("USERPROFILE", "PUBLIC", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "PROGRAMFILES",
                                        "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)",
                                        "PROGRAMFILES(ARM)", "COMMONPROGRAMFILES(ARM)", "TEMP", "TMP", "ONEDRIVE")]
    if profile:
        paths += [str(Path(profile) / name) for name in PROFILE_FOLDERS]
    for root in (env("LOCALAPPDATA", ""), env("APPDATA", ""), env("PROGRAMDATA", "")):
        if root:
            menu = Path(root) / "Microsoft" / "Windows" / "Start Menu"
            paths += [str(Path(root) / "Programs"), str(Path(root) / "Temp"), str(menu), str(menu / "Programs")]
    paths += [known_folder(folder_id) for folder_id in KNOWN_FOLDER_IDS]
    return {_comparable(path) for path in paths if path}


def is_protected_folder(resolved: Path, protected: set[str]) -> bool:
    """Vrai pour un dossier protégé, l'un de ses parents (racine d'un disque comprise) ou tout ce qui est dans Windows.

    True for a protected folder, one of its parents (a drive root included) or anything inside Windows.
    """
    candidate = _comparable(str(resolved))
    if not candidate or resolved.parent == resolved:
        return True  # racine d'un disque / drive root
    windows = os.environ.get("SYSTEMROOT", "")
    if windows and (candidate + os.sep).startswith(_comparable(windows) + os.sep):
        return True
    return any(folder == candidate or folder.startswith(candidate + os.sep) for folder in protected)


def file_candidates(app: AppEntry) -> list[FileCandidate]:
    """Trouve les résidus probablement propres à l'application sans parcourir récursivement les disques de l'utilisateur ou du système.

    Find likely app-owned leftovers without recursively crawling user or system disks.
    """
    target = normalize_name(app.name)
    if len(target) < 4:
        return []
    publisher = normalize_name(app.publisher)
    found: dict[str, FileCandidate] = {}
    protected = protected_folders()

    def add(path: Path, confidence: str, reason: str) -> None:
        try:
            # Un lien ou une jonction au nom de l'application mènerait n'importe où : refusé avant d'être suivi.
            # A link or junction named after the app could lead anywhere: refused before being followed.
            if path_is_reparse_point(path):
                return
            resolved = path.resolve(strict=True)
            # Seul le dernier élément du chemin est vérifié ci-dessus ; ses parents peuvent être résolus, pas lui.
            # Only the last part of the path is checked above; its parents may be resolved, not it.
            if os.path.normcase(str(resolved)) != os.path.normcase(str(path.parent.resolve(strict=True) / path.name)):
                return
            if resolved.name in {"", ".", ".."} or any(part.casefold() == "windowsapps" for part in resolved.parts):
                return
            if not (resolved.is_dir() or resolved.is_file()) or path_is_reparse_point(resolved):
                return
            if is_protected_folder(resolved, protected):
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
        os.environ.get("PROGRAMFILES(ARM)", ""),  # Windows sur ARM / Windows on ARM
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
                    elif publisher and is_dir and name == publisher and not path_is_reparse_point(Path(entry.path)):
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
    """Lien symbolique, jonction ou autre point d'analyse : jamais suivi par le nettoyage.

    Symbolic link, junction or other reparse point: never followed by the cleanup.
    """
    try:
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
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


# Options de SHFileOperation / SHFileOperation flags
FO_DELETE = 3
FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI, FOF_WANTNUKEWARNING = 0x4, 0x10, 0x40, 0x400, 0x4000
DRIVE_FIXED = 3
NOT_RECYCLABLE_ERROR = "not-recyclable"  # traduit par l'interface / translated by the interface


def drive_has_recycle_bin(path: str) -> bool:
    """Seuls les disques fixes ont une Corbeille ; ailleurs (réseau, clé USB) Windows supprimerait définitivement.

    Only fixed disks have a Recycle Bin; elsewhere (network, USB stick) Windows would delete permanently.
    """
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if not drive or drive.startswith(("\\\\", "//")):  # chemin réseau UNC / UNC network path
        return False
    get_type = system_dll("kernel32").GetDriveTypeW
    get_type.argtypes = (wintypes.LPCWSTR,)
    get_type.restype = wintypes.UINT
    return get_type(drive + "\\") == DRIVE_FIXED


def recycle_path(path: str) -> None:
    """Déplace un fichier ou un dossier vers la Corbeille Windows, sans jamais le supprimer définitivement.

    Move a file or folder to the Windows Recycle Bin, never permanently delete it.
    Un emplacement sans Corbeille est refusé (OSError ``NOT_RECYCLABLE_ERROR``). Si la Corbeille ne peut pas
    recevoir l'élément (trop gros), Windows demande avant de supprimer définitivement au lieu de le faire en silence.
    A location without a Recycle Bin is refused (OSError ``NOT_RECYCLABLE_ERROR``). If the Recycle Bin cannot take
    the item (too big), Windows asks before deleting permanently instead of doing it silently.
    """
    if not is_windows():
        raise OSError("Recycle Bin operations are only available on Windows.")
    if not drive_has_recycle_bin(path):
        raise OSError(NOT_RECYCLABLE_ERROR)
    buffer = ctypes.create_unicode_buffer(path + "\0\0")
    operation = SHFILEOPSTRUCTW(
        hwnd=None, wFunc=FO_DELETE, pFrom=ctypes.cast(buffer, wintypes.LPCWSTR), pTo=None,
        fFlags=FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_WANTNUKEWARNING,
        fAnyOperationsAborted=False, hNameMappings=None, lpszProgressTitle=None,
    )
    shell32 = system_dll("shell32", use_last_error=True)
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
