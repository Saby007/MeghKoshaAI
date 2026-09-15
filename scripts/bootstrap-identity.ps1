[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][guid] $TenantId,
    [Parameter(Mandatory)][guid] $SubscriptionId,
    [Parameter(Mandatory)][ValidatePattern('^[a-z0-9][a-z0-9-]{0,63}$')][string] $EnvironmentName,
    [Parameter(Mandatory)][uri] $WebOrigin,
    [Parameter(Mandatory)][string] $OboManagedIdentityResourceId,
    [switch] $IncludeLocalhostRedirects,
    [switch] $GrantAdminConsent,
    [switch] $Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion -lt [version]'7.0') { throw 'PowerShell 7 or later is required. Run this script with `pwsh`, not Windows PowerShell 5.1.' }
if ($TenantId -eq [guid]::Empty -or $SubscriptionId -eq [guid]::Empty) { throw 'Tenant and subscription IDs must be nonzero.' }
if (-not $WebOrigin.IsAbsoluteUri -or $WebOrigin.Scheme -ne 'https' -or $WebOrigin.Port -ne 443 -or
    $WebOrigin.AbsolutePath -ne '/' -or $WebOrigin.Query -or $WebOrigin.Fragment -or $WebOrigin.UserInfo) {
    throw 'WebOrigin must be an HTTPS origin without credentials, path, query or fragment.'
}
$identityPrefix = "/subscriptions/$SubscriptionId/resourceGroups/"
if (-not $OboManagedIdentityResourceId.StartsWith($identityPrefix, [StringComparison]::OrdinalIgnoreCase) -or
    $OboManagedIdentityResourceId -notmatch '^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft.ManagedIdentity/userAssignedIdentities/[^/]+$') {
    throw 'The OBO identity must be a user-assigned managed identity in the selected subscription.'
}
$origin = $WebOrigin.GetLeftPart([UriPartial]::Authority)
$callbacks = @("$origin/auth-callback.html")
if ($IncludeLocalhostRedirects) { $callbacks += @('http://localhost:5184/auth-callback.html', 'http://localhost:5185/auth-callback.html') }
$ownershipTag = "cost-assessment-app:$EnvironmentName"
$apiName = "$EnvironmentName-api"
$webName = "$EnvironmentName-web-spa"
$plan = [ordered]@{
    action = 'Preview'
    tenantId = $TenantId.ToString()
    subscriptionId = $SubscriptionId.ToString()
    environmentName = $EnvironmentName
    registrations = @($apiName, $webName)
    ownershipTag = $ownershipTag
    callbacks = $callbacks
    managedIdentityResourceId = $OboManagedIdentityResourceId
    delegatedPermissions = @('SPA -> API: access_as_user', 'API -> Azure Resource Manager: user_impersonation')
    managedIdentityAudience = 'api://AzureADTokenExchange'
    administratorConsentRequested = [bool]$GrantAdminConsent
    createsClientSecret = $false
    assignsSubscriptionRoles = $false
    liveValidationRequired = $true
}
if (-not $Apply -or $WhatIfPreference) {
    $plan | ConvertTo-Json -Depth 6
    return
}
if ($env:APP_ALLOW_AZURE_CHANGES -ne 'true') { throw 'Identity changes are not approved. Set APP_ALLOW_AZURE_CHANGES=true only after the required approval gates.' }
if (-not $PSCmdlet.ShouldProcess("$EnvironmentName in tenant $TenantId", 'Configure sign-in applications and UAMI federation')) { return }

function Invoke-AzureJson([string[]] $Arguments) {
    $output = & az @Arguments --only-show-errors --output json
    if ($LASTEXITCODE -ne 0) { throw 'Azure CLI prerequisite lookup failed. No fallback credentials will be created.' }
    return ($output -join "`n" | ConvertFrom-Json -AsHashtable)
}

function Invoke-IdentityGraph([string] $Path, [string] $Method = 'GET', [object] $Body = $null) {
    $uri = [uri]"https://graph.microsoft.com/v1.0/$Path"
    if ($uri.Host -ne 'graph.microsoft.com' -or $uri.Scheme -ne 'https' -or $uri.UserInfo -or $uri.Fragment -or
        -not $uri.AbsolutePath.StartsWith('/v1.0/', [StringComparison]::Ordinal)) { throw 'Invalid Graph request path.' }
    $parameters = @{
        Uri = $uri
        Method = $Method
        Headers = $graphHeaders
        ContentType = 'application/json'
        MaximumRedirection = 0
        TimeoutSec = 30
    }
    if ($null -ne $Body) { $parameters.Body = $Body | ConvertTo-Json -Depth 20 -Compress }
    try {
        $response = Invoke-RestMethod @parameters
    } catch [Microsoft.PowerShell.Commands.HttpResponseException] {
        if ($_.Exception.Response.StatusCode -eq [System.Net.HttpStatusCode]::Forbidden) {
            throw "Microsoft Graph $Method $($uri.AbsolutePath) was denied (HTTP 403). Completed setup is retained; check Entra permissions and rerun with an authorized administrator. No client-secret fallback was created."
        }
        throw
    }
    if ($null -ne $response) { return ($response | ConvertTo-Json -Depth 30 | ConvertFrom-Json -AsHashtable) }
}

function Get-GraphCollection([string] $Path) {
    $collectionPath = ([uri]"https://graph.microsoft.com/v1.0/$Path").AbsolutePath
    $visited = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    $items = [System.Collections.Generic.List[object]]::new()
    while ($Path) {
        if ($visited.Count -ge 20 -or -not $visited.Add($Path)) { throw 'Graph pagination limit or repeated page.' }
        $page = Invoke-IdentityGraph $Path
        if ($page['value'] -isnot [System.Collections.IList]) { throw 'Graph returned an invalid collection.' }
        foreach ($item in $page['value']) { $items.Add($item) }
        $next = $page['@odata.nextLink']
        if (-not $next) { break }
        $link = [uri]$next
        if (-not $link.IsAbsoluteUri -or $link.Scheme -ne 'https' -or $link.Authority -ne 'graph.microsoft.com' -or
            $link.AbsolutePath -ne $collectionPath -or $link.Fragment -or $link.UserInfo) { throw 'Graph pagination left the requested collection.' }
        $Path = $link.PathAndQuery.Substring('/v1.0/'.Length)
    }
    return $items.ToArray()
}

function Find-Application([string] $Name) {
    $filter = [uri]::EscapeDataString("displayName eq '$Name'")
    $matches = @(Get-GraphCollection "applications?`$filter=$filter")
    if ($matches.Count -gt 1) { throw 'Duplicate application display names require operator review.' }
    if ($matches.Count) {
        $application = $matches[0]
        if ($application.tags -notcontains $ownershipTag -or $application.signInAudience -ne 'AzureADMyOrg') {
            throw 'Existing application is not owned by this deployment environment; no adoption or overwrite is allowed.'
        }
        if (@($application.passwordCredentials | Where-Object { $null -ne $_ }).Count -or
            @($application.keyCredentials | Where-Object { $null -ne $_ }).Count -or
            @($application.appRoles | Where-Object { $null -ne $_ }).Count -or $application.isFallbackPublicClient) {
            throw 'Existing application has unexpected credentials, roles or public-client configuration.'
        }
        if (@($application.web.redirectUris | Where-Object { $_ }).Count -or
            @($application.publicClient.redirectUris | Where-Object { $_ }).Count) {
            throw 'Existing application has unexpected web or public-client redirect URIs.'
        }
        return $application
    }
    return $null
}

function Assert-ResourceAccess([object] $Application, [string] $ResourceAppId, [string] $ScopeId) {
    foreach ($resource in @($Application.requiredResourceAccess | Where-Object { $null -ne $_ })) {
        if ($resource.resourceAppId -ne $ResourceAppId -or
            @($resource.resourceAccess | Where-Object { $_.type -ne 'Scope' -or $_.id -ne $ScopeId }).Count) {
            throw 'Existing application requests unexpected permissions; nothing will be removed or expanded automatically.'
        }
    }
}

function Assert-Federation([object[]] $Credentials, [System.Collections.IDictionary] $Expected) {
    if ($Credentials.Count -gt 1 -or ($Credentials.Count -and ($Credentials[0].name -ne $Expected.name -or
        $Credentials[0].issuer -cne $Expected.issuer -or $Credentials[0].subject -cne $Expected.subject -or
        @($Credentials[0].audiences).Count -ne 1 -or $Credentials[0].audiences[0] -cne $Expected.audiences[0]))) {
        throw 'Existing federation differs from the approved OBO identity. No trust relationship was replaced.'
    }
}

function Ensure-ServicePrincipal([string] $AppId) {
    $filter = [uri]::EscapeDataString("appId eq '$AppId'")
    $principals = @(Get-GraphCollection "servicePrincipals?`$filter=$filter")
    if ($principals.Count -gt 1) { throw 'Duplicate service principals require operator review.' }
    if ($principals.Count -eq 0) { return (Invoke-IdentityGraph 'servicePrincipals' 'POST' @{ appId = $AppId }) }
    if ($principals[0].appOwnerOrganizationId -ne $TenantId.ToString()) { throw 'The sign-in service principal belongs to another tenant.' }
    if (@(Get-GraphCollection "servicePrincipals/$($principals[0].id)/appRoleAssignments").Count) {
        throw 'Application-only permission assignments are not expected on the application sign-in service principal.'
    }
    return $principals[0]
}

function Ensure-DelegatedConsent([string] $ClientObjectId, [string] $ResourceObjectId, [string] $Scope) {
    $filter = [uri]::EscapeDataString("clientId eq '$ClientObjectId' and resourceId eq '$ResourceObjectId'")
    $grants = @(Get-GraphCollection "oauth2PermissionGrants?`$filter=$filter" | Where-Object { $_.consentType -eq 'AllPrincipals' })
    if ($grants.Count -gt 1) { throw 'Conflicting administrator consent grants require operator review.' }
    if ($grants.Count -eq 1) {
        $scopes = @($grants[0].scope -split ' ' | Where-Object { $_ })
        if ($scopes.Count -ne 1 -or $scopes[0] -ne $Scope) { throw 'Unexpected existing delegated consent requires operator review.' }
        return
    }
    Invoke-IdentityGraph 'oauth2PermissionGrants' 'POST' @{
        clientId = $ClientObjectId
        resourceId = $ResourceObjectId
        consentType = 'AllPrincipals'
        principalId = $null
        scope = $Scope
    } | Out-Null
}

$graphHeaders = $null
$graphToken = $null
try {
    $account = Invoke-AzureJson @('account', 'show', '--subscription', $SubscriptionId.ToString())
    if ($account.tenantId -ne $TenantId.ToString() -or $account.state -ne 'Enabled' -or $account.environmentName -ne 'AzureCloud') {
        throw 'The selected subscription must be enabled in the approved tenant and Azure public cloud.'
    }
    $identity = Invoke-AzureJson @('identity', 'show', '--ids', $OboManagedIdentityResourceId, '--subscription', $SubscriptionId.ToString())
    if ($identity.tenantId -ne $TenantId.ToString() -or $identity.tags['azd-env-name'] -ne $EnvironmentName -or
        $identity.tags['app-name'] -ne 'cost-assessment' -or -not $identity.principalId -or -not $identity.clientId) {
        throw 'The OBO identity must belong to the approved deployment environment and tenant.'
    }
    $graphToken = Invoke-AzureJson @('account', 'get-access-token', '--resource', 'https://graph.microsoft.com',
                                   '--tenant', $TenantId.ToString())
    if (-not $graphToken.accessToken) { throw 'Microsoft Graph authentication is unavailable.' }
    $graphHeaders = @{ Authorization = "Bearer $($graphToken.accessToken)" }
    $api = Find-Application $apiName
    $web = Find-Application $webName
    $federation = @{
        name = 'obo-managed-identity'
        issuer = "https://login.microsoftonline.com/$TenantId/v2.0"
        subject = $identity.principalId
        audiences = @('api://AzureADTokenExchange')
    }
    $armAppId = '797f4846-ba00-4fd7-ba43-dac1f8f63013'
    $armFilter = [uri]::EscapeDataString("appId eq '$armAppId'")
    $armPrincipals = @(Get-GraphCollection "servicePrincipals?`$filter=$armFilter")
    if ($armPrincipals.Count -ne 1) { throw 'Azure Resource Manager service principal could not be uniquely resolved.' }
    $armScope = @($armPrincipals[0].oauth2PermissionScopes | Where-Object { $_.value -eq 'user_impersonation' -and $_.isEnabled })
    if ($armScope.Count -ne 1) { throw 'The ARM delegated user_impersonation scope is unavailable.' }
    if ($api) {
        Assert-ResourceAccess $api $armAppId $armScope[0].id
        Assert-Federation @(Get-GraphCollection "applications/$($api.id)/federatedIdentityCredentials") $federation
        if (@($api.api.oauth2PermissionScopes | Where-Object { $_.value -ne 'access_as_user' -or -not $_.isEnabled }).Count -or
            @($api.spa.redirectUris | Where-Object { $_ }).Count -or $api.api.acceptMappedClaims) {
            throw 'The API registration contains unexpected scopes or identity configuration.'
        }
        if (@($api.identifierUris | Where-Object { $_ -ne "api://$($api.appId)" }).Count -or
            @($api.api.knownClientApplications | Where-Object { -not $web -or $_ -ne $web.appId }).Count) {
            throw 'The API registration has unexpected identifiers or known clients.'
        }
    }
    if ($web) {
        if (-not $api) { throw 'Existing SPA without its API registration requires operator review.' }
        if (@($web.spa.redirectUris | Where-Object { $_ -notin $callbacks }).Count) { throw 'Unexpected SPA callbacks require explicit migration, not silent replacement.' }
    }
    if (-not $api) {
        $api = Invoke-IdentityGraph 'applications' 'POST' @{
            displayName = $apiName; signInAudience = 'AzureADMyOrg'; tags = @($ownershipTag); isFallbackPublicClient = $false
        }
    }
    $scopes = @($api.api.oauth2PermissionScopes | Where-Object { $_.value -eq 'access_as_user' })
    if ($scopes.Count -gt 1) { throw 'Duplicate API scopes require operator review.' }
    $scopeId = if ($scopes.Count) { $scopes[0].id } else { [guid]::NewGuid().ToString() }
    if ($web) { Assert-ResourceAccess $web $api.appId $scopeId }
    $apiConfiguration = @{
        requestedAccessTokenVersion = 2
        acceptMappedClaims = $false
        oauth2PermissionScopes = @(@{
            id = $scopeId; value = 'access_as_user'; type = 'Admin'; isEnabled = $true
            adminConsentDisplayName = 'Access the application as the signed-in user'
            adminConsentDescription = 'Use the application API; Azure subscription access remains independently enforced.'
        })
    }
    if ($api.api.preAuthorizedApplications -and (-not $web -or
        @($api.api.preAuthorizedApplications | Where-Object { $_.appId -ne $web.appId -or @($_.delegatedPermissionIds | Where-Object { $_ -ne $scopeId }).Count }).Count)) {
        throw 'Unexpected preauthorized clients require operator review.'
    }
    Invoke-IdentityGraph "applications/$($api.id)" 'PATCH' @{
        api = $apiConfiguration
        identifierUris = @("api://$($api.appId)")
        optionalClaims = @{ accessToken = @(@{ name = 'xms_cc'; source = $null; essential = $false; additionalProperties = @() }) }
        requiredResourceAccess = @(@{ resourceAppId = $armAppId; resourceAccess = @(@{ id = $armScope[0].id; type = 'Scope' }) })
    } | Out-Null
    $webConfiguration = @{
        spa = @{ redirectUris = $callbacks }
        requiredResourceAccess = @(@{ resourceAppId = $api.appId; resourceAccess = @(@{ id = $scopeId; type = 'Scope' }) })
    }
    if (-not $web) {
        $webConfiguration.displayName = $webName
        $webConfiguration.signInAudience = 'AzureADMyOrg'
        $webConfiguration.tags = @($ownershipTag)
        $webConfiguration.isFallbackPublicClient = $false
        $web = Invoke-IdentityGraph 'applications' 'POST' $webConfiguration
    } else {
        Invoke-IdentityGraph "applications/$($web.id)" 'PATCH' $webConfiguration | Out-Null
    }
    $apiConfiguration.preAuthorizedApplications = @(@{ appId = $web.appId; delegatedPermissionIds = @($scopeId) })
    $apiConfiguration.knownClientApplications = @($web.appId)
    Invoke-IdentityGraph "applications/$($api.id)" 'PATCH' @{ api = $apiConfiguration } | Out-Null
    $apiPrincipal = Ensure-ServicePrincipal $api.appId
    $webPrincipal = Ensure-ServicePrincipal $web.appId

    $credentials = @(Get-GraphCollection "applications/$($api.id)/federatedIdentityCredentials")
    Assert-Federation $credentials $federation
    if (-not $credentials.Count) { Invoke-IdentityGraph "applications/$($api.id)/federatedIdentityCredentials" 'POST' $federation | Out-Null }
    if ($GrantAdminConsent) {
        Ensure-DelegatedConsent $apiPrincipal.id $armPrincipals[0].id 'user_impersonation'
        Ensure-DelegatedConsent $webPrincipal.id $apiPrincipal.id 'access_as_user'
    }
    [ordered]@{
        action = 'Configured'
        AZURE_TENANT_ID = $TenantId.ToString()
        MEGHKOSHA_API_CLIENT_ID = $api.appId
        MEGHKOSHA_WEB_CLIENT_ID = $web.appId
        MEGHKOSHA_OBO_MANAGED_IDENTITY_CLIENT_ID = $identity.clientId
        apiApplicationObjectId = $api.id
        webApplicationObjectId = $web.id
        administratorConsentRequested = [bool]$GrantAdminConsent
        liveValidationRequired = $true
    } | ConvertTo-Json -Depth 5
} finally {
    $graphHeaders = $null
    $graphToken = $null
}