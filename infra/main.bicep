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
@minValue(30)
@maxValue(730)
param logRetentionDays int = 30
@allowed(['Standard_LRS', 'Standard_ZRS'])
param storageSku string = 'Standard_LRS'
param allowNativeExportTrustedServices bool = false
@description('Keep false until the scheduled processor implementation is validated end to end.')
param enableProcessor bool = false
param processorImage string = ''
param processorSchedule string = '*/15 * * * *'
@minLength(2)
@maxLength(32)
param foundryProjectName string = 'cost-agent-project'
param modelDeployments ModelDeployment[] = []
@description('Keep false until the Foundry agent and model invocation are verified end to end.')
param enableAiRuntime bool = false
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

resource group 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: empty(resourceGroupName) ? 'rg-${environmentName}' : resourceGroupName
  location: location
  tags: tags
}

var resourceToken = toLower(uniqueString(subscription().id, environmentName, group.name))
var exportName = 'focus-closed-month-${resourceToken}'

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
  }
}

module ai './modules/ai.bicep' = if (aiEnabled) {
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
  }
}

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
    apiEnvironment: [
      { name: 'APP_PROFILE', value: profile }
      { name: 'APP_SCHEDULER_ENABLED', value: string(deployProcessor) }
      { name: 'MEGHKOSHA_AI_ENABLED', value: string(aiEnabled && enableAiRuntime) }
      { name: 'AI_PROJECT_ENDPOINT', value: aiEnabled ? ai!.outputs.projectEndpoint : '' }
      { name: 'AI_SERVICES_ENDPOINT', value: aiEnabled ? ai!.outputs.endpoint : '' }
      { name: 'AGENT_NAME', value: agentName }
      { name: 'MODEL_ROUTER_DEPLOYMENT_NAME', value: modelRouterDeploymentName }
      { name: 'COST_EXPORT_STORAGE_URL', value: dataEnabled ? data!.outputs.storageUrl : '' }
      { name: 'COST_EXPORT_STORAGE_RESOURCE_ID', value: dataEnabled ? data!.outputs.storageResourceId : '' }
      { name: 'COST_EXPORT_CONTAINER', value: 'cost-exports' }
      { name: 'REPORT_SNAPSHOT_CONTAINER', value: 'report-snapshots' }
      { name: 'CONTROL_STATE_CONTAINER', value: 'control-state' }
      { name: 'COST_EXPORT_NAME', value: exportName }
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
    schedule: processorSchedule
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
output AZURE_CONTAINER_APPS_ENVIRONMENT_ID string = core.outputs.environmentId
output SERVICE_API_RESOURCE_NAME string = 'ca-api-${resourceToken}'
output SERVICE_WEB_RESOURCE_NAME string = 'ca-web-${resourceToken}'
output SERVICE_WEB_ENDPOINT_URL string = deployApplications ? apps!.outputs.webOrigin : ''
output APP_WEB_ORIGIN string = webOrigin
output MEGHKOSHA_OBO_MANAGED_IDENTITY_CLIENT_ID string = core.outputs.oboIdentity.clientId
output MEGHKOSHA_OBO_MANAGED_IDENTITY_RESOURCE_ID string = core.outputs.oboIdentity.resourceId
output MEGHKOSHA_OBO_MANAGED_IDENTITY_PRINCIPAL_ID string = core.outputs.oboIdentity.principalId
output COST_EXPORT_NAME string = exportName
output COST_EXPORT_STORAGE_RESOURCE_ID string = dataEnabled ? data!.outputs.storageResourceId : ''
output AI_PROJECT_ENDPOINT string = aiEnabled ? ai!.outputs.projectEndpoint : ''
output APP_DEPLOYMENT_STATE object = {
  profile: profile
  applicationsDeployed: deployApplications
  identityIdsConfigured: !empty(apiClientId) && !empty(webClientId)
  dataInfrastructureDeployed: dataEnabled
  processorDeployed: deployProcessor
  aiInfrastructureDeployed: aiEnabled
  aiRuntimeEnabled: aiEnabled && enableAiRuntime
  nativeExportIngressException: dataEnabled && allowNativeExportTrustedServices
  liveValidationRequired: true
}
