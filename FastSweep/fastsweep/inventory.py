"""Applications installées : inventaire du registre et du Microsoft Store, chemins d'icônes, tailles, mise en forme.

Installed applications: registry and Microsoft Store inventory, icons paths, sizes, formatting.
"""
from __future__ import annotations

import csv
import ctypes
import io
import os
import re
import stat
import subprocess
import winreg
import xml.etree.ElementTree as ET
from ctypes import wintypes
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from .models import AppEntry
from .system import run_powershell

FILE_ATTRIBUTE_REPARSE_POINT = 0x400


UNINSTALL_ROOTS = (
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "HKCU"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "HKLM"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall", "HKLM"),
)


SOFTWARE_ROOTS = (
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE", "HKCU"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE", "HKLM"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node", "HKLM"),
)


def registry_value(key: winreg.HKEYType, name: str) -> str:
    try:
        value, _ = winreg.QueryValueEx(key, name)
        return str(value).strip() if value is not None else ""
    except OSError:
        return ""


def registry_size_bytes(key: winreg.HKEYType) -> int | None:
    try:
        value, _ = winreg.QueryValueEx(key, "EstimatedSize")
        size_kb = int(value, 0) if isinstance(value, str) else int(value)
        return size_kb * 1024 if size_kb > 0 else None
    except (OSError, TypeError, ValueError):
        return None


def read_classic_apps() -> list[AppEntry]:
    # Clé par chemin complet : un même nom de sous-clé sous les vues 64 et 32 bits (WOW6432Node) désigne deux
    # installations distinctes (ex. 7-Zip x64 et x86), pas un doublon.
    # Key by full path: the same subkey name under the 64- and 32-bit views (WOW6432Node) means two
    # separate installs (e.g. 7-Zip x64 and x86), not a duplicate.
    apps: dict[tuple[str, str], AppEntry] = {}
    for hive, root, label in UNINSTALL_ROOTS:
        try:
            with winreg.OpenKey(hive, root) as parent:
                count = winreg.QueryInfoKey(parent)[0]
                for index in range(count):
                    try:
                        subkey_name = winreg.EnumKey(parent, index)
                        with winreg.OpenKey(parent, subkey_name) as key:
                            name = registry_value(key, "DisplayName")
                            system_component = registry_value(key, "SystemComponent") == "1"
                            release_type = registry_value(key, "ReleaseType").lower()
                            if not name or system_component or release_type in {"update", "hotfix", "security update"}:
                                continue
                            quiet = registry_value(key, "QuietUninstallString")
                            entry = AppEntry(
                                name=name,
                                publisher=registry_value(key, "Publisher"),
                                version=registry_value(key, "DisplayVersion"),
                                date=registry_value(key, "InstallDate"),
                                uninstall=registry_value(key, "UninstallString") or quiet,
                                quiet_uninstall=quiet,
                                location=registry_value(key, "InstallLocation"),
                                icon_path=registry_value(key, "DisplayIcon"),
                                size_bytes=registry_size_bytes(key),
                                size_source="registry",
                                hive=hive,
                                subkey=root + "\\" + subkey_name,
                                hive_label=label,
                            )
                            apps[(label, entry.subkey.casefold())] = entry
                    except OSError:
                        continue
        except OSError:
            continue
    return sorted(apps.values(), key=lambda app: app.name.casefold())


def read_store_apps() -> tuple[list[AppEntry], str]:
    script = (
        "Get-AppxPackage | Where-Object { -not $_.IsFramework -and -not $_.IsResourcePackage "
        "-and $_.SignatureKind -ne 'System' -and -not $_.NonRemovable } | "
        "Select-Object Name,PackageFullName,Publisher,Version,InstallLocation | ConvertTo-Csv -NoTypeInformation"
    )
    try:
        completed = run_powershell(script, timeout=45)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], str(exc)
    if completed.returncode != 0:
        return [], completed.stderr.strip()
    rows = list(csv.reader(io.StringIO(completed.stdout)))
    if not rows:
        return [], ""
    headers = [item.strip().strip('"').casefold() for item in rows[0]]
    apps = []
    for row in rows[1:]:
        values = dict(zip(headers, row))
        package_name = values.get("name", "").strip()
        if not package_name:
            continue
        full_name = values.get("packagefullname", "").strip()
        install_location = values.get("installlocation", "").strip()
        display_name, publisher_name = store_display_info(install_location, full_name, package_name)
        apps.append(AppEntry(
            name=display_name or package_name.replace("_", " "),
            publisher=publisher_name or values.get("publisher", "").strip(),
            version=values.get("version", "").strip(), source="store",
            package_full_name=full_name, package_name=package_name,
            install_location=install_location,
        ))
    return sorted(apps, key=lambda app: app.name.casefold()), ""


@lru_cache(maxsize=1024)
def read_appx_manifest(install_location: str) -> tuple[str, str, str]:
    """DisplayName, PublisherDisplayName et Logo bruts d'un package, lus une seule fois dans AppxManifest.xml.

    Raw DisplayName, PublisherDisplayName and Logo of a package, read once from AppxManifest.xml.
    """
    if not install_location:
        return "", "", ""
    try:
        manifest = ET.parse(Path(install_location) / "AppxManifest.xml")
    except (OSError, ET.ParseError):
        return "", "", ""
    display = publisher = logo = ""
    for node in manifest.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag == "Properties" and not display:
            values = {child.tag.rsplit("}", 1)[-1]: (child.text or "").strip() for child in node}
            display, publisher = values.get("DisplayName", ""), values.get("PublisherDisplayName", "")
        elif tag == "Logo" and node.text and not logo:
            logo = node.text.strip()
    return display, publisher, logo


def store_display_info(install_location: str, package_full_name: str, package_name: str) -> tuple[str, str]:
    """Noms lisibles de l'application et de l'éditeur d'un package Store ("" s'ils ne peuvent pas être résolus).

    Readable app and publisher names of a Store package ("" when they cannot be resolved).
    """
    display, publisher, _logo = read_appx_manifest(install_location)
    return (
        resolve_package_string(display, package_full_name, package_name),
        resolve_package_string(publisher, package_full_name, package_name),
    )


def resolve_package_string(value: str, package_full_name: str, package_name: str) -> str:
    """Résout une référence « ms-resource: » d'un manifeste de package vers son texte localisé.

    Resolve an "ms-resource:" reference from a package manifest to its localized text.
    """
    value = value.strip()
    if not value.casefold().startswith("ms-resource:"):
        return value
    if not package_full_name or not package_name:
        return ""
    resource = value[len("ms-resource:"):]
    if resource.startswith("//"):
        uris = ["ms-resource:" + resource]
    elif resource.startswith("/"):
        uris = [f"ms-resource://{package_name}{resource}"]
    elif "/" in resource:
        uris = [f"ms-resource://{package_name}/{resource}", f"ms-resource://{package_name}/Resources/{resource}"]
    else:
        uris = [f"ms-resource://{package_name}/Resources/{resource}", f"ms-resource://{package_name}/{resource}"]
    for uri in uris:
        text = load_indirect_string(f"@{{{package_full_name}? {uri}}}")
        if text and not text.casefold().startswith("ms-resource:"):
            return text
    return ""


def load_indirect_string(source: str) -> str:
    """SHLoadIndirectString de Windows ; "" si la ressource ne peut pas être chargée.

    Windows SHLoadIndirectString; "" when the resource cannot be loaded.
    """
    try:
        shlwapi = ctypes.WinDLL("shlwapi")
        load = shlwapi.SHLoadIndirectString
        load.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.UINT, ctypes.c_void_p)
        load.restype = ctypes.c_long
        buffer = ctypes.create_unicode_buffer(1024)
        if load(source, buffer, len(buffer), None) != 0:
            return ""
        return buffer.value.strip()
    except (AttributeError, OSError):
        return ""


def format_date(value: str) -> str:
    value = value.strip()
    if len(value) == 8 and value.isdigit():
        try:
            return datetime.strptime(value, "%Y%m%d").strftime("%d/%m/%Y")
        except ValueError:
            return value
    return value


def directory_size(path: str, should_stop=None) -> tuple[int | None, bool]:
    """Estime la taille d'un dossier sans suivre les jonctions ni les liens symboliques.

    Non mis en cache : la liste est actualisée après les désinstallations et doit afficher des tailles à jour.
    ``should_stop`` permet à un thread d'arrière-plan d'abandonner un long parcours (le résultat est alors partiel).

    Estimate a directory's size without following junctions or symbolic links.

    Not cached: the list is refreshed after uninstalls and must show current sizes.
    ``should_stop`` lets a background worker abandon a long walk (result is then partial).
    """
    root = Path(os.path.expandvars(path))
    if not root.is_dir():
        return None, True
    try:
        root = root.resolve(strict=True)
    except OSError:
        return None, False
    total = 0
    complete = True
    pending = [str(root)]  # chaînes simples : pas d'objet Path par dossier / plain strings: no Path object per folder
    while pending:
        if should_stop is not None and should_stop():
            return (total or None), False
        folder = pending.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    try:
                        # Sous Windows, ce stat vient du listing du dossier lui-même (pas d'E/S supplémentaire).
                        # On Windows this stat comes from the directory listing itself (no extra I/O).
                        info = entry.stat(follow_symlinks=False)
                        if getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
                            complete = False
                        elif stat.S_ISDIR(info.st_mode):
                            pending.append(entry.path)
                        elif stat.S_ISREG(info.st_mode):
                            total += info.st_size
                    except OSError:
                        complete = False
        except OSError:
            complete = False
    return (total if total or complete else None), complete


def format_size(size_bytes: int | None, lang: str = "fr", partial: bool = False) -> str:
    if size_bytes is None:
        return "—"
    units = ("Ko", "Mo", "Go", "To", "Po") if lang == "fr" else ("KB", "MB", "GB", "TB", "PB")
    value = max(size_bytes, 1) / 1024
    unit_index = 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024
        unit_index += 1
    number = f"{value:.1f}"
    if lang == "fr":
        number = number.replace(".", ",")
    return ("≈ " if partial else "") + f"{number} {units[unit_index]}"


@lru_cache(maxsize=4096)
def display_icon_path(value: str) -> str:
    """Extrait le chemin de l'exécutable ou de l'image d'une valeur DisplayIcon de Windows.

    Extract the executable or image path from a Windows DisplayIcon value.
    """
    value = os.path.expandvars(value.strip())
    if not value:
        return ""
    quoted = re.match(r'^"([^\"]+)"', value)
    if quoted:
        return quoted.group(1)
    match = re.match(r"(?i)^(.*?\.(?:exe|dll|ico|png))(?:\s*,\s*-?\d+)?$", value)
    return match.group(1).strip().strip('"') if match else value.strip().strip('"')


@lru_cache(maxsize=2048)
def store_logo_path(install_location: str) -> str:
    if not install_location:
        return ""
    root = Path(install_location)
    if not root.is_dir():
        return ""
    logo = read_appx_manifest(install_location)[2]
    candidates: list[Path] = []
    if logo:
        relative = Path(logo.replace("/", os.sep).replace("\\", os.sep))
        candidates.extend((root / relative, root / relative.parent / (relative.stem + ".scale-200" + relative.suffix)))
    assets = root / "Assets"
    if assets.is_dir():
        for pattern in ("StoreLogo*.png", "Square44x44Logo*.png", "Logo.png", "StoreLogo*.jpg"):
            candidates.extend(assets.glob(pattern))
    try:
        resolved_root = root.resolve()
    except OSError:
        return ""
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(resolved_root)  # ne jamais sortir du dossier du package / never leave the package folder
            if resolved.is_file():
                return str(resolved)
        except (OSError, ValueError):
            continue
    return ""
