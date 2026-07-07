"""WXBizMsgCrypt: signature determinism, AES round-trip, layout, and
cross-implementation decrypt. All pure — no network, no WeChat server.
"""
from __future__ import annotations

import base64
import hashlib
import os
import struct

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from apps.api.app.channels.wechat_crypto import (
    WeChatCryptError,
    WXBizMsgCrypt,
    compute_signature,
    verify_signature,
)

# A valid 43-char EncodingAESKey (decodes to 32 bytes with the trailing "=").
AES_KEY = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ"
TOKEN = "QDG6eK"
CORP_ID = "wx5823bde739bb4321"


def test_encoding_aes_key_decodes_to_32_bytes():
    crypto = WXBizMsgCrypt(TOKEN, AES_KEY, CORP_ID)
    assert len(crypto.key) == 32


def test_bad_encoding_aes_key_rejected():
    with pytest.raises(WeChatCryptError):
        WXBizMsgCrypt(TOKEN, "tooshort", CORP_ID)


# --------------------------------------------------------------------------
# signature
# --------------------------------------------------------------------------
def test_signature_is_sorted_sha1():
    ts, nonce, enc = "1409659589", "263014780", "encrypted_blob"
    expected = hashlib.sha1(
        "".join(sorted([TOKEN, ts, nonce, enc])).encode()
    ).hexdigest()
    assert compute_signature(TOKEN, ts, nonce, enc) == expected


def test_signature_is_order_independent_in_inputs():
    # the four inputs are sorted, so permuting nonce/timestamp yields the same
    # digest as long as the multiset of strings matches
    a = compute_signature("t", "111", "222", "zzz")
    b = compute_signature("t", "222", "111", "zzz")  # timestamp/nonce swapped
    assert a == b


def test_signature_known_vector():
    # deterministic hand-checkable vector: sorted(["A","B","C","D"]) → "ABCD"
    assert compute_signature("B", "C", "D", "A") == hashlib.sha1(b"ABCD").hexdigest()


def test_verify_signature_true_and_false():
    ts, nonce, enc = "1", "2", "cipher"
    sig = compute_signature(TOKEN, ts, nonce, enc)
    assert verify_signature(TOKEN, ts, nonce, enc, sig) is True
    assert verify_signature(TOKEN, ts, nonce, enc, "deadbeef") is False
    assert verify_signature(TOKEN, ts, nonce, enc, "") is False


# --------------------------------------------------------------------------
# AES round-trip
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "plaintext",
    [
        "",
        "hello",
        "<xml><Content><![CDATA[你好，世界]]></Content></xml>",
        "x" * 5000,  # spans many 32-byte blocks
        "emoji 🎉 mix 中文 abc",
    ],
)
def test_encrypt_decrypt_round_trip(plaintext):
    crypto = WXBizMsgCrypt(TOKEN, AES_KEY, CORP_ID)
    encrypt_b64, _sig = crypto.encrypt(plaintext, "12345", "nonce")
    assert crypto.decrypt(encrypt_b64) == plaintext


def test_encrypt_emits_valid_signature():
    crypto = WXBizMsgCrypt(TOKEN, AES_KEY, CORP_ID)
    ts, nonce = "12345", "n0nce"
    encrypt_b64, sig = crypto.encrypt("payload", ts, nonce)
    assert verify_signature(TOKEN, ts, nonce, encrypt_b64, sig)
    # ciphertext is a multiple of the 16-byte AES block
    assert len(base64.b64decode(encrypt_b64)) % 16 == 0


def test_decrypt_message_verifies_signature_first():
    crypto = WXBizMsgCrypt(TOKEN, AES_KEY, CORP_ID)
    ts, nonce = "12345", "n0nce"
    encrypt_b64, sig = crypto.encrypt("secret", ts, nonce)
    assert crypto.decrypt_message(sig, ts, nonce, encrypt_b64) == "secret"
    with pytest.raises(WeChatCryptError):
        crypto.decrypt_message("bad-sig", ts, nonce, encrypt_b64)


def test_verify_url_decrypts_echostr():
    crypto = WXBizMsgCrypt(TOKEN, AES_KEY, CORP_ID)
    ts, nonce = "111", "222"
    echo_cipher, sig = crypto.encrypt("echo-me-1234567890", ts, nonce)
    assert crypto.verify_url(sig, ts, nonce, echo_cipher) == "echo-me-1234567890"


def test_receiveid_mismatch_rejected():
    enc, _ = WXBizMsgCrypt(TOKEN, AES_KEY, "corpA").encrypt("hi", "1", "2")
    with pytest.raises(WeChatCryptError):
        WXBizMsgCrypt(TOKEN, AES_KEY, "corpB").decrypt(enc)


def test_empty_receiveid_skips_check():
    enc, _ = WXBizMsgCrypt(TOKEN, AES_KEY, "corpA").encrypt("hi", "1", "2")
    assert WXBizMsgCrypt(TOKEN, AES_KEY, "").decrypt(enc) == "hi"


# --------------------------------------------------------------------------
# cross-implementation: build the ciphertext independently, decrypt with the
# class (a genuine KAT for the 16-random || len(4) || msg || receiveid layout +
# 32-byte PKCS7 + AES-256-CBC IV=key[:16]).
# --------------------------------------------------------------------------
def _independent_encrypt(plaintext: str, key: bytes, receive_id: str) -> str:
    msg = plaintext.encode()
    raw = os.urandom(16) + struct.pack(">I", len(msg)) + msg + receive_id.encode()
    pad = 32 - (len(raw) % 32)
    pad = pad or 32
    raw += bytes([pad]) * pad
    cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
    enc = cipher.encryptor()
    return base64.b64encode(enc.update(raw) + enc.finalize()).decode()


def test_decrypt_matches_independent_encryptor():
    crypto = WXBizMsgCrypt(TOKEN, AES_KEY, CORP_ID)
    cipher_b64 = _independent_encrypt("cross-impl-payload 中文", crypto.key, CORP_ID)
    assert crypto.decrypt(cipher_b64) == "cross-impl-payload 中文"


def test_build_reply_wraps_envelope():
    crypto = WXBizMsgCrypt(TOKEN, AES_KEY, CORP_ID)
    xml = crypto.build_reply("<xml><Content>ok</Content></xml>", "1700000000", "abc")
    assert "<Encrypt>" in xml and "<MsgSignature>" in xml
    assert "<TimeStamp>1700000000</TimeStamp>" in xml
