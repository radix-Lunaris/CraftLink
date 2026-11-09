@echo off
chcp 65001 >nul
setlocal

rem ============================================================
rem  CraftLink - build the real Windows single-file exe
rem ============================================================
rem
rem  WHY THIS SCRIPT EXISTS
rem    Development happens in WSL2, but the client ships as a WINDOWS
rem    single-file exe. PyInstaller cannot cross-compile, so it has to run
rem    under Windows python. WSL can call Windows python.exe directly, so
rem    this script maps the project drive to Z: and builds from there --
rem    no manual switching to a Windows shell.
rem
rem  USAGE (from WSL):
rem    cd /tmp && cmd.exe /c "C:\Windows\Temp\clb.bat"
rem    (copy this file next to a .bat name you can execute)
rem
rem  ARTIFACTS
rem    client\dist\CraftLink.exe          GUI, no console
rem    client\dist\CraftLink-console.exe  with console, for tracebacks
rem
rem  NOTE: PyInstaller 7.0 refuses to run as administrator (warning for now).
rem
rem  KEEP THIS FILE ASCII-ONLY. cmd.exe parses .bat using the OEM code page,
rem  and non-ASCII bytes in echo/rem lines corrupt the parse ("'xit' is not
rem  recognized..."), which looks exactly like a broken build script.
rem ============================================================

echo ===== CraftLink Windows build =====

net use Z: \\wsl.localhost\Ubuntu /persistent:no >nul 2>&1

if not exist Z:\home\ai_workspace\CraftLink-V3\client\main.py (
    echo [FAIL] cannot see the project at Z:\home\ai_workspace\CraftLink-V3\client
    echo        Check the WSL distro name and the project path.
    net use
    exit /b 1
)

pushd Z:\home\ai_workspace\CraftLink-V3\client

python -m PyInstaller ..\scripts\mclink-client.spec --noconfirm --clean --log-level WARN
set RC=%ERRORLEVEL%
echo PyInstaller exit: %RC%

if not "%RC%"=="0" (
    popd
    exit /b %RC%
)

echo.
echo ===== selfcheck (console exe) =====
dist\CraftLink-console.exe --selfcheck
set RC=%ERRORLEVEL%
echo selfcheck exit: %RC%

popd
exit /b %RC%
