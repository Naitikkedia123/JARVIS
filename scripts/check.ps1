$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Virtual environment not found. Run .\scripts\setup-dev.ps1 first." }

Write-Host "Running Ruff..."
& $python -m ruff check .
if ($LASTEXITCODE -ne 0) { throw "Ruff failed. Fix lint issues before proceeding." }

Write-Host "Running tests..."
& $python -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "Tests failed. Review the failure output." }

Write-Host "Checking Git whitespace errors..."
if (Get-Command git -ErrorAction SilentlyContinue) {
    & git diff --check
    if ($LASTEXITCODE -ne 0) { throw "git diff --check failed." }
}
Write-Host "All local checks passed."
