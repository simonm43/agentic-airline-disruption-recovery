targetScope = 'resourceGroup'

@description('Azure region of the environment.')
param location string

@description('Workload and environment tags.')
param tags object

@description('Short environment name, for example dev or prd.')
param env string

@description('Short region code, for example swc.')
param regionCode string

@description('Daily ingestion cap of the workspace in GB.')
param dailyCapGb int = 1

resource workspace 'Microsoft.OperationalInsights/workspaces@2025-07-01' = {
  name: 'log-airrec-${env}-${regionCode}'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
    workspaceCapping: {
      dailyQuotaGb: dailyCapGb
    }
    features: {
      disableLocalAuth: true
    }
  }
}

resource component 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-airrec-${env}-${regionCode}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    DisableLocalAuth: true
    IngestionMode: 'LogAnalytics'
    WorkspaceResourceId: workspace.id
    // Provider defaults that what-if predicts; the live component has not stored them yet.
    Flow_Type: 'Bluefield'
    Request_Source: 'rest'
  }
}

output logAnalyticsWorkspaceId string = workspace.id
output logAnalyticsCustomerId string = workspace.properties.customerId
output appInsightsName string = component.name
