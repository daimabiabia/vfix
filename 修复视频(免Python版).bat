@echo off
chcp 65001 >nul
title Batch Video Repair Tool (standalone exe)
if "%~1"=="" (
  echo.
  echo   Drag video files or a folder onto this bat to repair them.
  echo   Files in the same folder can serve as parameter-set donors.
  echo.
  pause
  goto :EOF
)
"%~dp0dist\vfixtool.exe" %*
echo.
pause
