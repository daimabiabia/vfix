@echo off
chcp 65001 >nul
title Batch Video Repair Tool
setlocal
set PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe
if not exist "%PY%" set PY=python

if "%~1"=="" goto NOPARM
"%PY%" "%~dp0vfix.py" %*
goto END

:NOPARM
echo.
echo   Drag video files or a folder onto this bat to repair them.
echo   Or type a full path below (drag the folder here is easiest):
echo.
set /p TARGET=  Path:
"%PY%" "%~dp0vfix.py" %TARGET%

:END
echo.
pause
