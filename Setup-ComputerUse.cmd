@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
uv run --no-sync python scripts/setup_computer_use.py
pause
