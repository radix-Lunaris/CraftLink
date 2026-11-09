@echo off
setlocal

rem ============================================================
rem  MC-Link 客户端打包脚本 (PyInstaller)
rem
rem  结果: client\dist\MC-Link.exe   （单文件，双击即用，无控制台窗口）
rem  请把生成的 exe 发给朋友，对方不需要安装 Python。
rem ============================================================

title MC-Link 打包

cd /d "%~dp0"

set PY=
where py >nul 2>nul && set PY=py
if not defined PY (
    where python >nul 2>nul && set PY=python
)
if not defined PY (
    echo [错误] 没有找到 Python，请先安装。
    pause
    exit /b 1
)

echo ============================================================
echo   安装 PyInstaller ...
echo ============================================================
%PY% -m pip install --upgrade pip
%PY% -m pip install --upgrade pyinstaller
if errorlevel 1 (
    echo [错误] PyInstaller 安装失败，请检查网络。
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   开始打包 ...
echo ============================================================
cd /d "%~dp0..\client"
%PY% -m PyInstaller "..\scripts\mclink-client.spec" --noconfirm --clean
if errorlevel 1 (
    echo.
    echo [错误] 打包失败。
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   打包完成
echo ============================================================
if exist "dist\MC-Link.exe" (
    echo   输出文件: %CD%\dist\MC-Link.exe
    for %%A in ("dist\MC-Link.exe") do echo   文件大小: %%~zA 字节
    echo.
    echo   双击该文件即可启动客户端。
) else (
    echo   [警告] 没有找到 dist\MC-Link.exe，请查看上面的日志。
)

echo.
pause
endlocal
