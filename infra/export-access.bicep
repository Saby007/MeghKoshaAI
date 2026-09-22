targetScope = 'subscription'

@description('Existing resource group containing the export destination. No resources are provisioned by this template.')
param storageResourceGroupName string
param storageAccountName string
@minLength(36)
@maxLength(36)
param apiPrincipalId string
@minLength(36)
@maxLength(36)
param workerPrincipalId string

@description('Explicit operator approval for broader built-in Cost Management Contributor access for the API only.')
param enableApiCostManagementContributor bool = false

@description('Explicit operator approval for built-in Storage Account Contributor access for the API at the destination account only.')
param enableApiStorageAccountContributor bool = false

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
  scope: resourceGroup(storageResourceGroupName)
}

resource apiExportRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(subscription().id, storage.id, 'cost-assessment-api-export-configurator')
  properties: {
    roleName: 'Cost Assessment Export Configurator ${storageAccountName}'
    description: 'Read, configure, and run Cost Management exports on the selected subscription. No export deletion or general resource management.'
    type: 'CustomRole'
    assignableScopes: [subscription().id]
    permissions: [{
      actions: [
        'Microsoft.Authorization/permissions/read'
        'Microsoft.CostManagement/exports/read'
        'Microsoft.CostManagement/exports/write'
        'Microsoft.CostManagement/exports/action'
        'Microsoft.CostManagement/exports/run/action'
      ]
      notActions: []
      dataActions: []
      notDataActions: []
    }]
  }
}

resource workerExportRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(subscription().id, storage.id, 'cost-assessment-worker-export-executor')
  properties: {
    roleName: 'Cost Assessment Export Executor ${storageAccountName}'
    description: 'Read and run Cost Management exports on the selected subscription. No export creation, modification, deletion, or cost queries.'
    type: 'CustomRole'
    assignableScopes: [subscription().id]
    permissions: [{
      actions: [
        'Microsoft.Authorization/permissions/read'
        'Microsoft.CostManagement/exports/read'
        'Microsoft.CostManagement/exports/action'
        'Microsoft.CostManagement/exports/run/action'
      ]
      notActions: []
      dataActions: []
      notDataActions: []
    }]
  }
}

resource storageSetupRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(subscription().id, storage.id, 'cost-assessment-export-storage-setup')
  properties: {
    roleName: 'Cost Assessment Export Storage Setup ${storageAccountName}'
    description: 'Export setup for this destination account only: account read/write and role-assignment read/write. No keys, deletion, or data actions.'
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

resource apiExportAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(subscription().id, apiPrincipalId, apiExportRole.id)
  properties: {
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: apiExportRole.id
    description: 'Operator-approved API export setup access.'
  }
}

resource workerExportAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(subscription().id, workerPrincipalId, workerExportRole.id)
  properties: {
    principalId: workerPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: workerExportRole.id
    description: 'Operator-approved worker export execution access.'
  }
}

resource apiCostManagementContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (enableApiCostManagementContributor) {
  name: guid(subscription().id, apiPrincipalId, '434105ed-43f6-45c7-a02f-909b2ba83430')
  properties: {
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '434105ed-43f6-45c7-a02f-909b2ba83430')
    description: 'Operator-approved API-only Cost Management compatibility grant.'
  }
}

module apiStorageSetupAccess './modules/export-storage-access.bicep' = {
  name: 'cost-assessment-export-storage-access'
  scope: resourceGroup(storageResourceGroupName)
  params: {
    storageAccountName: storageAccountName
    apiPrincipalId: apiPrincipalId
    roleDefinitionId: storageSetupRole.id
    enableApiStorageAccountContributor: enableApiStorageAccountContributor
  }
}

output apiExportRoleId string = apiExportRole.id
output workerExportRoleId string = workerExportRole.id
output storageSetupRoleId string = storageSetupRole.id
output apiExportAssignmentId string = apiExportAccess.id
output workerExportAssignmentId string = workerExportAccess.id
output apiStorageSetupAssignmentId string = apiStorageSetupAccess.outputs.assignmentId
output apiCostManagementContributorAssignmentId string = enableApiCostManagementContributor ? apiCostManagementContributor.id : ''
output apiStorageAccountContributorAssignmentId string = apiStorageSetupAccess.outputs.storageAccountContributorAssignmentId
