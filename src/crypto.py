"""客户端加密/解密（与前端 bundle 的 CryptoJS 逻辑对齐）。

来自 `yooc/index.js` module 28841：

    key = MD5("yooc@admin" + yibanId).hex[8:24]   # 16 个 ASCII 字符 → 16 字节
    iv  = "42e07d2f7199c35d"                      # 16 字节
    decrypt(x) = AES-CBC(PKCS7).decrypt(base64(x))            # NX
    encrypt(x) = base64(AES-CBC(PKCS7).encrypt("yooc@admin"+x))  # zS（score）

用途：
1. `subject.answer` 是加密后的正确答案（前端本地判分用）→ 可以解密直接得到答案；
2. `submit/action` 的 `score` 字段是 `zS(总分字符串)` → 必须按同样方式加密。
"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PREFIX = "yooc@admin"
IV = b"42e07d2f7199c35d"


def derive_key(yiban_id: Any) -> bytes:
    raw = hashlib.md5(f"{PREFIX}{yiban_id}".encode("utf-8")).hexdigest()[8:24]
    return raw.encode("utf-8")


def _aes(key: bytes):
    return Cipher(algorithms.AES(key), modes.CBC(IV))


def decrypt_text(encrypted_b64: str, yiban_id: Any) -> str:
    data = base64.b64decode(str(encrypted_b64))
    dec = _aes(derive_key(yiban_id)).decryptor()
    padded = dec.update(data) + dec.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    plain = unpadder.update(padded) + unpadder.finalize()
    return plain.decode("utf-8", errors="replace")


def encrypt_text(plain: str, yiban_id: Any) -> str:
    body = str(plain).encode("utf-8")
    padder = padding.PKCS7(128).padder()
    padded = padder.update(body) + padder.finalize()
    enc = _aes(derive_key(yiban_id)).encryptor()
    return base64.b64encode(enc.update(padded) + enc.finalize()).decode("ascii")


def decrypt_answer_json(encrypted: Any, yiban_id: Any) -> Any:
    text = decrypt_text(encrypted, yiban_id)
    return json.loads(text)


def encrypt_score(total_points: Any, yiban_id: Any) -> str:
    # 前端 zS：加密前拼 "yooc@admin" 前缀
    return encrypt_text(f"{PREFIX}{total_points}", yiban_id)
