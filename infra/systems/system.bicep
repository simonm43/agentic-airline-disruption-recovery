// One airline system (Container App) behind Entra Easy Auth, reading SQL as its managed identity.
targetScope = 'resourceGroup'

@allowed(['schedule', 'maintenance', 'crew', 'booking', 'slots'])
param system string
@description('Image digest in airrec-systems, e.g. sha256:...')
param imageDigest string
@description('Foundry agents and explicitly approved service identities allowed to call this system.')
param allowedCallerOids array
param allowedCallerClientIds array = allowedCallerOids
@description('Object ids admitted to write routes. For Booking, include only ARROW\'s identity.')
param writerOids array = []
@description('Readers, besides the writers, that Booking gives each journey of a rebooking plan: ARROW\'s tools app, which prices plans and never writes (three container apps spec). Empty elsewhere.')
param journeyReaderOids array = []
@description('Short environment name used in resource names, for example dev or prd.')
param env string = 'dev'
@description('Short region code used in regional names, for example swc.')
param regionCode string = 'swc'
@description('Owner tag.')
param owner string = 'simonm43'
@description('Cost attribution tag.')
param costCenter string = 'arrow'
param location string = resourceGroup().location
param environmentName string = 'cae-airrec-${env}-${regionCode}'
param registryName string = 'crairrec${env}${uniqueString(subscription().subscriptionId, resourceGroup().id)}'
param identityName string = system == 'schedule' ? 'id-airrec-systems-${env}' : 'id-airrec-${system}-${env}'
@description('Application (client) ID of the airline systems\' API app registration.')
param apiAppId string = '00000000-0000-4000-8000-000000000017'
param sqlServer string = 'sql-airrec-${env}-${uniqueString(subscription().subscriptionId, resourceGroup().id)}${environment().suffixes.sqlServerHostname}'
param sqlDatabase string = 'airrec-ops'
param appInsightsName string = 'appi-airrec-${env}-${regionCode}'
@allowed([0, 1])
@description('One warm replica for interactive use; zero restores idle scale-to-zero.')
param minReplicas int = 1

var tags = {
  workload: 'airrec'
  environment: env
  owner: owner
  costCenter: costCenter
}
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource managedEnvironment 'Microsoft.App/managedEnvironments@2026-01-01' existing = {
  name: environmentName
}
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: identityName
}
// Traces go here, signed by the system's identity (infra/ops-app/telemetry.bicep grants it).
resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identity.id, acrPullRoleId)
  scope: registry
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

resource app 'Microsoft.App/containerApps@2026-01-01' = {
  name: 'ca-airrec-${system}-${env}'
  location: location
  tags: union(tags, { system: system })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  dependsOn: [acrPull]
  properties: {
    managedEnvironmentId: managedEnvironment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      maxInactiveRevisions: 100
      ingress: {
        external: true
        targetPort: 8080
        transport: 'auto'
        allowInsecure: false
        exposedPort: 0
        traffic: [{ latestRevision: true, weight: 100 }]
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: identity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: system
          image: '${registry.properties.loginServer}/airrec-systems@${imageDigest}'
          resources: { cpu: json('0.5'), memory: '1Gi' }
          // Appended last and only when set, so a redeploy of a running system only adds it.
          env: concat(
            [
              { name: 'SYSTEM', value: system }
              { name: 'SQL_SERVER', value: sqlServer }
              { name: 'SQL_DATABASE', value: sqlDatabase }
              { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
              { name: 'ALLOWED_CALLER_OIDS', value: join(allowedCallerOids, ',') }
              { name: 'WRITER_OIDS', value: join(writerOids, ',') }
              {
                name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
                value: appInsights.properties.ConnectionString
              }
            ],
            empty(journeyReaderOids) ? [] : [{ name: 'JOURNEY_READER_OIDS', value: join(journeyReaderOids, ',') }]
          )
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 8080 }
              periodSeconds: 30
            }
          ]
        }
      ]
      scale: { minReplicas: minReplicas, maxReplicas: 2, cooldownPeriod: 300, pollingInterval: 30 }
    }
  }
}

resource auth 'Microsoft.App/containerApps/authConfigs@2026-01-01' = {
  parent: app
  name: 'current'
  properties: {
    platform: { enabled: true }
    globalValidation: {
      unauthenticatedClientAction: 'Return401'
      excludedPaths: ['/healthz']
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        isAutoProvisioned: false
        registration: {
          clientId: apiAppId
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenant().tenantId}/v2.0'
        }
        validation: {
          allowedAudiences: ['api://${apiAppId}', apiAppId]
          defaultAuthorizationPolicy: {
            // The API stores an omitted allowedApplications as [], which refuses every app token
            // (HTTP 403). An agent identity's client id equals its object id.
            allowedApplications: allowedCallerClientIds
            allowedPrincipals: { identities: allowedCallerOids }
          }
        }
      }
    }
  }
}

output fqdn string = app.properties.configuration.ingress.fqdn
