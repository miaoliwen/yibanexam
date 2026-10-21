"""原生桌面界面（tkinter，零第三方 GUI 依赖）。

与浏览器版（gui.py + assets/index.html）并存，业务逻辑完全复用：

- ``gui.GuiApi``  —— 配置落盘、测试登录、考试列表、演练/正式交卷
- ``gui.LogBuffer`` —— runner 的 print 由后台线程写入，UI 线程定时轮询渲染
- ``gui._single_lock`` —— 命名 Mutex 单实例（与浏览器版同名互斥）

不启动本地 HTTP 服务、不开浏览器、不需要 index.html。
标准库 tkinter 在 Windows 官方 Python 中自带，打包体积比浏览器版更小。

启动：python gui_tk.py
自检：python gui_tk.py --selftest   （建窗、填配置后自动退出，仅供验证）

⚠ 桌面版需要带 tkinter 的官方 Python 3.11+。部分精简版 Python 不含 tkinter，
用它跑会报 ModuleNotFoundError: No module named 'tkinter'；
本文件启动时会自动体检并提示，也可以直接双击 run-tk.bat。
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _preflight() -> None:
    """启动前体检：缺依赖时给出可执行的修复提示，而不是裸报错。"""
    import importlib.util as _u

    need = ("tkinter", "yaml", "requests", "openai", "cryptography", "httpx")
    missing = [m for m in need if _u.find_spec(m) is None]
    if not missing:
        return

    lines = [
        "",
        "=" * 68,
        "  [启动失败] 当前 Python 缺少模块：" + "、".join(missing),
        "",
        f"  你正在用：{sys.executable}",
        "",
        "  处理办法：",
        "    1) 激活虚拟环境后安装依赖：pip install -r requirements.txt",
        "    2) 桌面版需要带 tkinter 的官方 Python 3.11+（安装时保留 tcl/tk 组件）",
        "    3) 也可以双击项目根目录的 run-tk.bat，或直接使用 Release 中的 yibanexam.exe",
        "    4) 浏览器版 gui.py 不需要 tkinter：python gui.py",
        "=" * 68,
        "",
    ]
    sys.stderr.write("\n".join(lines))
    raise SystemExit(2)


_preflight()

import tkinter as tk  # noqa: E402
from tkinter import messagebox, ttk  # noqa: E402

import gui as webgui  # noqa: E402  复用 GuiApi / LogBuffer / _single_lock

# ============================================================ 配色（与浏览器版一致）
BG = "#0b0f14"
PANEL = "#121a22"
PANEL2 = "#182230"
BORDER = "#22303f"
TEXT = "#e6edf3"
MUTED = "#7d8b9c"
ENTRY_BG = "#0d141c"
ACCENT = "#22d3ee"
ACCENT2 = "#10b981"
DANGER = "#f43f5e"
WARN = "#f59e0b"
LOG_BG = "#0a0e13"
LOG_FG = "#b8c4d0"
HINT = "#546274"

UI_FONT = ("Microsoft YaHei UI", 9)
UI_FONT_B = ("Microsoft YaHei UI", 9, "bold")
TITLE_FONT = ("Microsoft YaHei UI", 12, "bold")
MONO_FONT = ("Consolas", 10)

LOG_LEVELS = ("info", "ok", "err", "warn", "accent")
LEVEL_COLORS = {
    "info": LOG_FG,
    "ok": ACCENT2,
    "err": "#fb7185",
    "warn": WARN,
    "accent": ACCENT,
}


def enable_dpi() -> None:
    """高 DPI 下让 tk 按物理像素渲染，否则界面会发虚。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.logs = webgui.LogBuffer()
        self.api = webgui.GuiApi(self.logs)

        self.busy = False
        self.cursor = 0
        self.done_shown = False
        self.log_empty = True
        self.exam_ids: list[str] = []
        self.action_widgets: list[tk.Widget] = []

        self.vars = {
            "username": tk.StringVar(),
            "password": tk.StringVar(),
            "group_id": tk.StringVar(),
            "base_url": tk.StringVar(),
            "api_key": tk.StringVar(),
            "model": tk.StringVar(),
            "search_enabled": tk.BooleanVar(value=True),
            "pace": tk.StringVar(value="34"),
            "auto_start": tk.BooleanVar(value=False),
            "start_at": tk.StringVar(value="auto"),
            "search_all": tk.BooleanVar(value=True),
        }

        self._setup_style()
        self._build_ui()
        self._load_state()
        self.root.after(350, self._poll)

    # ------------------------------------------------------------ 样式
    def _setup_style(self) -> None:
        st = ttk.Style()
        st.theme_use("clam")

        st.configure(".", background=BG, foreground=TEXT, font=UI_FONT, bordercolor=BORDER)
        st.configure("TFrame", background=BG)
        st.configure("Panel.TFrame", background=PANEL)
        st.configure("Bar.TFrame", background=PANEL)

        st.configure("TLabel", background=BG, foreground=TEXT)
        st.configure("Card.TLabel", background=PANEL, foreground=MUTED)
        st.configure("CardTitle.TLabel", background=PANEL, foreground=MUTED, font=UI_FONT_B)
        st.configure("CardText.TLabel", background=PANEL, foreground=TEXT)
        st.configure("CardHint.TLabel", background=PANEL, foreground=HINT, font=("Microsoft YaHei UI", 8))
        st.configure("Header.TLabel", background=PANEL, foreground=TEXT)
        st.configure("HeaderSub.TLabel", background=PANEL, foreground=MUTED,
                     font=("Microsoft YaHei UI", 8))
        st.configure("BarTitle.TLabel", background=PANEL, foreground=MUTED, font=UI_FONT_B)
        st.configure("Pct.TLabel", background=PANEL, foreground=MUTED, font=UI_FONT)

        st.configure(
            "TEntry",
            fieldbackground=ENTRY_BG, foreground=TEXT, insertcolor=TEXT,
            bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER, padding=5,
        )
        st.map(
            "TEntry",
            bordercolor=[("focus", ACCENT)],
            lightcolor=[("focus", ACCENT)],
            darkcolor=[("focus", ACCENT)],
            fieldbackground=[("disabled", ENTRY_BG)],
        )

        st.configure(
            "TCombobox",
            fieldbackground=ENTRY_BG, background=PANEL2, foreground=TEXT,
            bordercolor=BORDER, arrowcolor=MUTED, padding=4,
        )
        st.map(
            "TCombobox",
            bordercolor=[("focus", ACCENT)],
            fieldbackground=[("readonly", ENTRY_BG)],
            foreground=[("readonly", TEXT)],
        )
        self.root.option_add("*TCombobox*Listbox.background", PANEL2)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", "#243447")
        self.root.option_add("*TCombobox*Listbox.selectForeground", TEXT)
        self.root.option_add("*TCombobox*Listbox.font", UI_FONT)

        st.configure("TCheckbutton", background=PANEL, foreground=MUTED, font=UI_FONT)
        st.map(
            "TCheckbutton",
            background=[("active", PANEL)],
            foreground=[("active", TEXT)],
            indicatorcolor=[("selected", ACCENT), ("!selected", ENTRY_BG)],
        )

        st.configure(
            "TButton",
            background=PANEL2, foreground=TEXT, bordercolor=BORDER,
            padding=(10, 7), font=UI_FONT,
        )
        st.map(
            "TButton",
            background=[("active", "#1e2a38"), ("disabled", PANEL2)],
            bordercolor=[("active", "#2f4256"), ("disabled", BORDER)],
            foreground=[("disabled", "#4b5a6b")],
        )
        st.configure(
            "Primary.TButton",
            background=ACCENT, foreground="#04121a", bordercolor=ACCENT,
            padding=(10, 8), font=UI_FONT_B,
        )
        st.map(
            "Primary.TButton",
            background=[("active", "#52e2f7"), ("disabled", "#163a44")],
            foreground=[("disabled", "#6f9aa4")],
        )
        st.configure(
            "Danger.TButton",
            background="#2a1620", foreground="#fb7185", bordercolor="#5a2536",
            padding=(10, 8), font=UI_FONT_B,
        )
        st.map(
            "Danger.TButton",
            background=[("active", "#3a1c2a"), ("disabled", "#22181d")],
            foreground=[("disabled", "#7d4b58")],
        )

        st.configure(
            "Horizontal.TProgressbar",
            background=ACCENT, troughcolor="#1a2530", bordercolor="#1a2530",
            lightcolor=ACCENT, darkcolor=ACCENT, thickness=7,
        )
        for sb_name in ("Vertical.TScrollbar", "Dark.Vertical.TScrollbar"):
            st.configure(
                sb_name,
                background="#243040", troughcolor=LOG_BG, bordercolor=LOG_BG,
                arrowcolor=MUTED, width=14,
            )
            st.map(
                sb_name,
                background=[("active", "#2f4256"), ("disabled", "#243040")],
                arrowcolor=[("active", TEXT)],
            )

    # ------------------------------------------------------------ 构建界面
    def _build_ui(self) -> None:
        self._build_header()

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True)

        left = tk.Frame(body, bg=BG, width=340)
        left.pack(side="left", fill="y", padx=(14, 7), pady=14)
        left.pack_propagate(False)

        # 左栏卡片在小屏上可能超出高度，用 Canvas 做纵向滚动（对应浏览器版 overflow-y:auto）
        self.left_canvas = tk.Canvas(left, bg=BG, highlightthickness=0, width=322)
        left_sb = ttk.Scrollbar(left, orient="vertical",
                                style="Dark.Vertical.TScrollbar",
                                command=self.left_canvas.yview)
        self.left_canvas.configure(yscrollcommand=left_sb.set)
        left_sb.pack(side="right", fill="y")
        self.left_canvas.pack(side="left", fill="both", expand=True)

        cards = tk.Frame(self.left_canvas, bg=BG)
        self.left_canvas.create_window((0, 0), window=cards, anchor="nw", width=322)
        cards.bind(
            "<Configure>",
            lambda _e: self.left_canvas.configure(
                scrollregion=self.left_canvas.bbox("all"))
        )

        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=(7, 14), pady=14)

        self._build_account_card(cards)
        self._build_llm_card(cards)
        self._build_exam_card(cards)
        self._build_run_card(cards)
        self._build_log_panel(right)
        self._bind_left_wheel(left)

    def _build_header(self) -> None:
        bar = tk.Frame(self.root, bg=PANEL, height=56)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        logo = tk.Label(bar, text=" Y ", bg=ACCENT, fg="#04121a",
                        font=("Microsoft YaHei UI", 11, "bold"))
        logo.pack(side="left", padx=(16, 10), pady=12)

        titles = tk.Frame(bar, bg=PANEL)
        titles.pack(side="left")
        tk.Label(titles, text="yooc 考试助手", bg=PANEL, fg=TEXT,
                 font=TITLE_FONT).pack(anchor="w")

        tk.Frame(bar, bg=PANEL).pack(side="left", expand=True)

        # pack(side="right") 按调用顺序从右往左排：退出最右，状态点+文字在其左
        btn_quit = ttk.Button(bar, text="退出", command=self.quit_app, width=7)
        btn_quit.pack(side="right", padx=(12, 16))
        self.dot = tk.Label(bar, text="●", bg=PANEL, fg="#4b5a6b",
                            font=("Microsoft YaHei UI", 10))
        self.dot.pack(side="right", padx=(0, 4))
        self.status_text = tk.Label(bar, text="就绪", bg=PANEL, fg=MUTED, font=UI_FONT)
        self.status_text.pack(side="right")
        self.action_widgets.append(btn_quit)

    def _card(self, parent: tk.Widget, title: str) -> tk.Frame:
        box = tk.Frame(parent, bg=PANEL, highlightthickness=1,
                       highlightbackground=BORDER)
        box.pack(fill="x", pady=(0, 10))
        inner = tk.Frame(box, bg=PANEL)
        inner.pack(fill="x", padx=13, pady=11)
        ttk.Label(inner, text=title, style="CardTitle.TLabel").pack(
            anchor="w", pady=(0, 9))
        return inner

    def _entry(self, parent: tk.Widget, key: str, show: str | None = None) -> ttk.Entry:
        e = ttk.Entry(parent, textvariable=self.vars[key], show=show)
        e.pack(fill="x")
        return e

    def _build_account_card(self, parent: tk.Widget) -> None:
        c = self._card(parent, "账号")

        ttk.Label(c, text="手机号", style="Card.TLabel").pack(anchor="w", pady=(0, 3))
        self._entry(c, "username")

        ttk.Label(c, text="密码", style="Card.TLabel").pack(anchor="w", pady=(9, 3))
        self._entry(c, "password", show="*")
        self.pwd_hint = ttk.Label(c, text="已保存密码 · 留空则沿用",
                                  style="CardHint.TLabel")
        # has_password 时才 pack

        ttk.Label(c, text="课群 ID  *", style="Card.TLabel").pack(anchor="w", pady=(9, 3))
        self._entry(c, "group_id")

        row = tk.Frame(c, bg=PANEL)
        row.pack(fill="x", pady=(12, 0))
        b_login = ttk.Button(row, text="测试登录", command=self.test_login)
        b_login.pack(side="left", expand=True, fill="x", padx=(0, 5))
        b_save = ttk.Button(row, text="保存配置", command=lambda: self.save_config(False))
        b_save.pack(side="left", expand=True, fill="x", padx=(5, 0))
        self.action_widgets += [b_login, b_save]

    def _build_llm_card(self, parent: tk.Widget) -> None:
        c = self._card(parent, "大模型")

        ttk.Label(c, text="接口地址", style="Card.TLabel").pack(anchor="w", pady=(0, 3))
        self._entry(c, "base_url")

        ttk.Label(c, text="API Key", style="Card.TLabel").pack(anchor="w", pady=(9, 3))
        self._entry(c, "api_key", show="*")
        self.key_hint = ttk.Label(c, text="已保存 API Key · 留空则沿用",
                                  style="CardHint.TLabel")

        ttk.Label(c, text="模型名", style="Card.TLabel").pack(anchor="w", pady=(9, 3))
        self._entry(c, "model")

        chk = ttk.Checkbutton(c, variable=self.vars["search_enabled"],
                              text="答不确定的题时联网搜索核实")
        chk.pack(anchor="w", pady=(11, 0))

    def _build_exam_card(self, parent: tk.Widget) -> None:
        c = self._card(parent, "考试")

        row = tk.Frame(c, bg=PANEL)
        row.pack(fill="x")
        self.exam_combo = ttk.Combobox(row, state="readonly", height=10)
        self.exam_combo.pack(side="left", expand=True, fill="x", padx=(0, 8))
        b_refresh = ttk.Button(row, text="刷新", width=7, command=self.refresh_exams)
        b_refresh.pack(side="left")
        self.action_widgets.append(b_refresh)
        self._set_exam_placeholder("— 先测试登录，再刷新 —")

    def _build_run_card(self, parent: tk.Widget) -> None:
        c = self._card(parent, "执行")

        ttk.Label(c, text="答题节奏（秒/题）", style="Card.TLabel").pack(anchor="w", pady=(0, 3))
        self._entry(c, "pace")

        chk_search = ttk.Checkbutton(
            c, text="每题都用 DeepSeek 搜索核实（全量）",
            variable=self.vars["search_all"],
        )
        chk_search.pack(anchor="w", pady=(10, 0))
        self.action_widgets.append(chk_search)

        chk = ttk.Checkbutton(
            c, text="到点自动开考（没到时间就等着，到点自己开考）",
            variable=self.vars["auto_start"],
        )
        chk.pack(anchor="w", pady=(10, 0))
        self.action_widgets.append(chk)

        ttk.Label(c, text="开考时刻（auto = 跟考试列表）", style="Card.TLabel").pack(
            anchor="w", pady=(8, 3)
        )
        e_start = self._entry(c, "start_at")
        self.action_widgets.append(e_start)

        self.btn_start = ttk.Button(
            c, text="开始考试", style="Primary.TButton",
            command=lambda: self.run_exam(False, start_only=True),
        )
        self.btn_start.pack(fill="x", pady=(12, 0))
        self.action_widgets.append(self.btn_start)

        self.btn_edit = ttk.Button(
            c, text="查看 / 修改答案（未交卷前）",
            command=self.open_answer_editor,
        )
        self.btn_edit.pack(fill="x", pady=(8, 0))
        self.action_widgets.append(self.btn_edit)

        row = tk.Frame(c, bg=PANEL)
        row.pack(fill="x", pady=(8, 0))
        self.btn_dry = ttk.Button(row, text="演练", style="Primary.TButton",
                                  command=lambda: self.run_exam(False))
        self.btn_dry.pack(side="left", expand=True, fill="x", padx=(0, 5))
        self.btn_live = ttk.Button(row, text="正式交卷", style="Danger.TButton",
                                   command=lambda: self.run_exam(True))
        self.btn_live.pack(side="left", expand=True, fill="x", padx=(5, 0))
        self.action_widgets += [self.btn_dry, self.btn_live]

        b_data = ttk.Button(c, text="打开数据目录", command=self.open_data_dir)
        b_data.pack(fill="x", pady=(9, 0))
        self.action_widgets.append(b_data)

        ttk.Label(
            c,
            text="「开始考试」= 开考并答题，但不交卷（之后可点「正式交卷」提交）；"
                 "「正式交卷」= 一次做完并提交；「演练」默认只读预检，不消耗考试机会。",
            style="CardHint.TLabel", wraplength=286, justify="left",
        ).pack(anchor="w", pady=(9, 0))

    def _build_log_panel(self, parent: tk.Widget) -> None:
        bar = tk.Frame(parent, bg=PANEL, highlightthickness=1,
                       highlightbackground=BORDER)
        bar.pack(fill="x")
        inner = tk.Frame(bar, bg=PANEL)
        inner.pack(fill="x", padx=13, pady=9)

        ttk.Label(inner, text="运行日志", style="BarTitle.TLabel").pack(side="left")

        self.progress = ttk.Progressbar(inner, maximum=100, value=0)
        self.progress.pack(side="left", fill="x", expand=True, padx=14)

        self.pct = ttk.Label(inner, text="0%", style="Pct.TLabel", width=5,
                             anchor="e")
        self.pct.pack(side="left")
        b_clear = ttk.Button(inner, text="清空", width=6, command=self.clear_log)
        b_clear.pack(side="left", padx=(10, 0))

        sbar = tk.Frame(parent, bg=PANEL, highlightthickness=1,
                        highlightbackground=BORDER)
        sbar.pack(fill="x", pady=(8, 0))
        sin = tk.Frame(sbar, bg=PANEL)
        sin.pack(fill="x", padx=13, pady=7)
        self.s_dot = tk.Label(sin, text="●", bg=PANEL, fg="#4b5a6b")
        self.s_dot.pack(side="left")
        self.s_text = tk.Label(
            sin, text="DeepSeek 搜索核实：待机", bg=PANEL, fg=MUTED,
            font=UI_FONT, anchor="w",
        )
        self.s_text.pack(side="left", padx=(6, 10), fill="x", expand=True)
        self.s_bar = ttk.Progressbar(sin, maximum=100, value=0, length=140)
        self.s_bar.pack(side="right")

        log_box = tk.Frame(parent, bg=LOG_BG, highlightthickness=1,
                           highlightbackground=BORDER)
        log_box.pack(fill="both", expand=True, pady=(10, 0))

        self.text = tk.Text(
            log_box, relief="flat", borderwidth=0, highlightthickness=0,
            bg=LOG_BG, fg=LOG_FG, insertbackground=ACCENT, font=MONO_FONT,
            wrap="word", padx=12, pady=10, spacing1=1, spacing3=1,
            state="disabled",
        )
        sb = ttk.Scrollbar(log_box, orient="vertical",
                          style="Dark.Vertical.TScrollbar",
                          command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        self.text.tag_configure("ts", foreground="#3d4d5e")
        self.text.tag_configure("hint", foreground="#3d4d5e")
        for lv, color in LEVEL_COLORS.items():
            self.text.tag_configure(lv, foreground=color)
        self._show_log_hint()

    def _bind_left_wheel(self, container: tk.Widget) -> None:
        """鼠标在左栏任意控件上时，滚轮滚动卡片；离开后解绑，

        避免 bind_all 抢走右侧日志框的滚轮事件。
        """

        def on_wheel(e: tk.Event) -> None:
            self.left_canvas.yview_scroll(int(-e.delta / 120), "units")

        def bind_all(_e: tk.Event) -> None:
            self.root.bind_all("<MouseWheel>", on_wheel)

        def unbind_all(_e: tk.Event) -> None:
            self.root.unbind_all("<MouseWheel>")

        def walk(w: tk.Widget) -> None:
            w.bind("<Enter>", bind_all)
            w.bind("<Leave>", unbind_all)
            for child in w.winfo_children():
                walk(child)

        walk(container)

    # ------------------------------------------------------------ 表单 / 状态
    def form_data(self) -> dict:
        v = self.vars
        return {
            "username": v["username"].get().strip(),
            "password": v["password"].get(),
            "group_id": v["group_id"].get().strip(),
            "base_url": v["base_url"].get().strip(),
            "api_key": v["api_key"].get().strip(),
            "model": v["model"].get().strip(),
            "search_enabled": bool(v["search_enabled"].get()),
            "pace": v["pace"].get().strip(),
            "auto_start": bool(v["auto_start"].get()),
            "start_at": v["start_at"].get().strip() or "auto",
            "search_all": bool(v["search_all"].get()),
        }

    def _load_state(self) -> None:
        s = self.api.get_state()
        self.vars["username"].set(s.get("username") or "")
        self.vars["group_id"].set(s.get("group_id") or "")
        self.vars["base_url"].set(s.get("base_url") or "")
        self.vars["model"].set(s.get("model") or "")
        self.vars["pace"].set(s.get("pace") or "34")
        self.vars["search_enabled"].set(s.get("search_enabled", True))
        self.vars["auto_start"].set(bool(s.get("auto_start", False)))
        self.vars["start_at"].set(s.get("start_at") or "auto")
        self.vars["search_all"].set(bool(s.get("search_all", True)))
        # 密码/API Key 后端不回传；留空即沿用已保存值
        if s.get("has_password"):
            self.pwd_hint.pack(anchor="w", pady=(4, 0))
        if s.get("has_api_key"):
            self.key_hint.pack(anchor="w", pady=(4, 0))
        self.log_line("界面就绪，配置已加载", "ok")

    def _set_status(self, kind: str, text: str) -> None:
        color = {"idle": "#4b5a6b", "run": ACCENT, "ok": ACCENT2, "err": DANGER}
        self.dot.configure(fg=color.get(kind, MUTED))
        self.status_text.configure(text=text)

    def set_busy(self, b: bool, label: str | None = None) -> None:
        self.busy = b
        state = "disabled" if b else "normal"
        for w in self.action_widgets:
            try:
                w.configure(state=state)
            except tk.TclError:
                pass
        if b:
            self._set_status("run", label or "运行中")

    def _set_exam_placeholder(self, text: str) -> None:
        self.exam_ids = []
        self.exam_combo.configure(values=())
        self.exam_combo.set(text)

    # ------------------------------------------------------------ 日志渲染
    def _show_log_hint(self) -> None:
        self.text.configure(state="normal")
        self.text.insert("1.0", "等待开始…", "hint")
        self.text.configure(state="disabled")
        self.log_empty = True

    def log_line(self, msg: str, level: str = "info") -> None:
        self._append_lines([[msg, level]])

    def _append_lines(self, items: list[list[str]]) -> None:
        w = self.text
        w.configure(state="normal")
        if self.log_empty:
            w.delete("1.0", "end")
            self.log_empty = False
        for item in items:
            msg = str(item[0])
            lv = item[1] if len(item) > 1 and item[1] in LEVEL_COLORS else "info"
            w.insert("end", time.strftime("%H:%M:%S "), "ts")
            w.insert("end", msg + "\n", lv)
        # 上限 4000 行，超出砍头部
        line_count = int(w.index("end-1c").split(".")[0])
        if line_count > 4000:
            w.delete("1.0", f"{line_count - 4000}.0")
        w.configure(state="disabled")
        w.see("end")

    def clear_log(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self.log_empty = True
        self._show_log_hint()
        self.cursor = 0
        self.done_shown = False
        self.progress.configure(value=0)
        self.pct.configure(text="0%")

    def _render_live(self, st: dict) -> None:
        """把 runner 推来的阶段状态画到状态条上。"""
        sp = st.get("search") or {}
        sv = st.get("solve") or {}
        mod = st.get("module") or {}
        state = sp.get("state")

        if state == "running":
            total = int(sp.get("total") or 0)
            done = int(sp.get("done") or 0)
            pct = int(done * 100 / total) if total else 0
            txt = (
                f"DeepSeek 搜索核实 {done}/{total}"
                f" · 已核实 {sp.get('verified', 0)}"
                f" · 未核实 {sp.get('unverified', 0)}"
                f" · 改判 {sp.get('revised', 0)}"
            )
            last = sp.get("last")
            if last:
                mark = "✓" if last.get("ok") else "✗"
                txt += f" · 第{last.get('index')}题 {mark} {last.get('note', '')}"
            self.s_dot.configure(fg=ACCENT)
            self.s_bar.configure(value=pct)
        elif state == "done":
            txt = (
                f"搜索核实完成 {sp.get('done', 0)}/{sp.get('total', 0)}"
                f" · 已核实 {sp.get('verified', 0)}"
                f" · 未核实 {sp.get('unverified', 0)}"
                f" · 改判 {sp.get('revised', 0)}"
            )
            self.s_dot.configure(fg=ACCENT2)
            self.s_bar.configure(value=100)
        elif state == "unavailable":
            txt = f"搜索不可用：{sp.get('reason') or ''}"
            self.s_dot.configure(fg=DANGER)
            self.s_bar.configure(value=0)
        else:
            if sv.get("total"):
                txt = f"答题中 {sv.get('done', 0)}/{sv.get('total', 0)}（尚未进入搜索核实）"
            else:
                txt = "DeepSeek 搜索核实：待机"
            self.s_dot.configure(fg="#4b5a6b")
            self.s_bar.configure(value=0)

        if mod.get("state") == "running" and mod.get("total"):
            txt += f" | 专项模块 {mod.get('done', 0)}/{mod.get('total', 0)}"
        elif mod.get("state") == "done" and mod.get("matched"):
            txt += f" | 专项模块命中 {mod.get('matched')} 改判 {mod.get('changed')}"

        self.s_text.configure(text=txt)

    def _poll(self) -> None:
        try:
            snap = self.logs.snapshot(self.cursor)
            # 缓冲超 3000 行会裁剪，total 可能倒退；出现倒退就全量重拉
            if snap["total"] < self.cursor:
                self.cursor = 0
                snap = self.logs.snapshot(0)
            lines = snap.get("lines") or []
            if lines:
                self._append_lines(lines)
            self.cursor = snap["total"]

            pct = snap.get("progress")
            if isinstance(pct, (int, float)):
                self.progress.configure(value=max(0, min(100, pct)))
                self.pct.configure(text=f"{int(round(pct))}%")

            done = snap.get("done")
            if done and not self.done_shown:
                self.done_shown = True
                self.set_busy(False)
                ok = bool(done.get("ok"))
                msg = done.get("msg") or ("完成" if ok else "失败")
                self._set_status("ok" if ok else "err", msg)
                self.progress.configure(value=100 if ok else self.progress["value"])
                if ok:
                    messagebox.showinfo("运行结束", msg, parent=self.root)
                else:
                    messagebox.showwarning("运行结束", msg, parent=self.root)

            self._render_live(self.api.get_live_status())
        except Exception:  # noqa: BLE001  轮询绝不能打断 after 链
            pass
        self.root.after(350, self._poll)

    # ------------------------------------------------------------ 后台任务
    def _bg(self, fn, on_done) -> None:
        """网络调用放后台线程，结果回 UI 线程，避免窗口冻结。"""
        def work() -> None:
            try:
                result = fn()
            except BaseException as exc:  # noqa: BLE001
                result = {"ok": False, "msg": f"操作异常：{exc}"}
            self.root.after(0, lambda: on_done(result))

        threading.Thread(target=work, daemon=True).start()

    def save_config(self, silent: bool) -> bool:
        r = self.api.save_config(self.form_data())
        if not r.get("ok"):
            messagebox.showerror("保存失败", r.get("msg") or "未知错误", parent=self.root)
        elif not silent:
            self._set_status("ok", "配置已保存")
            messagebox.showinfo("保存配置", "配置已保存", parent=self.root)
        return bool(r.get("ok"))

    def test_login(self) -> None:
        if self.busy:
            return
        self.set_busy(True, "登录中")
        self.log_line("开始测试登录…", "accent")
        data = self.form_data()

        def done(r: dict) -> None:
            self.set_busy(False)
            self.log_line(r.get("msg") or "", "ok" if r.get("ok") else "err")
            self._set_status("ok" if r.get("ok") else "err",
                             "已登录" if r.get("ok") else "登录失败")
            if r.get("ok"):
                self.refresh_exams()

        self._bg(lambda: self.api.test_login(data), done)

    def refresh_exams(self) -> None:
        if self.busy:
            return
        self.set_busy(True, "拉取考试")
        self.log_line("获取考试列表…", "accent")
        data = self.form_data()

        def done(r: dict) -> None:
            self.set_busy(False)
            if not r.get("ok"):
                self.log_line(r.get("msg") or "获取失败", "err")
                self._set_status("err", "失败")
                messagebox.showwarning("获取考试", r.get("msg") or "获取失败",
                                       parent=self.root)
                return
            exams = r.get("exams") or []
            self.exam_ids = [str(e.get("id")) for e in exams]
            labels = [f"{e.get('title')}  ({e.get('info')})" for e in exams]
            self.exam_combo.configure(values=labels, state="readonly")
            if labels:
                self.exam_combo.current(0)
            self.log_line(f"发现 {len(exams)} 场考试", "ok")
            self._set_status("idle", "就绪")

        self._bg(lambda: self.api.list_exams(data), done)

    def run_exam(self, live: bool, start_only: bool = False) -> None:
        if self.busy:
            return
        if not self.save_config(True):
            self._set_status("err", "配置保存失败")
            return
        idx = self.exam_combo.current()
        if idx < 0 or idx >= len(self.exam_ids):
            messagebox.showwarning("未选择考试", "请先点「刷新」拉取列表并选择一场考试。",
                                   parent=self.root)
            return
        if start_only and not messagebox.askokcancel(
            "确认开始考试",
            "确认现在开始考试？\n\n"
            "· 会真的调用开考接口，占用本次作答机会（每人只有一次）\n"
            "· 开考后自动取卷并答题，但**不会交卷**\n"
            "· 答完后可点「正式交卷」提交同一份作答记录，不会重新开考\n"
            "· 已经开考过的话，这里只会续跑，不会重复开考",
            parent=self.root,
        ):
            return
        if live and not messagebox.askokcancel(
            "确认正式交卷",
            "确认正式交卷？\n\n这会消耗考试机会，且不可撤销。\n建议先「演练」确认答案。",
            parent=self.root,
        ):
            return

        self.clear_log()
        if live:
            head, label = "=== 正式模式 ===", "正式答题"
        elif start_only:
            head, label = "=== 开始考试（开考 + 答题，不交卷）===", "开考答题中"
        else:
            head, label = "=== 演练模式（不交卷）===", "演练中"
        self.log_line(head, "accent")
        self.set_busy(True, label)
        payload = {
            **self.form_data(),
            "exam_id": self.exam_ids[idx],
            "live": live,
            "start_only": start_only,
        }
        r = self.api.run_exam(payload)
        if not r.get("ok"):
            self.set_busy(False)
            self._set_status("err", "启动失败")
            self.log_line(r.get("msg") or "启动失败", "err")
            messagebox.showerror("启动失败", r.get("msg") or "启动失败", parent=self.root)

    # ------------------------------------------------------------ 答题卡
    QTYPE_CN = {"single": "单选", "multi": "多选", "judge": "判断", "fill": "填空", "essay": "简答"}

    def open_answer_editor(self) -> None:
        """交卷前查看/修改答案。写回 exam_state.json，提交体以此为唯一来源。"""
        if self.busy:
            messagebox.showinfo("正在运行", "答题流程运行中，结束后再改。", parent=self.root)
            return
        r = self.api.load_answer_sheet()
        if not r.get("ok"):
            messagebox.showinfo("没有答题卡", r.get("msg") or "还没有答题卡", parent=self.root)
            return
        items = r.get("items") or []
        if not items:
            messagebox.showinfo("答题卡为空", "这场考试还没有作答记录。", parent=self.root)
            return

        editable = bool(r.get("editable", True))
        win = tk.Toplevel(self.root)
        win.title("答题卡 · 交卷前可修改")
        win.configure(bg=BG)
        win.geometry("920x700")
        win.transient(self.root)

        head = tk.Frame(win, bg=PANEL)
        head.pack(fill="x")
        tk.Label(head, text=r.get("exam_name") or "考试", bg=PANEL, fg=TEXT,
                 font=UI_FONT_B, anchor="w").pack(fill="x", padx=14, pady=(10, 2))
        sub = f"共 {len(items)} 题 · 已核实 {r.get('verified_count', 0)} 题"
        if not editable:
            sub += " · " + (r.get("lock_reason") or "当前不可修改")
        tk.Label(head, text=sub, bg=PANEL, fg=MUTED, font=UI_FONT, anchor="w").pack(
            fill="x", padx=14, pady=(0, 10)
        )

        body = tk.Frame(win, bg=BG)
        body.pack(fill="both", expand=True)
        canvas = tk.Canvas(body, bg=BG, highlightthickness=0)
        sb = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG)
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        def _wheel(e: tk.Event) -> None:
            canvas.yview_scroll(int(-e.delta / 120), "units")

        canvas.bind("<Enter>", lambda e: win.bind_all("<MouseWheel>", _wheel))
        canvas.bind("<Leave>", lambda e: win.unbind_all("<MouseWheel>"))

        fields: list[tuple[dict, tk.StringVar]] = []
        for it in items:
            row = tk.Frame(inner, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
            row.pack(fill="x", padx=12, pady=6)
            qt = self.QTYPE_CN.get(str(it.get("qtype")), str(it.get("qtype") or ""))
            flags = []
            if not it.get("verified"):
                flags.append("未核实")
            if it.get("edited"):
                flags.append("已修改")
            tag = f"第 {it.get('index')} 题 · {qt}" + ((" · " + " · ".join(flags)) if flags else "")
            tk.Label(row, text=tag, bg=PANEL, fg=MUTED, font=UI_FONT, anchor="w").pack(
                fill="x", padx=10, pady=(8, 2)
            )
            tk.Label(row, text=it.get("stem") or "", bg=PANEL, fg=TEXT, font=UI_FONT,
                     anchor="w", justify="left", wraplength=830).pack(fill="x", padx=10)
            opts = it.get("options") or []
            if opts:
                otxt = "    ".join(f"{o.get('key')}. {o.get('text')}" for o in opts)
                tk.Label(row, text=otxt, bg=PANEL, fg=MUTED, font=UI_FONT,
                         anchor="w", justify="left", wraplength=830).pack(fill="x", padx=10, pady=(2, 0))
            cur = "".join(it.get("answer") or []) if opts else str(it.get("text") or "")
            var = tk.StringVar(value=cur)
            ent = ttk.Entry(row, textvariable=var,
                            state="normal" if editable else "disabled")
            ent.pack(fill="x", padx=10, pady=(6, 10))
            ent.bind("<Return>", lambda e: None)
            fields.append((it, var))

        def close() -> None:
            try:
                win.unbind_all("<MouseWheel>")
            except tk.TclError:
                pass
            win.destroy()

        def save() -> None:
            edits: dict[str, dict] = {}
            for it, var in fields:
                raw = var.get().strip()
                if it.get("options"):
                    parts = raw.replace("，", ",").replace(" ", ",").replace("、", ",")
                    edits[str(it["qid"])] = {"answer": [p for p in parts.split(",") if p.strip()]}
                else:
                    edits[str(it["qid"])] = {"text": raw}
            res = self.api.save_answer_sheet(edits)
            if not res.get("ok"):
                messagebox.showerror("保存失败", res.get("msg") or "保存失败", parent=win)
                return
            self.log_line(f"答题卡已更新：{res.get('changed', 0)} 题被修改", "ok")
            close()

        bar = tk.Frame(win, bg=PANEL)
        bar.pack(fill="x")
        if editable:
            ttk.Button(bar, text="保存修改", style="Primary.TButton",
                       command=save).pack(side="right", padx=12, pady=10)
        ttk.Button(bar, text="关闭", command=close).pack(side="right", pady=10)

    def open_data_dir(self) -> None:
        r = self.api.open_data_dir()
        if not r.get("ok"):
            messagebox.showwarning("打开失败", r.get("msg") or "打开失败", parent=self.root)

    def quit_app(self) -> None:
        if self.api._running:
            messagebox.showwarning(
                "任务运行中",
                "考试流程正在运行，此时退出可能造成状态不确定。\n请等待运行结束后再退出。",
                parent=self.root,
            )
            return
        if messagebox.askokcancel("退出", "确定退出程序？", parent=self.root):
            self.root.destroy()
            os._exit(0)

    def on_close(self) -> None:
        if self.api._running:
            messagebox.showwarning(
                "任务运行中",
                "考试流程正在运行，此时退出可能造成状态不确定。\n请等待运行结束后再退出。",
                parent=self.root,
            )
            return
        self.root.destroy()
        os._exit(0)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    selftest = "--selftest" in argv

    enable_dpi()

    try:
        from src.config import peek_account_key

        _key = peek_account_key()
    except Exception:
        _key = ""

    ok, _lock = webgui._single_lock(_key)
    if not ok:
        messagebox.showwarning(
            "yooc 考试助手 · 已在运行",
            f"账号 {_key} 已经有一个实例在运行了。\n\n"
            "不同账号可以同时开（各用各的 profile 与凭据）；\n"
            "同一账号只能开一个，避免两个进程抢同一次作答记录。\n\n"
            "如确认没有，请打开任务管理器结束对应进程后重试。",
        )
        return 0

    root = tk.Tk()
    root.title("yooc 考试助手" + (f" · {_key}" if _key and _key != "default" else ""))
    root.configure(bg=BG)
    root.geometry("960x680")
    root.minsize(820, 560)

    # 居中
    root.update_idletasks()
    w, h = 960, 680
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 3}")

    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)

    if selftest:
        # 自动验证：主循环转 1.2 秒后退出，能跑完说明建窗/样式/读配置全部正常
        diag = Path(os.environ.get("TEMP", ".")) / "yooc_tk_diag.txt"
        _st = ttk.Style()
        diag.write_text(
            f"theme={_st.theme_use()}\n"
            f"trough={_st.lookup('Vertical.TScrollbar', 'troughcolor')}\n"
            f"thumb={_st.lookup('Vertical.TScrollbar', 'background')}\n",
            encoding="utf-8",
        )
        root.after(1200, root.destroy)
        root.mainloop()
        print("SELFTEST_OK")
        return 0

    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
