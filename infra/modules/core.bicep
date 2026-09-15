@minLength(2)
@maxLength(20)
param resourceToken string
param location string
param tags object
param vnetAddressPrefix string
param containerSubnetPrefix string
param privateEndpointSubnetPrefix string
@minValue(30)
@maxValue(730)
param logRetentionDays int = 30

resource network 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'vnet-${resourceToken}'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: [vnetAddressPrefix] }
    subnets: [
      {
        name: 'containers'
        properties: {
          addressPrefix: containerSubnetPrefix
          delegations: [
            {
              name: 'container-apps'
              properties: { serviceName: 'Microsoft.App/environments' }
            }
          ]
        }
      }
      {
        name: 'private-endpoints'
        properties: {
          addressPrefix: privateEndpointSubnetPrefix
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'law-${resourceToken}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: logRetentionDays
    features: { disableLocalAuth: true }
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'acr${resourceToken}'
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

resource identities 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = [for purpose in ['api', 'web', 'obo']: {
  name: 'id-${purpose}-${resourceToken}'
  location: location
  tags: tags
}]

resource registryReaders 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for identityIndex in range(0, 2): {
  name: guid(registry.id, identities[identityIndex].id, 'acrpull')
  scope: registry
  properties: {
    principalId: identities[identityIndex].properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}]

resource containerEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-${resourceToken}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: { destination: 'azure-monitor' }
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
    vnetConfiguration: {
      infrastructureSubnetId: resourceId('Microsoft.Network/virtualNetworks/subnets', network.name, 'containers')
      internal: false
    }
    peerTrafficConfiguration: { encryption: { enabled: true } }
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'to-workspace'
  scope: containerEnvironment
  properties: {
    workspaceId: workspace.id
    logs: [{ categoryGroup: 'allLogs', enabled: true }]
    metrics: [{ category: 'AllMetrics', enabled: true }]
  }
}

output environmentId string = containerEnvironment.id
output environmentName string = containerEnvironment.name
output environmentDomain string = containerEnvironment.properties.defaultDomain
output registryName string = registry.name
output registryId string = registry.id
output registryServer string = registry.properties.loginServer
output networkId string = network.id
output privateEndpointSubnetId string = resourceId('Microsoft.Network/virtualNetworks/subnets', network.name, 'private-endpoints')
output workspaceId string = workspace.id
output apiIdentity object = {
  resourceId: identities[0].id
  principalId: identities[0].properties.principalId
  clientId: identities[0].properties.clientId
}
output webIdentity object = {
  resourceId: identities[1].id
  principalId: identities[1].properties.principalId
  clientId: identities[1].properties.clientId
}
output oboIdentity object = {
  resourceId: identities[2].id
  principalId: identities[2].properties.principalId
  clientId: identities[2].properties.clientId
}
