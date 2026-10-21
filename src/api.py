"""yooc 考试接口客户端。

接口来自前端 bundle 反解，全部挂在 https://exambackend.yooc.me/ 下，
鉴权靠 query 参数：userId / token / yibanId（groupId 视接口而定）。

v2 变化
-------
1. **写操作走浏览器页面内 fetch**（session.page_fetch）：start / submit / repeat
   是最有检测价值的请求，交给真实浏览器网络栈发（真实 TLS/HTTP2/风控 SDK 环境）。
   浏览器起不来时按配置回退纯 HTTP。
2. 读请求带重试（网络抖动/5xx），写请求**不自动重试** —— 超时后服务端可能已经生效，
   盲目重发可能重复开考/重复交卷。写请求的幂等由 runner 统一核实。
3. 完整 cookie 复用，请求头补齐 sec-ch-ua / sec-fetch-* / Accept-Encoding。
4. 错误分类（AuthError / RiskControlError / TransientError / ApiError），
   登录态失效时可通过 on_auth_error 回调自动续期一次。
"""
from __future__ import annotations

import random
import time
from typing import Any, Callable
from urllib.parse import urlencode

import requests

from .config import Config
from .errors import (
    ApiError,
    AuthError,
    RiskControlError,
    SessionError,
    TransientError,
)
from .headers import ajax_headers

BACKEND = "https://exambackend.yooc.me"

# 真实前端每个接口的 query 参数白名单与顺序（index.js 里 `X("api/...")` 模板）
QUERY_SPEC: dict[str, tuple[str, ...]] = {
    "api/exam/user/check": ("groupId", "userId", "token", "yibanId"),
    "api/exam/list/get": ("userId", "token", "yibanId", "groupId"),
    "api/exam/detail/get": ("userId", "token", "yibanId", "examId"),
    "api/exam/result/get": ("userId", "token", "yibanId", "examId"),
    "api/exam/setting/get": ("examId", "userId", "token", "yibanId"),
    "api/exam/paper/get": ("examuserId", "token", "yibanId"),
    "api/exam/answer/get": ("examuserId", "token", "yibanId"),
    "api/exam/start/action": ("token", "yibanId"),
    "api/exam/submit/action": ("token", "yibanId"),
    "api/exam/repeat/action": ("token", "yibanId"),
    "api/shumei/captcha/check": ("token", "yibanId"),
}

# 有副作用的写接口，优先走浏览器通道（见 session.page_fetch）
WRITE_PATHS = (
    "api/exam/start/action",
    "api/exam/submit/action",
    "api/exam/repeat/action",
)

_RISK_WORDS = ("频繁", "限制", "风控", "验证码", "验证", "异常操作", "稍后", "too many")


def _looks_like_risk(msg: str) -> bool:
    low = (msg or "").lower()
    return any(w in low for w in _RISK_WORDS)


_NAV_HINTS = (
    "execution context was destroyed",
    "most likely because of a navigation",
    "execution context is not available",
    "frame was detached",
    "cannot find context",
)


def _is_nav_error(exc: BaseException) -> bool:
    """判断是不是「页面正在导航」造成的瞬时失效。

    这类错误不是通道坏了：等页面 load 完再来一次就能成功。
    只有真正的通道故障才值得停用页面通道。
    """
    msg = str(exc).lower()
    return any(h in msg for h in _NAV_HINTS)


class YoocApi:
    def __init__(
        self,
        cfg: Config,
        creds: dict[str, str],
        debug: bool = False,
        *,
        session: Any | None = None,
        cookies: list[dict] | None = None,
        on_auth_error: Callable[[], dict[str, str] | None] | None = None,
    ):
        self.cfg = cfg
        self.debug = debug
        self.group_id = cfg.account.group_id
        self.user_id = str(creds.get("user_id", "") or "")
        self.token = str(creds.get("user_token", "") or "")
        self.yiban_id = str(creds.get("yiban_id", "") or "")
        self._sess = session
        self._on_auth_error = on_auth_error
        self._refreshing = False
        self._fetcher = getattr(session, "page_fetch", None) if session else None
        self._page_disabled = False
        # 页面通道连续失败计数：偶发的导航抖动不该永久关闭页面通道，
        # 只有连续失败到阈值才判定为真的不可用（见 _request）。
        self._page_fail_streak = 0
        self._page_fail_limit = 3

        self.s = requests.Session()
        self.s.headers.update(
            ajax_headers(
                cfg.http.user_agent,
                referer=f"https://exam.yooc.me/group/{self.group_id}/exams",
                extra=cfg.http.extra_headers,
            )
        )
        if cfg.http.proxy:
            self.s.proxies = {"http": cfg.http.proxy, "https": cfg.http.proxy}
        if cookies:
            self.load_cookies(cookies)

    # ------------------------------------------------------------------ 凭据
    def update_creds(self, creds: dict[str, str]) -> None:
        self.user_id = str(creds.get("user_id", "") or "")
        self.token = str(creds.get("user_token", "") or "")
        self.yiban_id = str(creds.get("yiban_id", "") or "")

    def load_cookies(self, cookies: list[dict]) -> int:
        """把保存的 cookie jar 灌回 Session，保持跨域请求特征一致。"""
        n = 0
        for c in cookies or []:
            try:
                name = str(c.get("name") or "")
                if not name:
                    continue
                ck = requests.cookies.create_cookie(
                    name=name,
                    value=str(c.get("value") or ""),
                    domain=str(c.get("domain") or ""),
                    path=str(c.get("path") or "/"),
                    secure=bool(c.get("secure")),
                )
                self.s.cookies.set_cookie(ck)
                n += 1
            except Exception:
                continue
        return n

    # ------------------------------------------------------------------ 底层
    def _auth_qs(self) -> str:
        parts = []
        if self.user_id:
            parts.append(("userId", self.user_id))
        if self.token:
            parts.append(("token", self.token))
        if self.yiban_id:
            parts.append(("yibanId", self.yiban_id))
        return urlencode(parts)

    @staticmethod
    def _num(value: Any) -> Any:
        """数字 ID 还原成 int：前端 FormData 里是 JSON.stringify(Number) 的十进制串。"""
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value
        s = str(value or "").strip()
        if s.isdigit():
            return int(s)
        return value

    def _rel(self, path: str, params: dict[str, Any] | None = None) -> str:
        rel = path.lstrip("/")
        values: dict[str, Any] = {
            "userId": self.user_id,
            "token": self.token,
            "yibanId": self.yiban_id,
            "groupId": self.group_id,
        }
        if params:
            values.update(params)
        spec = QUERY_SPEC.get(rel)
        pairs: list[tuple[str, Any]] = []
        if spec:
            for key in spec:
                v = values.get(key)
                if v not in (None, ""):
                    pairs.append((key, self._num(v) if key in ("examId", "examuserId", "groupId", "userId") else v))
        else:
            if params:
                pairs.extend((k, v) for k, v in params.items() if v is not None)
            pairs.extend(
                [
                    ("userId", self.user_id),
                    ("token", self.token),
                    ("yibanId", self.yiban_id),
                ]
            )
        qs = urlencode([(k, v) for k, v in pairs if v not in (None, "")])
        return f"{rel}?{qs}" if qs else rel

    def _url(self, path: str, params: dict[str, Any] | None = None) -> str:
        return f"{BACKEND}/{self._rel(path, params)}"

    def _timeout(self) -> tuple[int, int]:
        t = max(5, int(self.cfg.http.timeout))
        return (min(10, t), t)

    def _use_page(self, path: str, is_write: bool = False) -> bool:
        mode = (self.cfg.account.browser_for_writes or "auto").lower()
        if not (self._fetcher and self._sess is not None and mode != "never"):
            return False
        # 读通道降级只影响读；写操作必须继续尝试页面通道（否则 start/submit 会静默变回 JSON）
        if is_write:
            return True
        if self._page_disabled:
            return False
        return self._page_fail_streak < self._page_fail_limit

    # ------------------------------------------------------------------ 错误
    def _check_payload(self, data: Any, path: str) -> Any:
        if isinstance(data, dict):
            code = data.get("code")
            if code == 403:
                raise AuthError(f"{path}: 接口返回 code=403，登录态失效")
            if data.get("result") is False or data.get("success") is False:
                msg = str(
                    data.get("message")
                    or data.get("msg")
                    or data.get("error")
                    or "接口返回失败"
                )
                if _looks_like_risk(msg):
                    raise RiskControlError(f"{path}: {msg}")
                raise ApiError(f"{path}: {msg}")
        return data

    def _handle(self, resp: requests.Response, path: str) -> Any:
        if self.debug:
            print(f"[http] {path} -> {resp.status_code} {resp.text[:400]}")
        status = resp.status_code
        if status in (401, 403):
            raise AuthError(f"{path}: HTTP {status}，登录态可能已过期")
        if status == 429:
            retry_after = resp.headers.get("Retry-After", "")
            raise RiskControlError(
                f"{path}: HTTP 429 触发限流"
                + (f"（Retry-After={retry_after}s）" if retry_after else "")
            )
        if status >= 500:
            raise TransientError(f"{path}: HTTP {status} 服务端暂时不可用")
        if status >= 400:
            raise ApiError(f"{path}: HTTP {status} {resp.text[:200]}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ApiError(f"{path} 返回非 JSON：{resp.text[:200]}") from exc
        return self._check_payload(payload, path)

    # ------------------------------------------------------------------ 传输
    def _via_page(self, rel: str, method: str, body: Any, path: str) -> Any:
        try:
            status, data = self._fetcher(rel, method, body)
        except SessionError as exc:
            if _looks_like_risk(str(exc)) or "登录" in str(exc) or "会话" in str(exc):
                raise AuthError(f"{path}: {exc}") from exc
            raise

        if status in (401, 403):
            raise AuthError(f"{path}: 浏览器内请求 HTTP {status}")
        if status == -1:
            # fetch 本身失败：写操作结果未知，绝不自动换通道重发
            msg = ""
            if isinstance(data, dict):
                msg = str(data.get("__error") or "")
            err = TransientError(f"{path}: 页面内请求失败（{msg or '网络错误'}），结果未知")
            setattr(err, "ambiguous_write", True)
            raise err
        if status == 0:
            raise TransientError(f"{path}: 页面内请求没有响应")
        if status >= 500:
            raise TransientError(f"{path}: 浏览器内请求 HTTP {status}")
        if status >= 400:
            raise ApiError(f"{path}: 浏览器内请求 HTTP {status} {str(data)[:200]}")
        return self._check_payload(data, path)

    def _http(self, rel: str, method: str, body: Any, path: str) -> Any:
        is_get = method == "GET"
        attempts = 1 + (self.cfg.http.retries if is_get else 0)
        last_exc: Exception | None = None
        for i in range(attempts):
            try:
                if is_get:
                    resp = self.s.get(f"{BACKEND}/{rel}", timeout=self._timeout())
                else:
                    resp = self.s.post(
                        f"{BACKEND}/{rel}",
                        json=body,
                        timeout=self._timeout(),
                        headers={"Origin": "https://exam.yooc.me"},
                    )
            except requests.RequestException as exc:
                last_exc = exc
                if i + 1 < attempts:
                    self._backoff(i)
                    continue
                raise TransientError(f"{path}: 网络失败（{exc.__class__.__name__}）") from exc
            if resp.status_code >= 500 and i + 1 < attempts:
                self._backoff(i)
                continue
            return self._handle(resp, path)
        raise TransientError(f"{path}: 网络失败（{last_exc}）")

    def _backoff(self, attempt: int) -> None:
        base = max(0.0, float(self.cfg.http.backoff_seconds))
        delay = min(base * (2**attempt), 15.0)
        time.sleep(delay + random.uniform(0, 0.4))

    def _request(
        self, path: str, params: dict[str, Any] | None, method: str, body: Any
    ) -> Any:
        rel = self._rel(path, params)
        is_write = method != "GET" or path.lstrip("/") in WRITE_PATHS
        nav_retried = False
        while True:
            if not self._use_page(path, is_write):
                break
            try:
                out = self._via_page(rel, method, body, path)
                self._page_fail_streak = 0  # 页面通道恢复正常
                return out
            except (AuthError, RiskControlError, ApiError):
                raise
            except Exception as exc:  # noqa: BLE001
                if is_write:
                    # 写操作绝不重试：请求可能已经到达服务端，重发有副作用
                    if isinstance(exc, TransientError):
                        raise
                    raise TransientError(f"{path}: 页面通道异常 {exc}") from exc
                # 读请求遇到「页面正在导航」：等页面稳定后重试一次。
                # 这类错误是瞬时的，不该记成通道故障，更不该永久降级。
                if not nav_retried and _is_nav_error(exc):
                    nav_retried = True
                    time.sleep(0.5)
                    continue
                # 真正的通道故障：本次降级到 HTTP；连续失败到阈值才停用页面通道，
                # 否则一次抖动就会让整场考试都退回纯 HTTP 指纹。
                self._page_fail_streak += 1
                if self._page_fail_streak >= self._page_fail_limit:
                    self._page_disabled = True
                    print(
                        f"[warn] 页面读通道连续 {self._page_fail_streak} 次失败，"
                        f"本次运行改用纯 HTTP：{exc}"
                    )
                else:
                    print(
                        f"[warn] 页面读通道失败（{self._page_fail_streak}/"
                        f"{self._page_fail_limit}），本次改用纯 HTTP：{exc}"
                    )
                break
        return self._http(rel, method, body, path)

    def _call(self, fn):
        try:
            return fn()
        except AuthError:
            if not self._on_auth_error or self._refreshing:
                raise
            self._refreshing = True
            try:
                creds = self._on_auth_error()
            finally:
                self._refreshing = False
            if not creds:
                raise
            self.update_creds(creds)
            return fn()

    # ------------------------------------------------------------------ 接口
    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._call(lambda: self._request(path, params, "GET", None))

    def post(self, path: str, body: Any) -> Any:
        # start/submit 一律不自动重试：超时后服务端可能已生效，重发有副作用。
        # 写请求的幂等由 runner 通过回查服务端状态统一处理。
        return self._call(lambda: self._request(path, None, "POST", body))

    # ------------------------------------------------------------------ 业务
    def user_check(self) -> Any:
        return self.get("api/exam/user/check", {"groupId": self.group_id})

    def exam_list(self) -> Any:
        return self.get("api/exam/list/get", {"groupId": self.group_id})

    def exam_detail(self, exam_id: Any) -> Any:
        return self.get("api/exam/detail/get", {"examId": exam_id})

    def exam_setting(self, exam_id: Any) -> Any:
        return self.get("api/exam/setting/get", {"examId": exam_id})

    def get_paper(self, examuser_id: Any) -> Any:
        return self.get("api/exam/paper/get", {"examuserId": examuser_id})

    def get_saved_answers(self, examuser_id: Any) -> Any:
        """读已保存答案。同时作为考试期间的心跳 + 登录态探测。"""
        return self.get("api/exam/answer/get", {"examuserId": examuser_id})

    def get_result(self, exam_id: Any) -> Any:
        return self.get("api/exam/result/get", {"examId": exam_id})

    # ---- 写操作 ----
    def start_exam(self, exam_id: Any, user_id: Any = None) -> Any:
        """开考，创建作答记录。

        前端实际提交体是 {userId: Number(userId), examId}，两个都要带；
        URL 只带 token / yibanId，userId 走 body。
        """
        body: dict[str, Any] = {"userId": self._num(user_id or self.user_id), "examId": self._num(exam_id)}
        if body["userId"] in (None, ""):
            body.pop("userId")
        return self.post("api/exam/start/action", body)

    def submit_exam(self, body: dict) -> Any:
        """交卷。body = {examuserId, score, answer}"""
        fixed = dict(body)
        if "examuserId" in fixed:
            fixed["examuserId"] = self._num(fixed["examuserId"])
        return self.post("api/exam/submit/action", fixed)

    def repeat_exam(self, exam_id: Any) -> Any:
        return self.post("api/exam/repeat/action", {"examId": exam_id})
