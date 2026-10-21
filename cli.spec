# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

用法：
    # 单文件（默认）
    python -m PyInstaller yooc-auto.spec --noconfirm

    # 文件夹模式（启动更快、更不易被杀软误报）
    set YOOC_ONEFILE=0
    python -m PyInstaller yooc-auto.spec --noconfirm

关键点：patchright 带一个 node 写的 driver，必须用 collect_all 完整收进包里，
否则运行时会出现 "Executable doesn't exist" 或 driver 启动失败。
"""
import os

from PyInstaller.utils.hooks import collect_all

ONEFILE = os.environ.get("YOOC_ONEFILE", "1") == "1"

# patchright 的 driver（node 二进制 + 协议脚本）全量收集
pw_datas, pw_binaries, pw_hidden = collect_all("patchright")

hiddenimports = pw_hidden + [
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
    "tzdata",                 # Windows 上 zoneinfo 依赖它解析 Asia/Shanghai
]

datas = pw_datas + [
    ("config.example.yaml", "."),   # 首次运行自动生成 config.yaml
]
binaries = pw_binaries

excludes = [
    "tkinter", "matplotlib", "numpy", "pandas", "scipy",
    "PIL", "IPython", "pytest", "unittest.test", "pydoc_data",
    "playwright",           # 只用 patchright，避免把两套 driver 都塞进去
]

a = Analysis(
    ["main.py"],
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

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="yooc-cli",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,              # UPX 压缩容易被杀软误判，关掉
        upx_exclude=[],
        runtime_tmpdir=None,
        console=True,           # 命令行程序，必须有控制台
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="yooc-cli",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="yooc-cli",
    )
