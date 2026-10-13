targetScope = 'subscription'

import { ModelDeployment } from './modules/ai.bicep'

@minLength(1)
@maxLength(64)
param environmentName string
param location string = 'centralindia'
param foundryLocation string = 'eastus2'
@allowed(['core', 'data', 'ai'])
param profile string = 'core'
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
@description('API vCPU on the Consumption profile; memory follows at 2 GiB per vCPU. The 4.0 default (8 GiB) holds six months of FOCUS history for large subscriptions; lower it for small ones.')
@allowed(['0.5', '0.75', '1.0', '1.25', '1.5', '1.75', '2.0', '2.25', '2.5', '2.75', '3.0', '3.25', '3.5', '3.75', '4.0'])
param apiCpu string = '4.0'
@description('Consecutive failed API liveness checks, 30 seconds apart, tolerated before the replica restarts. Large report builds can block the health check for over a minute.')
@minValue(3)
@maxValue(20)
param apiLivenessFailureThreshold int = 10
@minValue(30)
@maxValue(730)
param logRetentionDays int = 30
@allowed(['Standard_LRS', 'Standard_ZRS'])
param storageSku string = 'Standard_LRS'
param allowNativeExportTrustedServices bool = false
@description('Policy-restricted subscriptions: put the container registry behind a private endpoint and deny public network access. Requires the Premium registry tier and stops azd remote builds from reaching the registry.')
param privateRegistry bool = false
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
@description('Processor job vCPU on the Consumption profile; memory follows at 2 GiB per vCPU. The job rebuilds the full report after each pull, so it needs the same headroom as the API.')
@allowed(['0.5', '0.75', '1.0', '1.25', '1.5', '1.75', '2.0', '2.25', '2.5', '2.75', '3.0', '3.25', '3.5', '3.75', '4.0'])
param processorCpu string = '4.0'
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
var dataEnabled = contains(['data', 'ai'], profile)
var aiEnabled = profile == 'ai'
var deployApplications = !empty(apiImage) && !empty(webImage)
var deployProcessor = dataEnabled && enableProcessor && !empty(processorImage)
// Consumption replicas only accept CPU/memory pairs of 2 GiB per vCPU.
var consumptionMemory = {
  '0.5': '1Gi'
  '0.75': '1.5Gi'
  '1.0': '2Gi'
  '1.25': '2.5Gi'
  '1.5': '3Gi'
  '1.75': '3.5Gi'
  '2.0': '4Gi'
  '2.25': '4.5Gi'
  '2.5': '5Gi'
  '2.75': '5.5Gi'
  '3.0': '6Gi'
  '3.25': '6.5Gi'
  '3.5': '7Gi'
  '3.75': '7.5Gi'
  '4.0': '8Gi'
}

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
    privateRegistry: privateRegistry
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
    apiCpu: apiCpu
    apiMemory: consumptionMemory[apiCpu]
    apiLivenessFailureThreshold: apiLivenessFailureThreshold
    apiEnvironment: [
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
    ]
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
    processorCpu: processorCpu
    processorMemory: consumptionMemory[processorCpu]
  }
}

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
output APP_PRIVATE_REGISTRY bool = privateRegistry
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
