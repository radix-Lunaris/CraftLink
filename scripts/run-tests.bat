@echo off
setlocal

rem ============================================================
rem  MC-Link 全量回归测试
rem
rem  不需要 VPS、不需要 Docker、不需要 Minecraft。
rem  由 tests\run_all.py 依次执行 6 个测试套件。
rem ============================================================

title MC-Link 回归测试

cd /d %~dp0

set PY=
where py >nul 2>nul && set PY=py
if not defined PY (
    where python >nul 2>nul && set PY=python
)
if not defined PY (
    echo [错误] 没有找到 Python。
    pause
    exit /b 1
)

cd /d %~dp0..
%PY% tests\run_all.py

echo.
pause
endlocal
