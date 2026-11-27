targetScope = 'resourceGroup'

// The project's model access. The hosted orchestrator's registry pull permission and connection
// were removed (D111); ARROW's agents are prompt agents and pull no image.

@description('Short environment name used in resource names, for example dev or prd.')
param env string = 'dev'
// The Foundry account and project names (tools/env/environment.py): Dev's predate the rule.
param foundryAccountName string = env == 'dev' ? 'aif-airrec-chat-dev-${uniqueString(resourceGroup().id)}' : 'aif-airrec-${env}-${uniqueString(subscription().subscriptionId, resourceGroup().id)}'
param foundryProjectName string = env == 'dev' ? 'proj-airrec-chat-dev' : 'proj-airrec-${env}'

var foundryUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: account
  name: foundryProjectName
}

resource projectModelUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, project.id, foundryUserRoleId)
  scope: account
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryUserRoleId)
    principalId: project.identity.principalId
    principalType: 'ServicePrincipal'
  }
}
