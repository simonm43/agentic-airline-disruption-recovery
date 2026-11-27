targetScope = 'resourceGroup'

@description('Azure region of the environment.')
param location string

@description('Workload and environment tags.')
param tags object

@description('Short environment name, for example dev or prd.')
param env string

@description('Short region code, for example swc.')
param regionCode string

@description('Name of the workload VNet.')
param vnetName string

@description('Address space of the workload VNet.')
param vnetAddressPrefix string

@description('Private-endpoint subnet CIDR.')
param privateEndpointSubnetPrefix string

@description('Container Apps infrastructure subnet CIDR (/27 or larger for workload profiles).')
param acaSubnetPrefix string

@description('Resource ID of the developer VNet to peer with and link to the SQL DNS zone (Dev only); empty for none.')
param developerVnetId string = ''

var withDeveloperNetwork = !empty(developerVnetId)

// In Dev this adopts the NSG that external (probably platform) automation attached, named as
// that automation names them; every environment uses the same pattern. Declaring it stops a
// redeploy detaching it; a subnet that has an NSG gets no second one.
resource privateEndpointSubnetNsg 'Microsoft.Network/networkSecurityGroups@2026-05-01' = {
  name: '${vnetName}-snet-private-endpoints-nsg-${location}'
  location: location
  tags: tags
  properties: {
    securityRules: []
  }
}

// The Container Apps subnet has an IaC-owned NSG with only the default rules, so external
// automation has no reason to attach one and the platform's required traffic stays allowed.
resource acaSubnetNsg 'Microsoft.Network/networkSecurityGroups@2026-05-01' = {
  name: 'nsg-airrec-aca-${env}-${regionCode}'
  location: location
  tags: tags
  properties: {
    securityRules: []
  }
}

resource workloadVnet 'Microsoft.Network/virtualNetworks@2026-05-01' = {
  name: vnetName
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        vnetAddressPrefix
      ]
    }
    // Provider default, declared so what-if reports no change against live Dev.
    privateEndpointVNetPolicies: 'Disabled'
    subnets: [
      {
        name: 'snet-private-endpoints'
        properties: {
          addressPrefix: privateEndpointSubnetPrefix
          privateEndpointNetworkPolicies: 'Disabled'
          defaultOutboundAccess: false
          networkSecurityGroup: {
            id: privateEndpointSubnetNsg.id
          }
        }
      }
      {
        // Hosts the apps' Container Apps environment (infra\systems\environment.bicep).
        name: 'snet-aca'
        properties: {
          addressPrefix: acaSubnetPrefix
          defaultOutboundAccess: false
          delegations: [
            {
              name: 'Microsoft.App.environments'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
          networkSecurityGroup: {
            id: acaSubnetNsg.id
          }
        }
      }
    ]
  }
}

// Dev only: the workload side of the peering with the developer VM's VNet; its other half
// is recorded in infra\devnet-peering.bicep. New environments have no developer network: whatever
// needs the private network runs inside the environment.
resource devnetPeering 'Microsoft.Network/virtualNetworks/virtualNetworkPeerings@2026-05-01' = if (withDeveloperNetwork) {
  parent: workloadVnet
  name: 'airrec-to-devnet'
  properties: {
    remoteVirtualNetwork: {
      id: developerVnetId
    }
    allowVirtualNetworkAccess: true
    allowForwardedTraffic: false
    allowGatewayTransit: false
    useRemoteGateways: false
  }
}

resource privateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  #disable-next-line no-hardcoded-env-urls
  name: 'privatelink.database.windows.net'
  location: 'global'
  tags: tags
}

resource workloadDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: privateDnsZone
  name: 'link-airrec-${env}'
  location: 'global'
  properties: {
    virtualNetwork: {
      id: workloadVnet.id
    }
    registrationEnabled: false
    resolutionPolicy: 'Default'
  }
}

resource devnetDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (withDeveloperNetwork) {
  parent: privateDnsZone
  name: 'link-devnet-developer'
  location: 'global'
  properties: {
    virtualNetwork: {
      id: developerVnetId
    }
    registrationEnabled: false
    resolutionPolicy: 'Default'
  }
}

output vnetId string = workloadVnet.id
output privateEndpointSubnetId string = resourceId(
  'Microsoft.Network/virtualNetworks/subnets',
  workloadVnet.name,
  'snet-private-endpoints'
)
output privateDnsZoneId string = privateDnsZone.id
