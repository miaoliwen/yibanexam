# -*- mode: python ; coding: utf-8 -*-
"""GUI 版打包配置（本地 HTTP 服务 + 系统浏览器）。

用法：
    python -m PyInstaller gui.spec --noconfirm

说明：
1. 界面不再用 pywebview，改成本地 HTTP + 浏览器，所以**不需要**
   webview / pythonnet / clr_loader 那一堆 .NET 依赖，包体小很多。
2. assets/index.html 必须作为数据文件带上，服务启动时读取。
3. console=False —— GUI 程序不弹黑框。启动失败时用 MessageBox + 日志兜底。
"""
import os

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []

# patchright（浏览器兜底登录用）
for pkg in ("patchright",):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

hiddenimports += [
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
]

datas += [("assets/index.html", "assets")]

excludes = [
    "tkinter", "matplotlib", "numpy", "pandas", "scipy",
    "PIL", "IPython", "pytest", "pydoc_data",
    "playwright",
    "webview", "clr_loader", "pythonnet",   # 已改用 HTTP，别再塞进来
]

a = Analysis(
    ["gui.py"],
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

# 单文件 / 文件夹两种模式：
#   YOOC_ONEFILE=1（默认）→ dist/yooc-auto.exe，启动时解包到 %TEMP%
#   YOOC_ONEFILE=0        → dist/yooc-auto/yooc-auto.exe，不解包，启动快、无临时目录依赖
ONEFILE = os.environ.get("YOOC_ONEFILE", "1").strip() not in ("0", "false", "False", "no")

exe = EXE(
    pyz,
    a.scripts,
    a.binaries if ONEFILE else [],
    a.datas if ONEFILE else [],
    [],
    name="yooc-auto",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # GUI：不要黑框
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
        name="yooc-auto",
    )
