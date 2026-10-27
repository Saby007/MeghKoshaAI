param resourceToken string
param location string
param tags object
param environmentId string
param registryName string
param image string
param workerIdentity object
param bootstrapIdentity object
param apiPrincipalId string
param databaseHost string
param databaseName string
param workerEnvironment array
param enableWorker bool
param grantWorkerRegistryPull bool = true
@allowed(['1.0', '2.0', '4.0'])
param workerCpu string = '4.0'
@minValue(1)
@maxValue(4)
param workerReplicas int = 1

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource workerPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (grantWorkerRegistryPull) {
  name: guid(registry.id, workerIdentity.resourceId, 'acrpull')
  scope: registry
  properties: {
    principalId: workerIdentity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}

resource bootstrapPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, bootstrapIdentity.resourceId, 'acrpull')
  scope: registry
  properties: {
    principalId: bootstrapIdentity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}

resource worker 'Microsoft.App/containerApps@2024-03-01' = if (enableWorker) {
  name: 'ca-assessment-${resourceToken}'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${workerIdentity.resourceId}': {} }
  }
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [{ server: registry.properties.loginServer, identity: workerIdentity.resourceId }]
    }
    template: {
      containers: [{
        name: 'assessment'
        image: image
        command: ['/usr/bin/python', '-m', 'jobs.assessments']
        resources: {
          cpu: json(workerCpu)
          memory: workerCpu == '1.0' ? '2Gi' : workerCpu == '2.0' ? '4Gi' : '8Gi'
        }
        env: concat([
          { name: 'AZURE_CLIENT_ID', value: workerIdentity.clientId }
          { name: 'AZURE_TENANT_ID', value: tenant().tenantId }
          { name: 'ASSESSMENT_DB_USER', value: 'meghkosha_worker' }
          { name: 'ASSESSMENT_WORKER_PROCESS', value: 'true' }
          { name: 'MEGHKOSHA_AI_ENABLED', value: 'false' }
        ], workerEnvironment)
      }]
      scale: { minReplicas: workerReplicas, maxReplicas: workerReplicas }
    }
  }
  dependsOn: [workerPull]
}

resource bootstrap 'Microsoft.App/jobs@2024-03-01' = {
  name: 'job-db-bootstrap-${resourceToken}'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${bootstrapIdentity.resourceId}': {} }
  }
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 300
      replicaRetryLimit: 0
      manualTriggerConfig: { parallelism: 1, replicaCompletionCount: 1 }
      registries: [{ server: registry.properties.loginServer, identity: bootstrapIdentity.resourceId }]
    }
    template: {
      containers: [{
        name: 'bootstrap'
        image: image
        command: ['/usr/bin/python', '-m', 'jobs.assessment_bootstrap']
        resources: { cpu: json('0.5'), memory: '1Gi' }
        env: [
          { name: 'AZURE_CLIENT_ID', value: bootstrapIdentity.clientId }
          { name: 'ASSESSMENT_DB_HOST', value: databaseHost }
          { name: 'ASSESSMENT_DB_NAME', value: 'postgres' }
          { name: 'ASSESSMENT_TARGET_DB_NAME', value: databaseName }
          { name: 'ASSESSMENT_DB_USER', value: bootstrapIdentity.name }
          { name: 'ASSESSMENT_API_PRINCIPAL_ID', value: apiPrincipalId }
          { name: 'ASSESSMENT_WORKER_PRINCIPAL_ID', value: workerIdentity.principalId }
        ]
      }]
    }
  }
  dependsOn: [bootstrapPull]
}

output workerName string = enableWorker ? worker!.name : ''
output bootstrapJobName string = bootstrap.name
