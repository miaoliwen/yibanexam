# -*- mode: python ; coding: utf-8 -*-
"""原生桌面版（tkinter）打包配置，产物名 yibanexam。

两种产物模式，用环境变量 YOOC_ONEFILE 切换（默认 1 = 单文件）：

    # 单文件：dist/yibanexam.exe（一个 exe，首次启动解包到 %TEMP%，启动稍慢）
    python -m PyInstaller guitk.spec --noconfirm --clean

    # 文件夹：dist/yibanexam/yibanexam.exe + _internal/（不解包、启动快、杀软误报少）
    set YOOC_ONEFILE=0
    python -m PyInstaller guitk.spec --noconfirm --clean

注意：单文件模式依赖引导器在 %TEMP% 下创建带安全描述符的临时目录。本机浏览器版
曾遇到该步骤被环境拒绝（Could not create temporary directory!）；若单文件双击闪退，
改用 YOOC_ONEFILE=0 的文件夹模式。两种模式都把 config.yaml / data/ 放在 exe 同级
目录（源码里按 sys.frozen 取 exe 所在路径，不取临时解包目录）。

与 gui.spec（浏览器版）的区别：
1. 入口 gui_tk.py，界面是 tkinter 原生窗口 —— **必须保留 tkinter/tcl/tk**，
   不能像 gui.spec 那样 excludes；
2. 不需要 assets/index.html（不启动本地 HTTP 服务、不开浏览器）；
3. 业务逻辑 import 了 gui.py（复用 GuiApi/LogBuffer/单实例锁），显式加入 hiddenimports。
"""
import os

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []

# patchright（浏览器兜底登录用，node driver 必须全量收集）
for pkg in ("patchright",):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

hiddenimports += [
    "gui",              # gui_tk 复用 gui.GuiApi / LogBuffer / _single_lock
    "yaml",
    "requests",
    "openai",
    "openai.resources",
    "openai.types",
    "pydantic",
    "pydantic_core",
    "httpx",
    "httpcore",
    "h11",
    "anyio",
    "certifi",
    "charset_normalizer",
    "idna",
    "urllib3",
    "tzdata",
    # tkinter 标准库模块（PyInstaller 自带 hook 通常能收全，显式列出兜底）
    "tkinter",
    "tkinter.ttk",
    "tkinter.messagebox",
]

# 首次运行且 exe 旁边没有 config.yaml 时，从包内模板复制一份
datas += [("config.example.yaml", ".")]

# 注意：这里**不能**排除 tkinter；也不需要 webview/.NET 那套
excludes = [
    "matplotlib", "numpy", "pandas", "scipy",
    "PIL", "IPython", "pytest", "pydoc_data",
    "playwright",
    "webview", "clr_loader", "pythonnet",
]

a = Analysis(
    ["gui_tk.py"],
    pathex=[os.path.abspath(".")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# 与另外两个 spec 保持一致：YOOC_ONEFILE=0 出文件夹版（dist/yibanexam/）
ONEFILE = os.environ.get("YOOC_ONEFILE", "1").strip() not in ("0", "false", "False", "no")

exe = EXE(
    pyz,
    a.scripts,
    a.binaries if ONEFILE else [],
    a.datas if ONEFILE else [],
    [],
    name="yibanexam",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # GUI：不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    exclude_binaries=not ONEFILE,
)

if not ONEFILE:
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="yibanexam",
    )
