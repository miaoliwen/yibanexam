# -*- coding: utf-8 -*-
"""配置 / 状态 / 请求头 / 节奏器 的基础测试。"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

import yaml

from src import config as cfgmod
from src import headers
from src.config import Config, HumanizeCfg
from src.errors import ConfigError
from src import humanize
from src.state import ExamState, RunLock, StateStore


def _write_config(tmp: Path, data: dict) -> Path:
    p = tmp / "config.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return p


def _base(group_id=12345678):
    return {
        "account": {
            "group_id": group_id,
            "profile_dir": str(Path(tempfile.gettempdir()) / "yooc-test-profile"),
            "username": "u",
            "password": "p",
        },
        "exam": {"dry_run": True},
        "llm": {"api_key": "sk-test"},
        "humanize": {"enabled": False},
    }


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_defaults_and_types(self):
        cfg = cfgmod.load(_write_config(self.tmp, _base()))
        self.assertEqual(cfg.account.group_id, 12345678)
        self.assertTrue(cfg.exam.dry_run)
        self.assertTrue(cfg.account.reuse_session)
        self.assertEqual(cfg.account.browser_for_writes, "auto")
        self.assertEqual(cfg.exam.answer_style, "letters")
        self.assertEqual(cfg.exam.fill_answer_style, "array")
        self.assertFalse(cfg.exam.allow_start_in_dry_run)
        self.assertFalse(cfg.exam.auto_select_first)
        self.assertEqual(cfg.account.login_transport, "auto")
        self.assertEqual(cfg.exam.deadline_margin_seconds, 120)
        self.assertEqual(cfg.exam.heartbeat_questions, 0)
        self.assertTrue(cfg.exam.guess_unanswered)
        self.assertTrue(cfg.llm.preflight)
        self.assertEqual(cfg.http.retries, 2)
        self.assertEqual(cfg.humanize.heartbeat_seconds, 0)

    def test_missing_group_id_raises_config_error_not_system_exit(self):
        data = _base(group_id=0)
        with self.assertRaises(ConfigError):
            cfgmod.load(_write_config(self.tmp, data))

    def test_invalid_login_mode(self):
        data = _base()
        data["account"]["login_mode"] = "magic"
        with self.assertRaises(ConfigError):
            cfgmod.load(_write_config(self.tmp, data))

    def test_invalid_login_transport(self):
        data = _base()
        data["account"]["login_transport"] = "magic"
        with self.assertRaises(ConfigError):
            cfgmod.load(_write_config(self.tmp, data))

    def test_invalid_answer_style(self):
        data = _base()
        data["exam"]["answer_style"] = "bogus"
        with self.assertRaises(ConfigError):
            cfgmod.load(_write_config(self.tmp, data))

    def test_invalid_yaml(self):
        p = self.tmp / "config.yaml"
        p.write_text("account: [unclosed", encoding="utf-8")
        with self.assertRaises(ConfigError):
            cfgmod.load(p)

    def test_clamps_out_of_range(self):
        data = _base()
        data["llm"]["concurrency"] = 99
        data["llm"]["batch_size"] = 0
        data["llm"]["timeout"] = 1
        data["humanize"]["per_question_seconds"] = -5
        cfg = cfgmod.load(_write_config(self.tmp, data))
        self.assertEqual(cfg.llm.concurrency, 8)
        self.assertEqual(cfg.llm.batch_size, 1)
        self.assertEqual(cfg.llm.timeout, 5)
        self.assertEqual(cfg.humanize.per_question_seconds, 0.0)

    def test_unknown_keys_ignored(self):
        data = _base()
        data["mystery"] = {"x": 1}
        data["account"]["future_flag"] = True
        cfg = cfgmod.load(_write_config(self.tmp, data))
        self.assertEqual(cfg.account.group_id, 12345678)

    def test_bad_section_type(self):
        data = _base()
        data["llm"] = "nope"
        with self.assertRaises(ConfigError):
            cfgmod.load(_write_config(self.tmp, data))


class StateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.path = self.tmp / "exam_state.json"
        self.store = StateStore(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_roundtrip_and_atomic(self):
        st = ExamState(exam_id="1", examuser_id="EU1", phase="started")
        st.answers["101"] = {"answer": ["A"], "text": "", "confidence": 0.9}
        self.store.save(st)
        loaded = self.store.load()
        assert loaded is not None
        self.assertEqual(loaded.examuser_id, "EU1")
        self.assertEqual(loaded.answers["101"]["answer"], ["A"])
        self.assertGreater(loaded.updated_at, 0)
        leftovers = list(self.tmp.glob("*.tmp.*"))
        self.assertEqual(leftovers, [])

    def test_corrupt_file_quarantined(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.store.load())
        self.assertFalse(self.path.exists())
        self.assertTrue(list(self.tmp.glob("*.corrupt.*")))

    def test_unknown_fields_ignored(self):
        self.path.write_text(json.dumps({"exam_id": 5, "nope": 1}), encoding="utf-8")
        st = self.store.load()
        assert st is not None
        self.assertEqual(st.exam_id, "5")

    def test_lock_exclusive_and_release(self):
        a = RunLock(self.tmp / "run.lock")
        b = RunLock(self.tmp / "run.lock")
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire())
        a.release()
        self.assertTrue(b.acquire())
        b.release()

    def test_stale_lock_reclaimed(self):
        lock_path = self.tmp / "run.lock"
        lock_path.write_text(
            json.dumps({"pid": 2147483647, "started_at": time.time()}), encoding="utf-8"
        )
        lock = RunLock(lock_path)
        self.assertTrue(lock.acquire())
        lock.release()

    def test_corrupt_lock_treated_stale(self):
        lock_path = self.tmp / "run.lock"
        lock_path.write_text("garbage", encoding="utf-8")
        lock = RunLock(lock_path)
        self.assertTrue(lock.acquire())
        lock.release()


class HeaderTests(unittest.TestCase):
    def test_ua_major(self):
        ua = "Mozilla/5.0 Chrome/149.0.0.0 Safari/537.36"
        self.assertEqual(headers.ua_major(ua), "149")
        self.assertIn('"Google Chrome";v="149"', headers.sec_ch_ua(ua))

    def test_edge(self):
        ua = "Mozilla/5.0 Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0"
        self.assertTrue(headers.is_edge(ua))
        self.assertIn("Microsoft Edge", headers.sec_ch_ua(ua))

    def test_accept_encoding_has_gzip(self):
        enc = headers.accept_encoding()
        self.assertIn("gzip", enc)
        self.assertIn("deflate", enc)

    def test_ajax_headers_complete(self):
        h = headers.ajax_headers(
            "Mozilla/5.0 Chrome/149.0.0.0 Safari/537.36",
            origin="https://exam.yooc.me",
            referer="https://exam.yooc.me/group/1/exams",
            content_type="application/json",
            extra={"X-Test": "1"},
        )
        for key in (
            "sec-ch-ua",
            "sec-ch-ua-platform",
            "sec-fetch-site",
            "sec-fetch-mode",
            "sec-fetch-dest",
            "Accept-Encoding",
            "Origin",
            "Referer",
            "Content-Type",
        ):
            self.assertIn(key, h)
        self.assertEqual(h["X-Test"], "1")
        self.assertEqual(h["sec-fetch-mode"], "cors")


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class HumanizeTests(unittest.TestCase):
    def test_think_time_bounds(self):
        cfg = HumanizeCfg(enabled=True, min_delay_ms=1000, max_delay_ms=5000)
        for _ in range(200):
            ms = humanize.think_time_ms(cfg, difficulty=0.5)
            self.assertGreaterEqual(ms, 1000)
            self.assertLessEqual(ms, 5000)

    def test_pacer_respects_deadline(self):
        cfg = HumanizeCfg(enabled=True, per_question_seconds=10.0)
        clock = FakeClock(1000.0)
        pacer = humanize.Pacer(
            cfg,
            started_at=1000.0,
            total_questions=5,
            deadline_at=1000.0 + 60,
            reserve_seconds=30,
            clock=clock,
            sleep=clock.sleep,
        )
        total = 0.0
        for _ in range(5):
            total += pacer.wait_next()
        self.assertLessEqual(total, 30.0 + 1e-6)
        # 预算很紧时允许进入 fast 模式（不等待）；否则应逐题走完
        self.assertTrue(pacer.fast or pacer._idx == 5)
        # 计划按预算的 80%~97% 缩放，剩余裕量留作随机抖动
        self.assertGreaterEqual(pacer.budget_seconds(), 0.0)

    def test_submit_slack_is_not_constant(self):
        """交卷时刻不能恒等于「截止 - reserve」；不同随机种子应有明显分散。"""
        cfg = HumanizeCfg(enabled=True, per_question_seconds=34.0)
        slacks = []
        for seed in range(20):
            clock = FakeClock(1000.0)
            pacer = humanize.Pacer(
                cfg,
                started_at=1000.0,
                total_questions=50,
                deadline_at=1000.0 + 1200,
                reserve_seconds=120,
                clock=clock,
                sleep=clock.sleep,
                rng=__import__("random").Random(seed),
            )
            for _ in range(50):
                pacer.wait_next()
            slacks.append((1000.0 + 1200) - clock.t - 120)
        self.assertGreater(max(slacks) - min(slacks), 20.0)

    def test_pacer_fast_mode_when_budget_tiny(self):
        cfg = HumanizeCfg(enabled=True, per_question_seconds=10.0)
        clock = FakeClock(1000.0)
        pacer = humanize.Pacer(
            cfg,
            started_at=1000.0,
            total_questions=50,
            deadline_at=1000.0 + 60,
            reserve_seconds=30,
            clock=clock,
            sleep=clock.sleep,
        )
        # 预算 30s / 50 题 → 计划被压到 35% 以下，进入 fast 模式不再等待
        self.assertTrue(pacer.fast)
        self.assertEqual(sum(pacer.wait_next() for _ in range(50)), 0.0)

    def test_pacer_no_deadline_spends_expected_time(self):
        cfg = HumanizeCfg(enabled=True, per_question_seconds=10.0)
        clock = FakeClock(1000.0)
        pacer = humanize.Pacer(
            cfg,
            started_at=1000.0,
            total_questions=20,
            clock=clock,
            sleep=clock.sleep,
        )
        total = sum(pacer.wait_next() for _ in range(20))
        # 对数正态计划总和围绕 20*10 波动，允许较宽区间
        self.assertGreater(total, 60)
        self.assertLess(total, 400)

    def test_pacer_disabled(self):
        cfg = HumanizeCfg(enabled=False, per_question_seconds=30.0)
        clock = FakeClock()
        pacer = humanize.Pacer(
            cfg, started_at=clock(), total_questions=10, clock=clock, sleep=clock.sleep
        )
        self.assertEqual(pacer.wait_next(), 0.0)


if __name__ == "__main__":
    unittest.main()
