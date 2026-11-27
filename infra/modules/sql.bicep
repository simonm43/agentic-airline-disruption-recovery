targetScope = 'resourceGroup'

@description('Azure region of the environment.')
param location string

@description('Workload and environment tags.')
param tags object

@description('Short environment name, for example dev or prd.')
param env string

@description('Short region code, for example swc.')
param regionCode string

@description('Who administers SQL: the developer user, or the managed identity the migration job runs as.')
@allowed([
  'developer'
  'identity'
])
param administrator string = 'developer'

@description('Verified developer Entra object ID.')
param developerObjectId string

@description('Verified developer Entra UPN.')
param developerUpn string

@description('ID of the private-endpoint subnet.')
param subnetId string

@description('ID of the workload-owned Azure SQL private DNS zone.')
param privateDnsZoneId string

var serverName = 'sql-airrec-${env}-${uniqueString(subscription().subscriptionId, resourceGroup().id)}'
var adminIdentityName = 'id-airrec-sqladmin-${env}'
var byIdentity = administrator == 'identity'

// New environments have no developer network, so SQL is administered from inside the
// environment: this identity is the server's Entra admin and only the migration job
// (infra\systems\migration-job.bicep) runs as it. An application admin is named by its
// client ID (Microsoft Learn: Microsoft Entra service principals with Azure SQL).
resource adminIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = if (byIdentity) {
  name: adminIdentityName
  location: location
  tags: tags
}

// Always private: the tenant's Modify policy forces Disabled and ARM then refuses firewall
// rules (D28). Everything that reaches SQL runs in the workload VNet (Dev also from the developer VM).
resource sqlServer 'Microsoft.Sql/servers@2025-01-01' = {
  name: serverName
  location: location
  tags: tags
  // Live Dev has a system-assigned identity; declared so a redeploy keeps it.
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    version: '12.0'
    publicNetworkAccess: 'Disabled'
    minimalTlsVersion: '1.2'
    administrators: {
      administratorType: 'ActiveDirectory'
      login: byIdentity ? adminIdentityName : developerUpn
      principalType: byIdentity ? 'Application' : 'User'
      sid: byIdentity ? adminIdentity!.properties.clientId : developerObjectId
      tenantId: tenant().tenantId
      azureADOnlyAuthentication: true
    }
  }
}

resource entraOnlyAuth 'Microsoft.Sql/servers/azureADOnlyAuthentications@2025-01-01' = {
  parent: sqlServer
  name: 'Default'
  properties: {
    azureADOnlyAuthentication: true
  }
}

resource connectionPolicy 'Microsoft.Sql/servers/connectionPolicies@2025-01-01' = {
  parent: sqlServer
  name: 'default'
  properties: {
    connectionType: 'Proxy'
  }
}

// ARROW's database airrec-ops is declared in infra\ops-app\database.bicep.

resource sqlPrivateEndpoint 'Microsoft.Network/privateEndpoints@2026-05-01' = {
  name: 'pe-airrec-sql-${env}-${regionCode}'
  location: location
  tags: tags
  properties: {
    // Provider defaults, declared so what-if reports no change against live Dev.
    billingSku: 'PayAsYouGo'
    ipVersionType: 'IPv4'
    subnet: {
      id: subnetId
    }
    privateLinkServiceConnections: [
      {
        name: 'sql-airrec-private-link'
        properties: {
          privateLinkServiceId: sqlServer.id
          groupIds: [
            'sqlServer'
          ]
        }
      }
    ]
  }
}

resource sqlPrivateDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2026-05-01' = {
  parent: sqlPrivateEndpoint
  name: 'sql'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'privatelink-database-windows-net'
        properties: {
          privateDnsZoneId: privateDnsZoneId
        }
      }
    ]
  }
}

output sqlServerId string = sqlServer.id
output sqlServerFqdn string = '${sqlServer.name}${environment().suffixes.sqlServerHostname}'
