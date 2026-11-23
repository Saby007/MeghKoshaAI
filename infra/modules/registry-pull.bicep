param registryName string
@description('Identities that pull images from the private registry through managed identity.')
param pullers array

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

// Granted during Foundation, well before an image-backed app or job is created. Role
// assignments propagate asynchronously, and Container Apps validates the pull at creation.
resource pulls 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for puller in pullers: {
  name: guid(registry.id, puller.resourceId, 'acrpull')
  scope: registry
  properties: {
    principalId: puller.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}]
