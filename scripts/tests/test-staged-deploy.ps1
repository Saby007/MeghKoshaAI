$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$testRoot = Join-Path ([IO.Path]::GetTempPath()) "meghkosha-staged-$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path (Join-Path $testRoot 'scripts'), (Join-Path $testRoot '.azure\staged-test'), (Join-Path $testRoot 'api\jobs') | Out-Null
foreach ($file in @('deploy-staged.ps1', 'deploy-end-to-end.ps1', 'validate-deployment-inputs.ps1', 'private-builder.sh')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "..\$file") -Destination (Join-Path $testRoot "scripts\$file")
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot '..\..\api\jobs\scheduler.py') -Destination (Join-Path $testRoot 'api\jobs\scheduler.py')
[IO.File]::WriteAllText((Join-Path $testRoot 'scripts\bootstrap-identity.ps1'), @'
[CmdletBinding(SupportsShouldProcess)]
param($TenantId,$SubscriptionId,$EnvironmentName,$WebOrigin,$OboManagedIdentityResourceId,$ServiceManagementReference,[switch]$Apply,[switch]$IncludeLocalhostRedirects)
$global:stagedState.calls.Add('identity')
@{ MEGHKOSHA_API_CLIENT_ID='44444444-4444-4444-4444-444444444444'; MEGHKOSHA_WEB_CLIENT_ID='55555555-5555-5555-5555-555555555555' } | ConvertTo-Json -Compress
'@)
$global:stagedState = @{
    calls = [Collections.Generic.List[string]]::new()
    provisions = 0; previews = 0; badBuild = $false; bootstrapFailed = $false; quotaInsufficient = $false
    assignments = [Collections.Generic.List[object]]::new()
    responses = [Collections.Generic.Queue[string]]::new()
    prompts = [Collections.Generic.List[string]]::new()
    environmentExists = $true; cleanupFailed = $false; imageDrift = $false; keyCreates = 0; foundryUnavailable = $false
    quotaCoreUsage = 2; quotaFamilyUsage = 2; quotaCoreLimit = 150; quotaFamilyLimit = 100
    quotaPowerState = 'VM deallocated'; malformedQuota = $false
    values = @{
        AZURE_ENV_NAME = 'staged-test'; AZURE_SUBSCRIPTION_ID = '11111111-1111-1111-1111-111111111111'
        AZURE_TENANT_ID = '22222222-2222-2222-2222-222222222222'; AZURE_LOCATION = 'centralindia'
        AZURE_RESOURCE_GROUP = 'rg-staged-test'; APP_PROFILE = 'data'; APP_ASSESSMENT_MODE = 'postgres'
        APP_PRIVATE_REGISTRY = 'true'; APP_ENABLE_PRIVATE_BUILDER = 'true'; APP_REGISTRY_BUILD_MODE = 'private-runner'
        APP_PRIVATE_BUILDER_VM_SIZE = 'Standard_D2s_v5'; APP_PRIVATE_BUILDER_IMAGE_VERSION = '24.04.202610010'
        APP_PRIVATE_BUILDER_SSH_PUBLIC_KEY = 'ssh-ed25519 AAAAC3NzaSynthetic'
        APP_PRIVATE_BUILDER_VM_NAME = 'vm-builder-test'
        APP_PRIVATE_BUILDER_CLIENT_ID = '33333333-3333-3333-3333-333333333333'
        AZURE_CONTAINER_REGISTRY_NAME = 'acrstagedtest'; AZURE_CONTAINER_REGISTRY_ENDPOINT = 'acrstagedtest.azurecr.io'
        APP_ASSESSMENT_FOUNDATION_ONLY = 'true'; APP_ASSESSMENT_DB_BOOTSTRAPPED = 'false'
        ASSESSMENT_BOOTSTRAP_JOB = 'job-db-bootstrap-test'; APP_WEB_ORIGIN = 'https://web.example.test'
        ASSESSMENT_DB_HOST = 'pg-test.postgres.database.azure.com'
        APP_FOUNDRY_ACCOUNT_NAME = 'ai-test'
        AI_SERVICES_ENDPOINT = 'https://ai-test.cognitiveservices.azure.com/'
        SERVICE_API_RESOURCE_NAME = 'ca-api-test'; SERVICE_ASSESSMENT_RESOURCE_NAME = 'ca-assessment-test'
        SERVICE_WEB_RESOURCE_NAME = 'ca-web-test'
        MEGHKOSHA_OBO_MANAGED_IDENTITY_RESOURCE_ID = '/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-staged-test/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-obo-test'
    }
}
function azd {
    $a = @($args); $global:LASTEXITCODE = 0
    $global:stagedState.calls.Add("azd $($a[0])")
    if ($a[0] -eq 'env' -and $a[1] -eq 'get-values') { return ($global:stagedState.values | ConvertTo-Json -Depth 10) }
    if ($a[0] -eq 'env' -and $a[1] -eq 'set') { $global:stagedState.values[$a[2]] = $a[3]; return }
    if ($a[0] -eq 'auth' -and $a[1] -eq 'login') { return }
    if ($a[0] -eq 'env' -and $a[1] -eq 'list') {
        return $(if ($global:stagedState.environmentExists) { '[{"Name":"staged-test"}]' } else { '[]' })
    }
    if ($a[0] -eq 'env' -and $a[1] -eq 'new') {
        if ($a -notcontains '--location' -or $a -notcontains '--subscription' -or $a -notcontains '--no-prompt') {
            throw 'New environment must pin subscription/region without hidden AZD questions.'
        }
        $global:stagedState.environmentExists=$true
        return
    }
    if ($a[0] -eq 'provision') {
        if ($a -contains '--preview') { $global:stagedState.previews++ }
        else {
            $global:stagedState.provisions++
            $global:stagedState.values.APP_APPLICATIONS_DEPLOYED = $(if ($global:stagedState.values.APP_ASSESSMENT_FOUNDATION_ONLY -eq 'true') { 'false' } else { 'true' })
        }
        return
    }
    throw "Unexpected offline AZD call: $($a -join ' ')"
}
function az {
    $a = @($args); $global:LASTEXITCODE = 0
    $global:stagedState.calls.Add("az $($a[0..([Math]::Min(2,$a.Count-1))] -join ' ')")
    $command = $a[0..([Math]::Min(2,$a.Count-1))] -join ' '
    if ($a[0] -eq 'acr' -and $a[1] -eq 'show') {
        return '{"sku":{"name":"Premium"},"publicNetworkAccess":"Disabled","adminUserEnabled":false}'
    }
    if ($a[0] -eq 'account' -and $a[1] -eq 'show') {
        if ($a -contains 'none') { return }
        return '{"state":"Enabled","tenantId":"22222222-2222-2222-2222-222222222222"}'
    }
    if ($command -eq 'cognitiveservices model list') {
        if ($global:stagedState.foundryUnavailable) { return '[]' }
        return '[{"model":{"name":"model-router","format":"OpenAI","version":"2025-11-18","skus":[{"name":"GlobalStandard"}]}}]'
    }
    if ($command -eq 'cognitiveservices account show') {
        return '{"location":"eastus2","properties":{"publicNetworkAccess":"Disabled","disableLocalAuth":true}}'
    }
    if ($command -eq 'postgres flexible-server show') {
        return '{"network":{"publicNetworkAccess":"Disabled"},"authConfig":{"passwordAuth":"Disabled","activeDirectoryAuth":"Enabled"}}'
    }
    if ($a[0] -eq 'vm' -and $a[1] -eq 'list-skus') {
        return '[{"name":"Standard_D2s_v5","family":"standardDSv5Family","restrictions":[],"capabilities":[{"name":"vCPUs","value":"2"}]}]'
    }
    if ($a[0] -eq 'vm' -and $a[1] -eq 'list-usage') {
        $coreLimit = if ($global:stagedState.quotaInsufficient) { '0' } else { [string]$global:stagedState.quotaCoreLimit }
        $familyLimit = if ($global:stagedState.quotaInsufficient) { '0' } else { [string]$global:stagedState.quotaFamilyLimit }
        $coreUsage = if ($global:stagedState.malformedQuota) { 'invalid' } else { [string]$global:stagedState.quotaCoreUsage }
        return (@(
            @{ name = @{ value = 'cores' }; currentValue = $coreUsage; limit = $coreLimit }
            @{ name = @{ value = 'standardDSv5Family' }; currentValue = [string]$global:stagedState.quotaFamilyUsage; limit = $familyLimit }
        ) | ConvertTo-Json -Depth 4)
    }
    if ($command -eq 'vm image show') { return '{"name":"24.04.202610010"}' }
    if ($a[0] -eq 'vm' -and $a[1] -eq 'show') {
        return (@{ publicIps = ''; powerState = $global:stagedState.quotaPowerState
            hardwareProfile = @{ vmSize = 'Standard_D2s_v5' } } | ConvertTo-Json -Depth 4)
    }
    if ($a[0] -eq 'vm' -and $a[1] -in @('start', 'deallocate')) {
        if ($a[1] -eq 'deallocate' -and $global:stagedState.cleanupFailed) {
            $global:LASTEXITCODE=1
            return
        }
        return '{}'
    }
    if ($command -eq 'vm run-command invoke') {
        $argument = [string]$a[[array]::IndexOf($a, '--scripts') + 1]
        if (-not $argument.StartsWith('@')) { throw 'Run Command must read a local file, not shell-escaped inline code.' }
        $text = Get-Content -LiteralPath $argument.Substring(1) -Raw
        $match = [regex]::Match($text, "MEGHKOSHA_BUILD_CONFIG='([^']+)'")
        $config = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($match.Groups[1].Value)) | ConvertFrom-Json
        $result = @{
            nonce = $(if ($global:stagedState.badBuild) { 'wrong-nonce' } else { $config.nonce })
            commit = $config.commit; registry = $config.registry
            apiImage = "$($config.registry)/meghkoshaai-api@sha256:$('a'*64)"
            webImage = "$($config.registry)/meghkoshaai-web@sha256:$('b'*64)"
        } | ConvertTo-Json -Compress
        return (@{ value = @(@{ message = "MEGHKOSHA_BUILD_RESULT $result`n" }) } | ConvertTo-Json -Depth 5)
    }
    if ($command -eq 'containerapp job start') { return '{"name":"bootstrap-execution"}' }
    if ($command -eq 'containerapp job execution') {
        $status = if ($global:stagedState.bootstrapFailed) { 'Failed' } else { 'Succeeded' }
        return "[{`"name`":`"bootstrap-execution`",`"properties`":{`"status`":`"$status`"}}]"
    }
    if ($a[0] -eq 'containerapp' -and $a[1] -eq 'show') {
        $name = [string]$a[[array]::IndexOf($a, '--name') + 1]
        $image = if ($name -eq 'ca-web-test') { $global:stagedState.values.SERVICE_WEB_IMAGE_NAME }
            elseif ($name -eq 'ca-api-test') { $global:stagedState.values.SERVICE_API_IMAGE_NAME }
            else { $global:stagedState.values.SERVICE_ASSESSMENT_IMAGE_NAME }
        if ($global:stagedState.imageDrift) { $image = 'acrstagedtest.azurecr.io/unreviewed:latest' }
        return (@{ properties=@{ provisioningState='Succeeded'; runningStatus='Running'; template=@{ containers=@(@{image=$image}) } } } | ConvertTo-Json -Depth 7)
    }
    if ($a[0] -eq 'identity' -and $a[1] -eq 'show') {
        $id = if (($a -join ' ') -match 'id-api-test') { '66666666-6666-6666-6666-666666666666' } else { '77777777-7777-7777-7777-777777777777' }
        return "{`"principalId`":`"$id`"}"
    }
    if ($command -eq 'role assignment list') {
        $query = [string]$a[[array]::IndexOf($a, '--query') + 1]
        $id = [regex]::Match($query, '[0-9a-f-]{36}').Value
        $items = @($global:stagedState.assignments | Where-Object { $_.principalId -eq $id })
        return ConvertTo-Json -InputObject $items -Depth 4
    }
    if ($command -eq 'role assignment create') {
        $role = [string]$a[[array]::IndexOf($a, '--role') + 1]
        $id = [string]$a[[array]::IndexOf($a, '--assignee-object-id') + 1]
        $scope = [string]$a[[array]::IndexOf($a, '--scope') + 1]
        $roleId = if ($role -eq 'Reader') { 'acdd72a7-3385-48ef-bd42-f606fba81ae7' } else { '434105ed-43f6-45c7-a02f-909b2ba83430' }
        $global:stagedState.assignments.Add(@{ principalId = $id; scope = $scope; roleDefinitionId = "/roles/$roleId" })
        return '{}'
    }
    throw "Unexpected offline Azure call: $command"
}
function git {
    $global:LASTEXITCODE = 0
    if ($args[0] -eq 'rev-parse') { return ('c' * 40) }
    if ($args[0] -eq 'status') { return }
    throw 'Unexpected offline Git call.'
}
function Invoke-RestMethod {
    param([string] $Uri, [int] $TimeoutSec)
    if ($Uri -match '/commits/') { return @{ sha = 'c' * 40 } }
    if ($Uri -match '^https://api.github.com/repos/') { return @{ private = $false; visibility = 'public' } }
    if ($Uri -match '^https://web.example.test/api/(health|ready)$') { return @{ status = 'ok' } }
    throw "Unexpected offline HTTP call: $Uri"
}
function Start-Sleep { param([int] $Seconds) }
function Read-Host {
    param([string] $Prompt)
    $global:stagedState.prompts.Add($Prompt)
    if (-not $global:stagedState.responses.Count) { throw "Unexpected question: $Prompt" }
    return $global:stagedState.responses.Dequeue()
}
function ssh-keygen {
    $global:LASTEXITCODE=0
    $a=@($args)
    $path=[string]$a[[array]::IndexOf($a,'-f')+1]
    if (Test-Path -LiteralPath $path) { throw 'Do not overwrite a private key.' }
    $global:stagedState.keyCreates++
    [IO.File]::WriteAllText($path,'synthetic-local-private-key')
    [IO.File]::WriteAllText("$path.pub",'ssh-ed25519 AAAAC3NzaSyntheticGenerated')
}
function Assert-Refused([scriptblock] $Action, [string] $Message) {
    $failure = ''
    try { & $Action | Out-Null } catch { $failure = $_.Exception.Message }
    if ($failure -notlike "*$Message*") { throw "Expected refusal '$Message', got '$failure'." }
}
try {
    $script = Join-Path $testRoot 'scripts\deploy-staged.ps1'
    $initialValues = $global:stagedState.values.Clone()
    $common = @{ EnvironmentName = 'staged-test'; Apply = $true; ApproveAzureChanges = $true }
    $plan = & $script -Stage Foundation -EnvironmentName staged-test | ConvertFrom-Json
    if ($plan.appliesChanges -or $global:stagedState.calls.Count) { throw 'Plan-only mode made an external call.' }
    Assert-Refused { & $script -Stage Build @common } 'separate explicit approval'
    Assert-Refused { & $script -Stage Build -Apply -EnvironmentName staged-test } '-ApproveAzureChanges'
    Assert-Refused { & $script -Stage Foundation -EnvironmentName meghkoshaai-dev } 'layout hold'
    $global:stagedState.quotaInsufficient = $true
    Assert-Refused { & $script -Stage Initialize @common -SubscriptionId '11111111-1111-1111-1111-111111111111' } 'quota'
    if ($global:stagedState.provisions) { throw 'Insufficient quota must stop before provisioning.' }
    $global:stagedState.quotaInsufficient = $false
    & $script -Stage Initialize @common -SubscriptionId '11111111-1111-1111-1111-111111111111' | Out-Null
    $global:stagedState.quotaCoreUsage = 148
    $global:stagedState.quotaFamilyUsage = 98
    & $script -Stage Initialize @common -SubscriptionId '11111111-1111-1111-1111-111111111111' | Out-Null
    $global:stagedState.quotaCoreUsage = 149
    Assert-Refused { & $script -Stage Initialize @common -SubscriptionId '11111111-1111-1111-1111-111111111111' } 'used=149, limit=150, available=1, additional=2'
    $global:stagedState.quotaCoreUsage = 2
    $global:stagedState.quotaFamilyUsage = 99
    Assert-Refused { & $script -Stage Initialize @common -SubscriptionId '11111111-1111-1111-1111-111111111111' } 'used=99, limit=100, available=1, additional=2'
    $global:stagedState.quotaFamilyUsage = 2
    $global:stagedState.malformedQuota = $true
    Assert-Refused { & $script -Stage Initialize @common -SubscriptionId '11111111-1111-1111-1111-111111111111' } 'invalid nonnegative'
    $global:stagedState.malformedQuota = $false
    $global:stagedState.quotaCoreUsage = -1
    Assert-Refused { & $script -Stage Initialize @common -SubscriptionId '11111111-1111-1111-1111-111111111111' } 'invalid nonnegative'
    $global:stagedState.quotaCoreUsage = 2
    $global:stagedState.quotaCoreLimit = 2
    $global:stagedState.quotaFamilyLimit = 2
    $global:stagedState.quotaPowerState = 'VM running'
    & $script -Stage Initialize @common -SubscriptionId '11111111-1111-1111-1111-111111111111' | Out-Null
    $global:stagedState.quotaCoreLimit = 150
    $global:stagedState.quotaFamilyLimit = 100
    $global:stagedState.quotaPowerState = 'VM deallocated'
    if ($global:stagedState.values.APP_PROFILE -ne 'ai' -or
        $global:stagedState.values.FOUNDRY_LOCATION -ne 'eastus2' -or
        $global:stagedState.values.APP_ENABLE_CHAT_RUNTIME -ne 'true' -or
        $global:stagedState.values.APP_ENABLE_AI_RUNTIME -ne 'false') { throw 'Private rollout did not restore Model Router chat with the narrator kept separate.' }
    $global:stagedState.foundryUnavailable=$true
    Assert-Refused { & $script -Stage Foundation @common -ApproveCosts } 'not offered'
    if ($global:stagedState.provisions) { throw 'Unavailable model must block provisioning.' }
    $global:stagedState.foundryUnavailable=$false
    $preview = & $script -Stage Foundation @common -PreviewOnly | ConvertFrom-Json
    if ($preview.status -ne 'preview_completed' -or $global:stagedState.provisions) { throw 'Preview-only stage executed provisioning.' }
    & $script -Stage Build @common -ApproveImagePublication | Out-Null
    if ($global:stagedState.values.APP_STAGED_BUILD_COMMIT -ne ('c' * 40)) { throw 'Verified build was not recorded.' }
    if ($global:stagedState.provisions) { throw 'Build stage must not provision or release apps.' }
    $global:stagedState.badBuild = $true
    Assert-Refused { & $script -Stage Build @common -ApproveImagePublication } 'does not match'
    $global:stagedState.badBuild = $false
    Assert-Refused { & $script -Stage Release @common -ApproveRelease } 'Bootstrap must verify'
    $global:stagedState.bootstrapFailed = $true
    Assert-Refused { & $script -Stage Bootstrap @common -ApproveDatabaseBootstrap } 'bootstrap failed'
    if ($global:stagedState.values.APP_ASSESSMENT_DB_BOOTSTRAPPED -ne 'false') { throw 'Failed bootstrap was marked verified.' }
    $global:stagedState.bootstrapFailed = $false
    & $script -Stage Bootstrap @common -ApproveDatabaseBootstrap | Out-Null
    $global:stagedState.values.MEGHKOSHA_API_CLIENT_ID = '44444444-4444-4444-4444-444444444444'
    $global:stagedState.values.MEGHKOSHA_WEB_CLIENT_ID = '55555555-5555-5555-5555-555555555555'
    & $script -Stage Permissions @common -ApproveRoleAssignments -TargetSubscriptionId '11111111-1111-1111-1111-111111111111' | Out-Null
    & $script -Stage Permissions @common -ApproveRoleAssignments -TargetSubscriptionId '11111111-1111-1111-1111-111111111111' | Out-Null
    if ($global:stagedState.assignments.Count -ne 4) { throw 'Permission stage did not reuse existing scoped grants.' }
    & $script -Stage Release @common -ApproveRelease | Out-Null
    if ($global:stagedState.values.APP_ASSESSMENT_FOUNDATION_ONLY -ne 'false') { throw 'Release stage did not open the reviewed application gate.' }
    & $script -Stage Verify @common | Out-Null
    & $script -Stage StopBuilder @common | Out-Null
    $driver=Join-Path $testRoot 'scripts\deploy-end-to-end.ps1'
    $plan=& $driver -EnvironmentName staged-test -PlanOnly | ConvertFrom-Json
    if ($plan.stage -ne 'All' -or $plan.appliesChanges) { throw 'End-to-end default must describe every private stage without execution.' }
    $regionalPlan=& $driver -EnvironmentName staged-test -FoundryLocation northeurope -PlanOnly | ConvertFrom-Json
    if ($regionalPlan.ai.foundryLocation -ne 'northeurope' -or -not $regionalPlan.ai.enabled) { throw 'FoundryLocation was not forwarded through the PostgreSQL end-to-end entry point.' }
    function Reset-Rollout {
        $global:stagedState.values=$initialValues.Clone()
        $global:stagedState.calls.Clear()
        $global:stagedState.prompts.Clear()
        $global:stagedState.responses.Clear()
        $global:stagedState.assignments.Clear()
        $global:stagedState.provisions=0
        $global:stagedState.previews=0
        $global:stagedState.badBuild=$false
        $global:stagedState.bootstrapFailed=$false
        $global:stagedState.cleanupFailed=$false
        $global:stagedState.imageDrift=$false
    }
    Reset-Rollout
    $global:stagedState.responses.Enqueue('NO')
    Assert-Refused { & $driver -EnvironmentName staged-test -SkipLogin } 'not approved'
    if ($global:stagedState.provisions -or $global:stagedState.assignments.Count) { throw 'Declined rollout made changes.' }
    Reset-Rollout
    $global:stagedState.environmentExists=$false
    $global:stagedState.values.Remove('APP_PRIVATE_BUILDER_SSH_PUBLIC_KEY')
    $global:stagedState.responses.Enqueue('11111111-1111-1111-1111-111111111111')
    $global:stagedState.responses.Enqueue('YES')
    $result=& $driver -EnvironmentName staged-test | ConvertFrom-Json
    if ($result.status -ne 'deployment_completed' -or -not $result.builderDeallocated) { throw 'One-command deployment did not complete.' }
    if ($global:stagedState.prompts.Count -ne 2) { throw 'Expected only subscription entry and one upfront approval.' }
    if ($global:stagedState.provisions -ne 3) { throw 'One-command cold run must provision foundation, bootstrap and release.' }
    if ($global:stagedState.keyCreates -ne 1 -or
        -not (Test-Path -LiteralPath (Join-Path $testRoot '.azure\builder-keys\meghkosha-builder-staged-test.pub'))) {
        throw 'One-command setup did not create its dedicated local public key automatically.'
    }
    $calls=$global:stagedState.calls
    $build=$calls.IndexOf('az vm run-command invoke')
    $bootstrap=$calls.IndexOf('az containerapp job start')
    $identity=$calls.IndexOf('identity')
    $permissions=$calls.IndexOf('az role assignment create')
    $verify=$calls.IndexOf('az containerapp show --resource-group')
    $stop=$calls.IndexOf('az vm deallocate --resource-group')
    if (-not (0 -le $build -and $build -lt $bootstrap -and $bootstrap -lt $identity -and $identity -lt $permissions -and $permissions -lt $verify -and $verify -lt $stop)) {
        throw "End-to-end stages ran out of order: $($calls -join ', ')"
    }
    if ($global:stagedState.values.APP_EXPORT_TRUSTED_SERVICES -ne 'true') { throw 'End-to-end export setting was not forwarded.' }
    $provisions=$global:stagedState.provisions
    $global:stagedState.calls.Clear()
    $rerun=& $driver -EnvironmentName staged-test -SkipLogin -ApproveAll | ConvertFrom-Json
    if ($rerun.status -ne 'deployment_completed' -or $global:stagedState.provisions -ne $provisions) { throw 'Same-commit rerun unnecessarily reprovisioned.' }
    if (@($global:stagedState.calls | Where-Object { $_ -in @('az vm run-command invoke','az containerapp job start') }).Count) {
        throw 'Same-commit rerun rebuilt images or reexecuted bootstrap.'
    }
    if ($global:stagedState.keyCreates -ne 1) { throw 'Rerun must not replace the local SSH key.' }
    $global:stagedState.imageDrift=$true
    Assert-Refused { & $driver -EnvironmentName staged-test -SkipLogin -ApproveAll } 'reviewed immutable image'
    $global:stagedState.imageDrift=$false
    $global:stagedState.values.APP_STAGED_BUILD_COMMIT='d'*40
    Assert-Refused { & $driver -EnvironmentName staged-test -SkipLogin -ApproveAll } 'different source commit'
    Reset-Rollout
    $global:stagedState.bootstrapFailed=$true
    Assert-Refused { & $driver -EnvironmentName staged-test -SkipLogin -ApproveAll } 'bootstrap failed'
    if ($global:stagedState.calls.Contains('identity') -or -not $global:stagedState.calls.Contains('az vm deallocate --resource-group')) {
        throw 'Failed bootstrap must stop later stages and deallocate the builder.'
    }
    $buildCount=@($global:stagedState.calls | Where-Object { $_ -eq 'az vm run-command invoke' }).Count
    $global:stagedState.bootstrapFailed=$false
    $resume=& $driver -EnvironmentName staged-test -SkipLogin -ApproveAll | ConvertFrom-Json
    if ($resume.status -ne 'deployment_completed' -or @($global:stagedState.calls | Where-Object { $_ -eq 'az vm run-command invoke' }).Count -ne $buildCount) {
        throw 'Resuming after bootstrap failure did not reuse the completed build.'
    }
    Reset-Rollout
    $result=& $driver -EnvironmentName staged-test -SkipLogin -ApproveAll `
        -TargetSubscriptionId 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa,bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb' | ConvertFrom-Json
    if ($result.assessedSubscriptions.Count -ne 2 -or $global:stagedState.assignments.Count -ne 8) {
        throw 'The end-to-end entry point did not preserve the explicitly selected subscription scopes.'
    }
    Reset-Rollout
    $global:stagedState.badBuild=$true
    $global:stagedState.cleanupFailed=$true
    Assert-Refused { & $driver -EnvironmentName staged-test -SkipLogin -ApproveAll } 'does not match'
    if ($global:stagedState.calls.Contains('az containerapp job start')) { throw 'Bad build must not run bootstrap.' }
    Reset-Rollout
    $global:stagedState.cleanupFailed=$true
    Assert-Refused { & $driver -EnvironmentName staged-test -SkipLogin -ApproveAll } 'cleanup failed'
    [ordered]@{
        result = 'passed'; liveNetworkCalls = 0; defaultIsPlanOnly = $true
        failedBootstrapBlocksRelease = $true; staleBuildAttestationRejected = $true
        stagesAreExplicit = $true; provisioningCallsStubbed = $global:stagedState.provisions
        oneCommandCompletes = $true; checkpointResumeVerified = $true; failureCleanupVerified = $true
        numericQuotaValidationVerified = $true
    } | ConvertTo-Json -Compress
} finally {
    Remove-Item -LiteralPath $testRoot -Recurse -Force
    Remove-Variable stagedState -Scope Global
}
