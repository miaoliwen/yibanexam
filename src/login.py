"""纯 HTTP 账号密码登录 + 凭据持久化 + 本地登录熔断。

实测结论（2026-10-18）
---------------------
1. 登录表单是 Django 的 `POST /mobile/login`，字段：
   csrfmiddlewaretoken / username / password / rid / captcha_type / remember / yooc_submit
2. **后端不校验数美验证码**：`rid` 和 `captcha_type` 传空字符串即可登录成功。
3. 登录成功后凭据落在 cookie 里：`user_token` / `yiban_id` / `user_id`，
   同时前端会同步到 localStorage。

v2 变化：
- 保存**完整 cookie jar**（以前只存三个 query 参数，丢掉了 sessionid/csrftoken，
  请求特征与真人浏览器不一致）；
- 本地登录熔断：短时间多次登录直接拒绝，避免再把真实账号打到平台限频
  （上一轮就是半小时登录十几次，账号被临时限制）；
- 失败按 kind 分类，调用方可以决定重试/换浏览器/直接放弃。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import requests

from .config import CONFIG_PATH, DATA_DIR, ROOT, Config, paths_for, peek_account_key
from .errors import LoginError
from .headers import ajax_headers, navigation_headers
from .state import atomic_write_text

BASE = "https://www.yooc.me"
LOGIN_URL = f"{BASE}/mobile/login"
BACKEND = "https://exambackend.yooc.me"
SESSION_PATH = DATA_DIR / "session.json"
GUARD_PATH = DATA_DIR / "login_guard.json"
# 记住模块加载时的默认值：session_path() 用它判断 SESSION_PATH 是否被测试 patch
_LEGACY_SESSION_PATH = SESSION_PATH

__all__ = [
    "LoginError",
    "password_login",
    "load_saved",
    "load_saved_cookies",
    "save_session",
    "validate_saved",
    "LoginGuard",
    "SESSION_PATH",
]


# ============================================================ 熔断
class LoginGuard:
    """本地登录熔断器：窗口期内尝试次数上限 + 最小间隔。

    为什么需要：平台的限频提示是笼统的"密码不正确"，看不出是密码错还是被限。
    与其靠猜，不如客户端自己先限速 —— 真实账号只有一次考试机会，值得保守。
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        cfg: Config | None = None,
        max_attempts: int = 6,
        window_seconds: int = 600,
        min_interval_seconds: int = 8,
        clock=time.time,
    ):
        if path:
            self.path = Path(path)
        elif cfg is not None:
            # 熔断计数按账号隔离：A 账号被限频不能连带拦住 B 账号
            self.path = cfg.paths.guard
        else:
            self.path = paths_for(peek_account_key()).guard
        self.max_attempts = max(1, int(max_attempts))
        self.window = max(1, int(window_seconds))
        self.min_interval = max(0, int(min_interval_seconds))
        self.clock = clock

    def _read(self) -> list[float]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return []
        if isinstance(data, dict):
            data = data.get("attempts")
        if not isinstance(data, list):
            return []
        out: list[float] = []
        for v in data:
            try:
                out.append(float(v))
            except (TypeError, ValueError):
                continue
        return out

    def precheck(self) -> None:
        now = self.clock()
        attempts = [t for t in self._read() if now - t < self.window]
        if len(attempts) >= self.max_attempts:
            wait = int(self.window - (now - min(attempts))) + 1
            raise LoginError(
                f"最近 {self.window // 60} 分钟内已尝试登录 {len(attempts)} 次，"
                f"为避免触发平台限频，本地已暂停登录。\n"
                f"  请等约 {max(1, wait // 60)} 分钟再试；\n"
                f"  或改用 browser 模式（真实浏览器过一次验证码）。",
                kind="rate_limited",
            )
        if attempts and self.min_interval:
            gap = now - max(attempts)
            if 0 <= gap < self.min_interval:
                raise LoginError(
                    f"登录请求过于频繁（距上次 {gap:.1f}s，最小间隔 {self.min_interval}s）",
                    kind="rate_limited",
                )

    def record_attempt(self) -> None:
        now = self.clock()
        attempts = [t for t in self._read() if now - t < self.window]
        attempts.append(now)
        try:
            atomic_write_text(
                self.path,
                json.dumps({"attempts": attempts, "updated_at": now}, ensure_ascii=False),
            )
        except OSError:
            pass


# ============================================================ 解析
def _extract_error(html: str) -> tuple[str, str]:
    """从登录页 HTML 里抠出错误提示，返回 (人话提示, kind)。"""
    text = re.sub(r"<script[\s\S]*?</script>", " ", html or "")
    text = re.sub(r"<style[\s\S]*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)

    # 优考试在被风控限制时也会返回"密码不正确"，统一话术不暴露规则。
    if "频繁" in text or "限制" in text or "限流" in text or "风控" in text:
        return (
            "登录被平台临时限制（页面提示含频繁/限制/风控）。\n"
            "  请等 10~30 分钟，或改用 login_mode=browser 用真实浏览器登录一次。",
            "rate_limited",
        )
    if "验证码" in text or "滑块" in text or "验证" in text:
        return (
            "登录需要人机验证。\n"
            "  请把 login_mode 改成 browser，用真实浏览器完成一次登录。",
            "captcha",
        )
    if "密码不正确" in text or "账号或密码" in text:
        return (
            "账号或密码不正确。\n"
            "  如果确认密码无误，可能是短时间多次登录被临时限制 —— 等 10~30 分钟再试，\n"
            "  或用 browser 模式完成一次真实浏览器登录（会走验证码），通常能解除。",
            "credentials",
        )
    for kw in ("冻结", "锁定", "错误"):
        i = text.find(kw)
        if i >= 0:
            return text[max(0, i - 60) : i + 60].strip(), "unknown"
    return "登录失败，未取到 user_token（账号密码可能不正确，或被风控拦截）", "unknown"


def _login_headers(cfg: Config) -> dict[str, str]:
    return navigation_headers(
        cfg.http.user_agent,
        referer=LOGIN_URL,
        origin=BASE,
        extra=cfg.http.extra_headers,
    )


# ============================================================ 登录
def password_login(
    cfg: Config,
    username: str | None = None,
    password: str | None = None,
    debug: bool = False,
    guard: LoginGuard | None = None,
) -> dict[str, str]:
    """账号密码登录，返回 {user_id, user_token, yiban_id}。

    失败抛 LoginError（带 kind）。成功会把凭据 + 完整 cookie 落盘。
    """
    username = (username or cfg.account.username or "").strip()
    password = password or cfg.account.password or ""
    if not username or not password:
        raise LoginError(
            "未配置账号密码。请在 config.yaml 的 account.username / account.password 填写，"
            "或用 --user / --pass 传入。",
            kind="config",
        )

    guard = guard or LoginGuard(
        max_attempts=cfg.account.login_max_attempts,
        window_seconds=cfg.account.login_window_seconds,
        min_interval_seconds=cfg.account.login_min_interval_seconds,
    )
    guard.precheck()

    s = requests.Session()
    s.headers.update(_login_headers(cfg))
    if cfg.http.proxy:
        s.proxies = {"http": cfg.http.proxy, "https": cfg.http.proxy}

    # 1) 拿登录页与 CSRF
    try:
        r = s.get(LOGIN_URL, timeout=(min(10, cfg.http.timeout), cfg.http.timeout))
    except requests.RequestException as exc:
        raise LoginError(f"打开登录页失败（网络）：{exc}", kind="network") from exc
    if r.status_code != 200:
        raise LoginError(f"打开登录页失败：HTTP {r.status_code}", kind="unknown")
    m = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', r.text)
    csrf = m.group(1) if m else (s.cookies.get("csrftoken") or "")
    if not csrf:
        raise LoginError("未能获取 CSRF token，登录页结构可能变了", kind="unknown")

    # 2) 直接 POST 表单（rid / captcha_type 留空 —— 后端不校验）
    payload = {
        "csrfmiddlewaretoken": csrf,
        "username": username,
        "password": password,
        "rid": "",
        "captcha_type": "",
        "remember": "true",
        "yooc_submit": "登录",
    }
    guard.record_attempt()
    try:
        r2 = s.post(
            LOGIN_URL,
            data=payload,
            headers={
                **navigation_headers(
                    cfg.http.user_agent,
                    referer=LOGIN_URL,
                    origin=BASE,
                    extra=cfg.http.extra_headers,
                ),
                "X-CSRFToken": csrf,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=(min(10, cfg.http.timeout), cfg.http.timeout),
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        raise LoginError(f"提交登录表单失败（网络）：{exc}", kind="network") from exc
    if debug:
        print(f"[login] POST -> {r2.status_code} {r2.url}")

    # 3) 凭据在 cookie 里
    ck = s.cookies.get_dict()
    if not ck.get("user_token"):
        msg, kind = _extract_error(r2.text)
        raise LoginError(msg, kind=kind)

    creds = {
        "user_id": ck.get("user_id", ""),
        "user_token": ck["user_token"],
        "yiban_id": ck.get("yiban_id", ""),
    }

    # 4) 顺手验证凭据真的能用；失败不阻断（可能是网络抖动）
    state = validate_saved(cfg, creds, debug=debug)
    if state is False:
        raise LoginError(
            "登录成功但考试接口校验失败：凭据可能被平台判定无效，"
            "请稍后重试或改用 browser 模式。",
            kind="api",
        )

    save_session(cfg, creds, _cookies_to_list(s.cookies))
    return creds


# ============================================================ 持久化
def _cookies_to_list(jar: Any) -> list[dict]:
    out: list[dict] = []
    try:
        for c in jar:
            out.append(
                {
                    "name": c.name,
                    "value": c.value,
                    "domain": c.domain or "",
                    "path": c.path or "/",
                    "secure": bool(c.secure),
                    "expires": c.expires,
                }
            )
    except Exception:
        return []
    return out


def save_session(cfg: Config, creds: dict[str, str], cookies: list[dict]) -> None:
    record = {
        "group_id": cfg.account.group_id,
        "user_id": creds.get("user_id", ""),
        "user_token": creds.get("user_token", ""),
        "yiban_id": creds.get("yiban_id", ""),
        "cookies": cookies,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "saved_epoch": time.time(),
    }
    try:
        atomic_write_text(
            session_path(cfg), json.dumps(record, ensure_ascii=False, indent=2)
        )
    except OSError:
        pass


def load_session(cfg: Config | None = None) -> dict | None:
    try:
        d = json.loads(session_path(cfg).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(d, dict) or not d.get("user_token"):
        return None
    if cfg is not None and d.get("group_id") not in (None, "", cfg.account.group_id):
        return None
    d.setdefault("cookies", [])
    return d


def load_saved(cfg: Config) -> dict[str, str] | None:
    d = load_session(cfg)
    if not d:
        return None
    return {
        "user_id": d.get("user_id", ""),
        "user_token": d.get("user_token", ""),
        "yiban_id": d.get("yiban_id", ""),
    }


def load_saved_cookies(cfg: Config | None = None) -> list[dict]:
    d = load_session(cfg)
    if not d:
        return []
    cookies = d.get("cookies")
    return cookies if isinstance(cookies, list) else []


# ============================================================ 校验
def validate_saved(cfg: Config, creds: dict[str, str], debug: bool = False) -> bool | None:
    """校验凭据是否仍可用。True=可用，False=明确失效，None=网络原因无法判断。

    必须用 ``list/get`` 而不是 ``user/check`` 探测：实测 token 过期后
    user/check 仍返回 ``result:true``（它只校验课群角色，不判 token 死活），
    而 list/get 会明确返回 ``code=403``「用户token失效」——只调 check 会把
    失效凭据误判为可用，后续拉考试列表才炸 403。
    """
    if not creds or not creds.get("user_token"):
        return False
    headers = ajax_headers(
        cfg.http.user_agent,
        origin="https://exam.yooc.me",
        referer=f"https://exam.yooc.me/group/{cfg.account.group_id}/exams",
        extra=cfg.http.extra_headers,
    )
    # 参数顺序对齐前端模板 QUERY_SPEC：userId, token, yibanId, groupId
    params = {
        "userId": creds.get("user_id", ""),
        "token": creds.get("user_token", ""),
        "yibanId": creds.get("yiban_id", ""),
        "groupId": cfg.account.group_id,
    }
    url = f"{BACKEND}/api/exam/list/get"
    try:
        r = requests.get(
            url,
            params=params,
            headers=headers,
            timeout=(min(8, cfg.http.timeout), min(15, cfg.http.timeout)),
            proxies=(
                {"http": cfg.http.proxy, "https": cfg.http.proxy}
                if cfg.http.proxy
                else None
            ),
        )
        if debug:
            print(f"[login] validate -> {r.status_code} {r.text[:160]}")
        if r.status_code in (401, 403):
            return False
        if r.status_code >= 500:
            return None
        data = r.json()
        if isinstance(data, dict):
            if data.get("code") == 403:
                return False
            if "result" in data:
                return bool(data.get("result"))
        return None
    except (requests.RequestException, ValueError):
        return None


def session_path(cfg: Config | None = None) -> Path:
    """当前账号的凭据文件路径。

    有 cfg 时直接用已解析的账号路径；没有 cfg（CLI 的 --reset-login 等
    在加载配置前执行的场景）就按 config.yaml 的 account 段现推。
    测试里会 patch 模块级 SESSION_PATH，这种情况优先尊重 patch 值。
    """
    if SESSION_PATH != _LEGACY_SESSION_PATH:
        return SESSION_PATH
    if cfg is not None:
        return cfg.paths.session
    return paths_for(peek_account_key()).session


def config_path() -> Path:
    return CONFIG_PATH


def project_root() -> Path:
    return ROOT
