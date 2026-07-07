"""WXBizMsgCrypt — WeChat / WeCom callback message encryption (shared by the
``wecom`` and ``wechat_kf`` adapters).

Tencent's scheme (identical for 企業微信 self-built apps and 微信客服):

  * ``msg_signature = sha1(sorted(token, timestamp, nonce, encrypt))`` — the four
    strings are lexicographically sorted, concatenated, then SHA1-hex'd.
  * The ciphertext is AES-256-CBC, ``AESKey = base64decode(EncodingAESKey + "=")``
    (43-char key → 32 bytes), ``IV = AESKey[:16]``, PKCS#7 padding with a **32-byte**
    block (Tencent's quirk — the pad amount is 1..32, still a multiple of the 16-byte
    AES block so CBC is happy).
  * Plaintext layout before padding:
        ``random(16) || network_len(4, big-endian) || msg || receiveid``
    where ``receiveid`` is the CorpID (both WeCom and WeChat 客服 live under a corp).

Everything here is pure (no I/O) and unit-tested: round-trip ``encrypt→decrypt``
is the identity, and the signature is deterministic + order-independent.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_PKCS7_BLOCK = 32  # Tencent pads to a 32-byte block (see module docstring)


class WeChatCryptError(ValueError):
    """Raised on signature mismatch, malformed ciphertext or receiveid mismatch."""


# --------------------------------------------------------------------------
# signature
# --------------------------------------------------------------------------
def compute_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """dev msg_signature = hex(sha1(''.join(sorted([token, timestamp, nonce, encrypt]))))."""
    joined = "".join(sorted([token, timestamp, nonce, encrypt]))
    return hashlib.sha1(joined.encode()).hexdigest()  # noqa: S324 — spec-mandated SHA1


def verify_signature(
    token: str, timestamp: str, nonce: str, encrypt: str, msg_signature: str
) -> bool:
    return hmac.compare_digest(
        compute_signature(token, timestamp, nonce, encrypt), (msg_signature or "").strip()
    )


# --------------------------------------------------------------------------
# PKCS#7 (block size 32)
# --------------------------------------------------------------------------
def _pkcs7_pad(data: bytes) -> bytes:
    pad = _PKCS7_BLOCK - (len(data) % _PKCS7_BLOCK)
    if pad == 0:
        pad = _PKCS7_BLOCK
    return data + bytes([pad]) * pad


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if pad < 1 or pad > _PKCS7_BLOCK:
        return data  # lenient, mirrors Tencent's reference decoder
    return data[:-pad]


def _aes_key(encoding_aes_key: str) -> bytes:
    try:
        key = base64.b64decode(encoding_aes_key + "=")
    except (ValueError, TypeError) as e:  # binascii.Error subclasses ValueError
        raise WeChatCryptError(f"EncodingAESKey is not valid base64: {e}") from e
    if len(key) != 32:
        raise WeChatCryptError("EncodingAESKey must decode to 32 bytes (43-char key)")
    return key


class WXBizMsgCrypt:
    """Encrypt/decrypt + verify for one channel account.

    ``receive_id`` is the CorpID; it is embedded on encrypt and checked on decrypt
    (pass an empty string to skip the check, e.g. before the id is known)."""

    def __init__(self, token: str, encoding_aes_key: str, receive_id: str):
        self.token = token
        self.key = _aes_key(encoding_aes_key)
        self.receive_id = receive_id or ""

    # -- low-level AES ------------------------------------------------------
    def _encrypt_bytes(self, data: bytes) -> bytes:
        cipher = Cipher(algorithms.AES(self.key), modes.CBC(self.key[:16]))
        enc = cipher.encryptor()
        return enc.update(_pkcs7_pad(data)) + enc.finalize()

    def _decrypt_bytes(self, data: bytes) -> bytes:
        if not data or len(data) % 16 != 0:
            raise WeChatCryptError("ciphertext length is not a multiple of the AES block")
        cipher = Cipher(algorithms.AES(self.key), modes.CBC(self.key[:16]))
        dec = cipher.decryptor()
        return _pkcs7_unpad(dec.update(data) + dec.finalize())

    # -- public API ---------------------------------------------------------
    def encrypt(self, plaintext: str, timestamp: str, nonce: str) -> tuple[str, str]:
        """Return (encrypt_b64, msg_signature) for a reply envelope."""
        msg = plaintext.encode()
        raw = os.urandom(16) + struct.pack(">I", len(msg)) + msg + self.receive_id.encode()
        encrypt_b64 = base64.b64encode(self._encrypt_bytes(raw)).decode()
        return encrypt_b64, compute_signature(self.token, timestamp, nonce, encrypt_b64)

    def decrypt(self, encrypt_b64: str) -> str:
        """Decrypt a base64 <Encrypt> blob → the inner plaintext (XML/JSON string).
        Verifies the trailing receiveid when one was configured."""
        try:
            raw = self._decrypt_bytes(base64.b64decode(encrypt_b64))
        except WeChatCryptError:
            raise
        except Exception as e:  # noqa: BLE001 — malformed base64 / AES error
            raise WeChatCryptError(f"decrypt failed: {e}") from e
        content = raw[16:]  # strip the 16-byte random prefix
        if len(content) < 4:
            raise WeChatCryptError("decrypted payload too short")
        msg_len = struct.unpack(">I", content[:4])[0]
        if 4 + msg_len > len(content):
            raise WeChatCryptError("declared message length exceeds payload")
        msg = content[4 : 4 + msg_len]
        receive_id = content[4 + msg_len :].decode(errors="replace")
        if self.receive_id and receive_id != self.receive_id:
            raise WeChatCryptError("receiveid mismatch")
        return msg.decode()

    def decrypt_message(
        self, msg_signature: str, timestamp: str, nonce: str, encrypt_b64: str
    ) -> str:
        """Verify msg_signature over the ciphertext, then decrypt."""
        if not verify_signature(self.token, timestamp, nonce, encrypt_b64, msg_signature):
            raise WeChatCryptError("bad msg_signature")
        return self.decrypt(encrypt_b64)

    def verify_url(
        self, msg_signature: str, timestamp: str, nonce: str, echostr: str
    ) -> str:
        """GET server-URL handshake: verify + decrypt the (encrypted) echostr and
        return the plaintext to echo back."""
        return self.decrypt_message(msg_signature, timestamp, nonce, echostr)

    def build_reply(self, plaintext: str, timestamp: str, nonce: str) -> str:
        """Encrypt a passive reply and wrap it in the WeChat XML envelope."""
        encrypt_b64, signature = self.encrypt(plaintext, timestamp, nonce)
        return (
            "<xml>"
            f"<Encrypt><![CDATA[{encrypt_b64}]]></Encrypt>"
            f"<MsgSignature><![CDATA[{signature}]]></MsgSignature>"
            f"<TimeStamp>{timestamp}</TimeStamp>"
            f"<Nonce><![CDATA[{nonce}]]></Nonce>"
            "</xml>"
        )


__all__ = [
    "WXBizMsgCrypt",
    "WeChatCryptError",
    "compute_signature",
    "verify_signature",
]
