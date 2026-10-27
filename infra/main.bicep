targetScope = 'subscription'

import { ModelDeployment } from './modules/ai.bicep'

@sealed()
type AssessmentLimits = {
  inputBytes: int?
  spoolBytes: int?
  expandedBytes: int?
  csvRows: int?
  inventoryRows: int?
  historyRows: int?
  timeoutSeconds: int?
  leaseSeconds: int?
  maxAttempts: int?
  pageSize: int?
  responseBytes: int?
  recordBytes: int?
  inlineRows: int?
  apiConcurrency: int?
  interactiveRows: int?
  workConcurrency: int?
  readPoolSize: int?
  armIntervalMs: int?
  graphIntervalMs: int?
  metricsIntervalMs: int?
  costIntervalMs: int?
  azureGetRetries: int?
  detailRows: int?
  detailPoints: int?
}

@minLength(1)
@maxLength(64)
param environmentName string
param location string = 'centralindia'
param foundryLocation string = 'eastus2'
@allowed(['core', 'data', 'ai'])
param profile string = 'core'
@description('New configurations use private PostgreSQL/background assessments. Select blob explicitly for compatibility/foundation-only deployments.')
@allowed(['blob', 'postgres'])
param assessmentMode string = 'postgres'
param postgresSkuName string = 'Standard_D2ds_v5'
@allowed(['GeneralPurpose', 'MemoryOptimized'])
param postgresSkuTier string = 'GeneralPurpose'
@allowed(['16', '17'])
param postgresVersion string = '16'
@minValue(32)
@maxValue(16384)
param postgresStorageGB int = 128
@description('Worker CPU; memory is paired safely: 1 CPU/2 GiB, 2 CPU/4 GiB, 4 CPU/8 GiB.')
@allowed(['1.0', '2.0', '4.0'])
param assessmentWorkerCpu string = '4.0'
@minValue(1)
@maxValue(4)
param assessmentWorkerReplicas int = 1
@minValue(1)
@maxValue(1000)
param assessmentQueueCapacity int = 50
@minValue(2)
@maxValue(50)
param assessmentPoolSize int = 5
@description('Optional image for database bootstrap before application release; otherwise reuse the API image.')
param assessmentImage string = ''
@description('Stage only the private database and manual bootstrap job; preserve existing application resources until bootstrap is verified.')
param assessmentFoundationOnly bool = false
@description('Optional numeric worker/API safety budgets. Defaults are validated again by the runtime.')
param assessmentLimits AssessmentLimits = {}
param resourceGroupName string = ''
param vnetAddressPrefix string = '10.42.0.0/23'
param containerSubnetPrefix string = '10.42.0.0/24'
param privateEndpointSubnetPrefix string = '10.42.1.0/27'
@description('Published API image; leave both image parameters empty for foundation-only provisioning.')
param apiImage string = ''
@description('Published static web image; no public placeholder application is deployed.')
param webImage string = ''
param apiClientId string = ''
param webClientId string = ''
@minValue(0)
@maxValue(3)
param minimumReplicas int = 1
@minValue(30)
@maxValue(730)
param logRetentionDays int = 30
@allowed(['Standard_LRS', 'Standard_ZRS'])
param storageSku string = 'Standard_LRS'
param allowNativeExportTrustedServices bool = false
@description('Policy-restricted subscriptions: put the container registry behind a private endpoint and deny public network access. Requires the Premium registry tier and stops azd remote builds from reaching the registry.')
param privateRegistry bool = false
@description('PostgreSQL deployments always require private ACR. Use a private Docker build host, or opt into a managed pool only in a supported region.')
@allowed(['private-runner', 'managed-pool'])
param registryBuildMode string = assessmentMode == 'postgres' ? 'private-runner' : 'managed-pool'
@description('Optional operator-controlled private Docker VM with NAT egress. Adds VM/disk/NAT cost; no public VM IP or inbound access.')
param enablePrivateBuilder bool = false
param privateBuilderVmSize string = 'Standard_D2s_v5'
param privateBuilderSshPublicKey string = ''
param privateBuilderImageVersion string = ''
@description('Policy-restricted subscriptions: enable blob versioning and blob/container soft delete on the export storage account.')
param blobDataProtection bool = false
@minValue(1)
@maxValue(365)
param blobRetentionDays int = 7
@description('Days to keep daily month-to-date FOCUS snapshots under cost-exports/focus-daily/.')
@minValue(7)
@maxValue(365)
param dailyExportRetentionDays int = 60
@description('Days to keep closed-month FOCUS exports under cost-exports/focus/. Must cover the longest 6-month history window.')
@minValue(190)
@maxValue(3650)
param closedMonthRetentionDays int = 214
@description('Months each subscription may export at once. Keep 1 until Azure is confirmed to run one export\'s months in parallel.')
@minValue(1)
@maxValue(6)
param exportParallelMonths int = 1
@description('Keep false until the scheduled processor implementation is validated end to end.')
param enableProcessor bool = false
@description('Only set true when redeploying the same environment name after azd down without --purge left a soft-deleted AI Foundry account behind.')
param restoreAiAccount bool = false
@description('Reuse an already-created Foundry account and deploy only its project, model, RBAC and private endpoint children.')
param reuseAiAccount bool = false
param processorImage string = ''
@description('Worker cadence. Each tick advances monthly pulls and starts daily pulls once their UTC time has passed.')
param processorSchedule string = '*/5 * * * *'
@minLength(2)
@maxLength(32)
param foundryProjectName string = 'cost-agent-project'
param modelDeployments ModelDeployment[] = []
@description('Keep false until the Foundry agent and model invocation are verified end to end.')
param enableAiRuntime bool = false
@description('Enable grounded chat through the app-local Model Router without enabling the separate hosted-agent narrator.')
param enableChatRuntime bool = false
param agentName string = 'cost-agent'
param modelRouterDeploymentName string = ''

var tags = {
  'azd-env-name': environmentName
  'app-name': 'cost-assessment'
}
var dataEnabled = contains(['data', 'ai'], profile) || assessmentMode == 'postgres'
var scalableEnabled = assessmentMode == 'postgres'
var privateRegistryEnabled = privateRegistry || scalableEnabled
var aiEnabled = profile == 'ai'
var deployApplications = !empty(apiImage) && !empty(webImage) && !(scalableEnabled && assessmentFoundationOnly)
var deployProcessor = dataEnabled && enableProcessor && !empty(processorImage)

resource group 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: empty(resourceGroupName) ? 'rg-${environmentName}' : resourceGroupName
  location: location
  tags: tags
}

var resourceToken = toLower(uniqueString(subscription().id, environmentName, group.name))
var exportName = 'focus-closed-month-${resourceToken}'
var dailyExportName = 'focus-daily-${resourceToken}'

module core './modules/core.bicep' = {
  name: 'app-core'
  scope: group
  params: {
    resourceToken: resourceToken
    location: location
    tags: tags
    vnetAddressPrefix: vnetAddressPrefix
    containerSubnetPrefix: containerSubnetPrefix
    privateEndpointSubnetPrefix: privateEndpointSubnetPrefix
    logRetentionDays: logRetentionDays
    privateRegistry: privateRegistryEnabled
    registryBuildMode: registryBuildMode
    enablePrivateBuilder: enablePrivateBuilder
  }
}

module privateBuilder './modules/private-builder.bicep' = if (enablePrivateBuilder && privateRegistryEnabled && registryBuildMode == 'private-runner') {
  name: 'app-private-builder'
  scope: group
  params: {
    resourceToken: resourceToken
    location: location
    tags: tags
    subnetId: core.outputs.buildAgentSubnetId
    registryName: core.outputs.registryName
    vmSize: privateBuilderVmSize
    sshPublicKey: privateBuilderSshPublicKey
    imageVersion: privateBuilderImageVersion
  }
}

module data './modules/data.bicep' = if (dataEnabled) {
  name: 'app-data'
  scope: group
  params: {
    resourceToken: resourceToken
    location: location
    tags: tags
    networkId: core.outputs.networkId
    privateEndpointSubnetId: core.outputs.privateEndpointSubnetId
    apiPrincipalId: core.outputs.apiIdentity.principalId
    storageSku: storageSku
    allowNativeExportTrustedServices: allowNativeExportTrustedServices
    blobDataProtection: blobDataProtection
    blobRetentionDays: blobRetentionDays
    dailyExportRetentionDays: dailyExportRetentionDays
    closedMonthRetentionDays: closedMonthRetentionDays
  }
}

module postgres './modules/postgres.bicep' = if (scalableEnabled) {
  name: 'app-postgres'
  scope: group
  params: {
    resourceToken: resourceToken
    location: location
    tags: tags
    networkId: core.outputs.networkId
    privateEndpointSubnetId: core.outputs.privateEndpointSubnetId
    skuName: postgresSkuName
    skuTier: postgresSkuTier
    version: postgresVersion
    storageSizeGB: postgresStorageGB
  }
}

var assessmentEnvironment = [
  { name: 'ASSESSMENT_MODE', value: assessmentMode }
  { name: 'ASSESSMENT_DB_HOST', value: scalableEnabled ? postgres!.outputs.host : '' }
  { name: 'ASSESSMENT_DB_NAME', value: scalableEnabled ? postgres!.outputs.databaseName : '' }
  { name: 'ASSESSMENT_DB_POOL_SIZE', value: string(assessmentPoolSize) }
  { name: 'ASSESSMENT_QUEUE_CAPACITY', value: string(assessmentQueueCapacity) }
  { name: 'ASSESSMENT_INPUT_BYTES', value: string(assessmentLimits.?inputBytes ?? 4294967296) }
  { name: 'ASSESSMENT_SPOOL_BYTES', value: string(assessmentLimits.?spoolBytes ?? (assessmentWorkerCpu == '1.0' ? 2147483648 : assessmentWorkerCpu == '2.0' ? 4294967296 : 6442450944)) }
  { name: 'ASSESSMENT_EXPANDED_BYTES', value: string(assessmentLimits.?expandedBytes ?? 8589934592) }
  { name: 'ASSESSMENT_CSV_ROWS', value: string(assessmentLimits.?csvRows ?? 20000000) }
  { name: 'ASSESSMENT_INVENTORY_ROWS', value: string(assessmentLimits.?inventoryRows ?? 500000) }
  { name: 'ASSESSMENT_HISTORY_ROWS', value: string(assessmentLimits.?historyRows ?? 2000000) }
  { name: 'ASSESSMENT_TIMEOUT_SECONDS', value: string(assessmentLimits.?timeoutSeconds ?? 3600) }
  { name: 'ASSESSMENT_LEASE_SECONDS', value: string(assessmentLimits.?leaseSeconds ?? 120) }
  { name: 'ASSESSMENT_MAX_ATTEMPTS', value: string(assessmentLimits.?maxAttempts ?? 3) }
  { name: 'ASSESSMENT_PAGE_SIZE', value: string(assessmentLimits.?pageSize ?? 100) }
  { name: 'ASSESSMENT_RESPONSE_BYTES', value: string(assessmentLimits.?responseBytes ?? 1048576) }
  { name: 'ASSESSMENT_RECORD_BYTES', value: string(assessmentLimits.?recordBytes ?? 262144) }
  { name: 'ASSESSMENT_INLINE_ROWS', value: string(assessmentLimits.?inlineRows ?? 20) }
  { name: 'ASSESSMENT_API_CONCURRENCY', value: string(assessmentLimits.?apiConcurrency ?? 20) }
  { name: 'ASSESSMENT_INTERACTIVE_ROWS', value: string(assessmentLimits.?interactiveRows ?? 1000) }
  { name: 'ASSESSMENT_WORK_CONCURRENCY', value: string(assessmentLimits.?workConcurrency ?? 4) }
  { name: 'ASSESSMENT_READ_POOL_SIZE', value: string(assessmentLimits.?readPoolSize ?? (assessmentPoolSize > 3 ? 3 : assessmentPoolSize - 1)) }
  { name: 'ASSESSMENT_ARM_INTERVAL_MS', value: string(assessmentLimits.?armIntervalMs ?? 200) }
  { name: 'ASSESSMENT_GRAPH_INTERVAL_MS', value: string(assessmentLimits.?graphIntervalMs ?? 400) }
  { name: 'ASSESSMENT_METRICS_INTERVAL_MS', value: string(assessmentLimits.?metricsIntervalMs ?? 500) }
  { name: 'ASSESSMENT_COST_INTERVAL_MS', value: string(assessmentLimits.?costIntervalMs ?? 15000) }
  { name: 'ASSESSMENT_AZURE_GET_RETRIES', value: string(assessmentLimits.?azureGetRetries ?? 3) }
  { name: 'ASSESSMENT_DETAIL_ROWS', value: string(assessmentLimits.?detailRows ?? 50000) }
  { name: 'ASSESSMENT_DETAIL_POINTS', value: string(assessmentLimits.?detailPoints ?? 5000000) }
]

module ai './modules/ai.bicep' = if (aiEnabled && !reuseAiAccount) {
  name: 'app-ai'
  scope: group
  params: {
    resourceToken: resourceToken
    foundryLocation: foundryLocation
    networkLocation: location
    tags: tags
    networkId: core.outputs.networkId
    privateEndpointSubnetId: core.outputs.privateEndpointSubnetId
    apiPrincipalId: core.outputs.apiIdentity.principalId
    projectName: foundryProjectName
    modelDeployments: modelDeployments
    restoreAiAccount: restoreAiAccount
  }
}

module existingAi './modules/ai-existing.bicep' = if (aiEnabled && reuseAiAccount) {
  name: 'app-ai-existing'
  scope: group
  params: {
    resourceToken: resourceToken
    foundryLocation: foundryLocation
    networkLocation: location
    tags: tags
    networkId: core.outputs.networkId
    privateEndpointSubnetId: core.outputs.privateEndpointSubnetId
    apiPrincipalId: core.outputs.apiIdentity.principalId
    projectName: foundryProjectName
    modelDeployments: modelDeployments
  }
}

var aiEndpoint = !aiEnabled ? '' : reuseAiAccount ? existingAi!.outputs.endpoint : ai!.outputs.endpoint
var aiProjectEndpoint = !aiEnabled ? '' : reuseAiAccount ? existingAi!.outputs.projectEndpoint : ai!.outputs.projectEndpoint

var webOrigin = 'https://ca-web-${resourceToken}.${core.outputs.environmentDomain}'

module apps './modules/apps.bicep' = if (deployApplications) {
  name: 'app-apps'
  scope: group
  params: {
    resourceToken: resourceToken
    location: location
    tags: tags
    environmentId: core.outputs.environmentId
    registryServer: core.outputs.registryServer
    apiIdentity: core.outputs.apiIdentity
    webIdentity: core.outputs.webIdentity
    oboIdentity: core.outputs.oboIdentity
    apiImage: apiImage
    webImage: webImage
    apiClientId: apiClientId
    webClientId: webClientId
    minimumReplicas: minimumReplicas
    apiEnvironment: concat(assessmentEnvironment, [
      { name: 'ASSESSMENT_DB_USER', value: 'meghkosha_api' }
      { name: 'APP_PROFILE', value: profile }
      { name: 'APP_SCHEDULER_ENABLED', value: string(deployProcessor) }
      { name: 'MEGHKOSHA_AI_ENABLED', value: string(aiEnabled && enableAiRuntime) }
      { name: 'FOUNDRY_CHAT_ENABLED', value: string(aiEnabled && enableChatRuntime && !empty(modelRouterDeploymentName)) }
      { name: 'AI_PROJECT_ENDPOINT', value: aiProjectEndpoint }
      { name: 'AI_SERVICES_ENDPOINT', value: aiEndpoint }
      { name: 'AGENT_NAME', value: agentName }
      { name: 'MODEL_ROUTER_DEPLOYMENT_NAME', value: modelRouterDeploymentName }
      { name: 'COST_EXPORT_STORAGE_URL', value: dataEnabled ? data!.outputs.storageUrl : '' }
      { name: 'COST_EXPORT_STORAGE_RESOURCE_ID', value: dataEnabled ? data!.outputs.storageResourceId : '' }
      { name: 'COST_EXPORT_CONTAINER', value: 'cost-exports' }
      { name: 'REPORT_SNAPSHOT_CONTAINER', value: 'report-snapshots' }
      { name: 'CONTROL_STATE_CONTAINER', value: 'control-state' }
      { name: 'COST_EXPORT_NAME', value: exportName }
      { name: 'COST_EXPORT_DAILY_NAME', value: dailyExportName }
      { name: 'FOCUS_EXPORT_PARALLEL_MONTHS', value: string(exportParallelMonths) }
      { name: 'COST_EXPORT_LOCATION', value: location }
      { name: 'REPORT_PUBLIC_APP_URL', value: webOrigin }
    ])
  }
}

module processor './modules/processor.bicep' = if (deployProcessor) {
  name: 'app-processor'
  scope: group
  params: {
    resourceToken: resourceToken
    location: location
    tags: tags
    environmentId: core.outputs.environmentId
    registryName: core.outputs.registryName
    processorIdentity: data!.outputs.processorIdentity
    processorImage: processorImage
    storageUrl: data!.outputs.storageUrl
    storageResourceId: data!.outputs.storageResourceId
    exportName: exportName
    dailyExportName: dailyExportName
    exportParallelMonths: exportParallelMonths
    schedule: processorSchedule
    assessmentEnvironment: concat(assessmentEnvironment, [
      { name: 'ASSESSMENT_DB_USER', value: 'meghkosha_worker' }
    ])
  }
}

var assessmentRuntimeImage = empty(assessmentImage) ? apiImage : assessmentImage

module assessmentWorker './modules/assessment-worker.bicep' = if (scalableEnabled && !empty(assessmentRuntimeImage)) {
  name: 'app-assessment-worker'
  scope: group
  params: {
    resourceToken: resourceToken
    location: location
    tags: tags
    environmentId: core.outputs.environmentId
    registryName: core.outputs.registryName
    image: assessmentRuntimeImage
    workerIdentity: data!.outputs.processorIdentity
    bootstrapIdentity: postgres!.outputs.bootstrapIdentity
    apiPrincipalId: core.outputs.apiIdentity.principalId
    databaseHost: postgres!.outputs.host
    databaseName: postgres!.outputs.databaseName
    enableWorker: deployApplications
    grantWorkerRegistryPull: !deployProcessor
    workerCpu: assessmentWorkerCpu
    workerReplicas: assessmentWorkerReplicas
    workerEnvironment: concat(assessmentEnvironment, [
      { name: 'COST_EXPORT_STORAGE_URL', value: data!.outputs.storageUrl }
      { name: 'COST_EXPORT_STORAGE_RESOURCE_ID', value: data!.outputs.storageResourceId }
      { name: 'COST_EXPORT_CONTAINER', value: 'cost-exports' }
      { name: 'REPORT_SNAPSHOT_CONTAINER', value: 'report-snapshots' }
      { name: 'CONTROL_STATE_CONTAINER', value: 'control-state' }
      { name: 'COST_EXPORT_NAME', value: exportName }
      { name: 'COST_EXPORT_DAILY_NAME', value: dailyExportName }
    ])
  }
}

output ASSESSMENT_MODE string = assessmentMode
output ASSESSMENT_DB_HOST string = scalableEnabled ? postgres!.outputs.host : ''
output ASSESSMENT_BOOTSTRAP_JOB string = scalableEnabled && !empty(assessmentRuntimeImage) ? assessmentWorker!.outputs.bootstrapJobName : ''
output SERVICE_ASSESSMENT_RESOURCE_NAME string = scalableEnabled && deployApplications ? assessmentWorker!.outputs.workerName : ''
output AZURE_RESOURCE_GROUP string = group.name
output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = tenant().tenantId
output FOUNDRY_LOCATION string = foundryLocation
output APP_PROFILE string = profile
output APP_PROVISIONED_PROFILE string = profile
output APP_APPLICATIONS_DEPLOYED bool = deployApplications
output APP_PROCESSOR_DEPLOYED bool = deployProcessor
output AZURE_CONTAINER_REGISTRY_NAME string = core.outputs.registryName
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = core.outputs.registryServer
output APP_PRIVATE_REGISTRY bool = privateRegistryEnabled
output APP_REGISTRY_BUILD_MODE string = registryBuildMode
output APP_PRIVATE_BUILDER_VM_NAME string = enablePrivateBuilder && privateRegistryEnabled && registryBuildMode == 'private-runner' ? privateBuilder!.outputs.vmName : ''
output APP_PRIVATE_BUILDER_CLIENT_ID string = enablePrivateBuilder && privateRegistryEnabled && registryBuildMode == 'private-runner' ? privateBuilder!.outputs.identityClientId : ''
output APP_BUILD_AGENT_SUBNET_ID string = core.outputs.buildAgentSubnetId
output APP_BUILD_AGENT_POOL string = core.outputs.buildAgentPoolName
output AZURE_CONTAINER_APPS_ENVIRONMENT_ID string = core.outputs.environmentId
output SERVICE_API_RESOURCE_NAME string = 'ca-api-${resourceToken}'
output SERVICE_WEB_RESOURCE_NAME string = 'ca-web-${resourceToken}'
output SERVICE_WEB_ENDPOINT_URL string = deployApplications ? apps!.outputs.webOrigin : ''
output APP_WEB_ORIGIN string = webOrigin
output MEGHKOSHA_OBO_MANAGED_IDENTITY_CLIENT_ID string = core.outputs.oboIdentity.clientId
output MEGHKOSHA_OBO_MANAGED_IDENTITY_RESOURCE_ID string = core.outputs.oboIdentity.resourceId
output MEGHKOSHA_OBO_MANAGED_IDENTITY_PRINCIPAL_ID string = core.outputs.oboIdentity.principalId
output COST_EXPORT_NAME string = exportName
output COST_EXPORT_DAILY_NAME string = dailyExportName
output COST_EXPORT_STORAGE_RESOURCE_ID string = dataEnabled ? data!.outputs.storageResourceId : ''
output AI_PROJECT_ENDPOINT string = aiProjectEndpoint
output AI_SERVICES_ENDPOINT string = aiEndpoint
output APP_FOUNDRY_ACCOUNT_NAME string = aiEnabled ? 'ai-${resourceToken}' : ''
output APP_DEPLOYMENT_STATE object = {
  profile: profile
  applicationsDeployed: deployApplications
  identityIdsConfigured: !empty(apiClientId) && !empty(webClientId)
  dataInfrastructureDeployed: dataEnabled
  processorDeployed: deployProcessor
  aiInfrastructureDeployed: aiEnabled
  aiAccountReused: aiEnabled && reuseAiAccount
  aiRuntimeEnabled: aiEnabled && enableAiRuntime
  chatRuntimeEnabled: aiEnabled && enableChatRuntime && !empty(modelRouterDeploymentName)
  nativeExportIngressException: dataEnabled && allowNativeExportTrustedServices
  liveValidationRequired: true
}
