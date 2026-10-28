@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" "scripts\setup_opencli.py" --open-browser %*
pause
