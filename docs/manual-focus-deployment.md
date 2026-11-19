# Deploying manual FOCUS workspaces

This guide is for `feature/manual-focus-service-principal`. Use a new environment.
Do not copy an existing deployment's `.azure` environment, identities or secrets.
The application has no user login. The operator still authenticates Azure CLI/AZD
to provision the hosting resources.

## Prerequisites

- PowerShell 7.4+, Git, authenticated GitHub CLI, Azure CLI with Container Apps
  support, AZD 1.35+, and OpenSSH `ssh-keygen`.
- Hosting-subscription permissions to provision the resources and their
  hosting-scoped managed-identity RBAC. No target-subscription role assignment is
  performed by these scripts.
- Registered providers for Container Apps, Container Registry, PostgreSQL, Storage,
  Key Vault, Networking, Compute, Managed Identity and monitoring; Cognitive
  Services for the standard chat-enabled rollout. `Microsoft.CostManagementExports`
  registration is no longer a prerequisite.
- Approved address space/private DNS, outbound feeds for the private builder,
  PostgreSQL/ACA/VM quota, allowed regions/SKUs, and private endpoint policies.
- For the standard staged rollout, the exact approved private Model Router
  deployment must be available in the selected Foundry region. Chat remains
  separate from the optional target service principal.
- Review ongoing PostgreSQL D4/256 GiB, ACR Premium, private endpoint, Key Vault,
  container, builder disk/NAT and optional model usage costs.

No user-sign-in app registrations, SPA/API client IDs, admin consent or delegated
subscription permissions are required. No SP secret is a deployment parameter.

## 1. Get the correct branch

```powershell
git clone --branch feature/manual-focus-service-principal --single-branch `
    https://github.com/Saby007/MeghKoshaAI.git MeghKoshaAI-upload
Set-Location .\MeghKoshaAI-upload
git rev-parse HEAD
git status --short
```

Use the reviewed published commit and a clean checkout. The unchanged private
builder accepts public source at an exact commit; GitHub credentials stay on the
management computer, not on the VM.

## 2. Inspect the plan without Azure calls

```powershell
$EnvironmentName = 'meghkoshaai-manual'
$HostingSubscriptionId = '<hosting-subscription-uuid>'
$HostingTenantId = '<hosting-tenant-uuid>'

.\scripts\deploy-end-to-end.ps1 `
    -EnvironmentName $EnvironmentName `
    -SubscriptionId $HostingSubscriptionId `
    -Location 'centralindia' `
    -FoundryLocation 'eastus2' `
    -PlanOnly
```

Expected stages: Initialize, Foundation, Build, Bootstrap, Release, Verify,
StopBuilder. The plan reports manual uploads, optional Reader SP, no application
login and a private workspace vault. It does not create resources.

## 3. Authenticate the deployment operator and approve rollout

```powershell
gh auth login --hostname github.com --web
az login --tenant $HostingTenantId
az account set --subscription $HostingSubscriptionId
azd auth login --tenant-id $HostingTenantId

.\scripts\deploy-end-to-end.ps1 `
    -EnvironmentName $EnvironmentName `
    -SubscriptionId $HostingSubscriptionId `
    -Location 'centralindia' `
    -FoundryLocation 'eastus2' `
    -ApproveAll
```

This command **does provision resources and incur costs**. Omit `-ApproveAll` for
the runner's upfront approval prompt. `-SkipLogin` only skips operator CLI sign-in
checks; it has nothing to do with application login.

The runner provisions the private foundation, builds immutable images through the
existing private VM, runs the dedicated PostgreSQL schema/role bootstrap, releases
API/web/worker, verifies readiness/private dependencies and deallocates the builder.
It never starts the native export scheduler or grants access to target subscriptions.
Builder disks, NAT, registry and database costs remain after deallocation.

### Individual stages / recovery

Use `scripts/deploy-staged.ps1` with `-Stage` and `-EnvironmentName`. Without `-Apply`,
it is plan-only. Executing a stage requires `-Apply -ApproveAzureChanges`, plus:

| Stage | Additional approval |
| --- | --- |
| Foundation | `-ApproveCosts` |
| Build | `-ApproveImagePublication` |
| Bootstrap | `-ApproveDatabaseBootstrap` |
| Release | `-ApproveRelease` |

Foundation, Bootstrap and Release support `-PreviewOnly`. Resume the same reviewed
commit after fixing a reported error; verified image/bootstrap checkpoints are
retained. A changed released source commit requires the explicitly reviewed
individual-stage upgrade path, not an implicit all-stage migration.

Identity and Permissions are no longer valid stages.

## 4. Configure the application

1. Open the reported HTTPS URL; no account or sign-in is required.
2. In Admin, enter one target subscription UUID and save.
3. Upload all desired native FOCUS CSV/gzip partitions and generate the dashboard.
4. Optionally add the target tenant ID, client ID and secret of a service principal
   the customer has already provisioned with **Reader only**.

The SP is used only for target Azure reads. Backend managed identities access the
private database, storage, vault, registry and optional Foundry. The API stores a
secret in the private vault; SQL stores its exact version reference, never its
value. A browser never receives an existing secret.

Without SP credentials the financial dashboard works and live Azure sections
remain unavailable. Denied/expired optional enrichment is reported explicitly and
does not replace costs with zeros or use another identity.

Each browser has a private anonymous workspace. A replacement report replaces
that workspace's active dataset, not another visitor's. Upload-only mode does not
verify that the visitor has Azure RBAC: the evidence is the report they supplied.

## Sizing and ingestion limits

Defaults are API 2 vCPU/4 GiB, worker 4 vCPU/8 GiB, PostgreSQL
`Standard_D4ds_v5`/256 GiB. Parameters include `APP_API_CPU`,
`APP_ASSESSMENT_WORKER_CPU`, `APP_ASSESSMENT_WORKER_REPLICAS`,
`APP_POSTGRES_SKU`, `APP_POSTGRES_STORAGE_GB`, `APP_ASSESSMENT_POOL_SIZE` and
`APP_ASSESSMENT_LIMITS`.

The database command deadline defaults to 60 seconds
(`APP_ASSESSMENT_LIMITS.dbCommandSeconds`, range 5-300), with a 120-second nginx
read deadline. The previous 15-second database deadline was too short for the
measured cold full-history projections in the local 7.24-million-point check.
Review actual Azure latency; this is a bounded deadline, not a latency guarantee.

These settings are persisted in the selected AZD environment. Review them before
provision/preview; initialized environments preserve explicit existing values.
Do not increase worker replicas without reviewing database pools and Azure API
throttling. Full queues and exhausted capacity return explicit errors.

There is no six-month business restriction. Input/decompression/row/disk/runtime
and response budgets remain necessary. Gzip is recommended for large native CSV
exports. For example, 40,000 resources across January-June 2026 already imply
7.24 million daily points before extra services, tags or SKU rows.

Workspaces expire after seven days by default; `APP_WORKSPACE_TTL_DAYS` supports
1-90. This limits browser access, not archival retention. Establish approved
database/Blob/Key Vault retention and cleanup separately. Public upload endpoints
also require an operational abuse/cost boundary appropriate to the audience.

## Existing environments

This branch changes the authorization/data namespace and does not migrate shared
Entra-era reports into public anonymous workspaces. Reupload the required evidence.

Do not treat incremental provisioning as a migration:

- Omitting/disabling an export module does **not** stop an existing scheduled
  processor or its running executions.
- Removing role assignments from Bicep does **not** revoke already deployed grants.
- Old native-export storage management/IAM grants and target Cost Management
  Contributor grants require an explicitly approved operator review/removal.
- Preserve backups and old resources until the migration is accepted.

The preflight rejects a legacy processor-deployed flag. Use a new environment, or
have the operator explicitly stop/retire the known legacy processor, verify no
scheduled/running export work remains, review obsolete permissions and complete
the new schema/bootstrap before approving any in-place release. No automated
destructive migration or account/registration deletion is provided.

## Acceptance after deployment

- ACR remains Premium/public-disabled/admin-disabled; private pulls/builds work.
- PostgreSQL, Blob, Key Vault and optional Foundry remain private.
- `/api/health` and `/api/ready` succeed after schema bootstrap.
- A fresh browser reaches Admin with no Microsoft login.
- Upload without SP produces accurate costs and makes no target Azure calls.
- Another browser cannot view the first browser's files, config, jobs or snapshots.
- Adding a manually provisioned Reader SP produces permitted live evidence.
- A failed/replaced/cancelled job cannot replace a complete dashboard.
- Test actual customer file sizes, query latency, Azure throttling, restarts,
  backups, retention, regional quota/policy and availability requirements.

Local test results are recorded in the [implementation plan](../.azure/deployment-plan.md);
they are not an Azure deployment or customer-load certification.
