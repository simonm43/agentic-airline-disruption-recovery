// Records the existing developer-to-workload peering outside the Dev template, so deploying
// main.bicep never writes to the developer's subscription. No plan step deploys this file.
targetScope = 'resourceGroup'

@description('Approved existing developer VNet.')
param devnetVnetName string

@description('ID of the workload VNet.')
param devVnetId string

module devnetPeering 'modules/devnet-peering.bicep' = {
  name: 'devnetPeering'
  params: {
    devnetVnetName: devnetVnetName
    devVnetId: devVnetId
  }
}
