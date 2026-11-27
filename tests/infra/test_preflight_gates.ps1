$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$preflightPath = Join-Path $root 'tools\env\preflight.ps1'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $preflightPath,
    [ref]$tokens,
    [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) {
    throw "Preflight script did not parse: $($parseErrors[0].Message)"
}

$functionNames = @(
    'Invoke-AzJson',
    'Get-PolicyEffectsGate',
    'Assert-ApprovedSqlDnsZoneState',
    'Get-UnexpectedWorkloadResources'
)
foreach ($functionName in $functionNames) {
    $functionAst = $ast.Find(
        {
            param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                $node.Name -eq $functionName
        },
        $true
    )
    if ($null -eq $functionAst) {
        throw "Expected preflight helper $functionName was not found."
    }
    Invoke-Expression $functionAst.Extent.Text
}

function az {
    $global:LASTEXITCODE = 0
    if ($args -contains 'empty') {
        return '[]'
    }
    '{"outer":{"inner":{"value":"parsed"}}}'
}
$nestedJson = Invoke-AzJson -Arguments @('account', 'show')
if ($nestedJson.outer.inner.value -ne 'parsed') {
    throw 'Invoke-AzJson did not parse nested Azure CLI JSON.'
}
$emptyJson = @(Invoke-AzJson -Arguments @('empty'))
if ($emptyJson.Count -ne 0) {
    throw 'Invoke-AzJson wrapped an empty JSON array as a result item.'
}
Remove-Item Function:\az

$approvedSubscription = '00000000-0000-4000-8000-000000000013'
$developerSubscription = '00000000-0000-4000-8000-000000000002'
$devnetSubscription = $developerSubscription
$resourceGroupName = 'rg-airrec-dev-swc'
$workloadVnetName = 'vnet-airrec-dev-swc'
$dnsLinkName = 'link-airrec-dev'
$developerVnetId = "/subscriptions/$developerSubscription/resourceGroups/developer-rg/providers/Microsoft.Network/virtualNetworks/developer-vnet"
$resourceTags = @{
    workload = 'airrec'
    environment = 'dev'
    owner = 'simonm43'
    costCenter = 'arrow'
}
$knownPolicyDefinitions = @{
    DataProtectionSecurityCenter = '/providers/Microsoft.Authorization/policySetDefinitions/9cb3cc7a-b39b-4b82-bc89-e5a5d9ff7b97'
    SqlVmAndArcSqlServersProtection = '/providers/Microsoft.Authorization/policySetDefinitions/c1529623-9fc2-45bc-b84b-b14cd0b7484e'
    OpenSourceRelationalDatabasesProtectionSecurityCenter = '/providers/Microsoft.Authorization/policySetDefinitions/e77fc0b3-f7e9-4c58-bc13-cb753ed8e46e'
}
$expectedPolicyAssignments = @($knownPolicyDefinitions.Keys)

$policyAssignments = @(
    [pscustomobject]@{
        name = 'DataProtectionSecurityCenter'
        policyDefinitionId = '/providers/Microsoft.Authorization/policySetDefinitions/9cb3cc7a-b39b-4b82-bc89-e5a5d9ff7b97'
    }
    [pscustomobject]@{
        name = 'SqlVmAndArcSqlServersProtection'
        policyDefinitionId = '/providers/Microsoft.Authorization/policySetDefinitions/c1529623-9fc2-45bc-b84b-b14cd0b7484e'
    }
    [pscustomobject]@{
        name = 'OpenSourceRelationalDatabasesProtectionSecurityCenter'
        policyDefinitionId = '/providers/Microsoft.Authorization/policySetDefinitions/e77fc0b3-f7e9-4c58-bc13-cb753ed8e46e'
    }
)
$policyGate = @(Get-PolicyEffectsGate -Assignments $policyAssignments)
if (-not ($policyGate -match 'effects.*review|review.*effects')) {
    throw 'Policy assignment enumeration incorrectly cleared the policy-effects gate.'
}

$changedAssignment = $policyAssignments[0].psobject.Copy()
$changedAssignment.policyDefinitionId = '/providers/Microsoft.Authorization/policyDefinitions/changed'
$changedPolicies = @($policyAssignments[1], $policyAssignments[2], $changedAssignment)
try {
    Get-PolicyEffectsGate -Assignments $changedPolicies | Out-Null
    throw 'A changed policy assignment was accepted.'
}
catch {
    if ($_.Exception.Message -eq 'A changed policy assignment was accepted.') {
        throw
    }
}

$devGroupId = "/subscriptions/$approvedSubscription/resourceGroups/$resourceGroupName"
$devnetVnetId = "/subscriptions/$devnetSubscription/resourceGroups/developer-rg/providers/Microsoft.Network/virtualNetworks/developer-vnet"
$devVnetId = "$devGroupId/providers/Microsoft.Network/virtualNetworks/vnet-airrec-dev-swc"
$zoneId = "$devGroupId/providers/Microsoft.Network/privateDnsZones/privatelink.database.windows.net"
$ownedZone = [pscustomobject]@{
    id = $zoneId
    location = 'global'
    tags = $resourceTags
}
$ownedLinks = @(
    [pscustomobject]@{
        name = 'link-airrec-dev'
        virtualNetwork = [pscustomobject]@{ id = $devVnetId }
        registrationEnabled = $false
    }
    [pscustomobject]@{
        name = 'link-devnet-developer'
        virtualNetwork = [pscustomobject]@{ id = $devnetVnetId }
        registrationEnabled = $false
    }
)

Assert-ApprovedSqlDnsZoneState `
    -SubscriptionId $approvedSubscription `
    -Zones @([pscustomobject]@{ id = $zoneId }) `
    -Zone $ownedZone `
    -Links $ownedLinks

$unrelatedZoneId = '/subscriptions/00000000-0000-4000-8000-000000000013/resourceGroups/other-rg/providers/Microsoft.Network/privateDnsZones/privatelink.database.windows.net'
try {
    Assert-ApprovedSqlDnsZoneState `
        -SubscriptionId $approvedSubscription `
        -Zones @([pscustomobject]@{ id = $unrelatedZoneId }) `
        -Zone $ownedZone `
        -Links $ownedLinks
    throw 'An unrelated same-name SQL private DNS zone was accepted.'
}
catch {
    if ($_.Exception.Message -eq 'An unrelated same-name SQL private DNS zone was accepted.') {
        throw
    }
}

$wrongLink = $ownedLinks[0].psobject.Copy()
$wrongLink.virtualNetwork = [pscustomobject]@{ id = $devnetVnetId }
$wrongLink.registrationEnabled = $false
try {
    Assert-ApprovedSqlDnsZoneState `
        -SubscriptionId $approvedSubscription `
        -Zones @([pscustomobject]@{ id = $zoneId }) `
        -Zone $ownedZone `
        -Links @($wrongLink, $ownedLinks[1])
    throw 'A conflicting VNet link was accepted.'
}
catch {
    if ($_.Exception.Message -eq 'A conflicting VNet link was accepted.') {
        throw
    }
}

Assert-ApprovedSqlDnsZoneState `
    -SubscriptionId $approvedSubscription `
    -Zones @() `
    -Zone $null `
    -Links @()
Assert-ApprovedSqlDnsZoneState `
    -SubscriptionId $devnetSubscription `
    -Zones @() `
    -Zone $null `
    -Links @()

# Inventory: listed types pass case-insensitively; a platform side effect passes only by
# exact type and name (Application Insights creates its smart detection action group).
$inventoryTypes = @('Microsoft.Sql/servers', 'Microsoft.ServiceBus/namespaces')
$platformResources = @(
    @{ Type = 'microsoft.insights/actionGroups'; Name = 'Application Insights Smart Detection' }
)
$inventory = @(
    [pscustomobject]@{ type = 'Microsoft.Sql/servers'; name = 'sql-a' },
    [pscustomobject]@{ type = 'microsoft.sql/SERVERS'; name = 'sql-b' },
    [pscustomobject]@{
        type = 'microsoft.insights/actiongroups'
        name = 'Application Insights Smart Detection'
    },
    [pscustomobject]@{ type = 'microsoft.insights/actiongroups'; name = 'ag-other' },
    [pscustomobject]@{ type = 'Microsoft.Network/publicIPAddresses'; name = 'pip-x' }
)
$unexpected = @(
    Get-UnexpectedWorkloadResources `
        -Resources $inventory `
        -AllowedTypes $inventoryTypes `
        -AllowedPlatformResources $platformResources
)
$unexpectedNames = @($unexpected | ForEach-Object { $_.name }) -join ','
if ($unexpectedNames -ne 'ag-other,pip-x') {
    throw "Unexpected inventory classification: '$unexpectedNames'."
}
$none = @(
    Get-UnexpectedWorkloadResources `
        -Resources @() `
        -AllowedTypes $inventoryTypes `
        -AllowedPlatformResources $platformResources
)
if ($none.Count -ne 0) {
    throw 'An empty inventory produced unexpected resources.'
}

Write-Output 'Preflight policy and DNS safety gate tests passed.'
