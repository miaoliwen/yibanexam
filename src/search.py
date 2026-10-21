"""DeepSeek 网页搜索 API 客户端。

文档：https://api-docs.deepseek.com/zh-cn/api/search

    POST {base_url}/tools/search
    body: {query, count, ori_question, need_content, localization}
    返回: {results: [{rerank_score, title, url, snippet, summary, content,
                      site_name, site_icon, date, authority}]}

用途：给低置信度的题目补充外部资料，让模型带着证据再判断一次。
搜索本身可能有噪声，所以只作为「参考材料」交给模型，最终仍由模型决定。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class SearchError(RuntimeError):
    pass


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str
    summary: str
    score: float = 0.0        # rerank_score，>=1.5 算优质
    authority: int = -1       # 0~4，>=3 极权威
    site_name: str = ""
    date: str = ""

    def to_prompt(self, idx: int) -> str:
        """压成给模型看的一段，优先用 summary（600~800 字，信息量比 snippet 大）。"""
        body = (self.summary or self.snippet or "").strip()
        if len(body) > 700:
            body = body[:700] + "…"
        src = self.site_name or self.url
        head = f"[{idx}] {self.title}"
        if src:
            head += f"（{src}）"
        return f"{head}\n{body}"


class DeepSeekSearch:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: int = 30,
        count: int = 8,
        need_content: bool = False,
        proxy: str | None = None,
    ):
        if not api_key:
            raise SearchError("未配置 api_key，无法使用搜索")
        self.url = base_url.rstrip("/") + "/tools/search"
        self.api_key = api_key
        self.timeout = timeout
        self.count = max(1, min(50, count))
        self.need_content = need_content
        self.proxy = proxy
        self.calls = 0

    # ------------------------------------------------------------------
    def search(self, query: str, ori_question: str | None = None) -> list[SearchHit]:
        q = " ".join((query or "").split())
        if not q:
            return []
        # API 限制 1~128 字符
        q = q[:128]

        body: dict[str, Any] = {
            "query": q,
            "count": self.count,
            "need_content": self.need_content,
        }
        if ori_question:
            body["ori_question"] = " ".join(ori_question.split())[:128]

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        kw: dict[str, Any] = {"timeout": self.timeout}
        if self.proxy:
            kw["proxy"] = self.proxy

        try:
            with httpx.Client(**kw) as client:
                r = client.post(self.url, json=body, headers=headers)
        except Exception as exc:  # noqa: BLE001
            raise SearchError(f"搜索请求失败：{exc}") from exc

        self.calls += 1

        if r.status_code == 404:
            raise SearchError(
                f"搜索接口不存在（404）：{self.url}\n"
                "可能当前 base_url 不支持 /tools/search（中转站常见），"
                "可以关掉 llm.search_enabled 或换官方端点。"
            )
        if r.status_code == 401:
            raise SearchError("搜索鉴权失败（401）：api_key 无效")
        if r.status_code == 429:
            raise SearchError("搜索触发限流（429），稍后再试")
        if r.status_code >= 400:
            raise SearchError(f"搜索返回 HTTP {r.status_code}: {r.text[:200]}")

        try:
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            raise SearchError(f"搜索返回非 JSON：{r.text[:200]}") from exc

        hits: list[SearchHit] = []
        for it in (data.get("results") or []):
            if not isinstance(it, dict):
                continue
            hits.append(
                SearchHit(
                    title=str(it.get("title") or ""),
                    url=str(it.get("url") or ""),
                    snippet=str(it.get("snippet") or ""),
                    summary=str(it.get("summary") or ""),
                    score=float(it.get("rerank_score") or 0),
                    authority=int(it.get("authority") if it.get("authority") is not None else -1),
                    site_name=str(it.get("site_name") or ""),
                    date=str(it.get("date") or ""),
                )
            )
        return hits
