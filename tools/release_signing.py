"""Clé et signature des mises à jour de FastSweep (ECDSA P-256, SHA-256). Nécessite : pip install cryptography

FastSweep update key and signature (ECDSA P-256, SHA-256). Requires: pip install cryptography

  python tools/release_signing.py new-key [fichier.pem]
      Crée la paire de clés (une seule fois), écrit la clé privée dans le fichier (par défaut
      fastsweep-signing-key.pem dans le dossier personnel, hors du dépôt) et la clé publique dans fastsweep/signing.py.
      Creates the key pair (once), writes the private key to the file (default fastsweep-signing-key.pem in the
      home folder, outside the repository) and the public key to fastsweep/signing.py.

  python tools/release_signing.py sign dist/FastSweep.exe
      Signe l'exe pour la version de fastsweep/__init__.py avec la clé privée de la variable d'environnement
      FASTSWEEP_SIGNING_KEY (contenu du .pem) et écrit dist/FastSweep.exe.sig. Utilisé par le workflow de release.
      Signs the exe for the version in fastsweep/__init__.py with the private key in the FASTSWEEP_SIGNING_KEY
      environment variable (content of the .pem) and writes dist/FastSweep.exe.sig. Used by the release workflow.

  python tools/release_signing.py public-key fichier.pem
      Affiche la clé publique d'une clé privée. / Prints the public key of a private key.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import sys
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature

ROOT = Path(__file__).resolve().parent.parent
SIGNING_MODULE = ROOT / "fastsweep" / "signing.py"
VERSION_MODULE = ROOT / "fastsweep" / "__init__.py"
KEY_LINE = re.compile(r'^PUBLIC_KEY = "([0-9a-fA-F]*)"$', re.MULTILINE)


def fail(text: str) -> None:
    print(f"Erreur / Error: {text}", file=sys.stderr)
    sys.exit(1)


def public_hex(private: ec.EllipticCurvePrivateKey) -> str:
    numbers = private.public_key().public_numbers()
    return (numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")).hex()


def load_private(pem: bytes) -> ec.EllipticCurvePrivateKey:
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError) as exc:
        fail(f"clé privée illisible / unreadable private key ({exc})")
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        fail("la clé n'est pas une clé ECDSA P-256 / the key is not an ECDSA P-256 key")
    return key


def embedded_key() -> str:
    match = KEY_LINE.search(SIGNING_MODULE.read_text(encoding="utf-8"))
    if not match:
        fail(f"ligne PUBLIC_KEY introuvable dans / PUBLIC_KEY line not found in {SIGNING_MODULE}")
    return match.group(1).lower()


def version() -> str:
    match = re.search(r'^__version__ = "([^"]+)"$', VERSION_MODULE.read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        fail("__version__ introuvable / not found")
    return match.group(1)


def message(release_version: str, sha256: str) -> bytes:
    # Doit rester identique à fastsweep.signing.message. / Must stay identical to fastsweep.signing.message.
    return f"FastSweep update\nversion={release_version}\nsha256={sha256.lower()}\n".encode("ascii")


def new_key(target: str) -> None:
    path = Path(target).resolve()
    if path.exists():
        fail(f"{path} existe déjà : rien n'est écrasé / already exists: nothing is overwritten")
    if embedded_key():
        fail("fastsweep/signing.py contient déjà une clé publique ; la remplacer empêcherait les versions installées de se "
             "mettre à jour / already holds a public key; replacing it would stop installed versions from updating")
    private = ec.generate_private_key(ec.SECP256R1())
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as out:
        out.write(pem)
    key = public_hex(private)
    text = SIGNING_MODULE.read_text(encoding="utf-8")
    SIGNING_MODULE.write_text(KEY_LINE.sub(f'PUBLIC_KEY = "{key}"', text, count=1), encoding="utf-8", newline="\n")
    print(f"Clé privée / Private key : {path}")
    print(f"Clé publique / Public key (écrite dans / written to fastsweep/signing.py) :\n{key}")
    print("\n1. Copiez tout le contenu du .pem dans le secret GitHub FASTSWEEP_SIGNING_KEY (Settings > Secrets and "
          "variables > Actions).\n   Copy the whole .pem content into the GitHub secret FASTSWEEP_SIGNING_KEY.")
    print("2. Gardez une copie du .pem hors ligne (clé USB…) et ne le committez jamais.\n"
          "   Keep an offline copy of the .pem (USB stick…) and never commit it.")
    print("3. Committez fastsweep/signing.py. / Commit fastsweep/signing.py.")


def sign(exe: str) -> None:
    pem = os.environ.get("FASTSWEEP_SIGNING_KEY", "")
    if not pem.strip():
        fail("variable FASTSWEEP_SIGNING_KEY vide / empty FASTSWEEP_SIGNING_KEY variable")
    private = load_private(pem.encode("ascii", "replace"))
    expected = embedded_key()
    if not expected:
        fail("fastsweep/signing.py n'a pas de clé publique : FastSweep ne vérifierait pas la signature / "
             "has no public key: FastSweep would not check the signature")
    if public_hex(private) != expected:
        fail("la clé privée ne correspond pas à la clé publique de fastsweep/signing.py / "
             "the private key does not match the public key in fastsweep/signing.py")
    path = Path(exe)
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    release_version = version()
    signed = message(release_version, sha256)
    r, s = decode_dss_signature(private.sign(signed, ec.ECDSA(hashes.SHA256())))
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    # Relecture comme le fera FastSweep (r‖s brut). / Read back the way FastSweep will (raw r‖s).
    try:
        private.public_key().verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                                    signed, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        fail("signature produite invalide / produced signature is invalid")
    target = path.with_name(path.name + ".sig")
    target.write_bytes(base64.b64encode(raw) + b"\n")
    print(f"{target} : version {release_version}, sha256 {sha256}")


def main(argv: list[str]) -> None:
    if len(argv) >= 1 and argv[0] == "new-key" and len(argv) <= 2:
        new_key(argv[1] if len(argv) == 2 else str(Path.home() / "fastsweep-signing-key.pem"))
    elif len(argv) == 2 and argv[0] == "sign":
        sign(argv[1])
    elif len(argv) == 2 and argv[0] == "public-key":
        print(public_hex(load_private(Path(argv[1]).read_bytes())))
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main(sys.argv[1:])
