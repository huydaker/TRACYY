@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0.."
title Tracyy V2 - Windows Build

echo ============================================================
echo  TRACYY V2 - WINDOWS BUILD
echo ============================================================
echo.

rem --- locate Python ------------------------------------------------------
set "PY_CMD="
where py >nul 2>nul
if not errorlevel 1 (
    py -3.12 --version >nul 2>nul
    if not errorlevel 1 set "PY_CMD=py -3.12"
)
if not defined PY_CMD (
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3.11 --version >nul 2>nul
        if not errorlevel 1 set "PY_CMD=py -3.11"
    )
)
if not defined PY_CMD (
    where python >nul 2>nul
    if not errorlevel 1 set "PY_CMD=python"
)
if not defined PY_CMD (
    echo [ERROR] Khong tim thay Python 3.11+.
    echo   winget install --id Python.Python.3.12 -e
    goto :fail
)

rem --- preflight ----------------------------------------------------------
if not exist "main.py" (
    echo [ERROR] Khong tim thay main.py. Chay script tu thu muc Tracyy.
    goto :fail
)
if not exist "license_config.enc" (
    echo [ERROR] Khong tim thay license_config.enc.
    goto :fail
)
if exist "license_config.json" (
    echo [ERROR] Xoa license_config.json truoc khi build ^(khong duoc dong goi^).
    goto :fail
)

rem --- build environment --------------------------------------------------
if not exist ".venv-build\Scripts\python.exe" (
    echo [1/7] Tao moi truong build...
    %PY_CMD% -m venv .venv-build
    if errorlevel 1 goto :fail
)
set "PYTHON=%CD%\.venv-build\Scripts\python.exe"

echo [2/7] Cai dependencies...
"%PYTHON%" -m pip install --upgrade --quiet pip setuptools wheel
if errorlevel 1 goto :fail
"%PYTHON%" -m pip install --quiet -r requirements-dev.txt
if errorlevel 1 goto :fail

echo [3/7] Kiem tra ma nguon...
"%PYTHON%" -m ruff check tracyy tools
if errorlevel 1 goto :fail
"%PYTHON%" -m pytest -q
if errorlevel 1 goto :fail

echo [4/7] Xoa build cu...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "license_cache.dat" del /q "license_cache.dat"
for /d /r %%d in (__pycache__) do @if exist "%%d" rmdir /s /q "%%d"

echo [5/7] Build Tracyy.exe...
"%PYTHON%" -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\tracyy.spec
if errorlevel 1 goto :fail

echo [6/7] Build TracyyCueServer.exe...
"%PYTHON%" -m PyInstaller --noconfirm --clean --distpath dist_server --workpath build_server packaging\tracyy_cue_server.spec
if errorlevel 1 goto :fail

copy /Y "dist_server\TracyyCueServer.exe" "dist\Tracyy\TracyyCueServer.exe" >nul
if errorlevel 1 goto :fail

echo [7/7] Don dep...
for %%f in (Tracyy.ico Tracyy.png TracyyProject.ico) do (
    if exist "icon\%%f" copy /Y "icon\%%f" "dist\Tracyy\%%f" >nul
)
for %%f in (REGISTER_TRACYY_PROJECT_WINDOWS.bat UNREGISTER_TRACYY_PROJECT_WINDOWS.bat) do (
    if exist "scripts\%%f" copy /Y "scripts\%%f" "dist\Tracyy\%%f" >nul
)
if exist "dist\Tracyy\license_config.json" del /q "dist\Tracyy\license_config.json"
if exist "dist_server" rmdir /s /q "dist_server"
if exist "build_server" rmdir /s /q "build_server"

echo.
echo ============================================================
echo  BUILD THANH CONG
echo ============================================================
echo App: %CD%\dist\Tracyy\Tracyy.exe
echo Copy NGUYEN thu muc: %CD%\dist\Tracyy
echo.
echo De gan icon va double-click file .Tracyy, chay 1 lan:
echo   %CD%\dist\Tracyy\REGISTER_TRACYY_PROJECT_WINDOWS.bat
echo ============================================================
start "" "%CD%\dist\Tracyy"
pause
exit /b 0

:fail
echo.
echo ============================================================
echo  BUILD THAT BAI - xem dong ERROR phia tren
echo ============================================================
pause
exit /b 1
