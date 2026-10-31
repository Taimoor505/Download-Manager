# Builds dist\vidgrab\vidgrab.exe (a folder app; zip the folder to share it).
# Run from the repo root with the venv's dependencies installed:
#   pip install -e ".[desktop]" pyinstaller
param(
    # ffmpeg.exe to bundle; defaults to the one on PATH
    [string]$Ffmpeg = (Get-Command ffmpeg -ErrorAction Stop).Source
)
$ErrorActionPreference = "Stop"
$python = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }

& $python -m vidgrab.icon
& $python -m PyInstaller --noconfirm --clean --windowed `
    --name vidgrab `
    --icon packaging\vidgrab.ico `
    --add-data "vidgrab\static;vidgrab\static" `
    --add-binary "$Ffmpeg;." `
    --collect-submodules yt_dlp `
    --collect-data yt_dlp_ejs `
    --hidden-import uvicorn.logging `
    --hidden-import uvicorn.loops.auto `
    --hidden-import uvicorn.protocols.http.auto `
    --hidden-import uvicorn.lifespan.on `
    packaging\vidgrab_desktop.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
Write-Host "Built dist\vidgrab\vidgrab.exe"
