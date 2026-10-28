@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
set WARM_SYNC_EXTRAS=--extra bili --extra documents
if exist "%~dp0var\models\bge-small-zh-v1.5\manifest.json" set WARM_SYNC_EXTRAS=--extra bili --extra neural --extra documents
uv sync --frozen --python 3.12 %WARM_SYNC_EXTRAS%
if errorlevel 1 exit /b %errorlevel%
"%~dp0.venv\Scripts\python.exe" -m warmagent prepare
if errorlevel 1 exit /b %errorlevel%
"%~dp0.venv\Scripts\python.exe" -m warmagent check
