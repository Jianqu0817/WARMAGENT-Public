@echo off
cd /d "%~dp0"
set "PYTHONUTF8=1"
if not exist ".venv\Scripts\python.exe" (
  echo Run Setup.cmd first.
  exit /b 1
)
.venv\Scripts\python.exe -m benchmarks.perf_archive_lab
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe -m benchmarks.perf_graph_lab
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe -m benchmarks.perf_database_lab
exit /b %errorlevel%
