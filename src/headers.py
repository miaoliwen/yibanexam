"""请求头构造：让纯 HTTP 通道的指纹尽量自洽。

对照真实 Chrome：UA 声明 Chrome/149，就必须同时带 sec-ch-ua / sec-fetch-* /
Accept-Encoding 等头。缺这些头比 UA 写错更容易被识别（真实浏览器不会缺）。
"""
from __future__ import annotations

import re

_DEFAULT_MAJOR = "149"


def ua_major(ua: str) -> str:
    m = re.search(r"(?:Chrome|Chromium|Edg)/(\d+)", ua or "")
    return m.group(1) if m else _DEFAULT_MAJOR


def is_edge(ua: str) -> bool:
    return "Edg/" in (ua or "")


def sec_ch_ua(ua: str) -> str:
    major = ua_major(ua)
    if is_edge(ua):
        return (
            f'"Microsoft Edge";v="{major}", "Chromium";v="{major}", '
            '"Not.A/Brand";v="24"'
        )
    return (
        f'"Google Chrome";v="{major}", "Chromium";v="{major}", '
        '"Not.A/Brand";v="24"'
    )


def accept_encoding() -> str:
    """只声明本机真的能解的编码，否则服务端回了 br/zstd 会解析失败。"""
    enc = "gzip, deflate"
    try:  # noqa: SIM105 - 缺 brotli 是正常情况
        import brotli  # noqa: F401

        enc += ", br"
    except Exception:
        pass
    try:
        from urllib3.response import HTTPResponse

        if "zstd" in getattr(HTTPResponse, "CONTENT_DECODERS", []):
            enc += ", zstd"
    except Exception:
        pass
    return enc


def ajax_headers(
    ua: str,
    *,
    origin: str | None = None,
    referer: str,
    content_type: str | None = None,
    site: str = "same-site",
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """fetch/XHR 类请求头（考试后端接口）。

    GET 不发送 Origin（浏览器对跨域简单 GET 不带 Origin），POST 才带。
    """
    h = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": accept_encoding(),
        "User-Agent": ua,
        "Referer": referer,
        "sec-ch-ua": sec_ch_ua(ua),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-site": site,
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }
    if origin:
        h["Origin"] = origin
    if content_type:
        h["Content-Type"] = content_type
    if extra:
        h.update({str(k): str(v) for k, v in extra.items()})
    return h


def navigation_headers(
    ua: str,
    *,
    referer: str | None = None,
    origin: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """页面/表单类请求头（登录页）。"""
    h = {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": accept_encoding(),
        "User-Agent": ua,
        "Upgrade-Insecure-Requests": "1",
        "sec-ch-ua": sec_ch_ua(ua),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "navigate",
        "sec-fetch-dest": "document",
        "sec-fetch-user": "?1",
    }
    if referer:
        h["Referer"] = referer
    if origin:
        h["Origin"] = origin
    if extra:
        h.update({str(k): str(v) for k, v in extra.items()})
    return h
