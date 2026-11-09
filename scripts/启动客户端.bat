@echo off
setlocal

rem ============================================================
rem  MC-Link 客户端启动脚本（Windows）
rem
rem  双击本文件即可启动图形界面。
rem  前提：本机已安装 Python 3.9+（安装时勾选 Add Python to PATH）
rem ============================================================

title MC-Link 客户端

cd /d "%~dp0"

echo ============================================================
echo   MC-Link  Minecraft 联机客户端
echo ============================================================
echo.

rem ---- 查找 Python ----
set PY=
where py >nul 2>nul && set PY=py
if not defined PY (
    where python >nul 2>nul && set PY=python
)

if not defined PY (
    echo [错误] 没有找到 Python。
    echo.
    echo 请先安装 Python： https://www.python.org/downloads/
    echo 安装时务必勾选 "Add python.exe to PATH"。
    echo.
    pause
    exit /b 1
)

rem ---- 进入客户端目录 ----
cd /d "%~dp0..\client"
if not exist "main.py" (
    echo [错误] 找不到 client\main.py
    echo 请确认本脚本放在 mc-link-v1\scripts\ 目录下。
    pause
    exit /b 1
)

echo 使用解释器: %PY%
echo 启动图形界面 ...
echo.

%PY% main.py

if errorlevel 1 (
    echo.
    echo [提示] 客户端异常退出，错误码 %errorlevel%
    pause
)

endlocal
