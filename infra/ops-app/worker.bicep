// ARROW's workers (three container apps spec): the Service Bus intake, the assessment workers
// (which run the orchestrator in Foundry), the executor and the supervisor. No ingress: nothing
// calls it; it takes its work from the queues and Azure SQL. It runs as id-airrec-ops, the only
// ARROW identity the airline systems let write. Same image as the operator and tools apps
// (APP_ROLE workers).
targetScope = 'resourceGroup'

@description('Short environment name used in resource names, for example dev or prd.')
param env string = 'dev'
@description('Short region code used in regional names, for example swc.')
param regionCode string = 'swc'
@description('Owner tag.')
param owner string = 'simonm43'
@description('Cost attribution tag.')
param costCenter string = 'arrow'
param imageDigest string
@description('Application (client) ID of the airline systems\' API app registration.')
param systemsAppId string = '00000000-0000-4000-8000-000000000017'
param environmentName string = 'cae-airrec-${env}-${regionCode}'
param registryName string = 'crairrec${env}${uniqueString(subscription().subscriptionId, resourceGroup().id)}'
// The Foundry account and project names (tools/env/environment.py): Dev's predate the rule.
param projectEndpoint string = env == 'dev' ? 'https://aif-airrec-chat-dev-${uniqueString(resourceGroup().id)}.services.ai.azure.com/api/projects/proj-airrec-chat-dev' : 'https://aif-airrec-${env}-${uniqueString(subscription().subscriptionId, resourceGroup().id)}.services.ai.azure.com/api/projects/proj-airrec-${env}'
@description('Service Bus namespace of the assessment and execution queues (infra/ops-app/servicebus.bicep).')
param serviceBusNamespace string = 'sb-airrec-${env}-${uniqueString(subscription().subscriptionId, resourceGroup().id)}.servicebus.windows.net'
@description('Assessments run at once by the one replica (queue receivers).')
param assessmentWorkers int = 2

var appName = 'ca-airrec-worker-${env}'
var sqlServerName = 'sql-airrec-${env}-${uniqueString(subscription().subscriptionId, resourceGroup().id)}'

resource environment 'Microsoft.App/managedEnvironments@2026-01-01' existing = {
  name: environmentName
}
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: 'id-airrec-ops-${env}'
}
resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: 'appi-airrec-${env}-${regionCode}'
}
var domain = environment.properties.defaultDomain

resource app 'Microsoft.App/containerApps@2026-01-01' = {
  name: appName
  location: resourceGroup().location
  tags: {
    workload: 'airrec'
    environment: env
    owner: owner
    costCenter: costCenter
  }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      maxInactiveRevisions: 100
      registries: [{ server: '${registryName}.azurecr.io', identity: identity.id }]
    }
    template: {
      containers: [{
        name: 'worker'
        image: '${registryName}.azurecr.io/airrec-ops-app@${imageDigest}'
        resources: { cpu: json('0.5'), memory: '1Gi' }
        env: [
          { name: 'APP_ROLE', value: 'workers' }
          { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
          { name: 'SQL_SERVER', value: '${sqlServerName}${az.environment().suffixes.sqlServerHostname}' }
          { name: 'SQL_DATABASE', value: 'airrec-ops' }
          { name: 'SYSTEMS_API_APP_ID', value: systemsAppId }
          { name: 'SCHEDULE_URL', value: 'https://ca-airrec-schedule-${env}.${domain}' }
          { name: 'MAINTENANCE_URL', value: 'https://ca-airrec-maintenance-${env}.${domain}' }
          { name: 'BOOKING_URL', value: 'https://ca-airrec-booking-${env}.${domain}' }
          { name: 'CREW_URL', value: 'https://ca-airrec-crew-${env}.${domain}' }
          { name: 'SLOTS_URL', value: 'https://ca-airrec-slots-${env}.${domain}' }
          { name: 'FOUNDRY_PROJECT_ENDPOINT', value: projectEndpoint }
          // Its own identity: the reporter of the disruptions it records, the only writer.
          { name: 'OPS_PRINCIPAL_ID', value: identity.properties.principalId }
          { name: 'SERVICEBUS_NAMESPACE', value: serviceBusNamespace }
          { name: 'ASSESSMENT_QUEUE', value: 'assessments' }
          { name: 'EXECUTION_QUEUE', value: 'executions' }
          { name: 'ASSESSMENT_WORKERS', value: string(assessmentWorkers) }
          { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
        ]
        // No ingress, so no default probes: this one restarts the replica when an intake,
        // assessment or execution thread has stopped (ops_app/workers.py health_app).
        probes: [{ type: 'Liveness', httpGet: { path: '/healthz', port: 8080 }, periodSeconds: 30 }]
      }]
      // One replica: the supervisor and the timed actions must always run. Scaling on the
      // queues' length is the next step, once two replicas are shown to be safe.
      scale: { minReplicas: 1, maxReplicas: 1, cooldownPeriod: 300, pollingInterval: 30 }
    }
  }
}
output name string = app.name
