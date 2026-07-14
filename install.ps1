[CmdletBinding()]
param(
    [string]$Destination,
    [switch]$Force,
    [switch]$SkipDependencies
)

$ErrorActionPreference = 'Stop'
$RepoRoot = $PSScriptRoot

if (-not $Destination) {
    $CodexHome = $env:CODEX_HOME
    if (-not $CodexHome) {
        $CodexHome = Join-Path $HOME '.codex'
    }
    $Destination = Join-Path (Join-Path $CodexHome 'skills') 'translate-book'
}

$Python = Get-Command python -ErrorAction SilentlyContinue
if (-not $Python) {
    throw 'Python 3 was not found. Install it first and ensure python.exe is on PATH.'
}

if ((Test-Path -LiteralPath $Destination) -and -not $Force) {
    throw "Destination already exists: $Destination. Re-run with -Force to update it."
}

if (-not $SkipDependencies) {
    & $Python.Source -m pip install -r (Join-Path $RepoRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) {
        throw 'Python dependency installation failed.'
    }
}

New-Item -ItemType Directory -Force -Path $Destination | Out-Null
Copy-Item -LiteralPath (Join-Path $RepoRoot 'SKILL.md') -Destination $Destination -Force
New-Item -ItemType Directory -Force -Path (Join-Path $Destination 'scripts') | Out-Null
Get-ChildItem -LiteralPath (Join-Path $RepoRoot 'scripts') -File |
    Copy-Item -Destination (Join-Path $Destination 'scripts') -Force
Copy-Item -LiteralPath (Join-Path $RepoRoot 'agents') -Destination $Destination -Recurse -Force

Write-Host "Installed Codex skill to: $Destination"
& $Python.Source -B (Join-Path $Destination 'scripts\doctor.py')
if ($LASTEXITCODE -ne 0) {
    Write-Warning 'The skill is installed, but required external tools are missing. See the diagnostics above.'
    exit $LASTEXITCODE
}
