@echo off
chcp 65001 >nul
setlocal

rem 原生桌面版（tkinter）启动脚本
set "PY=py -3"
%PY% -c "import tkinter" >nul 2>&1
if errorlevel 1 set "PY=python"
%PY% -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    echo.
    echo [错误] 没找到带 tkinter 的 Python 3.11+。
    echo 请安装官方 Python（安装时保留 tcl/tk 组件），或运行浏览器版：python gui.py
    echo 也可以直接使用 Release 中的 yibanexam.exe。
    echo.
    pause
    exit /b 2
)

%PY% "%~dp0gui_tk.py" %*
if errorlevel 1 pause
