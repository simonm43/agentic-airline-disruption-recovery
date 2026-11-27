targetScope = 'resourceGroup'

@description('Short environment name used in resource names, for example dev or prd.')
param env string = 'dev'
@description('Instance identity (principal ID) of the agent that calls other agents: the orchestrator or the Case Assistant.')
param orchestratorPrincipalId string

// The Foundry account and project names (tools/env/environment.py): Dev's predate the rule.
resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: env == 'dev' ? 'aif-airrec-chat-dev-${uniqueString(resourceGroup().id)}' : 'aif-airrec-${env}-${uniqueString(subscription().subscriptionId, resourceGroup().id)}'
}
resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: account
  name: env == 'dev' ? 'proj-airrec-chat-dev' : 'proj-airrec-${env}'
}

// Foundry Agent Consumer: call agent endpoints (incoming A2A), nothing else. Agent-level scope is
// a Foundry data-plane path, not an ARM resource, so the project is the narrowest IaC scope.
var foundryAgentConsumer = 'eed3b665-ab3a-47b6-8f48-c9382fb1dad6'
resource consumer 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: project
  name: guid(project.id, orchestratorPrincipalId, foundryAgentConsumer)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      foundryAgentConsumer
    )
    principalId: orchestratorPrincipalId
    principalType: 'ServicePrincipal'
  }
}
