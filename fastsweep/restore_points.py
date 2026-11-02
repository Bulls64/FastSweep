"""Points de restauration du système : lecture, protection du plus récent et suppression.

System restore points: listing, protection of the most recent one, and deletion.
"""
from __future__ import annotations

import ctypes
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .system import run_powershell, system_dll

# Un tableau JSON même pour un seul point (run_powershell force déjà une sortie UTF-8).
# A JSON array even for a single point (run_powershell already forces UTF-8 output).
LIST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$points = @(Get-ComputerRestorePoint | ForEach-Object {
    [pscustomobject]@{
        Sequence = [int64]$_.SequenceNumber
        Description = [string]$_.Description
        Type = [int]$_.RestorePointType
        Created = [string]$_.CreationTime
    }
})
ConvertTo-Json -InputObject $points -Compress
"""

# Types renvoyés par Windows (RestorePointType) → clé de texte dans i18n.
# Types reported by Windows (RestorePointType) → text key in i18n.
TYPE_KEYS = {
    0: "rp_type_install", 1: "rp_type_uninstall", 6: "rp_type_restore", 7: "rp_type_checkpoint",
    10: "rp_type_driver", 12: "rp_type_settings", 13: "rp_type_cancelled", 14: "rp_type_backup",
}

# Horodatage WMI (CIM_DATETIME) : AAAAMMJJhhmmss.ffffff±UUU, décalage en minutes.
# WMI timestamp (CIM_DATETIME): YYYYMMDDhhmmss.ffffff±UUU, offset in minutes.
_DMTF = re.compile(r"^(\d{14})(?:\.(\d{1,6}))?([+-])(\d{3})$")
_DOTNET_DATE = re.compile(r"^/Date\((-?\d+)(?:[+-]\d{4})?\)/$")


@dataclass(frozen=True)
class RestorePoint:
    sequence: int
    description: str
    created: datetime | None  # avec fuseau horaire / timezone-aware
    kind: int = -1


def parse_creation_time(value) -> datetime | None:
    """Date de création d'un point (format WMI, ISO 8601 ou /Date(ms)/ de PowerShell), avec fuseau horaire.

    Creation date of a point (WMI, ISO 8601 or PowerShell /Date(ms)/ format), timezone-aware.
    """
    text = str(value or "").strip()
    if not text:
        return None
    match = _DMTF.match(text)
    if match:
        stamp, fraction, sign, minutes = match.groups()
        try:
            moment = datetime.strptime(stamp, "%Y%m%d%H%M%S")
        except ValueError:
            return None
        offset = timedelta(minutes=int(minutes)) * (-1 if sign == "-" else 1)
        moment = moment.replace(microsecond=int((fraction or "0").ljust(6, "0")))
        return moment.replace(tzinfo=timezone(offset))
    match = _DOTNET_DATE.match(text)
    if match:
        try:
            return datetime.fromtimestamp(int(match.group(1)) / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):  # horodatage hors limites / timestamp out of range
            return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def parse_restore_points(output: str) -> list[RestorePoint]:
    """Transforme la sortie JSON du script en points, du plus récent au plus ancien ; ignore les entrées illisibles.

    Turns the script's JSON output into points, newest first; skips unreadable entries.
    """
    output = (output or "").strip().lstrip("﻿")
    if not output:
        return []
    data = json.loads(output)
    if isinstance(data, dict):  # un seul objet si PowerShell a déplié le tableau / a single object if PowerShell unrolled the array
        data = [data]
    points = {}
    for entry in data if isinstance(data, list) else []:
        if not isinstance(entry, dict):
            continue
        try:
            sequence = int(entry.get("Sequence"))
        except (TypeError, ValueError):
            continue
        try:
            kind = int(entry.get("Type", -1))
        except (TypeError, ValueError):
            kind = -1
        points[sequence] = RestorePoint(sequence, str(entry.get("Description") or "").strip(),
                                        parse_creation_time(entry.get("Created")), kind)
    return sorted(points.values(), key=lambda point: point.sequence, reverse=True)


def list_restore_points() -> list[RestorePoint]:
    """Points de restauration présents sur le système (droits administrateur requis).

    Restore points present on the system (administrator rights required).
    Lève RuntimeError avec le message de PowerShell en cas d'échec.
    Raises RuntimeError with PowerShell's message on failure.
    """
    try:
        completed = run_powershell(LIST_SCRIPT, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(str(exc)) from exc
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}")
    try:
        return parse_restore_points(completed.stdout)
    except ValueError as exc:
        raise RuntimeError(f"{exc}: {completed.stdout[:200]}") from exc


def protected_sequences(points) -> set[int]:
    """Numéros à ne jamais supprimer : le point le plus récent par numéro de séquence et par date.

    Numbers that must never be deleted: the most recent point by sequence number and by date.
    Les deux coïncident d'ordinaire ; si l'horloge a été changée, les deux sont protégés.
    Both normally match; if the clock was changed, both are protected.
    """
    points = list(points)
    if not points:
        return set()
    protected = {max(point.sequence for point in points)}
    dated = [point for point in points if point.created is not None]
    if dated:
        protected.add(max(dated, key=lambda point: (point.created, point.sequence)).sequence)
    return protected


def select_by_rule(points, older_than_days: int | None = None, keep_latest: int | None = None,
                   now: datetime | None = None) -> list[RestorePoint]:
    """Points qu'une règle supprimerait, du plus récent au plus ancien ; jamais le point protégé.

    Points a rule would delete, newest first; never the protected point.
    ``older_than_days`` : créés il y a plus de N jours (un point sans date n'est jamais retenu).
    ``older_than_days``: created more than N days ago (a point without a date is never picked).
    ``keep_latest`` : tout sauf les N plus récents (au moins 1).
    ``keep_latest``: everything except the N most recent (at least 1).
    """
    points = sorted(points, key=lambda point: point.sequence, reverse=True)
    if keep_latest is not None:
        doomed = points[max(int(keep_latest), 1):]
    elif older_than_days is not None:
        limit = (now or datetime.now(timezone.utc)) - timedelta(days=int(older_than_days))
        doomed = [point for point in points if point.created is not None and point.created < limit]
    else:
        return []
    keep = protected_sequences(points)
    return [point for point in doomed if point.sequence not in keep]


def remove_restore_point(sequence: int) -> None:
    """Supprime un point par son numéro avec l'API SRRemoveRestorePoint de Windows.

    Delete a point by its number with Windows' SRRemoveRestorePoint API.
    """
    srclient = system_dll("srclient.dll")
    remove = srclient.SRRemoveRestorePoint
    remove.argtypes = (ctypes.c_uint32,)
    remove.restype = ctypes.c_uint32
    code = remove(int(sequence))
    if code:
        raise OSError(code, ctypes.FormatError(code).strip())


@dataclass
class DeletionReport:
    deleted: list[RestorePoint]
    protected: list[int]  # refusés : le plus récent / refused: the most recent one
    missing: list[int]  # déjà disparus / already gone
    errors: list[tuple[RestorePoint, str]]


def delete_restore_points(sequences, list_points=list_restore_points, remove=remove_restore_point) -> DeletionReport:
    """Supprime les points demandés, sauf le plus récent.

    Delete the requested points, except the most recent one.
    La liste est relue juste avant la suppression : un point créé entre-temps devient le plus récent
    et c'est lui qui est alors protégé, quelle que soit la liste affichée.
    The list is read again right before deleting: a point created in the meantime becomes the most recent
    one and is the one protected, whatever list is on screen.
    """
    current = {point.sequence: point for point in list_points()}
    keep = protected_sequences(current.values())
    report = DeletionReport([], [], [], [])
    for sequence in sorted({int(value) for value in sequences}):
        if sequence in keep:
            report.protected.append(sequence)
            continue
        point = current.get(sequence)
        if point is None:
            report.missing.append(sequence)
            continue
        try:
            remove(sequence)
        except OSError as exc:
            report.errors.append((point, str(exc)))
        else:
            report.deleted.append(point)
    return report
