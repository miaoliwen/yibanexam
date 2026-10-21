# -*- coding: utf-8 -*-
"""runner 主流程测试：断点续跑、开考/交卷幂等、单实例锁、开考前拦截。

全部使用 fake api / fake solver / 临时目录，不联网、不写真实 data/。
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from src import runner
from src.config import AccountPaths, Config
from src.errors import TransientError
from src.llm import LLMSolver
from src.state import ExamState, RunLock, StateStore

NOW = time.time()
STARTED = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW - 3600))
EXPIRED = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW + 7200))

PAPER = {
    "result": True,
    "data": {
        "sections": [
            {
                "sectionId": 7,
                "subjects": [
                    {
                        "subjectId": 101,
                        "content": "1+1 等于几？",
                        "type": 1,
                        "options": [{"content": "1"}, {"content": "2"}],
                    },
                    {
                        "subjectId": 102,
                        "content": "2+2 等于几？",
                        "type": 1,
                        "options": [{"content": "3"}, {"content": "4"}],
                    },
                ],
            }
        ]
    },
}


class FakeApi:
    def __init__(self, paper=None, exam_id=603733):
        self.paper = paper or PAPER
        self.exam_id = exam_id
        self.calls: list = []
        self.start_attempts = 0
        self.start_timeout = False
        self.submitted = False
        self.submit_bodies: list[dict] = []
        self.submit_fail: Exception | None = None
        self.examuser_id = "EU1"
        self.list_calls = 0
        self.uid_after: int | None = None
        # 可覆盖的考试窗口，供 auto_start 用例把开考时间推到未来
        self.started = STARTED
        self.expired = EXPIRED

    def _exam(self) -> dict:
        item = {
            "examId": self.exam_id,
            "examName": "单元测试考试",
            "started": self.started,
            "expired": self.expired,
            "duration": 60,
            "examuserId": 0,
        }
        if self.start_attempts or (
            self.uid_after is not None and self.list_calls >= self.uid_after
        ):
            item["examuserId"] = self.examuser_id
        return item

    def user_check(self):
        self.calls.append("user_check")
        return {"result": True}

    def exam_list(self):
        self.calls.append("exam_list")
        self.list_calls += 1
        return {"result": True, "data": [self._exam()]}

    def exam_detail(self, exam_id):
        self.calls.append("detail")
        return {"result": True, "data": {}}

    def exam_setting(self, exam_id):
        self.calls.append("setting")
        return {"result": True, "data": {}}

    def start_exam(self, exam_id, user_id=None):
        self.start_attempts += 1
        self.calls.append("start")
        if self.start_timeout:
            raise TransientError("模拟开考超时")
        return {"result": True, "data": {"examuserId": self.examuser_id}}

    def get_paper(self, examuser_id):
        self.calls.append(("paper", str(examuser_id)))
        return self.paper

    def get_saved_answers(self, examuser_id):
        self.calls.append("answer_get")
        return {"result": True, "data": []}

    def get_result(self, exam_id):
        self.calls.append("result")
        if self.submitted:
            return {
                "result": True,
                "data": {"score": 88, "submitTime": "2026-10-19 01:00:00"},
            }
        return {"result": True, "data": {}}

    def submit_exam(self, body):
        self.calls.append("submit")
        self.submit_bodies.append(body)
        if self.submit_fail is not None:
            raise self.submit_fail
        self.submitted = True
        return {"result": True}


class FakeSolver:
    def __init__(self, cfg):
        self.cfg = cfg
        self.usage = {"prompt": 0, "completion": 0, "calls": 0}
        self.searcher = None
        self._search_off = True
        self.offline = False
        self.events: list[str] = []

    def preflight(self):
        self.events.append("preflight")

    def solve_batch(self, batch):
        self.events.append("solve")
        out = {}
        for q in batch:
            if q.options:
                out[str(q.qid)] = {
                    "answer": [q.options[0].key],
                    "text": "",
                    "confidence": 0.9,
                    "reason": "mock",
                }
            else:
                out[str(q.qid)] = {
                    "answer": [],
                    "text": "mock",
                    "confidence": 0.5,
                    "reason": "",
                }
        return out

    def apply(self, batch, res):
        LLMSolver.apply(batch, res)


_TEST_ROOT = Path(tempfile.mkdtemp(prefix="yooc-runner-"))


def _test_paths() -> AccountPaths:
    """测试专用账号路径：确保 run() 不会写真实 data/ 目录。"""
    r = _TEST_ROOT
    return AccountPaths(
        key="test",
        root=r,
        profile_dir=r / "browser_profile",
        session=r / "session.json",
        guard=r / "login_guard.json",
        state=r / "exam_state.json",
        lock=r / "run.lock",
        log=r / "run.log",
    )


def _cfg(dry_run=True, api_key="sk-test", reuse=False):
    cfg = Config()
    cfg.paths = _test_paths()
    cfg.account.group_id = 12345678
    cfg.account.browser_for_writes = "never"
    cfg.account.reuse_session = reuse
    cfg.account.login_mode = "password"
    cfg.account.username = "u"
    cfg.account.password = "p"
    cfg.exam.dry_run = dry_run
    cfg.exam.auto_submit = True
    cfg.exam.submit_review_seconds = 0
    cfg.exam.heartbeat_questions = 5
    cfg.exam.allow_start_in_dry_run = bool(dry_run)
    cfg.humanize.enabled = False
    cfg.llm.api_key = api_key
    cfg.llm.batch_size = 2
    return cfg


def _login(cfg, username=None, password=None, guard=None):
    return {"user_id": "226", "user_token": "tok", "yiban_id": "y1"}


class RunnerTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.state_path = self.tmp / "exam_state.json"
        self.lock_path = self.tmp / "run.lock"
        self.store = StateStore(self.state_path)
        self.api = FakeApi()
        self.solvers: list[FakeSolver] = []

        self._patches = [
            mock.patch.multiple(
                runner,
                DATA=self.tmp,
                STATE_PATH=self.state_path,
                LOCK_PATH=self.lock_path,
                RUN_LOG=self.tmp / "run.log",
            ),
            mock.patch("time.sleep", lambda *a, **k: None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    # ------------------------------------------------------------------
    def _solver_factory(self, cfg):
        s = FakeSolver(cfg)
        self.solvers.append(s)
        return s

    def _run(self, cfg, **kw):
        clock = kw.pop("clock", time.time)
        sleep_fn = kw.pop("sleep_fn", lambda s: None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = runner.run(
                cfg,
                session_factory=lambda c: self.fail("不应启动浏览器"),
                login_fn=kw.pop("login_fn", _login),
                validate_fn=lambda c, cr: False,
                solver_factory=self._solver_factory,
                api_factory=lambda c, creds, **kwargs: self.api,
                store=StateStore(self.state_path),
                lock=RunLock(self.lock_path),
                clock=clock,
                sleep_fn=sleep_fn,
                **kw,
            )
        self.output = out.getvalue()
        return code

    def _seed_state(self, **kw) -> ExamState:
        st = ExamState(
            group_id=12345678,
            exam_id="603733",
            exam_name="单元测试考试",
            phase="started",
            examuser_id="EU9",
            started_at=time.time() - 60,
        )
        for k, v in kw.items():
            setattr(st, k, v)
        self.store.save(st)
        return st


class DryRunTests(RunnerTestBase):
    def test_start_once_solve_and_no_submit(self):
        code = self._run(_cfg(dry_run=True))
        self.assertEqual(code, 0)
        self.assertEqual(self.api.start_attempts, 1)
        self.assertEqual(self.api.submit_bodies, [])
        st = self.store.load()
        assert st is not None
        self.assertEqual(st.examuser_id, "EU1")
        self.assertEqual(st.phase, "solved")
        self.assertEqual(st.answer_count(), 2)
        self.assertEqual(len(self.solvers), 1)
        self.assertEqual(self.solvers[0].events.count("solve"), 1)  # batch_size=2 → 一批

    def test_resume_uses_saved_examuser_without_start(self):
        self._seed_state(
            answers={
                "101": {"answer": ["A"], "text": "", "confidence": 0.9, "reason": "old"},
                "102": {"answer": ["B"], "text": "", "confidence": 0.9, "reason": "old"},
            }
        )
        code = self._run(_cfg(dry_run=True))
        self.assertEqual(code, 0)
        self.assertEqual(self.api.start_attempts, 0)
        self.assertNotIn("start", self.api.calls)
        self.assertIn(("paper", "EU9"), self.api.calls)
        # 全部答案已恢复，不需要再调用模型
        self.assertEqual(self.solvers, [])
        st = self.store.load()
        assert st is not None
        self.assertEqual(st.phase, "solved")

    def test_already_submitted_state_returns_without_start(self):
        self._seed_state(phase="submitted", submitted=True, submit_confirmed=True)
        code = self._run(_cfg(dry_run=True))
        self.assertEqual(code, 0)
        self.assertEqual(self.api.start_attempts, 0)
        self.assertEqual(self.api.submit_bodies, [])
        self.assertNotIn(("paper", "EU9"), self.api.calls)

    def test_resume_disabled_refuses_to_restart(self):
        self._seed_state()
        cfg = _cfg(dry_run=True)
        cfg.exam.resume = False
        code = self._run(cfg)
        self.assertEqual(code, runner.EXIT_SESSION)
        self.assertEqual(self.api.start_attempts, 0)

    def test_start_timeout_recovers_without_second_start(self):
        self.api.start_timeout = True
        # 模拟服务端其实已经创建了记录：start 抛异常后 exam_list 能查到
        code = self._run(_cfg(dry_run=True))
        self.assertEqual(code, 0)
        self.assertEqual(self.api.start_attempts, 1)
        st = self.store.load()
        assert st is not None
        self.assertEqual(st.examuser_id, "EU1")

    def test_second_instance_refused(self):
        held = RunLock(self.lock_path)
        self.assertTrue(held.acquire())
        try:
            code = self._run(_cfg(dry_run=True))
        finally:
            held.release()
        self.assertEqual(code, runner.EXIT_SESSION)
        self.assertEqual(self.api.calls, [])

    def test_no_api_key_live_stops_before_start(self):
        cfg = _cfg(dry_run=False, api_key="")
        code = self._run(cfg)
        self.assertEqual(code, runner.EXIT_CONFIG)
        self.assertEqual(self.api.start_attempts, 0)

    def test_dry_run_without_attempt_is_read_only(self):
        cfg = _cfg(dry_run=True)
        cfg.exam.allow_start_in_dry_run = False
        code = self._run(cfg)
        self.assertEqual(code, 0)
        self.assertEqual(self.api.start_attempts, 0)
        self.assertNotIn("start", self.api.calls)
        self.assertFalse(any(isinstance(c, tuple) and c[0] == "paper" for c in self.api.calls))
        self.assertEqual(self.api.submit_bodies, [])
        self.assertIn("演练安全模式", self.output)

    def test_paper_answer_key_skips_llm(self):
        import json as _json

        from src.crypto import encrypt_text

        self.api.paper = {
            "result": True,
            "data": {
                "sections": [
                    {
                        "sectionId": 7,
                        "subjects": [
                            {
                                "subjectId": 101,
                                "title": ["1+1 等于几？"],
                                "type": "choice",
                                "points": 2,
                                "option": [{"content": "1"}, {"content": "2"}],
                                "answer": encrypt_text(_json.dumps([1]), "y1"),
                            },
                            {
                                "subjectId": 102,
                                "title": ["2+2 等于几？"],
                                "type": "choice",
                                "points": 2,
                                "option": [{"content": "3"}, {"content": "4"}],
                                "answer": encrypt_text(_json.dumps([0]), "y1"),
                            },
                        ],
                    }
                ]
            },
        }
        code = self._run(_cfg(dry_run=True))
        self.assertEqual(code, 0)
        # 全部题都有题库答案 → 不需要调用模型
        self.assertEqual(self.solvers, [])
        st = self.store.load()
        assert st is not None
        self.assertEqual(st.answers["101"]["answer"], ["B"])
        self.assertEqual(st.answers["102"]["answer"], ["A"])

    def test_unparseable_window_stops(self):
        cfg = _cfg(dry_run=True)
        self.api.exam = None  # 不影响 _exam()，下面直接换考试条目
        orig = self.api._exam

        def bad_exam():
            e = orig()
            e["started"] = "not-a-date"
            return e

        self.api._exam = bad_exam
        code = self._run(cfg)
        self.assertEqual(code, runner.EXIT_WINDOW)
        self.assertEqual(self.api.start_attempts, 0)

    def test_starting_state_delayed_server_record_is_adopted(self):
        # 上次 start 未知；服务端第 3 次列表查询才出现 examuserId → 必须复用，不能重开
        self.api.uid_after = 3
        self._seed_state(phase="starting", examuser_id="", started_at=time.time() - 10)
        code = self._run(_cfg(dry_run=True))
        self.assertEqual(code, 0)
        self.assertEqual(self.api.start_attempts, 0)
        st = self.store.load()
        assert st is not None
        self.assertEqual(st.examuser_id, "EU1")

    def test_starting_state_without_record_does_not_restart(self):
        self.api.uid_after = 999
        self._seed_state(phase="starting", examuser_id="", started_at=time.time() - 10)
        code = self._run(_cfg(dry_run=True))
        self.assertEqual(code, runner.EXIT_SESSION)
        self.assertEqual(self.api.start_attempts, 0)

    def test_deadline_passed_resume_refuses_to_submit_blank(self):
        self._seed_state(
            phase="solving",
            started_at=time.time() - 7200,
            answers={
                "101": {"answer": ["A"], "text": "", "confidence": 0.9, "reason": ""},
                "102": {"answer": ["A"], "text": "", "confidence": 0.9, "reason": ""},
            },
        )
        code = self._run(_cfg(dry_run=True))
        self.assertEqual(code, runner.EXIT_WINDOW)
        self.assertEqual(self.api.submit_bodies, [])
        self.assertEqual(self.api.start_attempts, 0)


class BankModeTests(RunnerTestBase):
    """题库两种模式：prefer 省模型调用，llm_first 先模型判卷再题库校正。"""

    def _bank_path(self) -> Path:
        p = self.tmp / "bank.json"
        items = [
            {
                "id": "b1",
                "no": 1,
                "qtype": "single",
                "stem": "1+1 等于几？",
                "options": {"A": "1", "B": "2"},
                "answer": ["B"],
                "answer_texts": ["2"],
            },
            {
                "id": "b2",
                "no": 2,
                "qtype": "single",
                "stem": "2+2 等于几？",
                "options": {"A": "3", "B": "4"},
                "answer": ["B"],
                "answer_texts": ["4"],
            },
        ]
        p.write_text(
            json.dumps({"name": "单测题库", "items": items}, ensure_ascii=False),
            encoding="utf-8",
        )
        return p

    def test_prefer_answers_from_bank_without_model(self):
        cfg = _cfg(dry_run=True)
        cfg.bank.path = self._bank_path()
        cfg.bank.mode = "prefer"
        code = self._run(cfg)
        self.assertEqual(code, 0)
        # 题库全覆盖 → 不创建模型
        self.assertEqual(self.solvers, [])
        st = self.store.load()
        assert st is not None
        self.assertEqual(
            [st.answers[str(i)]["answer"] for i in (101, 102)], [["B"], ["B"]]
        )

    def test_llm_first_runs_model_then_bank_corrects(self):
        cfg = _cfg(dry_run=True)
        cfg.bank.path = self._bank_path()
        cfg.bank.mode = "llm_first"
        code = self._run(cfg)
        self.assertEqual(code, 0)
        # 模型真的判了整卷（FakeSolver 全选 A）
        self.assertEqual(len(self.solvers), 1)
        self.assertIn("solve", self.solvers[0].events)
        st = self.store.load()
        assert st is not None
        # 最后题库把答案校正回 B
        self.assertEqual(
            [st.answers[str(i)]["answer"] for i in (101, 102)], [["B"], ["B"]]
        )


class LiveSubmitTests(RunnerTestBase):
    def test_live_preflight_then_single_submit(self):
        cfg = _cfg(dry_run=False, api_key="sk-test")
        code = self._run(cfg)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.solvers), 1)
        self.assertEqual(self.solvers[0].events[0], "preflight")
        self.assertIn("solve", self.solvers[0].events)
        self.assertEqual(len(self.api.submit_bodies), 1)
        body = self.api.submit_bodies[0]
        self.assertEqual(body["examuserId"], "EU1")
        self.assertIsInstance(body["answer"], str)
        parsed = json.loads(body["answer"])
        self.assertEqual(set(parsed), {"101", "102"})
        self.assertEqual(parsed["101"][0], {"1": ["0"]})
        st = self.store.load()
        assert st is not None
        self.assertTrue(st.submitted)
        self.assertEqual(st.phase, "done")

    def test_submit_is_skipped_when_server_already_submitted(self):
        # 本地有答案、服务端已有交卷标记：只允许查成绩，绝不重复交卷
        self.api.submitted = True
        self._seed_state(
            phase="solved",
            answers={
                "101": {"answer": ["A"], "text": "", "confidence": 0.9, "reason": "old"},
                "102": {"answer": ["B"], "text": "", "confidence": 0.9, "reason": "old"},
            },
        )
        cfg = _cfg(dry_run=False, api_key="sk-test")
        code = self._run(cfg)
        self.assertEqual(code, 0)
        self.assertEqual(self.api.submit_bodies, [])
        self.assertEqual(self.api.start_attempts, 0)


    def test_submit_transient_is_not_auto_resent(self):
        self.api.submit_fail = TransientError("模拟交卷超时")
        cfg = _cfg(dry_run=False, api_key="sk-test")
        code = self._run(cfg)
        self.assertEqual(code, runner.EXIT_SUBMIT)
        self.assertEqual(len(self.api.submit_bodies), 1)
        st = self.store.load()
        assert st is not None
        self.assertFalse(st.submitted)
        self.assertEqual(st.phase, "submitting")

    def test_synthetic_guesses_are_not_reused_for_live_submit(self):
        self._seed_state(
            phase="solved",
            answers={
                "101": {
                    "answer": ["B"],
                    "text": "",
                    "confidence": 0.01,
                    "reason": "[兜底猜测]",
                    "synthetic": True,
                },
                "102": {
                    "answer": ["B"],
                    "text": "",
                    "confidence": 0.01,
                    "reason": "[兜底猜测]",
                    "synthetic": True,
                },
            },
        )
        cfg = _cfg(dry_run=False, api_key="sk-test")
        code = self._run(cfg)
        self.assertEqual(code, 0)
        # 兜底猜测被忽略 → 重新用模型答题（FakeSolver 返回每题第一个选项 A）
        self.assertEqual(len(self.solvers), 1)
        self.assertIn("solve", self.solvers[0].events)
        parsed = json.loads(self.api.submit_bodies[0]["answer"])
        self.assertEqual(parsed["101"][0], {"1": ["0"]})
        self.assertEqual(parsed["102"][0], {"1": ["0"]})


class ConfirmSubmittedTests(unittest.TestCase):
    def test_other_examuserid_alone_is_not_confirmation(self):
        """服务端出现另一条 examuserId 不等于本次已交卷。"""
        api = FakeApi()
        api.examuser_id = "OTHER-RECORD"
        api.start_attempts = 1  # 列表里始终返回另一条记录
        st = ExamState(exam_id="603733", examuser_id="EU1", phase="submitting")
        with mock.patch("time.sleep", lambda *a, **k: None):
            self.assertFalse(runner._confirm_submitted(api, 603733, st))
        self.assertFalse(st.submit_confirmed)


class StartOnlyTests(RunnerTestBase):
    """开始考试：真的开考 + 答题，但停在交卷前。"""

    def test_start_only_solves_without_submit(self):
        cfg = _cfg(dry_run=False)
        cfg.exam.auto_submit = False
        code = self._run(cfg)
        self.assertEqual(code, 0)
        self.assertEqual(self.api.start_attempts, 1)
        self.assertEqual(self.api.submit_bodies, [])
        st = self.store.load()
        assert st is not None
        self.assertEqual(st.phase, "solved")
        self.assertEqual(st.answer_count(), 2)

    def test_live_after_start_only_reuses_record(self):
        cfg = _cfg(dry_run=False)
        cfg.exam.auto_submit = False
        self.assertEqual(self._run(cfg), 0)

        cfg2 = _cfg(dry_run=False)
        cfg2.exam.auto_submit = True
        self.assertEqual(self._run(cfg2), 0)
        self.assertEqual(self.api.start_attempts, 1)      # 没有重复开考
        self.assertEqual(len(self.api.submit_bodies), 1)  # 只在第二次交卷


class EventTests(RunnerTestBase):
    """runner 的阶段事件：GUI 状态条靠它显示搜索进度。"""

    def test_emits_solve_and_search_events(self):
        events: list[tuple[str, dict]] = []
        code = self._run(
            _cfg(dry_run=True), on_event=lambda k, d: events.append((k, dict(d)))
        )
        self.assertEqual(code, 0)
        kinds = [k for k, _ in events]
        self.assertIn("solve", kinds)
        self.assertIn("search", kinds)
        search_ev = [d for k, d in events if k == "search"]
        self.assertTrue(any(d.get("phase") == "unavailable" for d in search_ev))

    def test_event_callback_exception_does_not_break_run(self):
        def boom(kind, data):
            raise RuntimeError("前端挂了")

        code = self._run(_cfg(dry_run=True), on_event=boom)
        self.assertEqual(code, 0)


class AutoStartTests(RunnerTestBase):
    """auto_start：开考时间没到时等待，到点自动开考。"""

    def _virtual_clock(self):
        """可推进的虚拟时钟：sleep_fn 推进时间，避免用例真的睡几小时。"""
        now = [time.time()]
        return now, (lambda: now[0]), (lambda s: now.__setitem__(0, now[0] + s))

    def _future(self, seconds: float) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + seconds))

    def test_too_early_stops_without_auto_start(self):
        self.api.started = self._future(600)
        code = self._run(_cfg(dry_run=True))
        self.assertEqual(code, runner.EXIT_WINDOW)
        self.assertEqual(self.api.start_attempts, 0)
        self.assertIn("还没到开考时间", self.output)

    def test_auto_start_waits_then_starts(self):
        future = self._future(600)
        self.api.started = future
        cfg = _cfg(dry_run=True)
        cfg.exam.auto_start = True
        cfg.exam.auto_start_poll_seconds = 60
        now, clock, sleep = self._virtual_clock()
        code = self._run(cfg, clock=clock, sleep_fn=sleep)
        self.assertEqual(code, 0)
        self.assertEqual(self.api.start_attempts, 1)
        # 时钟确实被推到了开考时刻之后
        self.assertGreaterEqual(now[0], runner._parse_time(future))
        self.assertIn("自动开考", self.output)

    def test_auto_start_refuses_when_wait_exceeds_cap(self):
        self.api.started = self._future(3600 * 3)
        cfg = _cfg(dry_run=True)
        cfg.exam.auto_start = True
        cfg.exam.auto_start_max_wait_minutes = 60
        now, clock, sleep = self._virtual_clock()
        code = self._run(cfg, clock=clock, sleep_fn=sleep)
        self.assertEqual(code, runner.EXIT_WINDOW)
        self.assertEqual(self.api.start_attempts, 0)
        self.assertEqual(now[0], now[0])  # 没推进（没有空转等待）

    def test_auto_start_with_past_start_runs_immediately(self):
        cfg = _cfg(dry_run=True)
        cfg.exam.auto_start = True
        now, clock, sleep = self._virtual_clock()
        code = self._run(cfg, clock=clock, sleep_fn=sleep)
        self.assertEqual(code, 0)
        self.assertEqual(self.api.start_attempts, 1)

    def test_start_at_explicit_time_is_used(self):
        # 考试窗口已经开了，但 start_at 指到未来 → 仍要等
        cfg = _cfg(dry_run=True)
        cfg.exam.auto_start = True
        cfg.exam.start_at = self._future(120)
        cfg.exam.auto_start_poll_seconds = 30
        now, clock, sleep = self._virtual_clock()
        code = self._run(cfg, clock=clock, sleep_fn=sleep)
        self.assertEqual(code, 0)
        self.assertEqual(self.api.start_attempts, 1)
        self.assertGreaterEqual(now[0] - time.time(), 110)

    def test_start_at_garbage_raises_config_error(self):
        cfg = _cfg(dry_run=True)
        cfg.exam.auto_start = True
        cfg.exam.start_at = "tomorrow morning"
        now, clock, sleep = self._virtual_clock()
        code = self._run(cfg, clock=clock, sleep_fn=sleep)
        self.assertEqual(code, runner.EXIT_CONFIG)
        self.assertEqual(self.api.start_attempts, 0)
        self.assertIn("start_at", self.output)


if __name__ == "__main__":
    unittest.main()
