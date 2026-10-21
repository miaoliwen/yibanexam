# -*- coding: utf-8 -*-
"""试卷解析 + 接口客户端测试（全部离线，fake 传输层）。"""
from __future__ import annotations

import unittest
from typing import Any

import requests
from requests.cookies import RequestsCookieJar

from src.api import YoocApi
from src.config import Config
from src.errors import ApiError, AuthError, RiskControlError, TransientError
from src.parser import extract_examuser_id, parse_paper


class FakeResp:
    def __init__(self, status: int = 200, body: Any = None, text: str | None = None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else str(body)
        self.headers: dict[str, str] = {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeHTTP:
    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []
        self.headers: dict[str, str] = {}
        self.cookies = RequestsCookieJar()
        self.proxies: dict[str, str] = {}

    def get(self, url, timeout=None):
        self.calls.append(("GET", url))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def post(self, url, json=None, timeout=None, headers=None):
        self.calls.append(("POST", url))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakePageSession:
    def __init__(self, result=(200, {"result": True}), fail=None):
        self.calls: list[tuple] = []
        self.result = result
        self.fail = fail

    def page_fetch(self, path, method="GET", body=None):
        self.calls.append((path, method, body))
        if self.fail:
            raise self.fail
        return self.result


def _cfg(**kw) -> Config:
    cfg = Config()
    cfg.http.retries = 0
    cfg.http.backoff_seconds = 0
    cfg.account.group_id = 12345678
    for k, v in kw.items():
        setattr(cfg.account, k, v)
    return cfg


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
                        "options": [{"content": "A. 1"}, {"content": "B. 2"}],
                    },
                    {
                        "subjectId": 102,
                        "content": "判断：地球是圆的。",
                        "type": 3,
                        "options": [{"content": "正确"}, {"content": "错误"}],
                    },
                ],
            }
        ]
    },
}


class ParserTests(unittest.TestCase):
    def test_sections_parse(self):
        qs, sections = parse_paper(PAPER["data"])
        self.assertEqual(len(qs), 2)
        self.assertEqual(str(qs[0].qid), "101")
        self.assertEqual(qs[0].section_id, 7)
        self.assertEqual(qs[0].qtype, "single")
        self.assertEqual([o.key for o in qs[0].options], ["A", "B"])
        # "A. 1" 的前缀应被剥掉
        self.assertEqual(qs[0].options[0].text, "1")
        self.assertEqual(qs[1].qtype, "judge")

    def test_flat_list(self):
        qs, _ = parse_paper(
            [
                {
                    "subjectId": 1,
                    "content": "填空题：1+1=___",
                    "type": 4,
                }
            ]
        )
        self.assertEqual(len(qs), 1)
        self.assertEqual(qs[0].qtype, "fill")

    def test_nested_data(self):
        qs, _ = parse_paper({"data": PAPER["data"]})
        self.assertEqual(len(qs), 2)

    def test_missing_ids_fallback(self):
        qs, _ = parse_paper([{"content": "x"}, {"content": "y"}])
        self.assertEqual(len(qs), 2)
        self.assertNotEqual(qs[0].qid, qs[1].qid)

    def test_real_bundle_structure(self):
        paper = {
            "sections": [
                {
                    "sectionId": 1,
                    "sectionName": "一",
                    "subjects": [
                        {
                            "subjectId": 10,
                            "title": ["题干 {input} 和 {input}"],
                            "type": "completion",
                            "points": 2,
                            "inputs": "2",
                            "option": [],
                        },
                        {
                            "subjectId": 11,
                            "title": ["选择"],
                            "type": "multiplechoice",
                            "points": 2,
                            "option": [{"content": "A项"}, {"content": "B项"}],
                        },
                    ],
                }
            ]
        }
        qs, _ = parse_paper(paper)
        self.assertEqual([q.qtype for q in qs], ["fill", "multi"])
        self.assertEqual(qs[1].options[0].text, "A项")
        self.assertEqual(qs[1].score, 2)

    def test_judge_detected_when_platform_says_choice(self):
        """平台把判断题也标成 type=choice，必须靠选项特征认出来。"""
        paper = {
            "data": [
                {
                    "sectionId": 1,
                    "subjects": [
                        {
                            "subjectId": 1,
                            "type": "choice",
                            "title": ["学校坚持教育与宗教相分离原则。"],
                            "option": [["对"], ["错"]],
                            "answer": "x",
                        },
                        {
                            "subjectId": 2,
                            "type": "choice",
                            "title": ["这是单选题干。"],
                            "option": [["甲"], ["乙"], ["丙"], ["丁"]],
                            "answer": "x",
                        },
                    ],
                }
            ]
        }
        qs, _ = parse_paper(paper)
        self.assertEqual([q.qtype for q in qs], ["judge", "single"])

    def test_reject_placeholder_answer_key(self):
        """整卷 answer 密文相同 → 平台占位数据，不能当真答案用。"""
        import json as _json

        from src.crypto import encrypt_text
        from src.parser import apply_answer_key

        enc = encrypt_text(_json.dumps([1]), "y1")   # 同一明文 → 同一密文
        subjects = [
            {
                "subjectId": i,
                "type": "choice",
                "title": [f"题目 {i}"],
                "option": [["甲"], ["乙"], ["丙"], ["丁"]],
                "answer": enc,
            }
            for i in range(10)
        ]
        qs, _ = parse_paper({"data": [{"sectionId": 1, "subjects": subjects}]})
        report: list[str] = []
        self.assertEqual(apply_answer_key(qs, "y1", report=report), 0)
        self.assertEqual(qs[0].key_keys, [])
        self.assertFalse(any(q.has_key for q in qs))
        self.assertTrue(report and "占位" in report[0])

    def test_apply_answer_key_normal(self):
        """正常卷：每道题密文不同，应逐题解出答案。"""
        import json as _json

        from src.crypto import encrypt_text
        from src.parser import apply_answer_key

        subjects = [
            {
                "subjectId": i,
                "type": "choice",
                "title": [f"题目 {i}"],
                "option": [["甲"], ["乙"], ["丙"], ["丁"]],
                "answer": encrypt_text(_json.dumps([i % 4]), "y1"),
            }
            for i in range(10)
        ]
        qs, _ = parse_paper({"data": [{"sectionId": 1, "subjects": subjects}]})
        self.assertEqual(apply_answer_key(qs, "y1"), 10)
        self.assertEqual([q.key_keys[0] for q in qs], ["A", "B", "C", "D"] * 2 + ["A", "B"])

    def test_apply_answer_key_indices(self):
        import json as _json

        from src.crypto import encrypt_text
        from src.parser import apply_answer_key

        paper = {
            "sections": [
                {
                    "sectionId": 1,
                    "subjects": [
                        {
                            "subjectId": 10,
                            "title": ["t"],
                            "type": "choice",
                            "points": 2,
                            "option": [{"content": "甲"}, {"content": "乙"}],
                            "answer": encrypt_text(_json.dumps([1]), "y1"),
                        }
                    ],
                }
            ]
        }
        qs, _ = parse_paper(paper)
        self.assertEqual(apply_answer_key(qs, "y1"), 1)
        self.assertEqual(qs[0].key_keys, ["B"])

    def test_extract_examuser_id(self):
        self.assertEqual(extract_examuser_id({"data": {"examuserId": 9}}), 9)
        self.assertEqual(extract_examuser_id({"examuserId": "9"}), "9")
        self.assertEqual(extract_examuser_id({"data": {"data": {"id": 5}}}), 5)
        self.assertIsNone(extract_examuser_id({"data": {}}))
        # 0 / "0" 是未开考占位，不能当有效作答记录
        self.assertIsNone(extract_examuser_id({"data": {"examuserId": 0}}))
        self.assertIsNone(extract_examuser_id({"data": {"examuserId": "0"}}))
        self.assertIsNone(extract_examuser_id(None))


class ApiTests(unittest.TestCase):
    def _api(self, responses=None, session=None, cfg=None):
        cfg = cfg or _cfg()
        api = YoocApi(
            cfg,
            {"user_id": "226", "user_token": "tok", "yiban_id": "y1"},
            session=session,
        )
        if responses is not None:
            api.s = FakeHTTP(responses)
        return api

    def test_get_retries_on_transient_then_succeeds(self):
        cfg = _cfg()
        cfg.http.retries = 2
        api = self._api(
            [requests.exceptions.ConnectionError("boom"), FakeResp(200, {"result": True})],
            cfg=cfg,
        )
        out = api.get("api/exam/user/check", {"groupId": 1})
        self.assertTrue(out["result"])
        self.assertEqual(len(api.s.calls), 2)

    def test_403_raises_auth_error(self):
        api = self._api([FakeResp(403, {"result": False})])
        with self.assertRaises(AuthError):
            api.get("api/exam/user/check")

    def test_result_false_raises_api_error_with_message(self):
        api = self._api([FakeResp(200, {"result": False, "message": "考试次数已用完"})])
        with self.assertRaises(ApiError) as cm:
            api.get("api/exam/user/check")
        self.assertIn("次数", str(cm.exception))

    def test_risk_message_raises_risk_control(self):
        api = self._api([FakeResp(200, {"result": False, "message": "操作过于频繁，请稍后再试"})])
        with self.assertRaises(RiskControlError):
            api.get("api/exam/user/check")

    def test_429_raises_risk_control(self):
        api = self._api([FakeResp(429, {"result": False})])
        with self.assertRaises(RiskControlError):
            api.get("api/exam/user/check")

    def test_write_goes_through_page_fetch(self):
        sess = FakePageSession((200, {"result": True, "data": {"examuserId": 9}}))
        api = self._api(session=sess, cfg=_cfg(browser_for_writes="auto"))
        api.s = FakeHTTP([])
        out = api.start_exam(603733, "226")
        self.assertEqual(out["data"]["examuserId"], 9)
        self.assertEqual(api.s.calls, [])
        self.assertEqual(len(sess.calls), 1)
        path, method, body = sess.calls[0]
        self.assertTrue(path.startswith("api/exam/start/action?"))
        # 前端模板：start/action 的 query 只有 token / yibanId，userId 在 FormData body 里
        self.assertIn("token=tok", path)
        self.assertIn("yibanId=y1", path)
        self.assertNotIn("userId=", path)
        self.assertEqual(method, "POST")
        self.assertEqual(body["examId"], 603733)
        self.assertEqual(body["userId"], 226)

    def test_reads_also_use_page_when_session_present(self):
        sess = FakePageSession((200, {"result": True}))
        api = self._api(session=sess)
        api.s = FakeHTTP([])
        api.user_check()
        self.assertEqual(len(sess.calls), 1)
        self.assertEqual(api.s.calls, [])

    def test_browser_for_writes_never_uses_http(self):
        sess = FakePageSession((200, {"result": True}))
        api = self._api(session=sess, cfg=_cfg(browser_for_writes="never"))
        api.s = FakeHTTP([FakeResp(200, {"result": True})])
        api.start_exam(1, "2")
        self.assertEqual(sess.calls, [])
        self.assertEqual(len(api.s.calls), 1)

    def test_page_fetch_network_failure_on_write_is_not_retried_via_http(self):
        sess = FakePageSession(( -1, {"__error": "TypeError: Failed to fetch"}))
        api = self._api(session=sess, cfg=_cfg(browser_for_writes="auto"))
        api.s = FakeHTTP([FakeResp(200, {"result": True})])
        with self.assertRaises(TransientError) as cm:
            api.start_exam(1, "2")
        self.assertTrue(getattr(cm.exception, "ambiguous_write", False))
        self.assertEqual(api.s.calls, [])

    def test_page_session_error_auth_maps_to_auth_error(self):
        from src.errors import SessionError

        sess = FakePageSession(fail=SessionError("会话失效（HTTP 401）：token 过期"))
        api = self._api(session=sess)
        with self.assertRaises(AuthError):
            api.user_check()

    def test_read_failure_does_not_disable_write_page_channel(self):
        """R-01 回归：一次读失败只能降级读，start/submit 仍必须走页面 FormData。"""

        class FlakyPageSession:
            def __init__(self):
                self.calls = []

            def page_fetch(self, path, method="GET", body=None):
                self.calls.append((path, method))
                if method == "GET":
                    return (-1, {"__error": "read broken"})
                return (200, {"result": True, "data": {"examuserId": 9}})

        sess = FlakyPageSession()
        cfg = _cfg(browser_for_writes="auto")
        api = YoocApi(cfg, {"user_id": "226", "user_token": "tok", "yiban_id": "y1"}, session=sess)
        api.s = FakeHTTP([FakeResp(200, {"result": True, "data": {}})])
        api.get("api/exam/paper/get", {"examuserId": 1})  # 读失败 → HTTP 降级
        api.start_exam(603733, "226")  # 写仍然必须走页面
        methods = [m for _p, m in sess.calls]
        self.assertIn("POST", methods)
        # 读只走了一次 HTTP 兜底；写没有走 HTTP
        self.assertEqual([c[0] for c in api.s.calls], ["GET"])

    def test_auth_refresh_retries_once(self):
        api = self._api([FakeResp(403, {}), FakeResp(200, {"result": True})])
        refreshed = []

        def on_auth():
            refreshed.append(1)
            return {"user_id": "1", "user_token": "new", "yiban_id": "2"}

        api._on_auth_error = on_auth
        out = api.get("api/exam/list/get")
        self.assertTrue(out["result"])
        self.assertEqual(len(refreshed), 1)
        self.assertEqual(api.token, "new")

    def test_cookie_injection(self):
        api = self._api([FakeResp(200, {"result": True})])
        n = api.load_cookies(
            [
                {"name": "user_token", "value": "x", "domain": ".yooc.me", "path": "/"},
                {"name": "", "value": "bad"},
            ]
        )
        self.assertEqual(n, 1)
        self.assertEqual(api.s.cookies.get("user_token"), "x")


_NAV_MSG = (
    "页面内请求执行失败：Page.evaluate: Execution context was destroyed, "
    "most likely because of a navigation."
)


class PageChannelNavigationTests(unittest.TestCase):
    """页面导航导致的 Execution context destroyed 不该被当成通道故障。"""

    NAV_ERR = (
        "页面内请求执行失败：Page.evaluate: Execution context was destroyed, "
        "most likely because of a navigation."
    )

    def _api(self, session, cfg=None):
        cfg = cfg or _cfg(browser_for_writes="auto")
        api = YoocApi(
            cfg,
            {"user_id": "226", "user_token": "tok", "yiban_id": "y1"},
            session=session,
        )
        return api

    def test_navigation_error_on_read_retries_once_and_recovers(self):
        from src.errors import SessionError

        class NavOnce:
            def __init__(self):
                self.calls = 0

            def page_fetch(self, path, method="GET", body=None):
                self.calls += 1
                if self.calls == 1:
                    raise SessionError(_NAV_MSG)
                return (200, {"result": True})

        sess = NavOnce()
        api = self._api(sess)
        api.s = FakeHTTP([])
        out = api.user_check()
        self.assertTrue(out["result"])
        self.assertEqual(sess.calls, 2)          # 重试了一次
        self.assertEqual(api._page_fail_streak, 0)  # 不算故障
        self.assertFalse(api._page_disabled)
        self.assertEqual(api.s.calls, [])        # 没有退回纯 HTTP

    def test_navigation_error_on_write_is_not_retried(self):
        from src.errors import SessionError

        class NavAlways:
            def __init__(self):
                self.calls = 0

            def page_fetch(self, path, method="GET", body=None):
                self.calls += 1
                raise SessionError(_NAV_MSG)

        sess = NavAlways()
        api = self._api(sess)
        api.s = FakeHTTP([])
        with self.assertRaises(TransientError):
            api.start_exam(603733, "226")
        self.assertEqual(sess.calls, 1)   # 写操作绝不重发
        self.assertEqual(api.s.calls, [])

    def test_channel_disabled_only_after_consecutive_failures(self):
        class AlwaysBroken:
            def __init__(self):
                self.calls = 0

            def page_fetch(self, path, method="GET", body=None):
                self.calls += 1
                return (-1, {"__error": "boom"})

        sess = AlwaysBroken()
        api = self._api(sess)
        api.s = FakeHTTP([FakeResp(200, {"result": True}) for _ in range(5)])
        for _ in range(3):
            api.user_check()
        self.assertEqual(sess.calls, 3)
        self.assertTrue(api._page_disabled)
        api.user_check()                 # 第 4 次起不再碰页面
        self.assertEqual(sess.calls, 3)
        self.assertEqual(len(api.s.calls), 4)


class SubmitBodyTests(unittest.TestCase):
    def _q(self, qid, qtype, ans_keys=None, text="", raw=None):
        from src.parser import Option, Question

        opts = [Option("A", "1"), Option("B", "2")] if qtype not in ("fill", "essay") else []
        q = Question(
            qid=qid,
            section_id=7,
            index=1,
            stem="s",
            qtype=qtype,
            options=opts,
            answer_keys=ans_keys or [],
            answer_text=text,
            raw=raw or {},
        )
        return q

    def test_answer_object_shape_matches_frontend(self):
        import json

        from src.runner import build_submit_body

        q1 = self._q(101, "single", ["A"])
        q1.has_key, q1.key_keys = True, ["A"]          # 答对 → correctness 1
        q2 = self._q(102, "multi", ["B"])
        q2.has_key, q2.key_keys = True, ["A", "B"]     # 答错 → -1
        body = build_submit_body(9, [q1, q2], True, score="ENC")
        parsed = json.loads(body["answer"])
        # {subjectId: [{1:[选项下标字符串]}, correctness]}
        self.assertEqual(parsed["101"], [{"1": ["0"]}, 1])
        self.assertEqual(parsed["102"], [{"1": ["1"]}, -1])
        self.assertEqual(body["score"], "ENC")
        self.assertEqual(body["examuserId"], 9)

    def test_completion_multi_inputs(self):
        import json

        from src.runner import build_submit_body

        q = self._q(201, "fill", text="北京|||上海", raw={"inputs": "2"})
        parsed = json.loads(build_submit_body(9, [q], True)["answer"])
        self.assertEqual(parsed["201"][0], {"1": ["北京"], "2": ["上海"]})

    def test_local_score(self):
        from src.runner import local_correct_total

        q1 = self._q(101, "single", ["A"])
        q1.has_key, q1.key_keys, q1.score = True, ["A"], 2
        q2 = self._q(102, "single", ["B"])
        q2.has_key, q2.key_keys, q2.score = True, ["A"], 2
        self.assertEqual(local_correct_total([q1, q2]), 2.0)


class PayloadMarkerTests(unittest.TestCase):
    def test_numeric_status_marks_submitted(self):
        from src.runner import _payload_submitted

        self.assertTrue(_payload_submitted({"result": True, "data": {"status": 4}}))
        self.assertTrue(_payload_submitted({"data": [{"status": "5"}]}))
        self.assertFalse(_payload_submitted({"data": {"status": 3}}))
        self.assertFalse(_payload_submitted({"data": {"score": 0}}))

    def test_submit_time_marks_submitted(self):
        from src.runner import _payload_submitted

        self.assertTrue(_payload_submitted({"data": {"submitTime": "2026-10-19 01:00"}}))
        self.assertFalse(_payload_submitted({"data": {"submitTime": ""}}))


if __name__ == "__main__":
    unittest.main()
