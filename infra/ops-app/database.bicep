// ARROW's operations database on the environment's SQL server. Owner decision: an
// always-on Standard S2 database (~USD 74/month list price) instead of the free offer, because
// the free tier's fixed 15-minute idle pause made the first reads fail with 40613.
targetScope = 'resourceGroup'

@description('Short environment name used in resource names, for example dev or prd.')
param env string = 'dev'
@description('Owner tag.')
param owner string = 'simonm43'
@description('Cost attribution tag.')
param costCenter string = 'arrow'

resource server 'Microsoft.Sql/servers@2025-01-01' existing = {
  name: 'sql-airrec-${env}-${uniqueString(subscription().subscriptionId, resourceGroup().id)}'
}

resource database 'Microsoft.Sql/servers/databases@2025-01-01' = {
  parent: server
  name: 'airrec-ops'
  location: resourceGroup().location
  tags: {
    workload: 'airrec'
    environment: env
    owner: owner
    costCenter: costCenter
  }
  sku: {
    name: 'Standard'
    tier: 'Standard'
    capacity: 50
  }
  properties: {
    maxSizeBytes: 268435456000 // 250 GB, included in S2; Standard has fixed sizes
    requestedBackupStorageRedundancy: 'Local'
    zoneRedundant: false
    readScale: 'Disabled'
    collation: 'SQL_Latin1_General_CP1_CI_AS'
    catalogCollation: 'SQL_Latin1_General_CP1_CI_AS'
    isLedgerOn: false
    maintenanceConfigurationId: subscriptionResourceId('Microsoft.Maintenance/publicMaintenanceConfigurations', 'SQL_Default')
  }
}
