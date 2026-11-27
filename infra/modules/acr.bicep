targetScope = 'resourceGroup'

@description('Azure region of the environment.')
param location string

@description('Workload and environment tags.')
param tags object

@description('Short environment name, for example dev or prd.')
param env string

@description('Verified developer Entra object ID that pushes images.')
param developerObjectId string

var registryName = 'crairrec${env}${uniqueString(subscription().subscriptionId, resourceGroup().id)}'
// Built-in role: AcrPush. The apps' AcrPull assignments are in their own templates.
var acrPushRoleId = '8311e382-0749-4cb8-b61a-304f252e45ec'

// Documented exception (spec section 11.3): Basic has no private link, so the registry stays
// public but accepts only Microsoft Entra RBAC; admin user and anonymous pull are off.
resource registry 'Microsoft.ContainerRegistry/registries@2025-04-01' = {
  name: registryName
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    anonymousPullEnabled: false
    publicNetworkAccess: 'Enabled'
    // Provider defaults, declared so what-if reports no change against live Dev.
    dataEndpointEnabled: false
    encryption: {
      status: 'disabled'
    }
  }
}

resource developerAcrPush 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, developerObjectId, acrPushRoleId)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPushRoleId)
    principalId: developerObjectId
    principalType: 'User'
  }
}

output name string = registry.name
output loginServer string = registry.properties.loginServer
