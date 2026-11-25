"""Password hashing (Argon2id) and at-rest encryption (Fernet) of secrets."""
from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
from starlette.concurrency import run_in_threadpool

from app.config import settings

# OWASP recommended Argon2id parameters (m=19 MiB, t=2, p=1)
_ph = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
_fernet = Fernet(settings.encryption_key.encode())


async def hash_password(password: str) -> str:
    return await run_in_threadpool(_ph.hash, password)


async def verify_password(password_hash: str, password: str) -> bool:
    def _verify() -> bool:
        try:
            return _ph.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    return await run_in_threadpool(_verify)


def encrypt(value: str | None) -> str:
    if not value:
        return ""
    return _fernet.encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str:
    if not value:
        return ""
    try:
        return _fernet.decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        return ""


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hmac_hex(message: str, purpose: str = "generic") -> str:
    key = hashlib.sha256((settings.secret_key + "|" + purpose).encode()).digest()
    return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def mask(value: str) -> str:
    """Mask a secret for display: keep last 4 chars."""
    if not value:
        return ""
    return "•" * 8 + value[-4:]
