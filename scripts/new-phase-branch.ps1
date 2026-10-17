param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^(phase|feat|fix|docs|chore)/[a-z0-9][a-z0-9-]*$')]
    [string]$BranchName
)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
if (-not (Test-Path (Join-Path $ProjectRoot ".git"))) { throw "This folder is not a Git repository." }
$dirty = & git status --porcelain
if ($dirty) { throw "Working tree has changes. Review/commit them before starting a new branch." }
$current = (& git branch --show-current).Trim()
if ($current -ne "main") { throw "Switch to main in GitHub Desktop and sync it, then rerun this script." }
& git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    & git pull --ff-only
    if ($LASTEXITCODE -ne 0) { throw "Could not fast-forward main. Resolve sync in GitHub Desktop first." }
}
& git switch -c $BranchName
if ($LASTEXITCODE -ne 0) { throw "Could not create branch $BranchName." }
Write-Host "Now on branch $BranchName. Confirm the branch in VS Code before using a coding agent."
