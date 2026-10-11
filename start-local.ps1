param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Create .venv and install backend/requirements.txt first. See README.md: Local frontend development.'
}

$previousFrontendDir = $env:FRONTEND_DIR
Push-Location $projectRoot
try {
    $env:FRONTEND_DIR = Join-Path $projectRoot 'frontend'
    Write-Host "Payphone Pathfinder: http://127.0.0.1:$Port"
    Write-Host 'Configuration comes from .env. Route calculation requires reachable OSRM and VROOM services.'
    & $pythonPath -m uvicorn main:app --app-dir backend --host 127.0.0.1 --port $Port
    if ($LASTEXITCODE -ne 0) {
        throw "Local backend exited with code $LASTEXITCODE."
    }
}
finally {
    $env:FRONTEND_DIR = $previousFrontendDir
    Pop-Location
}
