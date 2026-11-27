// The Foundry project's MCP connections to the five airline systems. Each domain agent signs in
// with its own agent identity (AgenticIdentityToken) for the systems' API audience.
targetScope = 'resourceGroup'

@description('Short environment name used in resource names, for example dev or prd.')
param env string = 'dev'
@description('Default domain of the environment\'s Container Apps environment (its apps\' host suffix).')
param appsDomain string = 'exampledev-00000000.swedencentral.azurecontainerapps.io'
@description('Application (client) ID of the airline systems\' API app registration.')
param systemsAppId string = '00000000-0000-4000-8000-000000000017'
@description('Set false for an incremental recovery-domain rollout that leaves Maintenance untouched.')
param deployMaintenance bool = true

// The Foundry account and project names (tools/env/environment.py): Dev's predate the rule.
resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: env == 'dev' ? 'aif-airrec-chat-dev-${uniqueString(resourceGroup().id)}' : 'aif-airrec-${env}-${uniqueString(subscription().subscriptionId, resourceGroup().id)}'
}
resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: account
  name: env == 'dev' ? 'proj-airrec-chat-dev' : 'proj-airrec-${env}'
}
// Provider defaults are declared so a redeploy compares equal (the what-if gate refuses deletes).
var common = {
  category: 'RemoteTool'
  authType: 'AgenticIdentityToken'
  audience: 'api://${systemsAppId}'
  isSharedToAll: false
  peRequirement: 'NotRequired'
  peStatus: 'NotApplicable'
  useWorkspaceManagedIdentity: false
  metadata: { type: 'custom_MCP' }
}
resource schedule 'Microsoft.CognitiveServices/accounts/projects/connections@2025-12-01' = {
  parent: project
  name: 'schedule-mcp'
  properties: union(common, { target: 'https://ca-airrec-schedule-${env}.${appsDomain}/mcp' })
}
resource connection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-12-01' = if (deployMaintenance) {
  parent: project
  name: 'maintenance-mcp'
  properties: union(common, { target: 'https://ca-airrec-maintenance-${env}.${appsDomain}/mcp' })
}
resource crew 'Microsoft.CognitiveServices/accounts/projects/connections@2025-12-01' = {
  parent: project
  name: 'crew-mcp'
  properties: union(common, { target: 'https://ca-airrec-crew-${env}.${appsDomain}/mcp' })
}
resource booking 'Microsoft.CognitiveServices/accounts/projects/connections@2025-12-01' = {
  parent: project
  name: 'booking-mcp'
  properties: union(common, { target: 'https://ca-airrec-booking-${env}.${appsDomain}/mcp' })
}
resource slots 'Microsoft.CognitiveServices/accounts/projects/connections@2025-12-01' = {
  parent: project
  name: 'slots-mcp'
  properties: union(common, { target: 'https://ca-airrec-slots-${env}.${appsDomain}/mcp' })
}
