"""本地题库：把一份「答案 TXT」变成可命中的题库，在拉卷后先于大模型作答。

为什么要单独一层：
1. 入学教育这类考试题目固定，答案就在手边的 TXT 里，直接命中比让模型猜准得多；
2. 线上卷面的**选项顺序常常被打乱**，所以不能照抄题库里的字母，
   必须按「选项文本」把答案映射回当前卷面的 A/B/C/D；
3. 平台 paper 里偶尔能解出答案（apply_answer_key），但字段缺失时就解不出，
   题库正好补上这个空档。

命中优先级（runner 里体现）：
    平台解密答案 > 本地题库 > 专项模块 > 大模型 > 搜索核实 > 兜底猜测
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

DEFAULT_MIN_SCORE = 0.86
DEFAULT_MARGIN = 0.03

_JUDGE_TEXTS = {"对", "错", "正确", "错误", "是", "否", "√", "×", "t", "f", "true", "false"}
_TRUE_WORDS = {"对", "正确", "是", "√", "t", "true", "yes", "1", "a"}
_FALSE_WORDS = {"错", "错误", "否", "×", "x", "f", "false", "no", "0", "b"}


# ------------------------------------------------------------------ 归一化
def normalize(text: Any) -> str:
    """题干归一化：全角转半角、去空白、去标点，只留汉字/字母/数字。

    这样「（ B）」「(B)」「（  ）」、中英文标点差异都不会影响精确命中。
    """
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = s.lower()
    return "".join(ch for ch in s if ch.isalnum())


def _polarity(text: Any) -> int:
    """判断题选项的极性：1=对 / -1=错 / 0=认不出。"""
    s = unicodedata.normalize("NFKC", str(text or "")).strip().lower()
    s = re.sub(r"\s+", "", s)
    if s in _TRUE_WORDS:
        return 1
    if s in _FALSE_WORDS:
        return -1
    return 0


def option_key_set(options: Any) -> set[str]:
    if isinstance(options, dict):
        return {str(k).strip().upper() for k in options}
    out: set[str] = set()
    for o in options or []:
        k = getattr(o, "key", None)
        if k is not None:
            out.add(str(k).strip().upper())
    return out


def _stem_score(a: str, b: str) -> float:
    """两条归一化题干的相似度，0~1。"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b, autojunk=False).ratio()
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= 10 and shorter in longer:
        cover = len(shorter) / len(longer)
        ratio = max(ratio, 0.90 + 0.09 * cover)
    return ratio


def _text_score(a: str, b: str) -> float:
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.92
    return SequenceMatcher(None, na, nb, autojunk=False).ratio()


# ------------------------------------------------------------------ 数据结构
@dataclass
class BankEntry:
    id: str
    no: int
    qtype: str
    stem: str
    norm: str
    options: dict[str, str] = field(default_factory=dict)
    answer: list[str] = field(default_factory=list)
    answer_texts: list[str] = field(default_factory=list)


@dataclass
class BankHit:
    entry: BankEntry
    keys: list[str] = field(default_factory=list)
    text: str = ""
    score: float = 0.0
    kind: str = "exact"      # exact / fuzzy
    how: str = "text"        # text=按选项文本映射 / letter=按字母 / judge=判断题极性
    note: str = ""


@dataclass
class BankStats:
    enabled: bool = False
    total: int = 0
    matched: int = 0
    applied: int = 0
    replaced: int = 0        # 覆盖了模型/猜测/断点恢复的旧答案
    kept: int = 0            # 保留已有权威答案（平台答案 / 人工修改）
    conflict: int = 0        # 题库答案与已有答案不一致
    missed: int = 0
    bank_count: int = 0
    bank_name: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "total": self.total,
            "matched": self.matched,
            "applied": self.applied,
            "replaced": self.replaced,
            "kept": self.kept,
            "conflict": self.conflict,
            "missed": self.missed,
            "bank_count": self.bank_count,
            "bank_name": self.bank_name,
        }


# ------------------------------------------------------------------ 题库
class QuestionBank:
    def __init__(self, data: dict[str, Any], source: str = ""):
        self.name = str(data.get("name") or "题库")
        self.source = source or str(data.get("source") or "")
        self.entries: list[BankEntry] = []
        for raw in data.get("items") or []:
            if not isinstance(raw, dict):
                continue
            stem = str(raw.get("stem") or "").strip()
            if not stem:
                continue
            opts = raw.get("options") or {}
            if not isinstance(opts, dict):
                opts = {}
            self.entries.append(
                BankEntry(
                    id=str(raw.get("id") or f"q{len(self.entries) + 1}"),
                    no=int(raw.get("no") or len(self.entries) + 1),
                    qtype=str(raw.get("qtype") or "single"),
                    stem=stem,
                    norm=str(raw.get("norm") or normalize(stem)),
                    options={str(k).upper(): str(v) for k, v in opts.items()},
                    answer=[str(x).upper() for x in (raw.get("answer") or [])],
                    answer_texts=[str(x) for x in (raw.get("answer_texts") or []) if str(x)],
                )
            )
        self._exact: dict[str, BankEntry] = {}
        for e in self.entries:
            self._exact.setdefault(e.norm, e)

    # -------------------------------------------------------------- 加载
    @classmethod
    def load(cls, path: str | Path) -> "QuestionBank":
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError(f"题库格式不对（顶层应为对象）：{p}")
        bank = cls(data, source=str(p))
        if not bank.entries:
            raise ValueError(f"题库里没有题目：{p}")
        return bank

    def __len__(self) -> int:
        return len(self.entries)

    # -------------------------------------------------------------- 匹配
    def match(
        self,
        stem: str,
        options: Iterable[Any] | None = None,
        qtype: str = "",
        *,
        min_score: float | None = None,
        margin: float = DEFAULT_MARGIN,
    ) -> BankHit | None:
        """给一道线上题找到题库条目，并把答案映射到当前选项。"""
        live = _live_options(options)
        n = normalize(stem)
        if len(n) < 5:
            return None
        threshold = DEFAULT_MIN_SCORE if min_score is None else float(min_score)

        hit_entry: BankEntry | None = self._exact.get(n)
        score = 1.0
        kind = "exact"

        if hit_entry is None:
            scored: list[tuple[float, BankEntry]] = []
            for e in self.entries:
                if not e.norm:
                    continue
                # 长度差太大的直接跳过，省掉大量无意义的 ratio 计算
                if abs(len(e.norm) - len(n)) > max(len(n), len(e.norm)) * 0.45:
                    continue
                s = _stem_score(n, e.norm)
                if s >= max(0.55, threshold - 0.25):
                    scored.append((s, e))
            if not scored:
                return None
            scored.sort(key=lambda x: x[0], reverse=True)
            best_score, best_entry = scored[0]
            second = scored[1][0] if len(scored) > 1 else 0.0

            overlap = _option_overlap(best_entry, live)
            combined = best_score * 0.85 + overlap * 0.15 if live else best_score
            if combined < threshold:
                return None
            # 两道题开头高度相似（例如同一份条例的相邻条款）时，
            # 分数必须拉开差距，否则宁可不命中，交给模型。
            if (
                best_score < 0.98
                and second >= threshold - 0.02
                and best_score - second < margin
            ):
                return None
            if live and best_entry.options and overlap < 0.5:
                return None
            hit_entry, score, kind = best_entry, combined, "fuzzy"

        mapped = _map_answer(hit_entry, live, qtype)
        if mapped is None:
            # 题干命中了但答案映射不回去（选项文本对不上）：不硬套，交给后续环节
            return None
        keys, text, how = mapped
        note = f"#{hit_entry.no} {kind} {score:.2f}"
        return BankHit(
            entry=hit_entry,
            keys=keys,
            text=text,
            score=score,
            kind=kind,
            how=how,
            note=note,
        )


def _live_options(options: Iterable[Any] | None) -> list[tuple[str, str]]:
    if not options:
        return []
    out: list[tuple[str, str]] = []
    if isinstance(options, dict):
        for k, v in options.items():
            out.append((str(k).upper(), str(v)))
        return out
    for i, o in enumerate(options):
        key = getattr(o, "key", None)
        text = getattr(o, "text", None)
        if key is None and isinstance(o, dict):
            key = o.get("key")
            text = o.get("text")
        out.append((str(key if key is not None else chr(65 + i)).upper(), str(text or "")))
    return out


def _option_overlap(entry: BankEntry, live: list[tuple[str, str]]) -> float:
    """题库选项文本在线上选项里的覆盖率（正确处理选项被打乱的情况）。"""
    if not entry.options or not live:
        return 0.0
    texts = [t for t in entry.options.values() if t]
    if not texts:
        return 0.0
    hit = 0
    for bt in texts:
        if any(_text_score(bt, lt) >= 0.90 for _, lt in live):
            hit += 1
    return hit / len(texts)


def _is_judge(entry: BankEntry) -> bool:
    if entry.qtype == "judge":
        return True
    if len(entry.options) != 2:
        return False
    texts = {normalize(v) for v in entry.options.values()}
    return bool(texts) and all(_polarity(v) for v in entry.options.values())


def _map_answer(
    entry: BankEntry, live: list[tuple[str, str]], qtype: str
) -> tuple[list[str], str, str] | None:
    """把题库答案映射到当前卷面的选项字母。

    返回 (keys, text, how)；映射不回去返回 None。
    """
    if not live:
        text = "|||".join(entry.answer_texts)
        if not text and entry.answer:
            return None
        return [], text, "text"

    live_keys = [k for k, _ in live]
    live_norm = {k: normalize(t) for k, t in live}

    # 判断题：卷面常见「对/错」「正确/错误」「√/×」「T/F」，按极性对齐
    if _is_judge(entry) or qtype == "judge":
        target = 0
        for letter in entry.answer:
            target = _polarity(entry.options.get(letter, ""))
            if target:
                break
        if target:
            for k, t in live:
                if _polarity(t) == target:
                    return [k], "", "judge"
        # 极性认不出就退回文本比较

    # 一般选择：按选项文本找，逐项取最相似的线上选项
    keys: list[str] = []
    for letter in entry.answer:
        btext = entry.options.get(letter, "")
        if not btext:
            continue
        bn = normalize(btext)
        exact = [k for k in live_keys if live_norm[k] == bn]
        if exact:
            keys.append(exact[0])
            continue
        best_k, best_s = None, 0.0
        for k, t in live:
            s = _text_score(btext, t)
            if s > best_s:
                best_k, best_s = k, s
        if best_k and best_s >= 0.86:
            keys.append(best_k)
            continue
        keys = []
        break

    if keys and len(set(keys)) == len(keys):
        order = {k: i for i, k in enumerate(live_keys)}
        keys.sort(key=lambda x: order.get(x, 99))
        return keys, "", "text"

    # 选项文本对不上（被改写/转义）时，退回按字母：仅在选项数量一致时谨慎使用
    if entry.answer and set(entry.answer) <= set(live_keys):
        if len(entry.options) == len(live):
            return list(entry.answer), "", "letter"
    return None


# ------------------------------------------------------------------ 批处理
def resolve_questions(
    bank: QuestionBank | None,
    questions: list[Any],
    *,
    overwrite: bool = False,
    min_score: float | None = None,
) -> BankStats:
    """给整卷题目跑一遍题库命中。

    覆盖策略（overwrite=False 时）：
    - 平台解密出的答案（`has_key` / reason 带「平台答案」）保留，题库不动它；
    - 人工改过的答案（`edited`）保留；
    - 模型答案 / 兜底猜测 / 断点恢复的旧结论 → 题库命中就直接替换（题库更准）；
    - 空白题 → 直接填。

    overwrite=True 时连平台答案和人工修改一起覆盖（慎用）。
    """
    st = BankStats(enabled=bank is not None, total=len(questions))
    if bank is None:
        return st
    st.bank_count = len(bank)
    st.bank_name = bank.name

    for q in questions:
        hit = bank.match(
            getattr(q, "stem", "") or "",
            getattr(q, "options", None),
            getattr(q, "qtype", "") or "",
            min_score=min_score,
        )
        if hit is None:
            st.missed += 1
            continue
        st.matched += 1

        existing_keys = [str(k).upper() for k in (getattr(q, "answer_keys", None) or [])]
        existing_text = str(getattr(q, "answer_text", "") or "")
        answered = bool(existing_keys or existing_text)
        reason = str(getattr(q, "reason", "") or "")
        protected = bool(getattr(q, "has_key", False)) or "平台答案" in reason
        edited = bool(getattr(q, "edited", False))

        if answered and edited:
            # 人工确认过的答案永远保留（overwrite=true 也不动），避免覆盖手工修正
            st.kept += 1
            continue
        if answered and protected and not overwrite:
            st.kept += 1
            if hit.keys and existing_keys and set(hit.keys) != set(existing_keys):
                st.conflict += 1
            continue

        if hit.keys:
            q.answer_keys = list(hit.keys)
            q.answer_text = ""
        elif hit.text:
            q.answer_keys = []
            q.answer_text = hit.text
        else:
            st.missed += 1
            continue

        q.confidence = round(min(0.995, 0.90 + 0.10 * hit.score), 3)
        q.reason = f"[本地题库 {hit.note}]"
        q.verified = True
        q.evidence = f"本地题库《{bank.name}》第{hit.entry.no}题（{hit.how}）"
        st.applied += 1
        if answered:
            st.replaced += 1

    return st
