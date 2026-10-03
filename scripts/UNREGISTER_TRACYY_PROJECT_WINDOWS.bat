@echo off
setlocal EnableExtensions
reg delete "HKCU\Software\Classes\.Tracyy" /f >nul 2>nul
reg delete "HKCU\Software\Classes\Tracyy.Project" /f >nul 2>nul
where ie4uinit.exe >nul 2>nul && ie4uinit.exe -show >nul 2>nul
echo Tracyy Project association removed.
pause
