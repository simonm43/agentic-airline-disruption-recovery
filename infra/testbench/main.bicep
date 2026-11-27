// The ARROW Test Bench (event trigger and test bench spec §5.2): its own identity, scale-to-zero
// app and a custom role with exactly three actions, assigned only on the day-load and day-reset
// jobs. Events go to Service Bus (ADR 0025). Easy Auth and its client secret are set afterwards by
// tools/testbench/configure_access.py; a redeploy of this template keeps them only if that
// script runs again.
targetScope = 'resourceGroup'

param imageDigest string
@description('Comma-separated OIDs allowed into the test bench (the environment owner).')
param ownerOids string
@description('Short environment name used in resource names, for example dev or prd.')
param env string = 'dev'
@description('The environment as people write it, for the custom role\'s name (unique in the tenant).')
param envLabel string = 'Dev'
@description('Short region code used in regional names, for example swc.')
param regionCode string = 'swc'
@description('Owner tag.')
param owner string = 'simonm43'
@description('Cost attribution tag.')
param costCenter string = 'arrow'
@description('Application (client) ID of the ARROW app registration (sign-in and API audience).')
param opsApiAppId string = '00000000-0000-4000-8000-000000000007'
@description('Service Bus namespace whose topic the test bench publishes to (ADR 0025).')
param serviceBusNamespace string = 'sb-airrec-${env}-${uniqueString(subscription().subscriptionId, resourceGroup().id)}.servicebus.windows.net'
param environmentName string = 'cae-airrec-${env}-${regionCode}'
param registryName string = 'crairrec${env}${uniqueString(subscription().subscriptionId, resourceGroup().id)}'
@description('The day-reset job Reset starts (its own command reloads with --force).')
param jobName string = 'caj-airrec-dayreset-${env}'
@description('The nightly day-load job, read for when the day was last loaded.')
param nightlyJobName string = 'caj-airrec-dayload-${env}'

var tags = {
  workload: 'airrec'
  environment: env
  owner: owner
  costCenter: costCenter
}
var acrPullRole = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource environment 'Microsoft.App/managedEnvironments@2026-01-01' existing = {
  name: environmentName
}
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}
resource job 'Microsoft.App/jobs@2025-01-01' existing = {
  name: jobName
}
resource nightlyJob 'Microsoft.App/jobs@2025-01-01' existing = {
  name: nightlyJobName
}
var domain = environment.properties.defaultDomain

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-airrec-testbench-${env}'
  location: resourceGroup().location
  tags: tags
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, identity.id, acrPullRole)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRole)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource operatorRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, 'airrec-testbench-operator-${env}')
  properties: {
    roleName: 'Airrec test bench operator (${envLabel})'
    description: 'Start and read the day-load and day-reset jobs. No wildcards, no secrets.'
    type: 'CustomRole'
    permissions: [
      {
        actions: [
          'Microsoft.App/jobs/read'
          'Microsoft.App/jobs/start/action'
          'Microsoft.App/jobs/executions/read'
        ]
        notActions: []
      }
    ]
    assignableScopes: [resourceGroup().id]
  }
}

// Assignments name the role in its subscription-level form, as Azure stores it.
var operatorRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', operatorRole.name)

resource onJob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: job
  name: guid(job.id, identity.id, operatorRole.id)
  properties: {
    roleDefinitionId: operatorRoleId
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource onNightlyJob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: nightlyJob
  name: guid(nightlyJob.id, identity.id, operatorRole.id)
  properties: {
    roleDefinitionId: operatorRoleId
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource app 'Microsoft.App/containerApps@2026-01-01' = {
  name: 'ca-airrec-testbench-${env}'
  location: resourceGroup().location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  dependsOn: [acrPull]
  properties: {
    managedEnvironmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      // Provider defaults, declared so a redeploy compares equal (the what-if gate refuses deletes).
      maxInactiveRevisions: 100
      ingress: {
        external: true
        targetPort: 8080
        transport: 'auto'
        allowInsecure: false
        exposedPort: 0
        traffic: [{ latestRevision: true, weight: 100 }]
      }
      registries: [{ server: '${registryName}.azurecr.io', identity: identity.id }]
    }
    template: {
      containers: [{
        name: 'testbench'
        image: '${registryName}.azurecr.io/airrec-testbench@${imageDigest}'
        resources: { cpu: json('0.25'), memory: '0.5Gi' }
        // In the order the live Dev app holds them, so what-if compares like with like.
        env: [
          { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
          { name: 'PUBLIC_URL', value: 'https://ca-airrec-testbench-${env}.${domain}' }
          { name: 'OPS_URL', value: 'https://ca-airrec-ops-${env}.${domain}' }
          { name: 'OPS_API_APP_ID', value: opsApiAppId }
          { name: 'JOB_ID', value: job.id }
          { name: 'OWNER_OIDS', value: ownerOids }
          { name: 'NIGHTLY_JOB_ID', value: nightlyJob.id }
          { name: 'SERVICEBUS_NAMESPACE', value: serviceBusNamespace }
          { name: 'DISRUPTION_TOPIC', value: 'disruption-events' }
        ]
        probes: [{ type: 'Liveness', httpGet: { path: '/healthz', port: 8080 }, periodSeconds: 30 }]
      }]
      scale: { minReplicas: 0, maxReplicas: 1, cooldownPeriod: 300, pollingInterval: 30 }
    }
  }
}

output url string = 'https://${app.properties.configuration.ingress.fqdn}'
output identityPrincipalId string = identity.properties.principalId
output identityClientId string = identity.properties.clientId
