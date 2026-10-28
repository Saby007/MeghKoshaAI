$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$scripts = Split-Path $PSScriptRoot -Parent
$identityScript = Join-Path $scripts 'bootstrap-identity.ps1'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($identityScript, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($name in @('Assert-Federation', 'Assert-Callbacks')) {
    $definition = $ast.Find({ param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
    }, $true)
    if (-not $definition) { throw "Missing production guard: $name" }
    . ([scriptblock]::Create($definition.Extent.Text))
}
function Expect-Failure([scriptblock] $Action, [string] $Pattern) {
    try { & $Action } catch {
        if ($_.Exception.Message -notmatch $Pattern) { throw }
        return
    }
    throw "Expected a guarded failure matching $Pattern."
}
$PreviousOboPrincipalId = [guid]'11111111-1111-1111-1111-111111111111'
$expected = @{
    name = 'obo-managed-identity'; issuer = 'https://login.microsoftonline.com/tenant/v2.0'
    subject = '22222222-2222-2222-2222-222222222222'; audiences = @('api://AzureADTokenExchange')
}
$previous = @{ name = $expected.name; issuer = $expected.issuer; subject = $PreviousOboPrincipalId.ToString(); audiences = $expected.audiences }
$ApproveOwnedRegistrationMigration = $false
Assert-Federation @($expected) $expected
Expect-Failure { Assert-Federation @($previous) $expected -AllowPrevious } 'No trust relationship was replaced'
$ApproveOwnedRegistrationMigration = $true
Assert-Federation @($previous) $expected -AllowPrevious
Assert-Federation @($expected) $expected -AllowPrevious
Expect-Failure { Assert-Federation @($previous) $expected } 'No trust relationship was replaced'
foreach ($changed in @(
    @{ subject = '33333333-3333-3333-3333-333333333333' },
    @{ issuer = 'https://unexpected.example.test/' },
    @{ audiences = @('unexpected') },
    @{ name = 'unrelated-trust' }
)) {
    $credential = $previous.Clone()
    foreach ($key in $changed.Keys) { $credential[$key] = $changed[$key] }
    Expect-Failure { Assert-Federation @($credential) $expected -AllowPrevious } 'No trust relationship was replaced'
}
Expect-Failure { Assert-Federation @($previous, $expected) $expected -AllowPrevious } 'No trust relationship was replaced'
Assert-Callbacks @('https://old.example.test/auth-callback.html') @(
    'https://old.example.test/auth-callback.html', 'https://new.example.test/auth-callback.html'
)
Expect-Failure {
    Assert-Callbacks @('https://unrelated.example.test/auth-callback.html') @('https://old.example.test/auth-callback.html')
} 'Unexpected SPA callbacks'
$common = @{
    TenantId = '11111111-1111-1111-1111-111111111111'
    SubscriptionId = '11111111-1111-1111-1111-111111111111'
    EnvironmentName = 'synthetic-dev'
    WebOrigin = 'https://new.example.test'
    OboManagedIdentityResourceId = '/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/obo'
}
$migration = @{
    ApproveOwnedRegistrationMigration = $true
    PreviousWebOrigin = 'https://old.example.test'
    PreviousOboPrincipalId = $PreviousOboPrincipalId
    ExpectedApiClientId = '44444444-4444-4444-4444-444444444444'
    ExpectedWebClientId = '55555555-5555-5555-5555-555555555555'
}
$preview = & $identityScript @common @migration | ConvertFrom-Json
if ($preview.ownedRegistrationMigration.expectedApiClientId -ne $migration.ExpectedApiClientId -or
    $preview.createsClientSecret -or $preview.assignsSubscriptionRoles) { throw 'Migration preview widened its scope.' }
Expect-Failure { & $identityScript @common -ApproveOwnedRegistrationMigration } 'requires the exact previous'
Expect-Failure { & $identityScript @common -PreviousOboPrincipalId $PreviousOboPrincipalId } 'require -ApproveOwnedRegistrationMigration'
$badOrigin = $migration.Clone()
$badOrigin.PreviousWebOrigin = 'http://old.example.test'
Expect-Failure { & $identityScript @common @badOrigin } 'requires the exact previous'
$preview = & (Join-Path $scripts 'deploy-end-to-end.ps1') -EnvironmentName synthetic-dev -PlanOnly @migration | ConvertFrom-Json
if (-not $preview.ownedRegistrationMigrationApproved -or $preview.appliesChanges) { throw 'End-to-end preview dropped the explicit migration approval.' }
'Guarded owned-identity migration contracts passed; no Azure calls were made.'
