"""API key issuing and verification.

Keys look like ``gdap_<prefix>_<secret>``. Only a PBKDF2-SHA256 hash of the secret half is stored,
so a metadata database dump does not leak credentials. Verification is constant-time.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

PREFIX = "gdap"
_ITERATIONS = 200_000
_SALT_BYTES = 16


@dataclass(frozen=True, slots=True)
class IssuedKey:
    """The plaintext is returned exactly once, at creation time."""

    plaintext: str
    prefix: str
    key_hash: str


def generate(prefix_length: int = 8) -> IssuedKey:
    prefix = secrets.token_hex(prefix_length // 2)
    secret = secrets.token_urlsafe(32)
    plaintext = f"{PREFIX}_{prefix}_{secret}"
    return IssuedKey(plaintext=plaintext, prefix=prefix, key_hash=hash_secret(secret))


def hash_secret(secret: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify(plaintext_key: str, key_hash: str) -> bool:
    secret = split(plaintext_key)[1]
    if secret is None:
        return False
    try:
        algorithm, iterations, salt_hex, digest_hex = key_hash.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", secret.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate.hex(), digest_hex)


def split(plaintext_key: str) -> tuple[str | None, str | None]:
    """``gdap_<prefix>_<secret>`` → ``(prefix, secret)``; ``(None, None)`` when malformed."""
    parts = (plaintext_key or "").split("_", 2)
    if len(parts) != 3 or parts[0] != PREFIX or not parts[1] or not parts[2]:
        return None, None
    return parts[1], parts[2]
