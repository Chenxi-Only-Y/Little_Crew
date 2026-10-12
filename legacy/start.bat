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

rem ============================================================
rem  外部目录边界：**本项目目录里只放代码，不放模型与工具**
rem
rem  模型权重动辄几 GB，装进项目会污染仓库、也让项目无法搬迁。
rem  所以这些一律放外面（与 D:\AI\local-gen 同级或它自己的目录）：
rem
rem    LITTLE_CREW_TOOLS  edge-tts（配音）+ ffmpeg.exe（拼视频/混流）
rem    HF_HOME            HuggingFace 模型缓存（角色一致性用的
rem                       sentence-transformers 会往这里下载）
rem
rem  下面两行是**兜底默认值**：若外部目录不存在也不报错，
rem  只是相应的本地能力不可用（界面会标注"本机未就绪"）。
rem  想用别的位置，改成自己的路径即可。
rem ============================================================
if not defined LITTLE_CREW_TOOLS set "LITTLE_CREW_TOOLS=D:\AI\little-crew-tools"
if not defined HF_HOME set "HF_HOME=D:\AI\little-crew-tools\models\hf"

rem 没装外部工具时不拦启动 —— 只是少了本地配音/拼接能力。
rem 拦下来会让"只想用云端"的用户也启动不了，那不合理。
if not exist "%LITTLE_CREW_TOOLS%" (
  echo [i] 未找到外部工具目录 %LITTLE_CREW_TOOLS%
  echo     本地配音 / 视频拼接将不可用（云端功能不受影响）。
)

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
