targetScope = 'subscription'

// The shared platform ARROW runs on in one environment: the workload VNet (peered with a developer
// VNet only in Dev), private SQL (server, private endpoint and DNS zone), the container registry,
// Log Analytics with Application Insights, and the workload budget. The apps, their identities,
// the Container Apps environment, the airrec-ops database, Service Bus and the Foundry account
// are in infra\ops-app, infra\systems, infra\testbench and infra\foundry. Names follow one
// convention (tools\env\environment.py computes the same names); the defaults are Dev's values.

@description('Short environment name used in every resource name, for example dev or prd.')
param env string = 'dev'

@description('Short code of the region used in regional resource names, for example swc.')
param regionCode string = 'swc'

@description('Azure region of the environment; check model availability and quota first.')
param location string

@description('Dedicated resource group of the environment.')
param resourceGroupName string = 'rg-airrec-${env}-${regionCode}'

@description('Owner tag.')
param owner string = 'simonm43'

@description('Cost attribution tag.')
param costCenter string = 'arrow'

@description('Verified Entra object ID of the deploying developer (AcrPush; SQL admin in developer mode).')
param developerObjectId string

@description('Verified Entra UPN of the deploying developer.')
param developerUpn string

@description('Who administers SQL: the developer user (Dev), or a managed identity that only the in-environment migration job uses.')
@allowed([
  'developer'
  'identity'
])
param sqlAdministrator string = 'developer'

@description('Verified notification address for the workload budget.')
param budgetContactEmail string

@description('First day of the current deployment month, supplied by preflight.')
param budgetStartDate string

@description('Monthly workload budget in USD; it alerts and does not stop anything.')
param budgetAmount int = 900

@description('Log Analytics daily ingestion cap in GB.')
param logDailyCapGb int = 1

@description('Resource ID of a developer VNet to peer with and link to the SQL DNS zone (Dev only); empty for none.')
param developerVnetId string = ''

@description('Address space of the workload VNet.')
param vnetAddressPrefix string

@description('Subnet range reserved for private endpoints.')
param privateEndpointSubnetPrefix string

@description('Container Apps infrastructure subnet CIDR inside the workload VNet.')
param acaSubnetPrefix string = '10.60.0.64/26'

var resourceTags = {
  workload: 'airrec'
  environment: env
  owner: owner
  costCenter: costCenter
}

var workloadVnetName = 'vnet-airrec-${env}-${regionCode}'
var privateDnsZoneName = 'privatelink${environment().suffixes.sqlServerHostname}'
var privateEndpointSubnetId = resourceId(
  subscription().subscriptionId,
  resourceGroupName,
  'Microsoft.Network/virtualNetworks/subnets',
  workloadVnetName,
  'snet-private-endpoints'
)
var privateDnsZoneId = resourceId(
  subscription().subscriptionId,
  resourceGroupName,
  'Microsoft.Network/privateDnsZones',
  privateDnsZoneName
)

resource workloadResourceGroup 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: resourceGroupName
  location: location
  tags: resourceTags
}

module network 'modules/network.bicep' = {
  name: 'airrecNetwork'
  scope: workloadResourceGroup
  params: {
    location: location
    tags: resourceTags
    env: env
    regionCode: regionCode
    vnetName: workloadVnetName
    vnetAddressPrefix: vnetAddressPrefix
    privateEndpointSubnetPrefix: privateEndpointSubnetPrefix
    acaSubnetPrefix: acaSubnetPrefix
    developerVnetId: developerVnetId
  }
}

module acr 'modules/acr.bicep' = {
  name: 'airrecAcr'
  scope: workloadResourceGroup
  params: {
    location: location
    tags: resourceTags
    env: env
    developerObjectId: developerObjectId
  }
}

module sql 'modules/sql.bicep' = {
  name: 'airrecSql'
  scope: workloadResourceGroup
  params: {
    location: location
    tags: resourceTags
    env: env
    regionCode: regionCode
    administrator: sqlAdministrator
    developerObjectId: developerObjectId
    developerUpn: developerUpn
    subnetId: privateEndpointSubnetId
    privateDnsZoneId: privateDnsZoneId
  }
  dependsOn: [
    network
  ]
}

module budget 'modules/budget.bicep' = {
  name: 'airrecBudget'
  scope: workloadResourceGroup
  params: {
    name: 'budget-airrec-${env}'
    amount: budgetAmount
    budgetStartDate: budgetStartDate
    contactEmail: budgetContactEmail
  }
}

module monitoring 'modules/monitoring.bicep' = {
  name: 'airrecMonitoring'
  scope: workloadResourceGroup
  params: {
    location: location
    tags: resourceTags
    env: env
    regionCode: regionCode
    dailyCapGb: logDailyCapGb
  }
}

output resourceGroupId string = workloadResourceGroup.id
output sqlServerId string = sql.outputs.sqlServerId
output sqlServerFqdn string = sql.outputs.sqlServerFqdn
output acrName string = acr.outputs.name
output acrLoginServer string = acr.outputs.loginServer
output logAnalyticsWorkspaceId string = monitoring.outputs.logAnalyticsWorkspaceId
output logAnalyticsCustomerId string = monitoring.outputs.logAnalyticsCustomerId
output appInsightsName string = monitoring.outputs.appInsightsName
