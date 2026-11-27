// Disruption intake on Service Bus (Service Bus intake spec, ADR 0025): systems publish CloudEvents
// to the topic; ARROW reads its subscription and opens each case at once, then wakes it on the
// assessment queue, which also takes reports typed in ARROW and scheduled retries. The execution
// queue hands authorised plans from the operator and tools apps to the workers (three container
// apps spec §4): a wake only; the case in SQL decides what runs. Entra only: local (key)
// authentication is disabled. The data roles: the workers (id-airrec-ops) receive from the
// subscription and both queues and send to the assessment queue; the operator and tools apps send
// to both queues; the Test Bench sends to the topic.
targetScope = 'resourceGroup'

@description('Short environment name used in resource names, for example dev or prd.')
param env string = 'dev'
@description('Owner tag.')
param owner string = 'simonm43'
@description('Cost attribution tag.')
param costCenter string = 'arrow'
param namespaceName string = 'sb-airrec-${env}-${uniqueString(az.subscription().subscriptionId, resourceGroup().id)}'
param opsIdentityName string = 'id-airrec-ops-${env}'
param operatorIdentityName string = 'id-airrec-operator-${env}'
param toolsIdentityName string = 'id-airrec-tools-${env}'
param testbenchIdentityName string = 'id-airrec-testbench-${env}'

var tags = {
  workload: 'airrec'
  environment: env
  owner: owner
  costCenter: costCenter
}
var senderRole = '69a216fc-b8fb-44d8-bc22-1f3c2cd27a39' // Azure Service Bus Data Sender
// Provider defaults, declared so a redeploy compares equal (the what-if gate refuses deletes).
var neverAutoDelete = 'P10675199DT2H48M5.4775807S'
var receiverRole = '4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0' // Azure Service Bus Data Receiver

resource ops 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: opsIdentityName
}
resource operator 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: operatorIdentityName
}
resource tools 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: toolsIdentityName
}
resource testbench 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: testbenchIdentityName
}

resource namespace 'Microsoft.ServiceBus/namespaces@2024-01-01' = {
  name: namespaceName
  location: resourceGroup().location
  tags: tags
  sku: {
    name: 'Standard'
    tier: 'Standard'
  }
  properties: {
    disableLocalAuth: true
    minimumTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
  }
}

resource queue 'Microsoft.ServiceBus/namespaces/queues@2024-01-01' = {
  parent: namespace
  name: 'assessments'
  properties: {
    lockDuration: 'PT2M'
    maxDeliveryCount: 5
    defaultMessageTimeToLive: 'PT1H'
    deadLetteringOnMessageExpiration: true
    requiresDuplicateDetection: true
    duplicateDetectionHistoryTimeWindow: 'PT10M'
    maxSizeInMegabytes: 1024
    autoDeleteOnIdle: neverAutoDelete
    enablePartitioning: false
    enableBatchedOperations: true
    enableExpress: false
    requiresSession: false
  }
}

// Wakes that hand authorised plans to the workers. An expired wake is dropped, not dead-lettered:
// the workers' supervisor finds every approved plan in SQL at start and every minute.
resource executions 'Microsoft.ServiceBus/namespaces/queues@2024-01-01' = {
  parent: namespace
  name: 'executions'
  properties: {
    lockDuration: 'PT2M'
    maxDeliveryCount: 5
    defaultMessageTimeToLive: 'PT1H'
    deadLetteringOnMessageExpiration: false
    requiresDuplicateDetection: true
    duplicateDetectionHistoryTimeWindow: 'PT10M'
    maxSizeInMegabytes: 1024
    autoDeleteOnIdle: neverAutoDelete
    enablePartitioning: false
    enableBatchedOperations: true
    enableExpress: false
    requiresSession: false
  }
}

resource topic 'Microsoft.ServiceBus/namespaces/topics@2024-01-01' = {
  parent: namespace
  name: 'disruption-events'
  properties: {
    defaultMessageTimeToLive: 'PT1H'
    requiresDuplicateDetection: true
    duplicateDetectionHistoryTimeWindow: 'PT10M'
    maxSizeInMegabytes: 1024
    autoDeleteOnIdle: neverAutoDelete
    enablePartitioning: false
    enableBatchedOperations: true
    enableExpress: false
    supportOrdering: true
  }
}

resource subscription 'Microsoft.ServiceBus/namespaces/topics/subscriptions@2024-01-01' = {
  parent: topic
  name: 'assessment'
  properties: {
    lockDuration: 'PT1M'
    maxDeliveryCount: 5
    defaultMessageTimeToLive: 'PT1H'
    deadLetteringOnMessageExpiration: true
    deadLetteringOnFilterEvaluationExceptions: true
    requiresSession: false
    autoDeleteOnIdle: neverAutoDelete
    enableBatchedOperations: true
    isClientAffine: false
  }
}

resource opsSends 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: queue
  name: guid(queue.id, ops.id, senderRole)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', senderRole)
    principalId: ops.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource opsReceives 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: queue
  name: guid(queue.id, ops.id, receiverRole)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', receiverRole)
    principalId: ops.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource opsReadsEvents 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: subscription
  name: guid(subscription.id, ops.id, receiverRole)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', receiverRole)
    principalId: ops.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource testbenchSends 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: topic
  name: guid(topic.id, testbench.id, senderRole)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', senderRole)
    principalId: testbench.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource opsReceivesExecutions 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: executions
  name: guid(executions.id, ops.id, receiverRole)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', receiverRole)
    principalId: ops.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// The operator app queues typed reports and hands approved plans to the workers; the tools app
// wakes a reopened case and hands automatic plans to the workers.
resource operatorSends 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: queue
  name: guid(queue.id, operator.id, senderRole)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', senderRole)
    principalId: operator.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource operatorSendsExecutions 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: executions
  name: guid(executions.id, operator.id, senderRole)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', senderRole)
    principalId: operator.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource toolsSends 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: queue
  name: guid(queue.id, tools.id, senderRole)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', senderRole)
    principalId: tools.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource toolsSendsExecutions 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: executions
  name: guid(executions.id, tools.id, senderRole)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', senderRole)
    principalId: tools.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

output fullyQualifiedNamespace string = '${namespace.name}.servicebus.windows.net'
output queueName string = queue.name
output executionQueueName string = executions.name
output topicName string = topic.name
