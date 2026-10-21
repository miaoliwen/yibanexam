"""图形界面：本地 HTTP 服务 + 系统浏览器。

为什么不用 pywebview
--------------------
pywebview 的 Windows 后端（winforms）依赖 pythonnet 的 .NET 桥，在遍历窗口属性时会出现
`window.native...SyncRoot.SyncRoot.SyncRoot... maximum recursion depth exceeded`，
UI 线程直接卡死，窗口显示"未响应"。这是 pywebview/pythonnet 的兼容问题，
换参数绕不过去。

所以改成最朴素也最稳的方案：
- 标准库 ThreadingHTTPServer 监听 127.0.0.1 的随机端口
- 用系统默认浏览器打开页面，界面还是原来那份 HTML/CSS
- 前端用 fetch 调本地 JSON 接口，日志靠轮询增量拉取

零 GUI 依赖，打包体积还小了一截。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import secrets
import shutil
import socket
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.api import YoocApi  # noqa: E402
from src.config import CONFIG_PATH, ROOT, load  # noqa: E402
from src.errors import AppError  # noqa: E402
from src.login import (  # noqa: E402
    LoginError,
    LoginGuard,
    load_saved,
    load_saved_cookies,
    password_login,
    validate_saved,
)
from src.runner import run as run_cli  # noqa: E402
from src.state import atomic_write_text  # noqa: E402

BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
ASSETS = BUNDLE / "assets" / "index.html"


# ============================================================ 日志缓冲
class LogBuffer:
    """前端用 ?since=N 增量拉取，避免长轮询把线程占满。"""

    def __init__(self) -> None:
        self._lines: list[list[str]] = []
        self._lock = threading.Lock()
        self.progress = 0.0
        self.status = ""
        self.done: dict | None = None
        self.heartbeat = time.time()

    def add(self, msg: str, level: str = "info") -> None:
        with self._lock:
            self._lines.append([msg, level])
            if len(self._lines) > 3000:
                del self._lines[:1000]

    def snapshot(self, since: int) -> dict:
        with self._lock:
            total = len(self._lines)
            start = max(0, min(since, total))
            return {
                "lines": self._lines[start:],
                "total": total,
                "progress": self.progress,
                "status": self.status,
                "done": self.done,
            }

    def reset(self) -> None:
        with self._lock:
            self._lines.clear()
            self.progress = 0.0
            self.status = ""
            self.done = None


class _UIStream(io.TextIOBase):
    """把 runner 的 print 搬进日志缓冲。"""

    def __init__(self, buf: LogBuffer):
        self.buf = buf
        self._acc = ""

    def write(self, s: str) -> int:
        self._acc += s
        while True:
            i_n = self._acc.find("\n")
            i_r = self._acc.find("\r")
            if i_n == -1 and i_r == -1:
                break
            if i_r != -1 and (i_n == -1 or i_r < i_n):
                self._emit(self._acc[:i_r])
                self._acc = self._acc[i_r + 1:]
            else:
                line = self._acc[:i_n]
                self._acc = self._acc[i_n + 1:]
                self._emit(line)
        return len(s)

    def _emit(self, line: str) -> None:
        line = line.replace("\x1b", "").rstrip()
        if not line.strip():
            return
        if "█" in line or "░" in line:
            import re

            m = re.search(r"(\d+)\s*%", line)
            if m:
                self.buf.progress = int(m.group(1))
            return

        low = line.lower()
        if "错误" in line or "失败" in line or "error" in low or "异常" in line:
            lv = "err"
        elif "⚠" in line or "warn" in low or "跳过" in line:
            lv = "warn"
        elif line.lstrip().startswith(("[1/", "[2/", "[3/", "[4/", "[5/", "[6/", "[7/", "===")):
            lv = "accent"
        elif "ok" in low and len(line) < 80:
            lv = "ok"
        else:
            lv = "info"
        self.buf.add(line, lv)

    def flush(self) -> None:
        if self._acc.strip():
            self._emit(self._acc)
            self._acc = ""


# ============================================================ 业务逻辑
class GuiApi:
    def __init__(self, logs: LogBuffer):
        self.logs = logs
        self._running = False
        # 实时状态：runner 通过 on_event 推过来，前端每 350ms 拉一次
        self.live = {
            "phase": "idle",
            "solve": {"done": 0, "total": 0},
            "search": {
                "state": "idle",   # idle / running / done / unavailable
                "done": 0,
                "total": 0,
                "verified": 0,
                "unverified": 0,
                "revised": 0,
                "failed": 0,
                "all": False,
                "reason": "",
                "last": None,
            },
            "module": {"state": "idle", "done": 0, "total": 0, "matched": 0, "changed": 0},
            "bank": {"state": "idle", "matched": 0, "applied": 0, "total": 0},
        }
        self._live_lock = threading.Lock()

    # ------------------------------------------------ 实时状态
    def reset_live(self) -> None:
        with self._live_lock:
            self.live["phase"] = "idle"
            self.live["solve"] = {"done": 0, "total": 0}
            self.live["search"] = {
                "state": "idle",
                "done": 0,
                "total": 0,
                "verified": 0,
                "unverified": 0,
                "revised": 0,
                "failed": 0,
                "all": False,
                "reason": "",
                "last": None,
            }
            self.live["module"] = {
                "state": "idle",
                "done": 0,
                "total": 0,
                "matched": 0,
                "changed": 0,
            }
            self.live["bank"] = {"state": "idle", "matched": 0, "applied": 0, "total": 0}

    def _on_event(self, kind: str, data: dict) -> None:
        """runner 的阶段事件 → 结构化状态。运行在工作线程，必须加锁。"""
        if not isinstance(data, dict):
            return
        with self._live_lock:
            if kind == "phase":
                self.live["phase"] = str(data.get("name") or "")
                if data.get("name") == "solve":
                    self.live["solve"]["total"] = int(data.get("total") or 0)
            elif kind == "solve":
                self.live["solve"]["done"] = int(data.get("done") or 0)
                self.live["solve"]["total"] = int(data.get("total") or 0)
            elif kind == "search":
                s = self.live["search"]
                phase = data.get("phase")
                if phase == "start":
                    s.update(
                        state="running",
                        done=0,
                        total=int(data.get("total") or 0),
                        verified=0,
                        unverified=0,
                        revised=0,
                        failed=0,
                        all=bool(data.get("all")),
                        reason="",
                        last=None,
                    )
                elif phase == "done":
                    s["state"] = "done"
                    for k in ("verified", "unverified", "revised", "failed"):
                        if k in data:
                            s[k] = int(data.get(k) or 0)
                    s["done"] = s["total"]
                elif phase == "unavailable":
                    s["state"] = "unavailable"
                    s["reason"] = str(data.get("reason") or "搜索不可用")
                else:
                    for k in ("done", "total", "verified", "unverified", "revised", "failed"):
                        if k in data:
                            s[k] = int(data.get(k) or 0)
                    s["state"] = "running"
            elif kind == "search_item":
                s = self.live["search"]
                s["last"] = {
                    "index": data.get("index") or 0,
                    "stem": data.get("stem") or "",
                    "ok": bool(data.get("ok")),
                    "note": data.get("note") or "",
                }
            elif kind == "module":
                m = self.live["module"]
                phase = data.get("phase")
                if phase == "start":
                    m.update(state="running", done=0, total=0, matched=0, changed=0)
                elif phase == "done":
                    m["state"] = "done"
                    m["matched"] = int(data.get("matched") or 0)
                    m["changed"] = int(data.get("changed") or 0)
                else:
                    m["state"] = "running"
                    m["done"] = int(data.get("done") or 0)
                    m["total"] = int(data.get("total") or 0)
            elif kind == "bank":
                b = self.live["bank"]
                enabled = bool(data.get("enabled"))
                b.update(
                    state="done" if enabled else "off",
                    matched=int(data.get("matched") or 0),
                    applied=int(data.get("applied") or 0),
                    total=int(data.get("total") or 0),
                )

    def get_live_status(self) -> dict:
        with self._live_lock:
            return json.loads(json.dumps(self.live, ensure_ascii=False))

    # ------------------------------------------------ 配置
    @staticmethod
    def _read_raw() -> dict:
        if CONFIG_PATH.exists():
            try:
                return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
            except Exception:
                pass
        return {}

    @staticmethod
    def _raw_error() -> str | None:
        """返回 YAML 解析错误；文件不存在或为空不算错误。"""
        if not CONFIG_PATH.exists():
            return None
        try:
            text = CONFIG_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            return f"读取失败：{exc}"
        if not text.strip():
            return None
        try:
            yaml.safe_load(text)
            return None
        except Exception as exc:  # noqa: BLE001
            return str(exc)[:300]

    def get_state(self) -> dict:
        raw = self._read_raw()
        acc = raw.get("account") or {}
        llm = raw.get("llm") or {}
        hum = raw.get("humanize") or {}

        pace = hum.get("per_question_seconds") or 34
        try:
            pf = float(pace)
            pace_s = str(int(pf)) if pf == int(pf) else str(pf)
        except (TypeError, ValueError):
            pace_s = "34"

        pwd = acc.get("password") or ""
        return {
            "username": acc.get("username") or "",
            # 密码/API Key 不回传给前端：本地页面也可能被其它脚本读到。
            # 前端只显示「已保存 · 留空则沿用」，保存时留空不覆盖。
            "password": "",
            "has_password": bool(pwd),
            "group_id": str(acc.get("group_id") or ""),
            "base_url": llm.get("base_url") or "https://api.deepseek.com/v1",
            "api_key": "",
            "has_api_key": bool(llm.get("api_key")),
            "model": llm.get("model") or "deepseek-flash",
            "search_enabled": bool(llm.get("search_enabled", True)),
            "search_all": bool(llm.get("search_all_questions", False)),
            "pace": pace_s,
            "auto_start": bool((raw.get("exam") or {}).get("auto_start", False)),
            "start_at": str((raw.get("exam") or {}).get("start_at") or "auto"),
        }

    def save_config(self, d: dict) -> dict:
        try:
            # 坏 YAML 绝不能"解析失败→按默认重建"，否则 api_key/密码会被清空
            err = self._raw_error()
            if err:
                backup = CONFIG_PATH.with_name(
                    f"config.yaml.corrupt.{int(time.time())}"
                )
                try:
                    shutil.copyfile(CONFIG_PATH, backup)
                except OSError:
                    backup = None
                return {
                    "ok": False,
                    "msg": (
                        f"config.yaml 解析失败，已拒绝覆盖"
                        + (f"（原文件备份到 {backup}）" if backup else "")
                        + f"：{err}"
                    ),
                }
            raw = self._read_raw()
            acc = raw.setdefault("account", {})
            llm = raw.setdefault("llm", {})
            hum = raw.setdefault("humanize", {})

            acc["username"] = (d.get("username") or "").strip() or acc.get("username") or ""
            # 密码框常是空的，空值绝不能覆盖已存的密码
            new_pwd = d.get("password") or ""
            if new_pwd:
                acc["password"] = new_pwd
            else:
                acc.setdefault("password", "")
            # 课群 ID 传空时保留原值 —— 和密码同理，界面空不代表要清空配置
            raw_gid = str(d.get("group_id") or "").strip()
            if raw_gid:
                try:
                    acc["group_id"] = int(raw_gid)
                except ValueError:
                    pass  # 非法输入就忽略，别写坏配置
            else:
                acc.setdefault("group_id", 0)
            acc.setdefault("login_mode", "password")
            acc.setdefault("headless", False)
            acc.setdefault("profile_dir", "./data/browser_profile")
            acc.setdefault("reuse_session", True)
            acc.setdefault("browser_for_writes", "auto")

            llm["base_url"] = (d.get("base_url") or "").strip() or "https://api.deepseek.com/v1"
            new_key = (d.get("api_key") or "").strip()
            if new_key:
                llm["api_key"] = new_key
            else:
                llm.setdefault("api_key", "")
            llm["model"] = (d.get("model") or "").strip() or "deepseek-flash"
            llm["search_enabled"] = bool(d.get("search_enabled", True))
            if "search_all" in d:
                llm["search_all_questions"] = bool(d.get("search_all"))
            llm.setdefault("temperature", 0.2)
            llm.setdefault("concurrency", 3)
            llm.setdefault("batch_size", 6)
            llm.setdefault("max_retries", 3)
            llm.setdefault("timeout", 90)
            llm.setdefault("search_base_url", "")
            llm.setdefault("search_count", 8)
            llm.setdefault("search_need_content", False)
            llm.setdefault("search_confidence_threshold", 0.75)
            llm.setdefault("search_max_questions", 15)
            llm.setdefault("search_timeout", 30)
            llm.setdefault("search_max_seconds", 240)
            llm.setdefault("allow_guess_without_key", False)
            llm.setdefault("preflight", True)

            try:
                hum["per_question_seconds"] = float(d.get("pace") or 34)
            except ValueError:
                hum["per_question_seconds"] = 34.0
            hum.setdefault("enabled", True)
            hum.setdefault("min_delay_ms", 2200)
            hum.setdefault("max_delay_ms", 9500)
            hum.setdefault("long_think_rate", 0.18)
            hum.setdefault("long_think_ms", 18000)
            hum.setdefault("shuffle_submit_order", False)
            hum.setdefault("idle_before_submit_ms", 3000)
            hum.setdefault("heartbeat_seconds", 90)

            ex = raw.setdefault("exam", {})
            ex.setdefault("auto_submit", True)
            ex.setdefault("dry_run", True)
            ex.setdefault("auto_select_first", False)
            ex.setdefault("submit_review_seconds", 8)
            ex.setdefault("answer_style", "letters")
            ex.setdefault("answer_as_json_string", True)
            ex.setdefault("fill_answer_style", "array")
            ex.setdefault("resume", True)
            ex.setdefault("allow_start_in_dry_run", False)
            ex.setdefault("deadline_margin_seconds", 120)
            ex.setdefault("min_seconds_before_submit", 45)
            ex.setdefault("heartbeat_questions", 0)
            ex.setdefault("guess_unanswered", True)
            ex.setdefault("use_answer_key", True)
            ex.setdefault("captcha_browser_fallback", True)
            ex.setdefault("max_solve_seconds", 0)
            ex.setdefault("force_resubmit", False)
            ex["auto_start"] = bool(d.get("auto_start", ex.get("auto_start", False)))
            raw_start_at = str(d.get("start_at") or "").strip()
            if raw_start_at and raw_start_at.lower() != "auto":
                ex["start_at"] = raw_start_at
            else:
                ex["start_at"] = "auto"
            ex.setdefault("auto_start_max_wait_minutes", 240)
            ex.setdefault("auto_start_poll_seconds", 60)
            ex.setdefault("auto_start_verify", True)

            h = raw.setdefault("http", {})
            h.setdefault("timeout", 30)
            h.setdefault("proxy", None)
            h.setdefault(
                "user_agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
            )
            h.setdefault("retries", 2)
            h.setdefault("backoff_seconds", 1.2)
            h.setdefault("extra_headers", {})

            atomic_write_text(
                CONFIG_PATH,
                yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
            )
            return {"ok": True, "msg": "已保存"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "msg": f"保存失败：{exc}"}

    def _quick_cfg(self, d: dict):
        """先落盘再加载。

        注意：必须在 load() 之前拦下 group_id。load() 抛 ConfigError（Exception 子类），
        HTTP 线程可以正常兜底；但缺 group_id 属于用户输入问题，先给人话提示。
        """
        self.save_config(d)
        raw = self._read_raw()
        acc = raw.get("account") or {}
        try:
            gid = int(acc.get("group_id") or 0)
        except (TypeError, ValueError):
            gid = 0
        if not gid:
            raise ValueError("请先填写课群 ID（考试页 URL 里 /group/ 后面那串数字）")
        return load()

    # ------------------------------------------------ 登录 / 考试
    @staticmethod
    def _guard(cfg) -> LoginGuard:
        return LoginGuard(
            cfg=cfg,
            max_attempts=cfg.account.login_max_attempts,
            window_seconds=cfg.account.login_window_seconds,
            min_interval_seconds=cfg.account.login_min_interval_seconds,
        )

    def _creds_for(self, cfg, d: dict) -> dict:
        """优先复用已保存的登录态，避免每点一次测试就产生一次新登录。"""
        pwd = (d.get("password") or "").strip()
        raw_pwd = (self._read_raw().get("account") or {}).get("password") or ""
        if not pwd or pwd == raw_pwd:
            saved = load_saved(cfg)
            if saved and validate_saved(cfg, saved) is not False:
                return saved
        return password_login(
            cfg, d.get("username"), pwd or None, guard=self._guard(cfg)
        )

    def test_login(self, d: dict) -> dict:
        try:
            cfg = self._quick_cfg(d)
            saved = load_saved(cfg)
            if saved and validate_saved(cfg, saved) is not False:
                pwd = (d.get("password") or "").strip()
                raw_pwd = (self._read_raw().get("account") or {}).get("password") or ""
                if not pwd or pwd == raw_pwd:
                    return {
                        "ok": True,
                        "msg": (
                            "已保存的登录态可用（未重新登录）"
                            f"  userId={saved.get('user_id')}"
                        ),
                    }
            creds = password_login(
                cfg, d.get("username"), d.get("password") or None, guard=self._guard(cfg)
            )
            return {
                "ok": True,
                "msg": f"登录成功  userId={creds['user_id']}  yibanId={creds['yiban_id']}",
            }
        except ValueError as exc:          # 配置不全，直接说清楚
            return {"ok": False, "msg": str(exc)}
        except LoginError as exc:
            return {"ok": False, "msg": f"登录失败：{exc}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "msg": f"登录异常：{exc}"}

    def list_exams(self, d: dict) -> dict:
        try:
            cfg = self._quick_cfg(d)
            creds = self._creds_for(cfg, d)
            api = YoocApi(cfg, creds, cookies=load_saved_cookies(cfg))
            payload = api.exam_list()
            data = payload.get("data") if isinstance(payload, dict) else payload
            exams = []
            for e in (data or []):
                if not isinstance(e, dict):
                    continue
                eid = e.get("examId") or e.get("id")
                title = e.get("name") or e.get("title") or "(未命名)"
                bits = []
                if e.get("subjects"):
                    bits.append(f"{e['subjects']}题")
                if e.get("totalPoints"):
                    bits.append(f"{e['totalPoints']}分")
                if e.get("duration"):
                    bits.append(f"{e['duration']}分钟")
                if e.get("started"):
                    bits.append(str(e["started"])[5:16].replace("-", "/"))
                exams.append({"id": str(eid), "title": title, "info": " · ".join(bits) or "—"})
            if not exams:
                return {"ok": False, "msg": "该课群下没有可参加的考试"}
            return {"ok": True, "exams": exams, "msg": f"共 {len(exams)} 场"}
        except ValueError as exc:
            return {"ok": False, "msg": str(exc)}
        except LoginError as exc:
            return {"ok": False, "msg": f"登录失败：{exc}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "msg": f"获取考试失败：{exc}"}

    # ------------------------------------------------ 执行
    def run_exam(self, p: dict) -> dict:
        if self._running:
            return {"ok": False, "msg": "已有任务在运行"}
        self._running = True
        self.logs.reset()
        threading.Thread(target=self._worker, args=(p,), daemon=True).start()
        return {"ok": True}

    def _worker(self, p: dict) -> None:
        # 严格布尔：字符串 "false" 也会被判真，这里必须只认 JSON true
        live = p.get("live") is True
        # 开始考试：真的开考取卷并答题，但不交卷（之后可用「正式交卷」提交）
        start_only = p.get("start_only") is True
        stream = _UIStream(self.logs)
        self.reset_live()
        try:
            cfg = self._quick_cfg(p)
            cfg.exam.dry_run = not (live or start_only)
            if live:
                cfg.exam.auto_submit = True
            elif start_only:
                cfg.exam.auto_submit = False
            # 前端勾选框是权威来源：每题都用 DeepSeek 搜一遍
            if "search_all" in p:
                cfg.llm.search_all_questions = p.get("search_all") is True
            if p.get("exam_id"):
                cfg.exam.exam_id = p["exam_id"]

            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                try:
                    code = run_cli(
                        cfg,
                        login_only=False,
                        user=p.get("username"),
                        pwd=p.get("password"),
                        force=False,
                        on_event=self._on_event,
                    )
                    stream.flush()
                    if code == 0:
                        self.logs.progress = 100
                        self.logs.status = "完成"
                        if live:
                            msg = "正式交卷完成"
                        elif start_only:
                            msg = "已开考并答题（未交卷），可点「正式交卷」提交"
                        else:
                            msg = "演练完成（未交卷）"
                        self.logs.done = {"ok": True, "msg": msg}
                    else:
                        self.logs.done = {
                            "ok": False,
                            "msg": f"运行结束（退出码 {code}），详见日志",
                        }
                except SystemExit:
                    stream.flush()
                    self.logs.done = {"ok": False, "msg": "已停止"}
                except AppError as exc:
                    stream.flush()
                    self.logs.add(f"[错误] {exc}", "err")
                    self.logs.done = {"ok": False, "msg": str(exc)}
        except BaseException:  # noqa: BLE001
            # 包括 KeyboardInterrupt/SystemExit：不能让 HTTP 请求悬着
            self.logs.add(traceback.format_exc(), "err")
            self.logs.done = {"ok": False, "msg": "运行异常，详见日志"}
        finally:
            self._running = False

    def open_data_dir(self) -> dict:
        import subprocess

        d = self._acc_paths().root
        d.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.Popen(["explorer", str(d)])
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "msg": str(exc)}

    # ------------------------------------------------ 答题卡（交卷前改答案）
    def _acc_paths(self):
        """当前账号的运行态路径（与 runner 写入的位置保持一致）。"""
        from src.config import paths_for, peek_account_key

        return paths_for(peek_account_key())

    def _sheet_path(self) -> Path:
        return self._acc_paths().root / "answer_sheet.json"

    def _state_path(self) -> Path:
        return self._acc_paths().state

    def _read_state(self) -> dict:
        try:
            return json.loads(self._state_path().read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def load_answer_sheet(self) -> dict:
        """读答题卡。已经交卷的场次即使有文件也不允许再改。"""
        p = self._sheet_path()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"ok": False, "msg": "还没有答题卡：先点「开始考试」把题答完再改"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "msg": f"答题卡读取失败：{exc}"}
        if not isinstance(data, dict):
            return {"ok": False, "msg": "答题卡格式异常"}
        st = self._read_state()
        if st.get("submitted") or st.get("phase") in ("submitted", "done", "submitting"):
            data["editable"] = False
            data["lock_reason"] = "该场考试已交卷，不能再修改"
        return {"ok": True, **data}

    def save_answer_sheet(self, edits: dict) -> dict:
        """把人工订正的答案写回 exam_state.json（提交体就是从这里生成的）。"""
        if self._running:
            return {"ok": False, "msg": "答题流程正在运行，等它结束再改"}
        p = self._sheet_path()
        try:
            sheet = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"ok": False, "msg": "答题卡不存在"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "msg": f"答题卡读取失败：{exc}"}

        st = self._read_state()
        if st.get("submitted") or st.get("phase") in ("submitted", "done", "submitting"):
            return {"ok": False, "msg": "该场考试已交卷，不能再修改"}
        if not st.get("examuser_id"):
            return {"ok": False, "msg": "还没有开考记录，先点「开始考试」"}

        by_qid = {str(it.get("qid")): it for it in (sheet.get("items") or [])}
        answers = st.get("answers")
        if not isinstance(answers, dict):
            answers = {}

        errors: list[str] = []
        changed = 0
        for qid, val in (edits or {}).items():
            qid = str(qid)
            it = by_qid.get(qid)
            if not it or not isinstance(val, dict):
                continue

            keys = val.get("answer")
            if isinstance(keys, str):
                keys = [s for s in keys.replace("，", ",").replace(" ", ",").split(",") if s.strip()]
            keys = [str(k).strip().upper().replace(".", "") for k in (keys or []) if str(k).strip()]
            text = str(val.get("text") or "").strip()

            valid = {str(o.get("key", "")).upper() for o in (it.get("options") or [])}
            if keys and valid:
                bad = [k for k in keys if k not in valid]
                if bad:
                    errors.append(f"第 {it.get('index')} 题：{''.join(bad)} 不是有效选项")
                    continue
                keys = [k for k in keys if k in valid]
            if it.get("qtype") in ("single", "judge") and len(keys) > 1:
                keys = keys[:1]

            old = answers.get(qid) or {}
            if list(old.get("answer") or []) == keys and str(old.get("text") or "") == text:
                it["answer"], it["text"] = keys, text
                continue

            changed += 1
            answers[qid] = {
                **old,
                "answer": keys,
                "text": text,
                "confidence": 1.0,
                "reason": "[手动修改] 交卷前人工订正",
                "synthetic": False,
                "verified": bool(old.get("verified")),
                "edited": True,
            }
            it["answer"], it["text"] = keys, text
            it["edited"] = True

        if errors:
            return {"ok": False, "msg": "；".join(errors[:3])}

        st["answers"] = answers
        atomic_write_text(self._state_path(), json.dumps(st, ensure_ascii=False, indent=2))
        atomic_write_text(p, json.dumps(sheet, ensure_ascii=False, indent=2))
        return {"ok": True, "changed": changed}


# ============================================================ HTTP 服务
def build_handler(api: GuiApi, logs: LogBuffer, token: str, port: int, url: str):
    expected_host = f"127.0.0.1:{port}"
    expected_origin = f"http://127.0.0.1:{port}"

    class Handler(BaseHTTPRequestHandler):
        server_version = "yooc-gui"

        def log_message(self, *args: Any) -> None:  # 静音访问日志
            pass

        def _authorized(self) -> bool:
            """本地服务也要鉴权：防止本机其它进程/网页 POST /api/run 触发真交卷。"""
            try:
                if self.headers.get("Host") != expected_host:
                    return False
                origin = self.headers.get("Origin")
                if origin and origin != expected_origin:
                    return False
                got = self.headers.get("X-Yooc-Token") or ""
                return secrets.compare_digest(got, token)
            except Exception:
                return False

        # ---- 工具 ----
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # 防止被其它本地/远程页面 iframe 点击劫持
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            try:
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                pass

        def _json(self, obj: Any, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                return {}

        # ---- GET ----
        def do_GET(self) -> None:  # noqa: N802
            logs.heartbeat = time.time()
            u = urlparse(self.path)
            path = u.path

            if path in ("/", "/index.html"):
                try:
                    self._send(200, ASSETS.read_bytes(), "text/html; charset=utf-8")
                except Exception as exc:  # noqa: BLE001
                    self._send(500, f"读取界面失败：{exc}".encode(), "text/plain; charset=utf-8")
                return

            if path.startswith("/api/") and not self._authorized():
                self._json({"ok": False, "msg": "unauthorized"}, 403)
                return

            if path == "/api/state":
                self._json(api.get_state())
                return

            if path == "/api/logs":
                qs = parse_qs(u.query)
                try:
                    since = int((qs.get("since") or ["0"])[0])
                except ValueError:
                    since = 0
                self._json(logs.snapshot(since))
                return

            if path == "/api/sheet":
                # 答题卡：交卷前人工核验/编辑的入口
                self._json(api.load_answer_sheet())
                return

            self._json({"ok": False, "msg": "not found"}, 404)

        # ---- POST ----
        def do_POST(self) -> None:  # noqa: N802
            logs.heartbeat = time.time()
            path = urlparse(self.path).path
            if not self._authorized():
                self._json({"ok": False, "msg": "unauthorized"}, 403)
                return
            d = self._body()

            try:
                if path == "/api/save":
                    self._json(api.save_config(d))
                elif path == "/api/login":
                    self._json(api.test_login(d))
                elif path == "/api/exams":
                    self._json(api.list_exams(d))
                elif path == "/api/run":
                    self._json(api.run_exam(d))
                elif path == "/api/sheet":
                    # 人工订正：{"edits": {qid: {"answer": ["A"], "text": ""}}}
                    self._json(api.save_answer_sheet(d.get("edits") or {}))
                elif path == "/api/open-data":
                    self._json(api.open_data_dir())
                elif path == "/api/reopen":
                    # 自动唤起失败时的手动兜底：再试一次拉浏览器
                    ok = _open_browser(url)
                    self._json(
                        {"ok": True, "opened": ok}
                        if ok
                        else {"ok": False, "msg": "还是没能唤起浏览器，请从账号目录下的 gui_url.txt 复制地址"}
                    )
                elif path == "/api/quit":
                    if api._running:
                        self._json({"ok": False, "msg": "任务运行中，不能退出"})
                        return
                    self._json({"ok": True})
                    threading.Timer(0.4, lambda: os._exit(0)).start()
                else:
                    self._json({"ok": False, "msg": "not found"}, 404)
            except BaseException as exc:  # noqa: BLE001
                # 任何异常都必须变成 JSON 返回。绝不能让请求悬着——
                # 前端没有响应就只会一直转圈，用户完全看不到发生了什么。
                msg = str(exc) or exc.__class__.__name__
                if isinstance(exc, SystemExit):
                    msg = str(exc.code) if exc.code else "内部错误"
                self._json({"ok": False, "msg": msg}, 200)

    return Handler


def _pick_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _watchdog(logs: LogBuffer, started: threading.Event, api: GuiApi) -> None:
    """浏览器关掉后轮询就停了，进程不能一直挂着。

    但任务运行中不能退出（交卷/开考进行到一半被杀会留下不确定状态）。
    另外浏览器可能拉起失败/用户手动开得晚，启动初期给一段宽限期，
    否则会出现「浏览器还没打开，进程自己退了」。
    """
    boot = time.time()
    while True:
        time.sleep(10)
        if not started.is_set():
            continue
        if api._running:
            continue
        # 启动后 10 分钟内一律不自动退出，等用户把页面打开
        if time.time() - boot < 600:
            continue
        if time.time() - logs.heartbeat > 180:
            os._exit(0)


# ============================================================ 启动
def _single_lock(key: str = ""):
    """返回 (ok, lock_obj)。

    锁名按账号隔离：同一账号只能开一个实例（避免抢 WebView2/端口、
    两个进程同时开考），不同账号可以并行各跑各的。
    """
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        name = "Global\\yooc_exam_assistant_single"
        k = str(key or "").strip()
        if k and k != "default":
            name = f"{name}_{k[:32]}"
        h = k32.CreateMutexW(None, False, name)
        if not h:
            return True, None
        if ctypes.get_last_error() == 183:
            return False, None
        return True, h
    except Exception:
        return True, None


def _msgbox(text: str, title: str, icon: int = 0x40) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, text[:900], title, icon)
    except Exception:
        pass


# 常见浏览器安装路径（兜底用，顺序即优先级）
_BROWSER_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Mozilla Firefox\firefox.exe",
    r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
]


def _boot_log(msg: str) -> None:
    """启动期诊断日志。GUI 失败时用户看不到任何东西，全靠这个文件定位。"""
    try:
        p = ROOT / "data" / "gui_boot.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _copy_to_clipboard(text: str) -> None:
    """把文字放进剪贴板，失败不影响主流程。"""
    try:
        import subprocess

        subprocess.run(
            ["clip"],
            input=text.encode("utf-16-le"),
            capture_output=True,
            check=False,
            timeout=5,
        )
    except Exception:
        pass


def _shell_open(url: str) -> bool:
    """用 ShellExecuteW 打开，能拿到真实返回值。

    os.startfile 不抛异常 ≠ 浏览器真的弹了——之前日志里记的「成功: os.startfile」
    其实不可信（它是盲的）。ShellExecuteW 返回值 > 32 才表示系统真的受理了。
    """
    try:
        import ctypes

        rc = ctypes.windll.shell32.ShellExecuteW(None, "open", url, None, None, 1)
        # 32 = SE_ERR_NOASSOC，2/5 等都表示失败
        ok = rc > 32
        _boot_log(f"ShellExecuteW 返回 {int(rc)} -> {'已受理' if ok else '被拒绝'}")
        return ok
    except Exception as exc:  # noqa: BLE001
        _boot_log(f"ShellExecuteW 异常: {exc!r}")
        return False


def _open_browser(url: str) -> bool:
    """自动打开界面页，返回是否**确认**成功。

    打包成 console=False 的 GUI 程序后，浏览器常常拉不起来：
    子进程被继承到隐藏的窗口站、或已有的浏览器实例被复用而不弹新窗口。
    所以这里按可靠性多级尝试，每级都记日志，且**不把没验证过的当成功**。
    """
    import subprocess

    attempts: list[str] = []

    def spawn(exe: str) -> bool:
        """直接拉浏览器 exe。必须带 --new-window，否则复用已有实例不会弹窗口。"""
        argv = [exe, "--new-window", url]
        try:
            subprocess.Popen(
                argv,
                close_fds=True,
                creationflags=(
                    getattr(subprocess, "DETACHED_PROCESS", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                ),
            )
            _boot_log(f"Popen 成功: {exe}")
            return True
        except Exception as exc:  # noqa: BLE001
            attempts.append(f"Popen {os.path.basename(exe)} 失败: {exc!r}")
            return False

    # 1) 用户在 config.yaml 里指定的浏览器（最优先，尊重显式配置）
    try:
        cfg_path = str(load().account.browser_path or "").strip()
    except Exception:
        cfg_path = ""
    if cfg_path and os.path.exists(cfg_path):
        _boot_log(f"尝试配置路径: {cfg_path}")
        if spawn(cfg_path):
            _boot_log("成功: 配置路径")
            return True

    # 2) 已知浏览器 exe 直启。放前面是因为它能绕开「文件关联失效」
    #    和「复用已有实例不弹窗」两个最常见的坑。
    for p in _BROWSER_PATHS:
        if not os.path.exists(p):
            continue
        _boot_log(f"尝试: {p}")
        if spawn(p):
            _boot_log(f"成功: {p}")
            return True

    # 3) ShellExecuteW：有返回值可判断，比 startfile 可靠
    if _shell_open(url):
        _boot_log("成功: ShellExecuteW")
        return True

    # 4) os.startfile 兜底（返回值不可验证，仅作最后尝试）
    try:
        os.startfile(url)  # noqa: S606
        _boot_log("尝试成功(未验证): os.startfile")
        return True
    except Exception as exc:  # noqa: BLE001
        attempts.append(f"os.startfile 失败: {exc!r}")

    # 5) 标准库
    try:
        if webbrowser.open(url):
            _boot_log("成功: webbrowser.open")
            return True
        attempts.append("webbrowser.open 返回 False")
    except Exception as exc:  # noqa: BLE001
        attempts.append(f"webbrowser.open 异常: {exc!r}")

    _boot_log("全部失败 -> " + " | ".join(attempts))
    return False


def _url_file() -> Path:
    """端口地址文件按账号存放：多账号并行时各记各的，不互相覆盖。"""
    try:
        from src.config import paths_for, peek_account_key

        return paths_for(peek_account_key()).root / "gui_url.txt"
    except Exception:
        return ROOT / "data" / "gui_url.txt"


def _read_running_url() -> str:
    """读上一次成功启动时写下的地址，用于「已在运行」提示。"""
    try:
        p = _url_file()
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""


def main() -> int:
    try:
        from src.config import peek_account_key

        _key = peek_account_key()
    except Exception:
        _key = ""
    ok, lock = _single_lock(_key)
    if not ok:
        prev = _read_running_url()
        _msgbox(
            "程序已经在运行了。\n\n"
            + (f"页面地址：\n{prev}\n\n" if prev else "")
            + "把这个地址复制到浏览器打开即可。\n"
            "如果页面打不开，打开任务管理器结束 yooc-auto.exe 后重试。",
            "yooc 考试助手 · 已在运行",
        )
        return 0

    if not ASSETS.exists():
        _msgbox(f"找不到界面文件：\n{ASSETS}", "yooc 考试助手 · 启动失败", 0x10)
        return 1

    logs = LogBuffer()
    api = GuiApi(logs)
    port = _pick_port()
    token = secrets.token_urlsafe(24)
    url = f"http://127.0.0.1:{port}/?t={token}"

    try:
        httpd = ThreadingHTTPServer(
            ("127.0.0.1", port), build_handler(api, logs, token, port, url)
        )
    except Exception as exc:  # noqa: BLE001
        _msgbox(f"启动本地服务失败：\n{exc}", "yooc 考试助手 · 启动失败", 0x10)
        return 1

    httpd.daemon_threads = True

    ready = threading.Event()

    def serve() -> None:
        ready.set()
        httpd.serve_forever()

    threading.Thread(target=serve, daemon=True).start()

    logs.heartbeat = time.time()
    threading.Thread(target=_watchdog, args=(logs, ready, api), daemon=True).start()

    ready.wait(5)
    time.sleep(0.3)

    # 记录地址：浏览器拉不起来时用户还能手动找回来
    try:
        _uf = _url_file()
        _uf.parent.mkdir(parents=True, exist_ok=True)
        _uf.write_text(url, encoding="utf-8")
    except Exception:
        pass

    if not _open_browser(url):
        # 拉不起浏览器时必须给出可操作的下一步，否则用户只看到「双击没反应」
        _copy_to_clipboard(url)
        _msgbox(
            "没能自动打开浏览器。\n\n"
            f"地址已复制到剪贴板，直接到浏览器里按 Ctrl+V 打开：\n\n{url}\n\n"
            "如果粘贴后打不开，去 Edge/Chrome 里新建一个标签页再粘贴。\n"
            "（程序已在后台运行，关掉本提示不影响使用）",
            "yooc 考试助手 · 请手动打开页面",
            0x40,
        )

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
