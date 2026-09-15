@minLength(2)
@maxLength(20)
param resourceToken string
param location string
param tags object
param environmentId string
param registryServer string
param apiIdentity object
param webIdentity object
param oboIdentity object
@minLength(1)
param apiImage string
@minLength(1)
param webImage string
param apiClientId string
param webClientId string
param apiEnvironment array = []
@minValue(0)
@maxValue(3)
param minimumReplicas int = 1

resource api 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ca-api-${resourceToken}'
  location: location
  tags: union(tags, { 'azd-service-name': 'api' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${apiIdentity.resourceId}': {}
      '${oboIdentity.resourceId}': {}
    }
  }
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: false
        allowInsecure: false
        targetPort: 8000
        transport: 'http'
      }
      registries: [{ server: registryServer, identity: apiIdentity.resourceId }]
    }
    template: {
      containers: [
        {
          name: 'api'
          image: apiImage
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: concat([
            { name: 'AZURE_CLIENT_ID', value: apiIdentity.clientId }
            { name: 'AZURE_TENANT_ID', value: tenant().tenantId }
            { name: 'MEGHKOSHA_API_CLIENT_ID', value: apiClientId }
            { name: 'MEGHKOSHA_WEB_CLIENT_ID', value: webClientId }
            { name: 'MEGHKOSHA_OBO_MANAGED_IDENTITY_CLIENT_ID', value: oboIdentity.clientId }
            { name: 'MEGHKOSHA_ACTION_WRITES_PAUSED', value: 'true' }
          ], apiEnvironment)
          probes: [
            {
              type: 'Startup'
              httpGet: { path: '/api/health', port: 8000, scheme: 'HTTP' }
              periodSeconds: 10
              timeoutSeconds: 3
              failureThreshold: 30
            }
            {
              type: 'Liveness'
              httpGet: { path: '/api/health', port: 8000, scheme: 'HTTP' }
              periodSeconds: 30
              timeoutSeconds: 3
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: { path: '/api/health', port: 8000, scheme: 'HTTP' }
              periodSeconds: 10
              timeoutSeconds: 3
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: { minReplicas: minimumReplicas, maxReplicas: 3 }
    }
  }
}

resource web 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ca-web-${resourceToken}'
  location: location
  tags: union(tags, { 'azd-service-name': 'web' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${webIdentity.resourceId}': {} }
  }
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        allowInsecure: false
        targetPort: 8080
        transport: 'http'
      }
      registries: [{ server: registryServer, identity: webIdentity.resourceId }]
    }
    template: {
      containers: [
        {
          name: 'web'
          image: webImage
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: [{ name: 'API_HOST', value: api.properties.configuration.ingress.fqdn }]
          probes: [
            {
              type: 'Startup'
              httpGet: { path: '/healthz', port: 8080, scheme: 'HTTP' }
              periodSeconds: 10
              timeoutSeconds: 3
              failureThreshold: 30
            }
            {
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 8080, scheme: 'HTTP' }
              periodSeconds: 30
              timeoutSeconds: 3
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: { path: '/healthz', port: 8080, scheme: 'HTTP' }
              periodSeconds: 10
              timeoutSeconds: 3
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: { minReplicas: minimumReplicas, maxReplicas: 3 }
    }
  }
}

output apiName string = api.name
output apiFqdn string = api.properties.configuration.ingress.fqdn
output webName string = web.name
output webOrigin string = 'https://${web.properties.configuration.ingress.fqdn}'
