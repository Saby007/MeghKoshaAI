param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9-]{0,63}$')]
    [string] $EnvironmentName,
    [string] $Location = 'centralindia',
    [ValidateRange(1, 5)]
    [int] $MaxAttempts = 3,
    [switch] $SkipPreview,
    [switch] $SkipProcessor,
    [switch] $PlanOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$modelRouter = '[{"name":"model-router","modelFormat":"OpenAI","modelName":"model-router","modelVersion":"2025-11-18","sku":"GlobalStandard","capacity":100}]'
$settings = [ordered]@{
    AZURE_LOCATION = $Location
    APP_PROFILE = 'ai'
    APP_EXPORT_TRUSTED_SERVICES = 'true'
    APP_MODEL_DEPLOYMENTS = $modelRouter
    MODEL_ROUTER_DEPLOYMENT_NAME = 'model-router'
    APP_ENABLE_CHAT_RUNTIME = 'true'
    APP_AI_VALIDATED = 'true'
    APP_ENABLE_AI_RUNTIME = 'false'
    APP_RESTORE_AI_ACCOUNT = 'false'
}

if ($PlanOnly) {
    [pscustomobject]@{
        environment = $EnvironmentName
        settings = $settings
        preview = -not $SkipPreview
        enableProcessor = -not $SkipProcessor
        maxAttempts = $MaxAttempts
    } | ConvertTo-Json -Depth 5
    exit 0
}

if (-not (Get-Command azd -ErrorAction SilentlyContinue)) { throw 'Azure Developer CLI (azd) is required.' }
if (-not (Get-Command az -ErrorAction SilentlyContinue)) { throw 'Azure CLI (az) is required.' }

$environments = @(& azd env list --output json | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0) { throw 'Unable to list azd environments.' }
if ($environments.Name -contains $EnvironmentName) {
    & azd env select $EnvironmentName | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Unable to select azd environment '$EnvironmentName'." }
} else {
    & azd env new $EnvironmentName
    if ($LASTEXITCODE -ne 0) { throw "Unable to create azd environment '$EnvironmentName'." }
}
foreach ($entry in $settings.GetEnumerator()) {
    & azd env set --environment $EnvironmentName $entry.Key $entry.Value
    if ($LASTEXITCODE -ne 0) { throw "Unable to set $($entry.Key) in azd environment '$EnvironmentName'." }
}

if (-not $SkipPreview) {
    & azd provision --environment $EnvironmentName --preview
    if ($LASTEXITCODE -ne 0) { throw 'Infrastructure preview failed. No deployment was started.' }
}

function Wait-FoundryAccount {
    $resourceGroup = & azd env get-value --environment $EnvironmentName AZURE_RESOURCE_GROUP 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($resourceGroup)) {
        $resourceGroup = "rg-$EnvironmentName"
    }
    for ($attempt = 1; $attempt -le 40; $attempt++) {
        $accountNames = @(& az cognitiveservices account list --resource-group $resourceGroup --query '[].name' --output tsv 2>$null)
        $states = @($accountNames | ForEach-Object {
            & az cognitiveservices account show --resource-group $resourceGroup --name $_ --query properties.provisioningState --output tsv 2>$null
        })
        if ($accountNames.Count -gt 0 -and $states.Count -eq $accountNames.Count) {
            if ($states -contains 'Failed') { throw 'The Foundry account entered a failed provisioning state.' }
            if (@($states | Where-Object { $_ -ne 'Succeeded' }).Count -eq 0) { return }
        }
        Start-Sleep -Seconds 15
    }
    throw 'Timed out waiting for the Foundry account to reach Succeeded.'
}

function Sync-ProcessorImage {
    if ($SkipProcessor) { return }
    $apiImage = & azd env get-value --environment $EnvironmentName SERVICE_API_IMAGE_NAME 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($apiImage)) { return }
    & azd env set --environment $EnvironmentName APP_ENABLE_PROCESSOR true | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Unable to enable the scheduled processor.' }
    & azd env set --environment $EnvironmentName SERVICE_PROCESSOR_IMAGE_NAME $apiImage | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Unable to reuse the API image for the scheduled processor.' }
}

$completed = $false
for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
    Sync-ProcessorImage
    Write-Host "azd up attempt $attempt of $MaxAttempts" -ForegroundColor Cyan
    $output = [System.Collections.Generic.List[string]]::new()
    & azd up --environment $EnvironmentName 2>&1 | ForEach-Object {
        $line = $_.ToString()
        $output.Add($line)
        Write-Host $line
    }
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        $completed = $true
        break
    }
    $text = $output -join "`n"
    $foundryRace = $text -match 'AccountProvisioningStateInvalid|Another operation is in progress'
    $missingApp = $text -match "resource not found: unable to find a resource with name 'ca-(api|web)-"
    if (-not ($foundryRace -or $missingApp) -or $attempt -eq $MaxAttempts) {
        throw "azd up failed on attempt $attempt. Review the output above before retrying."
    }
    if ($foundryRace) {
        Write-Warning 'Foundry account creation is still settling. Waiting for Succeeded before retrying.'
        Wait-FoundryAccount
    } else {
        Write-Warning 'Images were published after infrastructure planning. Retrying so Bicep can create the Container Apps.'
    }
    Sync-ProcessorImage
}
if (-not $completed) { throw 'Deployment did not complete.' }

$url = & azd env get-value --environment $EnvironmentName SERVICE_WEB_ENDPOINT_URL 2>$null
if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($url)) {
    $health = Invoke-WebRequest -Uri "$($url.TrimEnd('/'))/api/health" -UseBasicParsing
    if ($health.StatusCode -ne 200) { throw "Deployment completed, but API health returned HTTP $($health.StatusCode)." }
    Write-Host "Deployment ready: $url" -ForegroundColor Green
}
