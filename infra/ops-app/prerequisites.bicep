targetScope = 'resourceGroup'

@description('Short environment name used in resource names, for example dev or prd.')
param env string = 'dev'
@description('Owner tag.')
param owner string = 'simonm43'
@description('Cost attribution tag.')
param costCenter string = 'arrow'
param registryName string = 'crairrec${env}${uniqueString(subscription().subscriptionId, resourceGroup().id)}'
// The Foundry account and project names (tools/env/environment.py): Dev's predate the rule.
param foundryAccountName string = env == 'dev' ? 'aif-airrec-chat-dev-${uniqueString(resourceGroup().id)}' : 'aif-airrec-${env}-${uniqueString(subscription().subscriptionId, resourceGroup().id)}'
var tags = {
  workload: 'airrec'
  environment: env
  owner: owner
  costCenter: costCenter
}
resource ops 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-airrec-ops-${env}'
  location: resourceGroup().location
  tags: tags
}
// ARROW's three apps (three container apps spec): id-airrec-ops runs the workers, the only
// writer; the operator app and the agents' tools app run as their own identities, which only
// read the airline systems.
resource operator 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-airrec-operator-${env}'
  location: resourceGroup().location
  tags: tags
}
resource tools 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-airrec-tools-${env}'
  location: resourceGroup().location
  tags: tags
}
// Schedule (operations control) and the day jobs run as this identity.
resource systems 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-airrec-systems-${env}'
  location: resourceGroup().location
  tags: tags
}
resource maintenance 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-airrec-maintenance-${env}'
  location: resourceGroup().location
  tags: tags
}
resource crew 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-airrec-crew-${env}'
  location: resourceGroup().location
  tags: tags
}
resource booking 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-airrec-booking-${env}'
  location: resourceGroup().location
  tags: tags
}
resource slots 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-airrec-slots-${env}'
  location: resourceGroup().location
  tags: tags
}
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}
resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}
resource pull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, ops.id, '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  scope: registry
  properties: {
    principalId: ops.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}
resource invoke 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, ops.id, '53ca6127-db72-4b80-b1b0-d745d6d5456d')
  scope: foundry
  properties: {
    principalId: ops.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '53ca6127-db72-4b80-b1b0-d745d6d5456d')
  }
}
resource operatorPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, operator.id, '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  scope: registry
  properties: {
    principalId: operator.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}
resource toolsPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, tools.id, '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  scope: registry
  properties: {
    principalId: tools.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}
// The operator app runs the Case Assistant and reads the agents' prompts for the trail.
resource operatorInvoke 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, operator.id, '53ca6127-db72-4b80-b1b0-d745d6d5456d')
  scope: foundry
  properties: {
    principalId: operator.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '53ca6127-db72-4b80-b1b0-d745d6d5456d')
  }
}
output opsClientId string = ops.properties.clientId
output opsPrincipalId string = ops.properties.principalId
output operatorClientId string = operator.properties.clientId
output operatorPrincipalId string = operator.properties.principalId
output toolsClientId string = tools.properties.clientId
output toolsPrincipalId string = tools.properties.principalId
output systemsClientId string = systems.properties.clientId
output systemsPrincipalId string = systems.properties.principalId
output maintenanceClientId string = maintenance.properties.clientId
output maintenancePrincipalId string = maintenance.properties.principalId
output crewClientId string = crew.properties.clientId
output crewPrincipalId string = crew.properties.principalId
output bookingClientId string = booking.properties.clientId
output bookingPrincipalId string = booking.properties.principalId
output slotsClientId string = slots.properties.clientId
output slotsPrincipalId string = slots.properties.principalId
