# -*- coding: utf-8 -*-
"""yibanexam 单元测试（标准库 unittest，不联网、不碰真实账号/配置）。"""
import sys

# Windows 控制台/CI 默认可能是 cp1252 或 GBK；测试会打印中文告警，统一成 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
