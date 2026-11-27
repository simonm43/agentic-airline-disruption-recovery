using './main.bicep'

// Dev's values. The deployment itself uses the parameters tools\env\preflight.ps1 writes from
// infra\environments\dev.json; this file feeds the offline contract build.
param env = 'dev'
param regionCode = 'swc'
param location = 'swedencentral'
param resourceGroupName = 'rg-airrec-dev-swc'
param developerVnetId = '/subscriptions/00000000-0000-4000-8000-000000000002/resourceGroups/developer-rg/providers/Microsoft.Network/virtualNetworks/developer-vnet'
param vnetAddressPrefix = '10.60.0.0/24'
param privateEndpointSubnetPrefix = '10.60.0.0/27'
param acaSubnetPrefix = '10.60.0.64/26'
param sqlAdministrator = 'developer'
param budgetAmount = 900
param logDailyCapGb = 1
param developerObjectId = readEnvironmentVariable('AIRREC_DEV_OBJECT_ID')
param developerUpn = readEnvironmentVariable('AIRREC_DEV_UPN')
param budgetContactEmail = readEnvironmentVariable('AIRREC_DEV_BUDGET_EMAIL')
param budgetStartDate = readEnvironmentVariable('AIRREC_DEV_BUDGET_START_DATE')
