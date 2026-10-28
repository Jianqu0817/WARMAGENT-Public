@echo off
cd /d "%~dp0"
set "PYTHONUTF8=1"
if not exist ".venv\Scripts\python.exe" (
  echo Run Setup.cmd first.
  exit /b 1
)
.venv\Scripts\python.exe -m pytest tests/test_perf_archive.py tests/test_perf_graph.py tests/test_perf_database.py tests/test_personal_space.py tests/test_self_direction.py tests/test_autonomy_choices.py tests/test_personal_autonomy.py -q
exit /b %errorlevel%
