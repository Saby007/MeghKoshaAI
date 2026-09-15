@minLength(2)
@maxLength(20)
param resourceToken string
param location string
param tags object
param environmentId string
param registryName string
param processorIdentity object
param processorImage string
param storageUrl string
param storageResourceId string
param exportName string
param schedule string = '*/15 * * * *'

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource registryReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, processorIdentity.resourceId, 'acrpull')
  scope: registry
  properties: {
    principalId: processorIdentity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}

resource processor 'Microsoft.App/jobs@2024-03-01' = {
  name: 'job-${resourceToken}'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${processorIdentity.resourceId}': {} }
  }
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Schedule'
      replicaTimeout: 3600
      replicaRetryLimit: 0
      scheduleTriggerConfig: {
        cronExpression: schedule
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: [{ server: registry.properties.loginServer, identity: processorIdentity.resourceId }]
    }
    template: {
      containers: [
        {
          name: 'processor'
          image: processorImage
          command: ['/usr/bin/python', '-m', 'jobs.scheduler']
          resources: { cpu: json('1.0'), memory: '2Gi' }
          env: [
            { name: 'AZURE_CLIENT_ID', value: processorIdentity.clientId }
            { name: 'AZURE_TENANT_ID', value: tenant().tenantId }
            { name: 'APP_SCHEDULER_ENABLED', value: 'true' }
            { name: 'COST_EXPORT_STORAGE_URL', value: storageUrl }
            { name: 'COST_EXPORT_STORAGE_RESOURCE_ID', value: storageResourceId }
            { name: 'COST_EXPORT_NAME', value: exportName }
            { name: 'COST_EXPORT_CONTAINER', value: 'cost-exports' }
            { name: 'CONTROL_STATE_CONTAINER', value: 'control-state' }
            { name: 'MEGHKOSHA_AI_ENABLED', value: 'false' }
          ]
        }
      ]
    }
  }
  dependsOn: [registryReader]
}

output name string = processor.name
