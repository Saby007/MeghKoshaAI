targetScope = 'resourceGroup'

@minLength(2)
@maxLength(64)
param accountName string

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource modelRouter 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: account
  name: 'model-router'
  sku: {
    name: 'GlobalStandard'
    capacity: 100
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'model-router'
      version: '2025-11-18'
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

output deploymentName string = modelRouter.name
output deploymentId string = modelRouter.id
