// The Test Bench's operator role on the day-reset job. Deployed on its own because a
// full redeploy of main.bicep removes the sign-in secret copied by configure_access.py. The
// assignment name uses the same expression as main.bicep, so both declare one assignment.
targetScope = 'resourceGroup'

@description('Short environment name used in resource names, for example dev or prd.')
param env string = 'dev'
param resetJobName string = 'caj-airrec-dayreset-${env}'

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: 'id-airrec-testbench-${env}'
}
resource operatorRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  name: guid(resourceGroup().id, 'airrec-testbench-operator-${env}')
}
resource resetJob 'Microsoft.App/jobs@2025-01-01' existing = {
  name: resetJobName
}

resource onResetJob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: resetJob
  name: guid(resetJob.id, identity.id, operatorRole.id)
  properties: {
    // The subscription-level form Azure stores for a custom role.
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', operatorRole.name)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
