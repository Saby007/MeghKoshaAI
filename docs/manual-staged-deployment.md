# One-command deployment with a private build VM

Use the published `feature/private-postgres-assessments` branch. This path prepares
private ACR, private PostgreSQL and a private build VM in Central India, then builds
images through the VM's managed identity. **You run one command; the script runs
the stages in order.** Nothing in this guide authorizes an assistant-run deployment.
The same rollout includes the existing **Model Router-backed chatbot**, with private
Foundry resources in East US 2 by default; `-FoundryLocation` selects the Foundry
region separately from the Central India application/database region.

## Recommended: run the existing end-to-end entry point

From the reviewed feature-branch checkout, use PowerShell 7.4+:

```powershell
git pull --ff-only
.\scripts\deploy-end-to-end.ps1 `
    -EnvironmentName meghkoshaai-scalable-dev `
    -SubscriptionId '<hosting-subscription-uuid>' `
    -FoundryLocation eastus2 `
    -Location centralindia
```

Replace the subscription placeholder. If omitted, the script asks for it (or uses
the selected environment's existing subscription). By default it grants access to
that hosting subscription only. To assess other approved subscriptions, add
`-TargetSubscriptionId '<subscription-a>,<subscription-b>'`.

The script handles:

1. Existing Azure/AZD sign-in or an interactive sign-in when required.
2. One approval showing the hosting scope, target subscriptions, private
   infrastructure/NAT costs, image publication, app registrations and role grants.
3. A dedicated local SSH key under ignored `.azure\builder-keys` when needed.
4. Private foundation, Foundry/Model Router and builder VM provisioning, then private image build/push.
5. Database bootstrap, sign-in registration, target grants, immutable-image release
   and verification of the deployed images, private AI configuration and readiness.
6. Builder VM deallocation after success or failure.

The default PostgreSQL flow includes Blob Storage's trusted-service ingress exception
for native exports, as the legacy end-to-end flow did. It keeps Storage default-deny;
ACR and PostgreSQL public access stay disabled. This exception is displayed in the
upfront approval. If that exception is not approved by your policy, use the individual
stages with your separately approved native-export configuration; do not bypass policy.

No separate SSH, login, build, bootstrap or release commands are required for a normal
run. A tenant-mandated Service Tree ID is requested only if needed; you can supply
`-ServiceManagementReference '<uuid>'` upfront. `-SkipLogin` reuses existing verified
sign-in. For a reviewed unattended run, `-ApproveAll` supplies the upfront approval
explicitly; without it the script requires you to type `YES`.

The one-command flow sets the `ai` profile and `APP_ENABLE_CHAT_RUNTIME=true`,
deploying the approved `model-router` version/SKU/capacity from the existing
application contract. It checks the Foundry region/model catalog before provisioning;
quota and policy failures still stop the run. Foundry token usage adds cost.
The separate hosted-agent narrator remains opt-in: the chatbot does not require
creating or enabling a hosted/prompt narrator agent.

Chat is bound to the selected completed PostgreSQL snapshot. The worker stores a
bounded evidence record with complete financial totals and exact top-resource
rankings; at most 25 resource details are displayed. Chat never downloads the full
Blob report or asks a model to generate SQL. Model calls use the API managed identity
through private Foundry networking. A model outage returns a clearly labeled
deterministic answer without inventing costs.

After upgrading an existing schema, bootstrap must add the new Chat evidence column,
and **Update report** must create a new completed assessment. Older snapshots with
no Chat evidence return an explicit rebuild message; they do not fall back to loading
the whole report. An already released data-only environment requires a reviewed
AI/schema/image migration, not an automatic incompatible upgrade.

Use the same command after resolving a failure. Verified foundation, exact-commit
images and successful bootstrap are reused; the same completed release is reverified.
The script never proceeds past a failed stage. Cleanup errors are reported explicitly.
It does not automatically migrate an already released environment to a different
source/schema commit; review that update through individual stages instead.

VM quota checks validate numeric usage and limits even when Azure CLI serializes
them as strings. A genuine shortage reports the quota name, region, used, limit,
available and additional vCPUs. Invalid/missing quota responses stop explicitly;
they are not reported as a confirmed quota shortage. Pull the latest feature
branch before retrying an older generic `VM quota cores is unavailable or insufficient`
error; do not request an increase solely from that old message.

To see the complete plan without sign-in, Azure calls or resource changes:

```powershell
.\scripts\deploy-end-to-end.ps1 `
    -EnvironmentName meghkoshaai-scalable-dev `
    -SubscriptionId '<hosting-subscription-uuid>' `
    -PlanOnly
```

The default uses the private builder stages internally, not `azd up` or `azd deploy`.
`-AssessmentMode blob` retains the separate legacy compatibility workflow. No
unsupported skip-stage or administrator-consent flags are silently accepted in
the PostgreSQL end-to-end flow.

## Requirements, cost and boundaries

- PowerShell 7.4+, Git, Azure CLI with Container Apps support, AZD 1.35+, and an
  OpenSSH public key. No Docker or VPN is needed on your management laptop.
- An approved **new parallel** environment, default `meghkoshaai-scalable-dev`.
  The existing `meghkoshaai-dev` layout hold is not bypassed.
- Hosting permissions to create resources/assign scoped roles and run Azure VM
  commands. Entra app registration and target-subscription grants require their
  own permissions. The one-command upfront approval covers all displayed stages;
  individual-stage execution retains separate approval switches. No client secrets are created.
- The private builder defaults to `Standard_D2s_v5`, pinned regional Ubuntu 24.04,
  a 128-GB disk, Trusted Launch and SSH-key-only authentication. It has **no public
  IP and no inbound access**. Management uses Azure Run Command, not SSH.
- A dedicated builder-subnet NAT gateway/public egress IP provides explicit outbound
  connectivity to GitHub, signed package feeds and pinned base-image registries.
  The public IP belongs to **NAT**, not the VM; it does not expose inbound access.
  The NAT gateway, public IP, disk, ACR Premium, PostgreSQL and private endpoints
  all add ongoing cost. `StopBuilder` deallocates VM compute only.
- Azure Policy must permit this reviewed topology. If public NAT egress is not
  permitted, use an approved existing private build host/firewall path instead;
  see [the existing-builder guide](manual-scalable-deployment.md).
- Source must be a **public** GitHub repository and an exact published commit.
  The script never copies your GitHub tokens, Azure CLI cache or Docker login state
  to the VM. Do not use the builder for untrusted PRs or arbitrary source.
- VM SKU/image/quota checks run in **your selected subscription** before provisioning.
  Availability was not checked against a live subscription during code preparation.
  PostgreSQL/ACA quota, policy, identity, network and live workload acceptance
  remain deployment-time checks. Local tests do not guarantee Azure success.

## Advanced: individual stages

The remaining commands are optional controls for debugging or separately governed
changes. They are **not required** when using the end-to-end command above.

## 1. Clone the feature branch and sign in

Run these commands in PowerShell. Enter your own tenant and subscription UUIDs:

```powershell
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
git clone --branch feature/private-postgres-assessments --single-branch https://github.com/Saby007/MeghKoshaAI.git MeghKoshaAI-scalable-dev
Set-Location .\MeghKoshaAI-scalable-dev
git branch --show-current
git rev-parse HEAD

$TenantId = Read-Host 'Hosting tenant UUID'
$SubscriptionId = Read-Host 'Hosting subscription UUID'
az login --tenant $TenantId
azd auth login
az account show --subscription $SubscriptionId --query '{subscription:id,tenant:tenantId,name:name}' --output table
```

Use an existing OpenSSH **public** `.pub` file. If you need a dedicated key:

```powershell
$Key = Join-Path $HOME '.ssh\meghkosha-builder'
if (Test-Path -LiteralPath $Key) { throw 'Key already exists. Use the existing .pub file; do not overwrite it.' }
New-Item -ItemType Directory -Force -Path (Split-Path $Key -Parent) | Out-Null
ssh-keygen -t ed25519 -N '' -f $Key
$PublicKey = "$Key.pub"
```

Only the public key enters the VM template. The private key remains on your machine;
the script does not use it to connect to the builder.

## 2. Review the plan; initialize the local environment

For the advanced stage runner, without `-Apply` every stage is **plan-only** and makes no Azure calls:

```powershell
$EnvName = 'meghkoshaai-scalable-dev'
.\scripts\deploy-staged.ps1 -Stage Foundation -EnvironmentName $EnvName
```

Initialize checks the selected subscription and regional VM SKU/image/quota, then
stores local AZD settings. It does not provision resources:

```powershell
.\scripts\deploy-staged.ps1 -Stage Initialize -EnvironmentName $EnvName `
    -SubscriptionId $SubscriptionId -Location centralindia `
    -FoundryLocation eastus2 `
    -BuilderSshPublicKeyPath $PublicKey -Apply -ApproveAzureChanges
```

If native FOCUS exports require the trusted-service ingress exception on Blob
Storage, review that separate policy/network change and pass
`-AllowNativeExportTrustedServices` to **Initialize**, or set
`APP_EXPORT_TRUSTED_SERVICES=true` explicitly before Foundation. This leaves storage
default-deny with the trusted-service bypass; it never enables public ACR or
PostgreSQL access. Do not bypass an organizational policy denial.

## 3. Provision the private foundation and builder

First run the real Azure preview without provisioning:

```powershell
.\scripts\deploy-staged.ps1 -Stage Foundation -EnvironmentName $EnvName `
    -Apply -ApproveAzureChanges -PreviewOnly
```

Review that output and costs. The approved stage repeats the preview and then
provisions only because you explicitly pass the cost/change approvals:

```powershell
.\scripts\deploy-staged.ps1 -Stage Foundation -EnvironmentName $EnvName `
    -Apply -ApproveAzureChanges -ApproveCosts
```

Use `-PreviewOnly` for a separate approval window. The
`-Apply` invocation executes the preview and then the approved stage; it is not
a dry run. Application images remain unset, so no application release occurs here.

## 4. Build and push private images

Start from the reviewed clean Git checkout. This invokes Azure Run Command on the
private builder, installs signed packages, logs in with the builder MI, fetches the
exact public Git commit, builds Linux/amd64 API/web images, and pushes through ACR's
private endpoint:

```powershell
.\scripts\deploy-staged.ps1 -Stage Build -EnvironmentName $EnvName `
    -Apply -ApproveAzureChanges -ApproveImagePublication
```

The script requires a nonce/commit/registry/digest attestation from this execution.
Missing or mismatched output is a failure, never an assumed successful build.
Build output is limited by Azure Run Command; failures report a bounded log tail.
Error reporting retains the original stderr descriptor, so a failed redirected
build step cannot hide its diagnostics in the temporary log that cleanup removes.
The launcher executes the build from its script file with stdin disconnected, so
dependency tools cannot consume later script lines or the success attestation.
Build phases are identified in `MEGHKOSHA_BUILD_ERROR` messages. The complete bounded
Run Command response is retained locally under
`.azure\<environment>\private-build-<nonce>.result.json`; the error includes that
path and the available stdout/stderr. These diagnostics are local deployment state,
not source files to commit or publish. A CLI-level success with empty guest output
is still rejected, as is a provider-reported failure even if a marker is present.
An interrupted/timeout build must be inspected before retrying; Run Command supports
only one concurrent invocation per VM and a maximum 90-minute execution.

The build keeps managed-identity/Docker state private with `umask 077`, but explicitly
sets the public APT signing key and repository list to mode `0644`. APT's `_apt`
sandbox cannot read a root-only key; that appears as `NO_PUBKEY` even when the key
is present. The recipe repairs these two existing public files before its first
package update, so a failed older build can be resumed without opening network
access or disabling signature verification.

## 5. Bootstrap the private database

This stage provisions the **manual** bootstrap job with the reviewed API digest,
runs it inside ACA, and waits for that exact execution to succeed:

```powershell
.\scripts\deploy-staged.ps1 -Stage Bootstrap -EnvironmentName $EnvName `
    -Apply -ApproveAzureChanges -ApproveDatabaseBootstrap
```

The bootstrap administrator identity is never attached to the API/worker. SQL schema,
indexes, extension allowlist and non-admin runtime grants must succeed before release.
No database public firewall exception is required.
Principal discovery uses Azure PostgreSQL's returned `rolname` column. A rerun
reuses matching non-admin runtime principals, but refuses an existing role bound
to a different identity, principal type or administrator privilege.

## 6. Configure sign-in and subscription grants

This calls the existing reviewed identity helper. Add
`-ServiceManagementReference '<service-tree-uuid>'` if your tenant requires it:

```powershell
.\scripts\deploy-staged.ps1 -Stage Identity -EnvironmentName $EnvName `
    -Apply -ApproveAzureChanges -ApproveIdentityChanges

$Targets = (Read-Host 'Approved assessed subscription UUIDs, comma-separated').Split(',') |
    ForEach-Object { [guid]$_.Trim() }
.\scripts\deploy-staged.ps1 -Stage Permissions -EnvironmentName $EnvName `
    -TargetSubscriptionId $Targets -Apply -ApproveAzureChanges -ApproveRoleAssignments
```

The shared assessment/export worker and API receive Reader and Cost Management
Contributor on those explicitly selected targets. Review those scopes before
running. The script cannot grant permissions your operator identity does not hold.
Allow RBAC propagation; app sign-in/consent policies still apply.

## 7. Release and verify

Release refuses unverified bootstrap, mismatched image digests, absent sign-in
registrations, or missing target-subscription attestation:

```powershell
.\scripts\deploy-staged.ps1 -Stage Release -EnvironmentName $EnvName `
    -Apply -ApproveAzureChanges -PreviewOnly
# Review the preview before running the approved release.
.\scripts\deploy-staged.ps1 -Stage Release -EnvironmentName $EnvName `
    -Apply -ApproveAzureChanges -ApproveRelease

.\scripts\deploy-staged.ps1 -Stage Verify -EnvironmentName $EnvName `
    -Apply -ApproveAzureChanges
```

Release uses the existing Bicep provisioning flow with immutable API/web/worker
digests; it does **not** call `azd deploy`, enable public ACR, or substitute a
placeholder image. Verify checks provisioned/running apps and health/readiness.

Sign in at the printed web origin, configure/complete native FOCUS exports, queue
one assessment and verify the exact authorized report scope. Open **Chat** without
opening classic tabs and check a response labeled **Model Router** (not only the
verified fallback). Runtime private connectivity, Entra/RBAC propagation, model
quota and token usage must be verified by the operator. Run the representative
retained-history/concurrent-read and restart checks from the
[manual rollout guide](manual-scalable-deployment.md#9-verify-before-accepting-the-release).
These user-level/load checks are not replaced by a healthy readiness endpoint.

## 8. Deallocate the builder

After image publication/release verification:

```powershell
.\scripts\deploy-staged.ps1 -Stage StopBuilder -EnvironmentName $EnvName `
    -Apply -ApproveAzureChanges
```

This stops VM compute billing, **not** disk/NAT/public IP/registry/database charges.
No resource is automatically deleted. A later Build explicitly starts the VM again.
Review all compatible image digests and schema changes before any rollback; do not
automatically switch an existing PostgreSQL deployment to Blob mode.
