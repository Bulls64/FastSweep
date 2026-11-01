"""Classes de données partagées par l'inventaire, les analyses de nettoyage et l'interface.

Data classes shared by the inventory, the cleanup scans and the interface.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, TypeVar

# Niveaux de confiance des candidats résiduels, du plus faible au plus fort.
# Confidence levels of leftover candidates, weakest first.
CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


# eq=False : une entrée est une application installée ; comparaison par identité (rapide, et hachable).
# eq=False: an entry is one installed app; compare by identity (cheap, and hashable).
@dataclass(eq=False)
class AppEntry:
    name: str
    publisher: str = ""
    version: str = ""
    date: str = ""
    uninstall: str = ""
    quiet_uninstall: str = ""  # QuietUninstallString : désinstallation sans assistant / uninstall without a wizard
    location: str = ""
    source: str = "classic"
    package_full_name: str = ""
    package_name: str = ""
    hive: int | None = None
    subkey: str = ""
    hive_label: str = ""
    icon_path: str = ""
    install_location: str = ""
    size_bytes: int | None = None
    size_source: str = ""
    size_path: str = ""

    @property
    def key(self) -> str:
        """Identifiant stable de l'application, identique d'une actualisation à l'autre (pour garder les cases cochées).

        Stable app identifier, the same from one refresh to the next (to keep boxes checked).
        """
        if self.source == "store":
            return f"store:{self.package_full_name or self.name}"
        return f"{self.hive_label}\\{self.subkey}" if self.subkey else f"classic:{self.name}"


@dataclass(frozen=True)
class RegistryCandidate:
    hive: int
    path: str
    hive_label: str
    confidence: str
    reason: str

    @property
    def identity(self) -> str:
        return f"{self.hive_label}\\{self.path.casefold()}"


@dataclass(frozen=True)
class FileCandidate:
    path: str
    confidence: str
    reason: str


Candidate = TypeVar("Candidate", RegistryCandidate, FileCandidate)


def keep_strongest(found: dict[str, Candidate], key: str, candidate: Candidate) -> None:
    """Enregistre ``candidate`` sous ``key`` sauf si une correspondance égale ou plus forte s'y trouve déjà.

    Record ``candidate`` under ``key`` unless an equal or stronger match is already there.
    """
    previous = found.get(key)
    if previous is None or CONFIDENCE_RANK[candidate.confidence] > CONFIDENCE_RANK[previous.confidence]:
        found[key] = candidate


def sort_by_confidence(candidates: Iterable[Candidate]) -> list[Candidate]:
    """Correspondances les plus fortes d'abord, puis par chemin.

    Strongest matches first, then by path.
    """
    return sorted(candidates, key=lambda item: (-CONFIDENCE_RANK[item.confidence], item.path.casefold()))
