<#
.SYNOPSIS
    Install Little Crew's local tools into an EXTERNAL folder (outside the repo).

.DESCRIPTION
    Project boundary: the project directory holds CODE ONLY -- no models, no
    binaries. Model weights are multi-GB; putting them in the repo pollutes it
    and makes the project non-relocatable.

    So this script installs two things into -ToolsDir (default
    D:\AI\little-crew-tools):

      venv\           marginal Python deps (edge-tts), invoked as a SUBPROCESS.
                      Deliberately NOT wired via PYTHONPATH: that would let its
                      deps (httpx/pydantic) fight the project venv's versions.
      bin\ffmpeg.exe  FFmpeg (image->video slideshow, muxing the voice track,
                      encoding whitebox frame sequences to mp4).

    Why FFmpeg is needed (not just for dubbing): the project already uses it in
    two places -- video.py (image slideshow) and whitebox_video.py (frames->mp4).

    No project code changes needed afterwards: start.bat exports
    LITTLE_CREW_TOOLS and find_ffmpeg() looks in <ToolsDir>\bin\.
    Your own ffmpeg on PATH still wins -- this never silently overrides it.

    NOTE: this file is intentionally ASCII-only. Windows PowerShell 5.1 reads
    scripts as ANSI, so non-ASCII text here would be mojibake and break parsing.

.PARAMETER ToolsDir
    External tools folder. Defaults to LITTLE_CREW_TOOLS or D:\AI\little-crew-tools.

.PARAMETER SkipFfmpeg
    Install Python deps only; skip the ~83 MB FFmpeg download.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\setup_tools.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\setup_tools.ps1 -ToolsDir E:\lc-tools -SkipFfmpeg
#>
[CmdletBinding()]
param(
    [string]$ToolsDir = $(if ($env:LITTLE_CREW_TOOLS) { $env:LITTLE_CREW_TOOLS } else { 'D:\AI\little-crew-tools' }),
    [switch]$SkipFfmpeg
)

$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'

function Info($m) { Write-Host "  $m" }
function Warn($m) { Write-Host "  [warn] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "  [ERROR] $m" -ForegroundColor Red; exit 1 }

Write-Host ''
Write-Host 'Little Crew - external tools setup' -ForegroundColor Cyan
Write-Host "Target folder: $ToolsDir"
Write-Host ''

# ---------- 1) pick a CLEAN python for the external venv ----------
# Deliberately NOT the project's .venv python: that would tie the external venv
# to the project interpreter, the opposite of "the two must not pollute
# each other".
$py = $null
$cands = @('D:\Python\python.exe')
$onPath = Get-Command python -ErrorAction SilentlyContinue
if ($onPath) { $cands += $onPath.Source }
foreach ($cand in $cands) {
    if ($cand -and (Test-Path $cand)) { $py = $cand; break }
}
if (-not $py) { Fail 'No python interpreter found. Install Python 3.10+ and add it to PATH.' }
Info "Interpreter: $py"

# ---------- 2) folders ----------
New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
foreach ($sub in @('bin', 'models\hf', 'scripts')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $ToolsDir $sub) | Out-Null
}
Info "Folders ready: $ToolsDir"

# ---------- 3) external venv + edge-tts ----------
$venvPy = Join-Path $ToolsDir 'venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Info '[1/3] Creating external venv ...'
    & $py -m venv (Join-Path $ToolsDir 'venv')
    if ($LASTEXITCODE -ne 0) { Fail 'venv creation failed' }
} else {
    Info '[1/3] External venv already exists, skipping'
}

Info '[2/3] Installing edge-tts (dubbing, ~1 MB) ...'
& $venvPy -m pip install --quiet --upgrade pip
& $venvPy -m pip install --quiet edge-tts
if ($LASTEXITCODE -ne 0) { Fail 'edge-tts install failed' }
Info 'edge-tts installed'

# ---------- 4) ffmpeg ----------
if ($SkipFfmpeg) {
    Info '[3/3] Skipping FFmpeg (per -SkipFfmpeg)'
} else {
    $have = Get-ChildItem (Join-Path $ToolsDir 'bin') -Filter 'ffmpeg*' -File -ErrorAction SilentlyContinue
    if ($have) {
        Info "[3/3] bin\ already has $($have[0].Name), skipping download"
    } else {
        Info '[3/3] Fetching FFmpeg (~83 MB) ...'
        $extractor = Join-Path $PSScriptRoot 'extract_ffmpeg.py'
        if (-not (Test-Path $extractor)) { Fail "Not found: $extractor" }
        & $venvPy $extractor --out (Join-Path $ToolsDir 'bin') --python $venvPy
        if ($LASTEXITCODE -ne 0) { Fail 'FFmpeg extraction failed (check network)' }
    }
}

# ---------- 5) self-check ----------
Write-Host ''
Write-Host 'Self-check:' -ForegroundColor Cyan
$ff = Join-Path $ToolsDir 'bin\ffmpeg.exe'
if (Test-Path $ff) {
    $v = (& $ff -version 2>&1 | Select-Object -First 1)
    Info "OK  ffmpeg: $v"
} elseif ($SkipFfmpeg) {
    Warn 'ffmpeg: skipped (image slideshow / muxing will be unavailable)'
} else {
    Warn 'ffmpeg: NOT found'
}
Info "OK  edge-tts: $venvPy"

$size = (Get-ChildItem $ToolsDir -Recurse -File -ErrorAction SilentlyContinue |
         Measure-Object -Property Length -Sum).Sum
Info ("Total size: {0:N1} MB" -f ($size / 1MB))

Write-Host ''
Write-Host 'Done. No project code changes needed:' -ForegroundColor Green
Write-Host "  start.bat exports LITTLE_CREW_TOOLS=$ToolsDir"
Write-Host '  To use another location, edit those two lines in start.bat (or set the env var yourself).'
Write-Host ''
Write-Host 'Note: your own ffmpeg on PATH takes priority; this never overrides it.' -ForegroundColor DarkGray
Write-Host ''
