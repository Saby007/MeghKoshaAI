[CmdletBinding()]
param(
    [ValidateSet('All', 'Backend', 'Frontend', 'Build', 'Browser')]
    [string] $Suite = 'All',
    [string] $PythonExecutable = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$settings = @{
    MEGHKOSHA_AI_ENABLED = 'false'
    AI_PROJECT_ENDPOINT = 'https://example.test/api/projects/test'
    AI_SERVICES_ENDPOINT = $null
    MODEL_ROUTER_DEPLOYMENT_NAME = $null
    AZURE_TENANT_ID = '11111111-1111-1111-1111-111111111111'
    MEGHKOSHA_API_CLIENT_ID = '33333333-3333-3333-3333-333333333333'
    MEGHKOSHA_WEB_CLIENT_ID = '44444444-4444-4444-4444-444444444444'
    MEGHKOSHA_OBO_MANAGED_IDENTITY_CLIENT_ID = $null
    MEGHKOSHA_LIVE_IDENTITY_CHECK = 'false'
    COST_EXPORT_NAME = $null
    COST_CONTROL_CLIENT_ID = $null
    COST_CONTROL_PRINCIPAL_ID = $null
    COST_EXPORT_STORAGE_URL = $null
    COST_EXPORT_STORAGE_RESOURCE_ID = $null
    PYTHONPATH = $null
    PYTEST_ADDOPTS = $null
}
$previous = @{}

try {
    foreach ($name in $settings.Keys) {
        $previous[$name] = @{
            Exists = Test-Path -LiteralPath "Env:$name"
            Value = [Environment]::GetEnvironmentVariable($name, 'Process')
        }
        if ($null -eq $settings[$name]) {
            Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
        } else {
            [Environment]::SetEnvironmentVariable($name, $settings[$name], 'Process')
        }
    }

    if ($Suite -in @('All', 'Backend')) {
        $python = $PythonExecutable
        if (-not $python) {
            $candidates = @(
                if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'cost-assessment-app/venv/Scripts/python.exe' }
                Join-Path $projectRoot 'api/.venv/Scripts/python.exe'
                Join-Path $projectRoot 'api/.venv/bin/python'
            )
            $python = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
        }
        if (-not $python -or -not (Test-Path -LiteralPath $python)) {
            throw 'Create the dedicated Python environment and install requirements-dev.txt, or supply -PythonExecutable.'
        }
        $python = (Resolve-Path -LiteralPath $python).Path
        Push-Location (Join-Path $projectRoot 'api')
        try {
            & $python -m pytest -q
            if ($LASTEXITCODE -ne 0) { throw "Backend tests failed (exit $LASTEXITCODE)." }
        } finally {
            Pop-Location
        }
    }

    $commands = [ordered]@{
        Frontend = 'test'
        Build = 'build'
        Browser = 'test:browser'
    }
    foreach ($name in $commands.Keys) {
        if ($Suite -notin @('All', $name)) { continue }
        if ($name -eq 'Frontend') {
            & npm --prefix (Join-Path $projectRoot 'web') run $commands[$name] -- --reporter=dot
        } else {
            & npm --prefix (Join-Path $projectRoot 'web') run $commands[$name]
        }
        if ($LASTEXITCODE -ne 0) { throw "$name check failed (exit $LASTEXITCODE)." }
    }
} finally {
    foreach ($name in $previous.Keys) {
        if ($previous[$name].Exists) {
            [Environment]::SetEnvironmentVariable($name, $previous[$name].Value, 'Process')
        } else {
            Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
        }
    }
}