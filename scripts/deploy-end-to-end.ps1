<#
.SYNOPSIS
Deploy the manual-FOCUS PostgreSQL application through the existing private builder.
.DESCRIPTION
Use -PlanOnly to inspect without any Azure calls. No application sign-in registrations, target subscription
role assignments, native cost exports, or scheduled export processor are created.
The deployment operator still authenticates Azure CLI/AZD to provision hosting
resources. Configure the target and optional Reader SP in Admin after deployment.
.EXAMPLE
pwsh .\scripts\deploy-end-to-end.ps1 -EnvironmentName mk-manual -PlanOnly
.EXAMPLE
pwsh .\scripts\deploy-end-to-end.ps1 -EnvironmentName mk-manual -SubscriptionId '<hosting-subscription-uuid>' -ApproveAll
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[a-z0-9][a-z0-9-]{0,63}$')]
    [string] $EnvironmentName,
    [string] $SubscriptionId = '',
    [string] $Location = 'centralindia',
    [string] $FoundryLocation = 'eastus2',
    [ValidateSet('postgres')]
    [string] $AssessmentMode = 'postgres',
    [string] $RepoUrl = 'https://github.com/Saby007/MeghKoshaAI.git',
    [string] $RepoDirectory = '',
    [ValidatePattern('^([a-f0-9]{40})?$')]
    [string] $SourceCommit = '',
    [string] $BuilderVmSize = 'Standard_D2s_v5',
    [string] $BuilderSshPublicKeyPath = '',
    [ValidateRange(1, 10)]
    [int] $MaxAttempts = 3,
    [ValidateRange(0, 900)]
    [int] $SettleSeconds = 120,
    [switch] $PrivateRegistry,
    [switch] $BlobDataProtection,
    [switch] $SkipLogin,
    [switch] $ApproveAll,
    [switch] $ApproveApplicationUpdate,
    [switch] $PlanOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion -lt [version]'7.4') { throw 'PowerShell 7.4 or later is required.' }
$checkout = if ($RepoDirectory) { [IO.Path]::GetFullPath($RepoDirectory) } else { Split-Path $PSScriptRoot -Parent }
$runner = Join-Path $checkout 'scripts\deploy-staged.ps1'
if (-not (Test-Path -LiteralPath $runner)) { throw 'The manual-FOCUS staged runner is missing from this checkout.' }
$arguments = @{
    Stage = 'All'; EnvironmentName = $EnvironmentName; SubscriptionId = $SubscriptionId
    Location = $Location; FoundryLocation = $FoundryLocation; Repository = $RepoUrl; SourceCommit = $SourceCommit
    BuilderVmSize = $BuilderVmSize; BuilderSshPublicKeyPath = $BuilderSshPublicKeyPath
    MaxAttempts = $MaxAttempts; SettleSeconds = $SettleSeconds
    BlobDataProtection = $BlobDataProtection; SkipLogin = $SkipLogin; ApproveAll = $ApproveAll
    ApproveApplicationUpdate = $ApproveApplicationUpdate
    Apply = -not $PlanOnly; ApproveAzureChanges = -not $PlanOnly
    WhatIf = [bool]$WhatIfPreference
}
& $runner @arguments
