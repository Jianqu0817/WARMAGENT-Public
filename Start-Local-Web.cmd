@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
"%~dp0.venv\Scripts\python.exe" -u -m warmagent web --live --ui %*
if errorlevel 1 pause
