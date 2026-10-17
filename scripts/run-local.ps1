$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Virtual environment not found. Run .\scripts\setup-dev.ps1 first." }
& $python -m uvicorn jarvis.main:app --host 127.0.0.1 --port 8000 --reload
