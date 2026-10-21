# -*- coding: utf-8 -*-
"""本地题库测试：TXT 解析、题干归一化、乱序选项映射、判断题极性、批处理覆盖策略。

全部离线，不联网。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.bank_builder import build_bank, parse_answer_txt
from src.qbank import QuestionBank, normalize, resolve_questions

SAMPLE = """1.
题干：南通师范高等专科学校肇端于（ B）的民立通州师范学校，是中国师范教育的三大源头之一。
A. 1901年
B. 1902年
C. 1903年
D. 1904年

2.
题干：如果在我校图书馆借阅的图书需要续借，需在（ ）办理续借手续。 C
A. 书借阅到期后
B. 书借出后的任何一天
C. 书借出后的第11至第20天
D. 书借出后的第21-30天

3.
题干：学校坚持教育与宗教相分离原则。任何组织和个人不得在学校进行宗教活动。  A
A. 对
B. 错

4.
题干：受到违纪处分者，处分期内仍有评奖评优资格。 B
A. 对
B. 错
"""


def _q(stem, options, qtype="single", keys=(), text="", reason="", has_key=False, edited=False):
    return SimpleNamespace(
        stem=stem,
        qtype=qtype,
        options=[SimpleNamespace(key=k, text=t) for k, t in options],
        answer_keys=list(keys),
        answer_text=text,
        confidence=1.0 if keys else 0.0,
        reason=reason,
        verified=False,
        evidence="",
        has_key=has_key,
        edited=edited,
    )


class TestParse(unittest.TestCase):
    def test_parse_inline_and_trailing_answer(self):
        items = parse_answer_txt(SAMPLE)
        self.assertEqual(len(items), 4)
        self.assertEqual([it["no"] for it in items], [1, 2, 3, 4])
        self.assertEqual(items[0]["answer"], ["B"])
        self.assertEqual(items[0]["answer_texts"], ["1902年"])
        self.assertEqual(items[1]["answer"], ["C"])          # 答案在题干末尾
        self.assertEqual(items[1]["answer_from"], "trail")
        self.assertEqual(items[2]["qtype"], "judge")
        self.assertEqual(items[3]["answer"], ["B"])

    def test_build_bank_writes_json(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "bank.json"
            bank = build_bank_text(SAMPLE, out)
            self.assertEqual(bank["count"], 4)
            self.assertTrue(out.exists())
            loaded = QuestionBank.load(out)
            self.assertEqual(len(loaded), 4)


def build_bank_text(text: str, out: Path):
    src = out.parent / "a.txt"
    src.write_text(text, encoding="utf-8")
    return build_bank(src, out, name="样例", verbose=False)


class TestMatch(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        out = Path(self.td.name) / "bank.json"
        build_bank_text(SAMPLE, out)
        self.bank = QuestionBank.load(out)

    def test_normalize_ignores_punctuation_and_width(self):
        a = normalize("南通师范高等专科学校肇端于（ ）的民立通州师范学校")
        b = normalize("南通师范高等专科学校肇端于(  )的民立通州师范学校。")
        self.assertEqual(a, b)

    def test_options_shuffled_maps_by_text(self):
        # 卷面把 1902 年放到 D，答案必须是 D 而不是题库里的 B
        q = _q(
            "南通师范高等专科学校肇端于（ ）的民立通州师范学校，是中国师范教育的三大源头之一。",
            [("A", "1901年"), ("B", "1903年"), ("C", "1904年"), ("D", "1902年")],
        )
        hit = self.bank.match(q.stem, q.options, q.qtype)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.keys, ["D"])

    def test_trailing_answer_question(self):
        q = _q(
            "如果在我校图书馆借阅的图书需要续借，需在（ ）办理续借手续。",
            [
                ("A", "书借出后的第11至第20天"),
                ("B", "书借阅到期后"),
                ("C", "书借出后的任何一天"),
                ("D", "书借出后的第21-30天"),
            ],
        )
        hit = self.bank.match(q.stem, q.options, q.qtype)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.keys, ["A"])

    def test_judge_polarity(self):
        q = _q(
            "受到违纪处分者，处分期内仍有评奖评优资格。",
            [("A", "正确"), ("B", "错误")],
            "judge",
        )
        hit = self.bank.match(q.stem, q.options, q.qtype)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.keys, ["B"])   # 原题选「错」

    def test_fuzzy_with_prefix(self):
        # 平台在题干前加了「（单选题）」也要能命中
        q = _q(
            "（单选题）南通师范高等专科学校肇端于（ ）的民立通州师范学校，是中国师范教育的三大源头之一。",
            [("A", "1901年"), ("B", "1902年")],
        )
        hit = self.bank.match(q.stem, q.options, q.qtype)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.keys, ["B"])
        self.assertEqual(hit.kind, "fuzzy")

    def test_unrelated_question_miss(self):
        q = _q("中华人民共和国成立于哪一年？", [("A", "1949年"), ("B", "1978年")])
        self.assertIsNone(self.bank.match(q.stem, q.options, q.qtype))


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        out = Path(self.td.name) / "bank.json"
        build_bank_text(SAMPLE, out)
        self.bank = QuestionBank.load(out)

    def test_fill_empty_and_mark_verified(self):
        q = _q(
            "南通师范高等专科学校肇端于（ ）的民立通州师范学校，是中国师范教育的三大源头之一。",
            [("A", "1901年"), ("B", "1902年")],
        )
        st = resolve_questions(self.bank, [q])
        self.assertEqual(st.applied, 1)
        self.assertEqual(q.answer_keys, ["B"])
        self.assertTrue(q.verified)
        self.assertIn("本地题库", q.reason)

    def test_keep_platform_answer_by_default(self):
        q = _q(
            "南通师范高等专科学校肇端于（ ）的民立通州师范学校，是中国师范教育的三大源头之一。",
            [("A", "1901年"), ("B", "1902年")],
            keys=["A"],
            reason="[平台答案]",
            has_key=True,
        )
        st = resolve_questions(self.bank, [q])
        self.assertEqual(q.answer_keys, ["A"])
        self.assertEqual(st.kept, 1)
        self.assertEqual(st.conflict, 1)
        self.assertEqual(st.applied, 0)

    def test_replace_model_answer(self):
        # 模型旧答案（非平台解密）应被题库覆盖，重跑时才不会留着错答案
        q = _q(
            "南通师范高等专科学校肇端于（ ）的民立通州师范学校，是中国师范教育的三大源头之一。",
            [("A", "1901年"), ("B", "1902年")],
            keys=["A"],
            reason="[断点恢复] 模型判断",
        )
        st = resolve_questions(self.bank, [q])
        self.assertEqual(q.answer_keys, ["B"])
        self.assertEqual(st.applied, 1)
        self.assertEqual(st.replaced, 1)

    def test_keep_edited_answer(self):
        q = _q(
            "南通师范高等专科学校肇端于（ ）的民立通州师范学校，是中国师范教育的三大源头之一。",
            [("A", "1901年"), ("B", "1902年")],
            keys=["A"],
            edited=True,
        )
        st = resolve_questions(self.bank, [q])
        self.assertEqual(q.answer_keys, ["A"])
        self.assertEqual(st.kept, 1)

    def test_overwrite_true(self):
        q = _q(
            "南通师范高等专科学校肇端于（ ）的民立通州师范学校，是中国师范教育的三大源头之一。",
            [("A", "1901年"), ("B", "1902年")],
            keys=["A"],
        )
        st = resolve_questions(self.bank, [q], overwrite=True)
        self.assertEqual(q.answer_keys, ["B"])
        self.assertEqual(st.applied, 1)

    def test_edited_survives_even_overwrite(self):
        q = _q(
            "南通师范高等专科学校肇端于（ ）的民立通州师范学校，是中国师范教育的三大源头之一。",
            [("A", "1901年"), ("B", "1902年")],
            keys=["A"],
            edited=True,
        )
        st = resolve_questions(self.bank, [q], overwrite=True)
        self.assertEqual(q.answer_keys, ["A"])
        self.assertEqual(st.kept, 1)


if __name__ == "__main__":
    unittest.main()
