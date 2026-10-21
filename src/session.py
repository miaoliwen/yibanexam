"""浏览器会话：真实浏览器通道 + 页面内请求。

设计要点
--------
1. 用 patchright（playwright 的反检测分支）启动持久化 profile。
   首次手动登录一次，之后 cookie / localStorage 全部复用。
2. 站点鉴权是 query 参数：?userId=..&token=..&yibanId=..，也同步在 cookie 里。
3. **写操作（start / submit）通过页面内 fetch 发送**：真实浏览器网络栈、
   TLS/HTTP2 指纹、风控 SDK 运行环境一起参与。只有页面不可用时才降级纯 HTTP。
4. 纯接口登录模式下也可以用浏览器做「写通道」：把登录拿到的 cookie 注入
   浏览器上下文，不弹登录框、不需要人工操作。

v2 变化：page_fetch 返回 (status, data)、带请求超时、可注入 cookie、
可从浏览器读真实 UA（避免 HTTP 通道 UA 与浏览器版本互相矛盾）。
"""
from __future__ import annotations

import json
import time
from typing import Any

from patchright.sync_api import BrowserContext, Page, sync_playwright

from .config import Config
from .errors import SessionError
from .login import save_session

EXAM_BASE = "https://exam.yooc.me"
BACKEND = "https://exambackend.yooc.me"

STORAGE_KEYS = {"user_id": "user_id", "user_token": "user_token", "yiban_id": "yiban_id"}

# 页面正在导航时的典型报错。这类错误是瞬时的：等 load 完再发一次就好，
# 不代表页面通道不可用，不能拿它当"永久降级"的依据。
_NAV_HINTS = (
    "execution context was destroyed",
    "most likely because of a navigation",
    "execution context is not available",
    "frame was detached",
    "cannot find context",
)

__all__ = ["SessionError", "YoocSession"]


class YoocSession:
    """持有持久化浏览器上下文，并抽取考试接口需要的凭据。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._pw = None
        self.ctx: BrowserContext | None = None
        self.page: Page | None = None
        self.creds: dict[str, str] = {}
        self.ua: str = cfg.http.user_agent
        self.headless: bool = bool(cfg.account.headless)

    # ------------------------------------------------------------------ 生命周期
    def __enter__(self) -> "YoocSession":
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def open(self, headless: bool | None = None) -> None:
        self.headless = bool(self.cfg.account.headless if headless is None else headless)
        self._pw = sync_playwright().start()
        kw: dict[str, Any] = {
            "user_data_dir": str(self.cfg.account.profile_dir),
            "headless": self.headless,
            "slow_mo": self.cfg.account.slow_mo_ms,
            "viewport": {"width": 1536, "height": 864},
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "args": ["--disable-features=IsolateOrigins,site-per-process"],
            "timeout": 45_000,
        }
        # 指纹优先级：配置指定 > 本机 Chrome > 本机 Edge > 包内 chromium。
        candidates: list[dict[str, Any]] = []
        if self.cfg.account.browser_path:
            candidates.append({"executable_path": self.cfg.account.browser_path})
        candidates.extend(
            [
                {"channel": "chrome"},
                {"channel": "msedge"},
                {"_bundled": True},
            ]
        )

        last_err: Exception | None = None
        for opt in candidates:
            kw2 = dict(kw)
            if not opt.get("_bundled"):
                kw2.update(opt)
            try:
                self.ctx = self._pw.chromium.launch_persistent_context(**kw2)
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                continue
        if self.ctx is None:
            self._stop_pw()
            raise SessionError(
                "启动浏览器失败：未找到可用的 Chrome/Edge。\n"
                "请安装 Google Chrome，或在 config.yaml 里把 account.browser_path "
                "设为 chrome.exe 的完整路径。\n"
                "如果浏览器正被其他实例占用，请先关掉残留的 chrome 进程。\n"
                f"最后一次错误：{last_err}"
            )

        self.ctx.set_default_timeout(30_000)
        self.ctx.set_default_navigation_timeout(45_000)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.ua = self.detect_ua()

    def close(self) -> None:
        try:
            if self.ctx:
                try:
                    self.ctx.close()
                except Exception:
                    pass
        finally:
            self._stop_pw()
            self.ctx = None
            self.page = None
            self._pw = None

    def _stop_pw(self) -> None:
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass

    def page_alive(self) -> bool:
        try:
            return bool(self.page and not self.page.is_closed())
        except Exception:
            return False

    def _settle_after_navigation(self, timeout_ms: int = 8_000) -> bool:
        """页面导航后等它稳定，返回是否还能继续用这个页面。

        不抛异常：调用方已经在处理原始错误，这里只负责把状态恢复到可重试。
        """
        try:
            if not self.page_alive():
                return False
            self.page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            return True
        except Exception:
            return self.page_alive()

    # ------------------------------------------------------------------ 浏览器元信息
    def detect_ua(self) -> str:
        if not self.page_alive():
            return self.cfg.http.user_agent
        try:
            ua = self.page.evaluate("() => navigator.userAgent")
            return str(ua or self.cfg.http.user_agent)
        except Exception:
            return self.cfg.http.user_agent

    def inject_cookies(self, cookies: list[dict]) -> int:
        """把登录接口拿到的 cookie 注入浏览器上下文（跨子域统一放到 .yooc.me）。"""
        if not self.ctx or not cookies:
            return 0
        prepared: list[dict] = []
        for c in cookies:
            try:
                name = str(c.get("name") or "")
                if not name:
                    continue
                domain = str(c.get("domain") or "")
                if "yooc.me" not in domain:
                    domain = ".yooc.me"
                expires = c.get("expires")
                try:
                    expires = int(expires) if expires is not None else -1
                except (TypeError, ValueError):
                    expires = -1
                prepared.append(
                    {
                        "name": name,
                        "value": str(c.get("value") or ""),
                        "domain": domain,
                        "path": str(c.get("path") or "/"),
                        "secure": bool(c.get("secure", True)),
                        "httpOnly": False,
                        "expires": expires,
                    }
                )
            except Exception:
                continue
        if not prepared:
            return 0
        try:
            self.ctx.add_cookies(prepared)
            return len(prepared)
        except Exception:
            return 0

    # ------------------------------------------------------------------ 导航 / 登录
    def goto_exams(self, attempts: int = 2) -> None:
        if not self.page_alive():
            raise SessionError("浏览器页面已关闭")
        url = f"{EXAM_BASE}/group/{self.cfg.account.group_id}/exams"
        self.goto(url, attempts=attempts)

    def goto(self, url: str, attempts: int = 2) -> None:
        if not self.page_alive():
            raise SessionError("浏览器页面已关闭")
        last: Exception | None = None
        for i in range(max(1, attempts)):
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                return
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(0.8 + i)
        raise SessionError(f"打开页面失败：{last}")

    def form_login(self, username: str, password: str, wait_seconds: int = 18) -> dict[str, str] | None:
        """在真实浏览器里提交登录表单。

        为什么不用 HTTP requests 登录：登录页本身会加载静态资源、走真实 TLS/HTTP2、
        cookie 也由浏览器自己管理。直接 form.submit() 绕过前端 `#yooc_submit`
        的点击拦截（后端实测不校验数美），但保留了真实浏览器的网络与 Cookie 行为。

        失败返回 None，由调用方回退纯 HTTP 登录。
        """
        if not username or not password or not self.page_alive():
            return None
        login_url = "https://www.yooc.me/mobile/login"
        try:
            self.goto(login_url)
        except SessionError:
            return None
        try:
            ok = self.page.evaluate(
                """({u, p}) => {
                    const forms = Array.from(document.querySelectorAll('form'));
                    const f = forms.find(x => x.querySelector('[name="username"]'))
                        || forms.find(x => x.querySelector('[name="password"]'));
                    if (!f) return false;
                    const set = (name, value) => {
                        const el = f.querySelector('[name="' + name + '"]');
                        if (el) {
                            el.value = value;
                            el.dispatchEvent(new Event('input', {bubbles: true}));
                            el.dispatchEvent(new Event('change', {bubbles: true}));
                        }
                    };
                    set('username', u);
                    set('password', p);
                    set('remember', 'true');
                    // 不要清空 rid / captcha_type：保留页面（数美 SDK）已生成的值。
                    // 原生 form.submit() 不触发 onsubmit，绕过前端验证码点击拦截。
                    const addHidden = (n, v) => {
                        if (!f.querySelector('[name="' + n + '"]')) {
                            const i = document.createElement('input');
                            i.type = 'hidden'; i.name = n; i.value = v;
                            f.appendChild(i);
                        }
                    };
                    addHidden('yooc_submit', '登录');
                    f.submit();
                    return true;
                }""",
                {"u": username, "p": password},
            )
        except Exception:
            return None
        if not ok:
            return None

        deadline = time.time() + max(5, wait_seconds)
        while time.time() < deadline:
            time.sleep(0.8)
            try:
                self.page.wait_for_load_state("domcontentloaded", timeout=5_000)
            except Exception:
                pass
            creds = self.read_creds()
            if creds.get("user_token"):
                self.creds = creds
                self._persist()
                return creds
            try:
                u = (self.page.url or "").lower()
            except Exception:
                u = ""
            try:
                body = self.page.inner_text("body")[:4000]
            except Exception:
                body = ""
            if any(
                kw in body
                for kw in ("密码不正确", "账号或密码", "验证码", "滑块", "频繁", "限制", "风控")
            ):
                return None
            if "login" not in u and "passport" not in u and u.startswith("http"):
                # 已离开登录页但仍读不到 token，再等一轮
                continue
        return None

    def _read_local(self, key: str) -> str:
        if not self.page_alive():
            return ""
        try:
            val = self.page.evaluate("k => window.localStorage.getItem(k) || ''", key)
        except Exception:
            val = ""
        return str(val or "").strip().strip('"')

    def _read_cookie(self, key: str) -> str:
        """localStorage 取不到时，从 cookie 兜底（跨子域可能只写 cookie）。"""
        if not self.ctx:
            return ""
        try:
            for c in self.ctx.cookies():
                if c.get("name") in (key, key.replace("_", "")):
                    if "yooc.me" in (c.get("domain") or ""):
                        return str(c.get("value") or "").strip().strip('"')
        except Exception:
            pass
        return ""

    def read_creds(self) -> dict[str, str]:
        creds = {}
        for name, k in STORAGE_KEYS.items():
            creds[name] = self._read_local(k) or self._read_cookie(k)
        return creds

    def has_creds(self) -> bool:
        return bool(self.read_creds().get("user_token"))

    def is_login_page(self) -> bool:
        if not self.page_alive():
            return True
        u = (self.page.url or "").lower()
        return "login" in u or "/passport" in u or "sso" in u

    def ensure_login(self, wait_seconds: int = 600, interactive: bool = True) -> dict[str, str]:
        """打开考试页；未登录则等待人工登录完成。

        注意：未登录时站点会 302 到 www.yooc.me/mobile/login，
        此时绝对不能反复 goto 考试页 —— 那会把用户正在输入的账号密码打断。
        """
        self.goto_exams()
        if self.has_creds() and not self.is_login_page():
            self.creds = self.read_creds()
            self._persist()
            return self.creds

        if not interactive:
            raise SessionError(
                "未检测到登录态。请把 account.headless 设为 false 后重跑，手动登录一次。"
            )

        print("\n" + "=" * 66)
        print(" 需要登录")
        print(" 浏览器已打开，请在里完成登录（账号密码 / 短信 / 滑块都可以，慢一点也行）")
        print(" 程序不会碰你的浏览器窗口，登录完成后自动继续")
        print(f" 最长等待 {wait_seconds} 秒")
        print("=" * 66 + "\n")

        deadline = time.time() + wait_seconds
        on_login_page = self.is_login_page()
        left_login_at = None

        while time.time() < deadline:
            time.sleep(1.5)
            try:
                if not self.page_alive():
                    raise SessionError("浏览器被关闭，登录中止")

                if self.is_login_page():
                    on_login_page = True
                    left_login_at = None
                    continue

                if on_login_page:
                    if left_login_at is None:
                        left_login_at = time.time()
                    if time.time() - left_login_at < 2.5:
                        continue
                    if "exam.yooc.me" not in (self.page.url or ""):
                        self.goto_exams()
                        time.sleep(2.0)
                        if self.is_login_page():
                            on_login_page = True
                            left_login_at = None
                            continue

                creds = self.read_creds()
                if creds.get("user_token"):
                    self.creds = creds
                    self._persist()
                    print("[ok] 登录态已捕获")
                    return creds
            except SessionError:
                raise
            except Exception:
                continue

        raise SessionError("等待登录超时。请确认已在浏览器中登录成功。")

    def _persist(self) -> None:
        cookies: list[dict] = []
        try:
            if self.ctx:
                for c in self.ctx.cookies():
                    cookies.append(
                        {
                            "name": c.get("name"),
                            "value": c.get("value"),
                            "domain": c.get("domain"),
                            "path": c.get("path"),
                            "secure": c.get("secure"),
                            "expires": c.get("expires"),
                        }
                    )
        except Exception:
            cookies = []
        if self.creds.get("user_token"):
            save_session(self.cfg, self.creds, cookies)

    def verify_login(self) -> bool:
        """页面在考试域且能读到 token，说明浏览器通道可用。"""
        try:
            if "(login" in (self.page.url or "") or self.is_login_page():
                return False
            return self.has_creds()
        except Exception:
            return False

    # ------------------------------------------------------------------ 页面内请求
    def page_fetch(self, path: str, method: str = "GET", body: Any = None) -> tuple[int, Any]:
        """在页面上下文里发请求：带真实 UA / TLS / 风控 SDK 环境。

        path 是相对路径且已包含鉴权 query，例如
        "api/exam/start/action?token=..&yibanId=.."

        **严格对齐前端 bundle 的请求包装器**（index.js `I()` 函数）：
        - 写请求 body 是 `FormData`，每个字段值 `JSON.stringify` 后 append；
        - 不手动设 Content-Type（浏览器自己带 multipart boundary）；
        - fetch 不传 credentials（默认 same-origin，跨域不带 Cookie）——
          真实前端就是这样，带 Cookie 反而会触发 CORS 预检差异。

        返回 (status, data)。网络层失败时 status=-1（此时写操作结果未知）。
        """
        if not self.page_alive():
            raise SessionError("浏览器页面已关闭，无法发送页面内请求")

        # 页面被重定向到登录页 = 登录态失效。这必须报 AuthError 让上层重登，
        # 而不是降级成纯 HTTP 继续跑（那样后续每个请求都会 401）。
        try:
            cur = (self.page.url or "").lower()
            if "/login" in cur or "passport" in cur:
                raise SessionError("页面已跳转到登录页，登录态失效")
        except SessionError:
            raise
        except Exception:
            pass

        url = f"{BACKEND}/{path.lstrip('/')}"
        timeout_ms = max(5, min(60, int(self.cfg.http.timeout))) * 1000
        js = """
        async ({url, method, body, timeoutMs}) => {
            const opt = { method };
            if (body !== null && body !== undefined && method !== 'GET') {
                // 与前端一致：FormData + 每个字段 JSON.stringify
                const fd = new FormData();
                for (const k of Object.keys(body)) {
                    fd.append(k, JSON.stringify(body[k]));
                }
                opt.body = fd;
            }
            const ctl = new AbortController();
            const timer = setTimeout(() => ctl.abort(), timeoutMs);
            opt.signal = ctl.signal;
            try {
                const r = await fetch(url, opt);
                const txt = await r.text();
                let data = null;
                try { data = JSON.parse(txt); } catch (e) { data = { __raw: txt }; }
                return { status: r.status, data };
            } catch (e) {
                return { status: -1, data: { __error: String(e) } };
            } finally {
                clearTimeout(timer);
            }
        }
        """
        try:
            res = self.page.evaluate(
                js, {"url": url, "method": method, "body": body, "timeoutMs": timeout_ms}
            )
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if any(h in msg for h in _NAV_HINTS):
                # 导航中：先把页面等稳定，上层（读请求）重试时就能命中新上下文。
                # 这里不重发请求：写操作可能已经到达服务端，重发会重复提交。
                self._settle_after_navigation()
            raise SessionError(f"页面内请求执行失败：{exc}") from exc
        if not isinstance(res, dict):
            raise SessionError("页面内请求返回了非预期结果")
        status = int(res.get("status", -1))
        data = res.get("data")
        if status in (401, 403):
            raise SessionError(f"会话失效（HTTP {status}）：token 可能已过期")
        return status, data

    def hover_idle(self, ms: int) -> None:
        """在页面上做点无意义的真实交互，避免长时间零输入。"""
        if not self.page_alive():
            return
        import random

        end = time.time() + max(0, ms) / 1000
        while time.time() < end:
            try:
                self.page.mouse.move(
                    random.randint(120, 1400),
                    random.randint(140, 780),
                    steps=random.randint(4, 12),
                )
                self.page.mouse.wheel(0, random.choice([-120, 120, 240]))
            except Exception:
                return
            time.sleep(random.uniform(0.25, 0.9))

    # ------------------------------------------------------------------ 诊断
    def dump_state(self) -> dict:
        return {
            "url": self.page.url if self.page_alive() else None,
            "ua": self.ua,
            "headless": self.headless,
            "has_creds": self.has_creds() if self.page_alive() else False,
            "login_page": self.is_login_page(),
        }

    def save_json(self, path: Any) -> None:
        path.write_text(
            json.dumps(self.dump_state(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
