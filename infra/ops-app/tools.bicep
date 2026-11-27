// ARROW's agent tools app (three container apps spec): the MCP tools the Foundry agents call,
// with Easy Auth for the agents' own identities. It records readings, plans and submissions on
// ARROW's cases and reads the airline systems for them; it writes nothing to the systems and
// hands an automatic plan to the workers on the execution queue. Same image as the operator app
// and the workers (APP_ROLE tools).
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
@description('Application (client) ID of the ARROW app registration, the audience of the agents\' tokens for ARROW\'s tools.')
param apiAppId string = '00000000-0000-4000-8000-000000000007'
@description('Application (client) ID of the airline systems\' API app registration.')
param systemsAppId string = '00000000-0000-4000-8000-000000000017'
@description('Instance identity of the recovery-orchestrator-agent prompt agent: the orchestrator\'s tools.')
param orchestratorAgentOid string = '00000000-0000-4000-8000-000000000016'
@description('Instance identity of the case-assistant-agent prompt agent: read_case, what_if and get_playbook only.')
param assistantAgentOid string = '00000000-0000-4000-8000-000000000001'
@description('Instance identity of the disruption-detection-agent prompt agent: the three record_* tools only.')
param detectionAgentOid string = '00000000-0000-4000-8000-000000000015'
@description('Instance identity of the passenger-reaccommodation-agent prompt agent: get_compensation_exposure only.')
param passengerAgentOid string = '00000000-0000-4000-8000-000000000009'
@description('Every agent whose definition calls ARROW\'s tools (tools/env/wiring.py derives them); Easy Auth admits these only, and code decides each one\'s tools by its role. An agent identity\'s client id equals its object id.')
param agentOids array = [
  '00000000-0000-4000-8000-000000000015'
  '00000000-0000-4000-8000-000000000009'
  '00000000-0000-4000-8000-000000000016'
  '00000000-0000-4000-8000-000000000001'
]
param environmentName string = 'cae-airrec-${env}-${regionCode}'
param registryName string = 'crairrec${env}${uniqueString(subscription().subscriptionId, resourceGroup().id)}'
// The Foundry account and project names (tools/env/environment.py): Dev's predate the rule.
param projectEndpoint string = env == 'dev' ? 'https://aif-airrec-chat-dev-${uniqueString(resourceGroup().id)}.services.ai.azure.com/api/projects/proj-airrec-chat-dev' : 'https://aif-airrec-${env}-${uniqueString(subscription().subscriptionId, resourceGroup().id)}.services.ai.azure.com/api/projects/proj-airrec-${env}'
@description('Service Bus namespace of the assessment and execution queues (infra/ops-app/servicebus.bicep).')
param serviceBusNamespace string = 'sb-airrec-${env}-${uniqueString(subscription().subscriptionId, resourceGroup().id)}.servicebus.windows.net'

var appName = 'ca-airrec-tools-${env}'
var sqlServerName = 'sql-airrec-${env}-${uniqueString(subscription().subscriptionId, resourceGroup().id)}'

resource environment 'Microsoft.App/managedEnvironments@2026-01-01' existing = {
  name: environmentName
}
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: 'id-airrec-tools-${env}'
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
        name: 'tools'
        image: '${registryName}.azurecr.io/airrec-ops-app@${imageDigest}'
        resources: { cpu: json('0.5'), memory: '1Gi' }
        env: [
          { name: 'APP_ROLE', value: 'tools' }
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
          { name: 'ORCHESTRATOR_AGENT_OIDS', value: orchestratorAgentOid }
          { name: 'ASSISTANT_AGENT_OIDS', value: assistantAgentOid }
          { name: 'DETECTION_AGENT_OIDS', value: detectionAgentOid }
          { name: 'PASSENGER_AGENT_OIDS', value: passengerAgentOid }
          { name: 'SERVICEBUS_NAMESPACE', value: serviceBusNamespace }
          { name: 'ASSESSMENT_QUEUE', value: 'assessments' }
          { name: 'EXECUTION_QUEUE', value: 'executions' }
          { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
        ]
        probes: [{ type: 'Liveness', httpGet: { path: '/healthz', port: 8080 }, periodSeconds: 30 }]
      }]
      // One warm replica: an agent run never waits for a cold start.
      scale: { minReplicas: 1, maxReplicas: 1, cooldownPeriod: 300, pollingInterval: 30 }
    }
  }
}
// Only the agents that call ARROW's tools, with tokens for ARROW's API; no person signs in here.
resource auth 'Microsoft.App/containerApps/authConfigs@2026-01-01' = {
  parent: app
  name: 'current'
  properties: {
    platform: { enabled: true }
    globalValidation: {
      unauthenticatedClientAction: 'Return401'
      excludedPaths: ['/healthz']
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        isAutoProvisioned: false
        registration: {
          clientId: apiAppId
          openIdIssuer: '${az.environment().authentication.loginEndpoint}${tenant().tenantId}/v2.0'
        }
        validation: {
          allowedAudiences: ['api://${apiAppId}', apiAppId]
          defaultAuthorizationPolicy: {
            allowedApplications: agentOids
            allowedPrincipals: { identities: agentOids }
          }
        }
      }
    }
    login: { tokenStore: { enabled: false }, preserveUrlFragmentsForLogins: false }
    httpSettings: { requireHttps: true }
  }
}
output url string = 'https://${app.properties.configuration.ingress.fqdn}'
