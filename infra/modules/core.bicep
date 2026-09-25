@minLength(2)
@maxLength(20)
param resourceToken string
param location string
param tags object
param vnetAddressPrefix string
param containerSubnetPrefix string
param privateEndpointSubnetPrefix string
@description('Subnet for the registry build agents. They run on virtual machine scale sets, so this subnet must carry no delegation and cannot be shared with the Container Apps subnet.')
param buildAgentSubnetPrefix string = '10.42.1.32/27'
@minValue(30)
@maxValue(730)
param logRetentionDays int = 30
@description('Put the registry behind a private endpoint and deny public network access. Requires the Premium tier, and azd remote builds can no longer reach the registry.')
param privateRegistry bool = false
@allowed(['S1', 'S2', 'S3'])
@description('Build agent size. S1 is 2 CPU/3 GB, S2 is 4 CPU/8 GB, S3 is 8 CPU/16 GB.')
param buildAgentTier string = 'S2'
@minValue(1)
@maxValue(10)
param buildAgentCount int = 1

var baseSubnets = [
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
// Only carved out when the registry is private, so the default network plan is unchanged.
var buildAgentSubnets = [
  {
    name: 'build-agents'
    properties: {
      addressPrefix: buildAgentSubnetPrefix
    }
  }
]

resource network 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'vnet-${resourceToken}'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: [vnetAddressPrefix] }
    subnets: privateRegistry ? concat(baseSubnets, buildAgentSubnets) : baseSubnets
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
  // Private endpoints are a Premium-tier feature, so denying public access forces the tier up.
  sku: { name: privateRegistry ? 'Premium' : 'Basic' }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: privateRegistry ? 'Disabled' : 'Enabled'
    // ACR Tasks are not implicitly trusted once public access is denied; this only readmits
    // first-party services such as Defender, not the public internet.
    networkRuleBypassOptions: 'AzureServices'
  }
}

resource registryZone 'Microsoft.Network/privateDnsZones@2020-06-01' = if (privateRegistry) {
  name: 'privatelink${environment().suffixes.acrLoginServer}'
  location: 'global'
  tags: tags
}

resource registryZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = if (privateRegistry) {
  parent: registryZone
  name: 'link-${resourceToken}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: network.id }
  }
}

resource registryEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = if (privateRegistry) {
  name: 'pe-acr-${resourceToken}'
  location: location
  tags: tags
  properties: {
    subnet: { id: resourceId('Microsoft.Network/virtualNetworks/subnets', network.name, 'private-endpoints') }
    privateLinkServiceConnections: [
      {
        name: 'registry'
        properties: {
          privateLinkServiceId: registry.id
          groupIds: ['registry']
        }
      }
    ]
  }
}

resource registryZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = if (privateRegistry) {
  parent: registryEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'registry'
        properties: { privateDnsZoneId: registryZone.id }
      }
    ]
  }
}

// az acr build cannot reach a registry that denies public access, because the shared task fleet
// runs from public IPs. A dedicated pool runs inside this VNet instead, resolving the registry
// through the private endpoint above.
resource buildAgents 'Microsoft.ContainerRegistry/registries/agentPools@2019-06-01-preview' = if (privateRegistry) {
  parent: registry
  name: 'build-agents'
  location: location
  tags: tags
  properties: {
    count: buildAgentCount
    os: 'Linux'
    tier: buildAgentTier
    virtualNetworkSubnetResourceId: resourceId('Microsoft.Network/virtualNetworks/subnets', network.name, 'build-agents')
  }
  dependsOn: [registryZoneGroup]
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
output buildAgentPoolName string = privateRegistry ? 'build-agents' : ''
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
