// Who may send telemetry to Application Insights: Monitoring Metrics Publisher on
// the environment's component for ARROW's three apps' and the airline systems' managed
// identities. The component accepts Entra-signed ingestion only (local auth disabled), so this
// role is what lets each app's Azure Monitor OpenTelemetry exporter send. The day jobs run as the
// systems identity. Foundry's own trace ingestion (the project identity) is in
// infra/foundry/monitoring.bicep.
targetScope = 'resourceGroup'

@description('Short environment name used in resource names, for example dev or prd.')
param env string = 'dev'
@description('Short region code used in regional names, for example swc.')
param regionCode string = 'swc'
param appInsightsName string = 'appi-airrec-${env}-${regionCode}'
param identityNames array = [
  'id-airrec-ops-${env}'
  'id-airrec-systems-${env}'
  'id-airrec-maintenance-${env}'
  'id-airrec-crew-${env}'
  'id-airrec-booking-${env}'
  'id-airrec-slots-${env}'
  'id-airrec-operator-${env}'
  'id-airrec-tools-${env}'
]

var monitoringMetricsPublisher = '3913510d-42f4-4e42-8a64-420c390055eb'

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

resource identities 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = [
  for name in identityNames: {
    name: name
  }
]

resource publishers 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for (name, i) in identityNames: {
    name: guid(appInsights.id, identities[i].id, monitoringMetricsPublisher)
    scope: appInsights
    properties: {
      principalId: identities[i].properties.principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId(
        'Microsoft.Authorization/roleDefinitions',
        monitoringMetricsPublisher
      )
    }
  }
]

// The operator app reads this component's own telemetry (owner): to open a case's end-to-end
// transaction view it looks up the case's first item id. Read only, scoped to the component; the
// workspace allows resource-scoped reads (enableLogAccessUsingOnlyResourcePermissions). An
// environment that ran ARROW as one app keeps the workers' (id-airrec-ops) earlier Monitoring
// Reader until a later clean-up removes it (three container apps spec §3).
param traceReaderName string = 'id-airrec-operator-${env}'

var monitoringReader = '43d0d8ad-25c7-4714-9337-8ba259a9fe05'

resource traceReader 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: traceReaderName
}

resource traceReading 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(appInsights.id, traceReader.id, monitoringReader)
  scope: appInsights
  properties: {
    principalId: traceReader.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringReader)
  }
}
