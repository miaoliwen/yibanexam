@echo off
chcp 65001 >nul
setlocal

rem 浏览器版图形界面启动脚本
set "PY=py -3"
%PY% --version >nul 2>&1
if errorlevel 1 set "PY=python"

%PY% "%~dp0gui.py" %*
if errorlevel 1 pause
