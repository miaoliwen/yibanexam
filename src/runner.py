"""主流程编排：登录 → 选卷 → 开考/续跑 → 取卷 → 答题 → 交卷 → 查分。

v2 的鲁棒性设计（单次机会考试，核心目标是不丢机会、不多耗机会）：

1. **状态机持久化**：每个关键动作前先落盘 `data/exam_state.json`
   （atomic write），进程被杀后重跑能接着走，而不是重新开考。
2. **start 幂等**：开考前先查本地状态和服务端列表里的 examuserId；
   有就直接复用。start 请求超时/结果未知时，先回查服务端，确认不了就停手，
   绝不盲目重发（可能白扔一次机会）。
3. **submit 幂等**：交卷前检查服务端是否已有交卷标记；交卷超时先核实再决定
   是否重试；仍无法确认就报 `SubmitUnconfirmed` 并提示人工核对，绝不重复提交。
4. **单实例锁**：data/run.lock 防止 GUI/CLI 同时跑同一场考试。
5. **登录复用 + 熔断**：优先复用 session.json，避免反复登录触发平台限频。
6. **截止时间预算**：按考试时长算出硬截止，逐题节奏器在截止前保留安全垫；
   时间不够时停止答题、兜底猜、立即交卷。
7. **降级不中断**：大模型单批失败逐题重试，仍失败的选择题兜底猜一个；
   浏览器写通道起不来则回落纯 HTTP；token 过期自动续期一次。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import random
import re
import time
from pathlib import Path
from typing import Any, Callable

from . import humanize
from .api import YoocApi
from .config import DATA_DIR, Config, summary_lines
from .errors import (
    AppError,
    AuthError,
    ApiError,
    ConfigError,
    ExamWindowError,
    LLMConfigError,
    LoginError,
    RiskControlError,
    SessionError,
    StateError,
    SubmitUnconfirmed,
    TransientError,
)
from .crypto import encrypt_score
from .llm import LLMSolver
from .login import (
    LoginGuard,
    load_saved,
    load_saved_cookies,
    password_login,
    validate_saved,
)
from .parser import Question, apply_answer_key, extract_examuser_id, parse_paper
from .qbank import QuestionBank, resolve_questions
from .session import EXAM_BASE, SessionError, YoocSession
from .state import ExamState, RunLock, StateStore, log_line

DATA = DATA_DIR
TYPE_LABEL = {
    "single": "单选",
    "multi": "多选",
    "judge": "判断",
    "fill": "填空",
    "essay": "简答",
}

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_SESSION = 3
EXIT_LOGIN = 4
EXIT_WINDOW = 5
EXIT_SUBMIT = 6
EXIT_INTERRUPT = 130

STATE_PATH = DATA / "exam_state.json"
LOCK_PATH = DATA / "run.lock"
RUN_LOG = DATA / "run.log"

# 账号隔离：run() 启动时把下面两个指向当前账号目录，_log/_dump 跟着走。
# 模块常量保留为默认值（未配置账号 / 单元测试直接 patch 时仍可用）。
_DATA_DIR = DATA
_LOG_PATH = RUN_LOG


# ============================================================ 工具
def _log(msg: str) -> None:
    log_line(_LOG_PATH, msg)


def _dump(name: str, obj: Any) -> Path:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    p = _DATA_DIR / name
    try:
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, TypeError) as exc:
        print(f"[warn] 落盘 {name} 失败：{exc}")
    return p


def _nums(payload: Any) -> list[dict]:
    """从 list/get 返回里挖出考试数组。"""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in ("list", "records", "examList", "items", "data"):
            v = data.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def _pick_key(d: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def _exam_title(e: dict) -> str:
    return str(_pick_key(e, ("examName", "name", "title", "examTitle")) or "(未命名考试)")


def _exam_id(e: dict) -> Any:
    return _pick_key(e, ("examId", "id", "exam_id"))


def _parse_time(value: Any) -> float | None:
    """把各种时间表示统一成 epoch 秒（本地时区）。无法解析返回 None。"""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e12:  # 毫秒
            v /= 1000.0
        return v if v > 0 else None
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        v = float(s)
        if v > 1e12:
            v /= 1000.0
        return v if v > 0 else None
    s2 = s.replace("/", "-")
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d",
    ):
        try:
            d = _dt.datetime.strptime(s2, fmt)
            if d.tzinfo is not None:
                return d.timestamp()
            return d.timestamp()
        except ValueError:
            continue
    return None


def _fmt_delta(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}小时{m}分"
    if m:
        return f"{m}分{s}秒"
    return f"{s}秒"


def _stable_index(qid: Any, index: int, n: int) -> int:
    if n <= 0:
        return 0
    h = hashlib.sha256(f"{qid}|{index}".encode("utf-8")).digest()
    return h[0] % n


# ============================================================ 选卷
def select_exam(cfg: Config, api: YoocApi) -> dict:
    payload = api.exam_list()
    _dump("exam_list_raw.json", payload)
    items = _nums(payload)
    if not items:
        raise ExamWindowError(
            "考试列表为空。请确认：\n"
            "  1) group_id 是否正确\n"
            "  2) 该分组下是否真的有你参加的考试\n"
            f"  原始返回已存到 {_DATA_DIR / 'exam_list_raw.json'}"
        )

    print("\n可参加的考试：")
    for i, e in enumerate(items, 1):
        print(f"  [{i}] {_exam_title(e)}   (examId={_exam_id(e)})")

    if cfg.exam.exam_id:
        want = str(cfg.exam.exam_id)
        for e in items:
            if str(_exam_id(e)) == want:
                return e
        raise ExamWindowError(f"配置的 exam_id={want} 不在可参加列表中")

    if cfg.exam.exam_name_keyword:
        kw = cfg.exam.exam_name_keyword
        hits = [e for e in items if kw in _exam_title(e)]
        if not hits:
            raise ExamWindowError(f"没有标题包含「{kw}」的考试")
        if len(hits) > 1:
            print(f"      ⚠ 有 {len(hits)} 场考试匹配「{kw}」，取第一场：{_exam_title(hits[0])}")
        return hits[0]

    if len(items) == 1:
        chosen = items[0]
        print(f"\n自动选择（列表仅一场）：{_exam_title(chosen)}")
        return chosen

    if cfg.exam.auto_select_first:
        print(f"\n⚠ auto_select_first=true，自动选择第一场：{_exam_title(items[0])}")
        return items[0]

    listing = "\n".join(
        f"  [{i}] {_exam_title(e)} (examId={_exam_id(e)})"
        for i, e in enumerate(items, 1)
    )
    raise ExamWindowError(
        "考试列表里有多场考试，未指定要考哪一场，已停止（避免开错考试）。\n"
        f"{listing}\n"
        "  请任选其一：\n"
        "    - python main.py --exam-id <examId>\n"
        "    - python main.py --keyword <标题关键字>\n"
        "    - 或设 exam.auto_select_first: true（明确接受自动选第一场）"
    )


# ============================================================ 答案体（对齐前端 eR）
def _ui_answer_value(q: Question) -> dict:
    """前端 examAnswers 里单题的 value 结构。

    choice:          {1: ["下标字符串"]}
    multiplechoice:  {1: ["下标", ...]}
    completion:      inputs=="1" -> {1: [答案...]}；多空 -> {1:[a], 2:[b]}
    """
    if q.options:
        order = {o.key.upper(): str(i) for i, o in enumerate(q.options)}
        idx = [order[k] for k in q.answer_keys if k.upper() in order]
        if q.qtype == "multi":
            return {"1": idx}
        return {"1": idx[:1]}
    parts = [p for p in re.split(r"\s*\|\|\|\s*", q.answer_text or "") if p.strip()]
    inputs = str(q.raw.get("inputs") or "")
    if q.qtype == "fill" and inputs and inputs != "1":
        return {str(i + 1): [p] for i, p in enumerate(parts)}
    return {"1": parts}


def _correctness(q: Question) -> int:
    """1 正确 / -1 错误 / 0 无判分依据（对应前端 eR 的 _ 字段）。"""
    if not q.has_key:
        return 0
    if q.options:
        if not q.answer_keys:
            return 0
        return (
            1
            if {k.upper() for k in q.answer_keys}
            == {k.upper() for k in q.key_keys}
            else -1
        )
    if q.qtype == "fill":
        a = [p.strip() for p in re.split(r"\s*\|\|\|\s*", q.answer_text or "") if p.strip()]
        b = [p.strip() for p in re.split(r"\s*\|\|\|\s*", q.key_text or "") if p.strip()]
        if not a:
            return 0
        return 1 if a == b else -1
    return 0


def local_correct_total(questions: list[Question]) -> float:
    return sum(float(q.score or 0) for q in questions if _correctness(q) == 1)


def build_submit_body(
    examuser_id: Any,
    questions: list[Question],
    as_string: bool,
    shuffle: bool = False,
    score: str = "0",
) -> dict:
    """构造真实前端的提交体。

    answer = JSON object：{subjectId: [{选项下标/填空值...}, correctness]}
    前端 wrapper 会对每个 FormData 字段再 JSON.stringify 一次，所以 answer 先
    json.dumps 成字符串，page_fetch 再 stringify → 与真实请求体一致（双层编码）。
    """
    answers: dict[str, Any] = {}
    for q in questions:
        if not q.answered:
            continue
        answers[str(q.qid)] = [_ui_answer_value(q), _correctness(q)]
    if shuffle:
        items = list(answers.items())
        random.shuffle(items)
        answers = dict(items)
    return {
        # 字段顺序对齐前端 mutate({answer, score, examuserId})
        "answer": json.dumps(answers, ensure_ascii=False) if as_string else answers,
        "score": score,
        "examuserId": examuser_id,
    }


# ============================================================ 展示
def print_paper(questions: list[Question]) -> None:
    print("\n" + "=" * 72)
    print(f" 共 {len(questions)} 题")
    print("=" * 72)
    for q in questions:
        flag = "" if q.answered else "  ⚠ 未作答"
        print(
            f"\n[{q.index}] ({TYPE_LABEL.get(q.qtype, q.qtype)}"
            f"{f' {q.score:g}分' if q.score else ''}){flag}"
        )
        print(f"    题干：{q.stem[:160]}")
        for o in q.options:
            print(f"      {o.key}. {o.text[:90]}")
        ans = q.answer_display() or "—"
        conf = f"{q.confidence:.0%}" if q.confidence else "—"
        print(f"    → 答案：{ans}   置信度：{conf}   {q.reason}")


def summary(questions: list[Question]) -> str:
    by_type: dict[str, int] = {}
    low = 0
    blank = 0
    for q in questions:
        by_type[q.qtype] = by_type.get(q.qtype, 0) + 1
        if not q.answered:
            blank += 1
        elif q.confidence and q.confidence < 0.5:
            low += 1
    parts = [f"{TYPE_LABEL.get(k, k)} {v}" for k, v in by_type.items()]
    return f"{' / '.join(parts)}；未作答 {blank}，低置信 {low}"


# ============================================================ 服务端事实
def _server_examuser(api: YoocApi, exam_id: Any) -> tuple[dict | None, str | None]:
    """查考试列表，返回 (该考试条目, 服务端已有 examuserId)。"""
    try:
        payload = api.exam_list()
    except AppError:
        raise
    except Exception:
        return None, None
    for e in _nums(payload):
        if str(_exam_id(e)) == str(exam_id):
            uid = e.get("examuserId") or e.get("examUserId") or e.get("userExamId")
            if uid not in (None, "", 0, "0"):
                return e, str(uid)
            return e, None
    return None, None


_SUBMIT_MARKERS = {
    "submittime",
    "submittedat",
    "submit_time",
    "finishtime",
    "finish_time",
    "submitted",
    "commit_time",
    "committime",
}
_SUBMIT_STATUS = {
    "已交卷",
    "已提交",
    "已完成",
    "完成",
    "已结束",
    "submitted",
    "finished",
    "done",
}


def _payload_submitted(payload: Any) -> bool:
    """保守判断返回体里是否有"已交卷"的明确标记。没有明确标记一律不算。"""
    stack = [payload]
    seen = 0
    while stack and seen < 4000:
        cur = stack.pop()
        seen += 1
        if isinstance(cur, dict):
            for k, v in cur.items():
                kl = str(k).lower()
                if kl in _SUBMIT_MARKERS and v not in (None, "", 0, "0", False):
                    return True
                if kl in ("status", "state", "examstatus", "exam_status"):
                    sv = str(v).strip().lower()
                    if sv in _SUBMIT_STATUS:
                        return True
                    # 前端映射：1 待开始 / 2 未答题 / 3 已答题 / 4 已完成 / 5 已结束
                    if sv in ("4", "5"):
                        return True
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return False


def _server_says_submitted(api: YoocApi, exam_id: Any) -> bool:
    try:
        return _payload_submitted(api.get_result(exam_id))
    except AppError:
        return False
    except Exception:
        return False


# ============================================================ 时间与截止
def _extract_duration_minutes(*payloads: Any) -> float:
    keys = (
        "duration",
        "durationMinutes",
        "duration_minutes",
        "limitTime",
        "timeLimit",
        "examTime",
        "limitMinutes",
    )
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        containers = [payload, payload.get("data")]
        for box in containers:
            if not isinstance(box, dict):
                continue
            for k in keys:
                v = box.get(k)
                if v in (None, ""):
                    continue
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    continue
                if 0 < f <= 24 * 60:
                    return f
                # 有些模板用秒（3600）存时长，换算成分钟
                if 24 * 60 < f <= 24 * 3600:
                    return f / 60.0
    return 0.0


def _extract_expired(*payloads: Any) -> float | None:
    keys = ("expired", "expireTime", "endTime", "examEndTime", "deadline", "end_time")
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for box in (payload, payload.get("data")):
            if not isinstance(box, dict):
                continue
            for k in keys:
                if k in box:
                    ts = _parse_time(box.get(k))
                    if ts:
                        return ts
    return None


def _compute_deadline(
    cfg: Config,
    exam: dict,
    detail: Any,
    setting: Any,
    start_epoch: float,
) -> tuple[float, float]:
    """返回 (截止 epoch, 时长分钟)。取所有已知约束里最早的那个。"""
    candidates: list[float] = []
    duration = _extract_duration_minutes(exam, detail, setting)
    expired = _extract_expired(exam, detail, setting)
    if expired:
        candidates.append(expired)
    if duration > 0:
        candidates.append(start_epoch + duration * 60)
    if not candidates:
        candidates.append(start_epoch + 3600)  # 兜底按 60 分钟
    deadline = min(candidates)
    return deadline, duration


def _attempt_start_epoch(state: ExamState | None, item: dict | None, now: float) -> float:
    if state and state.started_at:
        return min(state.started_at, now)
    if item:
        # 注意：不要用 createTime/列表创建时间 —— 那可能是几天前，
        # 会把「本次作答截止」算到过去，导致直接交白卷。
        for k in ("startTime", "startedAt", "beginTime", "examStartTime"):
            ts = _parse_time(item.get(k))
            if ts and ts <= now:
                return ts
    return now


# ============================================================ 登录
def _login_flow(
    cfg: Config,
    *,
    user: str | None,
    pwd: str | None,
    wait_seconds: int,
    login_only: bool,
    session_factory: Callable[[Config], Any],
    login_fn: Callable[..., dict],
    validate_fn: Callable[[Config, dict], bool | None],
    guard: LoginGuard,
) -> tuple[dict[str, str], Any | None]:
    mode = (cfg.account.login_mode or "auto").lower()
    username = user or cfg.account.username
    password = pwd or cfg.account.password
    can_pwd = bool(username and password)
    creds: dict[str, str] | None = None
    sess = None
    early = None

    # 0) 先把写通道浏览器起起来：用它的真实 UA 覆盖 HTTP 头。
    #    否则同一账号会出现「登录接口 Chrome/149、后面请求 Chrome/155」的 UA 漂移。
    if (
        not login_only
        and mode != "browser"
        and cfg.account.browser_for_writes != "never"
    ):
        print("[1/7] 启动浏览器通道（统一 UA，start/submit 走页面内 fetch）…")
        try:
            early = session_factory(cfg)
            # 必须用有头浏览器：Chrome 无头模式的 UA 是 HeadlessChrome/155，
            # 这本身就是最硬的检测特征之一；有头模式才是正常 Chrome/155。
            early.open(headless=False)
            ua = str(getattr(early, "ua", "") or "")
            if "HeadlessChrome" in ua:
                print("      ⚠ 检测到 HeadlessChrome UA，已从 HTTP 头中去除无头标记")
                ua = ua.replace("HeadlessChrome", "Chrome")
            if ua:
                cfg.http.user_agent = ua
            print(f"      ok UA={cfg.http.user_agent[:70]}")
        except Exception as exc:  # noqa: BLE001
            early = None
            if cfg.account.browser_for_writes == "always":
                raise SessionError(
                    "browser_for_writes=always，但浏览器通道启动失败：\n"
                    f"  {exc}\n"
                    "请安装 Chrome，或把 account.browser_for_writes 改成 auto/never。"
                ) from exc
            print(f"      ⚠ 浏览器通道不可用，回退纯 HTTP：{exc}")

    # 1) 复用保存的凭据（不再登录，避免制造高频登录特征）
    if cfg.account.reuse_session:
        saved = load_saved(cfg)
        if saved:
            print("[1/7] 发现已保存的登录态，校验中…")
            ok = validate_fn(cfg, saved)
            if ok is True:
                creds = saved
                print("      ok 直接复用（未重新登录）")
            elif ok is None:
                creds = saved
                print("      ⚠ 网络原因无法校验，先复用；失效会自动续期一次")
            else:
                print("      保存的凭据已失效，需要重新登录")

    # 2) 登录：有浏览器时优先用浏览器表单（真实 TLS/登录页资源/Cookie）
    if creds is None and mode in ("auto", "password") and can_pwd:
        if early is not None and cfg.account.login_transport in ("auto", "browser"):
            print("[1/7] 浏览器表单登录（真实登录页 + 真实 TLS/Cookie）…")
            guard.precheck()
            try:
                c = early.form_login(username, password)
            except Exception:  # noqa: BLE001
                c = None
            guard.record_attempt()
            ok_creds = bool(c and c.get("user_token") and c.get("user_id"))
            if ok_creds:
                creds = c
                print(f"      ok  userId={creds.get('user_id')}（浏览器表单）")
            elif cfg.account.login_transport == "browser":
                raise LoginError(
                    "浏览器表单登录失败（login_transport=browser）。\n"
                    "  请检查账号密码，或把 account.login_transport 改成 auto/http。",
                    kind="credentials",
                )
            else:
                print("      浏览器表单登录失败，回退 HTTP 登录")
                # form_login 失败也算一次登录尝试；等过本地最小间隔，避免
                # password_login 的 precheck 立刻把回退挡掉（R-05）
                time.sleep(max(1, cfg.account.login_min_interval_seconds))

    if creds is None and mode in ("auto", "password") and can_pwd:
        if cfg.account.login_transport == "browser" and early is None and mode != "browser":
            raise LoginError(
                "login_transport=browser，但浏览器通道不可用。\n"
                "  请安装 Chrome，或把 login_transport 改成 auto/http。",
                kind="config",
            )
        print("[1/7] 账号密码登录（直连登录接口，不过前端验证码）…")
        try:
            creds = login_fn(cfg, username, password, guard=guard)
            print(
                f"      ok  userId={creds.get('user_id')}  "
                f"yibanId={creds.get('yiban_id')}  token={str(creds.get('user_token', ''))[:12]}…"
            )
        except LoginError as exc:
            if mode == "password":
                raise
            print(f"      失败：{exc}")
            print("      改用浏览器登录…")
    elif creds is None and mode == "password":
        raise LoginError(
            "login_mode=password，但 config.yaml 里没有 account.username / account.password",
            kind="config",
        )

    # 3) 浏览器人工登录（browser 模式，或 auto 下密码登录失败）
    if creds is None:
        if early is not None:
            try:
                early.close()
            except Exception:
                pass
            early = None
            time.sleep(0.5)  # 等 profile 锁释放
        print("\n[1/7] 浏览器登录…")
        sess = session_factory(cfg)
        sess.open(headless=cfg.account.headless)
        try:
            creds = sess.ensure_login(wait_seconds=wait_seconds)
        except BaseException:
            sess.close()
            raise
        ua = str(getattr(sess, "ua", "") or "")
        if "HeadlessChrome" in ua:
            ua = ua.replace("HeadlessChrome", "Chrome")
        if ua:
            cfg.http.user_agent = ua
        print(
            f"      ok  userId={creds.get('user_id')}  "
            f"token={str(creds.get('user_token', ''))[:12]}…"
        )

    # 4) 把登录凭据注入写通道浏览器，确认页面处于登录态
    if early is not None:
        try:
            cookies = load_saved_cookies(cfg)
            # 旧版 session.json 没有 cookies 键（v1 schema）。此时浏览器只能靠 profile
            # 里残留的登录态；一旦 profile 失效，整场会静默降级成纯 HTTP。
            # 这里主动补一次浏览器表单登录，把 cookie/persistent profile 一起刷新。
            if (
                not cookies
                and can_pwd
                and cfg.account.login_transport in ("auto", "browser")
            ):
                print("[1/7] 已保存凭据没有 cookie，补一次浏览器表单登录…")
                try:
                    guard.precheck()
                    c0 = early.form_login(username, password, wait_seconds=18)
                    guard.record_attempt()
                except LoginError as exc:
                    c0 = None
                    print(f"      ⚠ 补登录被本地熔断拦住：{exc}")
                except Exception:
                    c0 = None
                if c0 and c0.get("user_token"):
                    creds = {**creds, **{k: v for k, v in c0.items() if v}}
                    cookies = load_saved_cookies(cfg)
                    print("      ok 已补齐 cookie")
            if cookies:
                n = early.inject_cookies(cookies)
                print(f"      已注入 {n} 个 cookie")
            elif creds.get("user_token"):
                # 最后一招：用已保存的 query 凭据伪造同名 cookie。
                # 服务端同时接受 cookie / query 鉴权；页面能停留在考试域，
                # 写通道就能继续用（平台侧看到的是页面内 fetch）。
                cookies = [
                    {"name": "user_token", "value": creds.get("user_token", "")},
                    {"name": "user_id", "value": creds.get("user_id", "")},
                    {"name": "yiban_id", "value": creds.get("yiban_id", "")},
                ]
                n = early.inject_cookies(cookies)
                print(f"      已注入 {n} 个凭据 cookie（兼容旧 session）")
            early.goto_exams()
            if early.verify_login():
                sess = early
                c = early.read_creds()
                if c.get("user_token"):
                    creds = {**creds, **{k: v for k, v in c.items() if v}}
                print("      ok 浏览器通道就绪（write 走页面内 fetch）")
            else:
                print("      ⚠ 浏览器内未识别到登录态，回退纯 HTTP")
                early.close()
                if cfg.account.browser_for_writes == "always":
                    raise SessionError(
                        "browser_for_writes=always，但浏览器内未识别到登录态。\n"
                        "可改用 login_mode: browser 让浏览器自己完成登录。"
                    )
        except SessionError as exc:
            try:
                early.close()
            except Exception:
                pass
            if cfg.account.browser_for_writes == "always":
                raise
            print(f"      ⚠ 浏览器写通道不可用，回退纯 HTTP：{exc}")
        except Exception as exc:  # noqa: BLE001
            try:
                early.close()
            except Exception:
                pass
            if cfg.account.browser_for_writes == "always":
                raise SessionError(f"浏览器写通道初始化失败：{exc}") from exc
            print(f"      ⚠ 浏览器写通道不可用，回退纯 HTTP：{exc}")

    assert creds is not None
    return creds, sess


def _make_refresh(
    cfg: Config,
    login_fn: Callable[..., dict],
    validate_fn: Callable[[Config, dict], bool | None],
    guard: LoginGuard,
    sess: Any | None,
) -> Callable[[], dict[str, str] | None]:
    """登录态失效时续期一次：保存的凭据 → 浏览器已有凭据 → 重新登录。"""

    def refresh() -> dict[str, str] | None:
        print("[auth] 登录态失效，尝试续期…")
        saved = load_saved(cfg)
        if saved and validate_fn(cfg, saved) is True:
            print("[auth] 复用保存的凭据完成续期")
            return saved
        if sess is not None:
            try:
                sess.goto_exams()
                c = sess.read_creds()
                if c.get("user_token") and validate_fn(cfg, c) is True:
                    print("[auth] 从浏览器上下文取到新凭据")
                    return c
            except Exception:
                pass
            # 浏览器表单登录（比裸 HTTP 更接近真实用户）
            if cfg.account.login_transport in ("auto", "browser"):
                username = cfg.account.username
                password = cfg.account.password
                if username and password:
                    try:
                        guard.precheck()
                        c = sess.form_login(username, password, wait_seconds=15)
                        guard.record_attempt()
                        if c and c.get("user_token"):
                            try:
                                sess.goto_exams()
                            except Exception:
                                pass
                            print("[auth] 浏览器表单续期成功")
                            return c
                    except Exception:
                        pass
        username = cfg.account.username
        password = cfg.account.password
        if username and password and cfg.account.login_mode in ("auto", "password"):
            try:
                c = login_fn(cfg, username, password, guard=guard)
                print("[auth] 重新登录成功")
                return c
            except LoginError as exc:
                print(f"[auth] 续期失败：{exc}")
        return None

    return refresh


# ============================================================ 开考/续跑
def _resolve_examuser_after_start(
    api: YoocApi,
    exam_id: Any,
    attempts: int = 4,
    delays: tuple[float, ...] | None = None,
) -> str | None:
    delay_plan = delays or tuple(1.0 + i for i in range(max(1, attempts)))
    for i in range(max(1, attempts)):
        try:
            _item, uid = _server_examuser(api, exam_id)
        except Exception:
            uid = None
        if uid:
            return uid
        if i < len(delay_plan):
            time.sleep(delay_plan[i])
    return None


def _looks_ended(msg: str) -> bool:
    return any(w in (msg or "") for w in ("已交卷", "已提交", "已完成", "已结束", "不存在", "无效"))


def _fetch_resume_paper(
    api: YoocApi, examuser_id: Any, state: ExamState, store: StateStore, tag: str
) -> tuple[Any | None, bool]:
    """续跑时拉试卷。返回 (paper, ended)。ended=True 表示服务端已结束该作答。"""
    try:
        paper = api.get_paper(examuser_id)
        _dump(f"paper_raw_{tag}.json", paper)
        print("      ok 作答记录仍有效，跳过开考")
        return paper, False
    except (AuthError, TransientError):
        raise
    except ApiError as exc:
        msg = str(exc)
        if _looks_ended(msg):
            print(f"      服务器显示该记录已结束：{msg}")
            state.phase = "submitted"
            state.submitted = True
            store.save(state)
            return None, True
        raise StateError(
            f"续跑失败：{msg}\n"
            "为避免重复开考，已停止。请先到网页确认作答状态后再重跑。"
        ) from exc


_CAPTCHA_WORDS = ("验证码", "验证", "数美", "风控", "captcha", "rid", "请完成", "滑块")


def _looks_like_captcha(msg: str) -> bool:
    low = (msg or "").lower()
    return any(w in low for w in _CAPTCHA_WORDS)


def _browser_captcha_start(
    sess: Any, api: YoocApi, exam_id: Any, cfg: Config, timeout: int = 240
) -> str | None:
    """start 被数美验证码拦截时，打开真实 take 页让用户手动过验证。

    take 页会自己调用 shumei/captcha/check → start/action；这里只轮询
    examuserId 并在成功后把页面导航走（避免它的超时自动交卷逻辑）。
    """
    if sess is None:
        return None
    url = f"{EXAM_BASE}/group/{cfg.account.group_id}/exam/{exam_id}/take"
    print("      ⚠ 开考请求被验证码拦截。已打开真实考试页，请在浏览器里完成验证。")
    print(f"        页面：{url}")
    try:
        sess.goto(url)
    except Exception as exc:  # noqa: BLE001
        print(f"        打开 take 页失败：{exc}")
        return None
    deadline = time.time() + max(30, timeout)
    while time.time() < deadline:
        try:
            _item, uid = _server_examuser(api, exam_id)
        except Exception:
            uid = None
        if uid:
            print(f"      ok 验证通过，服务端已创建作答记录 examuserId={uid}")
            try:
                sess.goto_exams()
            except Exception:
                pass
            return str(uid)
        alive = getattr(sess, "page_alive", lambda: True)()
        if not alive:
            print("      ⚠ 浏览器页面被关闭，验证中止")
            return None
        time.sleep(3)
    print("      ⚠ 等待验证超时（未检测到作答记录）")
    return None


def _ensure_started(
    cfg: Config,
    api: YoocApi,
    exam: dict,
    exam_id: Any,
    state: ExamState,
    store: StateStore,
    *,
    force_start: bool = False,
    user_id: Any = None,
    sess: Any = None,
) -> tuple[ExamState, Any | None]:
    """保证有 examuserId 且是「续跑」而不是重复开考。

    返回 (state, paper_prefetched)。paper 不为 None 表示续跑时顺便拉到了试卷。
    """
    exam_id_s = str(exam_id)

    # 已交卷的本地记录：直接返回，交给调用方查成绩，绝不重新开考
    if state.phase in ("submitted", "done") and state.examuser_id:
        return state, None

    # A. 本地已有该场考试的作答记录
    if state.examuser_id:
        if not cfg.exam.resume:
            raise StateError(
                "检测到未完成的作答记录（examuserId="
                f"{state.examuser_id}），但 exam.resume=false。\n"
                "为避免重复开考消耗机会，已停止。\n"
                "  继续作答：把 exam.resume 改回 true 后重跑；\n"
                f"  确认要放弃：删除 {cfg.paths.state}（下次会重新开考，通常要消耗新机会）。"
            )
        print(f"[3/7] 检测到未完成作答记录 examuserId={state.examuser_id}，尝试续跑…")
        paper, ended = _fetch_resume_paper(
            api, state.examuser_id, state, store, "resume"
        )
        if ended:
            return state, None
        return state, paper

    # B. 服务端已经有 examuserId（本地状态丢了也能捡回来）
    item, server_uid = _server_examuser(api, exam_id)
    if server_uid and not cfg.exam.resume:
        raise StateError(
            f"服务端已存在作答记录 examuserId={server_uid}，但 exam.resume=false。\n"
            "为避免重复开考，已停止；把 exam.resume 改回 true 即可续跑。"
        )
    if server_uid:
        if _payload_submitted(item):
            # 服务端已有交卷标记：只查成绩，绝不续跑/重交
            print(f"[3/7] 服务端记录 examuserId={server_uid} 已交卷，跳过作答。")
            state.examuser_id = server_uid
            state.phase = "submitted"
            state.submitted = True
            state.submit_confirmed = True
            store.save(state)
            return state, None
        print(f"[3/7] 服务端已有作答记录 examuserId={server_uid}，复用之（不重新开考）…")
        state.examuser_id = server_uid
        state.phase = "started"
        if not state.started_at:
            state.started_at = _attempt_start_epoch(state, item, time.time())
        store.save(state)
        paper, ended = _fetch_resume_paper(api, server_uid, state, store, "resume")
        if ended:
            return state, None
        return state, paper

    # C. 真正开考。先写「意图」再发请求：即使请求途中进程被杀，
    #    下一次启动也能看到 phase=starting 并先回查服务端，而不是直接重发。
    if state.phase == "starting" and not force_start:
        print("      ⚠ 上次开考请求结果未知，先回查服务端（最长约 25s）…")
        server_uid = _resolve_examuser_after_start(
            api, exam_id, attempts=6, delays=(2, 3, 4, 5, 6, 0)
        )
        if server_uid:
            print(f"      回查成功 examuserId={server_uid}，继续（未重复开考）")
            state.examuser_id = server_uid
            state.phase = "started"
            store.save(state)
            paper = api.get_paper(server_uid)
            _dump("paper_raw_resume.json", paper)
            return state, paper
        # 查不到 = 开考结果未知。默认绝不再发 start（服务端可能延迟创建记录）。
        raise StateError(
            "上次开考请求结果未知，服务端暂时查不到作答记录。\n"
            "为避免重复消耗考试机会，本次没有重发开考请求。\n"
            "  请先到网页确认：\n"
            "    - 已经进入考试 → 重跑本程序会自动续跑；\n"
            "    - 确实没有开考 → 加 --force-start 重跑（会重新调用 start）。"
        )
    if state.phase == "starting" and force_start:
        print("      --force-start：跳过未知状态回查，直接重新开考")

    print("[4/7] 开始考试（创建作答记录）…")
    state.phase = "starting"
    state.started_at = time.time()
    state.last_error = ""
    store.save(state)

    try:
        started = api.start_exam(exam_id, user_id)
    except TransientError as exc:
        # 请求可能已经生效：先回查，确认不了就停手
        print(f"      ⚠ 开考请求结果未知（{exc}），回查服务端…")
        server_uid = _resolve_examuser_after_start(api, exam_id, attempts=3)
        if server_uid:
            print(f"      回查成功 examuserId={server_uid}，继续（未重复开考）")
            state.examuser_id = server_uid
            state.phase = "started"
            store.save(state)
            paper = api.get_paper(server_uid)
            return state, paper
        state.last_error = str(exc)
        store.save(state)
        raise StateError(
            "开考请求超时，且服务端暂时查不到作答记录。\n"
            "为避免重复消耗考试机会，已停止，未重发开考请求。\n"
            "  请先到网页确认是否已进入考试，再重跑本程序（会自动续跑）。"
        ) from exc
    except AuthError:
        raise
    except ApiError as exc:
        msg = str(exc)
        if sess is not None and cfg.exam.captcha_browser_fallback and _looks_like_captcha(msg):
            uid = _browser_captcha_start(sess, api, exam_id, cfg)
            if uid:
                state.examuser_id = uid
                state.phase = "started"
                state.started_at = time.time()
                store.save(state)
                paper = api.get_paper(uid)
                _dump("paper_raw_resume.json", paper)
                return state, paper
        state.last_error = msg
        state.phase = "idle"
        store.save(state)
        raise ExamWindowError(
            f"开考失败：{exc}\n"
            f"原始返回：{_DATA_DIR / 'start_raw.json'}"
        ) from exc

    _dump("start_raw.json", started)
    if isinstance(started, dict) and started.get("result") is False:
        state.phase = "idle"
        store.save(state)
        raise ExamWindowError(
            f"开考失败：{started.get('message')}\n"
            f"原始返回：{_DATA_DIR / 'start_raw.json'}"
        )

    examuser_id = extract_examuser_id(started)
    if examuser_id is None:
        examuser_id = _resolve_examuser_after_start(api, exam_id, attempts=3)
        if examuser_id:
            print("      （从列表接口补取 examuserId）")
    if examuser_id is None:
        state.last_error = "start 返回中解析不到 examuserId"
        store.save(state)
        raise StateError(
            f"未能从开考返回中解析 examuserId，原始返回见 {_DATA_DIR / 'start_raw.json'}\n"
            "可能原因：考试未开始、次数已用完，或尚未加入该课群。\n"
            "为避免重复开考，已停止；请先到网页确认。"
        )
    state.examuser_id = str(examuser_id)
    state.phase = "started"
    store.save(state)
    print(f"      examuserId={state.examuser_id}")
    return state, None


# ============================================================ 答题
def _record_answer(state: ExamState, q: Question, synthetic: bool = False) -> None:
    state.answers[str(q.qid)] = {
        "answer": list(q.answer_keys),
        "text": q.answer_text,
        "confidence": round(float(q.confidence or 0.0), 4),
        "reason": (q.reason or "")[:120],
        # 兜底猜测标记：正式交卷的断点恢复会忽略它们并重新答题
        "synthetic": bool(synthetic),
        "verified": bool(getattr(q, "verified", False)),
        "search_checked": bool(getattr(q, "search_checked", False)),
        "evidence": (getattr(q, "evidence", "") or "")[:300],
        "edited": bool(getattr(q, "edited", False)),
    }


def _resolve_bank(
    cfg: Config,
    questions: list[Question],
    emit: Callable[..., None] | None = None,
    overwrite: bool | None = None,
) -> dict:
    """本地题库命中：拉卷后先跑一遍，命中就直接填答案。

    优先级在平台解密答案之后（平台答案更权威）、大模型之前。
    题库文件缺失/损坏只告警，绝不影响整场考试。
    """
    if not getattr(cfg, "bank", None) or not cfg.bank.enabled:
        return {"enabled": False}

    path = Path(cfg.bank.path)
    if not path.exists():
        print(f"      ⚠ 题库文件不存在（{path}），跳过题库命中")
        print("        生成：python main.py --build-bank 答案.txt")
        return {"enabled": False, "reason": "missing"}

    try:
        bank = QuestionBank.load(path)
    except Exception as exc:  # noqa: BLE001
        print(f"      ⚠ 题库加载失败，跳过：{exc}")
        return {"enabled": False, "reason": "load_failed"}

    st = resolve_questions(
        bank,
        questions,
        overwrite=cfg.bank.overwrite if overwrite is None else bool(overwrite),
        min_score=cfg.bank.min_score,
    )
    stats = st.to_dict()
    if emit:
        emit("bank", **stats)

    if st.applied:
        print(
            f"      题库命中 {st.matched}/{st.total} 题，直接填答 {st.applied} 题"
            + (f"（替换旧答案 {st.replaced} 题）" if st.replaced else "")
            + f"（题库 {st.bank_count} 题，{st.bank_name}）"
        )
    elif st.matched:
        print(f"      题库命中 {st.matched}/{st.total} 题，但已有更权威答案，未覆盖")
    else:
        print(f"      题库未命中任何题目（题库 {st.bank_count} 题）")
    if st.kept:
        print(f"        保留权威答案 {st.kept} 题（平台答案/人工修改）"
              + (f"，其中 {st.conflict} 题与题库不一致（以现有答案为准）" if st.conflict else ""))
    if st.missed:
        print(f"        未命中 {st.missed} 题 → 交给大模型")
    if (
        st.applied
        and st.total
        and st.applied >= st.total
        and getattr(cfg.bank, "mode", "prefer") != "llm_first"
    ):
        print(
            "        （题库已覆盖整卷，本场不会调用大模型；"
            "要模型先判卷+搜索核实请加 --llm-first）"
        )
    return stats


def _dump_answer_sheet(state: ExamState, questions: list[Question]) -> None:
    """把当前作答情况落成「答题卡」，供 GUI 交卷前查看/修改。

    这份文件是给人看的，也是编辑入口；真正的提交体仍由 exam_state.json 生成。
    """
    items: list[dict] = []
    for q in questions:
        a = state.answers.get(str(q.qid)) or {}
        items.append(
            {
                "qid": str(q.qid),
                "index": q.index,
                "qtype": q.qtype,
                "stem": q.stem,
                "options": [{"key": o.key, "text": o.text} for o in q.options],
                "score": q.score,
                "answer": list(a.get("answer") or []),
                "text": str(a.get("text") or ""),
                "confidence": a.get("confidence", 0.0),
                "reason": a.get("reason", ""),
                "verified": bool(a.get("verified")),
                "search_checked": bool(a.get("search_checked")),
                "evidence": a.get("evidence", ""),
                "edited": bool(a.get("edited")),
            }
        )
    payload = {
        "exam_id": state.exam_id,
        "exam_name": state.exam_name,
        "examuser_id": state.examuser_id,
        "phase": state.phase,
        "deadline_at": state.deadline_at,
        "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "editable": state.phase in ("started", "solving", "solved") and not state.submitted,
        "count": len(items),
        "verified_count": sum(1 for it in items if it["verified"]),
        "items": items,
    }
    _dump("answer_sheet.json", payload)


def _restore_answers(
    state: ExamState, questions: list[Question], include_synthetic: bool = True
) -> int:
    restored = 0
    for q in questions:
        a = state.answers.get(str(q.qid))
        if not isinstance(a, dict):
            continue
        if a.get("synthetic") and not include_synthetic:
            # 演练阶段的兜底猜测不能带进正式交卷
            continue
        keys = [str(k).upper() for k in (a.get("answer") or []) if str(k).strip()]
        text = str(a.get("text") or "")
        valid = {o.key.upper() for o in q.options}
        if keys and valid:
            keys = [k for k in keys if k in valid]
        if not keys and not text:
            continue
        q.answer_keys = keys
        q.answer_text = text
        try:
            q.confidence = float(a.get("confidence") or 0.0)
        except (TypeError, ValueError):
            q.confidence = 0.0
        q.reason = "[断点恢复] " + str(a.get("reason") or "")[:80]
        q.verified = bool(a.get("verified"))
        q.search_checked = bool(a.get("search_checked"))
        q.evidence = str(a.get("evidence") or "")[:300]
        q.edited = bool(a.get("edited"))
        restored += 1
    return restored


def _guess_answer(q: Question) -> None:
    """大模型没答出来时的兜底：选择题稳定地猜一个（不猜必 0 分）。"""
    if q.answered:
        return
    if q.options:
        i = _stable_index(q.qid, q.index, len(q.options))
        q.answer_keys = [q.options[i].key.upper()]
        q.confidence = 0.01
        q.reason = "[兜底猜测]"
    # 填空/简答无法猜，留空


def _heartbeat(api: YoocApi, examuser_id: Any, state: ExamState, store: StateStore) -> None:
    try:
        api.get_saved_answers(examuser_id)
        state.last_heartbeat_at = time.time()
        store.save(state)
    except AuthError:
        # 续期也失败才向上抛；交给主流程带着断点状态停下
        raise
    except Exception as exc:  # noqa: BLE001
        # 心跳失败（含限频）不该中断答题：继续把答案做完，交卷前还会再核实一次
        print(f"\n      ⚠ 心跳失败（继续答题）：{exc}")


def _solve_loop(
    cfg: Config,
    solver: Any,
    questions: list[Question],
    state: ExamState,
    store: StateStore,
    api: YoocApi,
    examuser_id: Any,
    pacer: humanize.Pacer,
    progress=None,
) -> None:
    pending = [q for q in questions if not q.answered]
    total = len(questions)
    done = total - len(pending)
    chunk_size = max(1, int(cfg.llm.batch_size))
    last_hb = time.time()
    since_hb = 0
    errors: list[str] = []

    for start in range(0, len(pending), chunk_size):
        if pacer.exhausted():
            print("\n      ⚠ 时间预算不足，停止答题，未答题目进入兜底")
            break
        remain = pacer.remaining_seconds()
        if remain != float("inf"):
            # 一次模型调用最坏要 timeout×max_retries，不能贴着截止时间再开新批次
            need = (
                cfg.exam.min_seconds_before_submit
                + max(30, int(cfg.llm.timeout)) * max(1, int(cfg.llm.max_retries) // 2)
                + 15
            )
            if remain <= need:
                print(
                    f"\n      ⚠ 距截止仅剩 {_fmt_delta(remain)}，不再开新批次，准备交卷"
                )
                break
        chunk = pending[start : start + chunk_size]
        try:
            res = solver.solve_batch(chunk)
            solver.apply(chunk, res)
        except LLMConfigError:
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"批次失败：{exc}")
            print(f"\n      ⚠ 批次失败，逐题重试：{exc}")
            for q in chunk:
                if pacer.exhausted():
                    break
                try:
                    solver.apply([q], solver.solve_batch([q]))
                except LLMConfigError:
                    raise
                except Exception as exc2:  # noqa: BLE001
                    errors.append(f"{q.qid}: {exc2}")

        for q in chunk:
            _record_answer(state, q)
            store.save(state)
            done += 1
            if progress:
                progress(done, total)
            # 逐题节奏：LLM 花的时间计入，截止前保留安全垫
            if not pacer.exhausted():
                pacer.wait_next()
            since_hb += 1
            hb_q = int(cfg.exam.heartbeat_questions or 0)
            hb_s = int(cfg.humanize.heartbeat_seconds or 0)
            if (hb_q > 0 or hb_s > 0) and (
                (hb_q > 0 and since_hb >= hb_q)
                or (hb_s > 0 and time.time() - last_hb >= hb_s)
            ):
                _heartbeat(api, examuser_id, state, store)
                last_hb = time.time()
                since_hb = 0

    if errors:
        print(f"\n      ⚠ {len(errors)} 个批次/题目失败，未答题目将兜底")


# ============================================================ 搜索核实
def _search_phase(
    cfg: Config,
    solver: Any,
    questions: list[Question],
    state: ExamState,
    store: StateStore,
    deadline: float | None,
    emit: Callable[..., None],
    staged: bool,
    clock: Callable[[], float] = time.time,
) -> dict:
    """联网搜索核实。

    staged 流程下最多跑 cfg.llm.search_rounds 轮，每轮只处理上一轮还没核过的题；
    非 staged 保持原来的单轮行为。改不改判仍由 _refine_one 的置信度闸门决定。
    """
    st = {"searched": 0, "revised": 0, "failed": 0, "unverified": 0, "verified": 0}
    if solver is None or getattr(solver, "offline", False):
        return st
    if not getattr(solver, "searcher", None) or getattr(solver, "_search_off", False):
        emit(
            "search",
            phase="unavailable",
            reason="未执行搜索（未启用 / 接口不可用 / 剩余时间不足）",
        )
        return st

    all_q = bool(getattr(cfg.llm, "search_all_questions", False))
    thr = cfg.llm.search_confidence_threshold
    rounds = max(1, int(getattr(cfg.llm, "search_rounds", 1))) if staged else 1

    for rd in range(rounds):
        if deadline is not None and deadline - clock() < 120:
            print("\n      ⚠ 剩余时间不足 2 分钟，停止搜索核实")
            break

        if staged:
            low = [
                q
                for q in questions
                if q.answered and not getattr(q, "search_checked", False)
            ]
        elif all_q:
            low = [
                q for q in questions if q.answered and not getattr(q, "verified", False)
            ]
        else:
            low = [
                q
                for q in questions
                if q.answered
                and q.confidence < thr
                and not getattr(q, "verified", False)
            ]

        if not low:
            if rd == 0:
                print("\n      所有题目都已有核实依据，跳过搜索。")
            break

        cap = cfg.llm.search_max_questions
        will = len(low) if cap <= 0 else min(len(low), cap)
        if staged:
            print(
                f"\n      搜索核实 第 {rd + 1}/{rounds} 轮："
                f"{len(low)} 题未核实，本次取 {will} 题…"
            )
        elif all_q:
            print(
                f"\n      全量核实：{len(low)} 题待核实，本次取 {will} 题…"
            )
        else:
            print(
                f"\n      低置信 {len(low)} 题（<{thr:.0%}），取 {will} 题做搜索核实…"
            )

        def rprogress(done: int, total: int) -> None:
            pct = done * 100 // max(total, 1)
            bar = "█" * (pct // 4) + "░" * (25 - pct // 4)
            print(f"\r      [{bar}] {pct:3d}%  {done}/{total}", end="", flush=True)
            emit("search", done=done, total=total, **solver.search_stats)

        emit("search", phase="start", total=will, all=all_q, round=rd + 1)
        solver.on_search_result = lambda q, ok, note: emit(
            "search_item",
            index=getattr(q, "index", 0),
            qid=str(getattr(q, "qid", "")),
            stem=(getattr(q, "stem", "") or "")[:42],
            ok=bool(ok),
            note=note,
        )
        try:
            st = solver.refine_low_confidence(
                questions, progress=rprogress, deadline=deadline, force=staged
            )
        finally:
            solver.on_search_result = None
        emit("search", phase="done", round=rd + 1, **st)

        print(
            f"\n      搜索 {st['searched']} 次，修正 {st['revised']} 题"
            + (f"，失败 {st['failed']} 次" if st["failed"] else "")
        )
        for q in questions:
            _record_answer(state, q)
        store.save(state)

        if getattr(solver, "_search_off", False):
            print("      （搜索接口不可用，后续已自动关闭）")
            break

    if st.get("unverified"):
        print(
            f"      ⚠ {st['unverified']} 题没搜到可用资料，保留原答案"
            "（答比空好；要强制留空设 exam.skip_unverified: true）"
        )
    return st


# ============================================================ 交卷
def _ensure_not_submitted(api: YoocApi, exam_id: Any, state: ExamState) -> bool:
    if state.submitted or state.phase in ("submitted", "done"):
        return True
    if _server_says_submitted(api, exam_id):
        state.submitted = True
        state.phase = "submitted"
        state.submit_confirmed = True
        return True
    return False


def _confirm_submitted(api: YoocApi, exam_id: Any, state: ExamState) -> bool:
    for i in range(3):
        if _server_says_submitted(api, exam_id):
            state.submit_confirmed = True
            return True
        if i < 2:
            time.sleep(2.0 + i)
    return False


def _submit(
    cfg: Config,
    api: YoocApi,
    exam_id: Any,
    body: dict,
    state: ExamState,
    store: StateStore,
    sess: Any | None,
    pacer: humanize.Pacer,
) -> int:
    preview = _dump("submit_payload_preview.json", body)
    print(f"\n      提交体预览已保存：{preview}")

    if cfg.exam.dry_run:
        state.phase = "solved"
        store.save(state)
        print("\n[7/7] dry_run=true —— 已停止，未提交。")
        print("      确认答案和提交体无误后，改 config.yaml 的 exam.dry_run: false")
        print(f"      examuserId={state.examuser_id}（重跑会自动续跑，不会重复开考）")
        return EXIT_OK

    if not cfg.exam.auto_submit:
        # manual_review：第一次答完停在这里等人工核验；之后再跑一次时
        # phase 已经是 solved，说明用户是点了「正式交卷」来的，必须放行，
        # 否则 manual_review 会永久挡住交卷。
        if getattr(cfg.exam, "manual_review", False) and state.phase == "solved":
            print("\n[7/7] 人工核验完成，按答题卡提交…")
        else:
            state.phase = "solved"
            store.save(state)
            if getattr(cfg.exam, "manual_review", False):
                print("\n[7/7] manual_review=true —— 已停在交卷前，等人工核验答题卡。")
                print("      1) 打开界面「答题卡」面板，逐题核对；答案可直接改，改完点保存；")
                print("      2) 确认无误后点「正式交卷」提交；")
                print("      3) 不放心可以先去平台网页看一眼，再回来交。")
            else:
                print("\n[7/7] auto_submit=false —— 已停在交卷前，不自动交卷。")
            print(f"      examuserId={state.examuser_id}")
            print(f"      答题卡：{cfg.paths.root / 'answer_sheet.json'}")
            print("      要提交：点「正式交卷」（或命令行加 --live 重跑），"
                  "会自动续跑同一份记录，不会重新开考；")
            print("      也可以到网页上手动提交。")
            return EXIT_OK

    # 交卷前先确认服务端没有已交卷标记（防止上一轮其实已经交上去了）
    if _ensure_not_submitted(api, exam_id, state):
        store.save(state)
        print("\n[7/7] 服务端已显示交卷，跳过重复提交。")
        return EXIT_OK

    # 上一次交卷结果未知（进程被杀/超时）：绝不自动重发，先核实再让人决定
    if (
        state.phase == "submitting"
        and state.submit_attempts >= 1
        and not state.submitted
        and not cfg.exam.force_resubmit
    ):
        print("\n      检测到上次交卷请求结果未知，先核实服务端…")
        if _confirm_submitted(api, exam_id, state):
            state.submitted = True
            state.phase = "submitted"
            store.save(state)
            print("      服务端已确认交卷，不重复提交。")
            return EXIT_OK
        raise SubmitUnconfirmed(
            "上次交卷请求结果未知，且当前查不到明确的交卷标记。\n"
            "为避免重复交卷，本次不自动提交。\n"
            "  请立即到网页确认：\n"
            "    - 显示已交卷 → 无需操作；\n"
            "    - 显示未交卷 → 直接在网页交卷，或设 exam.force_resubmit: true 后重跑。"
        )

    remaining = pacer.remaining_seconds()
    review_base = max(0, int(cfg.exam.submit_review_seconds))
    # 交卷前停留时间也带随机性：固定 8s 会让「交卷时刻」变成可预测的常数
    review = (
        int(round(review_base * random.uniform(0.6, 1.45))) if review_base > 0 else 0
    )
    if remaining != float("inf"):
        review = int(min(review, max(0, remaining - 15)))
    if review > 0:
        print(f"\n      交卷前停留 {review}s 检查卷面…")
        if sess is not None:
            try:
                sess.hover_idle(review * 1000)
            except Exception:
                time.sleep(review)
        else:
            time.sleep(review)

    print("[7/7] 交卷…")
    state.phase = "submitting"
    state.submit_attempts += 1
    store.save(state)

    resp: Any = None
    confirmed = False
    try:
        resp = api.submit_exam(body)
    except RiskControlError as exc:
        state.last_error = f"交卷被风控：{exc}"
        store.save(state)
        raise
    except AuthError:
        raise
    except ApiError as exc:
        # 服务端明确拒绝（格式/权限/重复等）：不盲目重发，先看是不是其实已交卷
        state.last_error = str(exc)
        store.save(state)
        if _confirm_submitted(api, exam_id, state):
            confirmed = True
            print("      服务端已确认交卷，忽略本次报错")
        else:
            state.phase = "solved"
            store.save(state)
            raise
    except TransientError as exc:
        # 交卷超时/网络错误：服务端可能已经收到。绝不重发 —— 先核实，查不到就交给人。
        state.last_error = str(exc)
        store.save(state)
        print(f"      ⚠ 交卷请求异常：{exc}")
        time.sleep(2.5)
        if _confirm_submitted(api, exam_id, state):
            confirmed = True
            print("      服务端已确认收到交卷")
        else:
            state.phase = "submitting"
            store.save(state)
            raise SubmitUnconfirmed(
                "交卷请求已发出，但无法确认服务端是否收到（网络/超时）。\n"
                "为避免重复交卷，程序不会自动重发。\n"
                "  请立即到网页查看成绩/作答状态：\n"
                "    - 显示已交卷 → 无需操作；\n"
                "    - 显示未交卷 → 直接在网页交卷，或设 exam.force_resubmit: true 后重跑。"
            ) from exc

    if resp is not None:
        _dump("submit_raw.json", resp)
        print(f"      返回：{json.dumps(resp, ensure_ascii=False)[:300]}")

    state.submitted = True
    state.phase = "submitted"
    state.last_error = ""
    if not confirmed:
        confirmed = _confirm_submitted(api, exam_id, state)
    state.submit_confirmed = bool(confirmed)
    store.save(state)

    if not confirmed:
        print(
            "\n      ⚠ 交卷请求已成功返回，但未查到明确的交卷标记。\n"
            "        请到网页确认成绩；程序已记录状态，不会自动重复提交。"
        )

    time.sleep(random.uniform(1.5, 3.0))
    try:
        result = api.get_result(exam_id)
        _dump("result_raw.json", result)
        print(f"      成绩：{json.dumps(result, ensure_ascii=False)[:400]}")
    except Exception as exc:  # noqa: BLE001
        print(f"      成绩查询失败（不影响已交卷）：{exc}")

    state.phase = "done"
    store.save(state)
    print("\n完成。")
    return EXIT_OK


# ============================================================ 主流程
def run(
    cfg: Config,
    login_only: bool = False,
    wait_seconds: int = 600,
    user: str | None = None,
    pwd: str | None = None,
    force: bool = False,
    force_start: bool = False,
    *,
    session_factory: Callable[[Config], Any] | None = None,
    login_fn: Callable[..., dict] | None = None,
    validate_fn: Callable[[Config, dict], bool | None] | None = None,
    solver_factory: Callable[[Config], Any] | None = None,
    api_factory: Callable[..., YoocApi] | None = None,
    store: StateStore | None = None,
    lock: RunLock | None = None,
    clock: Callable[[], float] = time.time,
    sleep_fn: Callable[[float], None] = time.sleep,
    on_event: Callable[[str, dict], None] | None = None,
) -> int:
    session_factory = session_factory or YoocSession
    login_fn = login_fn or password_login
    validate_fn = validate_fn or validate_saved
    solver_factory = solver_factory or LLMSolver
    api_factory = api_factory or YoocApi

    # 账号隔离：状态/锁/日志/调试落盘全部走当前账号目录
    global _DATA_DIR, _LOG_PATH
    paths = cfg.paths
    # 人工复核 = 答完停住，等人在答题卡上核验后再交卷
    if getattr(cfg.exam, "manual_review", False):
        cfg.exam.auto_submit = False
    _DATA_DIR = paths.root
    _LOG_PATH = paths.log
    store = store or StateStore(paths.state)
    lock = lock or RunLock(paths.lock)

    def emit(kind: str, **data: Any) -> None:
        """把阶段状态推给上层（GUI 状态条）。回调出错绝不影响答题。"""
        if on_event is None:
            return
        try:
            on_event(kind, data)
        except Exception:  # noqa: BLE001
            pass

    print("=" * 72)
    print(" yooc 考试自动化（硬化版）")
    print("=" * 72)
    for line in summary_lines(cfg):
        print(f" {line}")
    print("=" * 72)

    if not lock.acquire():
        print(
            f"[错误] 另一个实例正在运行（{paths.lock} 被占用）。\n"
            "  同一场考试只能有一个进程操作，避免重复开考/交卷。\n"
            f"  如果确认没有其他实例，请删除 {paths.lock} 后重试。"
        )
        return EXIT_SESSION

    sess = None
    try:
        guard = LoginGuard(
            cfg=cfg,
            max_attempts=cfg.account.login_max_attempts,
            window_seconds=cfg.account.login_window_seconds,
            min_interval_seconds=cfg.account.login_min_interval_seconds,
        )
        creds, sess = _login_flow(
            cfg,
            user=user,
            pwd=pwd,
            wait_seconds=wait_seconds,
            login_only=login_only,
            session_factory=session_factory,
            login_fn=login_fn,
            validate_fn=validate_fn,
            guard=guard,
        )

        refresh = _make_refresh(cfg, login_fn, validate_fn, guard, sess)
        api_kwargs: dict[str, Any] = {"on_auth_error": refresh}
        if sess is not None:
            api_kwargs["session"] = sess
            api_kwargs["cookies"] = load_saved_cookies(cfg)
        api = api_factory(cfg, creds, **api_kwargs)
        _log(f"login ok user={creds.get('user_id')}")

        # ---- 2. 接口自检 ----
        print("\n[2/7] 校验接口凭据…")
        try:
            chk = api.user_check()
            _dump("user_check_raw.json", chk)
            if isinstance(chk, dict) and chk.get("result") is False:
                raise LoginError(f"接口凭据无效：{chk.get('message')}", kind="api")
            print("      ok")
        except (ApiError, AuthError) as exc:
            raise LoginError(f"接口凭据无效：{exc}", kind="api") from exc

        # 正式交卷前先把「不可能成功」的前提拦在开考之前，避免白耗机会
        solver_ready = None
        if not login_only and not cfg.exam.dry_run and not cfg.llm.allow_guess_without_key:
            if not cfg.llm.api_key:
                raise ConfigError(
                    "准备正式交卷，但没有配置 llm.api_key —— 开考后无法答题，"
                    "会白白消耗一次机会。\n"
                    "  请先在 config.yaml 填写 llm.api_key；\n"
                    "  或先用 exam.dry_run: true 演练；\n"
                    "  确实只想兜底猜：设 llm.allow_guess_without_key: true。"
                )
            print("\n[2/7] 模型预检（确认 key/端点可用，避免开考后才发现）…")
            solver_ready = solver_factory(cfg)
            if getattr(cfg.llm, "preflight", True) and hasattr(solver_ready, "preflight"):
                solver_ready.preflight()

        if login_only:
            print("\n[--login-only] 登录态与接口凭据均已验证通过，已停止。")
            print(f"  userId   = {creds.get('user_id')}")
            print(f"  token    = {str(creds.get('user_token', ''))[:16]}…")
            print(f"  yibanId  = {creds.get('yiban_id')}")
            print(f"  凭据已存 = {cfg.paths.session}")
            return EXIT_OK

        # ---- 3. 选卷 + 时间窗 ----
        print("\n[3/7] 获取考试列表…")
        exam = select_exam(cfg, api)
        exam_id = _exam_id(exam)
        exam_title = _exam_title(exam)
        print(f"      目标：{exam_title}  (examId={exam_id})")

        auto = cfg.exam.auto_start and not force
        _check_window(
            cfg,
            exam,
            title=exam_title,
            force=force,
            auto_wait=auto,
            now=clock(),
        )
        if auto:
            # 目标时刻可能来自考试列表 started，也可能来自 exam.start_at
            target, _src = _resolve_start_target(cfg, exam)
            if target is not None and clock() < target:
                # 自动开考：挂到开考时刻再继续，不用人到点守着点按钮
                exam = _wait_for_start(
                    cfg, api, exam, exam_id, exam_title, clock=clock, sleep_fn=sleep_fn
                )
                # 等完再核一次窗口，防止等待期间被改期 / 已经截止
                _check_window(cfg, exam, title=exam_title, force=force, now=clock())

        detail: Any = None
        setting: Any = None
        try:
            detail = api.exam_detail(exam_id)
            _dump("exam_detail_raw.json", detail)
        except Exception as exc:  # noqa: BLE001
            print(f"      detail 查询跳过：{exc}")
        try:
            setting = api.exam_setting(exam_id)
            _dump("exam_setting_raw.json", setting)
        except Exception as exc:  # noqa: BLE001
            print(f"      setting 查询跳过：{exc}")

        # ---- 状态 + 开考/续跑 ----
        state = store.load()
        if state is None:
            state = ExamState(group_id=cfg.account.group_id)
        if state.exam_id and state.exam_id != str(exam_id):
            print(
                f"      ⚠ 本地状态属于另一场考试（examId={state.exam_id}），"
                "已重置本地状态文件（不影响旧考试的服务端记录）"
            )
            state = ExamState(group_id=cfg.account.group_id)
        state.exam_id = str(exam_id)
        state.exam_name = exam_title
        state.group_id = cfg.account.group_id
        store.save(state)

        # 演练安全闸：没有作答记录时，dry_run 绝不 start。
        # 「演练」如果也开考，第一次运行就把唯一一次机会消耗掉了。
        if cfg.exam.dry_run and not cfg.exam.allow_start_in_dry_run and not state.examuser_id:
            _item, server_uid = _server_examuser(api, exam_id)
            if not server_uid:
                print("\n[4/7] 演练安全模式：只做只读预检，不调用 start/action")
                _dry_preflight_summary(cfg, exam, detail, setting)
                return EXIT_OK
            print(f"      服务端已有作答记录 examuserId={server_uid}，演练复用该记录（不再开考）")

        state, prefetched_paper = _ensure_started(
            cfg,
            api,
            exam,
            exam_id,
            state,
            store,
            force_start=force_start,
            user_id=creds.get("user_id"),
            sess=sess,
        )

        if state.phase in ("submitted", "done") and prefetched_paper is None:
            confirmed = state.submit_confirmed or _confirm_submitted(
                api, exam_id, state
            )
            if confirmed:
                print("\n[完成] 该考试已交卷，无需重复操作。")
                try:
                    result = api.get_result(exam_id)
                    _dump("result_raw.json", result)
                    print(f"      成绩：{json.dumps(result, ensure_ascii=False)[:400]}")
                except Exception:
                    pass
                return EXIT_OK
            if not cfg.exam.force_resubmit:
                print(
                    "\n      ⚠ 本地记录显示已交卷，但服务端查不到明确的交卷标记。\n"
                    "        请先到网页确认成绩/作答状态：\n"
                    "          - 已交卷 → 可直接退出；\n"
                    "          - 未交卷 → 设 exam.force_resubmit: true 后重跑，会重新交卷。"
                )
                return EXIT_SUBMIT
            # 用户明确要求重交：回到交卷流程（不会重新开考，复用 examuserId）
            print("      force_resubmit=true：重新进入交卷流程（复用 examuserId，不重新开考）")
            state.phase = "solved"
            state.submitted = False
            state.submit_confirmed = False
            store.save(state)

        # ---- 截止时间 ----
        now = clock()
        start_epoch = _attempt_start_epoch(state, exam, now)
        if not state.started_at:
            state.started_at = start_epoch
        deadline, duration = _compute_deadline(cfg, exam, detail, setting, start_epoch)
        # 用本地状态里的开始时间更准（服务端 started 是考试窗口，不是作答开始）
        if state.started_at:
            duration_deadline = (
                state.started_at + duration * 60 if duration > 0 else None
            )
            if duration_deadline:
                deadline = min(deadline, duration_deadline)
        state.deadline_at = deadline
        state.duration_minutes = duration
        store.save(state)
        print(
            f"      截止：{_dt.datetime.fromtimestamp(deadline).strftime('%Y-%m-%d %H:%M:%S')}"
            f"（剩余 {_fmt_delta(deadline - now)}"
            + (f"，时长 {duration:g} 分钟" if duration else "")
            + "）"
        )

        # 断点恢复时如果作答时间早就过了，绝不能悄悄交一张白卷
        if deadline <= now:
            msg = (
                "本次作答时间已过"
                f"（截止 {_dt.datetime.fromtimestamp(deadline).strftime('%Y-%m-%d %H:%M:%S')}）。\n"
                "继续运行只会把现有答案（可能是空白/兜底猜测）提交上去，通常按超时计分。\n"
                "  请先到网页确认作答状态；\n"
                "  确实要强行提交请加 --force。"
            )
            if not force:
                raise ExamWindowError(msg)
            print(f"      ⚠ {msg.splitlines()[0]}（--force：继续）")

        # ---- 5. 取卷 ----
        print("\n[5/7] 拉取试卷…")
        if prefetched_paper is not None:
            paper = prefetched_paper
        else:
            paper = api.get_paper(state.examuser_id)
            _dump("paper_raw.json", paper)
        questions, sections = parse_paper(
            paper.get("data") if isinstance(paper, dict) else paper
        )
        if not questions:
            _dump("paper_raw.json", paper)
            raise AppError(
                f"试卷解析出 0 道题。原始数据见 {_DATA_DIR / 'paper_raw.json'}，"
                "请把该文件发我，我按真实结构调 parser。"
            )
        restored = _restore_answers(
            state, questions, include_synthetic=cfg.exam.dry_run
        )
        print(f"      解析到 {len(questions)} 题（{summary(questions)}）")
        if restored:
            print(f"      从断点恢复 {restored} 题答案，未答题目继续作答")
            # 历史版本可能把平台占位答案（整卷同一个字母）存进了断点；
            # 这种「恢复」会把错误答案一路带到交卷，必须先清掉。
            if restored >= 8:
                cand = [q for q in questions if q.answered and not q.edited]
                sigs = {"".join(q.answer_keys) or q.answer_text for q in cand}
                legacy_key = bool(cand) and all(
                    ("题库答案" in (q.reason or "")) or ("平台答案" in (q.reason or ""))
                    for q in cand
                )
                if legacy_key and len(sigs) <= 1:
                    dropped = 0
                    for q in cand:
                        q.answer_keys = []
                        q.answer_text = ""
                        q.confidence = 0.0
                        q.reason = ""
                        q.verified = False
                        q.evidence = ""
                        state.answers.pop(str(q.qid), None)
                        dropped += 1
                    if dropped:
                        store.save(state)
                        print(
                            f"      ⚠ 断点里 {dropped} 题答案完全相同（{next(iter(sigs))}），"
                            "疑似历史占位答案，已丢弃并重新作答"
                        )

        # paper.subject.answer 是前端本地判分用的加密正确答案（key 由 yibanId 派生）
        key_report: list[str] = []
        keyed = apply_answer_key(questions, creds.get("yiban_id"), report=key_report)
        for msg in key_report:
            print(f"      ⚠ {msg}")
        if keyed:
            print(f"      解密到 {keyed} 题平台答案")
            if cfg.exam.use_answer_key:
                # 再做一次整卷校验：真答案不可能整卷同一个字母
                uniq = {"".join(q.key_keys) or q.key_text for q in questions if q.has_key}
                if keyed >= 8 and len(uniq) <= 1:
                    print(
                        f"      ⚠ 平台解密出的 {keyed} 题答案完全相同（{uniq}），"
                        "判定为占位数据，已忽略，交给题库/模型作答"
                    )
                else:
                    for q in questions:
                        if q.has_key:
                            q.answer_keys = list(q.key_keys)
                            q.answer_text = q.key_text
                            q.confidence = 1.0
                            q.reason = "[平台答案]"
                            _record_answer(state, q)
                    store.save(state)
                    print("      已优先采用平台答案；没有平台答案的题继续走题库/模型")

        # ---- 本地题库命中（平台答案之后、大模型之前）----
        # llm_first 模式故意跳过这一步：先让模型把整卷判一遍（推理+搜索都会跑），
        # 交卷前再用题库校正，兼顾「模型真的在判题」和「答案以题库为准」。
        if getattr(cfg.bank, "mode", "prefer") == "llm_first":
            print("      题库模式 llm_first：先由模型判卷，交卷前再用题库校正")
        else:
            # staged：题库优先定答。overwrite=True 让它能覆盖平台解密答案，
            # 人工改过的题在 resolve_questions 里另有保护，不会被覆盖。
            bstats = _resolve_bank(
                cfg,
                questions,
                emit=emit,
                overwrite=True if getattr(cfg.bank, "mode", "prefer") == "staged" else None,
            )
            if bstats.get("applied"):
                for q in questions:
                    if q.answered:
                        _record_answer(state, q)
                store.save(state)

        _dump("paper_sections.json", sections)
        _dump("saved_answers_raw.json", state.answers)

        # ---- 6a. staged 流程第 2 步：模型二次校验已有答案 ----
        # 题库/平台/断点给出的答案先让模型独立再判一遍；只有模型高置信且结论
        # 不同才改判，人工改过的题（edited）一律跳过。
        staged = getattr(cfg.bank, "mode", "prefer") == "staged"
        # 已经答完停在交卷前（phase=solved）的这一次运行，就是「来交卷的」：
        # 直接按现有答案提交，不重跑校验/搜索（否则每次交卷都要再烧一遍 token）。
        # 例外：答题卡里还有兜底猜测（synthetic），正式交卷必须重新作答，
        # 不能把猜测直接提交上去。
        _has_synthetic = any(
            isinstance(a, dict) and a.get("synthetic")
            for a in (state.answers or {}).values()
        )
        resume_submit = (
            state.phase == "solved"
            and bool(state.examuser_id)
            and not cfg.exam.dry_run
            and not _has_synthetic
        )
        if resume_submit:
            print("\n[6/7] 检测到已完成的答题卡（phase=solved），直接进入交卷，不重跑校验。")
        if staged and not resume_submit and bool(getattr(cfg.llm, "verify_bank_answers", True)):
            answered_now = [q for q in questions if q.answered]
            if answered_now:
                if solver_ready is None:
                    try:
                        solver_ready = solver_factory(cfg)
                    except LLMConfigError as exc:
                        if not cfg.exam.dry_run:
                            raise
                        print(f"      ⚠ {exc}\n      演练模式：跳过模型二次校验")
                        solver_ready = None
                if solver_ready is not None and not getattr(solver_ready, "offline", False):
                    print(
                        f"\n[6a/7] 模型二次校验已有答案（{len(answered_now)} 题，"
                        f"阈值 {cfg.llm.verify_min_confidence:.0%}）…"
                    )
                    emit("phase", name="verify", total=len(answered_now))

                    def vprogress(done: int, total: int) -> None:
                        pct = done * 100 // max(total, 1)
                        bar = "█" * (pct // 4) + "░" * (25 - pct // 4)
                        print(f"\r      [{bar}] {pct:3d}%  {done}/{total}", end="", flush=True)
                        emit("verify", done=done, total=total)

                    try:
                        vst = solver_ready.verify_answers(
                            questions, progress=vprogress, deadline=deadline
                        )
                    except LLMConfigError as exc:
                        print(f"\n      ⚠ 二次校验不可用：{exc}\n        保留题库/平台答案继续")
                        vst = {"checked": 0, "changed": 0, "agreed": 0, "failed": 0}
                    print(
                        f"\n      校验 {vst['checked']} 题：一致 {vst['agreed']}，"
                        f"改判 {vst['changed']}"
                        + (f"，失败 {vst['failed']}" if vst.get("failed") else "")
                    )
                    for q in questions:
                        if q.answered:
                            _record_answer(state, q)
                    store.save(state)

        # ---- 6b. 大模型答题（只处理仍然空着的题）----
        pending = [q for q in questions if not q.answered]
        solver = solver_ready
        if pending and not resume_submit:
            print(
                f"\n[6/7] 大模型答题中（并发 {cfg.llm.concurrency}，每批 {cfg.llm.batch_size} 题）…"
            )
            if solver is None:
                try:
                    solver = solver_factory(cfg)
                except LLMConfigError as exc:
                    if cfg.exam.dry_run:
                        print(f"      ⚠ {exc}\n      演练模式：跳过模型，使用兜底猜测")
                        solver = None
                    else:
                        raise

            if solver is not None and not getattr(solver, "offline", False):
                pending_now = [q for q in questions if not q.answered]
                difficulties = [
                    humanize.question_difficulty(q.stem, len(q.options), q.qtype)
                    for q in pending_now
                ]
                pacer = humanize.Pacer(
                    cfg.humanize,
                    started_at=clock(),
                    total_questions=len(pending_now),
                    deadline_at=deadline,
                    reserve_seconds=cfg.exam.deadline_margin_seconds,
                    max_total_seconds=cfg.exam.max_solve_seconds,
                    difficulties=difficulties,
                    clock=clock,
                    sleep=sleep_fn,
                )

                def progress(done: int, total: int) -> None:
                    pct = done * 100 // max(total, 1)
                    bar = "█" * (pct // 4) + "░" * (25 - pct // 4)
                    print(f"\r      [{bar}] {pct:3d}%  {done}/{total}", end="", flush=True)
                    emit("solve", done=done, total=total)

                emit("phase", name="solve", total=len(pending_now))
                t0 = clock()
                solve_aborted = False
                try:
                    _solve_loop(
                        cfg,
                        solver,
                        questions,
                        state,
                        store,
                        api,
                        state.examuser_id,
                        pacer,
                        progress=progress,
                    )
                except LLMConfigError as exc:
                    # 开考后模型 key 才失效：机会已经消耗，必须继续到交卷
                    solve_aborted = True
                    print(
                        f"\n      ⚠ 模型中途不可用：{exc}\n"
                        "        剩余题目进入兜底，仍会按现有答案交卷。"
                    )
                print(
                    f"\n      完成，用时 {clock() - t0:.1f}s，"
                    f"token 消耗 {solver.usage['prompt']}+{solver.usage['completion']}"
                )

                # 专项模块核实：命中关键词的题目按指定权威原文重判。
                # 放在低置信搜索之前 —— 原文比网络搜索可靠，先定调再补充。
                if (
                    not solve_aborted
                    and getattr(solver, "modules", None)
                    and pacer.budget_seconds() > 90
                ):
                    print("\n      专项模块核实（命中规则的题目按权威原文重判）…")
                    emit("module", phase="start")

                    def mprogress(done: int, total: int) -> None:
                        pct = done * 100 // max(total, 1)
                        bar = "█" * (pct // 4) + "░" * (25 - pct // 4)
                        print(f"\r      [{bar}] {pct:3d}%  {done}/{total}", end="", flush=True)
                        emit("module", done=done, total=total)

                    mst = solver.apply_modules(
                        questions, progress=mprogress, deadline=deadline
                    )
                    emit("module", phase="done", **mst)
                    if mst["matched"]:
                        print(
                            f"\n      命中 {mst['matched']} 题，按原文定答 {mst['resolved']} 题，"
                            f"改动 {mst['changed']} 题"
                            + (f"，失败 {mst['failed']} 题" if mst["failed"] else "")
                        )
                        for q in questions:
                            _record_answer(state, q)
                        store.save(state)
                    else:
                        print("\r      没有题目命中专项规则，跳过。")

                # 搜索核实统一放到答题结束之后（见 6c），staged 模式下要跑多轮，
                # 不能挂在「有未答题」这个分支里，否则题库全命中时搜索根本不会执行。
        else:
            print("\n[6/7] 所有题目已有答案（平台答案/本地题库/断点恢复），跳过答题。")

        # ---- 6c. 搜索核实（staged：最多 N 轮；普通模式：单轮）----
        if staged:
            print(
                f"\n[6c/7] 联网搜索核实（最多 {cfg.llm.search_rounds} 轮，"
                "每轮只处理还没核过的题）…"
            )
        if not resume_submit:
            _search_phase(
                cfg,
                solver,
                questions,
                state,
                store,
                deadline,
                emit,
                staged,
                clock=clock,
            )

        # ---- llm_first：模型判完后用题库校正（题库答案更硬，覆盖模型结论）----
        if getattr(cfg.bank, "mode", "prefer") == "llm_first":
            print("\n      题库校正（模型答案与题库命中项比对）…")
            bstats = _resolve_bank(cfg, questions, emit=emit)
            if bstats.get("applied"):
                for q in questions:
                    _record_answer(state, q)
                store.save(state)

        # 兜底：未答出的选择题猜一个
        missing = [q for q in questions if not q.answered]
        if missing and cfg.exam.guess_unanswered:
            for q in missing:
                _guess_answer(q)
                _record_answer(state, q, synthetic=True)
            store.save(state)
            guessed = sum(1 for q in missing if q.answered)
            print(f"      ⚠ {len(missing)} 题未作答，已兜底猜测 {guessed} 题（选择题）")
        elif missing:
            print(f"      ⚠ {len(missing)} 题未作答，exam.guess_unanswered=false，按空答案提交")

        blank_subjective = [
            q for q in questions if not q.answered and q.qtype in ("fill", "essay")
        ]
        if blank_subjective:
            print(
                f"      ⚠ {len(blank_subjective)} 道填空/简答没有答案，将按空答案提交"
            )

        # 严格核实模式：只提交有外部依据的答案（默认关闭，留空就是 0 分）
        if cfg.exam.skip_unverified and not cfg.exam.dry_run and cfg.exam.auto_submit:
            dropped = 0
            for q in questions:
                if q.answered and not getattr(q, "verified", False) and not getattr(q, "edited", False):
                    q.answer_keys = []
                    q.answer_text = ""
                    _record_answer(state, q)
                    dropped += 1
            if dropped:
                store.save(state)
                print(f"      ⚠ skip_unverified：{dropped} 题没有核实依据，已按空答案处理")

        # 答题卡落盘：GUI「修改答案」读的就是这份，交卷前可反复改
        _dump_answer_sheet(state, questions)

        print_paper(questions)

        # ---- 7. 交卷 ----
        correct_n = sum(1 for q in questions if _correctness(q) == 1)
        correct_total = local_correct_total(questions)
        try:
            score_field = encrypt_score(
                int(round(correct_total)), creds.get("yiban_id")
            )
        except Exception:
            score_field = "0"
        print(
            f"      本地判分：正确 {correct_n} 题，预计 {correct_total:g} 分"
            f"（score 字段{'已加密' if score_field != '0' else '降级为 0'}）"
        )
        body = build_submit_body(
            state.examuser_id,
            questions,
            cfg.exam.answer_as_json_string,
            shuffle=cfg.humanize.shuffle_submit_order,
            score=score_field,
        )
        # pacer 在无模型/恢复场景下也要有，用于判定剩余时间
        pacer = humanize.Pacer(
            cfg.humanize,
            started_at=clock(),
            total_questions=0,
            deadline_at=deadline,
            reserve_seconds=cfg.exam.deadline_margin_seconds,
            clock=clock,
            sleep=sleep_fn,
        )
        return _submit(cfg, api, exam_id, body, state, store, sess, pacer)

    except KeyboardInterrupt:
        try:
            st = store.load()
            # 只回退「还没进入交卷流程」的状态；
            # 交卷中/已交卷必须保留，否则「上次交卷结果未知」的保护会被绕过。
            if st and st.examuser_id and st.phase in ("started", "solving"):
                st.phase = "solving"
                store.save(st)
        except Exception:
            pass
        print(
            "\n已中断。作答记录已保存；下次重跑会自动续跑，不会重新开考。\n"
            f"  状态文件：{cfg.paths.state}"
        )
        raise
    except LoginError as exc:
        print(f"\n[登录失败] {exc}")
        _log(f"login failed: {exc}")
        return EXIT_LOGIN
    except AuthError as exc:
        print(f"\n[登录态失效] {exc}\n  请重跑以重新登录（作答状态已保存）。")
        return EXIT_LOGIN
    except ExamWindowError as exc:
        print(f"\n[考试窗口] {exc}")
        return EXIT_WINDOW
    except SubmitUnconfirmed as exc:
        print(f"\n[交卷未确认] {exc}")
        return EXIT_SUBMIT
    except StateError as exc:
        print(f"\n[状态错误] {exc}")
        return EXIT_SESSION
    except SessionError as exc:
        print(f"\n[会话错误] {exc}")
        return EXIT_SESSION
    except RiskControlError as exc:
        print(f"\n[风控信号] {exc}\n  已停止；请等待一段时间后再试。")
        return EXIT_LOGIN
    except LLMConfigError as exc:
        print(f"\n[模型配置] {exc}")
        return EXIT_CONFIG
    except ConfigError as exc:
        print(f"\n[配置错误] {exc}")
        return EXIT_CONFIG
    except (TransientError, ApiError) as exc:
        print(f"\n[网络/接口错误] {exc}")
        return EXIT_ERROR
    except AppError as exc:
        print(f"\n[错误] {exc}")
        return EXIT_ERROR
    except Exception:  # noqa: BLE001
        import traceback

        print("\n[未处理异常]")
        traceback.print_exc()
        _log("unhandled exception:\n" + traceback.format_exc())
        return EXIT_ERROR
    finally:
        if sess is not None:
            try:
                sess.close()
            except Exception:
                pass
        lock.release()


def _dry_preflight_summary(
    cfg: Config, exam: dict, detail: Any, setting: Any
) -> None:
    """演练安全模式的只读预检输出（不会开考、不会取卷）。"""
    duration = _extract_duration_minutes(exam, detail, setting)
    print(f"      考试：{_exam_title(exam)}  (examId={_exam_id(exam)})")
    for label, key in (("题量", "subjects"), ("总分", "totalPoints"), ("时长(分)", "duration")):
        v = exam.get(key)
        if v not in (None, ""):
            print(f"      {label}：{v}")
    if duration:
        pace = cfg.humanize.per_question_seconds
        questions = int(exam.get("subjects") or 0)
        if questions and pace:
            print(f"      预计答题耗时（按 {pace:g}s/题）：约 {_fmt_delta(questions * pace)}")
    print(f"      开考窗口：{exam.get('started') or '?'} ~ {exam.get('expired') or '?'}")
    if exam.get("requirements"):
        print(f"      考试说明：{exam['requirements']}")
    print(
        "\n[演练完成] 登录、考试列表、设置接口均正常；未开考、未取卷、未提交。\n"
        "  要真正走「开考 → 答题 → 不提交」，任选其一：\n"
        "    - python main.py --start-dry-run   （显式允许演练开考，会占用本次作答机会）\n"
        "    - 把 exam.allow_start_in_dry_run 设为 true\n"
        "  要正式交卷：python main.py --live\n"
        "  注意：每人只有一次机会，演练与正式运行共用同一次作答记录。"
    )


def _check_window(
    cfg: Config,
    exam: dict,
    *,
    title: str,
    force: bool,
    auto_wait: bool = False,
    now: float | None = None,
) -> float:
    """校验考试窗口。

    返回「距离开考还剩多少秒」（已经开了就返回 0）。开启 auto_wait 时，
    时间没到不抛异常，而是把剩余秒数返回给调用方去等待。
    """
    raw_started = exam.get("started")
    raw_expired = exam.get("expired")
    started = _parse_time(raw_started)
    expired = _parse_time(raw_expired)
    now = time.time() if now is None else now
    if raw_started or raw_expired:
        print(f"      窗口：{raw_started or '?'} ~ {raw_expired or '?'}")
    # fail-closed：字段存在但解析不了时不猜，直接停（--force 显式跳过）
    if raw_started and started is None and not force:
        raise ExamWindowError(
            "无法解析开考时间字段，为避免在错误时间开考，已停止。\n"
            f"  原始值：{raw_started!r}\n"
            f"  考试列表原始返回：{_DATA_DIR / 'exam_list_raw.json'}"
        )
    if raw_expired and expired is None and not force:
        raise ExamWindowError(
            "无法解析截止时间字段，为避免浪费机会，已停止。\n"
            f"  原始值：{raw_expired!r}\n"
            f"  考试列表原始返回：{_DATA_DIR / 'exam_list_raw.json'}"
        )
    if started and now < started and not force:
        delta = _fmt_delta(started - now)
        if auto_wait:
            print(f"      ⚠ 尚未到开考时间，还差 {delta}（auto_start 已开启，转入等待）")
            return started - now
        print(f"      ⚠ 考试尚未开始，还差 {delta}")
        raise ExamWindowError(
            "考试还没到开考时间，已停止 —— 避免白白消耗考试机会。\n"
            f"  开考时间：{raw_started}\n"
            f"  剩余     ：{delta}\n"
            "  到点后重跑即可；确实要强行开考请加 --force\n"
            "  想让它自己等到点自动开考：开启 exam.auto_start 或加 --auto-start"
        )
    if expired and now > expired:
        raise ExamWindowError(
            "考试已截止，不能再作答。\n"
            f"  截止时间：{raw_expired}"
        )
    return 0.0


def _resolve_start_target(cfg: Config, exam: dict) -> tuple[float | None, str]:
    """算出「该在什么时刻开考」以及这个时刻的来源。

    优先用 exam.start_at（人工指定），否则用考试列表里的 started。
    时刻已过就返回过去的时间，交给调用方判断「立刻开考」。
    """
    raw = getattr(cfg.exam, "start_at", None)
    if raw:
        t = _parse_time(raw)
        if t is None:
            m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", str(raw).strip())
            if m:
                now = _dt.datetime.now()
                t = now.replace(
                    hour=int(m.group(1)),
                    minute=int(m.group(2)),
                    second=int(m.group(3) or 0),
                    microsecond=0,
                ).timestamp()
                # 只写 HH:MM 且今天的点已经过了 → 认为指的是明天
                if t <= time.time() - 60:
                    t += 86400
            else:
                raise ConfigError(
                    f"exam.start_at={raw!r} 无法解析。\n"
                    "  支持：'YYYY-MM-DD HH:MM:SS'、'YYYY-MM-DD HH:MM'、'HH:MM'\n"
                    "  想跟随考试列表里的开考时间，留空或写 auto。"
                )
        return t, f"exam.start_at={raw}"
    t = _parse_time(exam.get("started"))
    return t, "考试列表 started"


def _wait_for_start(
    cfg: Config,
    api: YoocApi,
    exam: dict,
    exam_id: Any,
    title: str,
    *,
    clock: Callable[[], float] = time.time,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict:
    """挂起进程，等到开考时刻再返回（开启 auto_start 时用）。

    等待期间只在本地倒计时，不刷接口；到点后再拉一次考试列表确认窗口
    真的放开了。返回（可能是刷新后的）exam 字典。
    """
    target, source = _resolve_start_target(cfg, exam)
    if target is None:
        raise ExamWindowError(
            "auto_start 已开启，但拿不到开考时间（考试列表没有 started，"
            "exam.start_at 也没配）。\n"
            "  请显式配置 exam.start_at，例如 '2026-10-19 00:00:00'。"
        )

    now = clock()
    if now >= target:
        return exam

    wait_total = target - now
    max_wait = cfg.exam.auto_start_max_wait_minutes * 60
    if wait_total > max_wait:
        raise ExamWindowError(
            "距离开考太久，超出自动等待上限，已停止。\n"
            f"  开考时间：{_dt.datetime.fromtimestamp(target):%Y-%m-%d %H:%M:%S}"
            f"（来源：{source}）\n"
            f"  还需等待：{_fmt_delta(wait_total)}\n"
            f"  等待上限：{cfg.exam.auto_start_max_wait_minutes} 分钟"
            "（exam.auto_start_max_wait_minutes）\n"
            "  确认要等就先把这个上限调大，或直接用 --start-at 指定更近的时刻。"
        )

    poll = max(5, int(cfg.exam.auto_start_poll_seconds))
    target_s = _dt.datetime.fromtimestamp(target).strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[自动开考] 目标时刻 {target_s}（来源：{source}）")
    print(f"      距开考 {_fmt_delta(wait_total)}，开始等待（每 {poll}s 报一次进度，Ctrl+C 可中断）")

    last_report = now
    while True:
        now = clock()
        remaining = target - now
        if remaining <= 0:
            break
        sleep_fn(min(poll, remaining))
        now2 = clock()
        if now2 <= now:
            # sleep_fn 没有推进时钟（被 mock 掉/系统时间被改）→ 不能空转
            raise ExamWindowError(
                "等待期间时钟没有推进，已停止自动等待（避免死循环空转）。\n"
                "  如果这是测试环境，请用可推进的 clock/sleep_fn。"
            )
        if now2 - last_report >= 30:
            last_report = now2
            print(f"      [{_dt.datetime.fromtimestamp(now2):%H:%M:%S}] 距开考 {_fmt_delta(target - now2)}")

    print(f"      [{_dt.datetime.fromtimestamp(clock()):%H:%M:%S}] 已到开考时间，共等待 {_fmt_delta(wait_total)}")

    # 到点后复核登录态：等待几小时的情况下 token 很可能已经过期
    try:
        chk = api.user_check()
        if isinstance(chk, dict) and chk.get("result") is False:
            print(f"      复核登录态返回失败：{chk.get('message')}（继续，写操作会自动续期）")
    except Exception as exc:  # noqa: BLE001
        print(f"      复核登录态跳过：{exc}")

    if not getattr(cfg.exam, "auto_start_verify", True):
        return exam

    # 平台偶尔到点不立刻放行，重新拉一次列表确认窗口确实开了
    try:
        fresh = select_exam(cfg, api)
        if _exam_id(fresh) == exam_id:
            fresh_started = _parse_time(fresh.get("started"))
            if fresh_started and clock() < fresh_started:
                print("      ⚠ 服务端仍显示未到开考时间，按本地时间继续（可用 --force 跳过时间检查）")
            return fresh
        print("      ⚠ 刷新后的列表里找不到同一场考试，继续用缓存数据")
    except Exception as exc:  # noqa: BLE001
        print(f"      到点刷新列表失败（继续用缓存）：{exc}")
    return exam
