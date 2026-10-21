"""配置加载与规范化。

打包成 exe 后 `__file__` 指向 PyInstaller 的临时解包目录，配置和数据必须落在
exe 自身所在目录，否则每次运行都读不到自己的 config.yaml。

错误类型统一为 ConfigError（而不是 SystemExit）：GUI 的 HTTP 线程里
SystemExit 属于 BaseException，`except Exception` 抓不到，会把请求挂死。
"""
from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError

if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
    BUNDLE = Path(getattr(sys, "_MEIPASS", ROOT))
else:
    ROOT = Path(__file__).resolve().parent.parent
    BUNDLE = ROOT

CONFIG_PATH = ROOT / "config.yaml"
EXAMPLE_PATH = ROOT / "config.example.yaml"
BUNDLED_EXAMPLE = BUNDLE / "config.example.yaml"
DATA_DIR = ROOT / "data"

VALID_LOGIN_MODES = ("auto", "password", "browser")
VALID_LOGIN_TRANSPORTS = ("auto", "http", "browser")
VALID_WRITE_TRANSPORTS = ("auto", "always", "never")
VALID_ANSWER_STYLES = ("letters", "letters_array", "optionid", "index")
VALID_FILL_STYLES = ("array", "string")
VALID_BANK_MODES = ("prefer", "llm_first", "staged")


@dataclass
class AccountCfg:
    # 账号隔离键（由 profile_key / username 推导，见 account_key_of）
    profile_key: str = "default"
    profile_dir: Path = ROOT / "data" / "browser_profile"
    group_id: int = 0
    browser_path: str | None = None
    headless: bool = False
    slow_mo_ms: int = 0
    # 登录方式：auto（先账号密码，失败再开浏览器）/ password（纯接口）/ browser（开浏览器）
    login_mode: str = "auto"
    username: str = ""
    password: str = ""
    # 是否复用 data/session.json 里的旧凭据（避免反复登录触发限频）
    reuse_session: bool = True
    # 登录传输通道：
    #   auto    —— 有浏览器就先用浏览器提交登录表单（真实 TLS/资产/ Cookie），失败回退 HTTP
    #   http    —— 只走 requests 直连登录接口
    #   browser —— 必须走浏览器表单登录
    login_transport: str = "auto"
    # 写操作（start/submit/repeat）的通道：
    #   auto   —— 有浏览器就用页面内 fetch，起不来就退回纯 HTTP
    #   always —— 必须走浏览器，起不来直接失败
    #   never  —— 全部走纯 HTTP
    browser_for_writes: str = "auto"
    # 本地登录熔断：窗口期内失败/尝试次数上限，防止把自己账号打到限频
    login_max_attempts: int = 6
    login_window_seconds: int = 600
    login_min_interval_seconds: int = 8


@dataclass
class ExamCfg:
    exam_id: Any = None
    exam_name_keyword: str | None = None
    # 未指定 exam_id/keyword 时，是否允许自动选第一场；
    # 默认 false：列表里有多场考试时停下，避免开错考试（一次机会）。
    auto_select_first: bool = False
    auto_submit: bool = True
    # 答完停在交卷前，等人工核验答题卡（GUI 可编辑）后再手动交卷。
    # 打开后等价于 auto_submit=false，但语义更明确，避免误以为"忘了交卷"。
    manual_review: bool = False
    dry_run: bool = True
    submit_review_seconds: int = 8
    # 答案在提交体里的形态：letters("A,C") / letters_array(["A","C"])
    # / optionid(选项 id) / index(选项下标)
    answer_style: str = "letters"
    # 前端提交时 answer 字段是 JSON 序列化后的字符串，默认沿用
    answer_as_json_string: bool = True
    # 填空题答案形态：array = 按空拆成数组（贴近前端逐空结构）/ string = 原样字符串
    fill_answer_style: str = "array"
    # 断点续跑：检测到同一场考试的未完成记录时，复用 examuserId 而不是重新开考
    resume: bool = True
    # 演练模式是否允许真的开考。
    # 默认 false：dry_run 在「没有作答记录」时只做只读预检（登录/列表/设置），
    # 绝不 start —— 否则第一次演练就会消耗唯一一次考试机会。
    allow_start_in_dry_run: bool = False
    # 距离截止时间留多少秒安全垫（用于最后一次心跳/交卷）
    deadline_margin_seconds: int = 120
    # 剩余时间少于这个值就不再答题，直接交卷
    min_seconds_before_submit: int = 45
    # 每答 N 题发一次 answer/get 心跳（同时探测登录态）。
    # 默认 0 = 关闭：前端 bundle 里没有逐题轮询，加心跳反而是新特征。
    # 需要提前发现 token 过期时可以打开（例如 5）。
    heartbeat_questions: int = 0
    # 大模型彻底失败时，选择题兜底随机猜一个（不猜就是 0 分）
    guess_unanswered: bool = True
    # paper 里的正确答案由前端用 yibanId 派生密钥加密（本地判分用）。
    # 打开后优先采用解密出的答案；关闭则只用于本地判分/score，不覆盖 LLM 答案。
    use_answer_key: bool = True
    # start 被数美验证码拦截时，是否自动打开真实 take 页让用户完成验证后继续
    captcha_browser_fallback: bool = True
    # 上次交卷结果未知时，是否允许再次自动交卷（默认 false：避免重复提交）
    force_resubmit: bool = False
    # 单场最大答题预算（秒），超过后停止答题并按现有答案交卷；0 = 只受考试截止时间约束
    max_solve_seconds: int = 0
    # ---- 到点自动开考 ----
    # 默认 false：开考时间没到就直接报错退出（原来的行为）。
    # 打开后：进程会一直挂到开考时刻再自动开考，不用人到点守着点按钮。
    auto_start: bool = False
    # 自定义开考时刻："YYYY-MM-DD HH:MM:SS" / "HH:MM"（当天，已过则算次日）
    # 留空或 "auto" = 用考试列表里的 started 字段
    start_at: str | None = None
    # 自动等待的上限（分钟）。防止填错时间后无限挂着；超过就报错。
    auto_start_max_wait_minutes: int = 240
    # 等待期间多久刷一次进度（秒）。只在本地倒计时，不额外请求接口。
    auto_start_poll_seconds: int = 60
    # 到点后是否重新拉一次考试列表确认窗口真的开了（平台偶尔延迟放行）
    auto_start_verify: bool = True
    # 交卷时是否丢掉「没从网络核实到依据」的答案。
    # 默认 false：留空就是 0 分，按模型答案填至少还有机会；要严格执行再加 true。
    skip_unverified: bool = False


@dataclass
class LLMCfg:
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model: str = "deepseek-flash"
    temperature: float = 0.2
    concurrency: int = 3
    max_retries: int = 3
    timeout: int = 90
    batch_size: int = 6
    # 没有 api_key 时是否允许用兜底猜测跑完流程（仅建议 dry_run 时开）
    allow_guess_without_key: bool = False
    # 正式开考前发一道 1+1 预检模型（防止 key 失效到开考后才发现）
    preflight: bool = True
    # ---- 搜索核实 ----
    # true = 每道题都搜索核实（不只是低置信题）。题量大会明显变慢/变贵。
    search_all_questions: bool = False
    # 全量核实模式下，核实结论达到这个置信度就采纳（不再要求比原答案更高）
    search_adopt_confidence: float = 0.7
    # ---- 网页搜索（对低置信题目做二次核实）----
    search_enabled: bool = True
    search_base_url: str = ""          # 留空则沿用 base_url
    search_count: int = 8              # 每次返回几条
    search_need_content: bool = False  # true 会返回网页正文，更准但更慢
    search_confidence_threshold: float = 0.75  # 低于此值才搜
    search_max_questions: int = 15     # 单场最多搜几题，控制成本
    search_timeout: int = 30
    search_max_seconds: int = 240      # 搜索核实阶段的总时间预算
    # ---- staged 流程：题库 -> 模型二次校验 -> 搜索 N 轮 ----
    # 题库命中后是否再让模型复核一遍（独立作答后比对，仅高置信不同才改判）
    verify_bank_answers: bool = True
    # 模型改判已有答案所需的最低置信度（低于它一律保留题库结论）
    verify_min_confidence: float = 0.85
    # 搜索核实最多做几轮：每轮只处理上一轮仍未核实的题
    search_rounds: int = 3


@dataclass
class HumanizeCfg:
    enabled: bool = True
    min_delay_ms: int = 2200
    max_delay_ms: int = 9500
    long_think_rate: float = 0.18
    long_think_ms: int = 18000
    # 打乱提交答案数组的顺序。默认关闭：部分系统按数组下标判分，
    # 打乱会导致答案错位；只有确认按 subjectId 匹配时才可打开。
    shuffle_submit_order: bool = False
    idle_before_submit_ms: int = 3000
    # 每题平均"占用"时长（秒），用于把整场考试耗时拉到真人水平
    per_question_seconds: float = 34.0
    # 心跳间隔（秒），默认 0 = 关闭（同 heartbeat_questions 的理由）
    heartbeat_seconds: int = 0


@dataclass
class HttpCfg:
    timeout: int = 30
    proxy: str | None = None
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
    )
    # 读请求的重试次数（写请求不自动重试，由 runner 核实服务端状态后决定）
    retries: int = 2
    backoff_seconds: float = 1.2
    # 追加到所有请求的头，便于临时补特征（不要放敏感信息）
    extra_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class ModulesCfg:
    """专项知识模块：题目命中关键词 → 拉指定权威原文 → 依据原文重判。"""

    enabled: bool = True
    # 每条规则：{name, keywords: [...], urls: [...], query: "..."}
    rules: list[dict] = field(default_factory=list)
    # 原文缓存有效期（小时）。官网内容变动不频繁，缓存能省掉整场考试的重复抓取。
    cache_ttl_hours: int = 168
    # 单题喂给模型的原文字数上限（太长会挤掉选项、也会烧 token）
    max_material_chars: int = 6000
    # 采纳门槛：模型基于原文给出的置信度低于这个值就不覆盖原判
    min_confidence: float = 0.5
    # 单场最多核几题（0 = 不限制），按模型置信度从低到高取
    max_questions: int = 20
    # 抓原文的超时（秒）
    fetch_timeout: int = 20


@dataclass
class BankCfg:
    """本地题库：拉卷后先按题干命中，命中就直接填答案（不消耗模型调用）。"""

    enabled: bool = True
    # 题库 JSON 路径，用 `python main.py --build-bank 答案.txt` 生成
    path: Path = DATA_DIR / "question_bank.json"
    # 模糊命中门槛（0~1）。题干被平台改写/加前后缀时靠它兜底，调低更容易命中但更易误伤
    min_score: float = 0.86
    # true = 题库答案覆盖已有答案（包括平台解密出的答案）；
    # 默认 false：只填空缺，平台解密答案优先
    overwrite: bool = False
    # prefer    —— 题库先答，模型只处理没命中的题（省时省钱，推荐）
    # llm_first —— 先让模型把整卷判一遍（推理/搜索都会跑），最后用题库校正命中题
    mode: str = "prefer"


# ------------------------------------------------------------------ 账号隔离
# 每个账号一套独立的运行态目录：浏览器 profile、登录凭据、登录熔断计数、
# 断点状态、运行锁、运行日志全部按账号分开，互不覆盖。
#
# 目录布局（key != "default" 时）：
#   data/profiles/<key>/browser_profile/   Chrome user-data-dir
#   data/profiles/<key>/session.json       user_token / cookies
#   data/profiles/<key>/login_guard.json   登录熔断计数
#   data/profiles/<key>/exam_state.json    断点状态
#   data/profiles/<key>/run.lock           运行锁
#   data/profiles/<key>/run.log            运行日志
#
# key == "default"（未配置账号）时退回旧布局，保证老配置行为不变。
DEFAULT_PROFILE_DIR = "./data/browser_profile"
PROFILES_DIR = DATA_DIR / "profiles"
_SLUG_KEEP = set("abcdefghijklmnopqrstuvwxyz0123456789_")


@dataclass(frozen=True)
class AccountPaths:
    """单个账号的全部运行态路径。"""

    key: str
    root: Path
    profile_dir: Path
    session: Path
    guard: Path
    state: Path
    lock: Path
    log: Path


def account_slug(raw: str) -> str:
    """把账号标识压成安全目录名：只留字母数字/-/_，其余转下划线。

    手机号、邮箱、学号都能直接用；中文名会全部变成下划线，
    此时用内容哈希补一段后缀，避免不同中文账号撞到同一目录。
    """
    s = str(raw or "").strip().lower()
    out = "".join(ch if ch in _SLUG_KEEP else "_" for ch in s).strip("_")
    out = "_".join(part for part in out.split("_") if part)[:32]
    if not out or not any(ch.isalnum() for ch in out):
        import hashlib

        return "u" + hashlib.sha256(s.encode("utf-8")).hexdigest()[:10]
    return out


def account_key_of(account: dict[str, Any] | None) -> str:
    """从 account 段推导隔离键：profile_key 优先，其次 username。"""
    a = account or {}
    raw = str(a.get("profile_key") or "").strip()
    if raw:
        return account_slug(raw)
    raw = str(a.get("username") or "").strip()
    if raw:
        return account_slug(raw)
    return "default"


def paths_for(key: str) -> AccountPaths:
    """按隔离键给出目录集合；default 键沿用旧布局。"""
    k = account_slug(key) if key and key != "default" else "default"
    if k == "default":
        return AccountPaths(
            key="default",
            root=DATA_DIR,
            profile_dir=DATA_DIR / "browser_profile",
            session=DATA_DIR / "session.json",
            guard=DATA_DIR / "login_guard.json",
            state=DATA_DIR / "exam_state.json",
            lock=DATA_DIR / "run.lock",
            log=DATA_DIR / "run.log",
        )
    root = PROFILES_DIR / k
    return AccountPaths(
        key=k,
        root=root,
        profile_dir=root / "browser_profile",
        session=root / "session.json",
        guard=root / "login_guard.json",
        state=root / "exam_state.json",
        lock=root / "run.lock",
        log=root / "run.log",
    )


def peek_account_key(path: Path | None = None) -> str:
    """只读 config.yaml 的 account 段推导隔离键（不做全量校验）。

    CLI 的 --status / --reset-state / --reset-login 以及 GUI 的单实例锁
    都要在完整加载配置之前知道账号，所以需要这个轻量入口。
    """
    cfg_path = Path(path) if path else CONFIG_PATH
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return "default"
    if not isinstance(raw, dict):
        return "default"
    acc = raw.get("account")
    return account_key_of(acc if isinstance(acc, dict) else {})


def _is_default_config(path: Path) -> bool:
    """判断这次加载的是不是项目自带的 config.yaml（迁移只对它生效）。"""
    try:
        return Path(path).resolve() == CONFIG_PATH.resolve()
    except OSError:
        return False


def _migrate_legacy(paths: AccountPaths) -> None:
    """把旧版共享目录搬到账号目录（仅目标不存在时搬，避免覆盖已有数据）。

    旧版只有一个 data/browser_profile + data/session.json。升级后第一次运行
    当前账号时，把这份历史数据归到该账号名下，用户不用重新登录。
    """
    if paths.key == "default":
        return
    pairs = (
        (DATA_DIR / "browser_profile", paths.profile_dir),
        (DATA_DIR / "session.json", paths.session),
        (DATA_DIR / "login_guard.json", paths.guard),
        (DATA_DIR / "exam_state.json", paths.state),
    )
    for src, dst in pairs:
        try:
            if not src.exists() or dst.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        except OSError:
            # 迁移失败不影响运行：新目录会重新生成，代价只是重登一次
            continue


@dataclass
class Config:
    account: AccountCfg = field(default_factory=AccountCfg)
    exam: ExamCfg = field(default_factory=ExamCfg)
    llm: LLMCfg = field(default_factory=LLMCfg)
    humanize: HumanizeCfg = field(default_factory=HumanizeCfg)
    http: HttpCfg = field(default_factory=HttpCfg)
    modules: ModulesCfg = field(default_factory=ModulesCfg)
    bank: BankCfg = field(default_factory=BankCfg)
    # 账号隔离后的运行态路径（由 load() 按账号键填充）
    paths: AccountPaths = field(default_factory=lambda: paths_for("default"))


# ------------------------------------------------------------------ 工具
def _as_int(value: Any, default: int, lo: int | None = None, hi: int | None = None) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        out = default
    if lo is not None:
        out = max(lo, out)
    if hi is not None:
        out = min(hi, out)
    return out


def _as_float(value: Any, default: float, lo: float | None = None, hi: float | None = None) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        out = default
    if lo is not None:
        out = max(lo, out)
    if hi is not None:
        out = min(hi, out)
    return out


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _norm_module_rules(value: Any) -> list[dict]:
    """规范化 modules.rules。单条规则写坏了只跳过它，不拖垮整场考试。"""
    if not value:
        return []
    if not isinstance(value, list):
        raise ConfigError("config.yaml: modules.rules 必须是列表（- name: ... 的形式）")
    out: list[dict] = []
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            print(f"[warn] modules.rules[{i}] 不是键值对，已跳过")
            continue
        kws = item.get("keywords") or []
        if isinstance(kws, str):
            kws = [kws]
        kws = [str(k).strip() for k in kws if str(k).strip()]
        urls = item.get("urls") or item.get("url") or []
        if isinstance(urls, str):
            urls = [urls]
        urls = [str(u).strip() for u in urls if str(u).strip()]
        query = str(item.get("query") or "").strip()
        name = str(item.get("name") or f"规则{i + 1}").strip()
        if not kws:
            print(f"[warn] modules.rules[{i}]（{name}）没有 keywords，已跳过")
            continue
        if not urls and not query:
            print(f"[warn] modules.rules[{i}]（{name}）既没有 urls 也没有 query，已跳过")
            continue
        out.append({"name": name, "keywords": kws, "urls": urls, "query": query})
    return out


def _resolve(p: str | Path) -> Path:
    p = Path(p)
    return p if p.is_absolute() else (ROOT / p).resolve()


def _norm_start_at(value: Any) -> str | None:
    """exam.start_at 归一化：空/auto/window 都当作「跟随考试列表」。"""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in ("auto", "window", "list", "none", "-"):
        return None
    return s


def ensure_config() -> Path:
    """config.yaml 不存在时从模板复制一份，方便直接改。"""
    if not CONFIG_PATH.exists():
        src = EXAMPLE_PATH if EXAMPLE_PATH.exists() else BUNDLED_EXAMPLE
        if not src.exists():
            raise ConfigError(
                f"缺少配置文件：{CONFIG_PATH}\n"
                "请把 config.example.yaml 复制为 config.yaml 并填写。"
            )
        try:
            shutil.copyfile(src, CONFIG_PATH)
        except OSError as exc:
            raise ConfigError(f"生成配置文件失败：{exc}") from exc
        print(f"[init] 已生成配置文件：{CONFIG_PATH}\n       请先填写 llm.api_key 再运行。")
    return CONFIG_PATH


def load(path: Path | None = None) -> Config:
    cfg_path = Path(path) if path else ensure_config()
    if not cfg_path.exists():
        raise ConfigError(f"配置文件不存在：{cfg_path}")
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ConfigError(f"读取配置文件失败：{cfg_path}\n{exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"config.yaml 语法错误（YAML 解析失败）：\n{exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml 顶层必须是键值对（YAML mapping）")

    a = raw.get("account") or {}
    e = raw.get("exam") or {}
    l = raw.get("llm") or {}
    h = raw.get("humanize") or {}
    t = raw.get("http") or {}
    mo = raw.get("modules") or {}
    bn = raw.get("bank") or {}
    for name, section in (
        ("account", a),
        ("exam", e),
        ("llm", l),
        ("humanize", h),
        ("http", t),
        ("modules", mo),
        ("bank", bn),
    ):
        if not isinstance(section, dict):
            raise ConfigError(f"config.yaml 的 {name} 段必须是键值对")

    # 账号隔离：先定隔离键，再决定 profile / 凭据 / 状态各自落在哪
    acc_key = account_key_of(a)
    acc_paths = paths_for(acc_key)

    login_mode = str(a.get("login_mode") or "auto").lower()
    if login_mode not in VALID_LOGIN_MODES:
        raise ConfigError(
            f"account.login_mode={login_mode!r} 非法，可选：{', '.join(VALID_LOGIN_MODES)}"
        )
    browser_for_writes = str(a.get("browser_for_writes") or "auto").lower()
    if browser_for_writes not in VALID_WRITE_TRANSPORTS:
        raise ConfigError(
            f"account.browser_for_writes={browser_for_writes!r} 非法，"
            f"可选：{', '.join(VALID_WRITE_TRANSPORTS)}"
        )
    login_transport = str(a.get("login_transport") or "auto").lower()
    if login_transport not in VALID_LOGIN_TRANSPORTS:
        raise ConfigError(
            f"account.login_transport={login_transport!r} 非法，"
            f"可选：{', '.join(VALID_LOGIN_TRANSPORTS)}"
        )
    answer_style = str(e.get("answer_style") or "letters").lower()
    if answer_style not in VALID_ANSWER_STYLES:
        raise ConfigError(
            f"exam.answer_style={answer_style!r} 非法，可选：{', '.join(VALID_ANSWER_STYLES)}"
        )
    fill_style = str(e.get("fill_answer_style") or "array").lower()
    if fill_style not in VALID_FILL_STYLES:
        raise ConfigError(
            f"exam.fill_answer_style={fill_style!r} 非法，可选：{', '.join(VALID_FILL_STYLES)}"
        )

    # profile_dir 只作为显式覆盖：留空、写默认值、写 data/browser_profile
    # 都视为「跟随账号隔离」，避免老配置把两个账号指到同一个 Chrome 用户目录。
    _pd_raw = str(a.get("profile_dir") or "").strip()
    follow_account = not _pd_raw or _pd_raw.replace("\\", "/").rstrip("/") in (
        "./data/browser_profile",
        "data/browser_profile",
    )
    if follow_account:
        profile_dir = acc_paths.profile_dir
    else:
        profile_dir = _resolve(_pd_raw)

    acc = AccountCfg(
        profile_key=acc_key,
        profile_dir=profile_dir,
        group_id=_as_int(a.get("group_id"), 0, 0),
        browser_path=str(a.get("browser_path")).strip() if a.get("browser_path") else None,
        headless=_as_bool(a.get("headless"), False),
        slow_mo_ms=_as_int(a.get("slow_mo_ms"), 0, 0, 5000),
        login_mode=login_mode,
        username=str(a.get("username") or ""),
        password=str(a.get("password") or ""),
        reuse_session=_as_bool(a.get("reuse_session"), True),
        login_transport=login_transport,
        browser_for_writes=browser_for_writes,
        login_max_attempts=_as_int(a.get("login_max_attempts"), 6, 1, 50),
        login_window_seconds=_as_int(a.get("login_window_seconds"), 600, 60, 86400),
        login_min_interval_seconds=_as_int(a.get("login_min_interval_seconds"), 8, 0, 600),
    )

    exam = ExamCfg(
        exam_id=e.get("exam_id") or None,
        exam_name_keyword=e.get("exam_name_keyword") or None,
        auto_select_first=_as_bool(e.get("auto_select_first"), False),
        auto_submit=_as_bool(e.get("auto_submit"), True),
        manual_review=_as_bool(e.get("manual_review"), False),
        dry_run=_as_bool(e.get("dry_run"), True),
        submit_review_seconds=_as_int(e.get("submit_review_seconds"), 8, 0, 600),
        answer_style=answer_style,
        answer_as_json_string=_as_bool(e.get("answer_as_json_string"), True),
        fill_answer_style=fill_style,
        resume=_as_bool(e.get("resume"), True),
        allow_start_in_dry_run=_as_bool(e.get("allow_start_in_dry_run"), False),
        deadline_margin_seconds=_as_int(e.get("deadline_margin_seconds"), 120, 30, 1800),
        min_seconds_before_submit=_as_int(e.get("min_seconds_before_submit"), 45, 10, 600),
        heartbeat_questions=_as_int(e.get("heartbeat_questions"), 0, 0, 100),
        guess_unanswered=_as_bool(e.get("guess_unanswered"), True),
        use_answer_key=_as_bool(e.get("use_answer_key"), True),
        captcha_browser_fallback=_as_bool(e.get("captcha_browser_fallback"), True),
        force_resubmit=_as_bool(e.get("force_resubmit"), False),
        max_solve_seconds=_as_int(e.get("max_solve_seconds"), 0, 0, 86400),
        auto_start=_as_bool(e.get("auto_start"), False),
        start_at=_norm_start_at(e.get("start_at")),
        auto_start_max_wait_minutes=_as_int(e.get("auto_start_max_wait_minutes"), 240, 1, 2880),
        auto_start_poll_seconds=_as_int(e.get("auto_start_poll_seconds"), 60, 5, 600),
        auto_start_verify=_as_bool(e.get("auto_start_verify"), True),
        skip_unverified=_as_bool(e.get("skip_unverified"), False),
    )

    llm = LLMCfg(
        base_url=str(l.get("base_url") or "https://api.deepseek.com/v1").rstrip("/"),
        api_key=str(l.get("api_key") or "").strip(),
        model=str(l.get("model") or "deepseek-flash"),
        temperature=_as_float(l.get("temperature"), 0.2, 0.0, 2.0),
        concurrency=_as_int(l.get("concurrency"), 3, 1, 8),
        max_retries=_as_int(l.get("max_retries"), 3, 1, 10),
        timeout=_as_int(l.get("timeout"), 90, 5, 600),
        batch_size=_as_int(l.get("batch_size"), 6, 1, 20),
        allow_guess_without_key=_as_bool(l.get("allow_guess_without_key"), False),
        preflight=_as_bool(l.get("preflight"), True),
        search_all_questions=_as_bool(l.get("search_all_questions"), False),
        search_adopt_confidence=_as_float(l.get("search_adopt_confidence"), 0.7, 0.0, 1.0),
        search_enabled=_as_bool(l.get("search_enabled"), True),
        search_base_url=str(l.get("search_base_url") or "").rstrip("/"),
        search_count=_as_int(l.get("search_count"), 8, 1, 50),
        search_need_content=_as_bool(l.get("search_need_content"), False),
        search_confidence_threshold=_as_float(
            l.get("search_confidence_threshold"), 0.75, 0.0, 1.0
        ),
        search_max_questions=_as_int(l.get("search_max_questions"), 15, 0, 200),
        search_timeout=_as_int(l.get("search_timeout"), 30, 5, 300),
        search_max_seconds=_as_int(l.get("search_max_seconds"), 240, 0, 7200),
        verify_bank_answers=_as_bool(l.get("verify_bank_answers"), True),
        verify_min_confidence=_as_float(l.get("verify_min_confidence"), 0.85, 0.5, 1.0),
        search_rounds=_as_int(l.get("search_rounds"), 3, 1, 10),
    )

    hum = HumanizeCfg(
        enabled=_as_bool(h.get("enabled"), True),
        min_delay_ms=_as_int(h.get("min_delay_ms"), 2200, 0, 600000),
        max_delay_ms=_as_int(h.get("max_delay_ms"), 9500, 0, 600000),
        long_think_rate=_as_float(h.get("long_think_rate"), 0.18, 0.0, 1.0),
        long_think_ms=_as_int(h.get("long_think_ms"), 18000, 0, 600000),
        shuffle_submit_order=_as_bool(h.get("shuffle_submit_order"), False),
        idle_before_submit_ms=_as_int(h.get("idle_before_submit_ms"), 3000, 0, 600000),
        per_question_seconds=_as_float(h.get("per_question_seconds"), 34.0, 0.0, 1200.0),
        heartbeat_seconds=_as_int(h.get("heartbeat_seconds"), 0, 0, 3600),
    )
    if hum.max_delay_ms < hum.min_delay_ms:
        hum.max_delay_ms = hum.min_delay_ms

    http = HttpCfg(
        timeout=_as_int(t.get("timeout"), 30, 5, 600),
        proxy=str(t.get("proxy")).strip() if t.get("proxy") else None,
        user_agent=str(t.get("user_agent") or HttpCfg.user_agent),
        retries=_as_int(t.get("retries"), 2, 0, 10),
        backoff_seconds=_as_float(t.get("backoff_seconds"), 1.2, 0.0, 30.0),
        extra_headers={
            str(k): str(v)
            for k, v in (t.get("extra_headers") or {}).items()
            if isinstance(k, str)
        }
        if isinstance(t.get("extra_headers"), dict)
        else {},
    )

    modules = ModulesCfg(
        enabled=_as_bool(mo.get("enabled"), True),
        rules=_norm_module_rules(mo.get("rules")),
        cache_ttl_hours=_as_int(mo.get("cache_ttl_hours"), 168, 0, 8760),
        max_material_chars=_as_int(mo.get("max_material_chars"), 6000, 500, 40000),
        min_confidence=_as_float(mo.get("min_confidence"), 0.5, 0.0, 1.0),
        max_questions=_as_int(mo.get("max_questions"), 20, 0, 200),
        fetch_timeout=_as_int(mo.get("fetch_timeout"), 20, 5, 120),
    )

    cfg = Config(account=acc, exam=exam, llm=llm, humanize=hum, http=http, modules=modules)

    bank = BankCfg(
        enabled=_as_bool(bn.get("enabled"), True),
        path=_resolve(bn.get("path") or "./data/question_bank.json"),
        min_score=_as_float(bn.get("min_score"), 0.86, 0.5, 1.0),
        overwrite=_as_bool(bn.get("overwrite"), False),
        mode=str(bn.get("mode") or "prefer").strip().lower(),
    )
    if bank.mode not in VALID_BANK_MODES:
        raise ConfigError(
            f"config.yaml: bank.mode={bank.mode!r} 非法，可选：{', '.join(VALID_BANK_MODES)}"
        )
    cfg.bank = bank

    if not cfg.account.group_id:
        raise ConfigError("config.yaml: account.group_id 未填写（考试页 URL 里 /group/ 后的数字）")

    cfg.paths = acc_paths
    # 旧版共享目录 -> 当前账号目录的一次性搬迁。
    # 只在加载默认 config.yaml 时做：用 --config 指到别处（含单元测试的临时配置）
    # 不应该动真实 data/ 目录里的历史数据。
    if _is_default_config(cfg_path):
        _migrate_legacy(acc_paths)

    try:
        cfg.account.profile_dir.mkdir(parents=True, exist_ok=True)
        if follow_account:
            acc_paths.root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"无法创建浏览器 profile 目录：{cfg.account.profile_dir}\n{exc}") from exc
    return cfg


def summary_lines(cfg: Config) -> list[str]:
    """给启动横幅用的配置摘要（不含密码/token）。"""
    try:
        _acc_dir = cfg.paths.root.relative_to(ROOT).as_posix()
    except ValueError:
        _acc_dir = str(cfg.paths.root)
    return [
        f"账号     : {cfg.account.profile_key}  ({_acc_dir})",
        f"分组     : {cfg.account.group_id}",
        f"模型     : {cfg.llm.model or '(未配置)'} @ {cfg.llm.base_url}",
        f"登录方式 : {cfg.account.login_mode}（复用凭据={cfg.account.reuse_session}）",
        f"写通道   : {cfg.account.browser_for_writes}",
        f"dry_run  : {cfg.exam.dry_run}  ({'只答题不提交' if cfg.exam.dry_run else '答完自动交卷'})",
        "自动开考 : "
        + (
            f"on （目标={cfg.exam.start_at or '考试列表 started'}，"
            f"最长等 {cfg.exam.auto_start_max_wait_minutes} 分钟）"
            if cfg.exam.auto_start
            else "off"
        ),
        "专项模块 : "
        + (
            f"on （{len(cfg.modules.rules)} 条规则）"
            if cfg.modules.enabled and cfg.modules.rules
            else "off"
        ),
        "本地题库 : "
        + (
            f"on （{cfg.bank.path.name}，覆盖={'是' if cfg.bank.overwrite else '否'}）"
            if cfg.bank.enabled
            else "off"
        ),
    ]
