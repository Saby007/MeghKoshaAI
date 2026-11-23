$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
# Compiles the exact parameter file AZD compiles, for foundation-only and full-release inputs.
$root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$digest = 'a' * 64
$registry = 'acrtest.azurecr.io'
$image = "$registry/meghkoshaai-api@sha256:$digest"
$common = @{
    AZURE_ENV_NAME = 'compile-check'; AZURE_LOCATION = 'centralindia'; FOUNDRY_LOCATION = 'eastus2'; APP_PROFILE = 'ai'
    APP_ASSESSMENT_MODE = 'postgres'; APP_PRIVATE_REGISTRY = 'true'; APP_REGISTRY_BUILD_MODE = 'private-runner'
    APP_ENABLE_PRIVATE_BUILDER = 'true'; APP_PRIVATE_BUILDER_SSH_PUBLIC_KEY = 'ssh-ed25519 AAAAC3NzaSynthetic'
    APP_PRIVATE_BUILDER_IMAGE_VERSION = '24.04.202610010'; APP_ENABLE_CHAT_RUNTIME = 'true'
    MODEL_ROUTER_DEPLOYMENT_NAME = 'model-router'; APP_ENABLE_PROCESSOR = 'false'; APP_EXPORT_TRUSTED_SERVICES = 'false'
    APP_MODEL_DEPLOYMENTS = '[{"name":"model-router","modelFormat":"OpenAI","modelName":"model-router","modelVersion":"2025-11-18","sku":"GlobalStandard","capacity":20}]'
}
$cases = @{
    foundation = @{ APP_ASSESSMENT_FOUNDATION_ONLY = 'true' }
    release = @{
        APP_ASSESSMENT_FOUNDATION_ONLY = 'false'; SERVICE_API_IMAGE_NAME = $image
        SERVICE_WEB_IMAGE_NAME = "$registry/meghkoshaai-web@sha256:$digest"; SERVICE_ASSESSMENT_IMAGE_NAME = $image
    }
}
$names = @($common.Keys) + @($cases.Values | ForEach-Object { $_.Keys }) | Select-Object -Unique
$saved = @{}
foreach ($name in $names) { $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
try {
    foreach ($case in $cases.GetEnumerator()) {
        foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $null, 'Process') }
        foreach ($entry in $common.GetEnumerator()) { [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process') }
        foreach ($entry in $case.Value.GetEnumerator()) { [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process') }
        $output = & az bicep build-params --file (Join-Path $root 'infra\main.bicepparam') --stdout 2>&1
        if ($LASTEXITCODE -ne 0) { throw "main.bicepparam failed for '$($case.Key)': $($output -join "`n")" }
    }
    [ordered]@{ result = 'passed'; cases = @($cases.Keys) | Sort-Object } | ConvertTo-Json -Compress
} finally {
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process') }
}
