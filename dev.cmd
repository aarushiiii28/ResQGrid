@echo off
rem ResQGrid one-command start: API on :8010 (new window) and web UI on :5180 (this window).
rem Needs PostgreSQL running and DATABASE_URL set in .env.
setlocal
cd /d "%~dp0"

if not exist venv\Scripts\python.exe (
  echo Creating virtual environment...
  python -m venv venv || goto :fail
)
venv\Scripts\python.exe -m pip install -q -e ".[dev]" || goto :fail

if not exist frontend\node_modules (
  echo Installing frontend packages...
  pushd frontend
  call npm install || (popd & goto :fail)
  popd
)

start "ResQGrid API (close to stop)" cmd /k venv\Scripts\python.exe scripts\start_backend.py
echo API:  http://localhost:8010/health   Docs: http://localhost:8010/docs
echo Web:  http://localhost:5180
cd frontend
call npm run dev
goto :eof

:fail
echo Setup failed. See the message above.
exit /b 1
