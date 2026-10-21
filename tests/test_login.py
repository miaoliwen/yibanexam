# -*- coding: utf-8 -*-
"""登录层测试：错误分类、本地熔断、会话持久化（全部离线）。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import login
from src.config import Config
from src.errors import LoginError


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class ExtractErrorTests(unittest.TestCase):
    def test_wrong_password(self):
        msg, kind = login._extract_error("<div>账号或密码不正确</div>")
        self.assertEqual(kind, "credentials")
        self.assertIn("密码", msg)

    def test_rate_limited(self):
        msg, kind = login._extract_error("<div>操作频繁，请稍后再试</div>")
        self.assertEqual(kind, "rate_limited")

    def test_captcha(self):
        msg, kind = login._extract_error("<div>请完成验证码</div>")
        self.assertEqual(kind, "captcha")

    def test_unknown(self):
        _, kind = login._extract_error("<div>something else</div>")
        self.assertEqual(kind, "unknown")


class GuardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "guard.json"
        self.clock = FakeClock()

    def tearDown(self):
        self._tmp.cleanup()

    def test_blocks_after_max_attempts(self):
        g = login.LoginGuard(
            self.path,
            max_attempts=2,
            window_seconds=600,
            min_interval_seconds=0,
            clock=self.clock,
        )
        g.precheck()
        g.record_attempt()
        g.record_attempt()
        with self.assertRaises(LoginError) as cm:
            g.precheck()
        self.assertEqual(cm.exception.kind, "rate_limited")

    def test_window_expires(self):
        g = login.LoginGuard(
            self.path,
            max_attempts=1,
            window_seconds=60,
            min_interval_seconds=0,
            clock=self.clock,
        )
        g.record_attempt()
        self.clock.t += 61
        g.precheck()  # 不应抛

    def test_min_interval(self):
        g = login.LoginGuard(
            self.path,
            max_attempts=10,
            window_seconds=600,
            min_interval_seconds=8,
            clock=self.clock,
        )
        g.record_attempt()
        with self.assertRaises(LoginError) as cm:
            g.precheck()
        self.assertEqual(cm.exception.kind, "rate_limited")

    def test_corrupt_guard_file_tolerated(self):
        self.path.write_text("not json", encoding="utf-8")
        g = login.LoginGuard(self.path, clock=self.clock)
        g.precheck()  # 不应崩


class SessionPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = Config()
        self.cfg.account.group_id = 12345678
        self._patch = mock.patch.object(login, "SESSION_PATH", self.tmp / "session.json")
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_roundtrip_creds_and_cookies(self):
        creds = {"user_id": "226", "user_token": "tok", "yiban_id": "y1"}
        cookies = [
            {
                "name": "user_token",
                "value": "tok",
                "domain": ".yooc.me",
                "path": "/",
                "secure": True,
                "expires": None,
            }
        ]
        login.save_session(self.cfg, creds, cookies)
        self.assertEqual(login.load_saved(self.cfg), creds)
        self.assertEqual(login.load_saved_cookies(self.cfg)[0]["name"], "user_token")

    def test_group_mismatch_rejected(self):
        login.save_session(
            self.cfg, {"user_id": "1", "user_token": "t", "yiban_id": "y"}, []
        )
        other = Config()
        other.account.group_id = 999
        self.assertIsNone(login.load_saved(other))

    def test_corrupt_session_returns_none(self):
        (self.tmp / "session.json").write_text("bad", encoding="utf-8")
        self.assertIsNone(login.load_saved(self.cfg))


if __name__ == "__main__":
    unittest.main()
