// The database migrations, run inside the environment: a new environment has no developer
// network, so nothing outside it can reach private SQL. A manual job applies
// infra\systems\sql\*.sql in order (tools\systems\migrate.py, built into the airrec-systems image
// by tools\systems\build.ps1) as id-airrec-sqladmin-<env>, the SQL server's Entra admin when
// infra\main.bicep runs with sqlAdministrator 'identity'. Each file's $(Name) placeholders are the
// client IDs of the identities it grants, passed as MIGRATION_<Name>. Dev keeps its developer
// admin and applies migrations with tools\systems\apply_sql.py over the developer-network peering.
targetScope = 'resourceGroup'

@description('Digest of an airrec-systems image built with the migrations, e.g. sha256:...')
param imageDigest string
@description('Short environment name used in resource names, for example dev or prd.')
param env string = 'dev'
@description('Short region code used in regional names, for example swc.')
param regionCode string = 'swc'
@description('Owner tag.')
param owner string = 'simonm43'
@description('Cost attribution tag.')
param costCenter string = 'arrow'
param location string = resourceGroup().location
param environmentName string = 'cae-airrec-${env}-${regionCode}'
param registryName string = 'crairrec${env}${uniqueString(subscription().subscriptionId, resourceGroup().id)}'
param sqlServer string = 'sql-airrec-${env}-${uniqueString(subscription().subscriptionId, resourceGroup().id)}${environment().suffixes.sqlServerHostname}'
param sqlDatabase string = 'airrec-ops'

var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource managedEnvironment 'Microsoft.App/managedEnvironments@2026-01-01' existing = {
  name: environmentName
}
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}
resource admin 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: 'id-airrec-sqladmin-${env}'
}
// The identities the SQL files grant; each placeholder $(<Name>ClientId) is one's client ID.
resource systems 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: 'id-airrec-systems-${env}'
}
resource ops 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: 'id-airrec-ops-${env}'
}
resource maintenance 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: 'id-airrec-maintenance-${env}'
}
resource crew 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: 'id-airrec-crew-${env}'
}
resource booking 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: 'id-airrec-booking-${env}'
}
resource slots 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: 'id-airrec-slots-${env}'
}
// The operator app's and the agents' tools app's own identities (three container apps spec).
resource operator 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: 'id-airrec-operator-${env}'
}
resource tools 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: 'id-airrec-tools-${env}'
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, admin.id, acrPullRoleId)
  scope: registry
  properties: {
    principalId: admin.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

resource job 'Microsoft.App/jobs@2026-01-01' = {
  name: 'caj-airrec-dbmigrate-${env}'
  location: location
  tags: {
    workload: 'airrec'
    environment: env
    owner: owner
    costCenter: costCenter
  }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${admin.id}': {} }
  }
  dependsOn: [acrPull]
  properties: {
    environmentId: managedEnvironment.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 1800
      replicaRetryLimit: 0
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: admin.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'dbmigrate'
          image: '${registry.properties.loginServer}/airrec-systems@${imageDigest}'
          command: ['python', '-m', 'airline_systems.migrate']
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            { name: 'SQL_SERVER', value: sqlServer }
            { name: 'SQL_DATABASE', value: sqlDatabase }
            { name: 'AZURE_CLIENT_ID', value: admin.properties.clientId }
            { name: 'MIGRATION_SystemsClientId', value: systems.properties.clientId }
            { name: 'MIGRATION_OpsClientId', value: ops.properties.clientId }
            { name: 'MIGRATION_MaintenanceClientId', value: maintenance.properties.clientId }
            { name: 'MIGRATION_CrewClientId', value: crew.properties.clientId }
            { name: 'MIGRATION_BookingClientId', value: booking.properties.clientId }
            { name: 'MIGRATION_SlotsClientId', value: slots.properties.clientId }
            { name: 'MIGRATION_OperatorClientId', value: operator.properties.clientId }
            { name: 'MIGRATION_ToolsClientId', value: tools.properties.clientId }
          ]
        }
      ]
    }
  }
}

output jobName string = job.name
