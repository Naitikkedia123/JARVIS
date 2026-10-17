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
$venvPython = Join-Path $resolvedVenvPath "Scripts\python.exe"

function Get-PythonVersion([string]$PythonPath) {
    $versionOutput = & $PythonPath --version 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Could not determine the Python version for '$PythonPath'."
    }
    return ($versionOutput | Out-String).Trim()
}

if (Test-Path -LiteralPath $resolvedVenvPath) {
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw "The target '$resolvedVenvPath' exists but is not a usable virtual environment. It was left unchanged. Choose another -VenvPath."
    }
    $existingVersion = Get-PythonVersion $venvPython
    if ($existingVersion -notmatch '^Python 3\.12(?:\.|$)') {
        throw "The existing target uses $existingVersion; Phase 01 requires Python 3.12. It was left unchanged. Choose a new -VenvPath."
    }
} else {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if (-not $launcher) {
        throw "Python launcher 'py' was not found. Install Python 3.12 using your approved machine setup, then retry."
    }
    $launcherVersion = & py -3.12 --version 2>&1
    if ($LASTEXITCODE -ne 0 -or ($launcherVersion | Out-String) -notmatch '(?m)^Python 3\.12(?:\.|\s|$)') {
        throw "The active 'py' launcher cannot select Python 3.12. No environment was created and no packages were installed."
    }

    Write-Host "Creating project-local virtual environment at $resolvedVenvPath..."
    & py -3.12 -m venv $resolvedVenvPath
    if ($LASTEXITCODE -ne 0) { throw "Failed to create virtual environment at '$resolvedVenvPath'." }
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw "The launcher did not create the expected environment at '$resolvedVenvPath'."
    }
    $createdVersion = Get-PythonVersion $venvPython
    if ($createdVersion -notmatch '^Python 3\.12(?:\.|$)') {
        throw "The newly created target reports $createdVersion; Phase 01 requires Python 3.12. The environment was not removed or recreated."
    }
}

Write-Host "Installing dependencies inside $resolvedVenvPath (not globally)..."
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }
& $venvPython -m pip install -e ".[dev]"
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed. Share the error; do not install packages globally." }
Write-Host "Setup complete. Next run: .\scripts\check.ps1 -VenvPath `"$VenvPath`""
