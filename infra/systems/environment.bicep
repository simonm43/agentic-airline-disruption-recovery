// External Container Apps environment for ARROW, the airline systems and the Test Bench, in the
// workload VNet so the apps and jobs reach private SQL. Needs the subscription feature
// Microsoft.Network/AllowBringYourOwnPublicIpAddress (registered in Dev).
targetScope = 'resourceGroup'

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
param subnetId string = resourceId('Microsoft.Network/virtualNetworks/subnets', 'vnet-airrec-${env}-${regionCode}', 'snet-aca')

resource managedEnvironment 'Microsoft.App/managedEnvironments@2026-01-01' = {
  name: environmentName
  location: location
  tags: {
    workload: 'airrec'
    environment: env
    owner: owner
    costCenter: costCenter
  }
  properties: {
    appLogsConfiguration: { destination: 'azure-monitor' }
    vnetConfiguration: {
      infrastructureSubnetId: subnetId
      internal: false
    }
    workloadProfiles: [
      { name: 'Consumption', workloadProfileType: 'Consumption' }
    ]
    zoneRedundant: false
    publicNetworkAccess: 'Enabled'
    // Provider defaults, declared so a redeploy compares equal (the what-if gate refuses deletes).
    peerAuthentication: { mtls: { enabled: false } }
    peerTrafficConfiguration: { encryption: { enabled: false } }
  }
}

output defaultDomain string = managedEnvironment.properties.defaultDomain
