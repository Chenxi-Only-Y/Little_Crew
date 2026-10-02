@echo off
rem ============================================================
rem  Little Crew - one-click launcher
rem  Double-click to start backend + frontend and open browser.
rem  Two black windows (backend/frontend) will appear; keep them
rem  open to run the app, close them to stop.
rem
rem  前端已从 Streamlit 换成导演台（React + Vite，端口 5183）。
rem  旧版 frontend\ 目录已删除；若需要历史版本请查 git 历史。
rem ============================================================
cd /d "%~dp0"

set "PYTHON=%~dp0.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf-8"
set "WEB=%~dp0web"
set "URL=http://127.0.0.1:5183"

if not exist "%PYTHON%" (
  echo [ERROR] .venv not found. Please create it first:
  echo         python -m venv .venv ^&^& .venv\Scripts\activate ^&^& pip install -r requirements.txt
  pause
  exit /b 1
)

rem 前端依赖：没有 node_modules 就先装。
rem 不检查 pnpm 是否在 PATH 里就调用，会得到一句含糊的
rem "不是内部或外部命令"，新手很难判断缺什么。
where pnpm >nul 2>nul
if errorlevel 1 (
  echo [ERROR] pnpm not found in PATH. Please install Node.js and pnpm first:
  echo         npm install -g pnpm
  pause
  exit /b 1
)

if not exist "%WEB%\node_modules" (
  echo [0/3] Installing frontend dependencies ^(first run^) ...
  pushd "%WEB%"
  call pnpm install
  popd
)

echo [1/3] Starting backend ...  http://127.0.0.1:8000
start "little-crew-backend" cmd /k "%PYTHON%" scripts\run_server.py

echo [2/3] Starting frontend ...  %URL%
start "little-crew-frontend" cmd /k "cd /d "%WEB%" && pnpm run dev"

echo [3/3] Waiting for services, then opening browser ...
timeout /t 8 /nobreak >nul
start "" "%URL%"

echo.
echo Done. Browser opened at %URL%
echo Two windows = backend(8000) / director console(5183). Keep them open.
pause
