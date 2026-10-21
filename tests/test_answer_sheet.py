# -*- coding: utf-8 -*-
"""答题卡接口测试：读取、人工改答案、已交卷锁定。

用临时目录 + 替换 _acc_paths，不碰真实 data/。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import gui
from src.config import AccountPaths


def _paths(root: Path) -> AccountPaths:
    return AccountPaths(
        key="test",
        root=root,
        profile_dir=root / "browser_profile",
        session=root / "session.json",
        guard=root / "login_guard.json",
        state=root / "exam_state.json",
        lock=root / "run.lock",
        log=root / "run.log",
    )


class AnswerSheetTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.api = gui.GuiApi(gui.LogBuffer())
        self.api._acc_paths = lambda: _paths(self.root)

        self.sheet = {
            "exam_id": "603733",
            "examuser_id": "EU1",
            "editable": True,
            "count": 2,
            "verified_count": 1,
            "items": [
                {
                    "qid": "101",
                    "index": 1,
                    "qtype": "single",
                    "stem": "1+1=?",
                    "options": [{"key": "A", "text": "1"}, {"key": "B", "text": "2"}],
                    "answer": ["A"],
                    "text": "",
                    "confidence": 0.9,
                    "reason": "[本地题库]",
                    "verified": True,
                    "search_checked": False,
                },
                {
                    "qid": "102",
                    "index": 2,
                    "qtype": "fill",
                    "stem": "填空",
                    "options": [],
                    "answer": [],
                    "text": "旧答案",
                    "confidence": 0.6,
                    "reason": "",
                    "verified": False,
                    "search_checked": False,
                },
            ],
        }
        (self.root / "answer_sheet.json").write_text(
            json.dumps(self.sheet, ensure_ascii=False), encoding="utf-8"
        )
        self.state = {
            "group_id": 12345678,
            "exam_id": "603733",
            "examuser_id": "EU1",
            "phase": "solved",
            "submitted": False,
            "answers": {
                "101": {"answer": ["A"], "text": "", "confidence": 0.9},
                "102": {"answer": [], "text": "旧答案", "confidence": 0.6},
            },
        }
        (self.root / "exam_state.json").write_text(
            json.dumps(self.state, ensure_ascii=False), encoding="utf-8"
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _read_state(self) -> dict:
        return json.loads((self.root / "exam_state.json").read_text(encoding="utf-8"))

    def test_load_returns_items_and_editable(self):
        r = self.api.load_answer_sheet()
        self.assertTrue(r["ok"])
        self.assertTrue(r["editable"])
        self.assertEqual(len(r["items"]), 2)

    def test_save_choice_answer_updates_state(self):
        r = self.api.save_answer_sheet({"101": {"answer": ["B"], "text": ""}})
        self.assertTrue(r["ok"])
        self.assertEqual(r["changed"], 1)
        st = self._read_state()
        self.assertEqual(st["answers"]["101"]["answer"], ["B"])
        self.assertTrue(st["answers"]["101"]["edited"])
        sheet = json.loads((self.root / "answer_sheet.json").read_text(encoding="utf-8"))
        self.assertEqual(sheet["items"][0]["answer"], ["B"])

    def test_save_fill_text(self):
        r = self.api.save_answer_sheet({"102": {"answer": [], "text": "新答案"}})
        self.assertTrue(r["ok"])
        self.assertEqual(self._read_state()["answers"]["102"]["text"], "新答案")

    def test_invalid_option_rejected(self):
        r = self.api.save_answer_sheet({"101": {"answer": ["Z"], "text": ""}})
        self.assertFalse(r["ok"])
        self.assertIn("不是有效选项", r["msg"])
        # 原答案不受影响
        self.assertEqual(self._read_state()["answers"]["101"]["answer"], ["A"])

    def test_submitted_sheet_is_locked(self):
        self.state["submitted"] = True
        self.state["phase"] = "submitted"
        (self.root / "exam_state.json").write_text(
            json.dumps(self.state, ensure_ascii=False), encoding="utf-8"
        )
        r = self.api.load_answer_sheet()
        self.assertFalse(r["editable"])
        r2 = self.api.save_answer_sheet({"101": {"answer": ["B"], "text": ""}})
        self.assertFalse(r2["ok"])
        self.assertIn("已交卷", r2["msg"])

    def test_running_blocks_edit(self):
        self.api._running = True
        r = self.api.save_answer_sheet({"101": {"answer": ["B"], "text": ""}})
        self.assertFalse(r["ok"])
        self.assertIn("运行", r["msg"])

    def test_missing_sheet(self):
        (self.root / "answer_sheet.json").unlink()
        r = self.api.load_answer_sheet()
        self.assertFalse(r["ok"])
        self.assertIn("还没有答题卡", r["msg"])


if __name__ == "__main__":
    unittest.main()
