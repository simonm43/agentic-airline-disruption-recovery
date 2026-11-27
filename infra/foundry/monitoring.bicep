targetScope = 'resourceGroup'

@description('Short environment name used in resource names, for example dev or prd.')
param env string = 'dev'
@description('Short region code used in regional names, for example swc.')
param regionCode string = 'swc'
// The Foundry account and project names (tools/env/environment.py): Dev's predate the rule.
param accountName string = env == 'dev' ? 'aif-airrec-chat-dev-${uniqueString(resourceGroup().id)}' : 'aif-airrec-${env}-${uniqueString(subscription().subscriptionId, resourceGroup().id)}'
param projectName string = env == 'dev' ? 'proj-airrec-chat-dev' : 'proj-airrec-${env}'

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: account
  name: projectName
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: 'appi-airrec-${env}-${regionCode}'
}

var monitoringMetricsPublisher = '3913510d-42f4-4e42-8a64-420c390055eb'
var reader = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'

// Connects the project to Application Insights for Foundry's agent tracing. The component accepts
// Entra ingestion only (local auth disabled), so the connection uses the project's managed
// identity instead of the connection string. The service accepts only ProjectManagedIdentity or
// ApiKey for AppInsights; ProjectManagedIdentity is missing from the Bicep types, hence any():
// https://learn.microsoft.com/azure/foundry/observability/how-to/trace-ingestion-entra-authentication
resource connection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-12-01' = {
  parent: project
  name: appInsights.name
  properties: {
    category: 'AppInsights'
    target: appInsights.id
    authType: any('ProjectManagedIdentity')
    isSharedToAll: false
    // Provider defaults, declared so the what-if gate sees no nested delete.
    peRequirement: 'NotRequired'
    peStatus: 'NotApplicable'
    useWorkspaceManagedIdentity: false
    metadata: {
      ApiType: 'Azure'
      ResourceId: appInsights.id
      // Required with ProjectManagedIdentity (where to send); not a credential, ingestion is Entra.
      ApplicationInsightsConnectionString: appInsights.properties.ConnectionString
    }
  }
}

// Foundry Agent Service ingests prompt agents' server-side traces as the project's identity.
resource projectPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(appInsights.id, project.id, monitoringMetricsPublisher)
  scope: appInsights
  properties: {
    principalId: project.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      monitoringMetricsPublisher
    )
  }
}

// Foundry Evaluations judge the agents' recorded runs from these traces (owner): a trace
// evaluation reads the component as the project's identity. Reader on the component only, as
// documented; AppGenAIContent is not a protected table here, so no privileged role is needed:
// https://learn.microsoft.com/azure/foundry/observability/how-to/evaluation-permissions
resource projectTraceReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(appInsights.id, project.id, reader)
  scope: appInsights
  properties: {
    principalId: project.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', reader)
  }
}
