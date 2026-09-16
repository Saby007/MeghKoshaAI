@export()
type ModelDeployment = {
  name: string
  modelFormat: string
  modelName: string
  modelVersion: string
  sku: 'Standard' | 'GlobalStandard' | 'DataZoneStandard'
  capacity: int
}

@minLength(2)
@maxLength(20)
param resourceToken string
param foundryLocation string
param networkLocation string
param tags object
param networkId string
param privateEndpointSubnetId string
param apiPrincipalId string
@minLength(2)
@maxLength(32)
param projectName string = 'cost-agent-project'
@description('Empty until specific models, versions, deployment SKUs and quota are approved.')
param modelDeployments ModelDeployment[] = []

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: 'ai-${resourceToken}'
  location: foundryLocation
  tags: tags
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    allowProjectManagement: true
    customSubDomainName: 'ai-${resourceToken}'
    disableLocalAuth: true
    publicNetworkAccess: 'Disabled'
    networkAcls: { defaultAction: 'Deny' }
    // Reclaims a soft-deleted account of the same name (e.g. after `azd down` without --purge)
    // instead of failing with FlagMustBeSetForRestore on the next `azd up`.
    restore: true
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: account
  name: projectName
  location: foundryLocation
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    displayName: 'Cost Assessment AI'
    description: 'Scoped cost evidence narration; agent initialization is a separate approved stage.'
  }
}

resource models 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = [for deployment in modelDeployments: {
  parent: account
  name: deployment.name
  sku: { name: deployment.sku, capacity: deployment.capacity }
  properties: {
    model: {
      format: deployment.modelFormat
      name: deployment.modelName
      version: deployment.modelVersion
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}]

resource projectReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(project.id, apiPrincipalId, 'foundry-user')
  scope: project
  properties: {
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '53ca6127-db72-4b80-b1b0-d745d6d5456d')
  }
}

resource modelCaller 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, apiPrincipalId, 'openai-user')
  scope: account
  properties: {
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
  }
}

var privateZoneNames = [
  'privatelink.cognitiveservices.azure.com'
  'privatelink.openai.azure.com'
  'privatelink.services.ai.azure.com'
]

resource privateZones 'Microsoft.Network/privateDnsZones@2020-06-01' = [for zoneName in privateZoneNames: {
  name: zoneName
  location: 'global'
  tags: tags
}]

resource zoneLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = [for (zoneName, zoneIndex) in privateZoneNames: {
  parent: privateZones[zoneIndex]
  name: 'link-${resourceToken}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: networkId }
  }
}]

resource endpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pe-ai-${resourceToken}'
  location: networkLocation
  tags: tags
  properties: {
    subnet: { id: privateEndpointSubnetId }
    privateLinkServiceConnections: [
      {
        name: 'account'
        properties: {
          privateLinkServiceId: account.id
          groupIds: ['account']
        }
      }
    ]
  }
}

resource zoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: endpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [for (zoneName, zoneIndex) in privateZoneNames: {
      name: 'zone-${zoneIndex}'
      properties: { privateDnsZoneId: privateZones[zoneIndex].id }
    }]
  }
}

output accountName string = account.name
output accountId string = account.id
output endpoint string = account.properties.endpoint
output projectEndpoint string = 'https://${account.name}.services.ai.azure.com/api/projects/${projectName}'
output projectId string = project.id
