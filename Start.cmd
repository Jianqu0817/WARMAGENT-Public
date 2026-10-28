@echo off
cd /d "%~dp0"
set "PYTHONUTF8=1"
if not exist ".venv\Scripts\python.exe" (
  call "%~dp0Setup.cmd"
  if errorlevel 1 (
    pause
    exit /b 1
  )
)
"%~dp0.venv\Scripts\python.exe" -u -m warmagent console %*
if errorlevel 1 pause
