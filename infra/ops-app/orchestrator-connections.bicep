targetScope = 'resourceGroup'

@description('Short environment name used in resource names, for example dev or prd.')
param env string = 'dev'
@description('Default domain of the environment\'s Container Apps environment (its apps\' host suffix).')
param appsDomain string = 'exampledev-00000000.swedencentral.azurecontainerapps.io'
@description('Application (client) ID of the ARROW app registration, the audience of ARROW\'s tools.')
param arrowAppId string = '00000000-0000-4000-8000-000000000007'
@description('Name of the MCP connection to ARROW\'s tools; Dev\'s predates generic resource names.')
param toolsConnectionName string = 'arrow-tools'

// The Foundry account and project names (tools/env/environment.py): Dev's predate the rule.
var accountName = env == 'dev' ? 'aif-airrec-chat-dev-${uniqueString(resourceGroup().id)}' : 'aif-airrec-${env}-${uniqueString(subscription().subscriptionId, resourceGroup().id)}'
var projectName = env == 'dev' ? 'proj-airrec-chat-dev' : 'proj-airrec-${env}'

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}
resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: account
  name: projectName
}
// Orchestrated recovery (spec D17/D18): the orchestrator prompt agent calls ARROW's tools on the
// tools app (MCP, its own agent identity; Easy Auth admits it; three container apps spec) and the
// five domain agents over GA A2A v1.0 (Entra; the orchestrator identity holds Foundry Agent
// Consumer on the project).
// Provider defaults are declared so a redeploy compares equal (the what-if gate refuses deletes).
resource arrowTools 'Microsoft.CognitiveServices/accounts/projects/connections@2025-12-01' = {
  parent: project
  name: toolsConnectionName
  properties: {
    category: 'RemoteTool'
    authType: 'AgenticIdentityToken'
    audience: 'api://${arrowAppId}'
    target: 'https://ca-airrec-tools-${env}.${appsDomain}/mcp'
    isSharedToAll: false
    peRequirement: 'NotRequired'
    peStatus: 'NotApplicable'
    useWorkspaceManagedIdentity: false
    metadata: { type: 'custom_MCP' }
  }
}
var domainAgents = {
  'a2a-detection': 'disruption-detection-agent'
  'a2a-aircraft': 'aircraft-recovery-agent'
  'a2a-crew': 'crew-recovery-agent'
  'a2a-passenger': 'passenger-reaccommodation-agent'
  'a2a-slots': 'slot-negotiation-agent'
}
resource a2a 'Microsoft.CognitiveServices/accounts/projects/connections@2025-12-01' = [for item in items(domainAgents): {
  parent: project
  name: item.key
  properties: {
    category: 'RemoteA2A'
    authType: 'AgenticIdentityToken'
    audience: 'https://ai.azure.com'
    target: 'https://${accountName}.services.ai.azure.com/api/projects/${projectName}/agents/${item.value}/endpoint/protocols/a2a'
    isSharedToAll: false
    peRequirement: 'NotRequired'
    peStatus: 'NotApplicable'
    useWorkspaceManagedIdentity: false
    metadata: {}
  }
}]
