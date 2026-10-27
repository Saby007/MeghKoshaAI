[CmdletBinding()]
param(
    [ValidateSet('Local', 'Provision', 'Publish', 'Deploy', 'Down')]
    [string] $Operation = 'Local'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion -lt [version]'7.4') { throw 'PowerShell 7.4 or later is required.' }

function Get-Setting([string] $Name, [string] $Default = '') {
    $value = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if ([string]::IsNullOrWhiteSpace($value)) { return $Default }
    return $value.Trim()
}

function Get-BooleanSetting([string] $Name, [bool] $Default = $false) {
    $value = Get-Setting $Name ($Default.ToString())
    $parsed = $false
    if (-not [bool]::TryParse($value, [ref]$parsed)) { throw "$Name must be true or false." }
    return $parsed
}

function Assert-Identifier([string] $Name) {
    $value = Get-Setting $Name
    $identifier = [guid]::Empty
    if (-not [guid]::TryParseExact($value, 'D', [ref]$identifier) -or $identifier -eq [guid]::Empty) {
        throw "$Name must contain a nonzero UUID."
    }
}

if ($Operation -ne 'Local' -and -not (Get-BooleanSetting 'APP_ALLOW_AZURE_CHANGES')) {
        throw 'Azure changes are not approved. Complete the required approval gates and explicitly set APP_ALLOW_AZURE_CHANGES=true.'
}
if ($Operation -eq 'Down' -and -not (Get-BooleanSetting 'APP_ALLOW_DESTRUCTIVE_CHANGES')) {
    throw 'Destructive cleanup requires separate approval and APP_ALLOW_DESTRUCTIVE_CHANGES=true.'
}
if ($Operation -eq 'Deploy') {
        throw 'Direct azd deploy is not enabled yet. Publish reviewed images, resolve their digests, then use the approved Bicep provision flow. A future guided delivery pipeline will orchestrate this.'
}

$environmentName = Get-Setting 'AZURE_ENV_NAME'
if ($environmentName -cnotmatch '^[a-z0-9][a-z0-9-]{0,63}$') { throw 'AZURE_ENV_NAME must be a lowercase alphanumeric/hyphen name of at most 64 characters.' }
$profile = Get-Setting 'APP_PROFILE' 'core'
if ($profile -cnotin @('core', 'data', 'ai')) { throw 'APP_PROFILE must be core, data or ai.' }
$profileOrder = @{ core = 0; data = 1; ai = 2 }
$previousProfile = Get-Setting 'APP_PROVISIONED_PROFILE'
if ($Operation -ne 'Down' -and $previousProfile -and
    (-not $profileOrder.ContainsKey($previousProfile) -or $profileOrder[$profile] -lt $profileOrder[$previousProfile])) {
    throw 'Profile downgrade is not supported by this incremental template. It does not remove already provisioned data or AI resources.'
}
foreach ($name in @('AZURE_LOCATION', 'FOUNDRY_LOCATION')) {
    $default = if ($name -eq 'AZURE_LOCATION') { 'centralindia' } else { 'eastus2' }
    if ((Get-Setting $name $default) -cnotmatch '^[a-z][a-z0-9]+$') { throw "$name must use an Azure location code." }
}

$vnet = [System.Net.IPNetwork]::Parse((Get-Setting 'APP_VNET_PREFIX' '10.42.0.0/23'))
$containers = [System.Net.IPNetwork]::Parse((Get-Setting 'APP_CONTAINER_SUBNET_PREFIX' '10.42.0.0/24'))
$endpoints = [System.Net.IPNetwork]::Parse((Get-Setting 'APP_PRIVATE_ENDPOINT_SUBNET_PREFIX' '10.42.1.0/27'))
foreach ($subnet in @($containers, $endpoints)) {
    if ($subnet.BaseAddress.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or
        -not $vnet.Contains($subnet.BaseAddress) -or $subnet.PrefixLength -lt $vnet.PrefixLength -or $subnet.PrefixLength -gt 27) {
        throw 'Each subnet must be IPv4, within the VNet, and /27 or larger.'
    }
}
if ($containers.Contains($endpoints.BaseAddress) -or $endpoints.Contains($containers.BaseAddress)) { throw 'Container and private-endpoint subnets must not overlap.' }
$reserved = [System.Net.IPNetwork]::Parse('172.17.0.0/16')
if ($vnet.Contains($reserved.BaseAddress) -or $reserved.Contains($vnet.BaseAddress)) { throw 'The VNet must not overlap Docker-reserved 172.17.0.0/16.' }

$apiImage = Get-Setting 'SERVICE_API_IMAGE_NAME'
$webImage = Get-Setting 'SERVICE_WEB_IMAGE_NAME'
if ([string]::IsNullOrEmpty($apiImage) -xor [string]::IsNullOrEmpty($webImage)) { throw 'Provide both application images, or leave both empty for the foundation stage.' }
$applications = -not [string]::IsNullOrEmpty($apiImage)
$assessmentMode = Get-Setting 'APP_ASSESSMENT_MODE' 'postgres'
if ($assessmentMode -cnotin @('blob', 'postgres')) { throw 'APP_ASSESSMENT_MODE must be blob or postgres.' }
$privateRegistry = Get-BooleanSetting 'APP_PRIVATE_REGISTRY' ($assessmentMode -eq 'postgres')
if ($assessmentMode -eq 'postgres' -and -not $privateRegistry) {
    throw 'PostgreSQL/scalable deployments require private ACR. Public registry access must not be enabled.'
}
$buildMode = Get-Setting 'APP_REGISTRY_BUILD_MODE' $(if ($assessmentMode -eq 'postgres') { 'private-runner' } else { 'managed-pool' })
if ($buildMode -cnotin @('private-runner', 'managed-pool')) { throw 'APP_REGISTRY_BUILD_MODE must be private-runner or managed-pool.' }
$privateBuilder = Get-BooleanSetting 'APP_ENABLE_PRIVATE_BUILDER'
if ($privateBuilder) {
    if (-not $privateRegistry -or $buildMode -ne 'private-runner') { throw 'Private builder requires private ACR and private-runner mode.' }
    if ((Get-Setting 'APP_PRIVATE_BUILDER_SSH_PUBLIC_KEY') -cnotmatch '^(ssh-ed25519|ssh-rsa) [A-Za-z0-9+/=]+(?: [^\r\n]+)?$') {
        throw 'Private builder requires an OpenSSH public key, not password or private-key credentials.'
    }
    if ((Get-Setting 'APP_PRIVATE_BUILDER_IMAGE_VERSION') -cnotmatch '^\d+\.\d+\.\d+$') {
        throw 'Resolve and pin the private builder Ubuntu image version before provisioning.'
    }
}
$assessmentFoundationOnly = Get-BooleanSetting 'APP_ASSESSMENT_FOUNDATION_ONLY'
if ($assessmentFoundationOnly -and $assessmentMode -ne 'postgres') {
    throw 'The assessment foundation/bootstrap stage requires PostgreSQL mode.'
}
if ($Operation -ne 'Down' -and (Get-Setting 'ASSESSMENT_MODE') -eq 'postgres' -and $assessmentMode -eq 'blob') {
    throw 'Switching a provisioned PostgreSQL environment to Blob mode requires a separately approved migration; incremental deployment does not stop its worker or remove its database.'
}
if ($applications -and $assessmentMode -eq 'postgres' -and -not $assessmentFoundationOnly -and
    -not (Get-BooleanSetting 'APP_ASSESSMENT_DB_BOOTSTRAPPED')) {
    throw 'Private database bootstrap is not verified. Set APP_ASSESSMENT_FOUNDATION_ONLY=true, provision the database/manual bootstrap job with the reviewed assessment image, run and verify bootstrap, then explicitly approve application release.'
}
$assessmentImage = Get-Setting 'SERVICE_ASSESSMENT_IMAGE_NAME'
if ($assessmentImage) {
    $registry = Get-Setting 'AZURE_CONTAINER_REGISTRY_ENDPOINT'
    if ($assessmentImage -cnotmatch '^[a-z0-9]+\.azurecr\.io/[a-z0-9][a-z0-9/_.-]*@sha256:[a-f0-9]{64}$' -or
        -not $registry -or -not $assessmentImage.StartsWith("$registry/", [StringComparison]::Ordinal)) {
        throw 'The assessment/bootstrap image must be an immutable digest in this environment registry.'
    }
}
$assessmentLimits = ConvertFrom-Json -InputObject (Get-Setting 'APP_ASSESSMENT_LIMITS' '{}') -AsHashtable
if ($assessmentLimits -isnot [System.Collections.IDictionary]) { throw 'APP_ASSESSMENT_LIMITS must be a JSON object.' }
$allowedAssessmentLimits = @{
    inputBytes = @(1048576L, 68719476736L); spoolBytes = @(1048576L, 68719476736L); expandedBytes = @(1048576L, 137438953472L)
    csvRows = @(1L, 100000000L); inventoryRows = @(1L, 5000000L); historyRows = @(1L, 10000000L)
    timeoutSeconds = @(60L, 86400L); leaseSeconds = @(30L, 600L); maxAttempts = @(1L, 5L)
    pageSize = @(1L, 200L); responseBytes = @(65536L, 2097152L); recordBytes = @(1024L, 524288L)
    inlineRows = @(1L, 100L); apiConcurrency = @(1L, 50L); interactiveRows = @(1L, 5000L)
    workConcurrency = @(1L, 20L); readPoolSize = @(1L, 49L)
    armIntervalMs = @(10L, 60000L); graphIntervalMs = @(10L, 60000L); metricsIntervalMs = @(10L, 60000L); costIntervalMs = @(10L, 60000L)
    azureGetRetries = @(0L, 5L); detailRows = @(1L, 250000L); detailPoints = @(1L, 20000000L)
}
foreach ($key in $assessmentLimits.Keys) {
    $value = $assessmentLimits[$key]
    if (-not $allowedAssessmentLimits.ContainsKey($key) -or $value -isnot [long] -and $value -isnot [int]) {
        throw "Unsupported or non-integer assessment safety budget: $key"
    }
    if ($value -lt $allowedAssessmentLimits[$key][0] -or $value -gt $allowedAssessmentLimits[$key][1]) {
        throw "Assessment safety budget outside its supported range: $key"
    }
}
$responseBytes = if ($assessmentLimits.Contains('responseBytes')) { $assessmentLimits.responseBytes } else { 1048576L }
$recordBytes = if ($assessmentLimits.Contains('recordBytes')) { $assessmentLimits.recordBytes } else { 262144L }
if ($recordBytes + 4096 -gt $responseBytes) { throw 'The detail-record budget must leave 4096 bytes within the response budget.' }
$poolSize = [int](Get-Setting 'APP_ASSESSMENT_POOL_SIZE' '5')
if ($poolSize -lt 2 -or $poolSize -gt 50) { throw 'APP_ASSESSMENT_POOL_SIZE must be between 2 and 50.' }
$readPoolSize = if ($assessmentLimits.Contains('readPoolSize')) { $assessmentLimits.readPoolSize } else { [Math]::Min(3, $poolSize - 1) }
if ($readPoolSize -ge $poolSize) { throw 'readPoolSize must leave at least one connection for work operations.' }
if ($Operation -ne 'Down' -and (Get-BooleanSetting 'APP_APPLICATIONS_DEPLOYED') -and -not $applications) {
    throw 'Do not clear deployed application image settings; incremental provisioning would leave the existing apps running.'
}
if ($applications) {
    foreach ($name in @('AZURE_TENANT_ID', 'MEGHKOSHA_API_CLIENT_ID', 'MEGHKOSHA_WEB_CLIENT_ID')) { Assert-Identifier $name }
    if ((Get-Setting 'MEGHKOSHA_API_CLIENT_ID') -eq (Get-Setting 'MEGHKOSHA_WEB_CLIENT_ID')) { throw 'API and SPA must use separate registrations.' }
}
if ($Operation -eq 'Publish' -and (Get-Setting 'AZURE_CONTAINER_REGISTRY_ENDPOINT') -cnotmatch '^[a-z0-9]+\.azurecr\.io$') {
    throw 'Publish requires the provisioned environment registry; no image digest is required before its first publication.'
}
if ($applications) {
    $registry = Get-Setting 'AZURE_CONTAINER_REGISTRY_ENDPOINT'
    foreach ($image in @($apiImage, $webImage)) {
        if ($image -notmatch '^[a-z0-9]+\.azurecr\.io/[a-z0-9][a-z0-9/_.-]*@sha256:[a-f0-9]{64}$' -or
            -not $registry -or -not $image.StartsWith("$registry/", [StringComparison]::Ordinal)) {
            throw 'Application images must be immutable digests in this environment registry.'
        }
    }
}

$processor = Get-BooleanSetting 'APP_ENABLE_PROCESSOR'
if ($Operation -ne 'Down' -and (Get-BooleanSetting 'APP_PROCESSOR_DEPLOYED') -and -not $processor) {
    throw 'Disabling this module does not stop an existing scheduled job. Use the separately approved processor lifecycle operation.'
}
if ($processor) {
    if ($profile -eq 'core') { throw 'The processor requires the data or ai stage.' }
    $processorImage = Get-Setting 'SERVICE_PROCESSOR_IMAGE_NAME'
    $registry = Get-Setting 'AZURE_CONTAINER_REGISTRY_ENDPOINT'
    if ($processorImage -cnotmatch '^[a-z0-9]+\.azurecr\.io/[a-z0-9][a-z0-9/_.-]*@sha256:[a-f0-9]{64}$' -or
        -not $registry -or -not $processorImage.StartsWith("$registry/", [StringComparison]::Ordinal)) {
        throw 'The processor requires its own immutable image digest in the environment registry.'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot '../api/jobs/scheduler.py'))) {
        throw 'The scheduled processor is not implemented yet. Complete the processor implementation before enabling it.'
    }
}
$trustedExports = Get-BooleanSetting 'APP_EXPORT_TRUSTED_SERVICES'
if ($profile -eq 'core' -and $trustedExports) { throw 'Native export storage settings are not part of the core stage.' }

$models = ConvertFrom-Json -InputObject (Get-Setting 'APP_MODEL_DEPLOYMENTS' '[]') -AsHashtable -NoEnumerate
if ($models -isnot [System.Collections.IList]) { throw 'APP_MODEL_DEPLOYMENTS must be a JSON array.' }
if ($models.Count -and $profile -ne 'ai') { throw 'Model deployments require the ai stage.' }
$modelNames = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
$modelRouterNames = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
foreach ($model in $models) {
    if ($model -isnot [System.Collections.IDictionary]) { throw 'Each model must be an object.' }
    foreach ($field in @('name', 'modelFormat', 'modelName', 'modelVersion', 'sku', 'capacity')) {
        if (-not $model.Contains($field)) { throw "Model deployment is missing $field." }
    }
    if ($model.Count -ne 6) { throw 'A model deployment contains unsupported fields.' }
    if (-not $modelNames.Add([string]$model.name) -or $model.name -cnotmatch '^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$') { throw 'Model deployment names must be valid and unique.' }
    if ($model.sku -cnotin @('Standard', 'GlobalStandard', 'DataZoneStandard') -or
        $model.capacity -isnot [long] -and $model.capacity -isnot [int] -or $model.capacity -lt 1) { throw 'Model SKU or capacity is invalid.' }
    foreach ($field in @('modelFormat', 'modelName', 'modelVersion')) {
        if ($model[$field] -isnot [string] -or [string]::IsNullOrWhiteSpace($model[$field])) { throw "Model $field must be specified." }
    }
    if ($model.modelName -eq 'model-router') {
        if ($model.modelFormat -ne 'OpenAI' -or $model.modelVersion -ne '2025-11-18' -or
            $model.sku -ne 'GlobalStandard' -or $model.capacity -ne 20) {
            throw 'Model Router must use the approved OpenAI 2025-11-18 GlobalStandard deployment at capacity 20.'
        }
        [void] $modelRouterNames.Add([string]$model.name)
    }
}
$aiRuntime = Get-BooleanSetting 'APP_ENABLE_AI_RUNTIME'
if ($aiRuntime -and ($profile -ne 'ai' -or -not $models.Count -or -not (Get-BooleanSetting 'APP_AI_VALIDATED'))) {
        throw 'AI runtime requires the ai stage, configured models and explicit validation (APP_AI_VALIDATED=true).'
}
$chatRuntime = Get-BooleanSetting 'APP_ENABLE_CHAT_RUNTIME'
$restoreAiAccount = Get-BooleanSetting 'APP_RESTORE_AI_ACCOUNT'
$reuseAiAccount = Get-BooleanSetting 'APP_REUSE_AI_ACCOUNT'
if ($restoreAiAccount -and $reuseAiAccount) {
    throw 'Foundry account restore and reuse cannot both be enabled.'
}
if ($reuseAiAccount -and $profile -ne 'ai') {
    throw 'Foundry account reuse requires the ai stage.'
}
$modelRouterDeploymentName = Get-Setting 'MODEL_ROUTER_DEPLOYMENT_NAME'
if ($chatRuntime -and ($profile -ne 'ai' -or -not (Get-BooleanSetting 'APP_AI_VALIDATED') -or
    [string]::IsNullOrWhiteSpace($modelRouterDeploymentName) -or -not $modelRouterNames.Contains($modelRouterDeploymentName))) {
        throw 'Foundry chat requires the ai stage, explicit validation and MODEL_ROUTER_DEPLOYMENT_NAME matching an approved model-router deployment.'
}

[pscustomobject]@{
    operation = $Operation
    environment = $environmentName
    profile = $profile
    applicationImagesSupplied = $applications
    processorEnabled = $processor
    aiRuntimeEnabled = $aiRuntime
    chatRuntimeEnabled = $chatRuntime
    aiAccountRestored = $restoreAiAccount
    aiAccountReused = $reuseAiAccount
    nativeExportNetworkException = $trustedExports
    cloudPreflightStillRequired = $true
} | ConvertTo-Json -Compress