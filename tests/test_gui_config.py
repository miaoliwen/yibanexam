# -*- coding: utf-8 -*-
"""GUI 配置保存的损坏保护测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import gui


class CorruptConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.path = self.tmp / "config.yaml"
        self.path.write_text("account: [broken\n", encoding="utf-8")
        self.api = gui.GuiApi(gui.LogBuffer())
        self._patch = mock.patch.object(gui, "CONFIG_PATH", self.path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_save_refuses_to_overwrite_corrupt_config(self):
        before = self.path.read_text(encoding="utf-8")
        r = self.api.save_config({"username": "u2", "api_key": "new-key"})
        self.assertFalse(r["ok"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        backups = list(self.tmp.glob("config.yaml.corrupt.*"))
        self.assertEqual(len(backups), 1)

    def test_empty_config_is_treated_as_new(self):
        self.path.write_text("", encoding="utf-8")
        self.assertIsNone(self.api._raw_error())
        r = self.api.save_config({"username": "u", "group_id": "1"})
        self.assertTrue(r["ok"])


if __name__ == "__main__":
    unittest.main()
