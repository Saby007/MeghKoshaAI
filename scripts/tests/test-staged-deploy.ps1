$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$testRoot = Join-Path ([IO.Path]::GetTempPath()) "meghkosha-staged-$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path (Join-Path $testRoot 'scripts'), (Join-Path $testRoot '.azure\staged-test'), (Join-Path $testRoot 'api\jobs') | Out-Null
foreach ($file in @('deploy-staged.ps1', 'validate-deployment-inputs.ps1', 'private-builder.sh')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "..\$file") -Destination (Join-Path $testRoot "scripts\$file")
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot '..\..\api\jobs\scheduler.py') -Destination (Join-Path $testRoot 'api\jobs\scheduler.py')
$global:stagedState = @{
    calls = [Collections.Generic.List[string]]::new()
    provisions = 0; previews = 0; badBuild = $false; bootstrapFailed = $false; quotaInsufficient = $false
    assignments = [Collections.Generic.List[object]]::new()
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
        SERVICE_API_RESOURCE_NAME = 'ca-api-test'; SERVICE_ASSESSMENT_RESOURCE_NAME = 'ca-assessment-test'
    }
}
function azd {
    $a = @($args); $global:LASTEXITCODE = 0
    $global:stagedState.calls.Add("azd $($a[0])")
    if ($a[0] -eq 'env' -and $a[1] -eq 'get-values') { return ($global:stagedState.values | ConvertTo-Json -Depth 10) }
    if ($a[0] -eq 'env' -and $a[1] -eq 'set') { $global:stagedState.values[$a[2]] = $a[3]; return }
    if ($a[0] -eq 'env' -and $a[1] -eq 'list') { return '[{"Name":"staged-test"}]' }
    if ($a[0] -eq 'provision') {
        if ($a -contains '--preview') { $global:stagedState.previews++ } else { $global:stagedState.provisions++ }
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
    if ($a[0] -eq 'account' -and $a[1] -eq 'show') { return '{"state":"Enabled","tenantId":"22222222-2222-2222-2222-222222222222"}' }
    if ($command -eq 'postgres flexible-server show') {
        return '{"network":{"publicNetworkAccess":"Disabled"},"authConfig":{"passwordAuth":"Disabled","activeDirectoryAuth":"Enabled"}}'
    }
    if ($a[0] -eq 'vm' -and $a[1] -eq 'list-skus') {
        return '[{"name":"Standard_D2s_v5","family":"standardDSv5Family","restrictions":[],"capabilities":[{"name":"vCPUs","value":"2"}]}]'
    }
    if ($a[0] -eq 'vm' -and $a[1] -eq 'list-usage') {
        $limit = if ($global:stagedState.quotaInsufficient) { 0 } else { 100 }
        return "[{`"name`":{`"value`":`"cores`"},`"currentValue`":0,`"limit`":$limit},{`"name`":{`"value`":`"standardDSv5Family`"},`"currentValue`":0,`"limit`":$limit}]"
    }
    if ($command -eq 'vm image show') { return '{"name":"24.04.202610010"}' }
    if ($a[0] -eq 'vm' -and $a[1] -eq 'show') {
        return '{"publicIps":"","powerState":"VM deallocated","hardwareProfile":{"vmSize":"Standard_D2s_v5"}}'
    }
    if ($a[0] -eq 'vm' -and $a[1] -in @('start', 'deallocate')) { return '{}' }
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
        return '{"properties":{"provisioningState":"Succeeded","runningStatus":"Running"}}'
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
function Assert-Refused([scriptblock] $Action, [string] $Message) {
    $failure = ''
    try { & $Action | Out-Null } catch { $failure = $_.Exception.Message }
    if ($failure -notlike "*$Message*") { throw "Expected refusal '$Message', got '$failure'." }
}
try {
    $script = Join-Path $testRoot 'scripts\deploy-staged.ps1'
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
    [ordered]@{
        result = 'passed'; liveNetworkCalls = 0; defaultIsPlanOnly = $true
        failedBootstrapBlocksRelease = $true; staleBuildAttestationRejected = $true
        stagesAreExplicit = $true; provisioningCallsStubbed = $global:stagedState.provisions
    } | ConvertTo-Json -Compress
} finally {
    Remove-Item -LiteralPath $testRoot -Recurse -Force
    Remove-Variable stagedState -Scope Global
}
