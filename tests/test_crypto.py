# -*- coding: utf-8 -*-
"""AES-CBC 兼容性测试（无网络）。

期望值由 Node 内置 crypto 独立计算（AES-128-CBC + PKCS7 + base64），
用于证明我们的实现与前端 CryptoJS 的参数一致。
"""
from __future__ import annotations

import base64
import hashlib
import unittest

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from src.crypto import decrypt_text, derive_key, encrypt_score, encrypt_text


class CryptoCompatTests(unittest.TestCase):
    def test_derive_key(self):
        # MD5("yooc@admin"+"20002").hexdigest()[8:24]
        expect = hashlib.md5(b"yooc@admin20002").hexdigest()[8:24].encode()
        self.assertEqual(derive_key("20002"), expect)
        self.assertEqual(len(derive_key("20002")), 16)

    def test_known_vector_matches_node(self):
        # Node: aes-128-cbc(key=md5('yooc@admin20002')[8:24], iv='42e07d2f7199c35d')
        # plaintext='yooc@admin0' → base64 ciphertext
        expected = "Rd2AqYNp2UKGN7MV8moXRw=="
        got = encrypt_score("0", "20002")
        self.assertEqual(got, expected)

    def test_roundtrip_and_prefix(self):
        token = encrypt_text("hello-答案", "22603256")
        self.assertEqual(decrypt_text(token, "22603256"), "hello-答案")
        # score 加密后解密应带 "yooc@admin" 前缀
        self.assertEqual(
            decrypt_text(encrypt_score("88", "22603256"), "22603256"), "yooc@admin88"
        )

if __name__ == "__main__":
    unittest.main()
