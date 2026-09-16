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
@description('Explicitly approved native-export ingress exception; never bypass an Azure Policy denial.')
param allowNativeExportTrustedServices bool = false

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
    publicNetworkAccess: allowNativeExportTrustedServices ? 'Enabled' : 'Disabled'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: allowNativeExportTrustedServices ? 'AzureServices' : 'None'
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
    deleteRetentionPolicy: { enabled: false }
    containerDeleteRetentionPolicy: { enabled: false }
    isVersioningEnabled: false
    changeFeed: { enabled: false }
  }
}

resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [for name in ['cost-exports', 'report-snapshots', 'control-state']: {
  parent: blobService
  name: name
  properties: { publicAccess: 'None' }
}]

resource processorIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-processor-${resourceToken}'
  location: location
  tags: tags
}

resource apiReaders 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for containerIndex in range(0, 2): {
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

// /api/report POSTs a new snapshot under the API's own identity (not the worker's); the
// blob-reader grant above is not sufficient for that write.
resource apiReportWriter 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(containers[1].id, apiPrincipalId, 'report-writer')
  scope: containers[1]
  properties: {
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  }
}

// Azure's own native FOCUS export creation grants Storage Blob Data Contributor to the export's
// system-assigned identity on the destination container as a side effect - but only if the caller
// creating the export (the API identity) itself has role-assignment write on this account. This
// custom role is scoped to only this app's own storage account, never customer resources.
resource storageSetupRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(storage.id, 'export-storage-setup')
  properties: {
    roleName: 'Cost Assessment Export Storage Setup ${resourceToken}'
    description: 'Lets the API identity complete the native FOCUS export destination role assignment on this account only. No keys, deletion or data actions.'
    type: 'CustomRole'
    assignableScopes: [storage.id]
    permissions: [{
      actions: [
        'Microsoft.Storage/storageAccounts/read'
        'Microsoft.Storage/storageAccounts/write'
        'Microsoft.Storage/storageAccounts/blobServices/containers/read'
        'Microsoft.Authorization/permissions/read'
        'Microsoft.Authorization/roleAssignments/read'
        'Microsoft.Authorization/roleAssignments/write'
      ]
      notActions: []
      dataActions: []
      notDataActions: []
    }]
  }
}

resource apiStorageSetupAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, apiPrincipalId, storageSetupRole.id)
  scope: storage
  properties: {
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: storageSetupRole.id
  }
}

resource processorWriters 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for containerIndex in range(0, 3): {
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
