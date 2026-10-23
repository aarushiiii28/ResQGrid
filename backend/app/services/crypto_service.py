"""M19 crypto primitives: canonical JSON, SHA-256, Ed25519 signatures, AES-GCM subject encryption (crypto-shredding)
and the server's own signing key. Standard library plus the `cryptography` package only; nothing here talks to a network.

Prototype. Client-side private keys live in browser storage, which needs a secure store in production (see docs/LEDGER.md)."""

import base64
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

GENESIS = "0" * 64
SERVER_KEY_ID = "server-1"


# ---- canonical encoding -----------------------------------------------------------------------------
def canonical(obj) -> str:
    """Deterministic JSON: sorted keys, no whitespace, UTF-8 characters kept. The same object always gives the same bytes."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default)


def _default(o):
    if isinstance(o, datetime):
        return iso(o)
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")


def iso(dt: datetime) -> str:
    """Fixed-width UTC timestamp, microsecond precision (what Postgres stores), so hashes survive a round trip."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def sha256_hex(data: bytes | str) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ---- Ed25519 -----------------------------------------------------------------------------------------
def key_id_for(public_key_b64: str) -> str:
    return "k-" + sha256_hex(unb64(public_key_b64))[:16]


def verify_signature(public_key_b64: str, message: bytes | str, signature_b64: str) -> bool:
    try:
        pub = Ed25519PublicKey.from_public_bytes(unb64(public_key_b64))
        pub.verify(unb64(signature_b64), message.encode("utf-8") if isinstance(message, str) else message)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def valid_public_key(public_key_b64: str) -> bool:
    try:
        raw = unb64(public_key_b64)
        Ed25519PublicKey.from_public_bytes(raw)
        return len(raw) == 32
    except (ValueError, TypeError):
        return False


class ServerKey:
    """The server's signing key. It signs every server-originated ledger entry and every receipt. The private key
    is kept in a git-ignored file (or LEDGER_SERVER_KEY) so the ledger stays verifiable across restarts."""

    def __init__(self, path: Path | None = None, env_key: str | None = None):
        raw: bytes | None = None
        if env_key:
            raw = unb64(env_key)
        elif path is not None and path.exists():
            raw = unb64(path.read_text(encoding="ascii").strip())
        if raw is None:
            self._priv = Ed25519PrivateKey.generate()
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(b64(self._priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                                             serialization.NoEncryption())), encoding="ascii")
        else:
            self._priv = Ed25519PrivateKey.from_private_bytes(raw)
        self.key_id = SERVER_KEY_ID
        self.public_b64 = b64(self._priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))

    def sign(self, message: bytes | str) -> str:
        return b64(self._priv.sign(message.encode("utf-8") if isinstance(message, str) else message))


# ---- subject encryption (crypto-shredding) -------------------------------------------------------------
class Vault:
    """Per-subject AES-GCM keys wrapped by a master key. Destroying a subject's wrapped key makes everything
    encrypted for them unreadable for good, while the ledger (which only holds commitments) stays verifiable."""

    def __init__(self, master_key_b64: str | None, fallback_secret: str):
        if master_key_b64:
            self._master = unb64(master_key_b64)
        else:  # demo default derived from the JWT secret; production sets LEDGER_MASTER_KEY
            self._master = hashlib.sha256(("resqgrid-vault:" + fallback_secret).encode()).digest()
        if len(self._master) != 32:
            raise ValueError("LEDGER_MASTER_KEY must be 32 bytes (base64)")

    def new_subject_key(self) -> tuple[bytes, str]:
        """(plain key, wrapped key as base64)."""
        key = AESGCM.generate_key(bit_length=256)
        nonce = os.urandom(12)
        return key, b64(nonce + AESGCM(self._master).encrypt(nonce, key, b"subject-key"))

    def unwrap(self, wrapped_b64: str) -> bytes:
        raw = unb64(wrapped_b64)
        return AESGCM(self._master).decrypt(raw[:12], raw[12:], b"subject-key")

    @staticmethod
    def encrypt(key: bytes, obj, aad: str) -> dict:
        nonce = os.urandom(12)
        return {"n": b64(nonce), "c": b64(AESGCM(key).encrypt(nonce, canonical(obj).encode("utf-8"), aad.encode()))}

    @staticmethod
    def decrypt(key: bytes, blob: dict, aad: str):
        try:
            return json.loads(AESGCM(key).decrypt(unb64(blob["n"]), unb64(blob["c"]), aad.encode()).decode("utf-8"))
        except (InvalidTag, KeyError, ValueError):
            return None
