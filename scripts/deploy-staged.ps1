[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)]
    [ValidateSet('All', 'Initialize', 'Foundation', 'Build', 'Bootstrap', 'Identity', 'Permissions', 'Release', 'Verify', 'StopBuilder')]
    [string] $Stage,
    [ValidatePattern('^[a-z0-9][a-z0-9-]{0,63}$')]
    [string] $EnvironmentName = 'meghkoshaai-scalable-dev',
    [string] $SubscriptionId = '',
    [string] $Location = 'centralindia',
    [string] $FoundryLocation = 'eastus2',
    [string] $BuilderVmSize = 'Standard_D2s_v5',
    [string] $BuilderSshPublicKeyPath = '',
    [ValidateRange(1, 10)]
    [int] $MaxAttempts = 3,
    [ValidateRange(0, 900)]
    [int] $SettleSeconds = 120,
    [ValidatePattern('^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git$')]
    [string] $Repository = 'https://github.com/Saby007/MeghKoshaAI.git',
    [ValidatePattern('^([a-f0-9]{40})?$')]
    [string] $SourceCommit = '',
    [guid[]] $TargetSubscriptionId = @(),
    [string] $ServiceManagementReference = '',
    [switch] $AllowNativeExportTrustedServices,
    [switch] $BlobDataProtection,
    [switch] $IncludeLocalhostRedirects,
    [switch] $ApproveOwnedRegistrationMigration,
    [uri] $PreviousWebOrigin,
    [guid] $PreviousOboPrincipalId = [guid]::Empty,
    [guid] $ExpectedApiClientId = [guid]::Empty,
    [guid] $ExpectedWebClientId = [guid]::Empty,
    [switch] $SkipLogin,
    [switch] $ApproveAll,
    [switch] $Apply,
    [switch] $PreviewOnly,
    [switch] $ApproveAzureChanges,
    [switch] $ApproveCosts,
    [switch] $ApproveImagePublication,
    [switch] $ApproveDatabaseBootstrap,
    [switch] $ApproveIdentityChanges,
    [switch] $ApproveRoleAssignments,
    [switch] $ApproveRelease
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion -lt [version]'7.4') { throw 'PowerShell 7.4 or later is required.' }
if ($EnvironmentName -eq 'meghkoshaai-dev') { throw 'The existing Dev environment is under a layout hold. Use a reviewed parallel environment.' }
if ($ApproveOwnedRegistrationMigration) {
    if (-not $PreviousWebOrigin -or -not $PreviousWebOrigin.IsAbsoluteUri -or $PreviousWebOrigin.Scheme -ne 'https' -or
        $PreviousWebOrigin.Port -ne 443 -or $PreviousWebOrigin.AbsolutePath -ne '/' -or $PreviousWebOrigin.Query -or
        $PreviousWebOrigin.Fragment -or $PreviousWebOrigin.UserInfo -or $PreviousOboPrincipalId -eq [guid]::Empty -or
        $ExpectedApiClientId -eq [guid]::Empty -or $ExpectedWebClientId -eq [guid]::Empty -or
        $ExpectedApiClientId -eq $ExpectedWebClientId) {
        throw 'Owned registration migration requires the exact previous HTTPS origin, previous OBO principal and distinct expected API/web client IDs.'
    }
} elseif ($PreviousWebOrigin -or $PreviousOboPrincipalId -ne [guid]::Empty -or
          $ExpectedApiClientId -ne [guid]::Empty -or $ExpectedWebClientId -ne [guid]::Empty) {
    throw 'Previous identity values require -ApproveOwnedRegistrationMigration.'
}
if (-not $Apply -or $WhatIfPreference) {
    [ordered]@{
        stage = $Stage; environment = $EnvironmentName; subscription = $SubscriptionId
        location = $Location; privateRegistry = $true; privateDatabase = $true
        ai = @{ enabled = $true; foundryLocation = $FoundryLocation; model = 'model-router'; hostedNarratorEnabled = $false }
        privateBuilder = @{ vmSize = $BuilderVmSize; publicVmIp = $false; inboundAccess = $false; outboundNat = $true }
        provisionRetry = @{ maxAttempts = $MaxAttempts; settleSeconds = $SettleSeconds; settlementTimeoutSeconds = 600 }
        source = @{ repository = $Repository; commit = $SourceCommit }
        stages = $(if ($Stage -eq 'All') { @('Initialize', 'Foundation', 'Build', 'Bootstrap', 'Identity', 'Permissions', 'Release', 'Verify', 'StopBuilder') } else { @($Stage) })
        targets = $TargetSubscriptionId
        appliesChanges = $false
        nativeExportTrustedServicesException = [bool]$AllowNativeExportTrustedServices
        ownedRegistrationMigrationApproved = [bool]$ApproveOwnedRegistrationMigration
        note = 'Plan only. No Azure calls. VM, disk, NAT, registry and database add cost; deallocation does not remove retained charges.'
    } | ConvertTo-Json -Depth 5
    return
}
if (-not $ApproveAzureChanges) { throw 'Executing a stage requires -Apply -ApproveAzureChanges. Nothing was changed.' }
if ($PreviewOnly -and $Stage -notin @('Foundation', 'Bootstrap', 'Release')) { throw '-PreviewOnly is supported only for provisioning stages.' }
$approval = @{
    Foundation = $ApproveCosts; Build = $ApproveImagePublication; Bootstrap = $ApproveDatabaseBootstrap
    Identity = $ApproveIdentityChanges; Permissions = $ApproveRoleAssignments; Release = $ApproveRelease
}
if (-not $PreviewOnly -and $approval.ContainsKey($Stage) -and -not $approval[$Stage]) { throw "Stage $Stage requires its separate explicit approval flag." }
if (-not $PSCmdlet.ShouldProcess("$EnvironmentName / $Stage", 'Execute the requested operator-controlled stage')) { return }

$root = Split-Path $PSScriptRoot -Parent
$values = @{}
$subscription = ''
function Invoke-Json([string] $Program, [string[]] $Arguments) {
    $result = & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program command failed: $($Arguments[0..([Math]::Min(2,$Arguments.Count-1))] -join ' ')" }
    return ($result -join "`n" | ConvertFrom-Json -AsHashtable)
}
function Azure([string[]] $Arguments) {
    return Invoke-Json 'az' ($Arguments + @('--subscription', $script:subscription, '--only-show-errors', '--output', 'json'))
}
function Value([string] $Name, [switch] $Required) {
    $value = if ($script:values.ContainsKey($Name)) { [string]$script:values[$Name] } else { '' }
    if ($Required -and [string]::IsNullOrWhiteSpace($value)) { throw "Missing $Name. Complete the preceding stage." }
    return $value
}
function Save([string] $Name, [string] $Value) {
    & azd env set $Name $Value --environment $EnvironmentName | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Could not persist $Name." }
    $script:values[$Name] = $Value
}
function Reload {
    $script:values = Invoke-Json 'azd' @('env', 'get-values', '--environment', $EnvironmentName, '--output', 'json')
}
function Check-Compute {
    $region = Value 'AZURE_LOCATION' -Required
    $size = Value 'APP_PRIVATE_BUILDER_VM_SIZE' -Required
    $skus = @(Azure @('vm', 'list-skus', '--location', $region, '--resource-type', 'virtualMachines', '--size', $size, '--all'))
    $matches = @($skus | Where-Object { $_.name -eq $size -and @($_.restrictions).Count -eq 0 })
    if ($matches.Count -ne 1) { throw "VM size $size is unavailable or restricted in $region for this subscription." }
    $cpus = [int](@($matches[0].capabilities | Where-Object { $_.name -eq 'vCPUs' })[0].value)
    $needed = $cpus
    if (Value 'APP_PRIVATE_BUILDER_VM_NAME') {
        $existing = Azure @('vm', 'show', '--resource-group', (Value 'AZURE_RESOURCE_GROUP' -Required),
                           '--name', (Value 'APP_PRIVATE_BUILDER_VM_NAME'), '--show-details')
        if ($existing.hardwareProfile.vmSize -ne $size) { throw 'Changing an existing builder VM size requires a separate reviewed capacity change.' }
        if ($existing.powerState -in @('VM running', 'VM stopped')) { $needed = 0 }
    }
    $usage = @(Azure @('vm', 'list-usage', '--location', $region))
    foreach ($quota in @('cores', [string]$matches[0].family)) {
        $entry = @($usage | Where-Object { $_.name.value -eq $quota })
        if ($entry.Count -ne 1) {
            throw "VM quota $quota in $region could not be read uniquely. No provisioning started."
        }
        $used = 0L
        $limit = 0L
        if (-not [long]::TryParse([string]$entry[0].currentValue, [Globalization.NumberStyles]::Integer,
                [Globalization.CultureInfo]::InvariantCulture, [ref]$used) -or
            -not [long]::TryParse([string]$entry[0].limit, [Globalization.NumberStyles]::Integer,
                [Globalization.CultureInfo]::InvariantCulture, [ref]$limit) -or $used -lt 0 -or $limit -lt 0) {
            throw "VM quota $quota in $region returned invalid nonnegative usage/limit values. No provisioning started."
        }
        $available = $limit - $used
        if ([long]$needed -gt $available) {
            throw "VM quota $quota in $region is insufficient: used=$used, limit=$limit, available=$available, additional=$needed vCPU(s). No provisioning started."
        }
    }
    $version = Value 'APP_PRIVATE_BUILDER_IMAGE_VERSION'
    if (-not $version) { $version = 'latest' }
    $image = Azure @('vm', 'image', 'show', '--location', $region, '--urn', "Canonical:ubuntu-24_04-lts:server:$version")
    if (-not $image.name -or $image.name -notmatch '^\d+\.\d+\.\d+$') { throw 'Ubuntu image version could not be resolved.' }
    Save 'APP_PRIVATE_BUILDER_IMAGE_VERSION' $image.name
}
function Chat-Configuration {
    return "$(Value 'FOUNDRY_LOCATION' -Required)|$(Value 'APP_MODEL_DEPLOYMENTS' -Required)|$(Value 'MODEL_ROUTER_DEPLOYMENT_NAME' -Required)"
}
function Check-Foundry {
    $region = Value 'FOUNDRY_LOCATION' -Required
    $models = @(ConvertFrom-Json -InputObject (Value 'APP_MODEL_DEPLOYMENTS' -Required))
    if (-not $models.Count) { throw 'The AI deployment requires its approved Model Router configuration.' }
    $catalog = @(Azure @('cognitiveservices', 'model', 'list', '--location', $region))
    foreach ($model in $models) {
        $offered = @($catalog | Where-Object {
            $_.model.name -eq $model.modelName -and $_.model.format -eq $model.modelFormat -and
            $_.model.version -eq $model.modelVersion -and @($_.model.skus | Where-Object { $_.name -eq $model.sku }).Count
        })
        if (-not $offered.Count) {
            throw "$($model.modelName) $($model.modelVersion)/$($model.sku) is not offered in $region. No provisioning started; choose an approved supported Foundry region."
        }
    }
}
function Invoke-ProvisionCommand([switch] $Preview) {
    $nativePreference = $PSNativeCommandUseErrorActionPreference
    try {
        $PSNativeCommandUseErrorActionPreference = $false
        $arguments = @('provision', '--environment', $EnvironmentName, '--no-prompt')
        if ($Preview) { $arguments += '--preview' }
        $lines = [Collections.Generic.List[string]]::new()
        & azd @arguments 2>&1 | ForEach-Object {
            $line = $_.ToString()
            $lines.Add($line)
            Write-Host $line
        }
        return @{ exitCode = $LASTEXITCODE; output = $lines -join "`n" }
    } finally {
        $PSNativeCommandUseErrorActionPreference = $nativePreference
    }
}
function Wait-ProvisionRetry {
    if ($SettleSeconds) {
        Write-Host "Waiting $SettleSeconds second(s) before checking the previous deployment."
        Start-Sleep -Seconds $SettleSeconds
    }
    $group = Value 'AZURE_RESOURCE_GROUP'
    if (-not $group) { $group = "rg-$EnvironmentName" }
    $scope = "/subscriptions/$script:subscription/resourceGroups/$group"
    $owned = Azure @('group', 'show', '--name', $group)
    if ($owned.id -ne $scope -or $owned.name -ne $group -or $owned.location -ne (Value 'AZURE_LOCATION' -Required) -or
        $owned.tags -isnot [Collections.IDictionary] -or $owned.tags['azd-env-name'] -ne $EnvironmentName -or
        $owned.tags['app-name'] -ne 'cost-assessment') {
        throw 'Retry requires the exact environment-owned resource group and location.'
    }
    $targets = @{}
    for ($poll = 0; $poll -le 40; $poll++) {
        $pending = [Collections.Generic.List[string]]::new()
        $deployments = @(Azure @('deployment', 'sub', 'list') | Where-Object {
            $_.name -match ('^' + [regex]::Escape($EnvironmentName) + '-\d+$')
        }) + @(Azure @('deployment', 'group', 'list', '--resource-group', $group))
        foreach ($deployment in $deployments) {
            if (-not $deployment.name -or -not $deployment.properties.provisioningState) {
                throw 'Deployment readiness metadata is incomplete; settlement cannot be confirmed.'
            }
            if ($deployment.properties.provisioningState -notin @('Succeeded', 'Failed', 'Canceled')) {
                $pending.Add("deployment $($deployment.name): $($deployment.properties.provisioningState)")
            }
        }
        if (-not $pending.Count) {
            if (-not $targets.Count) {
                $resources = @(Azure @('resource', 'list', '--resource-group', $group))
                $specifications = @{
                    postgres = @{ type = 'Microsoft.DBforPostgreSQL/flexibleServers'; pattern = '^pg-[a-z0-9]{13}$'; location = (Value 'AZURE_LOCATION' -Required) }
                }
                if ((Value 'APP_PROFILE') -eq 'ai') {
                    $specifications.foundry = @{ type = 'Microsoft.CognitiveServices/accounts'; pattern = '^ai-[a-z0-9]{13}$'; location = (Value 'FOUNDRY_LOCATION' -Required) }
                }
                foreach ($entry in $specifications.GetEnumerator()) {
                    $matches = @($resources | Where-Object { $_.type -eq $entry.Value.type })
                    if ($matches.Count -ne 1) { throw "Retry requires one unambiguous owned $($entry.Key) resource." }
                    $resource = $matches[0]
                    if ($resource.name -notmatch $entry.Value.pattern -or
                        $resource.id -ne "$scope/providers/$($entry.Value.type)/$($resource.name)" -or
                        $resource.location -ne $entry.Value.location -or $resource.tags -isnot [Collections.IDictionary] -or
                        $resource.tags['azd-env-name'] -ne $EnvironmentName -or $resource.tags['app-name'] -ne 'cost-assessment') {
                        throw "The $($entry.Key) resource does not match the owned environment, name, scope or region."
                    }
                    $targets[$entry.Key] = $resource
                }
            }
            $database = Azure @('postgres', 'flexible-server', 'show', '--resource-group', $group, '--name', $targets.postgres.name)
            if ($database.id -ne $targets.postgres.id -or $database.tags -isnot [Collections.IDictionary] -or
                $database.tags['azd-env-name'] -ne $EnvironmentName -or $database.tags['app-name'] -ne 'cost-assessment' -or
                $database.state -notin @('Ready', 'Creating', 'Updating', 'Starting')) {
                throw "PostgreSQL readiness is invalid or requires operator action: $($database.state)."
            }
            if ($database.state -ne 'Ready') { $pending.Add("PostgreSQL: $($database.state)") }
            elseif ($database.network.publicNetworkAccess -ne 'Disabled' -or
                $database.authConfig.passwordAuth -ne 'Disabled' -or $database.authConfig.activeDirectoryAuth -ne 'Enabled') {
                throw 'Retry requires private, Entra-only PostgreSQL; its security configuration was not confirmed.'
            }
            if ($targets.ContainsKey('foundry')) {
                $account = Azure @('cognitiveservices', 'account', 'show', '--resource-group', $group, '--name', $targets.foundry.name)
                if ($account.id -ne $targets.foundry.id -or $account.kind -ne 'AIServices' -or
                    $account.tags -isnot [Collections.IDictionary] -or $account.tags['azd-env-name'] -ne $EnvironmentName -or
                    $account.tags['app-name'] -ne 'cost-assessment' -or
                    $account.location -ne (Value 'FOUNDRY_LOCATION' -Required) -or
                    $account.properties.provisioningState -notin @('Succeeded', 'Accepted', 'Creating', 'Updating', 'Running')) {
                    throw "Foundry readiness is invalid or requires operator action: $($account.properties.provisioningState)."
                }
                if ($account.properties.provisioningState -ne 'Succeeded') {
                    $pending.Add("Foundry: $($account.properties.provisioningState)")
                } elseif ($account.properties.publicNetworkAccess -ne 'Disabled' -or
                    $account.properties.disableLocalAuth -isnot [bool] -or $account.properties.disableLocalAuth -ne $true) {
                    throw 'Retry requires private, Entra-only Foundry; its security configuration was not confirmed.'
                }
            }
            if (-not $pending.Count) {
                if ($targets.ContainsKey('foundry')) {
                    if ((Value 'APP_RESTORE_AI_ACCOUNT') -eq 'true') { Save 'APP_RESTORE_AI_ACCOUNT' 'false' }
                    if ((Value 'APP_REUSE_AI_ACCOUNT') -ne 'true') { Save 'APP_REUSE_AI_ACCOUNT' 'true' }
                }
                return
            }
        }
        if ($poll -eq 40) { throw 'Owned deployments/resources did not settle within 600 seconds. No retry was started.' }
        if ($poll % 4 -eq 0) { Write-Host "Waiting for settlement: $($pending -join '; ')" }
        Start-Sleep -Seconds 15
    }
}
function Provision {
    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        foreach ($entry in $script:values.GetEnumerator()) {
            [Environment]::SetEnvironmentVariable($entry.Key, [string]$entry.Value, 'Process')
        }
        $env:APP_ALLOW_AZURE_CHANGES = 'true'
        & (Join-Path $PSScriptRoot 'validate-deployment-inputs.ps1') -Operation Provision | Out-Host
        $preview = Invoke-ProvisionCommand -Preview
        if ($preview.exitCode -ne 0) { throw "Provision preview failed. No provisioning was started.`n$($preview.output)" }
        if ($PreviewOnly) { return }
        Write-Host "Provisioning attempt $attempt of $MaxAttempts."
        $result = Invoke-ProvisionCommand
        if ($result.exitCode -eq 0) { Reload; return }
        $permanent = 'RequestDisallowedByPolicy|QuotaExceeded|QuotaLimitReached|AuthorizationFailed|AuthenticationFailed|InvalidAuthenticationToken|Forbidden|SkuNotAvailable|LocationNotAvailable|InvalidTemplate|MissingSubscriptionRegistration|NoRegisteredProviderFound'
        $transient = 'AadAuthOperationCannotBePerformedWhenServerIsNotAccessible|AccountProvisioningStateInvalid|DeploymentActive|Another operation is in progress on the resource [^\r\n]*Microsoft\.CognitiveServices/accounts/'
        if ($result.output -match $permanent -or $result.output -notmatch $transient) {
            throw "Provisioning failed with a permanent or unsupported error; it was not automatically retried.`n$($result.output)"
        }
        if ($attempt -eq $MaxAttempts) {
            throw "Provisioning failed after $attempt attempt(s). Review the error before resuming.`n$($result.output)"
        }
        Write-Warning "Provisioning attempt $attempt of $MaxAttempts reported a supported transient error. Waiting for verified settlement before retrying."
        try { Wait-ProvisionRetry } catch {
            throw "Provisioning settlement could not be verified. No retry was started. $($_.Exception.Message)`nOriginal provisioning output:`n$($result.output)"
        }
    }
}
function Check-Private {
    $registry = Azure @('acr', 'show', '--name', (Value 'AZURE_CONTAINER_REGISTRY_NAME' -Required))
    if ($registry.sku.name -ne 'Premium' -or $registry.publicNetworkAccess -ne 'Disabled' -or $registry.adminUserEnabled) {
        throw 'Registry must remain Premium, public-disabled and admin-disabled.'
    }
    $hostName = Value 'ASSESSMENT_DB_HOST' -Required
    $serverName = $hostName.Split('.')[0]
    $database = Azure @('postgres', 'flexible-server', 'show', '--resource-group', (Value 'AZURE_RESOURCE_GROUP' -Required), '--name', $serverName)
    if ($database.network.publicNetworkAccess -ne 'Disabled' -or
        $database.authConfig.passwordAuth -ne 'Disabled' -or $database.authConfig.activeDirectoryAuth -ne 'Enabled') {
        throw 'PostgreSQL must remain private and Entra-only.'
    }
    if ((Value 'APP_ENABLE_CHAT_RUNTIME') -eq 'true') {
        $ai = Azure @('cognitiveservices', 'account', 'show', '--resource-group', (Value 'AZURE_RESOURCE_GROUP' -Required),
                     '--name', (Value 'APP_FOUNDRY_ACCOUNT_NAME' -Required))
        if ($ai.location -ne (Value 'FOUNDRY_LOCATION') -or $ai.properties.publicNetworkAccess -ne 'Disabled' -or
            -not $ai.properties.disableLocalAuth) { throw 'Foundry must use the selected region, private access and managed-identity authentication.' }
    }
}
function Check-Build {
    $commit = Value 'APP_STAGED_BUILD_COMMIT' -Required
    if ($commit -notmatch '^[a-f0-9]{40}$') { throw 'Build attestation is invalid.' }
    $registry = Value 'AZURE_CONTAINER_REGISTRY_ENDPOINT' -Required
    foreach ($service in @('API', 'WEB')) {
        $image = Value "APP_STAGED_${service}_IMAGE" -Required
        if ($image -notmatch "^$([regex]::Escape($registry))/meghkoshaai-$($service.ToLowerInvariant())@sha256:[a-f0-9]{64}$") {
            throw 'Build attestation must contain the reviewed registry and immutable image digests.'
        }
    }
}

function GitHub([string] $Path) {
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        throw 'GitHub CLI (gh) is required for authenticated source validation. Install it and run: gh auth login --hostname github.com --web. GitHub credentials stay on this management machine.'
    }
    $nativeErrors = $PSNativeCommandUseErrorActionPreference
    try {
        $PSNativeCommandUseErrorActionPreference = $false
        $output = & gh api --hostname github.com --method GET $Path
        $exitCode = $LASTEXITCODE
    } finally {
        $PSNativeCommandUseErrorActionPreference = $nativeErrors
    }
    if ($exitCode -ne 0) {
        throw "GitHub source validation failed for $Path (gh exit $exitCode). Resolve the GitHub error above; if authentication is missing or expired, run: gh auth login --hostname github.com --web. No anonymous fallback or token copying is used."
    }
    $json = $output -join "`n"
    if ([string]::IsNullOrWhiteSpace($json)) { throw "GitHub returned an empty response for $Path." }
    $result = $json | ConvertFrom-Json -AsHashtable
    if ($result -isnot [Collections.IDictionary]) { throw "GitHub did not return a JSON object for $Path." }
    return $result
}

function Check-Source {
    $head = (& git rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or (& git status --porcelain)) { throw 'Build only a reviewed clean checkout.' }
    if ($head -notmatch '^[a-f0-9]{40}$' -or ($SourceCommit -and $SourceCommit -ne $head)) {
        throw 'SourceCommit must equal the reviewed local HEAD.'
    }
    $repoPath = $Repository -replace '^https://github.com/', '' -replace '\.git$', ''
    $metadata = GitHub "repos/$repoPath"
    if (-not $metadata.Contains('private') -or $metadata['private'] -isnot [bool] -or
        -not $metadata.Contains('visibility') -or $metadata['visibility'] -isnot [string]) {
        throw 'GitHub repository metadata is incomplete; public source was not verified.'
    }
    if ($metadata.private -or $metadata.visibility -ne 'public') { throw 'This builder accepts public source only; no GitHub credentials are copied.' }
    $published = GitHub "repos/$repoPath/commits/$head"
    if ($published.sha -ne $head) { throw 'The reviewed commit is not published in the selected public repository.' }
    return $head
}

function Invoke-AllStages {
    foreach ($tool in @('git', 'azd', 'az')) {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "$tool is required." }
    }
    $commit = Check-Source
    if (-not $SkipLogin) {
        $nativeErrors = $PSNativeCommandUseErrorActionPreference
        try {
            $PSNativeCommandUseErrorActionPreference = $false
            & azd auth login --check-status 2>$null | Out-Host
            $needsAzdLogin = $LASTEXITCODE -ne 0
            & az account show --output none --only-show-errors 2>$null
            $needsAzLogin = $LASTEXITCODE -ne 0
        } finally {
            $PSNativeCommandUseErrorActionPreference = $nativeErrors
        }
        if ($needsAzdLogin) {
            & azd auth login | Out-Host
            if ($LASTEXITCODE -ne 0) { throw 'AZD sign-in failed; no resources were provisioned.' }
        }
        if ($needsAzLogin) {
            & az login --output none --only-show-errors | Out-Host
            if ($LASTEXITCODE -ne 0) { throw 'Azure CLI sign-in failed; no resources were provisioned.' }
        }
    }
    $environments = @(Invoke-Json 'azd' @('env', 'list', '--output', 'json'))
    $existing = @($environments | Where-Object { $_.Name -eq $EnvironmentName }).Count -gt 0
    if ($existing) { Reload }
    $selectedSubscription = $SubscriptionId
    if (-not $selectedSubscription -and $existing) { $selectedSubscription = Value 'AZURE_SUBSCRIPTION_ID' -Required }
    if (-not $selectedSubscription) { $selectedSubscription = (Read-Host 'Hosting subscription UUID').Trim() }
    $parsed = [guid]::Empty
    if (-not [guid]::TryParse($selectedSubscription, [ref]$parsed) -or $parsed -eq [guid]::Empty) {
        throw 'A nonzero hosting subscription UUID is required.'
    }
    $script:subscription = $parsed.ToString()
    if ($existing -and (Value 'AZURE_SUBSCRIPTION_ID' -Required) -ne $script:subscription) {
        throw 'Environment belongs to another subscription.'
    }
    if ($existing -and (Value 'AZURE_LOCATION' -Required) -ne $Location) { throw 'Existing environment region differs from -Location.' }
    if ($existing -and (Value 'FOUNDRY_LOCATION') -and (Value 'FOUNDRY_LOCATION') -ne $FoundryLocation) {
        throw 'Existing Foundry region differs from -FoundryLocation. Moving an AI account requires a reviewed migration.'
    }
    $account = Azure @('account', 'show')
    if ($account.state -ne 'Enabled') { throw 'The selected subscription is not enabled.' }
    $targets = $TargetSubscriptionId
    if (-not $targets.Count -and (Value 'APP_STAGED_TARGET_SUBSCRIPTIONS')) {
        $targets = [guid[]]@((Value 'APP_STAGED_TARGET_SUBSCRIPTIONS') | ConvertFrom-Json)
    }
    if (-not $targets.Count) { $targets = [guid[]]@($script:subscription) }
    $targets = [guid[]]@($targets | Select-Object -Unique)
    if ($targets -contains [guid]::Empty) { throw 'Target subscription IDs must be nonzero.' }
    $deployed = (Value 'APP_APPLICATIONS_DEPLOYED') -eq 'true'
    if ($deployed -and (Value 'APP_STAGED_BUILD_COMMIT') -ne $commit) {
        throw 'This environment already runs a different source commit. Review its image/schema update before using the individual stages; no automatic migration was started.'
    }
    Write-Host "`nEnd-to-end private deployment: $EnvironmentName"
    Write-Host "Hosting subscription: $script:subscription; tenant: $($account.tenantId); region: $Location"
    Write-Host "Private Foundry/Model Router region: $FoundryLocation. Chat is included; the hosted-agent narrator remains separate and opt-in."
    Write-Host "Assessed subscriptions: $($targets -join ', ')"
    Write-Host "Source commit: $commit"
    if ($ApproveOwnedRegistrationMigration) {
        Write-Host "Guarded owned sign-in migration: API $ExpectedApiClientId; web $ExpectedWebClientId."
        Write-Host "Only previous callback origin $PreviousWebOrigin and OBO principal $PreviousOboPrincipalId may be replaced."
    }
    Write-Host 'Includes private ACR Premium, PostgreSQL, private Foundry/model usage, VM/disk/NAT costs, image publication, sign-in registration, and Reader/Cost Management Contributor grants.'
    Write-Host "Blob trusted-service export exception: $($AllowNativeExportTrustedServices.IsPresent). No public ACR/PostgreSQL access or inbound VM access."
    Write-Host 'The builder is deallocated on completion/failure; disk, NAT, registry and database charges remain.'
    if (-not $ApproveAll -and (Read-Host 'Approve this complete rollout? Type YES').Trim() -cne 'YES') {
        throw 'End-to-end rollout was not approved. No resource changes were started.'
    }
    $keyPath = $BuilderSshPublicKeyPath
    if (-not $keyPath -and -not (Value 'APP_PRIVATE_BUILDER_SSH_PUBLIC_KEY')) {
        if (-not (Get-Command ssh-keygen -ErrorAction SilentlyContinue)) { throw 'OpenSSH ssh-keygen is required to create the local builder key.' }
        $keyDirectory = Join-Path $root '.azure\builder-keys'
        if (-not (Test-Path -LiteralPath $keyDirectory)) { New-Item -ItemType Directory -Path $keyDirectory | Out-Null }
        $privateKey = Join-Path $keyDirectory "meghkosha-builder-$EnvironmentName"
        $keyPath = "$privateKey.pub"
        if (-not (Test-Path -LiteralPath $keyPath)) {
            if (Test-Path -LiteralPath $privateKey) { throw "Private key exists without its public key. Recover $keyPath; it will not be overwritten." }
            & ssh-keygen -t ed25519 -N '' -f $privateKey | Out-Host
            if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $keyPath)) { throw 'Could not create the local builder SSH key.' }
        }
    }
    $common = @{
        EnvironmentName = $EnvironmentName; SubscriptionId = $script:subscription
        Location = $Location; FoundryLocation = $FoundryLocation; BuilderVmSize = $BuilderVmSize; BuilderSshPublicKeyPath = $keyPath
        Repository = $Repository; SourceCommit = $commit; Apply = $true; ApproveAzureChanges = $true
        Confirm = $false; AllowNativeExportTrustedServices = $AllowNativeExportTrustedServices
        BlobDataProtection = $BlobDataProtection; IncludeLocalhostRedirects = $IncludeLocalhostRedirects
        MaxAttempts = $MaxAttempts; SettleSeconds = $SettleSeconds
    }
    if ($ApproveOwnedRegistrationMigration) {
        $common.ApproveOwnedRegistrationMigration = $true
        $common.PreviousWebOrigin = $PreviousWebOrigin
        $common.PreviousOboPrincipalId = $PreviousOboPrincipalId
        $common.ExpectedApiClientId = $ExpectedApiClientId
        $common.ExpectedWebClientId = $ExpectedWebClientId
    }
    $runner = $PSCommandPath
    function Run-Stage([string] $Name, [hashtable] $Extra = @{}) {
        Write-Host "`n==> $Name" -ForegroundColor Cyan
        $output = & $runner @common -Stage $Name @Extra
        $result = $output -join "`n" | ConvertFrom-Json -AsHashtable
        if ($result.status -ne 'stage_completed') { throw "Stage $Name did not confirm completion." }
        Reload
    }
    $cleanup = $false
    $deploymentError = $null
    try {
        Run-Stage 'Initialize'
        $cleanup = $true
        if (((Value 'APP_STAGED_FOUNDATION_READY') -eq 'true' -or $deployed) -and
            (Value 'APP_STAGED_FOUNDATION_CHAT_CONFIG') -eq (Chat-Configuration)) {
            $null = Value 'APP_PRIVATE_BUILDER_VM_NAME' -Required
            Check-Private
            Write-Host 'Reusing the private foundation.'
        } else {
            Run-Stage 'Foundation' @{ ApproveCosts = $true }
        }
        if ((Value 'APP_STAGED_BUILD_COMMIT') -eq $commit -and (Value 'APP_STAGED_BUILD_REPOSITORY') -eq $Repository) {
            Check-Build
            Write-Host 'Reusing the attested images for this exact commit.'
        } else {
            Run-Stage 'Build' @{ ApproveImagePublication = $true }
        }
        if ((Value 'APP_ASSESSMENT_DB_BOOTSTRAPPED') -eq 'true' -and
            (Value 'APP_STAGED_BOOTSTRAP_IMAGE') -eq (Value 'APP_STAGED_API_IMAGE')) {
            Write-Host 'Reusing the verified database bootstrap for this image.'
        } else {
            Run-Stage 'Bootstrap' @{ ApproveDatabaseBootstrap = $true }
        }
        $treeId = $ServiceManagementReference
        if (-not $treeId) { $treeId = Value 'APP_SERVICE_MANAGEMENT_REFERENCE' }
        try {
            Run-Stage 'Identity' @{ ApproveIdentityChanges = $true; ServiceManagementReference = $treeId }
        } catch {
            if ($treeId -or $_.Exception.Message -notmatch 'serviceManagementReference') { throw }
            $treeId = (Read-Host 'This tenant requires a Service Tree UUID').Trim()
            $treeGuid = [guid]::Empty
            if (-not [guid]::TryParse($treeId, [ref]$treeGuid) -or $treeGuid -eq [guid]::Empty) { throw 'A valid Service Tree UUID is required.' }
            Run-Stage 'Identity' @{ ApproveIdentityChanges = $true; ServiceManagementReference = $treeGuid.ToString() }
        }
        if ($treeId) { Save 'APP_SERVICE_MANAGEMENT_REFERENCE' $treeId }
        Run-Stage 'Permissions' @{ ApproveRoleAssignments = $true; TargetSubscriptionId = $targets }
        if ((Value 'APP_APPLICATIONS_DEPLOYED') -eq 'true' -and
            (Value 'APP_STAGED_RELEASE_COMMIT') -eq $commit -and
            (Value 'SERVICE_API_IMAGE_NAME') -eq (Value 'APP_STAGED_API_IMAGE') -and
            (Value 'SERVICE_WEB_IMAGE_NAME') -eq (Value 'APP_STAGED_WEB_IMAGE') -and
            (Value 'APP_STAGED_RELEASE_API_CLIENT_ID') -eq (Value 'MEGHKOSHA_API_CLIENT_ID') -and
            (Value 'APP_STAGED_RELEASE_WEB_CLIENT_ID') -eq (Value 'MEGHKOSHA_WEB_CLIENT_ID')) {
            Write-Host 'The same images and sign-in configuration are already released; verifying them.'
        } else {
            Run-Stage 'Release' @{ ApproveRelease = $true }
        }
        Run-Stage 'Verify'
    } catch {
        $deploymentError = $_
        Write-Warning 'Rollout stopped. Completed checkpoints are retained; rerun the same command after resolving the reported failure.'
        throw
    } finally {
        if ($cleanup) {
            try {
                Reload
                if (Value 'APP_PRIVATE_BUILDER_VM_NAME') { Run-Stage 'StopBuilder' }
                else { Write-Warning 'No builder name was returned. Inspect any partially provisioned VM for ongoing charges.' }
            } catch {
                Write-Warning "Builder deallocation failed: $($_.Exception.Message)"
                if ($null -eq $deploymentError) { throw 'Applications were processed, but builder cleanup failed. Inspect/deallocate the builder before accepting the rollout.' }
            }
        }
    }
    return [ordered]@{
        stage = 'All'; environment = $EnvironmentName; status = 'deployment_completed'
        sourceCommit = $commit; webOrigin = (Value 'APP_WEB_ORIGIN' -Required)
        builderDeallocated = $true; assessedSubscriptions = $targets
        note = 'Sign in, complete native FOCUS exports, and validate a representative assessment. Live workload acceptance is not implied by readiness.'
    }
}

Push-Location $root
try {
    if ($Stage -eq 'All') {
        Invoke-AllStages | ConvertTo-Json -Depth 5
    } elseif ($Stage -eq 'Initialize') {
        $id = [guid]::Empty
        if (-not [guid]::TryParse($SubscriptionId, [ref]$id) -or $id -eq [guid]::Empty) { throw 'Initialize requires a nonzero -SubscriptionId.' }
        $script:subscription = $id.ToString()
        $account = Azure @('account', 'show')
        if ($account.state -ne 'Enabled') { throw 'The selected subscription is not enabled. Sign in with an authorized account first.' }
        $environments = @(Invoke-Json 'azd' @('env', 'list', '--output', 'json'))
        if (@($environments | Where-Object { $_.Name -eq $EnvironmentName }).Count) {
            Reload
            if ((Value 'AZURE_SUBSCRIPTION_ID' -Required) -ne $script:subscription) { throw 'Environment belongs to another subscription.' }
        } else {
            & azd env new $EnvironmentName --location $Location --subscription $script:subscription --no-prompt | Out-Host
            if ($LASTEXITCODE -ne 0) { throw 'Could not create the local AZD environment.' }
        }
        $defaults = [ordered]@{
            AZURE_SUBSCRIPTION_ID = $script:subscription; AZURE_TENANT_ID = $account.tenantId
            AZURE_LOCATION = $Location; APP_PROFILE = 'ai'; APP_ASSESSMENT_MODE = 'postgres'
            FOUNDRY_LOCATION = $FoundryLocation; APP_ENABLE_CHAT_RUNTIME = 'true'
            MODEL_ROUTER_DEPLOYMENT_NAME = 'model-router'; APP_ENABLE_AI_RUNTIME = 'false'; APP_AI_VALIDATED = 'true'
            APP_MODEL_DEPLOYMENTS = '[{"name":"model-router","modelFormat":"OpenAI","modelName":"model-router","modelVersion":"2025-11-18","sku":"GlobalStandard","capacity":20}]'
            APP_PRIVATE_REGISTRY = 'true'; APP_REGISTRY_BUILD_MODE = 'private-runner'
            APP_ENABLE_PRIVATE_BUILDER = 'true'; APP_PRIVATE_BUILDER_VM_SIZE = $BuilderVmSize
            APP_ASSESSMENT_FOUNDATION_ONLY = 'true'; APP_ASSESSMENT_DB_BOOTSTRAPPED = 'false'
            APP_ENABLE_PROCESSOR = 'false'; APP_MIN_REPLICAS = '2'
        }
        foreach ($entry in $defaults.GetEnumerator()) {
            if (-not (Value $entry.Key)) { Save $entry.Key ([string]$entry.Value) }
        }
        if ((Value 'APP_PROFILE') -ne 'ai') {
            if ((Value 'APP_APPLICATIONS_DEPLOYED') -eq 'true') { throw 'Promoting a released data-only environment to AI requires a reviewed migration.' }
            Save 'APP_PROFILE' 'ai'
        }
        if ((Value 'APP_ENABLE_CHAT_RUNTIME') -ne 'true') { Save 'APP_ENABLE_CHAT_RUNTIME' 'true' }
        if ((Value 'APP_MODEL_DEPLOYMENTS') -eq '[]') { Save 'APP_MODEL_DEPLOYMENTS' $defaults.APP_MODEL_DEPLOYMENTS }
        if (-not (Value 'APP_PRIVATE_BUILDER_SSH_PUBLIC_KEY')) {
            if (-not $BuilderSshPublicKeyPath) { throw 'Initialize requires -BuilderSshPublicKeyPath (public .pub file only).' }
            $key = (Get-Content -LiteralPath $BuilderSshPublicKeyPath -Raw).Trim()
            if ($key -notmatch '^(ssh-ed25519|ssh-rsa) [A-Za-z0-9+/=]+(?: [^\r\n]+)?$') { throw 'Supply an OpenSSH public key, never a private key.' }
            Save 'APP_PRIVATE_BUILDER_SSH_PUBLIC_KEY' $key
        }
        Check-Compute
        Check-Foundry
        if ($AllowNativeExportTrustedServices) { Save 'APP_EXPORT_TRUSTED_SERVICES' 'true' }
        if ($BlobDataProtection) { Save 'APP_BLOB_DATA_PROTECTION' 'true' }
    } else {
        Reload
        $script:subscription = Value 'AZURE_SUBSCRIPTION_ID' -Required
        if ($SubscriptionId -and $SubscriptionId -ne $script:subscription) { throw 'Subscription differs from the selected environment.' }
        foreach ($required in @('APP_PRIVATE_REGISTRY', 'APP_ENABLE_PRIVATE_BUILDER')) {
            if ((Value $required) -ne 'true') { throw "$required must remain true." }
        }
        if ((Value 'APP_ASSESSMENT_MODE') -ne 'postgres' -or (Value 'APP_REGISTRY_BUILD_MODE') -ne 'private-runner') {
            throw 'This script only supports the staged private-builder PostgreSQL configuration.'
        }
        $group = Value 'AZURE_RESOURCE_GROUP'
        switch ($Stage) {
            'Foundation' {
                if ((Value 'APP_APPLICATIONS_DEPLOYED') -eq 'true') { throw 'Foundation is not an implicit migration of deployed apps.' }
                Check-Compute
                Check-Foundry
                Provision
                if ($PreviewOnly) { break }
                Check-Private
                Save 'APP_STAGED_FOUNDATION_READY' 'true'
                Save 'APP_STAGED_FOUNDATION_CHAT_CONFIG' (Chat-Configuration)
                if ((Value 'APP_RESTORE_AI_ACCOUNT') -eq 'true') { Save 'APP_RESTORE_AI_ACCOUNT' 'false' }
                Save 'APP_REUSE_AI_ACCOUNT' 'true'
            }
            'Build' {
                Check-Private
                $SourceCommit = Check-Source
                $vm = Value 'APP_PRIVATE_BUILDER_VM_NAME' -Required
                $details = Azure @('vm', 'show', '--resource-group', $group, '--name', $vm, '--show-details')
                if ($details.publicIps) { throw 'Builder VM must not have a public IP.' }
                $null = Azure @('vm', 'start', '--resource-group', $group, '--name', $vm)
                $nonce = [guid]::NewGuid().ToString('N')
                $config = @{
                    repository = $Repository; commit = $SourceCommit; nonce = $nonce
                    registry = (Value 'AZURE_CONTAINER_REGISTRY_ENDPOINT' -Required)
                    clientId = (Value 'APP_PRIVATE_BUILDER_CLIENT_ID' -Required)
                } | ConvertTo-Json -Compress
                $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($config))
                $body = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'private-builder.sh') -Raw
                $remote = "#!/bin/sh`n" +
                    'if [ -z "${BASH_VERSION:-}" ]; then exec /bin/bash "$0" "$@"; fi' +
                    "`nexec </dev/null`nexport MEGHKOSHA_BUILD_CONFIG='$encoded'`n$body`n"
                $commandPath = Join-Path $root ".azure\$EnvironmentName\private-build-$nonce.sh"
                $responsePath = Join-Path $root ".azure\$EnvironmentName\private-build-$nonce.result.json"
                try {
                    [IO.File]::WriteAllText($commandPath, $remote.Replace("`r`n", "`n"), [Text.UTF8Encoding]::new($false))
                    $response = Azure @('vm', 'run-command', 'invoke', '--resource-group', $group, '--name', $vm,
                                        '--command-id', 'RunShellScript', '--scripts', "@$commandPath")
                    [IO.File]::WriteAllText($responsePath, ($response | ConvertTo-Json -Depth 10), [Text.UTF8Encoding]::new($false))
                } finally {
                    if (Test-Path -LiteralPath $commandPath) { Remove-Item -LiteralPath $commandPath -Force }
                }
                $text = ($response.value | ForEach-Object { $_.message }) -join "`n"
                $matches = [regex]::Matches($text, '(?m)^MEGHKOSHA_BUILD_RESULT (\{[^\r\n]+\})\s*$')
                $failed = @($response.value | Where-Object { $_.level -eq 'Error' -or $_.code -match '/failed$' })
                if ($matches.Count -ne 1 -or $failed.Count -or $text -match '(?m)^MEGHKOSHA_BUILD_ERROR ') {
                    $details = [regex]::Replace($text, '(?i)(Authorization:\s*Bearer\s+)\S+', '$1[redacted]')
                    $details = [regex]::Replace($details, '\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b', '[redacted-token]')
                    if ($details.Length -gt 3500) { $details = $details.Substring($details.Length - 3500) }
                    if ([string]::IsNullOrWhiteSpace($details)) { $details = 'Run Command returned no stdout/stderr. The image build was not verified.' }
                    throw "Private build did not return one successful, error-free attestation. Saved diagnostics: $responsePath`nRun Command output:`n$details"
                }
                $result = $matches[0].Groups[1].Value | ConvertFrom-Json -AsHashtable
                if ($result.commit -ne $SourceCommit -or $result.nonce -ne $nonce -or $result.registry -ne (Value 'AZURE_CONTAINER_REGISTRY_ENDPOINT')) {
                    throw 'Private build attestation does not match this request.'
                }
                foreach ($service in @('api', 'web')) {
                    if ($result["${service}Image"] -notmatch "^$([regex]::Escape($result.registry))/meghkoshaai-$service@sha256:[a-f0-9]{64}$") {
                        throw 'Private build returned an invalid image digest.'
                    }
                }
                Save 'APP_STAGED_BUILD_COMMIT' $SourceCommit
                Save 'APP_STAGED_BUILD_REPOSITORY' $Repository
                Save 'APP_STAGED_API_IMAGE' $result.apiImage
                Save 'APP_STAGED_WEB_IMAGE' $result.webImage
                Save 'SERVICE_ASSESSMENT_IMAGE_NAME' $result.apiImage
                Save 'APP_ASSESSMENT_DB_BOOTSTRAPPED' 'false'
            }
            'Bootstrap' {
                Check-Private
                Check-Build
                if ((Value 'APP_ASSESSMENT_FOUNDATION_ONLY') -ne 'true') { throw 'Bootstrap requires foundation-only staging before release.' }
                Save 'SERVICE_ASSESSMENT_IMAGE_NAME' (Value 'APP_STAGED_API_IMAGE')
                Provision
                if ($PreviewOnly) { break }
                $job = Value 'ASSESSMENT_BOOTSTRAP_JOB' -Required
                $execution = Azure @('containerapp', 'job', 'start', '--resource-group', $group, '--name', $job)
                $completed = $false
                for ($attempt = 0; $attempt -lt 60; $attempt++) {
                    $runs = @(Azure @('containerapp', 'job', 'execution', 'list', '--resource-group', $group, '--name', $job))
                    $run = @($runs | Where-Object { $_.name -eq $execution.name })
                    if ($run.Count -eq 1 -and $run[0].properties.status -eq 'Succeeded') { $completed = $true; break }
                    if ($run.Count -eq 1 -and $run[0].properties.status -in @('Failed', 'Stopped')) { throw 'Database bootstrap failed; release remains blocked.' }
                    Start-Sleep -Seconds 10
                }
                if (-not $completed) { throw 'Bootstrap did not report success before the deadline. Inspect logs; do not mark it verified.' }
                Save 'APP_STAGED_BOOTSTRAP_IMAGE' (Value 'APP_STAGED_API_IMAGE')
                Save 'APP_ASSESSMENT_DB_BOOTSTRAPPED' 'true'
            }
            'Identity' {
                $env:APP_ALLOW_AZURE_CHANGES = 'true'
                $arguments = @{
                    TenantId = (Value 'AZURE_TENANT_ID' -Required); SubscriptionId = $script:subscription
                    EnvironmentName = $EnvironmentName; WebOrigin = (Value 'APP_WEB_ORIGIN' -Required)
                    OboManagedIdentityResourceId = (Value 'MEGHKOSHA_OBO_MANAGED_IDENTITY_RESOURCE_ID' -Required)
                    Apply = $true
                }
                if ($ServiceManagementReference) { $arguments.ServiceManagementReference = $ServiceManagementReference }
                $arguments.Confirm = $false
                if ($IncludeLocalhostRedirects) { $arguments.IncludeLocalhostRedirects = $true }
                if ($ApproveOwnedRegistrationMigration) {
                    $arguments.ApproveOwnedRegistrationMigration = $true
                    $arguments.PreviousWebOrigin = $PreviousWebOrigin
                    $arguments.PreviousOboPrincipalId = $PreviousOboPrincipalId
                    $arguments.ExpectedApiClientId = $ExpectedApiClientId
                    $arguments.ExpectedWebClientId = $ExpectedWebClientId
                }
                $identity = & (Join-Path $PSScriptRoot 'bootstrap-identity.ps1') @arguments | ConvertFrom-Json -AsHashtable
                Save 'MEGHKOSHA_API_CLIENT_ID' $identity.MEGHKOSHA_API_CLIENT_ID
                Save 'MEGHKOSHA_WEB_CLIENT_ID' $identity.MEGHKOSHA_WEB_CLIENT_ID
            }
            'Permissions' {
                if (-not $TargetSubscriptionId.Count -or $TargetSubscriptionId -contains [guid]::Empty) { throw 'Select explicit nonzero target subscription IDs.' }
                $token = (Value 'SERVICE_API_RESOURCE_NAME' -Required) -replace '^ca-api-', ''
                foreach ($purpose in @('api', 'processor')) {
                    $identity = Azure @('identity', 'show', '--resource-group', $group, '--name', "id-$purpose-$token")
                    foreach ($target in $TargetSubscriptionId) {
                        $existing = @(Azure @('role', 'assignment', 'list', '--scope', "/subscriptions/$target",
                            '--query', "[?principalId=='$($identity.principalId)']"))
                        $roles = @{ Reader = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'; 'Cost Management Contributor' = '434105ed-43f6-45c7-a02f-909b2ba83430' }
                        foreach ($role in $roles.Keys) {
                            if (@($existing | Where-Object { $_.roleDefinitionId -like "*/$($roles[$role])" -and $_.scope -eq "/subscriptions/$target" }).Count) { continue }
                            $null = Azure @('role', 'assignment', 'create', '--assignee-object-id', $identity.principalId,
                                '--assignee-principal-type', 'ServicePrincipal', '--role', $role, '--scope', "/subscriptions/$target")
                        }
                    }
                }
                Save 'APP_STAGED_TARGET_SUBSCRIPTIONS' (($TargetSubscriptionId | ForEach-Object { $_.ToString() }) | ConvertTo-Json -Compress -AsArray)
            }
            'Release' {
                Check-Private
                Check-Build
                if ((Value 'APP_ASSESSMENT_DB_BOOTSTRAPPED') -ne 'true' -or
                    (Value 'APP_STAGED_BOOTSTRAP_IMAGE') -ne (Value 'APP_STAGED_API_IMAGE')) { throw 'Bootstrap must verify the exact reviewed API image before release.' }
                $null = Value 'APP_STAGED_TARGET_SUBSCRIPTIONS' -Required
                $null = Value 'MEGHKOSHA_API_CLIENT_ID' -Required
                $null = Value 'MEGHKOSHA_WEB_CLIENT_ID' -Required
                Save 'SERVICE_API_IMAGE_NAME' (Value 'APP_STAGED_API_IMAGE')
                Save 'SERVICE_WEB_IMAGE_NAME' (Value 'APP_STAGED_WEB_IMAGE')
                Save 'SERVICE_PROCESSOR_IMAGE_NAME' (Value 'APP_STAGED_API_IMAGE')
                Save 'SERVICE_ASSESSMENT_IMAGE_NAME' (Value 'APP_STAGED_API_IMAGE')
                Save 'APP_ENABLE_PROCESSOR' 'true'
                Save 'APP_ASSESSMENT_FOUNDATION_ONLY' 'false'
                Provision
                if (-not $PreviewOnly) {
                    Save 'APP_STAGED_RELEASE_COMMIT' (Value 'APP_STAGED_BUILD_COMMIT')
                    Save 'APP_STAGED_RELEASE_API_CLIENT_ID' (Value 'MEGHKOSHA_API_CLIENT_ID')
                    Save 'APP_STAGED_RELEASE_WEB_CLIENT_ID' (Value 'MEGHKOSHA_WEB_CLIENT_ID')
                }
            }
            'Verify' {
                Check-Private
                $expected = @{
                    (Value 'SERVICE_API_RESOURCE_NAME' -Required) = (Value 'SERVICE_API_IMAGE_NAME' -Required)
                    (Value 'SERVICE_WEB_RESOURCE_NAME' -Required) = (Value 'SERVICE_WEB_IMAGE_NAME' -Required)
                    (Value 'SERVICE_ASSESSMENT_RESOURCE_NAME' -Required) = (Value 'SERVICE_ASSESSMENT_IMAGE_NAME' -Required)
                }
                foreach ($name in $expected.Keys) {
                    $app = Azure @('containerapp', 'show', '--resource-group', $group, '--name', $name)
                    if ($app.properties.provisioningState -ne 'Succeeded' -or $app.properties.runningStatus -ne 'Running') {
                        throw "Application $name is not provisioned/running."
                    }
                    if ($app.properties.template.containers[0].image -ne $expected[$name]) {
                        throw "Application $name is not configured with the reviewed immutable image."
                    }
                }
                $origin = Value 'APP_WEB_ORIGIN' -Required
                foreach ($path in @('health', 'ready')) { $null = Invoke-RestMethod "$origin/api/$path" -TimeoutSec 30 }
            }
            'StopBuilder' {
                $null = Azure @('vm', 'deallocate', '--resource-group', $group, '--name', (Value 'APP_PRIVATE_BUILDER_VM_NAME' -Required))
            }
        }
    }
    if ($Stage -ne 'All') { [ordered]@{
        stage = $Stage; environment = $EnvironmentName; status = $(if ($PreviewOnly) { 'preview_completed' } else { 'stage_completed' })
        webOrigin = (Value 'APP_WEB_ORIGIN')
        note = 'Only this stage ran. Sign-in, native exports and realistic private-network load/restart acceptance remain separate checks.'
    } | ConvertTo-Json -Depth 4 }
} finally {
    Pop-Location
}
