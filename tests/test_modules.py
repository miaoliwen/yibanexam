# -*- coding: utf-8 -*-
"""专项模块测试：关键词命中、原文抓取/缓存、依据原文重判。

全部走假 searcher / 假模型，不联网。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.config import Config
from src.modules import ModuleResolver, ModuleRule, strip_html
from src.search import SearchHit

STEM_HIT = "根据《南通师范高等专科学校违纪处分条例》，学生违纪处分的种类不包括下列哪项？"
STEM_OTHER = "中华人民共和国成立于哪一年？"


def _q(stem=STEM_HIT, conf=0.9, keys=("A",), qtype="single"):
    return SimpleNamespace(
        stem=stem,
        qtype=qtype,
        confidence=conf,
        answer_keys=list(keys),
        answer_text="",
        reason="旧结论",
        options=[
            SimpleNamespace(key="A", text="警告"),
            SimpleNamespace(key="B", text="严重警告"),
            SimpleNamespace(key="C", text="罚款"),
            SimpleNamespace(key="D", text="留校察看"),
        ],
    )


class FakeSearcher:
    def __init__(self, hits=None):
        self.hits = hits or []
        self.queries: list[str] = []

    def search(self, query, ori_question=None):
        self.queries.append(query)
        return list(self.hits)


class FakeChat:
    def __init__(self, payload=None):
        self.payload = payload or {"answer": ["C"], "text": "", "confidence": 0.92, "reason": "第三条未列罚款"}
        self.calls = 0
        self.last_messages = None

    def __call__(self, messages):
        self.calls += 1
        self.last_messages = messages
        return json.dumps(self.payload, ensure_ascii=False)


def _cfg(tmp: Path) -> Config:
    cfg = Config()
    cfg.modules.enabled = True
    cfg.modules.rules = [
        {
            "name": "南通师范高等专科学校学生违纪处分条例",
            "keywords": ["违纪处分条例", "违纪处分", "处分条例"],
            "urls": ["https://xgc.ntnc.edu.cn/2018/1023/c609a18764/page.htm"],
            "query": "南通师范高等专科学校 学生违纪处分条例",
        }
    ]
    cfg.modules.min_confidence = 0.5
    cfg.modules.max_questions = 20
    return cfg


class StripHtmlTests(unittest.TestCase):
    def test_drops_script_and_keeps_body(self):
        raw = "<div><script>var x=1;</script><p>第三条 处分种类</p><style>.a{}</style></div>"
        out = strip_html(raw)
        self.assertIn("第三条 处分种类", out)
        self.assertNotIn("var x", out)
        self.assertNotIn(".a{}", out)

    def test_unescapes_entities(self):
        self.assertIn("《条例》", strip_html("&lt;&#12298;条例&#12299;&gt;").replace("&lt;", "").replace("&gt;", ""))


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _cfg(Path(self.tmp.name))
        self.r = ModuleResolver(self.cfg, cache_dir=Path(self.tmp.name) / "cache", log=lambda m: None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_matches_keyword_in_stem(self):
        rule = self.r.match(STEM_HIT)
        self.assertIsNotNone(rule)
        self.assertEqual(rule.name, "南通师范高等专科学校学生违纪处分条例")

    def test_no_match_for_unrelated(self):
        self.assertIsNone(self.r.match(STEM_OTHER))

    def test_rule_match_any_keyword(self):
        rule = ModuleRule(name="x", keywords=("记过", "留校察看"))
        self.assertTrue(rule.match("被给予留校察看处分"))
        self.assertFalse(rule.match("完全无关"))

    def test_disabled_when_no_rules(self):
        cfg = _cfg(Path(self.tmp.name))
        cfg.modules.rules = []
        r = ModuleResolver(cfg, cache_dir=Path(self.tmp.name) / "c2", log=lambda m: None)
        self.assertFalse(r.enabled)


class FetchCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cfg = _cfg(self.dir)
        self.r = ModuleResolver(self.cfg, cache_dir=self.dir / "cache", log=lambda m: None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cache_hit_skips_network(self):
        url = "https://xgc.ntnc.edu.cn/2018/1023/c609a18764/page.htm"
        self.r.cache_dir.mkdir(parents=True, exist_ok=True)
        self.r._cache_path(url).write_text("第三条 处分种类有五类", encoding="utf-8")
        # 没有网络也应当直接读到缓存
        self.assertIn("处分种类", self.r.fetch_page(url))


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cfg = _cfg(self.dir)
        self.rule = ModuleRule(
            name="南通师范高等专科学校学生违纪处分条例",
            keywords=("违纪处分条例",),
            urls=("https://xgc.ntnc.edu.cn/2018/1023/c609a18764/page.htm",),
            query="南通师范高等专科学校 学生违纪处分条例",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _resolver(self, chat, hits=None):
        r = ModuleResolver(
            self.cfg,
            searcher=FakeSearcher(hits),
            chat=chat,
            cache_dir=self.dir / "cache",
            log=lambda m: None,
        )
        # 不真的联网：原文固定
        r.fetch_page = lambda url, force=False: "第三条 学生违纪处分的种类分为：警告、严重警告、记过、留校察看、开除学籍。"
        return r

    def test_adopts_authoritative_answer(self):
        q = _q(conf=0.95, keys=("A",))
        chat = FakeChat({"answer": ["C"], "text": "", "confidence": 0.9, "reason": "第三条未列罚款"})
        st = self._resolver(chat).resolve([q])
        self.assertEqual(st["matched"], 1)
        self.assertEqual(st["changed"], 1)
        self.assertEqual(q.answer_keys, ["C"])
        self.assertIn("模块:", q.reason)
        # 材料里必须带原文
        self.assertIn("第三条", chat.last_messages[1]["content"])

    def test_low_confidence_is_not_adopted(self):
        q = _q(conf=0.8, keys=("A",))
        chat = FakeChat({"answer": ["C"], "text": "", "confidence": 0.2, "reason": "原文没写到"})
        st = self._resolver(chat).resolve([q])
        self.assertEqual(st["resolved"], 1)
        self.assertEqual(st["changed"], 0)
        self.assertEqual(q.answer_keys, ["A"])  # 保留原判

    def test_unmatched_question_untouched(self):
        q = _q(stem=STEM_OTHER, keys=("B",))
        chat = FakeChat()
        st = self._resolver(chat).resolve([q])
        self.assertEqual(st["matched"], 0)
        self.assertEqual(q.answer_keys, ["B"])
        self.assertEqual(chat.calls, 0)

    def test_search_query_is_site_scoped(self):
        q = _q()
        searcher = FakeSearcher([SearchHit(title="t", url="https://xgc.ntnc.edu.cn/a.htm", snippet="s", summary="正文")])
        r = ModuleResolver(self.cfg, searcher=searcher, chat=FakeChat(), cache_dir=self.dir / "c", log=lambda m: None)
        r.fetch_page = lambda url, force=False: ""
        r.resolve([q])
        self.assertTrue(searcher.queries)
        self.assertTrue(searcher.queries[0].startswith("site:xgc.ntnc.edu.cn"))

    def test_no_material_marks_failed_and_keeps_answer(self):
        q = _q(keys=("A",))
        r = ModuleResolver(self.cfg, searcher=None, chat=FakeChat(), cache_dir=self.dir / "c", log=lambda m: None)
        r.fetch_page = lambda url, force=False: ""
        st = r.resolve([q])
        self.assertEqual(st["failed"], 1)
        self.assertEqual(st["changed"], 0)
        self.assertEqual(q.answer_keys, ["A"])


if __name__ == "__main__":
    unittest.main()
