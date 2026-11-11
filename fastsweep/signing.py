"""Signature des mises à jour : FastSweep n'installe qu'un exécutable signé par la clé privée de son auteur.

Update signing: FastSweep only installs an executable signed with its author's private key.

Le workflow de release signe ``FastSweep.exe`` (ECDSA P-256, SHA-256) et joint la signature à la release
(``FastSweep.exe.sig``). FastSweep la vérifie avec la cryptographie intégrée à Windows (CNG, bcrypt.dll) et la
clé publique ci-dessous : un exe remplacé sur GitHub, ou servi par un tiers, est refusé même si son empreinte
annoncée correspond. Le message signé contient aussi le numéro de version : une ancienne version signée ne peut
pas être servie à la place d'une nouvelle.
The release workflow signs ``FastSweep.exe`` (ECDSA P-256, SHA-256) and attaches the signature to the release
(``FastSweep.exe.sig``). FastSweep checks it with the cryptography built into Windows (CNG, bcrypt.dll) and the
public key below: an exe replaced on GitHub, or served by a third party, is refused even if its announced hash
matches. The signed message also holds the version number: an old signed version cannot be served instead of a
new one.

Clé et signature : ``tools/release_signing.py`` (``new-key`` crée la paire de clés et écrit la clé publique ici).
Key and signature: ``tools/release_signing.py`` (``new-key`` creates the key pair and writes the public key here).
"""
from __future__ import annotations

import base64
import binascii
import ctypes
import hashlib
import re
import struct

from .system import is_windows, system_dll

# Clé publique ECDSA P-256 : X puis Y, 32 octets chacun, en hexadécimal. Vide : vérification désactivée.
# ECDSA P-256 public key: X then Y, 32 bytes each, in hexadecimal. Empty: checking disabled.
PUBLIC_KEY = "9bd11365f3209bb07a8a4fe9d7cb50ca700da913388586277f64d65aed471d3e867d3e17c36e22dee134d1303d223bc2d0610576ad9dcb8c84ffe121165b83de"

SIGNATURE_SUFFIX = ".sig"
MAX_SIGNATURE_FILE = 1024  # une signature encodée fait 88 caractères / an encoded signature is 88 characters
_BCRYPT_ECDSA_PUBLIC_P256_MAGIC = 0x31534345  # « ECS1 »


def enabled() -> bool:
    return bool(PUBLIC_KEY)


def public_key() -> bytes:
    """Clé publique (64 octets) ; ValueError si elle est mal formée. / Public key (64 bytes); ValueError if malformed."""
    if not re.fullmatch(r"[0-9a-fA-F]{128}", PUBLIC_KEY):
        raise ValueError("PUBLIC_KEY")
    return bytes.fromhex(PUBLIC_KEY)


def message(version: str, sha256: str) -> bytes:
    """Ce qui est signé : la version et l'empreinte de l'exécutable. / What is signed: the version and the exe hash."""
    return f"FastSweep update\nversion={version}\nsha256={sha256.lower()}\n".encode("ascii")


def decode_signature(data: bytes) -> bytes:
    """Contenu de ``FastSweep.exe.sig`` (base64 de r‖s) → 64 octets ; ValueError sinon.

    Content of ``FastSweep.exe.sig`` (base64 of r‖s) → 64 bytes; ValueError otherwise.
    """
    try:
        raw = base64.b64decode(data.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("signature") from exc
    if len(raw) != 64:
        raise ValueError("signature")
    return raw


def verify(version: str, sha256: str, signature: bytes) -> bool:
    """La signature (64 octets r‖s) couvre-t-elle cette version et cette empreinte ?

    Does the signature (64 bytes r‖s) cover this version and this hash?
    Faux si la vérification est impossible (clé mal formée, CNG indisponible) : on refuse plutôt que d'installer.
    False when checking is impossible (malformed key, CNG unavailable): refuse rather than install.
    """
    if len(signature) != 64 or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256 or ""):
        return False
    try:
        key = public_key()
    except ValueError:
        return False
    return verify_digest(key, hashlib.sha256(message(version, sha256)).digest(), signature)


def verify_digest(key: bytes, digest: bytes, signature: bytes) -> bool:
    """Vérification ECDSA P-256 par Windows (remplaçable dans les tests hors Windows).

    ECDSA P-256 check by Windows (replaceable in tests outside Windows).
    """
    if not is_windows():
        return False
    try:
        return _cng_verify(key, digest, signature)
    except (AttributeError, OSError):
        return False


def _cng_verify(key: bytes, digest: bytes, signature: bytes) -> bool:
    bcrypt = system_dll("bcrypt")
    handle, ulong = ctypes.c_void_p, ctypes.c_ulong
    bcrypt.BCryptOpenAlgorithmProvider.argtypes = [ctypes.POINTER(handle), ctypes.c_wchar_p, ctypes.c_wchar_p, ulong]
    bcrypt.BCryptImportKeyPair.argtypes = [handle, handle, ctypes.c_wchar_p, ctypes.POINTER(handle),
                                           ctypes.c_char_p, ulong, ulong]
    bcrypt.BCryptVerifySignature.argtypes = [handle, handle, ctypes.c_char_p, ulong, ctypes.c_char_p, ulong, ulong]
    bcrypt.BCryptDestroyKey.argtypes = [handle]
    bcrypt.BCryptCloseAlgorithmProvider.argtypes = [handle, ulong]
    for function in ("BCryptOpenAlgorithmProvider", "BCryptImportKeyPair", "BCryptVerifySignature",
                     "BCryptDestroyKey", "BCryptCloseAlgorithmProvider"):
        getattr(bcrypt, function).restype = ctypes.c_long  # NTSTATUS : 0 = succès / 0 = success
    algorithm, public = handle(), handle()
    if bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(algorithm), "ECDSA_P256", None, 0) != 0:
        return False
    try:
        blob = struct.pack("<II", _BCRYPT_ECDSA_PUBLIC_P256_MAGIC, 32) + key
        if bcrypt.BCryptImportKeyPair(algorithm, None, "ECCPUBLICBLOB", ctypes.byref(public), blob, len(blob), 0) != 0:
            return False  # point hors de la courbe, par exemple / point not on the curve, for instance
        try:
            return bcrypt.BCryptVerifySignature(public, None, digest, len(digest), signature, len(signature), 0) == 0
        finally:
            bcrypt.BCryptDestroyKey(public)
    finally:
        bcrypt.BCryptCloseAlgorithmProvider(algorithm, 0)
