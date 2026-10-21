"""专项知识模块：把「某类题目」绑定到「权威来源」。

背景：新生入学教育这类考试，大量题目出自学校自己发布的规章制度
（例如《南通师范高等专科学校违纪处分条例》），通用模型的先验知识覆盖不到，
而平台自己的官网上就有原文。所以这里做一层「专题绑定」：

    命中关键词的题目 → 拉取指定权威页面 / DeepSeek 搜索 → 只依据原文重判

和 `refine_low_confidence` 的区别：
1. 触发条件不同 —— 模块看「题目内容是否命中规则」，不看模型置信度；
2. 依据强度不同 —— 低置信搜索是「参考资料，可能带偏」，模块是「权威原文，优先采信」；
3. 采纳门槛不同 —— 模块只要模型在原文基础上给出条目明确的答案，就覆盖原判。

配置示例（config.yaml）：

    modules:
      enabled: true
      rules:
        - name: 违纪处分条例
          keywords: ["违纪处分条例", "违纪处分", "处分条例", "学生违纪"]
          urls:
            - https://xgc.ntnc.edu.cn/2018/1023/c609a18764/page.htm
          query: 南通师范高等专科学校 学生违纪处分条例
"""
from __future__ import annotations

import hashlib
import html as _html
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from .config import DATA_DIR

# ------------------------------------------------------------------ 提示词
MODULE_SYSTEM = """你是考试命题依据核对助手。会给你一道题和一份「权威原文」，
原文来自学校官方发布的规章制度。请只依据这份原文判断题目答案。

要求：
1. 原文是唯一依据。原文没有写到的内容不要臆测，confidence 必须给低值。
2. 原文与你的常识冲突时，以原文为准，并在 reason 里指出依据的条款或原句。
3. 选择题/判断题：answer 用选项字母数组（如 ["A"] 或 ["A","C"]）；填空题/简答题：用 text 字段。
4. confidence 表示「原文能支持这个答案」的程度，0~1。
5. 严格只输出 JSON，不要 markdown 代码块，不要额外说明。"""

MODULE_USER = """题目：{stem}

选项：
{options}

权威原文（{name}）：
{material}

请依据原文作答，输出 JSON：
{{"answer": ["A"], "text": "", "confidence": 0.9, "reason": "依据第几条/哪句原文"}}"""

TAG_BLOCK = re.compile(r"(?is)<(script|style|noscript|iframe|svg)[^>]*>.*?</\1>")
BR = re.compile(r"(?i)<br\s*/?>")
BLOCK_END = re.compile(r"(?i)</(p|div|tr|td|li|h[1-6]|table|section|article|dd|dt)>")
ANY_TAG = re.compile(r"(?s)<[^>]+>")
SPACES = re.compile(r"[ \t\u00a0\u3000]+")
BLANKS = re.compile(r"\n\s*\n\s*\n+")


def strip_html(raw: str) -> str:
    """把 HTML 压成纯文本。不依赖 bs4，够读就行。"""
    if not raw:
        return ""
    s = TAG_BLOCK.sub(" ", raw)
    s = BR.sub("\n", s)
    s = BLOCK_END.sub("\n", s)
    s = ANY_TAG.sub(" ", s)
    s = _html.unescape(s)
    s = SPACES.sub(" ", s)
    s = "\n".join(line.strip() for line in s.splitlines())
    s = BLANKS.sub("\n\n", s)
    return s.strip()


def _decode(raw: bytes, declared: str | None = None) -> str:
    """学校站点 GBK/GB2312 很常见，这里逐个试，别信 header。"""
    for enc in (declared, "utf-8", "gb18030", "gbk"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


@dataclass
class ModuleRule:
    name: str
    keywords: tuple[str, ...] = ()
    urls: tuple[str, ...] = ()
    query: str = ""

    def match(self, text: str) -> bool:
        t = text or ""
        return any(k and k in t for k in self.keywords)

    @property
    def site(self) -> str:
        if not self.urls:
            return ""
        return urlparse(self.urls[0]).netloc.lower()


@dataclass
class ModuleStats:
    matched: int = 0
    resolved: int = 0
    changed: int = 0
    failed: int = 0
    hits: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "matched": self.matched,
            "resolved": self.resolved,
            "changed": self.changed,
            "failed": self.failed,
        }


class ModuleResolver:
    """规则匹配 + 原文抓取 + 依据原文重判。"""

    def __init__(
        self,
        cfg: Any,
        *,
        searcher: Any = None,
        chat: Callable[[list[dict]], str] | None = None,
        cache_dir: Path | None = None,
        log: Callable[[str], None] = print,
    ):
        self.cfg = cfg
        m = cfg.modules
        self.rules = [
            ModuleRule(
                name=str(r.get("name") or "未命名规则"),
                keywords=tuple(str(k) for k in (r.get("keywords") or []) if str(k).strip()),
                urls=tuple(str(u) for u in (r.get("urls") or []) if str(u).strip()),
                query=str(r.get("query") or "").strip(),
            )
            for r in (m.rules or [])
        ]
        self.enabled = bool(m.enabled) and bool(self.rules)
        self.searcher = searcher
        self.chat = chat
        self.cache_dir = cache_dir or (DATA_DIR / "module_cache")
        self.ttl = max(1, int(m.cache_ttl_hours)) * 3600
        self.max_chars = max(500, int(m.max_material_chars))
        self.min_conf = float(m.min_confidence)
        self.max_q = int(m.max_questions)
        self.timeout = int(m.fetch_timeout)
        self.proxy = getattr(getattr(cfg, "http", None), "proxy", None)
        self.log = log
        self.stats = ModuleStats()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 匹配
    def match(self, text: str) -> ModuleRule | None:
        for rule in self.rules:
            if rule.match(text):
                return rule
        return None

    # ------------------------------------------------------------------ 取原文
    def _cache_path(self, url: str) -> Path:
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / f"{key}.txt"

    def fetch_page(self, url: str, *, force: bool = False) -> str:
        """抓一页并存缓存。失败返回空串（模块降级，不影响主流程）。"""
        path = self._cache_path(url)
        try:
            if not force and path.exists() and time.time() - path.stat().st_mtime < self.ttl:
                return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass

        kw: dict[str, Any] = {
            "timeout": self.timeout,
            "follow_redirects": True,
            "headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        }
        if self.proxy:
            kw["proxy"] = self.proxy
        try:
            with httpx.Client(**kw) as client:
                r = client.get(url)
            if r.status_code >= 400:
                self.log(f"[warn] 模块原文抓取 HTTP {r.status_code}：{url}")
                return ""
            text = strip_html(_decode(r.content, r.encoding))
        except Exception as exc:  # noqa: BLE001
            self.log(f"[warn] 模块原文抓取失败：{url}（{exc}）")
            return ""

        if not text:
            return ""
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError:
            pass
        return text

    def _search_material(self, rule: ModuleRule, stem: str) -> list[str]:
        """走 DeepSeek /tools/search，优先挑规则指定站点/URL 的结果。

        实测：直接搜「学校名 + 条例名」会被其它学校的同名条例挤满，
        加上 `site:<host>` 限定后能稳定命中本校页面。所以有 urls 时一律带 site:。
        """
        if not self.searcher:
            return []

        base = rule.query or rule.name
        site = rule.site
        queries = []
        if site:
            queries.append(f"site:{site} {base}")
        queries.append(base)

        hits: list[Any] = []
        for q in queries:
            q = " ".join(q.split())[:128]
            try:
                hits = self.searcher.search(query=q, ori_question=stem)
            except Exception as exc:  # noqa: BLE001
                self.log(f"[warn] 模块搜索失败：{exc}")
                return []
            if hits:
                break
        if not hits:
            return []

        wanted = {u.rstrip("/") for u in rule.urls}
        site = rule.site

        def rank(h: Any) -> tuple:
            url = (getattr(h, "url", "") or "").rstrip("/")
            exact = 0 if url in wanted else 1
            same_site = 0 if (site and site in url.lower()) else 1
            return (exact, same_site, -float(getattr(h, "score", 0) or 0))

        ordered = sorted(hits, key=rank)
        out: list[str] = []
        for i, h in enumerate(ordered[:4]):
            out.append(h.to_prompt(i + 1))
        return out

    def material_for(self, rule: ModuleRule, stem: str) -> str:
        """搜索资料 + 官网原文，凑成给模型看的材料。"""
        parts: list[str] = []
        parts.extend(self._search_material(rule, stem))
        for url in rule.urls:
            text = self.fetch_page(url)
            if not text:
                continue
            body = text[: self.max_chars]
            if len(text) > self.max_chars:
                body += "\n…（原文过长，已截断）"
            parts.append(f"[原文] {rule.name}（{url}）\n{body}")
        return "\n\n".join(p.strip() for p in parts if p.strip())

    # ------------------------------------------------------------------ 重判
    def resolve(
        self,
        questions: list[Any],
        *,
        progress: Callable[[int, int], None] | None = None,
        deadline: float | None = None,
    ) -> dict:
        if not self.enabled or not self.chat:
            return self.stats.as_dict()

        cands: list[tuple[Any, ModuleRule]] = []
        for q in questions:
            if not q.stem:
                continue
            text = q.stem + " " + " ".join(getattr(o, "text", "") for o in q.options)
            rule = self.match(text)
            if rule:
                cands.append((q, rule))

        self.stats.matched = len(cands)
        if not cands:
            return self.stats.as_dict()

        # 命中太多时，优先核最没把握的题
        cands.sort(key=lambda pair: pair[0].confidence)
        if self.max_q > 0:
            cands = cands[: self.max_q]

        total = len(cands)
        done = 0
        workers = max(1, min(4, getattr(self.cfg.llm, "concurrency", 3)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(self._refine_one, q, rule, deadline): (q, rule)
                for q, rule in cands
            }
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception:  # noqa: BLE001
                    with self._lock:
                        self.stats.failed += 1
                done += 1
                if progress:
                    progress(done, total)
        return self.stats.as_dict()

    def _refine_one(self, q: Any, rule: ModuleRule, deadline: float | None) -> None:
        if deadline and time.time() >= deadline:
            return
        material = self.material_for(rule, q.stem)
        if not material.strip():
            with self._lock:
                self.stats.failed += 1
            self.log(f"      [模块] {q.stem[:18]}… 命中「{rule.name}」但没取到原文，跳过")
            return

        opts = "\n".join(f"{o.key}. {o.text}" for o in q.options) or "（本题无选项）"
        msgs = [
            {"role": "system", "content": MODULE_SYSTEM},
            {
                "role": "user",
                "content": MODULE_USER.format(
                    stem=q.stem, options=opts, material=material, name=rule.name
                ),
            },
        ]
        try:
            obj = _extract_json(self.chat(msgs))
        except Exception:  # noqa: BLE001
            with self._lock:
                self.stats.failed += 1
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
            with self._lock:
                self.stats.failed += 1
            return
        with self._lock:
            self.stats.resolved += 1

        # 权威原文优先：只要模型给出了条目明确、且不是瞎猜的答案就采纳
        if conf < self.min_conf:
            return

        changed = (ans != q.answer_keys) or (bool(text) and text != q.answer_text)
        if changed:
            with self._lock:
                self.stats.changed += 1
                self.stats.hits.append(f"{rule.name}: {q.stem[:20]}")
        q.answer_keys = ans or q.answer_keys
        if text:
            q.answer_text = text
        q.confidence = max(conf, q.confidence)
        q.verified = True
        q.evidence = rule.urls[0] if rule.urls else f"模块:{rule.name}"
        q.reason = f"[模块:{rule.name}] " + str(obj.get("reason") or "")[:60]


def _extract_json(raw: str) -> dict:
    """容忍 ```json 包裹 / 前后闲聊，取第一个 JSON 对象。"""
    import json

    s = (raw or "").strip()
    if not s:
        return {}
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        pass
    s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.M).strip()
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        try:
            obj = json.loads(s[i : j + 1])
            return obj if isinstance(obj, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}
