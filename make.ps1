# 常用任务入口（Windows 版，对应 Makefile）
#
#   .\make.ps1 doctor     环境自检
#   .\make.ps1 test       全量测试
#   .\make.ps1 serve      起 API（默认 8787）
#   .\make.ps1 episode -Ep EF40
#   .\make.ps1 lib        列出跑过的每一集
#   .\make.ps1 report -Ep EF40
#   .\make.ps1 prune      outputs 超 2GB 才清
#   .\make.ps1 services   一键起 ComfyUI + TTS（另开窗口）
#   .\make.ps1 stop       停掉 ComfyUI + TTS

param(
  [Parameter(Position = 0)][string]$Task = "help",
  [string]$Ep = "EF40",
  [int]$Port = 8787,
  [string]$ComfyInput = "D:\ComfyUI\ComfyUI\input"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot   # 不管从哪里调用，都按仓库根解析相对路径
$PY = "D:\Python\python.exe"
$env:PYTHONPATH = "$PSScriptRoot\src"
$env:PYTHONIOENCODING = "utf-8"
$wd = "outputs/_$Ep"

function Show-Help {
  Write-Host "用法: .\make.ps1 <任务> [-Ep EF40] [-Port 8787]"
  Write-Host "  doctor    环境自检（服务/key/模型/磁盘/ffmpeg）"
  Write-Host "  test      全量测试"
  Write-Host "  serve     起对外 API"
  Write-Host "  episode   跑一集（-Ep 指定集号）"
  Write-Host "  lib       列出跑过的每一集"
  Write-Host "  report    生成该集报告页"
  Write-Host "  prune     outputs 超 2GB 才深度清理"
  Write-Host "  services  一键起 ComfyUI + TTS"
  Write-Host "  stop      停掉 ComfyUI + TTS"
}

switch ($Task) {
  "help"    { Show-Help }
  "doctor"  { & $PY -m little_crew.cli doctor }
  "test"    { & $PY tests/run_all.py }
  "serve"   { & $PY -m little_crew.cli serve --port $Port }
  "lib"     { & $PY -m little_crew.cli library }
  "report"  { & $PY -m little_crew.cli report $wd }
  "prune"   { & $PY tools/prune_artifacts.py --auto --outputs outputs --max-gb 2.0 }
  "episode" {
    & $PY -m little_crew.runtime.episode_demo --llm --accept-script `
      --voice --final --vision-review --backend comfyui --use-white-model `
      --comfy-input $ComfyInput --episode-id $Ep --workdir $wd
  }
  "services" {
    # 为什么用 Start-Process 而不是同一窗口：两个服务都是常驻进程，
    # 前台跑会把当前终端的控制权交出去（然后就敲不了别的命令了）。
    Write-Host "  起 ComfyUI（8188）…"
    Start-Process -FilePath "D:\ComfyUI\python_embeded\python.exe" `
      -ArgumentList @("main.py", "--port", "8188", "--disable-auto-launch") `
      -WorkingDirectory "D:\ComfyUI\ComfyUI" -WindowStyle Minimized
    Write-Host "  起 TTS（8110）…"
    Start-Process -FilePath "D:\AI\local-ai\tts\bin\breeze-tts-2-v0.1.6-windows-x64-vulkan\breeze-server.exe" `
      -ArgumentList @("D:\AI\local-ai\tts\models\breeze-tts-2-q8_0.gguf",
                      "--host", "127.0.0.1", "--port", "8110",
                      "--voices-dir", "D:\AI\local-ai\tts\voices") `
      -WindowStyle Minimized
    Write-Host "  等 20 秒后跑 doctor 看是否就绪…"
    Start-Sleep -Seconds 20
    & $PY -m little_crew.cli doctor
  }
  "stop" {
    Get-Process -Name breeze-server -ErrorAction SilentlyContinue |
      ForEach-Object { Stop-Process -Id $_.Id -Force }
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
      Where-Object { $_.CommandLine -match 'main\.py' } |
      ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Start-Sleep -Seconds 2
    Write-Host "  已停服务 · GPU: $(& nvidia-smi --query-gpu=memory.used --format=csv,noheader)"
  }
  default { Write-Host "未知任务：$Task"; Show-Help; exit 2 }
}
