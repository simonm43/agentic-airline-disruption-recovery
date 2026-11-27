targetScope = 'resourceGroup'

@description('Approved existing developer VNet.')
param devnetVnetName string

@description('ID of the newly created workload VNet.')
param devVnetId string

resource devnetVnet 'Microsoft.Network/virtualNetworks@2026-05-01' existing = {
  name: devnetVnetName
}

resource workloadPeering 'Microsoft.Network/virtualNetworks/virtualNetworkPeerings@2026-05-01' = {
  parent: devnetVnet
  name: 'devnet-to-airrec'
  properties: {
    remoteVirtualNetwork: {
      id: devVnetId
    }
    allowVirtualNetworkAccess: true
    allowForwardedTraffic: false
    allowGatewayTransit: false
    useRemoteGateways: false
  }
}
