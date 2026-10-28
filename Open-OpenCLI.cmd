@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" "scripts\opencli_cli.py" --open-browser
if errorlevel 1 pause
