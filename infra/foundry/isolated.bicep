targetScope = 'resourceGroup'

@description('Short environment name used in resource names, for example dev or prd.')
param env string = 'dev'
@description('Owner tag.')
param owner string = 'simonm43'
@description('Cost attribution tag.')
param costCenter string = 'arrow'
@description('Tag describing what the account is for.')
param purpose string = 'hosted-chat-proof'
param location string = resourceGroup().location
// The Foundry account and project names (tools/env/environment.py): Dev's predate the rule.
param accountName string = env == 'dev' ? 'aif-airrec-chat-dev-${uniqueString(resourceGroup().id)}' : 'aif-airrec-${env}-${uniqueString(subscription().subscriptionId, resourceGroup().id)}'
param projectName string = env == 'dev' ? 'proj-airrec-chat-dev' : 'proj-airrec-${env}'
@description('Display name of the Foundry project.')
param projectDisplayName string = 'ARROW isolated hosted-chat proof (Dev)'
@description('Description of the Foundry project.')
param projectDescription string = 'Read-only defect assessment. Public endpoint, Entra-only access, no operational integrations.'
@description('Tokens-per-minute units of the gpt-4.1-mini deployment; check the region\'s quota first.')
param miniCapacity int = 600
param operatorPrincipalId string

var tags = {
  workload: 'airrec'
  environment: env
  owner: owner
  costCenter: costCenter
  purpose: purpose
}
var foundryUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: accountName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    allowProjectManagement: true
    customSubDomainName: accountName
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      ipRules: []
      virtualNetworkRules: []
    }
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: account
  name: projectName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: projectDisplayName
    description: projectDescription
  }
  // The account takes one change at a time: created beside the model deployment, the project
  // failed with RequestConflict ("Another operation is in progress").
  dependsOn: [model]
}

resource model 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: account
  name: 'gpt-4.1-mini'
  sku: {
    name: 'Standard'
    // Three concurrent tool exchanges need burst headroom, not just a minute average.
    capacity: miniCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-4.1-mini'
      version: '2025-04-14'
    }
    versionUpgradeOption: 'NoAutoUpgrade'
    raiPolicyName: 'Microsoft.DefaultV2'
  }
}

resource operatorAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, operatorPrincipalId, foundryUserRoleId)
  scope: account
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryUserRoleId)
    principalId: operatorPrincipalId
    principalType: 'User'
  }
}

module hosting 'main.bicep' = {
  name: 'chat-proof-isolated-hosting'
  params: {
    foundryAccountName: account.name
    foundryProjectName: project.name
  }
}

output accountName string = account.name
output projectName string = project.name
output projectEndpoint string = 'https://${account.name}.services.ai.azure.com/api/projects/${project.name}'
output projectPrincipalId string = project.identity.principalId
