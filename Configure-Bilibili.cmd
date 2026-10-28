@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
uv run --no-sync python scripts/configure_bili_login.py --show
pause
