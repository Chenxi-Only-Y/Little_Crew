@echo off
rem ============================================================
rem  Little Crew - one-click launcher
rem  Double-click to start backend + frontend and open browser.
rem  Two black windows (backend/frontend) will appear; keep them
rem  open to run the app, close them to stop.
rem ============================================================
cd /d "%~dp0"

set "PYTHON=%~dp0.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf-8"

if not exist "%PYTHON%" (
  echo [ERROR] .venv not found. Please create it first:
  echo         python -m venv .venv ^&^& .venv\Scripts\activate ^&^& pip install -r requirements.txt
  pause
  exit /b 1
)

echo [1/3] Starting backend ...  http://127.0.0.1:8000
start "little-crew-backend" cmd /k "%PYTHON%" scripts\run_server.py

echo [2/3] Starting frontend ...  http://127.0.0.1:8501
start "little-crew-frontend" cmd /k "%PYTHON%" -m streamlit run frontend\streamlit_app.py --server.port 8501 --server.headless true --browser.gatherUsageStats false

echo [3/3] Waiting for services, then opening browser ...
timeout /t 8 /nobreak >nul
start "" "http://127.0.0.1:8501"

echo.
echo Done. Browser opened at http://127.0.0.1:8501
echo Two windows = backend(8000) / frontend(8501). Keep them open.
pause
