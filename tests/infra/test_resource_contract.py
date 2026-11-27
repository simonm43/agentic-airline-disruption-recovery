import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPILED_TEMPLATE = ROOT / ".local" / "infra-contract" / "main.json"
COMPILED_DEVNET_TEMPLATE = ROOT / ".local" / "infra-contract" / "devnet-peering.json"
COMPILED_PARAMETERS = ROOT / ".local" / "infra-contract" / "dev.parameters.json"


def _flatten_resources(template: dict[str, Any]) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    declared = template.get("resources", [])
    # languageVersion 2.0 (symbolic-name) templates key resources by name.
    if isinstance(declared, dict):
        declared = list(declared.values())
    for resource in declared:
        # `existing` references are read, never deployed.
        if resource.get("existing", False):
            continue
        resources.append(resource)
        nested_template = resource.get("properties", {}).get("template")
        if isinstance(nested_template, dict):
            resources.extend(_flatten_resources(nested_template))
    return resources


@pytest.fixture(scope="module")
def compiled_resources() -> list[dict[str, Any]]:
    assert COMPILED_TEMPLATE.is_file(), (
        f"compiled ARM template missing: {COMPILED_TEMPLATE}; "
        "follow the offline contract build in infra\\README.md before running these tests"
    )
    template = json.loads(COMPILED_TEMPLATE.read_text(encoding="utf-8"))
    return _flatten_resources(template)


@pytest.fixture(scope="module")
def dev_parameters() -> dict[str, Any]:
    assert COMPILED_PARAMETERS.is_file(), (
        f"compiled Dev parameters missing: {COMPILED_PARAMETERS}; "
        "follow the offline contract build in infra\\README.md before running these tests"
    )
    return json.loads(COMPILED_PARAMETERS.read_text(encoding="utf-8"))["parameters"]


def _of_type(resources: list[dict[str, Any]], resource_type: str) -> list[dict[str, Any]]:
    return [
        resource
        for resource in resources
        if resource.get("type", "").casefold() == resource_type.casefold()
    ]


def _single(resources: list[dict[str, Any]], resource_type: str) -> dict[str, Any]:
    matches = _of_type(resources, resource_type)
    assert len(matches) == 1, f"expected one {resource_type}, found {len(matches)}"
    return matches[0]


def test_sql_is_private_entra_only_and_proxy(
    compiled_resources: list[dict[str, Any]],
) -> None:
    server = _single(compiled_resources, "Microsoft.Sql/servers")
    # Always private: the tenant's Modify policy forces Disabled (D28).
    assert server["properties"]["publicNetworkAccess"] == "Disabled"
    assert server["properties"]["minimalTlsVersion"] == "1.2"
    administrators = server["properties"]["administrators"]
    assert administrators["administratorType"] == "ActiveDirectory"
    assert administrators["azureADOnlyAuthentication"] is True
    # Dev keeps the developer as admin; a new environment names the migration job's identity,
    # an application admin identified by its client ID.
    assert administrators["login"] == (
        "[if(variables('byIdentity'), variables('adminIdentityName'), parameters('developerUpn'))]"
    )
    assert administrators["principalType"] == "[if(variables('byIdentity'), 'Application', 'User')]"
    assert administrators["sid"].startswith("[if(variables('byIdentity'), reference(")
    assert administrators["sid"].endswith(".clientId, parameters('developerObjectId'))]")
    assert "administratorLoginPassword" not in server["properties"]
    # Declared to match live Dev, so a redeploy does not remove the server's identity.
    assert server["identity"] == {"type": "SystemAssigned"}

    entra_only = _single(compiled_resources, "Microsoft.Sql/servers/azureADOnlyAuthentications")
    assert entra_only["name"].endswith(", 'Default')]")
    assert entra_only["properties"]["azureADOnlyAuthentication"] is True

    connection_policy = _single(compiled_resources, "Microsoft.Sql/servers/connectionPolicies")
    assert connection_policy["name"].endswith(", 'default')]")
    assert connection_policy["properties"]["connectionType"] == "Proxy"

    # The retired `airrec` database is gone; ARROW's airrec-ops is in infra\ops-app.
    assert _of_type(compiled_resources, "Microsoft.Sql/servers/databases") == []
    assert _of_type(compiled_resources, "Microsoft.Sql/servers/firewallRules") == []


def test_compiled_sql_fqdn_output_uses_the_dot_prefixed_environment_suffix(
    compiled_resources: list[dict[str, Any]],
) -> None:
    sql_module = next(
        resource
        for resource in _of_type(compiled_resources, "Microsoft.Resources/deployments")
        if resource.get("name") == "airrecSql"
    )
    fqdn_output = sql_module["properties"]["template"]["outputs"]["sqlServerFqdn"]

    assert fqdn_output["value"] == (
        "[format('{0}{1}', variables('serverName'), environment().suffixes.sqlServerHostname)]"
    )


def test_network_uses_only_the_approved_vnet_and_private_endpoint_subnet(
    compiled_resources: list[dict[str, Any]],
    dev_parameters: dict[str, Any],
) -> None:
    vnet = _single(compiled_resources, "Microsoft.Network/virtualNetworks")
    assert vnet["properties"]["addressSpace"]["addressPrefixes"] == [
        "[parameters('vnetAddressPrefix')]"
    ]
    subnets = [
        subnet
        for subnet in vnet["properties"]["subnets"]
        if subnet["name"] == "snet-private-endpoints"
    ]
    assert len(subnets) == 1
    subnet = subnets[0]
    assert subnet["properties"]["addressPrefix"] == ("[parameters('privateEndpointSubnetPrefix')]")
    assert subnet["properties"]["privateEndpointNetworkPolicies"] == "Disabled"
    assert dev_parameters["vnetAddressPrefix"]["value"] == "10.60.0.0/24"
    assert dev_parameters["privateEndpointSubnetPrefix"]["value"] == "10.60.0.0/27"
    assert dev_parameters["location"]["value"] == "swedencentral"
    assert dev_parameters["resourceGroupName"]["value"] == "rg-airrec-dev-swc"

    endpoint = _single(compiled_resources, "Microsoft.Network/privateEndpoints")
    assert endpoint["name"] == (
        "[format('pe-airrec-sql-{0}-{1}', parameters('env'), parameters('regionCode'))]"
    )
    assert endpoint["properties"]["subnet"]["id"] == "[parameters('subnetId')]"
    connections = endpoint["properties"]["privateLinkServiceConnections"]
    assert len(connections) == 1
    assert connections[0]["properties"]["groupIds"] == ["sqlServer"]
    assert "Microsoft.Sql/servers" in connections[0]["properties"]["privateLinkServiceId"]
    # Provider defaults, declared so what-if reports no change against live Dev.
    assert endpoint["properties"]["billingSku"] == "PayAsYouGo"
    assert endpoint["properties"]["ipVersionType"] == "IPv4"
    assert vnet["properties"]["privateEndpointVNetPolicies"] == "Disabled"

    zone = _single(compiled_resources, "Microsoft.Network/privateDnsZones")
    assert zone["name"] == "privatelink.database.windows.net"
    assert zone["location"] == "global"
    assert zone["tags"] == "[parameters('tags')]"
    dns_links = {
        link["name"]: (link.get("condition"), link["properties"])
        for link in _of_type(
            compiled_resources, "Microsoft.Network/privateDnsZones/virtualNetworkLinks"
        )
    }
    # The workload link always; the developer network's link (Dev's developer VNet) only when one is
    # given, so a new environment depends on no developer network.
    assert dns_links == {
        "[format('{0}/{1}', 'privatelink.database.windows.net', "
        "format('link-airrec-{0}', parameters('env')))]": (
            None,
            {
                "virtualNetwork": {
                    "id": (
                        "[resourceId('Microsoft.Network/virtualNetworks', parameters('vnetName'))]"
                    )
                },
                "registrationEnabled": False,
                "resolutionPolicy": "Default",
            },
        ),
        "[format('{0}/{1}', 'privatelink.database.windows.net', 'link-devnet-developer')]": (
            "[variables('withDeveloperNetwork')]",
            {
                "virtualNetwork": {"id": "[parameters('developerVnetId')]"},
                "registrationEnabled": False,
                "resolutionPolicy": "Default",
            },
        ),
    }
    peering = _single(
        compiled_resources, "Microsoft.Network/virtualNetworks/virtualNetworkPeerings"
    )
    assert peering["condition"] == "[variables('withDeveloperNetwork')]"
    assert dev_parameters["developerVnetId"]["value"].endswith("/virtualNetworks/developer-vnet")

    zone_group = _single(
        compiled_resources, "Microsoft.Network/privateEndpoints/privateDnsZoneGroups"
    )
    assert zone_group["name"] == (
        "[format('{0}/{1}', format('pe-airrec-sql-{0}-{1}', parameters('env'), "
        "parameters('regionCode')), 'sql')]"
    )
    configs = zone_group["properties"]["privateDnsZoneConfigs"]
    assert [config["properties"]["privateDnsZoneId"] for config in configs] == [
        "[parameters('privateDnsZoneId')]"
    ]


def test_every_subnet_has_an_iac_owned_nsg(
    compiled_resources: list[dict[str, Any]],
) -> None:
    nsg_names = {
        "snet-private-endpoints": (
            "[format('{0}-snet-private-endpoints-nsg-{1}', parameters('vnetName'), "
            "parameters('location'))]"
        ),
        "snet-aca": (
            "[format('nsg-airrec-aca-{0}-{1}', parameters('env'), parameters('regionCode'))]"
        ),
    }
    vnet = _single(compiled_resources, "Microsoft.Network/virtualNetworks")
    subnets = vnet["properties"]["subnets"]
    assert {subnet["name"] for subnet in subnets} == set(nsg_names)
    # No subnet keeps implicit default outbound access, which Azure is retiring.
    for subnet in subnets:
        assert subnet["properties"]["defaultOutboundAccess"] is False, subnet["name"]
        nsg_id = subnet["properties"]["networkSecurityGroup"]["id"]
        assert nsg_names[subnet["name"]][1:-1] in nsg_id

    nsgs = _of_type(compiled_resources, "Microsoft.Network/networkSecurityGroups")
    assert {nsg["name"] for nsg in nsgs} == set(nsg_names.values())
    for nsg in nsgs:
        assert nsg["location"] == "[parameters('location')]"
        assert nsg["tags"] == "[parameters('tags')]"
        assert nsg["properties"]["securityRules"] == []

    network_module = next(
        resource
        for resource in _of_type(compiled_resources, "Microsoft.Resources/deployments")
        if resource.get("name") == "airrecNetwork"
    )
    assert network_module["properties"]["parameters"]["tags"]["value"] == (
        "[variables('resourceTags')]"
    )


def test_main_template_deploys_only_into_dev(
    compiled_resources: list[dict[str, Any]],
) -> None:
    deployments = _of_type(compiled_resources, "Microsoft.Resources/deployments")
    assert deployments
    assert not [resource for resource in deployments if "subscriptionId" in resource]
    assert not [resource for resource in deployments if resource.get("name") == "devnetPeering"]


def test_devnet_scope_contains_only_the_approved_peering_child() -> None:
    assert COMPILED_DEVNET_TEMPLATE.is_file(), (
        f"compiled developer-network peering template missing: {COMPILED_DEVNET_TEMPLATE}; "
        "follow the offline contract build in infra\\README.md before running these tests"
    )
    devnet_resources = _flatten_resources(
        json.loads(COMPILED_DEVNET_TEMPLATE.read_text(encoding="utf-8"))
    )
    devnet_module = next(
        resource
        for resource in _of_type(devnet_resources, "Microsoft.Resources/deployments")
        if resource.get("name") == "devnetPeering"
    )
    nested_template = devnet_module["properties"]["template"]
    devnet_resource_types = {
        resource["type"]
        for resource in _flatten_resources(nested_template)
        if resource.get("type") != "Microsoft.Resources/deployments"
    }
    assert devnet_resource_types == {"Microsoft.Network/virtualNetworks/virtualNetworkPeerings"}
    peering = _single(
        _flatten_resources(nested_template),
        "Microsoft.Network/virtualNetworks/virtualNetworkPeerings",
    )
    assert peering["properties"]["allowVirtualNetworkAccess"] is True
    assert peering["properties"]["allowForwardedTraffic"] is False
    assert peering["properties"]["allowGatewayTransit"] is False
    assert peering["properties"]["useRemoteGateways"] is False


def test_cross_module_resource_ids_do_not_short_circuit_provider_validation(
    compiled_resources: list[dict[str, Any]],
) -> None:
    template = json.loads(COMPILED_TEMPLATE.read_text(encoding="utf-8"))
    variables = template["variables"]
    modules = {
        resource.get("name"): resource
        for resource in _of_type(compiled_resources, "Microsoft.Resources/deployments")
    }
    sql_parameters = modules["airrecSql"]["properties"]["parameters"]

    subnet_id = sql_parameters["subnetId"]["value"]
    dns_zone_id = sql_parameters["privateDnsZoneId"]["value"]
    assert subnet_id == "[variables('privateEndpointSubnetId')]"
    assert dns_zone_id == "[variables('privateDnsZoneId')]"
    assert "resourceId(" in variables["privateEndpointSubnetId"]
    assert "snet-private-endpoints" in variables["privateEndpointSubnetId"]
    assert "variables('workloadVnetName')" in variables["privateEndpointSubnetId"]
    assert variables["workloadVnetName"] == (
        "[format('vnet-airrec-{0}-{1}', parameters('env'), parameters('regionCode'))]"
    )
    assert "resourceId(" in variables["privateDnsZoneId"]
    assert variables["privateDnsZoneName"] == (
        "[format('privatelink{0}', environment().suffixes.sqlServerHostname)]"
    )
    assert all(
        "reference(" not in variables[key]
        for key in ("privateEndpointSubnetId", "privateDnsZoneId")
    )
    assert any("airrecNetwork" in dependency for dependency in modules["airrecSql"]["dependsOn"])


def test_budget_and_footprint_stay_within_the_retained_platform(
    compiled_resources: list[dict[str, Any]],
) -> None:
    budget = _single(compiled_resources, "Microsoft.Consumption/budgets")
    assert budget["properties"]["amount"] == "[parameters('amount')]"
    assert budget["properties"]["category"] == "Cost"
    assert budget["properties"]["timeGrain"] == "Monthly"
    # ARM stores the start as a timestamp and defaults the end to ten calendar years later, so
    # both are declared in that form (dateTimeAdd's P10Y counts 365-day years).
    template = json.loads(COMPILED_TEMPLATE.read_text(encoding="utf-8"))
    budget_module = next(
        resource
        for resource in _of_type(compiled_resources, "Microsoft.Resources/deployments")
        if resource.get("name") == "airrecBudget"
    )
    assert budget_module["properties"]["template"]["variables"]["startDate"] == (
        "[dateTimeAdd(parameters('budgetStartDate'), 'PT0S')]"
    )
    assert budget["properties"]["timePeriod"] == {
        "startDate": "[variables('startDate')]",
        "endDate": (
            "[format('{0}{1}', add(int(substring(variables('startDate'), 0, 4)), 10), "
            "substring(variables('startDate'), 4))]"
        ),
    }
    assert "budgetStartDate" in template["parameters"]
    # Dev's USD 900 stays the default; an environment passes its own budget.
    assert template["parameters"]["budgetAmount"]["defaultValue"] == 900
    assert budget_module["properties"]["parameters"]["amount"] == {
        "value": "[parameters('budgetAmount')]"
    }
    notifications = budget["properties"]["notifications"]
    assert set(notifications) == {"threshold50", "threshold80", "threshold100", "forecast100"}
    for notification in notifications.values():
        assert notification["enabled"] is True
        assert notification["contactEmails"] == ["[parameters('contactEmail')]"]
    forecast = notifications["forecast100"]
    assert forecast["thresholdType"] == "Forecasted"
    assert forecast["threshold"] == 100
    assert forecast["operator"] == "GreaterThanOrEqualTo"

    resource_types = {
        resource.get("type", "").casefold()
        for resource in compiled_resources
        if resource.get("type", "").casefold() != "microsoft.resources/deployments"
    }
    assert resource_types == {
        "microsoft.resources/resourcegroups",
        "microsoft.network/virtualnetworks",
        "microsoft.network/networksecuritygroups",
        "microsoft.network/virtualnetworks/virtualnetworkpeerings",
        "microsoft.network/privatednszones",
        "microsoft.network/privatednszones/virtualnetworklinks",
        "microsoft.network/privateendpoints",
        "microsoft.network/privateendpoints/privatednszonegroups",
        "microsoft.sql/servers",
        "microsoft.sql/servers/azureadonlyauthentications",
        "microsoft.sql/servers/connectionpolicies",
        "microsoft.consumption/budgets",
        "microsoft.authorization/roleassignments",
        "microsoft.managedidentity/userassignedidentities",
        "microsoft.containerregistry/registries",
        "microsoft.operationalinsights/workspaces",
        "microsoft.insights/components",
    }
    # The group; the VNet, two NSGs, the developer peering, the SQL DNS zone and its two links;
    # the SQL admin identity (identity mode only), the SQL server, Entra-only auth, connection
    # policy, private endpoint and zone group; the registry and the developer's AcrPush; the
    # workspace, the component and the budget.
    assert (
        sum(
            resource.get("type", "").casefold() != "microsoft.resources/deployments"
            for resource in compiled_resources
        )
        == 19
    )


def test_every_located_resource_carries_the_workload_tags(
    compiled_resources: list[dict[str, Any]],
) -> None:
    template = json.loads(COMPILED_TEMPLATE.read_text(encoding="utf-8"))
    assert template["variables"]["resourceTags"] == {
        "workload": "airrec",
        "environment": "[parameters('env')]",
        "owner": "[parameters('owner')]",
        "costCenter": "[parameters('costCenter')]",
    }
    group = _single(compiled_resources, "Microsoft.Resources/resourceGroups")
    assert group["tags"] == "[variables('resourceTags')]"
    # The region is a parameter; the preflight checks model availability and quota there.
    assert "allowedValues" not in template["parameters"]["location"]
    for module in _of_type(compiled_resources, "Microsoft.Resources/deployments"):
        if "tags" in module["properties"]["parameters"]:
            assert module["properties"]["parameters"]["tags"] == {
                "value": "[variables('resourceTags')]"
            }
    untagged_types = {
        # Child resources and extension resources take no tags of their own.
        "microsoft.network/privatednszones/virtualnetworklinks",
        "microsoft.network/virtualnetworks/virtualnetworkpeerings",
        "microsoft.network/privateendpoints/privatednszonegroups",
        "microsoft.sql/servers/azureadonlyauthentications",
        "microsoft.sql/servers/connectionpolicies",
        "microsoft.authorization/roleassignments",
        "microsoft.consumption/budgets",
    }
    for resource in compiled_resources:
        kind = resource.get("type", "").casefold()
        if kind in untagged_types or kind == "microsoft.resources/deployments":
            continue
        assert resource.get("tags") in {
            "[parameters('tags')]",
            "[variables('resourceTags')]",
        }, resource["type"]


def test_azure_maps_is_retired(
    compiled_resources: list[dict[str, Any]],
    dev_parameters: dict[str, Any],
) -> None:
    # D103: the operator map draws a bundled outline; no Maps account is declared.
    template = json.loads(COMPILED_TEMPLATE.read_text(encoding="utf-8"))
    assert not _of_type(compiled_resources, "Microsoft.Maps/accounts")
    assert "mapsLocation" not in template["parameters"]
    assert "mapsLocation" not in dev_parameters
    assert not {"mapsaccountid", "mapsclientid"} & {name.casefold() for name in template["outputs"]}


def test_outputs_do_not_expose_credentials_or_connection_strings(
    compiled_resources: list[dict[str, Any]],
) -> None:
    template = json.loads(COMPILED_TEMPLATE.read_text(encoding="utf-8"))
    output_names = {name.casefold() for name in template.get("outputs", {})}
    assert output_names
    assert not any(
        token in output_name
        for output_name in output_names
        for token in ("token", "secret", "password", "connectionstring")
    )
