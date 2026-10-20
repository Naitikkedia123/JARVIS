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
    throw "The target uses $pythonVersion; the local server requires Python 3.12. The target was left unchanged."
}

$listenerHostOutput = & $python -c "from jarvis.main import app; print(app.state.listener_host)"
if ($LASTEXITCODE -ne 0) { throw "JARVIS listener configuration validation failed; the server was not started." }
$listenerHost = ($listenerHostOutput | Out-String).Trim()
try {
    [void][System.Net.IPAddress]::Parse($listenerHost)
} catch {
    throw "JARVIS returned an invalid listener address; the server was not started."
}

Write-Host "Starting JARVIS with $pythonVersion on ${listenerHost}:8000..."
& $python -m uvicorn jarvis.main:app --host $listenerHost --port 8000 --reload
