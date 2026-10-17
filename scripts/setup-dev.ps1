$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

Write-Host "Checking for Python 3.12..."
$py = Get-Command py -ErrorAction SilentlyContinue
if (-not $py) {
    throw "Python launcher 'py' was not found. Install Python 3.12 x64 from https://www.python.org/downloads/windows/ and enable PATH, then reopen PowerShell."
}
& py -3.12 --version
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.12 was not found. Run 'py -0p' to list installed Python versions, then install Python 3.12 x64."
}

$venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Creating project-local virtual environment..."
    & py -3.12 -m venv (Join-Path $ProjectRoot ".venv")
    if ($LASTEXITCODE -ne 0) { throw "Failed to create virtual environment." }
}

Write-Host "Installing dependencies inside .venv (not globally)..."
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }
& $venvPython -m pip install -e ".[dev]"
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed. Share the error with the coding agent; do not install packages globally." }
Write-Host "Setup complete. Next run: .\scripts\check.ps1"
