<#
.SYNOPSIS
Read-only preflight for infra\main.bicep in one environment; writes
.local\<environment>.parameters.json for tools\env\deploy.ps1.

.DESCRIPTION
Reads infra\environments\<environment>.json and verifies the signed-in user, the environment's
exact subscription and its scoped Owner assignment with RBAC conditions, enumerates the
applicable policy assignments, checks the providers and API versions the template uses, the
providers, the Container Apps feature and the model quota the later steps need, the SQL private
DNS zone, the USD billing currency and the workload group's inventory. With a developer network
(Dev's developer VNet) it also checks that network's CIDR and DNS. It changes nothing in Azure. Exit 2
lists NEEDS_CONTEXT items for a person to review.
#>
[CmdletBinding()]
param(
    [ValidatePattern('^[a-z][a-z0-9]{1,5}$')]
    [string]$Environment = 'dev',
    [string]$SubscriptionId
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$definitionPath = Join-Path $repoRoot "infra\environments\$Environment.json"
if (-not (Test-Path $definitionPath)) {
    throw "No environment definition at $definitionPath."
}
$definition = Get-Content -Raw -Encoding utf8 $definitionPath | ConvertFrom-Json
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
$names = (& $python -m tools.env.environment $Environment) -join "`n" | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw "tools.env.environment could not read environment $Environment." }

$approvedSubscription = $definition.subscriptionId
$tenantId = $definition.tenantId
$resourceGroupName = $names.resourceGroup
$location = $definition.location
$workloadVnetName = $names.vnet
$dnsLinkName = "link-airrec-$($definition.name)"
$developerVnetId = [string]$definition.developerVnetId
$developerSubscription = ''
if ($developerVnetId) {
    if ($developerVnetId -notmatch '^/subscriptions/([0-9a-f-]{36})/resourceGroups/([^/]+)/providers/Microsoft\.Network/virtualNetworks/([^/]+)$') {
        throw "developerVnetId is not a VNet resource ID: $developerVnetId"
    }
    $developerSubscription = $Matches[1]
    $developerGroup = $Matches[2]
    $developerVnetName = $Matches[3]
}
$expectedPolicyAssignments = @($definition.policyAssignments)
$resourceTags = @{
    workload = 'airrec'
    environment = $definition.name
    owner = $definition.tags.owner
    costCenter = $definition.tags.costCenter
}

if ($PSBoundParameters.ContainsKey('SubscriptionId') -and $SubscriptionId -ne $approvedSubscription) {
    throw "Refusing subscription '$SubscriptionId'; environment $Environment permits only $approvedSubscription."
}

# Policy initiatives this preflight recognises by name; an environment lists the ones it expects.
$knownPolicyDefinitions = @{
    DataProtectionSecurityCenter = '/providers/Microsoft.Authorization/policySetDefinitions/9cb3cc7a-b39b-4b82-bc89-e5a5d9ff7b97'
    SqlVmAndArcSqlServersProtection = '/providers/Microsoft.Authorization/policySetDefinitions/c1529623-9fc2-45bc-b84b-b14cd0b7484e'
    OpenSourceRelationalDatabasesProtectionSecurityCenter = '/providers/Microsoft.Authorization/policySetDefinitions/e77fc0b3-f7e9-4c58-bc13-cb753ed8e46e'
}

function Invoke-AzJson {
    param([Parameter(Mandatory)][string[]]$Arguments)

    $output = & az @Arguments --only-show-errors 2>&1
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "Azure CLI failed (exit $exitCode): az $($Arguments -join ' ')`n$($output -join "`n")"
    }

    $jsonText = $output -join "`n"
    if ([string]::IsNullOrWhiteSpace($jsonText)) {
        return $null
    }

    $parsedJson = $jsonText | ConvertFrom-Json
    if ($parsedJson -is [array]) {
        foreach ($item in $parsedJson) {
            $item
        }
        return
    }
    return $parsedJson
}

function Get-IpRange {
    param([Parameter(Mandatory)][string]$Cidr)

    $parts = $Cidr.Split('/')
    $address = [System.Net.IPAddress]::Parse($parts[0])
    if ($address.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
        throw "Only IPv4 CIDRs are accepted by this preflight: $Cidr"
    }

    $prefix = [int]$parts[1]
    if ($prefix -lt 0 -or $prefix -gt 32) {
        throw "Invalid IPv4 prefix in CIDR: $Cidr"
    }

    $octets = $address.GetAddressBytes()
    $numericAddress = [uint64]$octets[0] * 16777216 +
        [uint64]$octets[1] * 65536 +
        [uint64]$octets[2] * 256 +
        [uint64]$octets[3]
    $size = [uint64][math]::Pow(2, 32 - $prefix)
    $start = [uint64]([math]::Floor($numericAddress / $size) * $size)
    return [pscustomobject]@{
        Start = $start
        End = $start + $size - 1
    }
}

function Test-CidrOverlap {
    param(
        [Parameter(Mandatory)][string]$Left,
        [Parameter(Mandatory)][string]$Right
    )

    $leftRange = Get-IpRange $Left
    $rightRange = Get-IpRange $Right
    return $leftRange.Start -le $rightRange.End -and
        $rightRange.Start -le $leftRange.End
}

function Get-DnsServers {
    param([Parameter(Mandatory)][object]$Vnet)

    $dhcpOptions = $Vnet.PSObject.Properties['dhcpOptions']
    if ($null -eq $dhcpOptions -or $null -eq $dhcpOptions.Value) {
        return @()
    }
    $dnsServers = $dhcpOptions.Value.PSObject.Properties['dnsServers']
    if ($null -eq $dnsServers -or $null -eq $dnsServers.Value) {
        return @()
    }
    return @($dnsServers.Value | Where-Object { $_ })
}

function Get-PolicyEffectsGate {
    param([Parameter(Mandatory)][object[]]$Assignments)

    foreach ($expectedName in $expectedPolicyAssignments) {
        if (-not $knownPolicyDefinitions.ContainsKey($expectedName)) {
            throw "Expected policy assignment $expectedName has no recorded definition; stop for policy review."
        }
        $matches = @($Assignments | Where-Object { $_.name -eq $expectedName })
        if ($matches.Count -ne 1 -or
            $matches[0].policyDefinitionId -ne $knownPolicyDefinitions[$expectedName]) {
            throw "Expected policy assignment $expectedName is missing or changed; stop for policy review."
        }
    }

    $unexpected = @(
        $Assignments | Where-Object {
            $expectedPolicyAssignments -notcontains [string]$_.name
        }
    )
    $reviewItems = [System.Collections.Generic.List[string]]::new()
    if ($unexpected.Count -gt 0) {
        $unexpectedNames = @($unexpected | ForEach-Object { [string]$_.name } | Sort-Object -Unique)
        $reviewItems.Add(
            "Subscription-applicable policy assignments changed or include unreviewed entries: $($unexpectedNames -join ', ')."
        )
    }
    $reviewItems.Add(
        'The expected policy assignments are in place, but their initiatives'' parameters, enforcement mode and effects are not resolved here: review them before deploying.'
    )
    return $reviewItems.ToArray()
}

function Assert-ApprovedSqlDnsZoneState {
    param(
        [Parameter(Mandatory)][string]$SubscriptionId,
        [AllowEmptyCollection()][object[]]$Zones = @(),
        [AllowNull()][object]$Zone,
        [AllowEmptyCollection()][object[]]$Links = @()
    )

    $zones = @($Zones)
    if ($developerSubscription -and $SubscriptionId -eq $developerSubscription) {
        if ($zones.Count -gt 0) {
            throw 'A conflicting SQL private DNS zone exists in the developer network subscription.'
        }
        return
    }
    if ($SubscriptionId -ne $approvedSubscription) {
        throw "Unexpected subscription in SQL private DNS zone check: $SubscriptionId"
    }
    if ($zones.Count -eq 0) {
        return
    }

    $expectedGroupId = "/subscriptions/$approvedSubscription/resourceGroups/$resourceGroupName"
    $expectedZoneId = "$expectedGroupId/providers/Microsoft.Network/privateDnsZones/privatelink.database.windows.net"
    if ($zones.Count -ne 1 -or $zones[0].id -ne $expectedZoneId) {
        throw 'A SQL private DNS zone exists outside the exact workload-owned scope.'
    }
    if ($null -eq $Zone -or
        $Zone.id -ne $expectedZoneId -or
        $Zone.location -ne 'global') {
        throw 'The existing SQL private DNS zone does not match the exact workload-owned zone.'
    }
    foreach ($tagName in $resourceTags.Keys) {
        if ($Zone.tags.$tagName -ne $resourceTags[$tagName]) {
            throw "The existing SQL private DNS zone has an unexpected $tagName tag."
        }
    }

    $expectedLinks = @{
        $dnsLinkName = "$expectedGroupId/providers/Microsoft.Network/virtualNetworks/$workloadVnetName"
    }
    if ($developerVnetId) {
        $expectedLinks['link-devnet-developer'] = $developerVnetId
    }
    if ($Links.Count -ne $expectedLinks.Count) {
        throw 'The existing SQL private DNS zone has missing or unexpected VNet links.'
    }
    foreach ($expectedLinkName in $expectedLinks.Keys) {
        $link = @($Links | Where-Object { $_.name -eq $expectedLinkName })
        if ($link.Count -ne 1 -or
            $link[0].virtualNetwork.id -ne $expectedLinks[$expectedLinkName] -or
            $link[0].registrationEnabled -ne $false) {
            throw "The existing SQL private DNS VNet link $expectedLinkName conflicts with the approved topology."
        }
    }
}

function Get-UnexpectedWorkloadResources {
    param(
        [object[]]$Resources,
        [Parameter(Mandatory)][string[]]$AllowedTypes,
        [object[]]$AllowedPlatformResources = @()
    )

    # -contains and -eq compare case-insensitively, as ARM resource types are.
    return @(
        $Resources | Where-Object {
            $resource = $_
            $AllowedTypes -notcontains $resource.type -and -not @(
                $AllowedPlatformResources | Where-Object {
                    $_.Type -eq $resource.type -and $_.Name -ceq $resource.name
                }
            ).Count
        }
    )
}

function Get-BillingCurrency {
    # The cost gate's bounded fetch: it retries only throttling, as long as Cost Management's
    # retry-after hints ask, for at most 10 minutes, and writes no file on failure (D36).
    Push-Location $repoRoot
    try {
        & $python -m tools.env.costgate bodies --out-dir .local\preflight
        if ($LASTEXITCODE -ne 0) {
            throw 'Building the Cost Management query failed.'
        }
        & $python -m tools.env.costgate fetch --subscription $approvedSubscription --url "https://management.azure.com/subscriptions/$approvedSubscription/providers/Microsoft.CostManagement/query?api-version=2025-03-01" --body .local\preflight\cost-query.json --out .local\preflight\cost-actual.json
        if ($LASTEXITCODE -ne 0) {
            throw "Cost Management did not return the billing currency of $Environment."
        }
        $response = Get-Content -Raw -Encoding utf8 .local\preflight\cost-actual.json | ConvertFrom-Json
    }
    finally {
        Pop-Location
    }

    $currencyIndex = -1
    for ($index = 0; $index -lt $response.properties.columns.Count; $index++) {
        if ($response.properties.columns[$index].name -eq 'Currency') {
            $currencyIndex = $index
            break
        }
    }
    # No cost recorded yet this month (a subscription with no cost history): no currency to read.
    if (@($response.properties.rows).Count -eq 0) {
        return ''
    }
    if ($currencyIndex -lt 0) {
        throw "Cost Management returned no billing currency for $Environment."
    }
    $currencies = @(
        $response.properties.rows |
            ForEach-Object { [string]$_[$currencyIndex] } |
            Sort-Object -Unique
    )
    if ($currencies.Count -ne 1) {
        throw "Cost Management returned inconsistent currency values: $($currencies -join ', ')."
    }
    return $currencies[0]
}

$checks = [System.Collections.Generic.List[string]]::new()
$needsContext = [System.Collections.Generic.List[string]]::new()

$account = Invoke-AzJson @(
    'account', 'show', '--subscription', $approvedSubscription, '-o', 'json'
)
if ($account.id -ne $approvedSubscription -or
    $account.tenantId -ne $tenantId -or
    $account.state -ne 'Enabled' -or
    $account.environmentName -ne 'AzureCloud' -or
    $account.user.type -ne 'user') {
    throw "The active Azure CLI account is not an enabled user identity in the tenant of environment $Environment."
}

$user = Invoke-AzJson @(
    'ad', 'signed-in-user', 'show',
    '--query', '{id:id,userPrincipalName:userPrincipalName,mail:mail}',
    '-o', 'json'
)
if ($user.userPrincipalName -ne $account.user.name -or
    [string]::IsNullOrWhiteSpace($user.mail)) {
    throw 'The verified Entra user, Azure CLI user, or budget notification address is missing or inconsistent.'
}
$checks.Add("Azure CLI user identity and the exact $Environment subscription verified.")

$ownerScopes = @($approvedSubscription)
if ($developerSubscription) { $ownerScopes += $developerSubscription }
foreach ($scopeSubscription in $ownerScopes) {
    $assignments = Invoke-AzJson @(
        'role', 'assignment', 'list',
        '--subscription', $scopeSubscription,
        '--assignee-object-id', $user.id,
        '--all', '--include-inherited', '-o', 'json'
    )
    $owner = @(
        $assignments | Where-Object {
            $_.roleDefinitionName -eq 'Owner' -and
            $_.scope -eq "/subscriptions/$scopeSubscription"
        }
    )
    if ($owner.Count -eq 0) {
        throw "The active user has no Owner assignment at approved subscription scope $scopeSubscription."
    }
    if ($owner[0].conditionVersion -ne '2.0' -or
        [string]::IsNullOrWhiteSpace($owner[0].condition)) {
        throw "The expected version-2.0 privileged-role safeguard was not confirmed at $scopeSubscription."
    }
}
$checks.Add('Scoped Owner assignments verified; existing RBAC conditions remain intact.')

try {
    $policyAssignments = Invoke-AzJson @(
        'policy', 'assignment', 'list',
        '--scope', "/subscriptions/$approvedSubscription",
        '--disable-scope-strict-match', 'true',
        '--subscription', $approvedSubscription,
        '--expand', 'LatestDefinitionVersion,EffectiveDefinitionVersion',
        '-o', 'json'
    )
    $policyReviewItems = @(Get-PolicyEffectsGate -Assignments @($policyAssignments))
    $checks.Add('Subscription-applicable policy assignments and definition versions enumerated.')
    foreach ($policyReviewItem in $policyReviewItems) {
        $needsContext.Add($policyReviewItem)
    }
}
catch {
    $needsContext.Add(
        "Subscription-applicable policy enumeration or expected-assignment validation failed: $($_.Exception.Message)"
    )
    $needsContext.Add(
        'Policy initiatives'' parameters, enforcement mode and effects are not resolved: review the subscription''s policy assignments before deploying.'
    )
}

$providerMetadata = @{}
foreach ($provider in @(
    'Microsoft.Resources',
    'Microsoft.Network',
    'Microsoft.Sql',
    'Microsoft.Consumption',
    'Microsoft.ContainerRegistry',
    'Microsoft.OperationalInsights',
    'Microsoft.Insights',
    'Microsoft.ManagedIdentity'
)) {
    $metadata = Invoke-AzJson @(
        'provider', 'show', '--namespace', $provider,
        '--subscription', $approvedSubscription, '-o', 'json'
    )
    if ($metadata.registrationState -ne 'Registered') {
        throw "Provider $provider is not registered in $Environment; register it (guide step 1), this preflight will not."
    }
    $providerMetadata[$provider] = $metadata
}
$requiredApiVersions = @(
    @{ Provider = 'Microsoft.Resources'; Type = 'resourceGroups'; Version = '2025-04-01' },
    @{ Provider = 'Microsoft.Network'; Type = 'virtualNetworks'; Version = '2026-05-01' },
    @{ Provider = 'Microsoft.Network'; Type = 'virtualNetworks/virtualNetworkPeerings'; Version = '2026-05-01' },
    @{ Provider = 'Microsoft.Network'; Type = 'networkSecurityGroups'; Version = '2026-05-01' },
    @{ Provider = 'Microsoft.Network'; Type = 'privateEndpoints'; Version = '2026-05-01' },
    @{ Provider = 'Microsoft.Network'; Type = 'privateEndpoints/privateDnsZoneGroups'; Version = '2026-05-01' },
    @{ Provider = 'Microsoft.Network'; Type = 'privateDnsZones'; Version = '2024-06-01' },
    @{ Provider = 'Microsoft.Network'; Type = 'privateDnsZones/virtualNetworkLinks'; Version = '2024-06-01' },
    @{ Provider = 'Microsoft.Sql'; Type = 'servers'; Version = '2025-01-01' },
    @{ Provider = 'Microsoft.Sql'; Type = 'servers/azureADOnlyAuthentications'; Version = '2025-01-01' },
    @{ Provider = 'Microsoft.Sql'; Type = 'servers/connectionPolicies'; Version = '2025-01-01' },
    @{ Provider = 'Microsoft.Consumption'; Type = 'budgets'; Version = '2023-11-01' }
    @{ Provider = 'Microsoft.ContainerRegistry'; Type = 'registries'; Version = '2025-04-01' }
    @{ Provider = 'Microsoft.OperationalInsights'; Type = 'workspaces'; Version = '2025-07-01' }
    @{ Provider = 'Microsoft.Insights'; Type = 'components'; Version = '2020-02-02' }
    @{ Provider = 'Microsoft.ManagedIdentity'; Type = 'userAssignedIdentities'; Version = '2023-01-31' }
)
foreach ($required in $requiredApiVersions) {
    $resourceType = @(
        $providerMetadata[$required.Provider].resourceTypes |
            Where-Object { $_.resourceType -eq $required.Type }
    )
    if ($resourceType.Count -ne 1 -or
        @($resourceType[0].apiVersions) -notcontains $required.Version) {
        if ($required.Provider -eq 'Microsoft.Resources' -and
            $required.Type -eq 'resourceGroups') {
            $needsContext.Add(
                'Microsoft.Resources metadata does not advertise the approved resourceGroups API version 2025-04-01; retain the approved template version and validate it in the scoped what-if.'
            )
        }
        elseif ($required.Type -like '*/*' -and $resourceType.Count -eq 0) {
            $needsContext.Add(
                "Provider metadata does not expose child type $($required.Provider)/$($required.Type); validate its pinned API version in the scoped what-if."
            )
        }
        else {
            throw "Provider metadata does not advertise $($required.Provider)/$($required.Type)@$($required.Version)."
        }
    }
}
$checks.Add('Required parent and budget API versions are live; missing child metadata is listed as NEEDS_CONTEXT.')

# The later steps' providers, feature and model quota: reported, never registered or requested here.
$laterProviders = @('Microsoft.App', 'Microsoft.CognitiveServices', 'Microsoft.ServiceBus')
foreach ($provider in $laterProviders) {
    $state = (Invoke-AzJson @(
        'provider', 'show', '--namespace', $provider,
        '--subscription', $approvedSubscription, '-o', 'json'
    )).registrationState
    if ($state -ne 'Registered') {
        $needsContext.Add("Provider $provider is $state in $Environment; the later steps need it registered.")
    }
}
$feature = Invoke-AzJson @(
    'feature', 'show', '--namespace', 'Microsoft.Network', '--name', 'AllowBringYourOwnPublicIpAddress',
    '--subscription', $approvedSubscription, '-o', 'json'
)
if ($feature.properties.state -ne 'Registered') {
    $needsContext.Add("Feature Microsoft.Network/AllowBringYourOwnPublicIpAddress is $($feature.properties.state); the Container Apps environment needs it registered.")
}
$cognitive = (Invoke-AzJson @(
    'provider', 'show', '--namespace', 'Microsoft.CognitiveServices',
    '--subscription', $approvedSubscription, '-o', 'json'
)).registrationState
if ($cognitive -eq 'Registered') {
    $usages = @(Invoke-AzJson @(
        'cognitiveservices', 'usage', 'list', '--location', $location,
        '--subscription', $approvedSubscription, '-o', 'json'
    ))
    $deployed = @{}
    # A new environment's resource group does not exist yet: nothing is deployed there.
    $groupFound = Invoke-AzJson @(
        'group', 'exists', '--subscription', $approvedSubscription,
        '--name', $resourceGroupName, '-o', 'json'
    )
    $accountExists = if ($groupFound -eq $true) {
        @(Invoke-AzJson @(
            'resource', 'list', '--subscription', $approvedSubscription,
            '--resource-group', $resourceGroupName,
            '--resource-type', 'Microsoft.CognitiveServices/accounts', '-o', 'json'
        )) | Where-Object { $_.name -eq $names.foundryAccount }
    }
    if ($accountExists) {
        foreach ($existing in @(Invoke-AzJson @(
            'cognitiveservices', 'account', 'deployment', 'list',
            '--name', $names.foundryAccount, '--resource-group', $resourceGroupName,
            '--subscription', $approvedSubscription, '-o', 'json'
        ))) {
            $deployed[$existing.name] = [int]$existing.sku.capacity
        }
    }
    foreach ($model in $definition.models) {
        $usage = @($usages | Where-Object { $_.name.value -eq $model.quota })
        if ($usage.Count -ne 1) {
            $needsContext.Add("No $($model.quota) quota is listed in $location for $($model.name).")
            continue
        }
        $own = if ($deployed.ContainsKey($model.name)) { $deployed[$model.name] } else { 0 }
        $free = [double]$usage[0].limit - [double]$usage[0].currentValue + $own
        if ($free -lt [double]$model.capacity) {
            $needsContext.Add("$($model.quota) in ${location}: $free units free, $($model.name) needs $($model.capacity).")
        }
        else {
            $checks.Add("$($model.quota) in ${location}: $free units free for $($model.name) ($($model.capacity) needed).")
        }
    }
}
else {
    $needsContext.Add("Model quota in $location is unreadable until Microsoft.CognitiveServices is registered.")
}

$dnsScopes = @($approvedSubscription)
if ($developerSubscription) { $dnsScopes += $developerSubscription }
$sqlDnsZones = @{}
foreach ($scopeSubscription in $dnsScopes) {
    $sqlDnsZones[$scopeSubscription] = @(
        Invoke-AzJson @(
        'network', 'private-dns', 'zone', 'list',
        '--subscription', $scopeSubscription,
        '--query', "[?name=='privatelink.database.windows.net'].{id:id,resourceGroup:resourceGroup}",
        '-o', 'json'
        )
    )
}
$expectedSqlDnsZoneId = "/subscriptions/$approvedSubscription/resourceGroups/$resourceGroupName/providers/Microsoft.Network/privateDnsZones/privatelink.database.windows.net"
$sqlDnsZone = $null
$sqlDnsLinks = @()
if ($sqlDnsZones[$approvedSubscription].Count -eq 1 -and
    $sqlDnsZones[$approvedSubscription][0].id -eq $expectedSqlDnsZoneId) {
    $sqlDnsZone = Invoke-AzJson @(
        'network', 'private-dns', 'zone', 'show',
        '--subscription', $approvedSubscription,
        '--resource-group', $resourceGroupName,
        '--name', 'privatelink.database.windows.net', '-o', 'json'
    )
    $sqlDnsLinks = @(
        Invoke-AzJson @(
            'network', 'private-dns', 'link', 'vnet', 'list',
            '--subscription', $approvedSubscription,
            '--resource-group', $resourceGroupName,
            '--zone-name', 'privatelink.database.windows.net', '-o', 'json'
        )
    )
}
foreach ($scopeSubscription in $dnsScopes) {
    if ($scopeSubscription -eq $approvedSubscription) {
        Assert-ApprovedSqlDnsZoneState `
            -SubscriptionId $approvedSubscription `
            -Zones $sqlDnsZones[$approvedSubscription] `
            -Zone $sqlDnsZone `
            -Links $sqlDnsLinks
    }
    else {
        Assert-ApprovedSqlDnsZoneState `
            -SubscriptionId $scopeSubscription `
            -Zones $sqlDnsZones[$scopeSubscription] `
            -Zone $null `
            -Links @()
    }
}
$checks.Add(
    'No conflicting SQL private DNS zone exists; only the exact owned zone with its approved VNet links may be reused.'
)

if ($developerVnetId) {
    $developerVnet = Invoke-AzJson @(
        'network', 'vnet', 'show',
        '--subscription', $developerSubscription,
        '--resource-group', $developerGroup,
        '--name', $developerVnetName, '-o', 'json'
    )
    $existingPeering = Invoke-AzJson @(
        'network', 'vnet', 'peering', 'show',
        '--subscription', $developerSubscription,
        '--resource-group', $developerGroup,
        '--vnet-name', $developerVnetName,
        '--name', 'spoke-to-hub', '-o', 'json'
    )
    $hubVnetId = [string]$existingPeering.remoteVirtualNetwork.id
    $expectedHubVnetId = "/subscriptions/$developerSubscription/resourceGroups/hubnet/providers/Microsoft.Network/virtualNetworks/hub-vnet"
    if ($hubVnetId -ne $expectedHubVnetId) {
        throw 'The existing developer-to-hub peering target changed from the approved hub VNet.'
    }
    $hubVnet = Invoke-AzJson @(
        'network', 'vnet', 'show',
        '--subscription', $developerSubscription,
        '--ids', $hubVnetId, '-o', 'json'
    )
    $plannedPrefix = $definition.network.addressPrefix
    foreach ($prefix in @($developerVnet.addressSpace.addressPrefixes) +
        @($hubVnet.addressSpace.addressPrefixes)) {
        if (Test-CidrOverlap $plannedPrefix ([string]$prefix)) {
            throw "Workload CIDR $plannedPrefix overlaps existing VNet range $prefix."
        }
    }
    $developerDnsServers = @(Get-DnsServers $developerVnet)
    $hubDnsServers = @(Get-DnsServers $hubVnet)
    if ($developerDnsServers.Count -gt 0 -or $hubDnsServers.Count -gt 0) {
        throw 'The peered VNets no longer use Azure-provided DNS; stop for network review.'
    }
    $checks.Add('Workload CIDR is disjoint from the developer network and its hub; peering target and DNS are unchanged.')
}
else {
    $checks.Add('No developer network: nothing peers with the workload VNet.')
}

$currency = Get-BillingCurrency
if (-not $currency) {
    $needsContext.Add("No cost is recorded in $Environment this month, so Cost Management shows no billing currency yet; the cost gates count USD and refuse any other currency once costs appear.")
}
elseif ($currency -ne 'USD') {
    throw "Billing currency of $Environment is $currency, not the approved USD basis."
}
else {
    $checks.Add("Cost Management confirms USD billing; the budget is USD $($definition.budgetUsd) a month.")
}

$groupExists = Invoke-AzJson @(
    'group', 'exists', '--subscription', $approvedSubscription,
    '--name', $resourceGroupName, '-o', 'json'
)
if ($groupExists -eq $true) {
    $group = Invoke-AzJson @(
        'group', 'show', '--subscription', $approvedSubscription,
        '--name', $resourceGroupName, '-o', 'json'
    )
    foreach ($tagName in $resourceTags.Keys) {
        if ($group.tags.$tagName -ne $resourceTags[$tagName]) {
            throw "Existing resource group $resourceGroupName is not tagged as the approved workload."
        }
    }
    $existingResources = Invoke-AzJson @(
        'resource', 'list', '--subscription', $approvedSubscription,
        '--resource-group', $resourceGroupName, '-o', 'json'
    )
    # The workload group holds this template's platform and the ARROW apps deployed from
    # infra\ops-app, infra\systems, infra\testbench and infra\foundry.
    $allowedTypes = @(
        'Microsoft.Network/virtualNetworks',
        'Microsoft.Network/networkSecurityGroups',
        'Microsoft.Network/networkInterfaces',
        'Microsoft.Network/privateDnsZones',
        'Microsoft.Network/privateDnsZones/virtualNetworkLinks',
        'Microsoft.Network/privateEndpoints',
        'Microsoft.Sql/servers',
        'Microsoft.Sql/servers/databases',
        'Microsoft.Consumption/budgets',
        'Microsoft.CognitiveServices/accounts',
        'Microsoft.CognitiveServices/accounts/projects',
        'Microsoft.ContainerRegistry/registries',
        'Microsoft.ManagedIdentity/userAssignedIdentities',
        'Microsoft.App/managedEnvironments',
        'Microsoft.App/containerApps',
        'Microsoft.App/jobs',
        'Microsoft.ServiceBus/namespaces',
        'Microsoft.OperationalInsights/workspaces',
        'Microsoft.Insights/components'
    )
    # Platform side effects allowed by exact type and name only. Creating Application
    # Insights adds its default smart detection action group (Learn:
    # alerts-smart-detections-migration). Never delete it.
    $allowedPlatformResources = @(
        @{ Type = 'microsoft.insights/actionGroups'; Name = 'Application Insights Smart Detection' }
    )
    $unexpected = @(
        Get-UnexpectedWorkloadResources -Resources $existingResources -AllowedTypes $allowedTypes -AllowedPlatformResources $allowedPlatformResources
    )
    if ($unexpected.Count -gt 0) {
        $unexpectedNames = ($unexpected | ForEach-Object { "$($_.type)/$($_.name)" }) -join ', '
        throw "The workload resource group contains resources outside the IaC footprint: $unexpectedNames."
    }
}
$checks.Add('Workload resource-group scope and existing inventory checked without applying changes.')

$parameters = @{
    '$schema' = 'https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#'
    contentVersion = '1.0.0.0'
    parameters = @{
        env = @{ value = $definition.name }
        regionCode = @{ value = $definition.regionCode }
        location = @{ value = $location }
        resourceGroupName = @{ value = $resourceGroupName }
        owner = @{ value = $definition.tags.owner }
        costCenter = @{ value = $definition.tags.costCenter }
        developerObjectId = @{ value = $user.id }
        developerUpn = @{ value = $user.userPrincipalName }
        sqlAdministrator = @{ value = $definition.sqlAdministrator }
        budgetContactEmail = @{ value = $user.mail }
        budgetStartDate = @{ value = (Get-Date).ToUniversalTime().ToString('yyyy-MM-01') }
        budgetAmount = @{ value = [int]$definition.budgetUsd }
        logDailyCapGb = @{ value = [int]$definition.logDailyCapGb }
        developerVnetId = @{ value = $developerVnetId }
        vnetAddressPrefix = @{ value = $definition.network.addressPrefix }
        privateEndpointSubnetPrefix = @{ value = $definition.network.privateEndpointSubnetPrefix }
        acaSubnetPrefix = @{ value = $definition.network.acaSubnetPrefix }
    }
}
$localDirectory = Join-Path $repoRoot '.local'
New-Item -ItemType Directory -Force -Path $localDirectory | Out-Null
$parameterFile = Join-Path $localDirectory "$Environment.parameters.json"
$parameterJson = $parameters | ConvertTo-Json -Depth 12
[System.IO.File]::WriteAllText(
    $parameterFile,
    $parameterJson,
    [System.Text.UTF8Encoding]::new($false)
)

Write-Output "[GREEN] Read-only preflight completed against the $Environment subscription $approvedSubscription."
foreach ($check in $checks) {
    Write-Output "[GREEN] $check"
}
foreach ($item in $needsContext) {
    Write-Output "[NEEDS_CONTEXT] $item"
}
Write-Output "[GREEN] Local-only what-if parameters written to $parameterFile; no credentials or tokens were written."
Write-Output '[RED] No Azure resources were applied.'
exit 2
