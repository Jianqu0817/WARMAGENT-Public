@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run Setup.cmd first.
  exit /b 1
)
uv pip install --python .venv/Scripts/python.exe ziglang==0.16.0
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe scripts/build_native_text.py
exit /b %errorlevel%
