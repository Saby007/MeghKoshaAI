Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$tenantId = '11111111-1111-1111-1111-111111111111'
$subscriptionId = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
$environmentName = 'app-test'
$identityId = "/subscriptions/$subscriptionId/resourceGroups/rg-app-test/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-obo"
$armAppId = '797f4846-ba00-4fd7-ba43-dac1f8f63013'
$armPrincipal = @{
    id = '33333333-3333-3333-3333-333333333333'
    appId = $armAppId
    oauth2PermissionScopes = @(@{ id = '44444444-4444-4444-4444-444444444444'; value = 'user_impersonation'; isEnabled = $true })
}
$applications = [System.Collections.Generic.List[object]]::new()
$principals = [System.Collections.Generic.List[object]]::new()
$federations = [System.Collections.Generic.List[object]]::new()
$grants = [System.Collections.Generic.List[object]]::new()
$writes = [System.Collections.Generic.List[object]]::new()
$principals.Add($armPrincipal)
$global:bootstrapTestState = @{
    applications = $applications; principals = $principals; federations = $federations
    grants = $grants; writes = $writes; scenario = 'valid'
}

function az {
    $arguments = @($args)
    $global:LASTEXITCODE = 0
    if ($arguments[0] -eq 'account' -and $arguments[1] -eq 'show') {
        $effectiveTenant = if ($global:bootstrapTestState.scenario -eq 'wrong-tenant') { $subscriptionId } else { $tenantId }
        return (@{ tenantId = $effectiveTenant; state = 'Enabled'; environmentName = 'AzureCloud' } | ConvertTo-Json -Compress)
    }
    if ($arguments[0] -eq 'identity') {
        return (@{ tenantId = $tenantId; id = $identityId; principalId = '55555555-5555-5555-5555-555555555555';
                   clientId = '66666666-6666-6666-6666-666666666666';
                   tags = @{ 'azd-env-name' = $environmentName; 'app-name' = 'cost-assessment' } } | ConvertTo-Json -Depth 5 -Compress)
    }
    if ($arguments[0] -eq 'account' -and $arguments[1] -eq 'get-access-token') {
        if ($arguments -contains '--subscription' -or $arguments -notcontains '--tenant' -or
            $arguments[$arguments.IndexOf('--tenant') + 1] -ne $tenantId) {
            throw 'Graph token acquisition must select only the verified tenant, not tenant plus subscription.'
        }
        return (@{ accessToken = 'synthetic-bootstrap-token' } | ConvertTo-Json -Compress)
    }
    throw 'Unexpected Azure CLI call in offline bootstrap test.'
}

function Invoke-RestMethod {
    param($Uri, $Method, $Headers, $ContentType, $MaximumRedirection, $TimeoutSec, $Body)
    if ($Uri.Host -ne 'graph.microsoft.com' -or $Headers.Authorization -ne 'Bearer synthetic-bootstrap-token' -or $MaximumRedirection -ne 0) {
        throw 'Graph transport contract mismatch.'
    }
    $path = $Uri.AbsolutePath.Substring('/v1.0/'.Length)
    $query = [System.Web.HttpUtility]::ParseQueryString($Uri.Query)
    $filter = $query['$filter']
    $payload = if ($Body) { $Body | ConvertFrom-Json -AsHashtable } else { @{} }
    if ($Method -eq 'GET') {
        if ($path -eq 'applications') {
            if ($filter -notmatch "^displayName eq '([^']+)'$") { throw 'Unexpected application filter.' }
            $name = $Matches[1]
            $values = @($global:bootstrapTestState.applications | Where-Object { $_.displayName -eq $name })
            if ($global:bootstrapTestState.scenario -eq 'foreign-page') { return @{ value = $values; '@odata.nextLink' = 'https://attacker.example/v1.0/applications' } }
            return @{ value = $values }
        }
        if ($path -eq 'servicePrincipals') {
            if ($filter -notmatch "^appId eq '([^']+)'$") { throw 'Unexpected principal filter.' }
            $appId = $Matches[1]
            return @{ value = @($global:bootstrapTestState.principals | Where-Object { $_.appId -eq $appId }) }
        }
        if ($path -match '^servicePrincipals/[^/]+/appRoleAssignments$') { return @{ value = @() } }
        if ($path -match '^applications/[^/]+/federatedIdentityCredentials$') { return @{ value = @($global:bootstrapTestState.federations.ToArray()) } }
        if ($path -eq 'oauth2PermissionGrants') {
            if ($filter -notmatch "^clientId eq '([^']+)' and resourceId eq '([^']+)'$") { throw 'Unexpected grant filter.' }
            $client = $Matches[1]
            $resource = $Matches[2]
            return @{ value = @($global:bootstrapTestState.grants | Where-Object { $_.clientId -eq $client -and $_.resourceId -eq $resource }) }
        }
        throw "Unexpected Graph GET path: $path"
    }
    $global:bootstrapTestState.writes.Add(@{ path = $path; method = $Method; payload = $payload })
    if ($Method -eq 'POST' -and $path -eq 'applications') {
        $application = @{
            id = [guid]::NewGuid().ToString(); appId = [guid]::NewGuid().ToString()
            api = @{ oauth2PermissionScopes = @(); preAuthorizedApplications = @(); knownClientApplications = @(); acceptMappedClaims = $false }
            passwordCredentials = @(); keyCredentials = @(); appRoles = @(); identifierUris = @()
            web = @{ redirectUris = @() }; publicClient = @{ redirectUris = @() }; spa = @{ redirectUris = @() }
            requiredResourceAccess = @(); isFallbackPublicClient = $false
        }
        foreach ($key in $payload.Keys) { $application[$key] = $payload[$key] }
        $global:bootstrapTestState.applications.Add($application)
        return $application
    }
    if ($Method -eq 'PATCH' -and $path -match '^applications/([^/]+)$') {
        $objectId = $Matches[1]
        $application = @($global:bootstrapTestState.applications | Where-Object { $_.id -eq $objectId })[0]
        foreach ($key in $payload.Keys) { $application[$key] = $payload[$key] }
        return $null
    }
    if ($Method -eq 'POST' -and $path -eq 'servicePrincipals') {
        $principal = @{ id = [guid]::NewGuid().ToString(); appId = $payload.appId; appOwnerOrganizationId = $tenantId }
        $global:bootstrapTestState.principals.Add($principal)
        return $principal
    }
    if ($Method -eq 'POST' -and $path -match '^applications/[^/]+/federatedIdentityCredentials$') {
        $global:bootstrapTestState.federations.Add($payload)
        return $payload
    }
    if ($Method -eq 'POST' -and $path -eq 'oauth2PermissionGrants') {
        if ($global:bootstrapTestState.scenario -eq 'deny-admin-consent') {
            $response = [System.Net.Http.HttpResponseMessage]::new([System.Net.HttpStatusCode]::Forbidden)
            throw [Microsoft.PowerShell.Commands.HttpResponseException]::new('Administrator consent is denied.', $response)
        }
        $global:bootstrapTestState.grants.Add($payload)
        return $payload
    }
    throw "Unexpected Graph write path: $path"
}

function Invoke-WebRequest { throw 'Network access is not permitted in this offline test.' }

$hadApproval = Test-Path Env:APP_ALLOW_AZURE_CHANGES
$oldApproval = [Environment]::GetEnvironmentVariable('APP_ALLOW_AZURE_CHANGES', 'Process')
try {
    $env:APP_ALLOW_AZURE_CHANGES = 'true'
    $bootstrap = Join-Path $PSScriptRoot '../bootstrap-identity.ps1'
    $parameters = @{
        TenantId = $tenantId; SubscriptionId = $subscriptionId; EnvironmentName = $environmentName
        WebOrigin = 'https://app.example.test'; OboManagedIdentityResourceId = $identityId
        Apply = $true; GrantAdminConsent = $false; Confirm = $false
    }
    $first = & $bootstrap @parameters | ConvertFrom-Json -AsHashtable
    if ($first.action -ne 'Configured' -or -not $first.liveValidationRequired) { throw 'Bootstrap output misrepresents readiness.' }
    if ($applications.Count -ne 2 -or $principals.Count -ne 3 -or $federations.Count -ne 1 -or $grants.Count -ne 0) { throw 'Unexpected object counts or implicit administrator consent.' }
    $parameters.GrantAdminConsent = $true
    $global:bootstrapTestState.scenario = 'deny-admin-consent'
    $consentDenied = $false
    try { & $bootstrap @parameters | Out-Null } catch {
        $consentDenied = $_.Exception.Message.Contains('Microsoft Graph POST /v1.0/oauth2PermissionGrants was denied (HTTP 403)')
    }
    if (-not $consentDenied -or $applications.Count -ne 2 -or $principals.Count -ne 3 -or
        $federations.Count -ne 1 -or $grants.Count -ne 0) {
        throw 'A denied consent operation must identify the stage and preserve completed setup without inventing grants.'
    }
    $global:bootstrapTestState.scenario = 'valid'
    & $bootstrap @parameters | Out-Null
    if ($grants.Count -ne 2) { throw 'Explicit administrator consent did not produce the expected grants.' }
    $creates = @($writes | Where-Object { $_.method -eq 'POST' }).Count
    $second = & $bootstrap @parameters | ConvertFrom-Json -AsHashtable
    if ($first.MEGHKOSHA_API_CLIENT_ID -ne $second.MEGHKOSHA_API_CLIENT_ID -or
        @($writes | Where-Object { $_.method -eq 'POST' }).Count -ne $creates) { throw 'Repeated bootstrap duplicated identities, trust or consent.' }
    if (@($grants | Where-Object { $_.consentType -ne 'AllPrincipals' -or $_.scope -notin @('user_impersonation', 'access_as_user') }).Count) {
        throw 'Unexpected consent scopes.'
    }
    foreach ($failure in @('wrong-tenant', 'foreign-page', 'wrong-owner', 'conflicting-federation')) {
        $global:bootstrapTestState.scenario = $failure
        $count = $writes.Count
        $originalTags = $applications[0].tags
        $originalSubject = $federations[0].subject
        if ($failure -eq 'wrong-owner') { $applications[0].tags = @('unrelated-environment') }
        if ($failure -eq 'conflicting-federation') { $federations[0].subject = '77777777-7777-7777-7777-777777777777' }
        $denied = $false
        try { & $bootstrap @parameters | Out-Null } catch { $denied = $true }
        $applications[0].tags = $originalTags
        $federations[0].subject = $originalSubject
        if (-not $denied -or $writes.Count -ne $count) { throw "Unsafe bootstrap failure: $failure" }
    }
    [ordered]@{ result = 'passed'; networkCalls = 0; registrations = 2; federations = 1; consentGrants = 2;
                idempotentCreates = $true; rejectedConflictsBeforeWrites = 4; consentFailureRecoverable = $true } | ConvertTo-Json -Compress
} finally {
    Remove-Variable -Name bootstrapTestState -Scope Global -ErrorAction SilentlyContinue
    if ($hadApproval) { $env:APP_ALLOW_AZURE_CHANGES = $oldApproval }
    else { Remove-Item Env:APP_ALLOW_AZURE_CHANGES -ErrorAction SilentlyContinue }
}