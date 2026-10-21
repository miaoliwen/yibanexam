# -*- coding: utf-8 -*-
"""桌面版（tkinter）启动体检测试：缺 tkinter 时给出可执行提示并以 2 退出。"""
from __future__ import annotations

import contextlib
import importlib
import importlib.util
import io
import sys
import unittest
from unittest import mock


class GuiTkPreflightTests(unittest.TestCase):
    def _import_with_missing(self, missing: str) -> tuple[int, str]:
        real = importlib.util.find_spec

        def fake(name, *a, **k):
            if name == missing:
                return None
            return real(name, *a, **k)

        buf = io.StringIO()
        sys.modules.pop("gui_tk", None)
        with mock.patch("importlib.util.find_spec", fake):
            with contextlib.redirect_stderr(buf):
                with self.assertRaises(SystemExit) as cm:
                    importlib.import_module("gui_tk")
        sys.modules.pop("gui_tk", None)
        return cm.exception.code, buf.getvalue()

    def test_missing_tkinter_exits_with_hint(self):
        code, out = self._import_with_missing("tkinter")
        self.assertEqual(code, 2)
        self.assertIn("tkinter", out)
        self.assertIn("run-tk.bat", out)
        self.assertIn("yibanexam.exe", out)

    def test_import_ok_on_system_python(self):
        if importlib.util.find_spec("tkinter") is None:
            self.skipTest("当前解释器没有 tkinter，跳过导入检查")
        sys.modules.pop("gui_tk", None)
        mod = importlib.import_module("gui_tk")
        self.assertTrue(hasattr(mod, "tk"))
        self.assertTrue(hasattr(mod, "App") or hasattr(mod, "main"))


if __name__ == "__main__":
    unittest.main()
