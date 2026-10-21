# -*- coding: utf-8 -*-
"""账号隔离测试：隔离键推导、目录布局、profile_dir 覆盖语义。

不联网、不写真实 data/：全部用临时 config.yaml。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from src import config as cfgmod


def _write(tmp: Path, account: dict) -> Path:
    p = tmp / "config.yaml"
    data = {
        "account": {"group_id": 12345678, "username": "u", "password": "p", **account},
        "exam": {"dry_run": True},
        "llm": {"api_key": "sk-test"},
        "humanize": {"enabled": False},
    }
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return p


class SlugTests(unittest.TestCase):
    def test_phone_and_email_are_kept_readable(self):
        self.assertEqual(cfgmod.account_slug("13800138000"), "13800138000")
        self.assertEqual(cfgmod.account_slug("A.B@x.com"), "a_b_x_com")

    def test_chinese_falls_back_to_hash_suffix(self):
        a = cfgmod.account_slug("张三")
        b = cfgmod.account_slug("李四")
        self.assertTrue(a.startswith("u"))
        self.assertNotEqual(a, b)

    def test_empty_is_default(self):
        self.assertEqual(cfgmod.account_key_of({}), "default")
        self.assertEqual(cfgmod.account_key_of({"username": "  "}), "default")

    def test_profile_key_wins_over_username(self):
        self.assertEqual(
            cfgmod.account_key_of({"profile_key": "exam-a", "username": "13800138000"}),
            "exam_a",
        )


class PathsTests(unittest.TestCase):
    def test_default_key_keeps_legacy_layout(self):
        p = cfgmod.paths_for("default")
        self.assertEqual(p.profile_dir, cfgmod.DATA_DIR / "browser_profile")
        self.assertEqual(p.session, cfgmod.DATA_DIR / "session.json")
        self.assertEqual(p.state, cfgmod.DATA_DIR / "exam_state.json")

    def test_account_key_gets_own_subtree(self):
        p = cfgmod.paths_for("13800138000")
        root = cfgmod.PROFILES_DIR / "13800138000"
        self.assertEqual(p.root, root)
        self.assertEqual(p.profile_dir, root / "browser_profile")
        self.assertEqual(p.session, root / "session.json")
        self.assertEqual(p.guard, root / "login_guard.json")
        self.assertEqual(p.state, root / "exam_state.json")
        self.assertEqual(p.lock, root / "run.lock")
        self.assertEqual(p.log, root / "run.log")

    def test_two_accounts_never_share_files(self):
        a = cfgmod.paths_for("111")
        b = cfgmod.paths_for("222")
        for field in ("root", "profile_dir", "session", "guard", "state", "lock", "log"):
            self.assertNotEqual(getattr(a, field), getattr(b, field), field)


class LoadIsolationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        # 把账号根目录指到临时目录，避免测试在真实 data/ 下建空目录
        self._patch = mock.patch.object(cfgmod, "PROFILES_DIR", self.tmp / "profiles")
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_username_drives_profile_dir(self):
        cfg = cfgmod.load(_write(self.tmp, {"profile_key": "acc-a"}))
        self.assertEqual(cfg.account.profile_key, "acc_a")
        self.assertEqual(cfg.paths.root, cfgmod.PROFILES_DIR / "acc_a")
        self.assertEqual(cfg.account.profile_dir, cfg.paths.profile_dir)

    def test_explicit_profile_dir_is_respected(self):
        custom = self.tmp / "custom_profile"
        cfg = cfgmod.load(_write(self.tmp, {"profile_dir": str(custom)}))
        self.assertEqual(cfg.account.profile_dir, custom)
        # 显式覆盖时不再跟随账号目录，但状态/凭据仍按账号隔离
        self.assertEqual(cfg.paths.root, cfgmod.PROFILES_DIR / "u")

    def test_switching_account_switches_paths(self):
        cfg_a = cfgmod.load(_write(self.tmp, {"profile_key": "acc-a"}))
        cfg_b = cfgmod.load(_write(self.tmp, {"profile_key": "acc-b"}))
        self.assertNotEqual(cfg_a.paths.session, cfg_b.paths.session)
        self.assertNotEqual(cfg_a.paths.profile_dir, cfg_b.paths.profile_dir)


if __name__ == "__main__":
    unittest.main()
