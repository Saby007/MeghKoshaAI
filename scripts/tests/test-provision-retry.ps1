$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
Set-StrictMode -Version Latest
$source = Join-Path $PSScriptRoot '..\deploy-staged.ps1'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($source, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$definitions = foreach ($name in @('Invoke-ProvisionCommand', 'Wait-ProvisionRetry', 'Provision')) {
    $definition = $ast.Find({
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
    }, $true)
    if ($definition) { $definition.Extent.Text }
    elseif ($name -eq 'Provision') { throw 'Production provisioning function was not found.' }
}
$testRoot = Join-Path ([IO.Path]::GetTempPath()) "meghkosha-retry-$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path $testRoot | Out-Null
[IO.File]::WriteAllText((Join-Path $testRoot 'runtime.ps1'), ($definitions -join "`n"))
[IO.File]::WriteAllText((Join-Path $testRoot 'validate-deployment-inputs.ps1'), '$global:retryState.validations++')
$nativeShell = (Get-Command pwsh -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
$EnvironmentName = 'retry-test'
$MaxAttempts = 3
$SettleSeconds = 120
$PreviewOnly = $false
$subscription = '11111111-1111-1111-1111-111111111111'
$prefix = "/subscriptions/$subscription/resourceGroups/rg-retry-test"
$postgresId = "$prefix/providers/Microsoft.DBforPostgreSQL/flexibleServers/pg-aaaaaaaaaaaaa"
$foundryId = "$prefix/providers/Microsoft.CognitiveServices/accounts/ai-aaaaaaaaaaaaa"
$postgresFailure = 'AadAuthOperationCannotBePerformedWhenServerIsNotAccessible: Server is not accessible.'
$foundryFailure = "RequestConflict: Another operation is in progress on the resource '$foundryId'."
$checks = 0

function Assert([bool] $Condition, [string] $Message) {
    if (-not $Condition) { throw $Message }
}
function Reset-Case {
    $script:MaxAttempts = 3
    $script:SettleSeconds = 120
    $script:PreviewOnly = $false
    $global:retryState = @{
        values = @{
            AZURE_RESOURCE_GROUP = 'rg-retry-test'; AZURE_LOCATION = 'centralindia'
            FOUNDRY_LOCATION = 'eastus2'; APP_PROFILE = 'ai'
            APP_REUSE_AI_ACCOUNT = 'false'; APP_RESTORE_AI_ACCOUNT = 'false'
        }
        failures = [Collections.Generic.Queue[string]]::new()
        sleeps = [Collections.Generic.List[int]]::new()
        calls = [Collections.Generic.List[string]]::new()
        reuseAtAttempt = [Collections.Generic.List[string]]::new()
        provisions = 0; previews = 0; validations = 0; reloads = 0
        nativeFailure = $false; previewFails = $false
        polls = 0; activePolls = 0; nestedActivePolls = 0
        pgReads = 0; pgPendingReads = 0; pgState = 'Ready'
        aiReads = 0; aiPendingReads = 0; aiState = 'Succeeded'
        owner = 'retry-test'; resourceOwner = 'retry-test'; currentOwner = 'retry-test'; foreignResource = $false
        ambiguousAccount = $false; queryFailure = ''; unknownDeploymentState = $false
        aiPublic = 'Disabled'; pgPublic = 'Disabled'; aiLocation = 'eastus2'; aiAuth = $true
    }
    $script:values = $global:retryState.values
}
function Value([string] $Name, [switch] $Required) {
    $value = if ($global:retryState.values.ContainsKey($Name)) { $global:retryState.values[$Name] } else { '' }
    if ($Required -and -not $value) { throw "Missing $Name" }
    return $value
}
function Save([string] $Name, [string] $Value) {
    $global:retryState.calls.Add("save $Name")
    $global:retryState.values[$Name] = $Value
}
function Reload { $global:retryState.reloads++ }
function Start-Sleep {
    param([int] $Seconds)
    $global:retryState.sleeps.Add($Seconds)
}
function az { throw 'Live Azure CLI is forbidden in retry tests.' }
function azd {
    $a = @($args)
    $state = $global:retryState
    $global:LASTEXITCODE = 0
    if ($a[0] -ne 'provision' -or $a -notcontains '--environment' -or $a -notcontains '--no-prompt') {
        throw 'Unexpected AZD call in offline provisioning tests.'
    }
    if ($a -contains '--preview') {
        $state.previews++
        if ($state.previewFails) {
            $global:LASTEXITCODE = 1
            return $script:postgresFailure
        }
        return 'Preview succeeded.'
    }
    $state.provisions++
    $state.reuseAtAttempt.Add([string]$env:APP_REUSE_AI_ACCOUNT)
    if ($state.failures.Count) {
        $failure = $state.failures.Dequeue()
        if ($state.nativeFailure) {
            $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes(
                "[Console]::Error.WriteLine('$($failure.Replace("'", "''"))'); exit 1"
            ))
            & $script:nativeShell -NoLogo -NoProfile -NonInteractive -OutputFormat Text -EncodedCommand $encoded
            $global:LASTEXITCODE = $LASTEXITCODE
        } else {
            $global:LASTEXITCODE = 1
            return $failure
        }
        return
    }
    return 'Provisioning succeeded.'
}
function Azure([string[]] $Arguments) {
    $state = $global:retryState
    $command = $Arguments[0..2] -join ' '
    $state.calls.Add($command)
    if ($command -eq $state.queryFailure) { throw 'Management read failed; readiness is unknown.' }
    $tags = @{ 'azd-env-name' = $state.resourceOwner; 'app-name' = 'cost-assessment' }
    switch ($command) {
        'group show --name' {
            return @{ id = $script:prefix; name = 'rg-retry-test'; location = 'centralindia'
                tags = @{ 'azd-env-name' = $state.owner; 'app-name' = 'cost-assessment' } }
        }
        'resource list --resource-group' {
            $pg = @{ id = $script:postgresId; name = 'pg-aaaaaaaaaaaaa'
                type = 'Microsoft.DBforPostgreSQL/flexibleServers'; location = 'centralindia'; tags = $tags }
            $ai = @{ id = $script:foundryId; name = 'ai-aaaaaaaaaaaaa'
                type = 'Microsoft.CognitiveServices/accounts'; kind = 'AIServices'; location = $state.aiLocation; tags = $tags }
            if ($state.foreignResource) { $ai.id = '/subscriptions/other/resourceGroups/other/providers/Microsoft.CognitiveServices/accounts/ai-aaaaaaaaaaaaa' }
            $pg; $ai
            if ($state.ambiguousAccount) { $ai.Clone() }
            return
        }
        'deployment sub list' {
            $state.polls++
            @{ name = 'retry-test-1234'; properties = @{
                provisioningState = $(if ($state.unknownDeploymentState) { '' } elseif ($state.polls -le $state.activePolls) { 'Running' } else { 'Failed' })
            } }
            @{ name = 'unrelated-environment-1234'; properties = @{ provisioningState = 'Running' } }
            return
        }
        'deployment group list' {
            return @(@{ name = 'core'; properties = @{
                provisioningState = $(if ($state.polls -le $state.nestedActivePolls) { 'Accepted' } else { 'Succeeded' })
            } })
        }
        'postgres flexible-server show' {
            $state.pgReads++
            return @{ id = $script:postgresId; state = $(if ($state.pgReads -le $state.pgPendingReads) { 'Updating' } else { $state.pgState })
                tags = @{ 'azd-env-name' = $state.currentOwner; 'app-name' = 'cost-assessment' }
                network = @{ publicNetworkAccess = $state.pgPublic }
                authConfig = @{ passwordAuth = 'Disabled'; activeDirectoryAuth = 'Enabled' } }
        }
        'cognitiveservices account show' {
            $state.aiReads++
            return @{ id = $script:foundryId; location = $state.aiLocation; kind = 'AIServices'
                tags = @{ 'azd-env-name' = $state.currentOwner; 'app-name' = 'cost-assessment' }
                properties = @{
                    provisioningState = $(if ($state.aiReads -le $state.aiPendingReads) { 'Accepted' } else { $state.aiState })
                    publicNetworkAccess = $state.aiPublic; disableLocalAuth = $state.aiAuth
                } }
        }
        default { throw "Unexpected management read in retry tests: $($Arguments -join ' ')" }
    }
}
function Expect-Failure([string] $Pattern, [int] $Attempts) {
    $failure = ''
    try { Provision } catch { $failure = $_.Exception.Message }
    Assert ($failure -match "(?s)$Pattern") "Expected '$Pattern', received '$failure'."
    Assert ($global:retryState.provisions -eq $Attempts) 'Unexpected provisioning attempt count after refusal.'
    Assert ($global:retryState.reloads -eq 0) 'A failed deployment was reloaded as if it succeeded.'
    Assert $PSNativeCommandUseErrorActionPreference 'Failure changed strict native-command handling.'
    $script:checks++
}
try {
    . (Join-Path $testRoot 'runtime.ps1')
    $forwarding = $ast.Find({
        param($node)
        $node -is [System.Management.Automation.Language.AssignmentStatementAst] -and $node.Left.Extent.Text -eq '$common'
    }, $true)
    Assert ($forwarding.Right.Extent.Text -match 'MaxAttempts\s*=\s*\$MaxAttempts' -and
        $forwarding.Right.Extent.Text -match 'SettleSeconds\s*=\s*\$SettleSeconds') 'Stage All must forward retry controls to every child provisioning stage.'
    $checks++
    Reset-Case
    $global:retryState.nativeFailure = $true
    $global:retryState.failures.Enqueue($postgresFailure)
    Provision
    Assert ($global:retryState.provisions -eq 2 -and $global:retryState.previews -eq 2) 'The actual native first failure was not retried with a fresh preview.'
    Assert ($global:retryState.reloads -eq 1) 'Only successful provisioning may reload the environment.'
    Assert $PSNativeCommandUseErrorActionPreference 'Recovery changed strict native-command handling.'
    Assert ($global:retryState.sleeps[0] -eq 120) 'The existing default settlement delay was not preserved.'
    Assert (($global:retryState.reuseAtAttempt -join '|') -eq 'false|true') 'Verified account reuse was not applied to the retry process environment.'
    $checks++

    Reset-Case
    $global:retryState.values.Remove('AZURE_RESOURCE_GROUP')
    $global:retryState.failures.Enqueue("$postgresFailure`n$foundryFailure")
    Provision
    Assert ($global:retryState.provisions -eq 2) 'First-foundation recovery required AZD outputs that a failed deployment has not returned.'
    $checks++

    Reset-Case
    $global:retryState.values.APP_RESTORE_AI_ACCOUNT = 'true'
    $global:retryState.failures.Enqueue("$postgresFailure`n$foundryFailure")
    $global:retryState.activePolls = 1
    $global:retryState.nestedActivePolls = 2
    $global:retryState.pgPendingReads = 1
    $global:retryState.aiPendingReads = 2
    Provision
    Assert ($global:retryState.polls -ge 5 -and $global:retryState.provisions -eq 2) 'Retry did not wait for both deployment graphs and both resources.'
    Assert ($global:retryState.values.APP_RESTORE_AI_ACCOUNT -eq 'false' -and $global:retryState.values.APP_REUSE_AI_ACCOUNT -eq 'true') 'Only a verified settled account may transition restore to reuse.'
    $checks++

    foreach ($errorCode in @('RequestDisallowedByPolicy', 'QuotaExceeded', 'AuthorizationFailed', 'SkuNotAvailable', 'LocationNotAvailableForResourceType', 'InvalidTemplate', 'UnrecognizedPermanentError')) {
        Reset-Case
        $global:retryState.failures.Enqueue("${errorCode}: refused.")
        Expect-Failure $errorCode 1
        Assert (-not $global:retryState.calls.Count -and -not $global:retryState.sleeps.Count) 'A permanent/unknown refusal triggered retry or recovery reads.'
    }
    Reset-Case
    $global:retryState.failures.Enqueue("RequestDisallowedByPolicy: refused.`n$postgresFailure")
    Expect-Failure 'RequestDisallowedByPolicy' 1
    Assert (-not $global:retryState.calls.Count) 'A transient error masked a simultaneous policy refusal.'

    Reset-Case
    $global:retryState.failures.Enqueue($postgresFailure)
    $global:retryState.failures.Enqueue($foundryFailure)
    Provision
    Assert ($global:retryState.provisions -eq 3 -and $global:retryState.previews -eq 3) 'Default retry budget did not permit a third successful attempt.'
    $checks++

    Reset-Case
    $MaxAttempts = 2
    $SettleSeconds = 7
    $global:retryState.failures.Enqueue($postgresFailure)
    $global:retryState.failures.Enqueue($postgresFailure)
    Expect-Failure 'after 2 attempt.*AadAuthOperation' 2
    Assert (($global:retryState.sleeps -join '|') -eq '7') 'Exhaustion waited again or ignored the configured settlement delay.'

    Reset-Case
    $MaxAttempts = 1
    $global:retryState.failures.Enqueue($postgresFailure)
    Expect-Failure 'after 1 attempt' 1
    Assert (-not $global:retryState.calls.Count -and -not $global:retryState.sleeps.Count) 'MaxAttempts=1 must not run recovery.'

    foreach ($mode in @('owner', 'resourceOwner', 'currentOwner', 'foreignResource', 'ambiguousAccount', 'aiPublic', 'pgPublic', 'aiLocation', 'aiAuth', 'aiState', 'pgState', 'queryFailure', 'unknownDeploymentState')) {
        Reset-Case
        $global:retryState.failures.Enqueue("$postgresFailure`n$foundryFailure")
        switch ($mode) {
            owner { $global:retryState.owner = 'other-environment' }
            resourceOwner { $global:retryState.resourceOwner = 'other-environment' }
            currentOwner { $global:retryState.currentOwner = 'other-environment' }
            foreignResource { $global:retryState.foreignResource = $true }
            ambiguousAccount { $global:retryState.ambiguousAccount = $true }
            aiPublic { $global:retryState.aiPublic = 'Enabled' }
            pgPublic { $global:retryState.pgPublic = 'Enabled' }
            aiLocation { $global:retryState.aiLocation = 'westus' }
            aiAuth { $global:retryState.aiAuth = 'true' }
            aiState { $global:retryState.aiState = 'Failed' }
            pgState { $global:retryState.pgState = 'Stopped' }
            queryFailure { $global:retryState.queryFailure = 'deployment sub list' }
            unknownDeploymentState { $global:retryState.unknownDeploymentState = $true }
        }
        Expect-Failure 'No retry was started' 1
        Assert ($global:retryState.values.APP_REUSE_AI_ACCOUNT -eq 'false') "Unsafe/unknown $mode enabled account reuse."
    }

    Reset-Case
    $global:retryState.failures.Enqueue('DeploymentActive: an earlier deployment is still running.')
    $global:retryState.activePolls = 100
    Expect-Failure 'settle.*600|600.*settle' 1
    Assert ($global:retryState.polls -eq 41 -and @($global:retryState.sleeps | Where-Object { $_ -eq 15 }).Count -eq 40) 'Active-deployment wait is not bounded to ten minutes.'

    foreach ($resource in @('pgPendingReads', 'aiPendingReads')) {
        Reset-Case
        $global:retryState.failures.Enqueue("$postgresFailure`n$foundryFailure")
        $global:retryState[$resource] = 100
        Expect-Failure 'settle.*600|600.*settle' 1
        Assert ($global:retryState.polls -eq 41 -and $global:retryState.values.APP_REUSE_AI_ACCOUNT -eq 'false') 'Resource-readiness timeout enabled reuse or exceeded the polling budget.'
    }

    Reset-Case
    $global:retryState.previewFails = $true
    Expect-Failure 'preview failed.*AadAuthOperation' 0
    Assert ($global:retryState.previews -eq 1 -and -not $global:retryState.calls.Count) 'Preview failure must not trigger provisioning recovery.'

    Reset-Case
    $PreviewOnly = $true
    Provision
    Assert ($global:retryState.previews -eq 1 -and -not $global:retryState.provisions -and -not $global:retryState.calls.Count) 'PreviewOnly performed provisioning or settlement reads.'
    $checks++

    Reset-Case
    $PSNativeCommandUseErrorActionPreference = $false
    Provision
    Assert (-not $PSNativeCommandUseErrorActionPreference) 'Successful capture did not restore a false native-command preference.'
    $global:retryState.failures.Enqueue('RequestDisallowedByPolicy: refused.')
    $failure = ''
    try { Provision } catch { $failure = $_.Exception.Message }
    Assert ($failure -match 'RequestDisallowedByPolicy' -and -not $PSNativeCommandUseErrorActionPreference) 'Failed capture did not preserve a false native-command preference.'
    $PSNativeCommandUseErrorActionPreference = $true
    $checks++
    [ordered]@{ result = 'passed'; cases = $checks; realNativeFailureCovered = $true; liveAzureCalls = 0 } | ConvertTo-Json -Compress
} finally {
    Remove-Item -LiteralPath $testRoot -Recurse -Force
    Remove-Variable retryState -Scope Global -ErrorAction SilentlyContinue
}
