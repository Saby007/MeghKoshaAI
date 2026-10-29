$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
Set-StrictMode -Version Latest
$path = Join-Path $PSScriptRoot '..\deploy-staged.ps1'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($name in @('GitHub', 'Check-Source')) {
    $definition = $ast.Find({
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
    }, $true)
    if ($definition) { . ([scriptblock]::Create($definition.Extent.Text)) }
    elseif ($name -eq 'Check-Source') { throw 'Production source validation was not found.' }
}
$Repository = 'https://github.com/synthetic/public-repository.git'
$SourceCommit = ''
$global:sourcePreflightTest = @{
    mode = 'success'
    head = 'c' * 40
    dirty = $false
    missingGh = $false
    calls = [Collections.Generic.List[object]]::new()
    anonymousCalls = 0
    checks = 0
}
function Get-Command {
    param([string] $Name, [object] $ErrorAction)
    if ($Name -ne 'gh') { throw "Unexpected executable discovery: $Name" }
    if (-not $global:sourcePreflightTest.missingGh) { return @{ Name = 'gh' } }
}
function git {
    $global:LASTEXITCODE = 0
    if ($args[0] -eq 'rev-parse') { return $global:sourcePreflightTest.head }
    if ($args[0] -eq 'status') {
        if ($global:sourcePreflightTest.dirty) { return ' M unexpected.txt' }
        return
    }
    throw 'Unexpected Git operation in source preflight.'
}
function gh {
    $arguments = @($args)
    $state = $global:sourcePreflightTest
    $global:LASTEXITCODE = 0
    if ($arguments.Count -ne 6 -or ($arguments[0..4] -join '|') -ne 'api|--hostname|github.com|--method|GET') {
        throw 'GitHub access must be an explicit GET to the fixed github.com host.'
    }
    $endpoint = [string]$arguments[5]
    if ($endpoint -notmatch '^repos/synthetic/public-repository(?:/commits/[a-f0-9]{40})?$') {
        throw "Unexpected GitHub endpoint: $endpoint"
    }
    $state.calls.Add($endpoint)
    if ($state.mode -in @('auth-failed', 'rate-limited', 'network-failed')) {
        $global:LASTEXITCODE = $(if ($state.mode -eq 'auth-failed') { 4 } else { 1 })
        return
    }
    if ($state.mode -eq 'malformed') { return '{not-json' }
    if ($state.mode -eq 'array-response') { return '[]' }
    if ($state.mode -eq 'empty-response') { return }
    if ($endpoint -match '/commits/') {
        if ($state.mode -eq 'missing-commit') {
            $global:LASTEXITCODE = 1
            return '{"message":"Not Found"}'
        }
        return (@{ sha = $(if ($state.mode -eq 'wrong-commit') { 'd' * 40 } else { $state.head }) } |
            ConvertTo-Json -Compress)
    }
    if ($state.mode -eq 'private') { return '{"private":true,"visibility":"private"}' }
    if ($state.mode -eq 'incomplete-metadata') { return '{"visibility":"public"}' }
    if ($state.mode -eq 'invalid-private-type') { return '{"private":"false","visibility":"public"}' }
    return '{"private":false,"visibility":"public"}'
}
function Invoke-RestMethod {
    param([string] $Uri)
    $global:sourcePreflightTest.anonymousCalls++
    throw "Anonymous GitHub API rate limit exceeded: $Uri"
}
function Assert([bool] $Condition, [string] $Message) {
    if (-not $Condition) { throw $Message }
}
function Reset-Case([string] $Mode = 'success') {
    $global:sourcePreflightTest.mode = $Mode
    $global:sourcePreflightTest.head = 'c' * 40
    $global:sourcePreflightTest.dirty = $false
    $global:sourcePreflightTest.missingGh = $false
    $global:sourcePreflightTest.calls.Clear()
}
function Expect-Failure([string] $Pattern, [int] $Calls) {
    $failure = ''
    try { Check-Source | Out-Null } catch { $failure = $_.Exception.Message }
    Assert ($failure -match $Pattern) "Expected '$Pattern', received '$failure'."
    Assert ($global:sourcePreflightTest.calls.Count -eq $Calls) 'Unexpected GitHub calls after a rejected preflight.'
    Assert $PSNativeCommandUseErrorActionPreference 'Native-command error preference was not preserved.'
    $global:sourcePreflightTest.checks++
}
try {
    $result = Check-Source
    Assert ($result -eq ('c' * 40)) 'Authenticated source validation did not return the exact HEAD.'
    Assert (($global:sourcePreflightTest.calls -join '|') -eq (
        'repos/synthetic/public-repository|repos/synthetic/public-repository/commits/' + ('c' * 40)
    )) 'Public metadata and exact commit must both be verified.'
    Assert $PSNativeCommandUseErrorActionPreference 'Successful lookup changed strict native-command handling.'
    $global:sourcePreflightTest.checks++

    foreach ($mode in @('auth-failed', 'rate-limited', 'network-failed')) {
        Reset-Case $mode
        Expect-Failure 'GitHub source validation failed' 1
    }
    Reset-Case
    $global:sourcePreflightTest.missingGh = $true
    Expect-Failure 'GitHub CLI.*required' 0
    Reset-Case 'private'
    Expect-Failure 'public source only' 1
    Reset-Case 'incomplete-metadata'
    Expect-Failure 'metadata.*incomplete' 1
    Reset-Case 'invalid-private-type'
    Expect-Failure 'metadata.*incomplete' 1
    Reset-Case 'missing-commit'
    Expect-Failure 'GitHub source validation failed' 2
    Reset-Case 'wrong-commit'
    Expect-Failure 'not published' 2
    Reset-Case 'malformed'
    Expect-Failure 'JSON' 1
    Reset-Case 'array-response'
    Expect-Failure 'JSON object' 1
    Reset-Case 'empty-response'
    Expect-Failure 'empty response' 1

    Reset-Case
    $global:sourcePreflightTest.dirty = $true
    Expect-Failure 'reviewed clean checkout' 0
    Reset-Case
    $SourceCommit = 'd' * 40
    Expect-Failure 'reviewed local HEAD' 0
    $SourceCommit = ''
    Reset-Case
    $global:sourcePreflightTest.head = 'not-a-commit'
    Expect-Failure 'reviewed local HEAD' 0

    Assert ($global:sourcePreflightTest.anonymousCalls -eq 0) 'Source validation fell back to the anonymous API.'
    [ordered]@{
        result = 'passed'
        checks = $global:sourcePreflightTest.checks
        liveNetworkCalls = 0
        azureCalls = 0
        anonymousFallbacks = 0
        credentialsExtracted = $false
    } | ConvertTo-Json -Compress
} finally {
    Remove-Item Function:\Get-Command, Function:\git, Function:\gh, Function:\Invoke-RestMethod
    Remove-Variable sourcePreflightTest -Scope Global
    $global:LASTEXITCODE = 0
}
