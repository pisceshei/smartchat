"""Security primitives: argon2 password hashing, JWT issue/verify, and
envelope encryption for channel credentials.

Envelope scheme (plan A.0): each workspace gets a random Fernet data key; the
data key is wrapped (encrypted) by CREDENTIALS_MASTER_KEY and stored on
workspaces.data_key_enc. Credentials are encrypted with the unwrapped data
key. Rotating the master key = re-wrap N small keys, not N credential blobs.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from cryptography.fernet import Fernet, InvalidToken
from jose import JWTError, jwt
from passlib.context import CryptContext

from ..settings import get_settings

pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

ALGORITHM = "HS256"
TokenType = Literal["access", "refresh"]


class TokenInvalid(Exception):
    pass


# --------------------------------------------------------------------------
# passwords
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return pwd_context.verify(password, password_hash)
    except Exception:
        return False


# --------------------------------------------------------------------------
# JWT
# --------------------------------------------------------------------------
def issue_token(
    user_id: uuid.UUID | str,
    *,
    token_type: TokenType,
    ttl: timedelta | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    s = get_settings()
    if ttl is None:
        ttl = (
            timedelta(minutes=s.access_token_ttl_min)
            if token_type == "access"
            else timedelta(days=s.refresh_token_ttl_days)
        )
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "typ": token_type,
        "jti": secrets.token_hex(8),
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
    }
    if extra:
        claims.update(extra)
    return jwt.encode(claims, s.secret_key, algorithm=ALGORITHM)


def issue_token_pair(user_id: uuid.UUID | str) -> dict[str, str]:
    return {
        "access_token": issue_token(user_id, token_type="access"),
        "refresh_token": issue_token(user_id, token_type="refresh"),
        "token_type": "bearer",
    }


def verify_token(token: str, *, expected_type: TokenType = "access") -> dict[str, Any]:
    """Returns claims or raises TokenInvalid (expired / bad sig / wrong typ)."""
    s = get_settings()
    try:
        claims = jwt.decode(token, s.secret_key, algorithms=[ALGORITHM])
    except JWTError as e:
        raise TokenInvalid(str(e)) from e
    if claims.get("typ") != expected_type:
        raise TokenInvalid(f"expected {expected_type} token")
    if "sub" not in claims:
        raise TokenInvalid("missing sub")
    return claims


# --------------------------------------------------------------------------
# envelope encryption
# --------------------------------------------------------------------------
def _fernet_key_from_secret(secret: str) -> bytes:
    """Accept a proper urlsafe-b64 32-byte Fernet key, or derive one from an
    arbitrary passphrase (dev convenience)."""
    try:
        raw = base64.urlsafe_b64decode(secret.encode())
        if len(raw) == 32:
            return secret.encode()
    except Exception:
        pass
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())


def generate_master_key() -> str:
    return Fernet.generate_key().decode()


class EnvelopeCipher:
    """Wraps/unwraps per-workspace data keys and encrypts payloads with them."""

    def __init__(self, master_key: str | None = None):
        secret = master_key or get_settings().credentials_master_key or get_settings().secret_key
        self._master = Fernet(_fernet_key_from_secret(secret))

    def new_wrapped_data_key(self) -> bytes:
        """Generate a fresh workspace data key, returned wrapped (store this
        on workspaces.data_key_enc)."""
        return self._master.encrypt(Fernet.generate_key())

    def _data_fernet(self, wrapped_data_key: bytes) -> Fernet:
        try:
            return Fernet(self._master.decrypt(bytes(wrapped_data_key)))
        except InvalidToken as e:
            raise ValueError("cannot unwrap data key (wrong master key?)") from e

    def encrypt(self, wrapped_data_key: bytes, plaintext: bytes) -> bytes:
        return self._data_fernet(wrapped_data_key).encrypt(plaintext)

    def decrypt(self, wrapped_data_key: bytes, token: bytes) -> bytes:
        try:
            return self._data_fernet(wrapped_data_key).decrypt(bytes(token))
        except InvalidToken as e:
            raise ValueError("credential blob corrupt or tampered") from e

    def encrypt_json(self, wrapped_data_key: bytes, obj: Any) -> bytes:
        return self.encrypt(wrapped_data_key, json.dumps(obj, separators=(",", ":")).encode())

    def decrypt_json(self, wrapped_data_key: bytes, token: bytes) -> Any:
        return json.loads(self.decrypt(wrapped_data_key, token).decode())

    def rewrap(self, old: EnvelopeCipher, wrapped_data_key: bytes) -> bytes:
        """Master key rotation: unwrap with old master, wrap with this one."""
        return self._master.encrypt(old._master.decrypt(bytes(wrapped_data_key)))


_cipher: EnvelopeCipher | None = None


def get_cipher() -> EnvelopeCipher:
    global _cipher
    if _cipher is None:
        _cipher = EnvelopeCipher()
    return _cipher


# --------------------------------------------------------------------------
# API tokens (OpenAPI surface)
# --------------------------------------------------------------------------
def generate_api_token() -> tuple[str, str, str]:
    """Returns (plaintext_token, sha256_hash, display_prefix). Only hash+prefix
    are stored; plaintext is shown once."""
    token = "sct_" + secrets.token_urlsafe(32)
    return token, hash_api_token(token), token[:12]


def hash_api_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
