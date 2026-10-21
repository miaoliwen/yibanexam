# -*- coding: utf-8 -*-
"""staged 答题流程测试：题库 -> 模型二次校验 -> 搜索 N 轮 -> 停住等人工。

全部用 fake api / fake solver，不联网、不写真实 data/。
"""
from __future__ import annotations

import unittest

from tests.test_runner import FakeSolver, RunnerTestBase, _cfg


class StagedSolver(FakeSolver):
    """记录 verify / search 调用次数，便于断言流程顺序。"""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.searcher = object()          # 非 None 即视为搜索可用
        self._search_off = False
        self.search_stats = {
            "searched": 0,
            "revised": 0,
            "failed": 0,
            "unverified": 0,
            "verified": 0,
        }
        self.verify_calls = 0
        self.verify_seen: list[str] = []
        self.search_rounds = 0
        self.force_flags: list[bool] = []
        self.mark_checked = True

    def verify_answers(self, questions, progress=None, deadline=None, min_conf=None):
        self.verify_calls += 1
        self.verify_seen = [str(q.qid) for q in questions if q.answered]
        return {
            "checked": len(self.verify_seen),
            "changed": 0,
            "agreed": len(self.verify_seen),
            "failed": 0,
        }

    def refine_low_confidence(self, questions, progress=None, deadline=None, force=False):
        self.search_rounds += 1
        self.force_flags.append(bool(force))
        cands = [
            q
            for q in questions
            if q.answered and not getattr(q, "search_checked", False)
        ]
        if self.mark_checked:
            for q in cands:
                q.search_checked = True
                q.verified = True
        self.search_stats["searched"] += len(cands)
        return self.search_stats


class StagedPipelineTests(RunnerTestBase):
    def setUp(self):
        super().setUp()
        self._orig_factory = self._solver_factory
        self.staged: list[StagedSolver] = []
        self._mark_checked = True

        def factory(cfg):
            s = StagedSolver(cfg)
            s.mark_checked = self._mark_checked
            self.staged.append(s)
            self.solvers.append(s)
            return s

        self._solver_factory = factory

    def _staged_cfg(self, rounds=3, manual=True):
        cfg = _cfg(dry_run=False)
        cfg.bank.enabled = False        # 不依赖真实题库文件
        cfg.bank.mode = "staged"
        cfg.exam.manual_review = manual
        cfg.llm.search_rounds = rounds
        cfg.llm.verify_bank_answers = True
        return cfg

    def _seed_all_answered(self):
        self._seed_state(
            answers={
                "101": {"answer": ["A"], "text": "", "confidence": 0.95, "reason": "题库"},
                "102": {"answer": ["B"], "text": "", "confidence": 0.95, "reason": "题库"},
            }
        )

    def test_verify_and_search_run_when_all_questions_answered(self):
        """关键回归：题库全命中时，搜索不能因为「没有未答题」被跳过。"""
        self._seed_all_answered()
        code = self._run(self._staged_cfg(rounds=3))
        self.assertEqual(code, 0)
        self.assertTrue(self.staged, "staged 模式必须创建 solver")
        s = self.staged[0]
        self.assertEqual(s.verify_calls, 1)
        self.assertGreaterEqual(s.search_rounds, 1)
        self.assertTrue(all(s.force_flags), "staged 搜索必须用 force 模式")

    def test_search_stops_after_configured_rounds(self):
        """每轮都没核实成功时，不能无限重搜：严格按 search_rounds 截断。"""
        self._seed_all_answered()
        self._mark_checked = False      # 模拟永远搜不出可用结论
        self._run(self._staged_cfg(rounds=3))
        self.assertEqual(self.staged[0].search_rounds, 3)

    def test_search_stops_early_when_everything_verified(self):
        self._seed_all_answered()
        self._mark_checked = True       # 一轮就全部核实
        self._run(self._staged_cfg(rounds=3))
        self.assertEqual(self.staged[0].search_rounds, 1)

    def test_manual_review_blocks_submit(self):
        self._seed_all_answered()
        code = self._run(self._staged_cfg(rounds=1, manual=True))
        self.assertEqual(code, 0)
        self.assertEqual(self.api.submit_bodies, [])
        st = self.store.load()
        assert st is not None
        self.assertEqual(st.phase, "solved")

    def test_resume_submit_skips_verify_and_search(self):
        """人工核验后点「正式交卷」：直接按答题卡提交，不重跑模型。"""
        self._seed_state(
            phase="solved",
            answers={
                "101": {"answer": ["A"], "text": "", "confidence": 0.9, "reason": "题库"},
                "102": {"answer": ["B"], "text": "", "confidence": 0.9, "reason": "题库"},
            },
        )
        cfg = self._staged_cfg(rounds=3, manual=False)
        cfg.exam.auto_submit = True
        code = self._run(cfg)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.api.submit_bodies), 1)
        for s in self.staged:
            self.assertEqual(s.verify_calls, 0)
            self.assertEqual(s.search_rounds, 0)

    def test_auto_submit_still_works_without_manual_review(self):
        self._seed_all_answered()
        cfg = self._staged_cfg(rounds=1, manual=False)
        cfg.exam.auto_submit = True
        code = self._run(cfg)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.api.submit_bodies), 1)


class VerifyAnswersTests(unittest.TestCase):
    """LLM 二次校验的采纳规则（不经过 runner，直接测 solver 方法）。"""

    def _solver(self, min_conf=0.85):
        from src.llm import LLMSolver

        cfg = _cfg()
        cfg.llm.verify_min_confidence = min_conf
        cfg.llm.batch_size = 2
        s = LLMSolver.__new__(LLMSolver)   # 跳过网络客户端初始化
        s.cfg = cfg
        s.offline = False
        s.client = object()
        return s

    def _question(self, qid="101", keys=("A",)):
        from src.parser import Option, Question

        return Question(
            qid=qid,
            section_id=1,
            index=1,
            stem="1+1=?",
            qtype="single",
            options=[Option("A", "1"), Option("B", "2")],
            answer_keys=list(keys),
            confidence=0.95,
            reason="[本地题库]",
        )

    def test_high_confidence_disagreement_overrides(self):
        s = self._solver(min_conf=0.85)
        q = self._question()
        s.solve_batch = lambda batch: {
            "101": {"answer": ["B"], "text": "", "confidence": 0.95, "reason": "独立作答"}
        }
        st = s.verify_answers([q])
        self.assertEqual(st["changed"], 1)
        self.assertEqual(q.answer_keys, ["B"])
        self.assertTrue(q.verified)

    def test_low_confidence_disagreement_keeps_bank_answer(self):
        s = self._solver(min_conf=0.85)
        q = self._question()
        s.solve_batch = lambda batch: {
            "101": {"answer": ["B"], "text": "", "confidence": 0.6, "reason": "拿不准"}
        }
        st = s.verify_answers([q])
        self.assertEqual(st["changed"], 0)
        self.assertEqual(q.answer_keys, ["A"])

    def test_agreement_marks_verified(self):
        s = self._solver()
        q = self._question()
        q.verified = False
        s.solve_batch = lambda batch: {
            "101": {"answer": ["A"], "text": "", "confidence": 0.9, "reason": "一致"}
        }
        st = s.verify_answers([q])
        self.assertEqual(st["agreed"], 1)
        self.assertTrue(q.verified)
        self.assertIn("模型复核一致", q.reason)

    def test_edited_answers_are_never_touched(self):
        s = self._solver()
        q = self._question()
        q.edited = True
        s.solve_batch = lambda batch: {
            "101": {"answer": ["B"], "text": "", "confidence": 0.99, "reason": "改判"}
        }
        st = s.verify_answers([q])
        self.assertEqual(st["checked"], 0)
        self.assertEqual(q.answer_keys, ["A"])


if __name__ == "__main__":
    unittest.main()
