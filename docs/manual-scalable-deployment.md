# Manual Dev rollout: private ACR, private PostgreSQL, background assessments

These are **operator-run steps**, not an automatic deployment workflow. The assistant
does not execute them. Review the cost, network, identity and policy changes first.
No step enables public ACR access or uses registry/database passwords.

The staged runner's source check additionally requires a local authenticated
GitHub CLI (`gh auth login --hostname github.com --web`). It checks public source
and exact commits through authenticated GitHub API requests; it never extracts
or copies the GitHub credential to the private builder.

If you do not already have private Docker/VPN connectivity, use the
[one-command script with a private build VM](manual-staged-deployment.md) instead:
`scripts\deploy-end-to-end.ps1 -EnvironmentName meghkoshaai-scalable-dev -SubscriptionId '<uuid>'`.
It provisions the builder/NAT and runs the stages automatically through Azure Run
Command; it does not require opening inbound VM or registry access.

The implementation branch is `feature/private-postgres-assessments`, based on `Dev`.
Review the published branch and exact commit before using this guide. Provisioning,
image publication, database bootstrap, permissions and application release remain
operator-run steps. Do not commit `.azure`, `.env`, credentials, or sign-in state.

## Stop conditions

- The existing `meghkoshaai-dev` environment has a layout hold: it still uses Static Web
  Apps whereas this branch declares an ACA web service. **Do not run this guide against
  that environment as an implicit migration.** Use a separately approved parallel Dev
  environment, or resolve/review the existing migration first.
- This guide is the existing-private-builder/manual alternative. The default
  `deploy-end-to-end.ps1` now orchestrates the private-VM rollout described in the
  linked guide. Do not mix the two flows or call `deploy-ai.ps1`, `azd up` or
  `azd deploy` for the PostgreSQL rollout. Both flows release immutable images through Bicep.
- A public GitHub-hosted runner or ordinary public Cloud Shell is not a private build
  host. Do not temporarily enable registry public access to work around connectivity.
- If policy, permissions, regional quota, SKU availability, private DNS or bootstrap
  checks fail, stop. Do not bypass policy or release an unverified runtime.

## What is provisioned

- ACR Premium, public network access disabled, admin user disabled, private endpoint
  and `privatelink.azurecr.io` DNS. ACA pulls images with its managed identities.
- PostgreSQL Flexible Server with public access and password authentication disabled,
  private endpoint/DNS and Entra managed-identity authentication.
- A separate ingress-disabled assessment worker, durable PostgreSQL jobs, and a
  dedicated **manual** database-bootstrap job. Runtime identities are not DB admins.
- The original dashboard and features use PostgreSQL aggregates, series and
  bounded detail queries. No alternative workspace or classic-mode switch is used.
  Full/custom/cost downloads are generated in the isolated background worker from
  SQL and streamed from SQL chunks, not reconstructed in API requests.
- Existing Blob exports/snapshots remain the raw-data/audit source.

Expect ongoing charges for ACR Premium, PostgreSQL, private endpoints, Log Analytics
and worker/API replicas. A managed ACR build pool, if selected, adds its own charge.
Preview and approve these before every provisioning stage.

## 1. Prerequisites and clean checkout

Use PowerShell **7.4+**, Git, Azure CLI with Container Apps support, AZD **1.35+**,
and an approved identity. Hosting-scope resource creation and role assignments require
appropriate Contributor/User Access Administrator permissions or an approved equivalent.
App-registration changes require your organization's Entra permissions/process.
Target-subscription role grants need their own explicit authorization.

```powershell
git clone --branch feature/private-postgres-assessments https://github.com/Saby007/MeghKoshaAI.git MeghKoshaAI-scalable-dev
Set-Location .\MeghKoshaAI-scalable-dev
git status --short
git rev-parse HEAD
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
```

The branch must exist on GitHub first. Build the reviewed commit, not a dirty checkout.
Private Docker builds require a Linux/amd64-capable Docker host with network routing
and DNS to the registry private endpoint. Builders also need approved outbound access
to the pinned base-image registries and dependency feeds.

## 2. Choose the private build path

| Setting | Use |
|---|---|
| `APP_REGISTRY_BUILD_MODE=private-runner` | Recommended default. An approved VM, self-hosted runner or VPN-connected Docker host builds/pushes through the private endpoint. No managed build-pool preview dependency. |
| `APP_REGISTRY_BUILD_MODE=managed-pool` | Optional ACR Tasks dedicated agent pool on the app VNet. Check current regional availability, permissions and quota before provisioning. |

Central India is not in the currently documented managed-agent-pool region list;
do not confuse it with Jio India Central. Use `private-runner` there unless the
platform's supported-region list changes. This template does not provision your
private Docker VM or VPN; arrange that approved build path before the image step.
If you select `managed-pool`, replace the build-mode setting in step 3 before
provisioning. Do not select managed-pool build commands for a private-runner foundation.

## 3. Management terminal: configure a new Dev environment

Replace these values. Confirm the subscription/tenant/region rather than inheriting
an unrelated Azure CLI context.

```powershell
$TenantId = Read-Host 'Hosting tenant UUID'
$SubscriptionId = Read-Host 'Hosting subscription UUID'
$EnvironmentName = 'meghkoshaai-scalable-dev'
$Location = 'centralindia'

az login --tenant $TenantId
az account set --subscription $SubscriptionId
az account show --query '{subscription:id,tenant:tenantId,name:name}' --output table
azd auth login
azd env new $EnvironmentName
azd env set AZURE_SUBSCRIPTION_ID $SubscriptionId
azd env set AZURE_LOCATION $Location
azd env set APP_PROFILE data
azd env set APP_ASSESSMENT_MODE postgres
azd env set APP_PRIVATE_REGISTRY true
azd env set APP_REGISTRY_BUILD_MODE private-runner
azd env set APP_ASSESSMENT_FOUNDATION_ONLY true
azd env set APP_ASSESSMENT_DB_BOOTSTRAPPED false
azd env set APP_ENABLE_PROCESSOR false
azd env set APP_MIN_REPLICAS 2
azd env set APP_POSTGRES_SKU Standard_D2ds_v5
azd env set APP_POSTGRES_TIER GeneralPurpose
azd env set APP_POSTGRES_STORAGE_GB 128
azd env set APP_ASSESSMENT_WORKER_CPU 4.0
azd env set APP_ASSESSMENT_WORKER_REPLICAS 1
azd env set APP_ASSESSMENT_POOL_SIZE 5
azd env set APP_ASSESSMENT_QUEUE_CAPACITY 50
azd env set APP_VNET_PREFIX '10.42.0.0/23'
azd env set APP_ASSESSMENT_LIMITS '{"readPoolSize":3,"workConcurrency":4,"graphIntervalMs":400,"metricsIntervalMs":500,"detailRows":50000,"detailPoints":5000000}'
```

The commands above intentionally show the **data-only manual alternative**. To retain
the existing AI chatbot, prefer the [one-command rollout](manual-staged-deployment.md),
which provisions private Foundry/Model Router and enables Chat. For a manually
governed AI rollout, select `APP_PROFILE=ai`, `FOUNDRY_LOCATION`, the approved
`APP_MODEL_DEPLOYMENTS`, `MODEL_ROUTER_DEPLOYMENT_NAME`, `APP_ENABLE_CHAT_RUNTIME=true`
and the existing model-approval gates before provisioning. Merely adding a region
does not enable the model when using this data-only manual sequence.

Check PostgreSQL SKU/region capacity and ACA CPU/memory quota before continuing.
The API read pool (3) and operation pool (2) share the total connection budget (5);
also budget every API replica and worker process against the database connection limit.
Intervals are conservative application settings, not universal Azure quotas.

Define this local preflight helper. It reads AZD values into this terminal's process
environment; it does not provision anything or grant Azure permission.

```powershell
function Test-ManualProvisionInputs {
    $values = azd env get-values --output json | ConvertFrom-Json -AsHashtable
    foreach ($entry in $values.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, [string]$entry.Value, 'Process')
    }
    & .\scripts\validate-deployment-inputs.ps1 -Operation Provision
}
```

## 4. Preview, review, then provision the private foundation

Only after you approve resource changes in your terminal:

```powershell
$env:APP_ALLOW_AZURE_CHANGES = 'true'
Test-ManualProvisionInputs
azd provision --preview --no-prompt
# Review the preview and costs. Run the next command only if approved.
azd provision --no-prompt
$Values = azd env get-values --output json | ConvertFrom-Json
$ResourceGroup = $Values.AZURE_RESOURCE_GROUP
$Registry = $Values.AZURE_CONTAINER_REGISTRY_NAME
$RegistryEndpoint = $Values.AZURE_CONTAINER_REGISTRY_ENDPOINT
$BuildSubnet = $Values.APP_BUILD_AGENT_SUBNET_ID
$WebOrigin = $Values.APP_WEB_ORIGIN
```

With no image parameters supplied, this stage does not release the API/web/assessment
worker. Keep both API and web images unset; do not clear them on an existing live
environment. Arrange the approved private build host/routing/DNS using `$BuildSubnet`
or a correctly peered network.

Inspect the registry over ARM; this does not prove private data-plane connectivity:

```powershell
az acr show --name $Registry --resource-group $ResourceGroup --query '{sku:sku.name,publicNetworkAccess:publicNetworkAccess,adminUserEnabled:adminUserEnabled}' --output table
```

Required: **Premium**, **Disabled**, **false**.

## 5. Private build terminal: verify DNS, build and push

On the approved private build host, clone the same reviewed commit. Sign in with
your own authorized identity; do not copy Azure CLI/Docker sign-in state between
machines. The builder needs `AcrPush` or the appropriate repository-writer role for
the registry's permission mode. Runtime identities need only their scoped pull grants.

Set `$TenantId`, `$SubscriptionId`, `$Registry` and `$RegistryEndpoint` to the nonsecret
values from the management terminal. Verify the login-server **and regional data**
endpoints resolve inside the intended VNet and are reachable over HTTPS.

```powershell
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
$TenantId = Read-Host 'Hosting tenant UUID from step 3'
$SubscriptionId = Read-Host 'Hosting subscription UUID from step 3'
$Registry = Read-Host 'ACR name from step 4'
$RegistryEndpoint = Read-Host 'ACR login server from step 4 (no https prefix)'
az login --tenant $TenantId
az account set --subscription $SubscriptionId
[System.Net.Dns]::GetHostAddresses($RegistryEndpoint)
az acr login --name $Registry
$Tag = git rev-parse --short=12 HEAD
if (git status --porcelain) { throw 'Build only the reviewed clean checkout.' }
```

Private endpoint DNS configuration in Azure lists the additional data FQDNs to check.
Public addresses or registry login/list/push failures are stop conditions, not reasons
to enable public access.

For `private-runner`:

```powershell
docker build --platform linux/amd64 --file .\api\Dockerfile --tag "${RegistryEndpoint}/meghkoshaai-api:${Tag}" .\api
docker build --platform linux/amd64 --file .\web\Dockerfile --tag "${RegistryEndpoint}/meghkoshaai-web:${Tag}" .\web
docker push "${RegistryEndpoint}/meghkoshaai-api:${Tag}"
docker push "${RegistryEndpoint}/meghkoshaai-web:${Tag}"
```

For a verified `managed-pool`, replace the Docker build/push commands with:

```powershell
$Pool = 'build-agents'
az acr build --registry $Registry --agent-pool $Pool --platform linux/amd64 --file .\api\Dockerfile --image "meghkoshaai-api:${Tag}" .\api
az acr build --registry $Registry --agent-pool $Pool --platform linux/amd64 --file .\web\Dockerfile --image "meghkoshaai-web:${Tag}" .\web
```

Do not use the shared public `az acr build` fleet or AZD remote builds against this
private registry. Get immutable digests from the private build terminal:

```powershell
$ApiDigest = az acr repository show --name $Registry --image "meghkoshaai-api:${Tag}" --query digest --output tsv
$WebDigest = az acr repository show --name $Registry --image "meghkoshaai-web:${Tag}" --query digest --output tsv
$ApiImage = "${RegistryEndpoint}/meghkoshaai-api@${ApiDigest}"
$WebImage = "${RegistryEndpoint}/meghkoshaai-web@${WebDigest}"
$ApiImage
$WebImage
```

These two image references are nonsecret. Record them with the reviewed commit ID.

## 6. Management terminal: provision the manual DB bootstrap job

Copy only the two immutable image references into the management terminal:

```powershell
$ApiImage = Read-Host 'Reviewed API image reference including @sha256'
$WebImage = Read-Host 'Reviewed web image reference including @sha256'
azd env set SERVICE_ASSESSMENT_IMAGE_NAME $ApiImage
Test-ManualProvisionInputs
azd provision --preview --no-prompt
# Approve the preview before proceeding.
azd provision --no-prompt
$Values = azd env get-values --output json | ConvertFrom-Json
$BootstrapJob = $Values.ASSESSMENT_BOOTSTRAP_JOB
```

Keep the foundation-only flag true and API/web image parameters unset. Bootstrap
runs inside the ACA environment, so no database public firewall exception is needed.
The PostgreSQL template allowlists `pg_trgm`; the reviewed bootstrap creates the
extension, schema, indexes, quota-coordination table and least-privilege Entra roles.
For upgrades, review schema/index changes and extension allowlists before rerunning it.

```powershell
az containerapp job start --resource-group $ResourceGroup --name $BootstrapJob
az containerapp job execution list --resource-group $ResourceGroup --name $BootstrapJob --output table
```

Wait for the intended execution to report **Succeeded**. Inspect job logs and resolve
errors before proceeding. A provisioning success alone is not a bootstrap success.
Do not attach the bootstrap administrator identity to a runtime app.

## 7. Management terminal: configure sign-in and target-subscription access

Preview the existing identity helper before applying it:

```powershell
$Values = azd env get-values --output json | ConvertFrom-Json
$IdentityArgs = @{
    TenantId = $TenantId
    SubscriptionId = $SubscriptionId
    EnvironmentName = $EnvironmentName
    WebOrigin = $Values.APP_WEB_ORIGIN
    OboManagedIdentityResourceId = $Values.MEGHKOSHA_OBO_MANAGED_IDENTITY_RESOURCE_ID
}
# Add ServiceManagementReference here if your organization requires a Service Tree ID.
& .\scripts\bootstrap-identity.ps1 @IdentityArgs
# Apply only after identity changes are approved.
$Identity = & .\scripts\bootstrap-identity.ps1 @IdentityArgs -Apply | ConvertFrom-Json
azd env set MEGHKOSHA_API_CLIENT_ID $Identity.MEGHKOSHA_API_CLIENT_ID
azd env set MEGHKOSHA_WEB_CLIENT_ID $Identity.MEGHKOSHA_WEB_CLIENT_ID
```

Do not add administrator consent or create client secrets as a workaround for policy.
The user must also have the required Azure rights on every assessed subscription.

Identify the two runtime principals and explicitly choose the target subscriptions:

```powershell
$Token = $Values.SERVICE_API_RESOURCE_NAME -replace '^ca-api-', ''
$ApiPrincipal = az identity show --resource-group $ResourceGroup --name "id-api-${Token}" --query principalId --output tsv
$WorkerPrincipal = az identity show --resource-group $ResourceGroup --name "id-processor-${Token}" --query principalId --output tsv
$TargetSubscriptions = (Read-Host 'Approved assessed subscription UUIDs, comma-separated').Split(',') | ForEach-Object { $_.Trim() }
foreach ($Target in $TargetSubscriptions) {
    $ParsedTarget = [guid]::Empty
    if (-not [guid]::TryParse($Target, [ref]$ParsedTarget) -or $ParsedTarget -eq [guid]::Empty) { throw "Invalid target subscription UUID: $Target" }
    foreach ($Principal in @($ApiPrincipal, $WorkerPrincipal)) {
        az role assignment create --assignee-object-id $Principal --assignee-principal-type ServicePrincipal --role Reader --scope "/subscriptions/$Target"
        az role assignment create --assignee-object-id $Principal --assignee-principal-type ServicePrincipal --role 'Cost Management Contributor' --scope "/subscriptions/$Target"
    }
}
```

These are explicit operator-approved writes. The shared export processor needs the
export permissions; an assessment-only identity can use a narrower cost-read role
when the deployment separates that duty. Allow RBAC propagation and verify grants.
For native FOCUS export ingress, review the separately governed
`APP_EXPORT_TRUSTED_SERVICES` setting and Azure policy; it does not open ACR or PostgreSQL.

## 8. Preview and release the reviewed application images

Only after bootstrap, private connectivity and permissions are verified:

```powershell
azd env set APP_ASSESSMENT_DB_BOOTSTRAPPED true
azd env set SERVICE_API_IMAGE_NAME $ApiImage
azd env set SERVICE_WEB_IMAGE_NAME $WebImage
azd env set SERVICE_PROCESSOR_IMAGE_NAME $ApiImage
azd env set APP_ENABLE_PROCESSOR true
azd env set APP_ASSESSMENT_FOUNDATION_ONLY false
Test-ManualProvisionInputs
azd provision --preview --no-prompt
# Review image digests, identities, network settings and cost before approving release.
azd provision --no-prompt
```

The two API replicas reduce single-instance disruption; they do not enable zone
redundancy. PostgreSQL HA remains disabled in this Dev profile. Production HA,
environment migration and zone redundancy require their own design/cost approval.

## 9. Verify before accepting the release

```powershell
$Values = azd env get-values --output json | ConvertFrom-Json
$ResourceGroup = $Values.AZURE_RESOURCE_GROUP
$Token = $Values.SERVICE_API_RESOURCE_NAME -replace '^ca-api-', ''
az postgres flexible-server show --resource-group $ResourceGroup --name "pg-${Token}" --query '{publicNetworkAccess:network.publicNetworkAccess,passwordAuth:authConfig.passwordAuth,entraAuth:authConfig.activeDirectoryAuth}' --output table
az containerapp show --resource-group $ResourceGroup --name $Values.SERVICE_API_RESOURCE_NAME --query '{provisioning:properties.provisioningState,running:properties.runningStatus}' --output table
az containerapp show --resource-group $ResourceGroup --name $Values.SERVICE_ASSESSMENT_RESOURCE_NAME --query '{provisioning:properties.provisioningState,running:properties.runningStatus}' --output table
Invoke-RestMethod "$($Values.APP_WEB_ORIGIN)/api/health"
Invoke-RestMethod "$($Values.APP_WEB_ORIGIN)/api/ready"
```

Also verify in the portal/approved tooling:

- Registry remains Premium/private/admin-disabled; all image references are the
  reviewed digests. API, web, assessment and bootstrap image pulls use MI.
- PostgreSQL remains public-disabled/password-disabled and resolves privately.
  API and worker successfully connect as their own non-admin roles.
- Worker has no ingress. Unauthenticated protected API calls are rejected.
- Configure and complete the native FOCUS exports. Queue one explicit assessment;
  observe job progress, complete publication and reopening the exact authorized scope.
- Run the representative full-history workload while ten users read existing
  summaries/pages. Measure p95, pool waits, DB CPU/I/O, queue age, Azure 429s,
  memory/ephemeral disk, OOM events and ACA restart counts.
- Kill a worker execution, test recovery/cancellation, and simulate transient
  upstream delays only in the approved Dev test scope. No partial financial report
  should replace the last complete report.

The local synthetic benchmark is not an Azure availability guarantee. Accept the
release only after the actual private-network and retained-history checks pass.

## 10. Recovery

Retain the prior **compatible** API/web/worker digests before release. If validation
fails, restore those digests through a separately previewed provisioning change.
Do not switch an existing PostgreSQL deployment to Blob mode, drop the database,
clear deployed image settings, or enable public endpoints as an automatic rollback.
Database bootstrap/index changes need a reviewed compatible migration plan.

## References

- [ACR private endpoints](https://learn.microsoft.com/azure/container-registry/container-registry-private-endpoints)
- [Dedicated ACR task agent pools and regions](https://learn.microsoft.com/azure/container-registry/tasks-agent-pools)
- [Project template](../infra/main.bicep)
- [Local input validation](../scripts/validate-deployment-inputs.ps1)
