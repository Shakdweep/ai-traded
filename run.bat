@echo off
REM One-click start on Windows: creates the virtual environment on first run.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Creating virtual environment...
  python -m venv .venv || goto :err
  .venv\Scripts\python -m pip install -r requirements.txt || goto :err
)
start "" http://127.0.0.1:8000
.venv\Scripts\python -m backend
goto :eof
:err
echo Setup failed. Install Python 3.10+ from python.org and try again.
pause
