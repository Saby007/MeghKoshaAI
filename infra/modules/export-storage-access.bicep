param storageAccountName string
param apiPrincipalId string
param roleDefinitionId string
param enableApiStorageAccountContributor bool = false

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource apiStorageSetupAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, apiPrincipalId, roleDefinitionId)
  scope: storage
  properties: {
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: roleDefinitionId
    description: 'Operator-approved setup authority on the export destination account only.'
  }
}

resource apiStorageAccountContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (enableApiStorageAccountContributor) {
  name: guid(storage.id, apiPrincipalId, '17d1049b-9a84-46fb-8f53-869881c3d3ab')
  scope: storage
  properties: {
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '17d1049b-9a84-46fb-8f53-869881c3d3ab')
    description: 'Operator-approved API-only compatibility grant at this storage account.'
  }
}

output assignmentId string = apiStorageSetupAccess.id
output storageAccountContributorAssignmentId string = enableApiStorageAccountContributor ? apiStorageAccountContributor.id : ''
