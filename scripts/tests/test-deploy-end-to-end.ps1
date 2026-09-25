Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$tenantId = '11111111-1111-1111-1111-111111111111'
$subscriptionId = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
$targetOne = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
$targetTwo = 'cccccccc-cccc-cccc-cccc-cccccccccccc'
$environmentName = 'app-test'
$resourceGroup = "rg-$environmentName"
$apiPrincipalId = '22222222-2222-2222-2222-222222222222'
$processorPrincipalId = '33333333-3333-3333-3333-333333333333'
$apiClientId = '44444444-4444-4444-4444-444444444444'
$webClientId = '55555555-5555-5555-5555-555555555555'

$global:deployTestState = @{
    environments = @{}
    upCalls = [System.Collections.Generic.List[string]]::new()
    previewCalls = 0
    authChecks = 0
    interactiveLogins = 0
    cloneCalls = 0
    containerApps = $false
    imagesPublished = $false
    failNextUp = 'AccountProvisioningStateInvalid: the account is still in Accepted'
    roleAssignments = [System.Collections.Generic.List[object]]::new()
    roleCreates = [System.Collections.Generic.List[object]]::new()
    bootstrapCalls = 0
    modelChecks = 0
    catalogueUnavailable = $false
    # eastus2 offers the approved Model Router; centralindia offers an older variant only, which is
    # what the gate must reject rather than silently deploy.
    modelRegions = @{
        'eastus2' = @(
            @{ model = @{ name = 'model-router'; format = 'OpenAI'; version = '2025-11-18'; skus = @(@{ name = 'GlobalStandard' }, @{ name = 'DataZoneStandard' }) } },
            @{ model = @{ name = 'gpt-4o'; format = 'OpenAI'; version = '2024-11-20'; skus = @(@{ name = 'GlobalStandard' }) } }
        )
        'centralindia' = @(
            @{ model = @{ name = 'model-router'; format = 'OpenAI'; version = '2025-05-19'; skus = @(@{ name = 'DataZoneStandard' }) } },
            @{ model = @{ name = 'gpt-4o'; format = 'OpenAI'; version = '2024-11-20'; skus = @(@{ name = 'GlobalStandard' }) } }
        )
        'westus3' = @(
            @{ model = @{ name = 'gpt-4o'; format = 'OpenAI'; version = '2024-11-20'; skus = @(@{ name = 'GlobalStandard' }) } }
        )
    }
    # PowerShell resolves a function's unqualified variables through the runtime caller chain, so a
    # stub reading $subscriptionId would silently pick up the script's own -SubscriptionId parameter.
    # Every fixture the stubs need therefore lives here and is read through $global:deployTestState.
    tenantId = $tenantId
    subscriptionId = $subscriptionId
    environmentName = $environmentName
    resourceGroup = $resourceGroup
    apiPrincipalId = $apiPrincipalId
    processorPrincipalId = $processorPrincipalId
}

function Get-StubArgument {
    param([string[]] $Arguments, [string] $Name)
    $index = [array]::IndexOf($Arguments, $Name)
    if ($index -lt 0 -or $index + 1 -ge $Arguments.Count) { return '' }
    return $Arguments[$index + 1]
}

function Start-Sleep { param([int] $Seconds) }

function Invoke-WebRequest {
    param($Uri, [switch] $UseBasicParsing, [int] $TimeoutSec)
    if ("$Uri" -ne 'https://web.example.test/api/health') { throw "Unexpected health probe: $Uri" }
    return [pscustomobject]@{ StatusCode = 200 }
}

function git {
    $global:LASTEXITCODE = 0
    $global:deployTestState.cloneCalls++
    throw 'The test checkout already exists; cloning must be skipped.'
}

function azd {
    $a = @($args)
    $global:LASTEXITCODE = 0
    $state = $global:deployTestState
    $environment = Get-StubArgument $a '--environment'
    if ($a[0] -eq 'auth' -and $a[1] -eq 'login') {
        if ($a -contains '--check-status') {
            $state.authChecks++
            $global:LASTEXITCODE = 0
            return
        }
        $state.interactiveLogins++
        return
    }
    if ($a[0] -eq 'env') {
        switch ($a[1]) {
            'list' { return (ConvertTo-Json -InputObject @($state.environments.Keys | ForEach-Object { @{ Name = $_ } }) -Depth 3) }
            'new' {
                $name = $a[2]
                if ((Get-StubArgument $a '--subscription') -ne $state.subscriptionId -or (Get-StubArgument $a '--location') -ne 'centralindia') {
                    throw 'azd env new must pin the subscription and location, otherwise azd prompts for them interactively.'
                }
                $state.environments[$name] = [ordered]@{
                    AZURE_ENV_NAME = $name
                    AZURE_TENANT_ID = $state.tenantId
                }
                return
            }
            'select' {
                if (-not $state.environments.ContainsKey($a[2])) { throw "Unknown environment $($a[2])." }
                return
            }
            'set' {
                if (-not $state.environments.ContainsKey($environment)) { throw "azd env set targeted an unknown environment '$environment'." }
                $state.environments[$environment][$a[4]] = $a[5]
                return
            }
            'get-value' {
                if (-not $state.environments.ContainsKey($environment)) { throw "azd env get-value targeted an unknown environment '$environment'." }
                $name = $a[-1]
                if (-not $state.environments[$environment].Contains($name)) {
                    $global:LASTEXITCODE = 1
                    return "ERROR: key '$name' not found"
                }
                return $state.environments[$environment][$name]
            }
        }
        throw "Unexpected azd env call: $($a -join ' ')"
    }
    if ($a[0] -eq 'provision') {
        if ($a -notcontains '--preview') { throw 'The helper must only ever call azd provision in preview mode.' }
        $state.previewCalls++
        return
    }
    if ($a[0] -eq 'up') {
        $values = $state.environments[$environment]
        if ($state.failNextUp) {
            $message = $state.failNextUp
            $state.failNextUp = ''
            $state.upCalls.Add('failed')
            Write-Output $message
            $global:LASTEXITCODE = 1
            return
        }
        if (-not $state.imagesPublished) {
            # azd publishes the images on the pass that creates them, before Bicep can use the names.
            $state.imagesPublished = $true
            $values['SERVICE_API_IMAGE_NAME'] = 'cost-app/api:v1'
            $values['SERVICE_WEB_IMAGE_NAME'] = 'cost-app/web:v1'
            $values['AZURE_RESOURCE_GROUP'] = $state.resourceGroup
            $values['APP_WEB_ORIGIN'] = 'https://web.example.test'
            $values['MEGHKOSHA_OBO_MANAGED_IDENTITY_RESOURCE_ID'] = "/subscriptions/$($state.subscriptionId)/resourceGroups/$($state.resourceGroup)/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-obo"
        } else {
            $state.containerApps = $true
            $values['SERVICE_WEB_ENDPOINT_URL'] = 'https://web.example.test'
        }
        $state.upCalls.Add('succeeded')
        return
    }
    throw "Unexpected azd call: $($a -join ' ')"
}

function az {
    $a = @($args)
    $global:LASTEXITCODE = 0
    $state = $global:deployTestState
    if ($a[0] -eq 'account' -and $a[1] -eq 'show') {
        $query = Get-StubArgument $a '--query'
        if ($query -eq 'id') { return $state.subscriptionId }
        if ($query -eq 'name') {
            if ((Get-StubArgument $a '--subscription') -ne $state.subscriptionId) { throw 'The subscription must be verified by ID, not by whatever az happens to default to.' }
            return 'Contoso Subscription'
        }
        return
    }
    if ($a[0] -eq 'login') { $state.interactiveLogins++; return }
    if ($a[0] -eq 'cognitiveservices' -and $a[1] -eq 'model' -and $a[2] -eq 'list') {
        $state.modelChecks++
        $region = Get-StubArgument $a '--location'
        if ((Get-StubArgument $a '--subscription') -ne $state.subscriptionId) { throw 'The model catalogue must be read from the deployment subscription.' }
        if ($state.catalogueUnavailable) { $global:LASTEXITCODE = 1; return }
        if (-not $state.modelRegions.ContainsKey($region)) { return (ConvertTo-Json -InputObject @() -Depth 3) }
        return (ConvertTo-Json -InputObject $state.modelRegions[$region] -Depth 6)
    }
    if ($a[0] -in @('cognitiveservices', 'deployment', 'containerapp', 'identity') -and
        (Get-StubArgument $a '--subscription') -ne $state.subscriptionId) {
        throw "az $($a[0]) must be pinned to the deployment subscription, otherwise it reads the wrong one."
    }
    if ($a[0] -eq 'cognitiveservices') { return '' }
    if ($a[0] -eq 'deployment') { return '' }
    if ($a[0] -eq 'containerapp' -and $a[1] -eq 'list') {
        if (-not $state.containerApps) { return '' }
        $query = Get-StubArgument $a '--query'
        if ($query -match 'fqdn') { return 'web.example.test' }
        return "ca-api-$($state.environmentName)`nca-web-$($state.environmentName)"
    }
    if ($a[0] -eq 'identity' -and $a[1] -eq 'list') {
        if ((Get-StubArgument $a '-g') -ne $state.resourceGroup) { throw 'Managed identities must be listed from the deployment resource group.' }
        $query = Get-StubArgument $a '--query'
        if ($query -match "id-api-") { return $state.apiPrincipalId }
        if ($query -match "id-processor-") { return $state.processorPrincipalId }
        return "name              principalId`nid-api-x          $($state.apiPrincipalId)"
    }
    if ($a[0] -eq 'role' -and $a[1] -eq 'assignment' -and $a[2] -eq 'list') {
        $scope = Get-StubArgument $a '--scope'
        return (ConvertTo-Json -InputObject @($state.roleAssignments |
            Where-Object { $_.scope -eq $scope } |
            ForEach-Object { @{ principalId = $_.principalId; role = $_.role } }) -Depth 3)
    }
    if ($a[0] -eq 'role' -and $a[1] -eq 'assignment' -and $a[2] -eq 'create') {
        if ((Get-StubArgument $a '--assignee-principal-type') -ne 'ServicePrincipal') { throw 'Role assignments must declare the ServicePrincipal principal type.' }
        $assignment = @{
            principalId = Get-StubArgument $a '--assignee-object-id'
            role = Get-StubArgument $a '--role'
            scope = Get-StubArgument $a '--scope'
        }
        if (@($state.roleAssignments | Where-Object {
                $_.principalId -eq $assignment.principalId -and $_.role -eq $assignment.role -and $_.scope -eq $assignment.scope }).Count) {
            Write-Output 'ERROR: (RoleAssignmentExists) The role assignment already exists.'
            $global:LASTEXITCODE = 1
            return
        }
        $state.roleAssignments.Add($assignment)
        $state.roleCreates.Add($assignment)
        return
    }
    throw "Unexpected az call: $($a -join ' ')"
}

$repoRoot = Join-Path ([System.IO.Path]::GetTempPath()) "meghkosha-deploy-test-$([guid]::NewGuid().ToString('n'))"
New-Item -ItemType Directory -Path (Join-Path $repoRoot 'scripts') -Force | Out-Null
try {
    Set-Content -LiteralPath (Join-Path $repoRoot 'azure.yaml') -Value 'name: meghkosha-test'
    Set-Content -LiteralPath (Join-Path $repoRoot 'scripts/bootstrap-identity.ps1') -Value @"
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][guid] `$TenantId,
    [Parameter(Mandatory)][guid] `$SubscriptionId,
    [Parameter(Mandatory)][string] `$EnvironmentName,
    [Parameter(Mandatory)][uri] `$WebOrigin,
    [Parameter(Mandatory)][string] `$OboManagedIdentityResourceId,
    [switch] `$IncludeLocalhostRedirects,
    [switch] `$GrantAdminConsent,
    [switch] `$Apply
)
if (-not `$Apply) { throw 'The helper must apply the identity configuration.' }
if (`$env:APP_ALLOW_AZURE_CHANGES -ne 'true') { throw 'The helper must set the explicit approval flag.' }
`$global:deployTestState.bootstrapCalls++
[ordered]@{
    action = 'Configured'
    AZURE_TENANT_ID = `$TenantId.ToString()
    MEGHKOSHA_API_CLIENT_ID = '$apiClientId'
    MEGHKOSHA_WEB_CLIENT_ID = '$webClientId'
    liveValidationRequired = `$true
} | ConvertTo-Json -Depth 5
"@

    $script = Join-Path $PSScriptRoot '../deploy-end-to-end.ps1'
    $parameters = @{
        EnvironmentName = $environmentName
        RepoDirectory = $repoRoot
        TargetSubscriptionId = "$targetOne, $targetTwo"
        SettleSeconds = 0
        MaxAttempts = 3
    }

    $plan = & $script @parameters -PlanOnly | ConvertFrom-Json -AsHashtable
    if (@($plan.assessedSubscriptions) -join ',' -ne "$targetOne,$targetTwo") { throw 'Comma-separated subscriptions were not expanded into separate targets.' }
    if ($global:deployTestState.upCalls.Count -ne 0) { throw '-PlanOnly must not deploy anything.' }

    $summary = & $script @parameters
    if (@($summary).Count -ne 1) { throw 'The helper must return only its deployment summary; progress output belongs on the host.' }
    $summary = $summary | ConvertFrom-Json -AsHashtable
    $state = $global:deployTestState
    $values = $state.environments[$environmentName]

    $expected = @{
        AZURE_LOCATION = 'centralindia'
        FOUNDRY_LOCATION = 'eastus2'
        AZURE_SUBSCRIPTION_ID = $subscriptionId
        APP_PROFILE = 'ai'
        APP_EXPORT_TRUSTED_SERVICES = 'true'
        MODEL_ROUTER_DEPLOYMENT_NAME = 'model-router'
        APP_ENABLE_CHAT_RUNTIME = 'true'
        APP_AI_VALIDATED = 'true'
        APP_ENABLE_AI_RUNTIME = 'false'
        APP_REUSE_AI_ACCOUNT = 'true'
        APP_ENABLE_PROCESSOR = 'true'
        SERVICE_PROCESSOR_IMAGE_NAME = 'cost-app/api:v1'
        MEGHKOSHA_API_CLIENT_ID = $apiClientId
        MEGHKOSHA_WEB_CLIENT_ID = $webClientId
    }
    foreach ($key in $expected.Keys) {
        if (-not $values.Contains($key) -or $values[$key] -ne $expected[$key]) {
            throw "Environment value $key is '$(if ($values.Contains($key)) { $values[$key] } else { '<unset>' })' instead of '$($expected[$key])'."
        }
    }
    if ($values['APP_MODEL_DEPLOYMENTS'] -notmatch '"modelName":"model-router"' -or $values['APP_MODEL_DEPLOYMENTS'] -notmatch '"capacity":20\}') { throw 'The approved Model Router deployment definition was not applied.' }
    if ($state.previewCalls -ne 1) { throw "azd provision --preview ran $($state.previewCalls) times instead of once." }
    if ($state.authChecks -ne 1 -or $state.interactiveLogins -ne 0) { throw 'Sign-in must be checked once and must not prompt when already authenticated.' }
    if ($state.cloneCalls -ne 0) { throw 'An existing checkout must not be cloned again.' }
    if ($state.bootstrapCalls -ne 1) { throw "bootstrap-identity.ps1 ran $($state.bootstrapCalls) times instead of once." }
    if ($state.modelChecks -ne 1) { throw "The model availability check ran $($state.modelChecks) times instead of once." }
    if (($state.upCalls -join ',') -ne 'failed,succeeded,succeeded,succeeded,succeeded') {
        throw "Unexpected azd up sequence: $($state.upCalls -join ',')"
    }
    if ($state.roleCreates.Count -ne 6) { throw "Expected six role assignments, got $($state.roleCreates.Count)." }
    foreach ($scope in @("/subscriptions/$targetOne", "/subscriptions/$targetTwo")) {
        $roles = @($state.roleCreates | Where-Object { $_.scope -eq $scope })
        if (@($roles | Where-Object { $_.principalId -eq $apiPrincipalId -and $_.role -eq 'Reader' }).Count -ne 1 -or
            @($roles | Where-Object { $_.principalId -eq $apiPrincipalId -and $_.role -eq 'Cost Management Contributor' }).Count -ne 1 -or
            @($roles | Where-Object { $_.principalId -eq $processorPrincipalId -and $_.role -eq 'Cost Management Contributor' }).Count -ne 1) {
            throw "The three documented role assignments were not all made on $scope."
        }
    }
    if ($summary.webUrl -ne 'https://web.example.test' -or -not $summary.processorEnabled -or
        $summary.apiClientId -ne $apiClientId -or $summary.subscription -ne $subscriptionId -or
        @($summary.assessedSubscriptions).Count -ne 2) {
        throw 'The deployment summary misreports the delivered environment.'
    }

    # A rerun against a settled environment must deploy the current code once and nothing more:
    # no repeated image-handoff, processor or sign-in deployments, and no duplicated role assignments.
    $state.upCalls.Clear()
    $state.roleCreates.Clear()
    $state.previewCalls = 0
    & $script @parameters -SkipPreview | Out-Null
    if (($state.upCalls -join ',') -ne 'succeeded') { throw "A rerun deployed $($state.upCalls.Count) time(s) instead of only redeploying the current code once." }
    if ($state.roleCreates.Count -ne 0) { throw 'A rerun duplicated role assignments instead of detecting the existing ones.' }
    if ($state.previewCalls -ne 0) { throw '-SkipPreview still ran the infrastructure preview.' }
    if ($state.bootstrapCalls -ne 2) { throw 'The sign-in bootstrap must be re-verified on every run.' }

    # An unavailable model must stop the run before anything is provisioned, not fail deep inside
    # Bicep after the account and networking already exist.
    foreach ($case in @(
        @{ Region = 'westus3'; Reason = 'the model is not offered in the region at all' },
        @{ Region = 'centralindia'; Reason = 'only a different version/SKU of the model is offered' },
        @{ Region = 'nowhere-1'; Reason = 'the region returns an empty catalogue' })) {
        $state.upCalls.Clear()
        $state.previewCalls = 0
        $before = $state.environments.Count
        $stopped = $null
        try { & $script @parameters -FoundryLocation $case.Region | Out-Null }
        catch { $stopped = $_.Exception.Message }
        if (-not $stopped) { throw "Deployment continued even though $($case.Reason) in '$($case.Region)'." }
        if ($stopped -notmatch 'model-router' -or $stopped -notmatch [regex]::Escape($case.Region) -or $stopped -notmatch '-FoundryLocation') {
            throw "The stop message for '$($case.Region)' must name the model, the region and the way to change it. Got: $stopped"
        }
        if ($state.upCalls.Count -ne 0 -or $state.previewCalls -ne 0 -or $state.environments.Count -ne $before) {
            throw "Provisioning started for '$($case.Region)' before the model availability gate stopped it."
        }
    }

    # A catalogue that cannot be read is not evidence of an unavailable model and must not block.
    $state.catalogueUnavailable = $true
    $state.upCalls.Clear()
    try { & $script @parameters -SkipPreview | Out-Null } catch { throw "An unreadable model catalogue must not stop the deployment: $($_.Exception.Message)" }
    if ($state.upCalls.Count -eq 0) { throw 'An unreadable model catalogue wrongly prevented the deployment.' }
    $state.catalogueUnavailable = $false

    # The override exists for a catalogue that disagrees with reality.
    $state.upCalls.Clear()
    $checksBefore = $state.modelChecks
    try { & $script @parameters -FoundryLocation 'westus3' -SkipPreview -SkipModelAvailabilityCheck | Out-Null }
    catch { throw "-SkipModelAvailabilityCheck did not bypass the gate: $($_.Exception.Message)" }
    if ($state.modelChecks -ne $checksBefore) { throw '-SkipModelAvailabilityCheck still queried the catalogue.' }
    if ($state.upCalls.Count -eq 0) { throw '-SkipModelAvailabilityCheck did not allow the deployment to proceed.' }

    [ordered]@{ result = 'passed'; deployments = 5; roleAssignments = 6; rerunRedeploysOnce = $true
                modelGateStopsBeforeProvisioning = 3; catalogueFailureIsNonFatal = $true; gateOverridable = $true } | ConvertTo-Json -Compress
} finally {
    Remove-Variable -Name deployTestState -Scope Global -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $repoRoot -Recurse -Force -ErrorAction SilentlyContinue
}
