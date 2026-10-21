# -*- coding: utf-8 -*-
"""GUI 本地 HTTP 服务的鉴权边界测试（不联网、不触发真实运行）。"""
from __future__ import annotations

import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

from gui import GuiApi, LogBuffer, _pick_port, build_handler


class GuiAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.logs = LogBuffer()
        cls.api = GuiApi(cls.logs)
        cls.token = "test-token-123"
        cls.port = _pick_port()
        cls.httpd = ThreadingHTTPServer(
            ("127.0.0.1", cls.port),
            build_handler(
                cls.api, cls.logs, cls.token, cls.port, f"http://127.0.0.1:{cls.port}/"
            ),
        )
        cls.httpd.daemon_threads = True
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        raw = r.read().decode()
        conn.close()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = raw
        return r.status, payload

    def test_api_requires_token(self):
        status, payload = self._req("GET", "/api/logs?since=0")
        self.assertEqual(status, 403)
        self.assertFalse(payload["ok"])

    def test_wrong_token_rejected(self):
        status, _ = self._req(
            "GET", "/api/logs?since=0", headers={"X-Yooc-Token": "wrong"}
        )
        self.assertEqual(status, 403)

    def test_cross_origin_rejected_even_with_token(self):
        status, _ = self._req(
            "GET",
            "/api/logs?since=0",
            headers={"X-Yooc-Token": self.token, "Origin": "http://evil.example"},
        )
        self.assertEqual(status, 403)

    def test_valid_token_allowed(self):
        status, payload = self._req(
            "GET", "/api/logs?since=0", headers={"X-Yooc-Token": self.token}
        )
        self.assertEqual(status, 200)
        self.assertIn("lines", payload)

    def test_run_endpoint_requires_token(self):
        status, _ = self._req("POST", "/api/run", body={"live": True})
        self.assertEqual(status, 403)
        self.assertFalse(self.api._running)

    def test_static_page_without_token(self):
        # 静态页允许打开（否则浏览器拿不到页面）；API 仍然受保护
        status, _ = self._req("GET", "/")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
