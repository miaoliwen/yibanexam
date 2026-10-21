"""大模型答题。

兼容任何 OpenAI 协议端点：DeepSeek / 通义 / Kimi / 智谱 / OpenAI / 本地 vLLM 等，
只改 config.yaml 里的 base_url + model + api_key 即可。

v2 变化（鲁棒性）：
- 缺 key / key 失效抛 LLMConfigError，不做无意义的指数退避重试；
- 429 / 5xx / 超时分开处理，带抖动退避；
- 模型返回的 qid 做归一化匹配，缺题不崩，由 runner 兜底猜；
- 搜索核实支持总时间预算，考试快结束时不会把时间花在搜索上。
"""
from __future__ import annotations

import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from openai import OpenAI

from .config import Config
from .errors import LLMConfigError, LLMError
from .parser import Option, Question
from .search import DeepSeekSearch, SearchError, SearchHit
from .modules import ModuleResolver

SYSTEM_PROMPT = """你是一位严谨的考试答题专家，精通各类学科知识。

规则：
1. 只依据题目本身作答，不要臆造题干没有的信息。
2. 单选题：返回唯一正确选项的字母。
3. 多选题：返回所有正确选项的字母，按字母顺序。
4. 判断题：按给出的选项字母作答。
5. 填空题：返回要填入的内容，多空用「|||」分隔，不要带题号。
6. 简答题：给出简洁、要点化的答案文本，200 字以内。
7. 即使不确定也要给出你认为最可能的答案，并在 confidence 里如实反映把握程度（0~1）。
8. 严格只输出 JSON，不要 markdown 代码块，不要任何解释性文字。"""

USER_TMPL = """请回答以下题目，输出 JSON：

{{
  "answers": [
    {{"qid": "<题目 qid>", "answer": ["A"], "text": "", "confidence": 0.95, "reason": "<20字内理由>"}}
  ]
}}

字段说明：
- 选择题/判断题：answer 放字母数组，text 留空字符串。
- 填空题/简答题：answer 放空数组 []，text 放答案内容。

题目列表：
{questions}"""


REFINE_SYSTEM = """你是一位严谨的考试答题专家。现在会给你一道题和若干网络搜索资料，
请结合资料重新判断这道题的答案。

要求：
1. 搜索资料可能不完整、过时，甚至与题目完全无关。要批判性地使用。
2. 资料无关、或只有模糊说法时，坚持你自己最可靠的判断，不要被带偏。
3. 只有当资料给出明确、可信、可核实的依据时，才改变结论。
4. 作答格式：选择题/判断题用 answer 字母数组；填空/简答用 text 字段。
5. 严格只输出 JSON，不要 markdown 代码块，不要额外说明。"""

REFINE_USER = """题目：{stem}

选项：
{options}

网络搜索资料：
{material}

请判断这道题，输出 JSON：
{{"answer": ["A"], "text": "", "confidence": 0.95, "reason": "20字内说明依据"}}"""


def _fmt_question(q: Question) -> str:
    lines = [f"[qid={q.qid}] 题型：{q.qtype}"]
    lines.append(f"题干：{q.stem}")
    if q.options:
        for o in q.options:
            lines.append(f"  {o.key}. {o.text}")
    if q.qtype in ("fill", "essay"):
        lines.append("（本题为填答/主观题，请用 text 字段作答）")
    return "\n".join(lines)


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start : i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        break
    raise ValueError(f"模型未返回合法 JSON：{text[:300]}")


def _status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "http_status"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    resp = getattr(exc, "response", None)
    v = getattr(resp, "status_code", None)
    return v if isinstance(v, int) else None


def _err_text(exc: BaseException) -> str:
    return f"{exc.__class__.__name__}: {exc}"[:400]


def _is_auth_error(exc: BaseException) -> bool:
    if _status_code(exc) in (401, 403):
        return True
    low = _err_text(exc).lower()
    return "invalid api key" in low or "authentication" in low or "unauthorized" in low


def _is_rate_limit(exc: BaseException) -> bool:
    if _status_code(exc) == 429:
        return True
    low = _err_text(exc).lower()
    return "rate limit" in low or "too many requests" in low


def _is_format_rejection(exc: BaseException) -> bool:
    """端点不支持 response_format 时 OpenAI SDK 会抛 400。"""
    code = _status_code(exc)
    low = _err_text(exc).lower()
    return code in (400, 422) and (
        "response_format" in low or "json_object" in low or "json mode" in low
    )


def _norm_qid(v: Any) -> str:
    s = str(v if v is not None else "").strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def _parse_answers(obj: Any) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not isinstance(obj, dict):
        return out
    items = obj.get("answers")
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        qid = _norm_qid(item.get("qid"))
        if not qid:
            continue
        ans = item.get("answer") or []
        if isinstance(ans, str):
            ans = [ans]
        if not isinstance(ans, list):
            ans = [ans]
        keys: list[str] = []
        for a in ans:
            s = str(a).strip().upper().replace(".", "")
            if s:
                keys.append(s)
        try:
            conf = float(item.get("confidence") if item.get("confidence") is not None else 0.6)
        except (TypeError, ValueError):
            conf = 0.6
        out[qid] = {
            "answer": keys,
            "text": str(item.get("text") or "").strip(),
            "confidence": max(0.0, min(1.0, conf)),
            "reason": str(item.get("reason") or "")[:120],
        }
    return out


class LLMSolver:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.offline = False
        self.client: OpenAI | None = None
        if not cfg.llm.api_key:
            if cfg.llm.allow_guess_without_key:
                self.offline = True
                print("[warn] 未配置 llm.api_key：进入兜底猜测模式（只适合 dry_run）")
            else:
                raise LLMConfigError(
                    "config.yaml: llm.api_key 未填写。\n"
                    "DeepSeek 示例：llm.base_url=https://api.deepseek.com/v1, model=deepseek-flash\n"
                    "确实只想验证流程可设 llm.allow_guess_without_key: true（不建议正式交卷）。"
                )
        else:
            self.client = OpenAI(
                base_url=cfg.llm.base_url,
                api_key=cfg.llm.api_key,
                timeout=cfg.llm.timeout,
                max_retries=0,  # 自己控制重试节奏
            )
        self._lock = threading.Lock()
        self.usage = {"prompt": 0, "completion": 0, "calls": 0}
        # 逐题核实结果回调：f(question, ok: bool, note: str)
        # GUI 用它把「第几题核实到没有」实时显示出来；CLI 不用。
        self.on_search_result = None

        # ---- 搜索（可选，失败自动降级）----
        self.searcher = None
        self._search_off = False
        self.search_stats = {
            "searched": 0,
            "revised": 0,
            "failed": 0,
            "verified": 0,
            "unverified": 0,
        }
        if cfg.llm.search_enabled and not self.offline:
            try:
                self.searcher = DeepSeekSearch(
                    base_url=cfg.llm.search_base_url or cfg.llm.base_url,
                    api_key=cfg.llm.api_key,
                    timeout=cfg.llm.search_timeout,
                    count=cfg.llm.search_count,
                    need_content=cfg.llm.search_need_content,
                    proxy=cfg.http.proxy,
                )
            except SearchError as exc:
                print(f"[warn] 搜索初始化失败，已跳过：{exc}")
                self.searcher = None

        # ---- 专项模块（题目命中关键词 → 指定权威原文重判）----
        self.modules = None
        if getattr(cfg, "modules", None) and cfg.modules.enabled and cfg.modules.rules and not self.offline:
            try:
                self.modules = ModuleResolver(
                    cfg, searcher=self.searcher, chat=self._chat, log=print
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] 专项模块初始化失败，已跳过：{exc}")
                self.modules = None

    # ------------------------------------------------------------------
    def _raw_chat(self, kw: dict) -> str:
        assert self.client is not None
        resp = self.client.chat.completions.create(**kw)
        if getattr(resp, "usage", None):
            with self._lock:
                self.usage["prompt"] += resp.usage.prompt_tokens or 0
                self.usage["completion"] += resp.usage.completion_tokens or 0
                self.usage["calls"] += 1
        try:
            return resp.choices[0].message.content or ""
        except (AttributeError, IndexError) as exc:
            raise LLMError(f"模型返回结构异常：{_err_text(exc)}") from exc

    def _chat(self, messages: list[dict], json_mode: bool = True) -> str:
        kw: dict[str, Any] = {
            "model": self.cfg.llm.model,
            "messages": messages,
            "temperature": self.cfg.llm.temperature,
        }
        if json_mode:
            kw["response_format"] = {"type": "json_object"}
        try:
            return self._raw_chat(kw)
        except Exception as exc:  # noqa: BLE001
            if json_mode and _is_format_rejection(exc):
                kw.pop("response_format", None)
                return self._raw_chat(kw)
            raise

    # ------------------------------------------------------------------
    def preflight(self) -> None:
        """正式开考前做一次极小成本的模型连通性/鉴权检查。

        单次机会考试里，模型 key 失效如果等到开考后才发现，机会就白扔了。
        这里发一道 1+1 的样例题，只为验证 base_url / api_key / model 可用。
        """
        if self.offline:
            return
        q = Question(
            qid="__preflight__",
            section_id=0,
            index=1,
            stem="单选题：1+1 等于几？",
            qtype="single",
            options=[Option("A", "1"), Option("B", "2")],
            score=0,
        )
        res = self.solve_batch([q])
        if not res:
            raise LLMConfigError("模型预检失败：没有返回有效 JSON")
        print(f"      ok 模型可用（{self.cfg.llm.model}）")

    def _backoff(self, attempt: int, rate_limited: bool) -> None:
        if rate_limited:
            delay = min(5.0 * (2 ** (attempt - 1)), 30.0)
        else:
            delay = min(2.0**attempt, 8.0)
        time.sleep(delay + random.uniform(0, 1.0))

    # ------------------------------------------------------------------
    def solve_batch(self, questions: list[Question]) -> dict[str, dict]:
        """一批题目 → {qid: {answer, text, confidence, reason}}"""
        if self.offline or self.client is None:
            raise LLMConfigError("未配置大模型 api_key，不能调用模型")

        body = "\n\n".join(_fmt_question(q) for q in questions)
        prompt = USER_TMPL.format(questions=body)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        last_err: Exception | None = None
        for attempt in range(1, self.cfg.llm.max_retries + 1):
            try:
                raw = self._chat(messages)
                obj = _extract_json(raw)
                out = _parse_answers(obj)
                if out:
                    return out
                last_err = ValueError("模型返回 answers 为空")
            except LLMConfigError:
                raise
            except Exception as exc:  # noqa: BLE001
                if _is_auth_error(exc):
                    raise LLMConfigError(
                        f"大模型鉴权失败（api_key 无效或无权限）：{_err_text(exc)}\n"
                        "请检查 llm.base_url / llm.api_key / llm.model。"
                    ) from exc
                last_err = exc
                if attempt < self.cfg.llm.max_retries:
                    self._backoff(attempt, _is_rate_limit(exc))
        raise LLMError(
            f"批量答题失败（{len(questions)} 题，已试 {self.cfg.llm.max_retries} 次）："
            f"{_err_text(last_err) if last_err else '未知错误'}"
        )

    # ------------------------------------------------------------------
    def solve_all(self, questions: list[Question], progress=None) -> None:
        """并发跑完全部题目，结果直接写回 Question 对象。

        保留给同步/离线场景；runner 在考试中改用按批调用以便逐题控制节奏。
        """
        batches = [
            questions[i : i + self.cfg.llm.batch_size]
            for i in range(0, len(questions), self.cfg.llm.batch_size)
        ]
        done = 0
        total = len(questions)
        errors: list[str] = []

        with ThreadPoolExecutor(max_workers=self.cfg.llm.concurrency) as pool:
            futs = {pool.submit(self.solve_batch, b): b for b in batches}
            for fut in as_completed(futs):
                batch = futs[fut]
                try:
                    res = fut.result()
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc))
                    for q in batch:
                        try:
                            res1 = self.solve_batch([q])
                            self.apply([q], res1)
                            done += 1
                        except Exception as exc2:  # noqa: BLE001
                            errors.append(str(exc2))
                        if progress:
                            progress(done, total)
                    continue
                self.apply(batch, res)
                done += len(batch)
                if progress:
                    progress(done, total)

        if errors:
            print(f"[warn] {len(errors)} 个批次/题目出现异常，已尽力补救")
            for e in errors[:3]:
                print(f"       - {e}")

    # ------------------------------------------------------------------
    @staticmethod
    def apply(batch: list[Question], res: dict[str, dict]) -> None:
        by_id = {_norm_qid(q.qid): q for q in batch}
        for qid, r in res.items():
            q = by_id.get(qid)
            if q is None:
                continue
            keys = [str(k).strip().upper() for k in (r.get("answer") or []) if str(k).strip()]
            text = str(r.get("text") or "").strip()
            q.answer_keys = keys
            q.answer_text = text
            q.confidence = float(r.get("confidence") or 0.0)
            q.reason = str(r.get("reason") or "")

            # 选择题维度校验：字母必须真实存在于选项里
            valid = {o.key.upper() for o in q.options}
            if q.answer_keys and valid:
                kept = [k for k in q.answer_keys if k in valid]
                q.answer_keys = kept
            if q.qtype in ("single", "judge") and len(q.answer_keys) > 1:
                q.answer_keys = q.answer_keys[:1]
            # 去重且保持选项顺序
            if q.answer_keys and q.options:
                order = {o.key.upper(): i for i, o in enumerate(q.options)}
                q.answer_keys = sorted(set(q.answer_keys), key=lambda k: order.get(k, 99))

    # 兼容旧调用
    _apply = apply

    # ============================================================ 二次校验
    def verify_answers(
        self,
        questions: list[Question],
        progress=None,
        deadline: float | None = None,
        min_conf: float | None = None,
    ) -> dict:
        """对已有答案做二次校验（staged 流程的第 2 步）。

        做法是让模型**独立再答一遍**，而不是把旧答案喂给它 —— 避免锚定效应。
        采纳规则：
          - 模型结论与原答案一致 -> 标记 verified（等于背书）
          - 不一致且置信度 >= verify_min_confidence -> 改判
          - 不一致但置信度不足 -> 保留原答案（题库通常比低置信模型可靠）
        人工改过的题（edited）直接跳过，绝不覆盖。
        """
        st = {"checked": 0, "changed": 0, "agreed": 0, "failed": 0}
        if self.offline or self.client is None:
            return st
        thr = float(
            self.cfg.llm.verify_min_confidence if min_conf is None else min_conf
        )
        cands = [
            q for q in questions if q.answered and not getattr(q, "edited", False)
        ]
        if not cands:
            return st

        size = max(1, int(self.cfg.llm.batch_size))
        batches = [cands[i : i + size] for i in range(0, len(cands), size)]
        done = 0
        total = len(cands)

        def _bump(n: int = 1) -> None:
            nonlocal done
            done += n
            if progress:
                progress(done, total)

        with ThreadPoolExecutor(max_workers=self.cfg.llm.concurrency) as pool:
            futs = {pool.submit(self.solve_batch, b): b for b in batches}
            for fut in as_completed(futs):
                batch = futs[fut]
                try:
                    res = fut.result()
                except Exception:  # noqa: BLE001
                    st["failed"] += len(batch)
                    _bump(len(batch))
                    continue

                for q in batch:
                    if deadline and time.time() >= deadline:
                        _bump()
                        continue
                    r = res.get(_norm_qid(q.qid))
                    st["checked"] += 1
                    if not isinstance(r, dict):
                        st["failed"] += 1
                        _bump()
                        continue

                    keys = [
                        str(k).strip().upper().replace(".", "")
                        for k in (r.get("answer") or [])
                        if str(k).strip()
                    ]
                    text = str(r.get("text") or "").strip()
                    try:
                        conf = float(r.get("confidence") or 0.0)
                    except (TypeError, ValueError):
                        conf = 0.0

                    valid = {o.key.upper() for o in q.options}
                    if keys and valid:
                        keys = [k for k in keys if k in valid]
                    if q.qtype in ("single", "judge") and len(keys) > 1:
                        keys = keys[:1]
                    if keys and q.options:
                        order = {o.key.upper(): i for i, o in enumerate(q.options)}
                        keys = sorted(set(keys), key=lambda k: order.get(k, 99))

                    if not keys and not text:
                        st["failed"] += 1
                        _bump()
                        continue

                    old_keys = [str(k).upper() for k in q.answer_keys]
                    same = keys == old_keys and (not text or text == q.answer_text)
                    if same:
                        q.verified = True
                        if "模型复核一致" not in (q.reason or ""):
                            q.reason = (q.reason + " | 模型复核一致").strip(" |")[:120]
                        st["agreed"] += 1
                        _bump()
                        continue

                    if conf < thr:
                        # 模型有异议但不够自信：不动原答案，留痕即可
                        note = f"模型异议 {''.join(keys) or text[:12]}@{conf:.0%}，未达阈值"
                        if note not in (q.reason or ""):
                            q.reason = (q.reason + " | " + note).strip(" |")[:160]
                        _bump()
                        continue

                    q.answer_keys = keys
                    q.answer_text = text
                    q.confidence = conf
                    q.verified = True
                    q.reason = (
                        f"[模型复核改判] {str(r.get('reason') or '')[:80]}"
                    )
                    st["changed"] += 1
                    _bump()
        return st

    # ============================================================ 搜索核实
    def apply_modules(
        self,
        questions: list[Question],
        progress=None,
        deadline: float | None = None,
    ) -> dict:
        """专项模块核实：命中规则的题目按权威原文重判（不依赖置信度）。"""
        if not self.modules:
            return {"matched": 0, "resolved": 0, "changed": 0, "failed": 0}
        return self.modules.resolve(questions, progress=progress, deadline=deadline)

    def refine_low_confidence(
        self,
        questions: list[Question],
        progress=None,
        deadline: float | None = None,
        force: bool = False,
    ) -> dict:
        """对低置信度的题目搜索外部资料，再让模型带着证据重判一次。

        只有新结论的置信度显著高于原来时才采纳 —— 搜索结果噪声很大，
        不加这道闸门反而容易把对的答案改错。

        force=True（staged 流程用）：不管置信度，凡是还没搜过的题都过一遍。
        此时是否改判仍由 _refine_one 的置信度闸门决定，题库结论不会轻易被推翻。
        """
        st = self.search_stats
        if not self.searcher or self._search_off or self.cfg.llm.search_enabled is False:
            return st

        budget_deadline = None
        if self.cfg.llm.search_max_seconds > 0:
            budget_deadline = time.time() + self.cfg.llm.search_max_seconds
        if deadline:
            budget_deadline = (
                min(budget_deadline, deadline - 60)
                if budget_deadline
                else deadline - 60
            )
        if budget_deadline and time.time() >= budget_deadline:
            return st

        thr = self.cfg.llm.search_confidence_threshold
        all_q = bool(getattr(self.cfg.llm, "search_all_questions", False))
        if force:
            # staged：题库/模型给过的答案都值得用外部资料再核一次
            cands = [
                q
                for q in questions
                if q.answered and not getattr(q, "search_checked", False)
            ]
        elif all_q:
            cands = [q for q in questions if q.answered and not getattr(q, "verified", False)]
        else:
            cands = [
                q
                for q in questions
                if q.answered
                and q.confidence < thr
                and not getattr(q, "verified", False)
            ]
        # 没核实过的排前面，其次按置信度低的优先
        cands.sort(key=lambda q: (bool(getattr(q, "verified", False)), q.confidence))
        limit = self.cfg.llm.search_max_questions
        if limit > 0:
            cands = cands[:limit]
        if not cands:
            return st

        done = 0
        total = len(cands)
        with ThreadPoolExecutor(max_workers=self.cfg.llm.concurrency) as pool:
            futs = [pool.submit(self._refine_one, q, budget_deadline) for q in cands]
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception:  # noqa: BLE001
                    pass
                done += 1
                if progress:
                    progress(done, total)
        return st

    def _refine_one(self, q: Question, deadline: float | None = None) -> None:
        if self._search_off or not self.searcher:
            return
        if deadline and time.time() >= deadline:
            _report_search(self, q, False, "时间不够，跳过")
            return

        try:
            hits = self.searcher.search(query=q.stem, ori_question=q.stem)
        except SearchError as exc:
            with self._lock:
                self.search_stats["failed"] += 1
                msg = str(exc)
                if "404" in msg or "401" in msg:
                    self._search_off = True
            print(f"[warn] 搜索失败：{exc}")
            _report_search(self, q, False, "搜索失败")
            return

        if not hits:
            with self._lock:
                self.search_stats["unverified"] += 1
            _report_search(self, q, False, "没有搜索结果")
            return

        with self._lock:
            self.search_stats["searched"] += 1

        good = [h for h in hits if h.score >= 1.0] or hits
        good = good[:6]
        if not good:
            with self._lock:
                self.search_stats["unverified"] += 1
            _report_search(self, q, False, "没有可用结果")
            return
        evidence = str(getattr(good[0], "url", "") or "")
        material = "\n\n".join(h.to_prompt(i + 1) for i, h in enumerate(good))

        opts = "\n".join(f"{o.key}. {o.text}" for o in q.options) or "（本题无选项）"
        msgs = [
            {"role": "system", "content": REFINE_SYSTEM},
            {
                "role": "user",
                "content": REFINE_USER.format(stem=q.stem, options=opts, material=material),
            },
        ]
        try:
            obj = _extract_json(self._chat(msgs))
        except Exception:  # noqa: BLE001
            _report_search(self, q, False, "模型解析失败")
            return

        ans = obj.get("answer") or []
        if isinstance(ans, str):
            ans = [ans]
        if not isinstance(ans, list):
            ans = []
        ans = [str(a).strip().upper().replace(".", "") for a in ans if str(a).strip()]
        text = str(obj.get("text") or "").strip()
        try:
            conf = float(obj.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0

        valid = {o.key.upper() for o in q.options}
        if ans and valid:
            ans = [k for k in ans if k in valid]
        if q.qtype in ("single", "judge") and len(ans) > 1:
            ans = ans[:1]

        if not ans and not text:
            _report_search(self, q, False, "没有有效结论")
            return
        if deadline and time.time() >= deadline:
            _report_search(self, q, False, "时间不够，跳过")
            return

        all_q = bool(getattr(self.cfg.llm, "search_all_questions", False))
        if all_q:
            # 全量核实：材料明确到一定程度就采信，不再要求「比原答案更自信」
            adopt = conf >= float(getattr(self.cfg.llm, "search_adopt_confidence", 0.7))
        else:
            # 低置信模式沿用原来的闸门：新结论必须明显更自信
            adopt = conf > q.confidence + 0.02

        if not adopt:
            # 结论没被采纳，但确实拿到了可读材料 —— 也算核实过
            if conf >= 0.5:
                q.verified = True
                q.search_checked = True
                if not getattr(q, "evidence", ""):
                    q.evidence = evidence
                _report_search(self, q, True, "已核实（结论不变）")
            else:
                # 资料不足：不标记，留给下一轮搜索重试
                _report_search(self, q, False, "资料不足")
            return

        changed = (ans != q.answer_keys) or (bool(text) and text != q.answer_text)
        with self._lock:
            self.search_stats["revised"] += 1
            if not getattr(q, "verified", False):
                self.search_stats["verified"] += 1
        q.answer_keys = ans
        q.answer_text = text
        q.confidence = conf
        q.verified = True
        q.search_checked = True
        q.evidence = evidence
        q.reason = "[搜索核实] " + str(obj.get("reason") or "")[:60]
        _report_search(self, q, True, "已核实并改判" if changed else "已核实")


def _report_search(solver: Any, q: Question, ok: bool, note: str) -> None:
    """把单题核实结果推给上层（GUI 状态条）。回调异常不能影响答题。"""
    cb = getattr(solver, "on_search_result", None)
    if not cb:
        return
    try:
        cb(q, bool(ok), str(note))
    except Exception:  # noqa: BLE001
        pass
