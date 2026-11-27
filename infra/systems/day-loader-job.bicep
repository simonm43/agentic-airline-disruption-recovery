// Nightly load of the new Lisbon operating day into the sample airline systems (D86 follow-up).
// A scheduled Container Apps job, independent of the apps; it runs as the Schedule system's
// identity, which already loads days and pulls from ACR. Load on first use stays the fallback,
// and both share the same database lock.
// The day-reset job runs the same container with --force when the Test Bench Reset
// starts it: Azure refuses a start that overrides a job's command unless the caller may rewrite
// the job, so the forced reload is a job of its own that the Test Bench may only start. In a new
// environment the first day is loaded by starting the reset job once.
targetScope = 'resourceGroup'

@description('Image digest in airrec-systems, e.g. sha256:...')
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
@description('UTC. Lisbon is UTC+0 or UTC+1, so 00:05 UTC is always just after Lisbon midnight.')
param cronExpression string = '5 0 * * *'

resource managedEnvironment 'Microsoft.App/managedEnvironments@2026-01-01' existing = {
  name: environmentName
}
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: 'id-airrec-systems-${env}'
}
// Traces go here, signed by the Schedule identity (infra/ops-app/telemetry.bicep grants it).
resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: 'appi-airrec-${env}-${regionCode}'
}

var tags = {
  workload: 'airrec'
  environment: env
  owner: owner
  costCenter: costCenter
}
var registries = [
  {
    server: registry.properties.loginServer
    identity: identity.id
  }
]
// The jobs pull as the Schedule identity. The same assignment as infra/systems/system.bicep's for
// Schedule (same name), declared here too because a new environment loads its first day before
// the systems' apps exist.
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identity.id, '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  scope: registry
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}
var dayContainer = {
  name: 'dayload'
  image: '${registry.properties.loginServer}/airrec-systems@${imageDigest}'
  resources: { cpu: json('0.5'), memory: '1Gi' }
  env: [
    { name: 'SQL_SERVER', value: sqlServer }
    { name: 'SQL_DATABASE', value: sqlDatabase }
    { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
    { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
  ]
}

resource job 'Microsoft.App/jobs@2026-01-01' = {
  name: 'caj-airrec-dayload-${env}'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  dependsOn: [acrPull]
  properties: {
    environmentId: managedEnvironment.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Schedule'
      replicaTimeout: 900
      replicaRetryLimit: 2
      scheduleTriggerConfig: {
        cronExpression: cronExpression
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: registries
    }
    template: {
      containers: [union(dayContainer, { command: ['python', '-m', 'airline_systems.day_loader'] })]
    }
  }
}

resource resetJob 'Microsoft.App/jobs@2026-01-01' = {
  name: 'caj-airrec-dayreset-${env}'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  dependsOn: [acrPull]
  properties: {
    environmentId: managedEnvironment.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 900
      replicaRetryLimit: 0
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: registries
    }
    template: {
      containers: [
        union(dayContainer, { command: ['python', '-m', 'airline_systems.day_loader', '--force'] })
      ]
    }
  }
}

output resetJobId string = resetJob.id
