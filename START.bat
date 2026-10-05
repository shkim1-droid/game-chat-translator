@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap.ps1"
if errorlevel 1 (
 echo.
 echo Failed. Please send setup-log.txt from this folder.
 pause
 exit /b 1
)
endlocal
