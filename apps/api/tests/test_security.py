"""Pure-logic tests: argon2, JWT roundtrip, envelope encryption."""
from __future__ import annotations

from datetime import timedelta

import pytest

from apps.api.app.services.security import (
    EnvelopeCipher,
    TokenInvalid,
    generate_api_token,
    generate_master_key,
    hash_api_token,
    hash_password,
    issue_token,
    issue_token_pair,
    verify_password,
    verify_token,
)


def test_password_hash_roundtrip():
    h = hash_password("s3cret-pw")
    assert h != "s3cret-pw"
    assert h.startswith("$argon2")
    assert verify_password("s3cret-pw", h)
    assert not verify_password("wrong", h)
    assert not verify_password("s3cret-pw", "not-a-hash")


def test_jwt_roundtrip():
    pair = issue_token_pair("11111111-1111-7111-8111-111111111111")
    claims = verify_token(pair["access_token"], expected_type="access")
    assert claims["sub"] == "11111111-1111-7111-8111-111111111111"
    rclaims = verify_token(pair["refresh_token"], expected_type="refresh")
    assert rclaims["typ"] == "refresh"
    assert claims["jti"] != rclaims["jti"]


def test_jwt_wrong_type_rejected():
    access = issue_token("u1", token_type="access")
    with pytest.raises(TokenInvalid):
        verify_token(access, expected_type="refresh")


def test_jwt_expired_rejected():
    tok = issue_token("u1", token_type="access", ttl=timedelta(seconds=-5))
    with pytest.raises(TokenInvalid):
        verify_token(tok)


def test_jwt_tampered_rejected():
    tok = issue_token("u1", token_type="access")
    with pytest.raises(TokenInvalid):
        verify_token(tok[:-2] + "xx")


def test_envelope_roundtrip():
    cipher = EnvelopeCipher(generate_master_key())
    dk = cipher.new_wrapped_data_key()
    blob = cipher.encrypt_json(dk, {"api_key": "wa-secret", "n": 7})
    assert b"wa-secret" not in blob
    assert cipher.decrypt_json(dk, blob) == {"api_key": "wa-secret", "n": 7}


def test_envelope_distinct_data_keys():
    cipher = EnvelopeCipher(generate_master_key())
    assert cipher.new_wrapped_data_key() != cipher.new_wrapped_data_key()


def test_envelope_tamper_detected():
    cipher = EnvelopeCipher(generate_master_key())
    dk = cipher.new_wrapped_data_key()
    blob = bytearray(cipher.encrypt(dk, b"payload"))
    blob[-1] ^= 0xFF
    with pytest.raises(ValueError):
        cipher.decrypt(dk, bytes(blob))


def test_envelope_wrong_master_key():
    c1 = EnvelopeCipher(generate_master_key())
    c2 = EnvelopeCipher(generate_master_key())
    dk = c1.new_wrapped_data_key()
    with pytest.raises(ValueError):
        c2.decrypt(dk, c1.encrypt(dk, b"x"))


def test_envelope_passphrase_master_key():
    """Non-Fernet passphrases are derived, and derivation is stable."""
    a = EnvelopeCipher("just a passphrase")
    b = EnvelopeCipher("just a passphrase")
    dk = a.new_wrapped_data_key()
    assert b.decrypt(dk, a.encrypt(dk, b"hello")) == b"hello"


def test_api_token_hashing():
    token, token_hash, prefix = generate_api_token()
    assert token.startswith("sct_")
    assert token.startswith(prefix)
    assert hash_api_token(token) == token_hash
    assert len(token_hash) == 64
