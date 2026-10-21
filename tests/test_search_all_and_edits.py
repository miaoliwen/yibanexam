# -*- coding: utf-8 -*-
"""全量搜索核实 + 答题卡修改 的测试。全部离线。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.config import Config
from src.llm import LLMSolver
from src.parser import Option, Question
from src.search import SearchHit


def _q(qid, conf=0.9, keys=("A",), qtype="single"):
    return Question(
        qid=qid,
        section_id=1,
        index=int(qid),
        stem=f"题目 {qid}：下列说法正确的是？",
        qtype=qtype,
        options=[Option("A", "甲"), Option("B", "乙"), Option("C", "丙")],
        answer_keys=list(keys),
        confidence=conf,
        reason="原有判断",
    )


class FakeSearcher:
    def __init__(self, hits):
        self.hits = hits
        self.queries = []

    def search(self, query, ori_question=None):
        self.queries.append(query)
        return list(self.hits)


def _solver(hits, reply=None, *, all_q=True, adopt=0.7):
    cfg = Config()
    cfg.llm.api_key = "sk-test"
    cfg.llm.search_all_questions = all_q
    cfg.llm.search_adopt_confidence = adopt
    cfg.llm.search_max_questions = 0
    cfg.llm.concurrency = 1
    cfg.modules.rules = []
    s = LLMSolver(cfg)
    s.searcher = FakeSearcher(hits)
    s._search_off = False
    s._chat = lambda msgs, json_mode=True: json.dumps(
        reply or {"answer": ["B"], "text": "", "confidence": 0.9, "reason": "资料支持"}
    )
    return s


class SearchAllTests(unittest.TestCase):
    def test_high_confidence_is_also_searched(self):
        hits = [SearchHit(title="t", url="https://e.com/1", snippet="s", summary="材料", score=1.5)]
        s = _solver(hits)
        q = _q(1, conf=0.99)
        st = s.refine_low_confidence([q])
        self.assertEqual(len(s.searcher.queries), 1)
        self.assertEqual(st["searched"], 1)
        self.assertEqual(q.answer_keys, ["B"])   # 全量模式下 0.9 >= 0.7 直接采纳
        self.assertTrue(q.verified)

    def test_low_confidence_only_by_default(self):
        hits = [SearchHit(title="t", url="https://e.com/1", snippet="s", summary="材料", score=1.5)]
        s = _solver(hits, all_q=False)
        q = _q(2, conf=0.99)
        s.refine_low_confidence([q])
        self.assertEqual(s.searcher.queries, [])  # 高置信题不搜

    def test_no_hits_marks_unverified_and_keeps_answer(self):
        s = _solver([])
        q = _q(3, conf=0.6, keys=("A",))
        st = s.refine_low_confidence([q])
        self.assertEqual(st["unverified"], 1)
        self.assertEqual(q.answer_keys, ["A"])
        self.assertFalse(q.verified)

    def test_low_confidence_reply_is_not_adopted(self):
        hits = [SearchHit(title="t", url="https://e.com/1", snippet="s", summary="材料", score=1.5)]
        s = _solver(hits, reply={"answer": ["C"], "text": "", "confidence": 0.3, "reason": "不确定"})
        q = _q(4, conf=0.6, keys=("A",))
        s.refine_low_confidence([q])
        self.assertEqual(q.answer_keys, ["A"])
        self.assertFalse(q.verified)

    def test_already_verified_is_skipped(self):
        hits = [SearchHit(title="t", url="https://e.com/1", snippet="s", summary="材料", score=1.5)]
        s = _solver(hits)
        q = _q(5, conf=0.5)
        q.verified = True
        s.refine_low_confidence([q])
        self.assertEqual(s.searcher.queries, [])


class AnswerSheetTests(unittest.TestCase):
    def setUp(self):
        import gui

        self.gui = gui
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "data").mkdir()
        # 账号隔离后，答题卡/状态按账号目录定位；测试把它指到临时目录
        from src.config import AccountPaths

        root = self.dir / "data"
        paths = AccountPaths(
            key="test",
            root=root,
            profile_dir=root / "browser_profile",
            session=root / "session.json",
            guard=root / "login_guard.json",
            state=root / "exam_state.json",
            lock=root / "run.lock",
            log=root / "run.log",
        )
        self._patch = mock.patch.object(gui.GuiApi, "_acc_paths", lambda _self: paths)
        self._patch.start()
        self.api = gui.GuiApi(gui.LogBuffer())
        self._write_state()
        self._write_sheet()

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()

    def _write_state(self):
        st = {
            "exam_id": "603733",
            "examuser_id": "EU1",
            "phase": "solved",
            "submitted": False,
            "answers": {"101": {"answer": ["A"], "text": "", "confidence": 0.9, "verified": True}},
        }
        (self.dir / "data" / "exam_state.json").write_text(
            json.dumps(st, ensure_ascii=False), encoding="utf-8"
        )

    def _write_sheet(self):
        sheet = {
            "exam_id": "603733",
            "exam_name": "测试考试",
            "editable": True,
            "items": [
                {
                    "qid": "101",
                    "index": 1,
                    "qtype": "single",
                    "stem": "题干",
                    "options": [{"key": "A", "text": "甲"}, {"key": "B", "text": "乙"}],
                    "answer": ["A"],
                    "text": "",
                    "verified": True,
                }
            ],
        }
        (self.dir / "data" / "answer_sheet.json").write_text(
            json.dumps(sheet, ensure_ascii=False), encoding="utf-8"
        )

    def _state(self):
        return json.loads((self.dir / "data" / "exam_state.json").read_text(encoding="utf-8"))

    def test_edit_answer_before_submit(self):
        r = self.api.save_answer_sheet({"101": {"answer": ["B"]}})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["changed"], 1)
        ans = self._state()["answers"]["101"]
        self.assertEqual(ans["answer"], ["B"])
        self.assertTrue(ans["edited"])

    def test_invalid_option_rejected(self):
        r = self.api.save_answer_sheet({"101": {"answer": ["Z"]}})
        self.assertFalse(r["ok"])
        self.assertEqual(self._state()["answers"]["101"]["answer"], ["A"])

    def test_refuses_after_submit(self):
        st = self._state()
        st["submitted"] = True
        st["phase"] = "submitted"
        (self.dir / "data" / "exam_state.json").write_text(
            json.dumps(st, ensure_ascii=False), encoding="utf-8"
        )
        r = self.api.save_answer_sheet({"101": {"answer": ["B"]}})
        self.assertFalse(r["ok"])
        self.assertIn("已交卷", r["msg"])

    def test_load_marks_not_editable_after_submit(self):
        st = self._state()
        st["phase"] = "done"
        (self.dir / "data" / "exam_state.json").write_text(
            json.dumps(st, ensure_ascii=False), encoding="utf-8"
        )
        r = self.api.load_answer_sheet()
        self.assertTrue(r["ok"])
        self.assertFalse(r["editable"])

    def test_single_choice_keeps_only_first(self):
        self.api.save_answer_sheet({"101": {"answer": ["A", "B"]}})
        self.assertEqual(self._state()["answers"]["101"]["answer"], ["A"])


class LiveStatusTests(unittest.TestCase):
    """前端状态条：runner 事件 → 结构化状态。"""

    def setUp(self):
        import gui

        self.api = gui.GuiApi(gui.LogBuffer())

    def test_solve_and_search_progress(self):
        self.api._on_event("phase", {"name": "solve", "total": 50})
        self.api._on_event("solve", {"done": 12, "total": 50})
        self.api._on_event("search", {"phase": "start", "total": 50, "all": True})
        self.api._on_event(
            "search",
            {"done": 7, "total": 50, "verified": 6, "unverified": 1, "revised": 2, "failed": 0},
        )
        self.api._on_event(
            "search_item", {"index": 12, "stem": "题干", "ok": True, "note": "已核实并改判"}
        )
        st = self.api.get_live_status()
        self.assertEqual(st["phase"], "solve")
        self.assertEqual(st["solve"]["done"], 12)
        self.assertEqual(st["search"]["state"], "running")
        self.assertTrue(st["search"]["all"])
        self.assertEqual(st["search"]["done"], 7)
        self.assertEqual(st["search"]["verified"], 6)
        self.assertEqual(st["search"]["unverified"], 1)
        self.assertEqual(st["search"]["last"]["index"], 12)
        self.assertTrue(st["search"]["last"]["ok"])

    def test_done_fills_total(self):
        self.api._on_event("search", {"phase": "start", "total": 50, "all": True})
        self.api._on_event(
            "search", {"phase": "done", "verified": 47, "unverified": 3, "revised": 4, "failed": 0}
        )
        st = self.api.get_live_status()["search"]
        self.assertEqual(st["state"], "done")
        self.assertEqual(st["done"], 50)
        self.assertEqual(st["revised"], 4)

    def test_unavailable(self):
        self.api._on_event("search", {"phase": "unavailable", "reason": "接口 404"})
        st = self.api.get_live_status()["search"]
        self.assertEqual(st["state"], "unavailable")
        self.assertIn("404", st["reason"])

    def test_reset(self):
        self.api._on_event("search", {"phase": "start", "total": 5, "all": True})
        self.api.reset_live()
        st = self.api.get_live_status()["search"]
        self.assertEqual(st["state"], "idle")
        self.assertEqual(st["total"], 0)


if __name__ == "__main__":
    unittest.main()
