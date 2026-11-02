"""Mise à jour de FastSweep depuis les releases GitHub : recherche, téléchargement vérifié, remplacement de l'exécutable.

FastSweep self-update from GitHub releases: lookup, verified download, replacement of the executable.

Rien ici ne dépend de Qt : tout se teste sans interface. Seul l'exécutable construit par PyInstaller peut se mettre
à jour tout seul ; lancé depuis les sources, FastSweep signale la nouvelle version mais renvoie vers GitHub.
Nothing here depends on Qt: everything can be tested without an interface. Only the PyInstaller-built executable
can update itself; run from source, FastSweep reports the new version but points to GitHub.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from . import __version__
from .system import is_windows

REPO = "Bulls64/FastSweep"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases"
ASSET_NAME = "FastSweep.exe"
# Seul un fichier publié par ce dépôt est téléchargé ; GitHub redirige ensuite vers son stockage (githubusercontent.com).
# Only a file published by this repository is downloaded; GitHub then redirects to its storage (githubusercontent.com).
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"
TRUSTED_HOSTS = ("github.com", "githubusercontent.com")
TIMEOUT = 10  # secondes par requête / seconds per request
CHUNK = 256 * 1024
# Plafonds : un serveur qui enverrait sans fin ne remplit ni la mémoire ni le disque (l'exe fait environ 60 Mo).
# Caps: a server that would send endlessly fills neither memory nor disk (the exe is about 60 MB).
MAX_DOWNLOAD = 200 * 1024 * 1024
MAX_JSON = 2 * 1024 * 1024
LEFTOVER_SUFFIXES = (".old", ".new", ".part")  # fichiers que la mise à jour crée à côté de l'exécutable / files the update creates next to the executable


class UpdateError(Exception):
    """Échec d'une étape de mise à jour ; ``key`` est une clé de texte i18n, ``detail`` un complément technique.

    Failure of an update step; ``key`` is an i18n text key, ``detail`` a technical complement.
    """

    def __init__(self, key: str, detail: str = ""):
        super().__init__(f"{key}: {detail}" if detail else key)
        self.key = key
        self.detail = detail


@dataclass(frozen=True)
class UpdateInfo:
    version: str  # « 1.0.2 »
    tag: str
    notes: str
    url: str  # lien de téléchargement de l'exécutable, "" s'il n'est pas (encore) joint à la release / "" if not (yet) attached
    size: int
    sha256: str  # "" si GitHub n'en donne pas / "" when GitHub gives none
    page: str
    newer: bool  # plus récente que la version installée / newer than the installed version
    published: str = ""  # date de publication ISO 8601 donnée par GitHub / ISO 8601 publication date from GitHub

    @property
    def installable(self) -> bool:
        return bool(self.url)

    @property
    def published_date(self) -> str:
        """Date de publication au format JJ/MM/AAAA (heure locale), "" si inconnue.

        Publication date as DD/MM/YYYY (local time), "" if unknown.
        """
        try:
            return datetime.fromisoformat(self.published.replace("Z", "+00:00")).astimezone().strftime("%d/%m/%Y")
        except (ValueError, OverflowError, OSError):  # date absente, illisible ou hors limites / missing, unreadable or out of range
            return ""


def parse_version(text: str) -> tuple[int, ...]:
    """« v1.0.10 » → (1, 0, 10) ; () si le texte ne commence pas par un numéro de version.

    "v1.0.10" → (1, 0, 10); () when the text does not start with a version number.
    """
    match = re.match(r"\s*v?(\d+(?:\.\d+)*)", text or "")
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def is_newer(candidate: str, current: str) -> bool:
    """Comparaison numérique (1.0.10 > 1.0.9), pas alphabétique ; 1.0 et 1.0.0 sont égales.

    Numeric comparison (1.0.10 > 1.0.9), not alphabetical; 1.0 and 1.0.0 are equal.
    """
    a, b = parse_version(candidate), parse_version(current)
    width = max(len(a), len(b))
    return bool(a) and a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def _request(url: str, accept: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": f"FastSweep/{__version__}", "Accept": accept})


def open_url(request: urllib.request.Request):
    """Ouvre une requête HTTPS (point d'entrée unique, remplaçable dans les tests).

    Opens an HTTPS request (single entry point, replaceable in tests).
    """
    return urllib.request.urlopen(request, timeout=TIMEOUT)


def _trusted(url: str) -> bool:
    parts = urlparse(url)
    host = parts.hostname or ""
    return parts.scheme == "https" and any(host == name or host.endswith("." + name) for name in TRUSTED_HOSTS)


def _fetch_json(url: str):
    try:
        with open_url(_request(url, "application/vnd.github+json")) as response:
            body = response.read(MAX_JSON + 1)
        if len(body) > MAX_JSON:
            raise UpdateError("update_err_data")
        return json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:  # avant URLError, dont elle hérite / before URLError, which it inherits from
        if exc.code in (403, 429):
            raise UpdateError("update_err_rate") from exc
        if exc.code == 404:
            raise UpdateError("update_err_none") from exc
        raise UpdateError("update_err_network", f"HTTP {exc.code}") from exc
    except ValueError as exc:  # JSON ou encodage invalide / invalid JSON or encoding
        raise UpdateError("update_err_data") from exc
    except OSError as exc:  # URLError, délai dépassé, réseau coupé… / URLError, timeout, network down…
        raise UpdateError("update_err_network", str(getattr(exc, "reason", None) or exc)) from exc


_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", re.MULTILINE)  # ligne horizontale Markdown / Markdown horizontal rule
_WORDS = {
    "fr": {"nouveautés", "corrections", "les", "des", "une", "est", "pour", "avec", "depuis", "plus", "dans", "mise", "jour"},
    "en": {"new", "fixes", "the", "and", "with", "from", "is", "for", "now", "no", "longer", "update", "when"},
}


def clean_notes(text: str, lang: str, tag: str = "") -> str:
    """Notes de version prêtes à afficher : fins de ligne normalisées, lignes vides en série réduites à une,
    titre qui ne fait que répéter le numéro de version retiré, et seulement la partie dans la langue voulue
    quand les notes sont rédigées en plusieurs langues séparées par une ligne horizontale.

    Release notes ready to display: line endings normalized, runs of blank lines reduced to one, a heading
    that only repeats the version number removed, and only the part in the wanted language when the notes are
    written in several languages separated by a horizontal rule.
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    sections = [part.strip() for part in _RULE.split(text) if part.strip()]
    if len(sections) > 1 and lang in _WORDS:
        def score(part: str, language: str) -> int:
            words = re.findall(r"[a-zàâçéèêëîïôûùüÿœ]+", part.casefold())
            return sum(word in _WORDS[language] for word in words)
        others = [language for language in _WORDS if language != lang]
        best = max(sections, key=lambda part: score(part, lang) - max(score(part, other) for other in others))
        if all(score(best, lang) > score(best, other) for other in others):
            text = best  # sinon, langue incertaine : tout garder / otherwise, unsure language: keep everything
    version = ".".join(str(part) for part in parse_version(tag))
    lines = text.strip().split("\n")
    if lines and version and re.fullmatch(rf"#+\s*v?{re.escape(version)}\s*", lines[0].strip(), re.IGNORECASE):
        lines = lines[1:]  # déjà dit par « La version … est disponible » / already said by "Version … is available"
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in lines)).strip()


def check_latest(current: str = __version__) -> UpdateInfo:
    """Interroge GitHub sur la dernière release publiée ; ``newer`` dit si elle dépasse la version installée.

    Asks GitHub for the latest published release; ``newer`` tells whether it is above the installed version.
    """
    data = _fetch_json(API_LATEST)
    if not isinstance(data, dict):
        raise UpdateError("update_err_data")
    tag = str(data.get("tag_name") or "")
    numbers = parse_version(tag)
    if not numbers:
        raise UpdateError("update_err_data")
    url, size, sha256 = "", 0, ""
    for asset in data.get("assets") or []:
        if isinstance(asset, dict) and str(asset.get("name", "")).casefold() == ASSET_NAME.casefold() \
                and asset.get("state", "uploaded") == "uploaded":
            link = str(asset.get("browser_download_url") or "")
            url = link if link.startswith(DOWNLOAD_PREFIX) else ""
            try:
                size = max(int(asset.get("size") or 0), 0)
            except (TypeError, ValueError):
                size = 0
            digest = str(asset.get("digest") or "").lower()
            sha256 = digest.removeprefix("sha256:") if digest.startswith("sha256:") else ""
            break
    page = str(data.get("html_url") or "")
    version = ".".join(str(part) for part in numbers)
    return UpdateInfo(version, tag, str(data.get("body") or "").strip(), url, size, sha256,
                      page if page.startswith(f"https://github.com/{REPO}/") else RELEASES_URL,
                      is_newer(version, current), str(data.get("published_at") or ""))


def download(info: UpdateInfo, destination: Path, progress=None, stop=None) -> Path:
    """Télécharge l'exécutable dans ``destination`` après vérification de sa taille, de sa somme SHA-256 et de son type.

    Downloads the executable to ``destination`` after checking its size, its SHA-256 sum and its type.
    ``progress(fait, total)`` est appelé au fil de l'eau ; ``stop`` (un Event) interrompt le téléchargement.
    ``progress(done, total)`` is called along the way; ``stop`` (an Event) interrupts the download.
    Le fichier n'apparaît sous son vrai nom qu'une fois complet et vérifié.
    The file only appears under its real name once complete and verified.
    """
    if not info.installable or not info.url.startswith(DOWNLOAD_PREFIX):
        raise UpdateError("update_no_exe")
    if info.size > MAX_DOWNLOAD:
        raise UpdateError("update_err_too_big", _megabytes(info.size))
    part = destination.with_name(destination.name + ".part")
    digest = hashlib.sha256()
    done = 0
    try:
        with open_url(_request(info.url, "application/octet-stream")) as response:
            if not _trusted(response.geturl()):  # redirection vers un hôte inattendu / redirect to an unexpected host
                raise UpdateError("update_err_host", urlparse(response.geturl()).hostname or "")
            total = info.size or _content_length(response)
            if total > MAX_DOWNLOAD:
                raise UpdateError("update_err_too_big", _megabytes(total))
            limit = info.size or MAX_DOWNLOAD  # jamais plus que la taille annoncée / never more than the announced size
            with part.open("wb") as out:
                while True:
                    if stop is not None and stop.is_set():
                        raise UpdateError("update_err_cancelled")
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    done += len(chunk)
                    if done > limit:
                        raise UpdateError("update_err_too_big", _megabytes(done))
                    out.write(chunk)
                    digest.update(chunk)
                    if progress is not None:
                        progress(done, total)
        _check(part, info, done, digest.hexdigest())
        os.replace(part, destination)
    except PermissionError as exc:
        raise UpdateError("update_err_write", str(exc)) from exc
    except OSError as exc:  # réseau coupé en route ou disque plein / network lost midway or full disk
        raise UpdateError("update_err_network", str(getattr(exc, "reason", None) or exc)) from exc
    finally:
        try:
            part.unlink(missing_ok=True)  # notre propre fichier temporaire / our own temporary file
        except OSError:
            pass
    return destination


def _megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):.0f} MB"


def _check(path: Path, info: UpdateInfo, size: int, sha256: str) -> None:
    """Taille, somme SHA-256 et en-tête d'exécutable conformes à la release, sinon UpdateError.

    Size, SHA-256 sum and executable header matching the release, otherwise UpdateError.
    """
    if info.size and size != info.size:
        raise UpdateError("update_err_size", f"{size} / {info.size}")
    if info.sha256 and sha256 != info.sha256:
        raise UpdateError("update_err_hash")
    with path.open("rb") as check:
        if check.read(2) != b"MZ":  # un exécutable Windows commence par « MZ » / a Windows executable starts with "MZ"
            raise UpdateError("update_err_notexe")


def verify_staged(staged: Path, info: UpdateInfo) -> None:
    """Revérifie le fichier prêt à installer, juste avant de remplacer l'exécutable.

    Checks the file ready to install again, right before replacing the executable.
    Entre le téléchargement et l'installation (qui peut attendre que FastSweep soit inactif), un autre programme
    aurait pu remplacer le fichier ; s'il ne correspond plus à la release, il est supprimé et rien n'est installé.
    Between the download and the install (which may wait until FastSweep is idle), another program could have
    replaced the file; if it no longer matches the release, it is deleted and nothing is installed.
    """
    digest, size = hashlib.sha256(), 0
    try:
        with staged.open("rb") as source:
            while chunk := source.read(CHUNK):
                size += len(chunk)
                if size > MAX_DOWNLOAD:
                    raise UpdateError("update_err_changed")
                digest.update(chunk)
        _check(staged, info, size, digest.hexdigest())
    except (UpdateError, OSError) as exc:
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass
        raise UpdateError("update_err_changed") from exc


def _content_length(response) -> int:
    """Taille annoncée par le serveur, 0 si absente ou invalide (la progression reste alors indéterminée).

    Size announced by the server, 0 when missing or invalid (progress then stays indeterminate).
    """
    try:
        return max(int(response.headers.get("Content-Length") or 0), 0)
    except (TypeError, ValueError):
        return 0


def current_executable() -> Path | None:
    """Chemin de FastSweep.exe quand le programme tourne depuis l'exécutable PyInstaller, sinon None.

    Path of FastSweep.exe when running from the PyInstaller executable, otherwise None.
    """
    return Path(sys.executable) if is_windows() and getattr(sys, "frozen", False) else None


def self_update_problem() -> str:
    """"" si FastSweep peut se remplacer lui-même, sinon la clé de texte qui explique pourquoi pas.

    "" if FastSweep can replace itself, otherwise the text key that explains why not.
    """
    exe = current_executable()
    if exe is None:
        return "update_from_source"
    return "" if os.access(exe.parent, os.W_OK) else "update_not_writable"


def staged_path(exe: Path) -> Path:
    return exe.with_name(exe.name + ".new")


def install(staged: Path, exe: Path, info: UpdateInfo) -> None:
    """Remplace ``exe`` par ``staged`` ; l'ancien exécutable est mis de côté (``.old``) et remis en place si ça échoue.

    Replaces ``exe`` with ``staged``; the old executable is set aside (``.old``) and put back if it fails.
    ``staged`` est d'abord revérifié contre ``info`` (la release téléchargée). / ``staged`` is first checked again
    against ``info`` (the downloaded release).
    Windows interdit de supprimer un exécutable en cours d'exécution mais autorise à le renommer.
    Windows forbids deleting a running executable but allows renaming it.
    """
    verify_staged(staged, info)
    old = exe.with_name(exe.name + ".old")
    try:
        old.unlink(missing_ok=True)  # reste d'une mise à jour précédente / leftover from a previous update
        exe.rename(old)
    except OSError as exc:
        raise UpdateError("update_err_write", str(exc)) from exc
    try:
        os.replace(staged, exe)
    except OSError as exc:
        try:
            old.rename(exe)  # on remet l'ancienne version : l'application reste utilisable / put the old version back
        except OSError:
            pass
        raise UpdateError("update_err_write", str(exc)) from exc


def restart(exe: Path) -> None:
    """Lance la nouvelle version, détachée de cette instance qui va se fermer.

    Starts the new version, detached from this instance which is about to close.
    PyInstaller : sans cette variable, le nouveau processus réutiliserait le dossier temporaire de l'ancien,
    supprimé à sa fermeture, et échouerait au démarrage.
    PyInstaller: without this variable the new process would reuse the old one's temporary folder,
    deleted when it closes, and fail to start.
    """
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(exe)], env=env, close_fds=True, creationflags=flags)


def cleanup_leftovers(exe: Path | None = None) -> None:
    """Retire les fichiers que la mise à jour a laissés à côté de l'exécutable (ancienne version, reste de téléchargement).

    Removes the files the update left next to the executable (old version, download leftovers).
    """
    exe = exe or current_executable()
    if exe is None:
        return
    for suffix in LEFTOVER_SUFFIXES:
        try:
            exe.with_name(exe.name + suffix).unlink(missing_ok=True)
        except OSError:
            pass  # encore utilisé : ce sera pour la prochaine fois / still in use: next time
