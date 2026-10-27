param resourceToken string
param location string
param tags object
param networkId string
param privateEndpointSubnetId string
param skuName string = 'Standard_D2ds_v5'
@allowed(['GeneralPurpose', 'MemoryOptimized'])
param skuTier string = 'GeneralPurpose'
@allowed(['16', '17'])
param version string = '16'
@minValue(32)
@maxValue(16384)
param storageSizeGB int = 128

resource bootstrapIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-db-bootstrap-${resourceToken}'
  location: location
  tags: tags
}

resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: 'pg-${resourceToken}'
  location: location
  tags: tags
  sku: { name: skuName, tier: skuTier }
  properties: {
    version: version
    createMode: 'Default'
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Disabled'
      tenantId: tenant().tenantId
    }
    network: { publicNetworkAccess: 'Disabled' }
    storage: { storageSizeGB: storageSizeGB, autoGrow: 'Enabled' }
    backup: { backupRetentionDays: 7, geoRedundantBackup: 'Disabled' }
    highAvailability: { mode: 'Disabled' }
  }
}

module administrator './postgres-admin.bicep' = {
  name: 'postgres-administrator'
  params: {
    serverName: server.name
    principalId: bootstrapIdentity.properties.principalId
    principalName: bootstrapIdentity.name
  }
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: server
  name: 'meghkosha'
  properties: { charset: 'UTF8', collation: 'en_US.utf8' }
}

resource extensions 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: server
  name: 'azure.extensions'
  properties: { source: 'user-override', value: 'pg_trgm' }
}

resource zone 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: 'privatelink.postgres.database.azure.com'
  location: 'global'
  tags: tags
}

resource link 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: zone
  name: 'link-${resourceToken}'
  location: 'global'
  properties: { registrationEnabled: false, virtualNetwork: { id: networkId } }
}

resource endpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pe-postgres-${resourceToken}'
  location: location
  tags: tags
  properties: {
    subnet: { id: privateEndpointSubnetId }
    privateLinkServiceConnections: [{
      name: 'postgres'
      properties: { privateLinkServiceId: server.id, groupIds: ['postgresqlServer'] }
    }]
  }
}

resource zoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: endpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [{ name: 'postgres', properties: { privateDnsZoneId: zone.id } }]
  }
}

output host string = server.properties.fullyQualifiedDomainName
output databaseName string = database.name
output bootstrapIdentity object = {
  resourceId: bootstrapIdentity.id
  clientId: bootstrapIdentity.properties.clientId
  principalId: bootstrapIdentity.properties.principalId
  name: bootstrapIdentity.name
}
