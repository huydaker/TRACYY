@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "APP=%CD%\Tracyy.exe"
set "ICON=%CD%\TracyyProject.ico"

if not exist "%APP%" (
    echo [ERROR] Tracyy.exe not found in this folder.
    pause
    exit /b 1
)

if not exist "%ICON%" set "ICON=%APP%"

reg add "HKCU\Software\Classes\.Tracyy" /ve /d "Tracyy.Project" /f >nul
reg add "HKCU\Software\Classes\Tracyy.Project" /ve /d "Tracyy Project" /f >nul
reg add "HKCU\Software\Classes\Tracyy.Project\DefaultIcon" /ve /d "\"%ICON%\",0" /f >nul
reg add "HKCU\Software\Classes\Tracyy.Project\shell\open\command" /ve /d "\"%APP%\" \"%%1\"" /f >nul

where ie4uinit.exe >nul 2>nul && ie4uinit.exe -show >nul 2>nul

echo Tracyy Project association registered for .Tracyy
pause
