@minLength(2)
@maxLength(20)
param resourceToken string
param location string
param tags object
param networkId string
param privateEndpointSubnetId string
param apiPrincipalId string
@allowed(['Standard_LRS', 'Standard_ZRS'])
param storageSku string = 'Standard_LRS'
@description('Enable blob versioning and blob/container soft delete for subscriptions that audit data-protection settings.')
param blobDataProtection bool = false
@minValue(1)
@maxValue(365)
param blobRetentionDays int = 7
@description('Days to keep daily month-to-date FOCUS snapshots (cost-exports/focus-daily/).')
@minValue(7)
@maxValue(365)
param dailyExportRetentionDays int = 60
@description('Days to keep closed-month FOCUS exports (cost-exports/focus/); covers the longest 6-month window.')
@minValue(190)
@maxValue(3650)
param closedMonthRetentionDays int = 214

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'st${resourceToken}'
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: storageSku }
  properties: {
    isHnsEnabled: false
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    allowCrossTenantReplication: false
    defaultToOAuthAuthentication: true
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'None'
      ipRules: []
      virtualNetworkRules: []
    }
    encryption: {
      keySource: 'Microsoft.Storage'
      requireInfrastructureEncryption: true
      services: { blob: { enabled: true, keyType: 'Account' } }
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    deleteRetentionPolicy: blobDataProtection ? { enabled: true, days: blobRetentionDays } : { enabled: false }
    containerDeleteRetentionPolicy: blobDataProtection ? { enabled: true, days: blobRetentionDays } : { enabled: false }
    isVersioningEnabled: blobDataProtection
    changeFeed: { enabled: false }
  }
}

resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [for name in ['focus-uploads', 'report-snapshots', 'control-state']: {
  parent: blobService
  name: name
  properties: { publicAccess: 'None' }
}]

// With versioning on, a lifecycle delete keeps a previous version; expire those too.
var expireVersions = blobDataProtection ? { version: { delete: { daysAfterCreationGreaterThan: blobRetentionDays } } } : {}

resource exportRetention 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'focus-daily-snapshots'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: { blobTypes: ['blockBlob'], prefixMatch: ['cost-exports/focus-daily/'] }
            actions: union({ baseBlob: { delete: { daysAfterModificationGreaterThan: dailyExportRetentionDays } } }, expireVersions)
          }
        }
        {
          name: 'focus-closed-months'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: { blobTypes: ['blockBlob'], prefixMatch: ['cost-exports/focus/'] }
            actions: union({ baseBlob: { delete: { daysAfterModificationGreaterThan: closedMonthRetentionDays } } }, expireVersions)
          }
        }
      ]
    }
  }
  dependsOn: [containers]
}

resource processorIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-processor-${resourceToken}'
  location: location
  tags: tags
}

resource apiReaders 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for containerIndex in range(1, 1): {
  name: guid(containers[containerIndex].id, apiPrincipalId, 'blob-reader')
  scope: containers[containerIndex]
  properties: {
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1')
  }
}]

resource apiMetadataWriter 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(containers[2].id, apiPrincipalId, 'metadata-writer')
  scope: containers[2]
  properties: {
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  }
}

resource apiUploadsWriter 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(containers[0].id, apiPrincipalId, 'focus-upload-writer')
  scope: containers[0]
  properties: {
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  }
}

resource workerUploadReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(containers[0].id, processorIdentity.id, 'focus-upload-reader')
  scope: containers[0]
  properties: {
    principalId: processorIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1')
  }
}

resource processorWriters 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for containerIndex in range(1, 2): {
  name: guid(containers[containerIndex].id, processorIdentity.id, 'processor-writer')
  scope: containers[containerIndex]
  properties: {
    principalId: processorIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  }
}]

var storageServices = ['blob']

resource privateZones 'Microsoft.Network/privateDnsZones@2020-06-01' = [for service in storageServices: {
  name: 'privatelink.${service}.${environment().suffixes.storage}'
  location: 'global'
  tags: tags
}]

resource zoneLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = [for (service, serviceIndex) in storageServices: {
  parent: privateZones[serviceIndex]
  name: 'link-${resourceToken}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: networkId }
  }
}]

resource endpoints 'Microsoft.Network/privateEndpoints@2024-05-01' = [for service in storageServices: {
  name: 'pe-${service}-${resourceToken}'
  location: location
  tags: tags
  properties: {
    subnet: { id: privateEndpointSubnetId }
    privateLinkServiceConnections: [
      {
        name: service
        properties: {
          privateLinkServiceId: storage.id
          groupIds: [service]
        }
      }
    ]
  }
}]

resource zoneGroups 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = [for (service, serviceIndex) in storageServices: {
  parent: endpoints[serviceIndex]
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: service
        properties: { privateDnsZoneId: privateZones[serviceIndex].id }
      }
    ]
  }
}]

output storageName string = storage.name
output storageResourceId string = storage.id
output storageUrl string = storage.properties.primaryEndpoints.blob
output exportContainer string = 'cost-exports'
output reportContainer string = 'report-snapshots'
output controlContainer string = 'control-state'
output processorIdentity object = {
  resourceId: processorIdentity.id
  clientId: processorIdentity.properties.clientId
  principalId: processorIdentity.properties.principalId
}
