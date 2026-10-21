"""试卷结构解析。

前端 paper/get 返回 section(大题) → subject(题目) 两层，
但字段名在不同考试模板下会有差异，所以这里全部做多字段兜底，
并保留原始 dict，便于按真实返回微调。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# ---- 字段名兜底表 ---------------------------------------------------------
SECTIONS_KEYS = ("sections", "sectionList", "groups", "data", "paper")
SUBJECTS_KEYS = ("subjects", "subjectList", "questions", "questionList", "items")
STEM_KEYS = ("content", "stem", "title", "question", "name", "subjectContent", "topic")
OPTION_KEYS = ("option", "options", "optionList", "choices", "items", "optionContents")
TYPE_KEYS = ("type", "questionType", "subjectType", "question_type", "category", "categoryId")
ID_KEYS = ("subjectId", "id", "questionId", "subject_id", "qid")
SECTION_ID_KEYS = ("sectionId", "id", "section_id", "groupId")

MULTI_FLAGS = ("isMultiple", "multiple", "multi", "isMulti", "isMultipleChoice")

# yooc 常见题型编码（不同题库有出入，只作初判，最终由选项特征兜底）
TYPE_CODE = {
    1: "single",
    2: "multi",
    3: "judge",
    4: "fill",
    5: "essay",
    6: "essay",
}


def _first(d: dict, keys: tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, "", []):
            return d[k]
    return default


def _clean(text: Any) -> str:
    """题干/选项去掉 HTML 标签，压缩空白。title 可能是数组，先拼起来。"""
    if text is None:
        return ""
    if isinstance(text, (list, tuple)):
        text = "".join(str(x) for x in text)
    s = str(text)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</p>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return re.sub(r"[ \t]+", " ", s).strip()


@dataclass
class Option:
    key: str            # 展示用标识：A / B / C …
    text: str
    raw: dict = field(default_factory=dict)

    @property
    def ident(self) -> Any:
        """原始标识（可能是 optionId / 数字 id），提交答案时可能要用。"""
        for k in ("optionId", "id", "value", "optionKey", "key"):
            if k in self.raw and self.raw[k] not in (None, ""):
                return self.raw[k]
        return self.key


@dataclass
class Question:
    qid: Any                 # subjectId
    section_id: Any
    index: int               # 全局序号，从 1 开始
    stem: str
    qtype: str               # single / multi / judge / fill / essay
    options: list[Option]
    score: float = 0.0
    raw: dict = field(default_factory=dict)
    # 答题结果
    answer_keys: list[str] = field(default_factory=list)
    answer_text: str = ""
    confidence: float = 0.0
    reason: str = ""
    # 从 paper.subject.answer 解密出来的正确答案（前端本地判分用）
    key_keys: list[str] = field(default_factory=list)
    key_text: str = ""
    has_key: bool = False
    # 是否已经拿到外部依据（搜索/权威原文）核实过
    verified: bool = False
    # 是否已经用联网搜索核过（与 verified 分开：题库命中算 verified，但没搜过）
    search_checked: bool = False
    evidence: str = ""
    # 交卷前是否被人工改过（人工确认的答案不受 skip_unverified 影响）
    edited: bool = False

    @property
    def answered(self) -> bool:
        return bool(self.answer_keys) or bool(self.answer_text.strip())

    def answer_display(self) -> str:
        if self.answer_text:
            return self.answer_text
        return "".join(self.answer_keys)


def _detect_type(raw: dict, options: list[Option], stem: str) -> str:
    # 1) 显式多选标记
    for f in MULTI_FLAGS:
        v = raw.get(f)
        if isinstance(v, bool) and v:
            return "multi"
        if isinstance(v, (int, str)) and str(v) in ("1", "true", "True"):
            return "multi"

    t = _first(raw, TYPE_KEYS)
    if isinstance(t, str) and not t.isdigit():
        low = t.lower()
        # 真实 bundle 的题型字符串：choice / multiplechoice / completion
        if "multiplechoice" in low or "multiple" in low or "multi" in low or "多选" in t:
            return "multi"
        if "completion" in low or "blank" in low or "填空" in t:
            return "fill"
        if "judge" in low or "判断" in t or "true" in low or "false" in low:
            return "judge"
        if "essay" in low or "简答" in t or "问答" in t or "subjective" in low:
            return "essay"
        # 注意：平台把判断题、多选题也一律标成 "choice"。
        # 所以 "choice"/"single" 不能直接返回，否则判断题会被当成单选，
        # 下面的选项特征判断（2 个选项且是 对/错、正确/错误、√/×）才是准的。
        if ("choice" in low or "single" in low or "单选" in t) and not options:
            return "single"
    elif t is not None:
        try:
            mapped = TYPE_CODE.get(int(t))
            if mapped:
                return mapped
        except (TypeError, ValueError):
            pass

    # 2) 从选项特征反推
    if not options:
        return "fill" if ("___" in stem or "____" in stem) else "essay"

    texts = {o.text.strip().replace(" ", "") for o in options}
    if len(options) == 2 and texts & {"正确", "错误", "对", "错", "√", "×", "是", "否"}:
        return "judge"
    if 2 <= len(options) <= 3 and all(len(t) <= 1 for t in texts) and texts & {"T", "F"}:
        return "judge"
    return "single"


def _parse_options(raw: dict) -> list[Option]:
    arr = _first(raw, OPTION_KEYS, [])
    if isinstance(arr, dict):
        arr = [dict(v, **{"__k": k}) if isinstance(v, dict) else {"content": v, "__k": k}
               for k, v in arr.items()]
    if not isinstance(arr, list):
        return []

    out: list[Option] = []
    for i, item in enumerate(arr):
        letter = chr(ord("A") + i)
        if isinstance(item, dict):
            text = _clean(_first(item, ("content", "text", "option", "value", "name", "title"), ""))
            key = _first(item, ("key", "optionKey", "label", "prefix", "code", "optionLabel"), None)
            if key is None:
                key = item.get("__k") or letter
            key = str(key).strip()
            # 去掉 "A." "A、" 这类前缀，保留干净文本
            text = re.sub(r"^[A-Za-z][\.\、\)）:：]\s*", "", text).strip()
            out.append(Option(key=key, text=text or f"选项{letter}", raw=item))
        else:
            text = _clean(item)
            text = re.sub(r"^[A-Za-z][\.\、\)）:：]\s*", "", text).strip()
            out.append(Option(key=letter, text=text, raw={"content": text}))
    return out


def _iter_sections(payload: Any) -> list[dict]:
    """把各种形态的 paper 数据统一成 section 列表。"""
    if payload is None:
        return []
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        inner = _first(payload, SECTIONS_KEYS, payload)
        if isinstance(inner, list):
            items = inner
        elif isinstance(inner, dict):
            deeper = _first(inner, SECTIONS_KEYS, None)
            items = deeper if isinstance(deeper, list) else [inner]
        else:
            items = [payload]
    else:
        return []

    sections: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        subs = _first(it, SUBJECTS_KEYS, None)
        if isinstance(subs, list) and subs:
            sections.append(it)
        elif sections:
            # 平铺题目：塞进上一个 section
            sections[-1].setdefault("__flat__", []).append(it)
        else:
            sections.append({"__flat__": [it], "sectionId": it.get("sectionId", 0)})
    return sections


def parse_paper(payload: Any) -> tuple[list[Question], list[dict]]:
    """返回 (题目列表, section 原始列表)。"""
    sections = _iter_sections(payload)
    questions: list[Question] = []
    idx = 0
    for sec in sections:
        sid = _first(sec, SECTION_ID_KEYS, 0)
        subs = sec.get("__flat__") or _first(sec, SUBJECTS_KEYS, []) or []
        for sub in subs:
            if not isinstance(sub, dict):
                continue
            idx += 1
            opts = _parse_options(sub)
            stem = _clean(_first(sub, STEM_KEYS, ""))
            qtype = _detect_type(sub, opts, stem)
            score = _first(sub, ("score", "subjectScore", "point", "points"), 0) or 0
            try:
                score = float(score)
            except (TypeError, ValueError):
                score = 0.0
            questions.append(
                Question(
                    qid=_first(sub, ID_KEYS, idx),
                    section_id=sub.get("sectionId", sid),
                    index=idx,
                    stem=stem,
                    qtype=qtype,
                    options=opts,
                    score=score,
                    raw=sub,
                )
            )
    return questions, sections


def _valid_id(v: Any) -> bool:
    return v not in (None, "", 0, "0")


def _match_option(q: "Question", token: Any) -> str | None:
    """把正确答案 token 映射到选项 key。

    前端 UI 提交的是选项下标（0/1/2…），也可以兼容字母/文本/optionId。
    """
    s = str(token).strip()
    if not s:
        return None
    if s.isdigit():
        i = int(s)
        if 0 <= i < len(q.options):
            return q.options[i].key.upper()
    for o in q.options:
        if s.upper() == o.key.upper():
            return o.key.upper()
        if s == o.text.strip():
            return o.key.upper()
        if str(o.ident) == s:
            return o.key.upper()
    return None


def _reject_key(report: list[str] | None, count: int, why: str) -> str:
    msg = (
        f"平台答案疑似占位数据（{count} 题：{why}），已整份忽略，"
        "改由本地题库/大模型重新作答"
    )
    if report is not None:
        report.append(msg)
    else:
        print(f"[warn] {msg}")
    return msg


def apply_answer_key(
    questions: list["Question"],
    yiban_id: Any,
    report: list[str] | None = None,
) -> int:
    """解密 paper 里的正确答案（前端本地判分用的同一份数据）。

    返回成功解出答案的题数。解不出来（字段缺失/流程变了）不影响后续 LLM 答题。

    **占位答案识别**：部分考试模板下，`subject.answer` 并不是真答案，而是平台
    塞的占位密文——实测某场 50 题的 answer 字段完全是同一串
    `GivF87UZNBdRXqWEUxAsqQ==`，解密出来全是 `["1"]`（=B）。如果照单全收，
    整卷答案会变成一模一样的字母，模型和搜索根本没有机会介入。
    这里做两道闸：
      1. 密文完全相同（>=8 题）——AES-CBC 同一 IV 下同明文才同密文，真答案不可能；
      2. 明文完全相同（>=12 题）——换汤不换药的另一种占位写法。
    """
    if not yiban_id:
        return 0
    try:
        from .crypto import decrypt_answer_json
    except Exception:
        return 0

    encs: list[tuple["Question", str]] = []
    for q in questions:
        enc = q.raw.get("answer")
        if isinstance(enc, str) and enc.strip():
            encs.append((q, enc.strip()))
    if not encs:
        return 0

    if len(encs) >= 8 and len({e for _, e in encs}) == 1:
        _reject_key(report, len(encs), "所有题目的 answer 密文完全相同")
        return 0

    decrypted: list[tuple["Question", Any]] = []
    for q, enc in encs:
        try:
            data = decrypt_answer_json(enc, yiban_id)
        except Exception:
            continue
        decrypted.append((q, data))
    if not decrypted:
        return 0

    if len(decrypted) >= 12:
        sigs = [json.dumps(d, ensure_ascii=False, sort_keys=True) for _, d in decrypted]
        if len(set(sigs)) == 1:
            _reject_key(report, len(decrypted), "所有题目的解密结果完全相同")
            return 0

    count = 0
    for q, data in decrypted:
        if q.options:
            tokens = data if isinstance(data, list) else [data]
            keys: list[str] = []
            for tok in tokens:
                k = _match_option(q, tok)
                if k and k not in keys:
                    keys.append(k)
            if not keys:
                continue
            q.key_keys = keys
        else:
            if isinstance(data, list):
                parts: list[str] = []
                for item in data:
                    if isinstance(item, (list, tuple)):
                        parts.extend(str(x) for x in item)
                    else:
                        parts.append(str(item))
            else:
                parts = [str(data)]
            q.key_text = "|||".join(p for p in parts if p != "")
            if not q.key_text:
                continue
        q.has_key = True
        count += 1

    if count == 0 and report is not None:
        report.append("平台答案解密成功但无法映射到任何选项，已忽略")
    return count


def extract_examuser_id(payload: Any) -> Any:
    """start/action 的返回里找出 examuserId（不同版本字段名不一）。

    注意 0 / "0" 是"未开考"的占位值，绝不能当成有效作答记录。
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data", payload)
    if isinstance(data, (str, int)):
        return data if _valid_id(data) else None
    if isinstance(data, dict):
        for k in ("examuserId", "examUserId", "id", "userExamId", "recordId"):
            v = data.get(k)
            if _valid_id(v):
                return v
        inner = data.get("data")
        if isinstance(inner, dict):
            return extract_examuser_id(inner)
        if isinstance(inner, (str, int)):
            return inner if _valid_id(inner) else None
    return None
