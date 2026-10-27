[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Initialize', 'Foundation', 'Build', 'Bootstrap', 'Identity', 'Permissions', 'Release', 'Verify', 'StopBuilder')]
    [string] $Stage,
    [ValidatePattern('^[a-z0-9][a-z0-9-]{0,63}$')]
    [string] $EnvironmentName = 'meghkoshaai-scalable-dev',
    [string] $SubscriptionId = '',
    [string] $Location = 'centralindia',
    [string] $BuilderVmSize = 'Standard_D2s_v5',
    [string] $BuilderSshPublicKeyPath = '',
    [ValidatePattern('^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git$')]
    [string] $Repository = 'https://github.com/Saby007/MeghKoshaAI.git',
    [ValidatePattern('^([a-f0-9]{40})?$')]
    [string] $SourceCommit = '',
    [guid[]] $TargetSubscriptionId = @(),
    [string] $ServiceManagementReference = '',
    [switch] $AllowNativeExportTrustedServices,
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
if (-not $Apply -or $WhatIfPreference) {
    [ordered]@{
        stage = $Stage; environment = $EnvironmentName; subscription = $SubscriptionId
        location = $Location; privateRegistry = $true; privateDatabase = $true
        privateBuilder = @{ vmSize = $BuilderVmSize; publicVmIp = $false; inboundAccess = $false; outboundNat = $true }
        source = @{ repository = $Repository; commit = $SourceCommit }
        appliesChanges = $false
        nativeExportTrustedServicesException = [bool]$AllowNativeExportTrustedServices
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
        if ($entry.Count -ne 1 -or $entry[0].currentValue + $needed -gt $entry[0].limit) {
            throw "VM quota $quota is unavailable or insufficient. No provisioning started."
        }
    }
    $version = Value 'APP_PRIVATE_BUILDER_IMAGE_VERSION'
    if (-not $version) { $version = 'latest' }
    $image = Azure @('vm', 'image', 'show', '--location', $region, '--urn', "Canonical:ubuntu-24_04-lts:server:$version")
    if (-not $image.name -or $image.name -notmatch '^\d+\.\d+\.\d+$') { throw 'Ubuntu image version could not be resolved.' }
    Save 'APP_PRIVATE_BUILDER_IMAGE_VERSION' $image.name
}
function Provision {
    foreach ($entry in $script:values.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, [string]$entry.Value, 'Process')
    }
    $env:APP_ALLOW_AZURE_CHANGES = 'true'
    & (Join-Path $PSScriptRoot 'validate-deployment-inputs.ps1') -Operation Provision | Out-Host
    & azd provision --environment $EnvironmentName --preview --no-prompt | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Provision preview failed. No provisioning was started.' }
    if ($PreviewOnly) { return }
    & azd provision --environment $EnvironmentName --no-prompt | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Provisioning failed. Inspect the deployment before retrying.' }
    Reload
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

Push-Location $root
try {
    if ($Stage -eq 'Initialize') {
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
            & azd env new $EnvironmentName --no-prompt | Out-Host
            if ($LASTEXITCODE -ne 0) { throw 'Could not create the local AZD environment.' }
        }
        $defaults = [ordered]@{
            AZURE_SUBSCRIPTION_ID = $script:subscription; AZURE_TENANT_ID = $account.tenantId
            AZURE_LOCATION = $Location; APP_PROFILE = 'data'; APP_ASSESSMENT_MODE = 'postgres'
            APP_PRIVATE_REGISTRY = 'true'; APP_REGISTRY_BUILD_MODE = 'private-runner'
            APP_ENABLE_PRIVATE_BUILDER = 'true'; APP_PRIVATE_BUILDER_VM_SIZE = $BuilderVmSize
            APP_ASSESSMENT_FOUNDATION_ONLY = 'true'; APP_ASSESSMENT_DB_BOOTSTRAPPED = 'false'
            APP_ENABLE_PROCESSOR = 'false'; APP_MIN_REPLICAS = '2'
        }
        foreach ($entry in $defaults.GetEnumerator()) {
            if (-not (Value $entry.Key)) { Save $entry.Key ([string]$entry.Value) }
        }
        if (-not (Value 'APP_PRIVATE_BUILDER_SSH_PUBLIC_KEY')) {
            if (-not $BuilderSshPublicKeyPath) { throw 'Initialize requires -BuilderSshPublicKeyPath (public .pub file only).' }
            $key = (Get-Content -LiteralPath $BuilderSshPublicKeyPath -Raw).Trim()
            if ($key -notmatch '^(ssh-ed25519|ssh-rsa) [A-Za-z0-9+/=]+(?: [^\r\n]+)?$') { throw 'Supply an OpenSSH public key, never a private key.' }
            Save 'APP_PRIVATE_BUILDER_SSH_PUBLIC_KEY' $key
        }
        Check-Compute
        if ($AllowNativeExportTrustedServices) { Save 'APP_EXPORT_TRUSTED_SERVICES' 'true' }
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
                Provision
                if ($PreviewOnly) { break }
                Check-Private
            }
            'Build' {
                Check-Private
                $head = (& git rev-parse HEAD).Trim()
                if ($LASTEXITCODE -ne 0 -or (& git status --porcelain)) { throw 'Build only a reviewed clean checkout.' }
                if (-not $SourceCommit) { $SourceCommit = $head }
                if ($SourceCommit -ne $head) { throw 'SourceCommit must equal the reviewed local HEAD.' }
                $repoPath = $Repository -replace '^https://github.com/', '' -replace '\.git$', ''
                $metadata = Invoke-RestMethod "https://api.github.com/repos/$repoPath"
                if ($metadata.private -or $metadata.visibility -ne 'public') { throw 'This builder accepts public source only; no GitHub credentials are copied.' }
                $published = Invoke-RestMethod "https://api.github.com/repos/$repoPath/commits/$SourceCommit"
                if ($published.sha -ne $SourceCommit) { throw 'The reviewed commit is not published in the selected public repository.' }
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
                $remote = "exec /bin/bash <<'MEGHKOSHA_PRIVATE_BUILD'`nexport MEGHKOSHA_BUILD_CONFIG='$encoded'`n$body`nMEGHKOSHA_PRIVATE_BUILD"
                $commandPath = Join-Path $root ".azure\$EnvironmentName\private-build-$nonce.sh"
                try {
                    [IO.File]::WriteAllText($commandPath, $remote.Replace("`r`n", "`n"), [Text.UTF8Encoding]::new($false))
                    $response = Azure @('vm', 'run-command', 'invoke', '--resource-group', $group, '--name', $vm,
                                        '--command-id', 'RunShellScript', '--scripts', "@$commandPath")
                } finally {
                    if (Test-Path -LiteralPath $commandPath) { Remove-Item -LiteralPath $commandPath -Force }
                }
                $text = ($response.value | ForEach-Object { $_.message }) -join "`n"
                $matches = [regex]::Matches($text, '(?m)^MEGHKOSHA_BUILD_RESULT (\{[^\r\n]+\})\s*$')
                if ($matches.Count -ne 1) { throw 'Private build did not return one success attestation. Inspect VM Run Command diagnostics.' }
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
            }
            'Verify' {
                Check-Private
                foreach ($name in @((Value 'SERVICE_API_RESOURCE_NAME' -Required), (Value 'SERVICE_ASSESSMENT_RESOURCE_NAME' -Required))) {
                    $app = Azure @('containerapp', 'show', '--resource-group', $group, '--name', $name)
                    if ($app.properties.provisioningState -ne 'Succeeded' -or $app.properties.runningStatus -ne 'Running') {
                        throw "Application $name is not provisioned/running."
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
    [ordered]@{
        stage = $Stage; environment = $EnvironmentName; status = $(if ($PreviewOnly) { 'preview_completed' } else { 'stage_completed' })
        webOrigin = (Value 'APP_WEB_ORIGIN')
        note = 'Only this stage ran. Sign-in, native exports and realistic private-network load/restart acceptance remain separate checks.'
    } | ConvertTo-Json -Depth 4
} finally {
    Pop-Location
}
