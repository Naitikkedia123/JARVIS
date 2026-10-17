param(
    [string]$VenvPath = ".venv"
)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if ([System.IO.Path]::IsPathRooted($VenvPath)) {
    $resolvedVenvPath = [System.IO.Path]::GetFullPath($VenvPath)
} else {
    $resolvedVenvPath = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot $VenvPath))
}
$python = Join-Path $resolvedVenvPath "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Virtual environment not found at '$resolvedVenvPath'. Run .\scripts\setup-dev.ps1 -VenvPath `"$VenvPath`" first."
}
$versionOutput = & $python --version 2>&1
if ($LASTEXITCODE -ne 0) { throw "Could not determine the Python version for '$python'." }
$pythonVersion = ($versionOutput | Out-String).Trim()
if ($pythonVersion -notmatch '^Python 3\.12(?:\.|$)') {
    throw "The target uses $pythonVersion; Phase 01 checks require Python 3.12. The target was left unchanged."
}

Write-Host "Running Ruff with $pythonVersion..."
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
