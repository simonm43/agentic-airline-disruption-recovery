targetScope = 'resourceGroup'

@description('Short environment name used in resource names, for example dev or prd.')
param env string = 'dev'
@description('Tokens-per-minute units of the deployment (1 unit = 1,000 tokens and 1 request a minute); check the region\'s quota first.')
param capacity int = 500

// The Foundry account and project names (tools/env/environment.py): Dev's predate the rule.
resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: env == 'dev' ? 'aif-airrec-chat-dev-${uniqueString(resourceGroup().id)}' : 'aif-airrec-${env}-${uniqueString(subscription().subscriptionId, resourceGroup().id)}'
}

// Regional, consumption-priced; no provisioned throughput or global routing. Also serves the
// recovery orchestrator and Detection agents: 500 units (500 requests and 500k tokens per minute)
// so a burst of disruption events is not rate-limited (60 units gave HTTP 429).
resource model 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: account
  name: 'gpt-4.1-eval'
  sku: {
    name: 'Standard'
    capacity: capacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-4.1'
      version: '2025-04-14'
    }
    versionUpgradeOption: 'NoAutoUpgrade'
    raiPolicyName: 'Microsoft.DefaultV2'
  }
}

output deploymentName string = model.name
