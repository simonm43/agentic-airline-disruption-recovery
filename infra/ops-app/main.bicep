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
@description('Application (client) ID of the ARROW app registration (sign-in and API audience).')
param apiAppId string
@secure()
param clientSecret string
@description('OID of the signed-in operator, who serves as OCC Duty Manager.')
param operatorOid string
@description('Further OCC Duty Managers, so a rule change can be approved by a second person (four-eyes, D97). Dev: Test User (D98).')
param additionalOperatorOids array = ['00000000-0000-4000-8000-000000000014']
@description('Comma-separated OIDs of Passenger Services experts who review out-of-policy rebookings. Dev demo: the operator.')
param expertOids string = operatorOid
// The Foundry account and project names (tools/env/environment.py): Dev's predate the rule.
@description('Microsoft Foundry portal address of the project, for the operator app\'s links to each agent and version; empty shows no link.')
param foundryPortalUrl string = 'https://ai.azure.com/nextgen/r/AAAAAAAAQACAAAAAAAAAEw,rg-airrec-dev-swc,,aif-airrec-chat-dev-4w7wuej5i2a5m,proj-airrec-chat-dev'
param environmentName string = 'cae-airrec-${env}-${regionCode}'
param registryName string = 'crairrec${env}${uniqueString(subscription().subscriptionId, resourceGroup().id)}'
param projectEndpoint string = env == 'dev' ? 'https://aif-airrec-chat-dev-${uniqueString(resourceGroup().id)}.services.ai.azure.com/api/projects/proj-airrec-chat-dev' : 'https://aif-airrec-${env}-${uniqueString(subscription().subscriptionId, resourceGroup().id)}.services.ai.azure.com/api/projects/proj-airrec-${env}'
@description('Application (client) ID of the airline systems\' API app registration.')
param systemsAppId string = '00000000-0000-4000-8000-000000000017'
@allowed([0, 1])
@description('One warm replica for interactive use; zero restores idle scale-to-zero.')
param minReplicas int = 1
@description('Service Bus namespace of the assessment and execution queues (infra/ops-app/servicebus.bicep, ADR 0025).')
param serviceBusNamespace string = 'sb-airrec-${env}-${uniqueString(subscription().subscriptionId, resourceGroup().id)}.servicebus.windows.net'
@description('ARROW Test Bench identity (infra/testbench); empty admits no test bench.')
param testbenchOid string = ''
param testbenchClientId string = ''
@description('Identities the live app still holds besides its own, kept attached while it moves to its own identity (from one ARROW app to three: tools/env/wiring.py looks them up); empty in a new environment.')
param heldIdentityNames array = []

var appName = 'ca-airrec-ops-${env}'
var sqlServerName = 'sql-airrec-${env}-${uniqueString(subscription().subscriptionId, resourceGroup().id)}'

resource environment 'Microsoft.App/managedEnvironments@2026-01-01' existing = {
  name: environmentName
}
// The operator app's own identity (three container apps spec): it reads Schedule and Booking and
// writes nothing to the airline systems; the workers run as id-airrec-ops, the only writer.
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: 'id-airrec-operator-${env}'
}
resource workers 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: 'id-airrec-ops-${env}'
}
resource tools 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: 'id-airrec-tools-${env}'
}
// ARROW's traces go here, signed by its identity (telemetry.bicep grants it).
resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: 'appi-airrec-${env}-${regionCode}'
}
var domain = environment.properties.defaultDomain
var publicUrl = 'https://${appName}.${domain}'

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
    // Its own identity, plus any it still holds from running as one app until the deployment
    // guide's next command removes it (the what-if gate refuses removing it in a deployment).
    userAssignedIdentities: union(
      { '${identity.id}': {} },
      toObject(
        heldIdentityNames,
        name => resourceId('Microsoft.ManagedIdentity/userAssignedIdentities', name),
        name => {}
      )
    )
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
      secrets: [{ name: 'entra-client-secret', value: clientSecret }]
    }
    template: {
      containers: [{
        name: 'ops'
        image: '${registryName}.azurecr.io/airrec-ops-app@${imageDigest}'
        resources: { cpu: json('0.5'), memory: '1Gi' }
        // In the order the live Dev app holds them, so what-if compares like with like; the
        // settings of the three apps' split come last (three container apps spec).
        env: [
          { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
          { name: 'SQL_SERVER', value: '${sqlServerName}${az.environment().suffixes.sqlServerHostname}' }
          { name: 'SQL_DATABASE', value: 'airrec-ops' }
          { name: 'PUBLIC_URL', value: publicUrl }
          { name: 'OPERATOR_OIDS', value: join(concat([operatorOid], additionalOperatorOids), ',') }
          { name: 'SYSTEMS_API_APP_ID', value: systemsAppId }
          { name: 'SCHEDULE_URL', value: 'https://ca-airrec-schedule-${env}.${domain}' }
          { name: 'MAINTENANCE_URL', value: 'https://ca-airrec-maintenance-${env}.${domain}' }
          { name: 'FOUNDRY_PROJECT_ENDPOINT', value: projectEndpoint }
          { name: 'BOOKING_URL', value: 'https://ca-airrec-booking-${env}.${domain}' }
          { name: 'CREW_URL', value: 'https://ca-airrec-crew-${env}.${domain}' }
          { name: 'SLOTS_URL', value: 'https://ca-airrec-slots-${env}.${domain}' }
          { name: 'EXPERT_OIDS', value: expertOids }
          // The workers' identity: its calls show as ARROW's own in the trail.
          { name: 'OPS_PRINCIPAL_ID', value: workers.properties.principalId }
          { name: 'TESTBENCH_OIDS', value: testbenchOid }
          { name: 'SERVICEBUS_NAMESPACE', value: serviceBusNamespace }
          { name: 'ASSESSMENT_QUEUE', value: 'assessments' }
          { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
          { name: 'APPLICATIONINSIGHTS_RESOURCE_ID', value: appInsights.id }
          { name: 'FOUNDRY_PORTAL_URL', value: foundryPortalUrl }
          { name: 'APP_ROLE', value: 'operator' }
          { name: 'EXECUTION_QUEUE', value: 'executions' }
          { name: 'OPERATOR_PRINCIPAL_ID', value: identity.properties.principalId }
          { name: 'TOOLS_PRINCIPAL_ID', value: tools.properties.principalId }
        ]
        probes: [{ type: 'Liveness', httpGet: { path: '/healthz', port: 8080 }, periodSeconds: 30 }]
      }]
      scale: { minReplicas: minReplicas, maxReplicas: 1, cooldownPeriod: 300, pollingInterval: 30 }
    }
  }
}
resource auth 'Microsoft.App/containerApps/authConfigs@2026-01-01' = {
  parent: app
  name: 'current'
  properties: {
    platform: { enabled: true }
    globalValidation: {
      unauthenticatedClientAction: 'Return401'
      // Public shell contains no operation data; every API route also validates the caller.
      excludedPaths: ['/healthz', '/', '/app.js', '/network.js', '/recovery_view.js', '/styles.css']
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        isAutoProvisioned: false
        registration: {
          clientId: apiAppId
          clientSecretSettingName: 'entra-client-secret'
          openIdIssuer: 'https://login.microsoftonline.com/${tenant().tenantId}/v2.0'
        }
        validation: {
          allowedAudiences: ['api://${apiAppId}', apiAppId]
          // Operators and the Test Bench only: the agents call the tools app (three container
          // apps spec). Lists in the order the live Dev app holds them.
          defaultAuthorizationPolicy: {
            allowedApplications: concat(
              [apiAppId],
              empty(testbenchClientId) ? [] : [testbenchClientId]
            )
            allowedPrincipals: {
              identities: concat(
                [operatorOid],
                empty(testbenchOid) ? [] : [testbenchOid],
                additionalOperatorOids
              )
            }
          }
        }
      }
    }
    login: { tokenStore: { enabled: false }, preserveUrlFragmentsForLogins: false }
    httpSettings: { requireHttps: true }
  }
}
output url string = publicUrl
