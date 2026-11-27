"""Offline contract for the shared Dev platform that infra/main.bicep deploys.

The owner retired the first-phase platform: Fabric, Cosmos DB, the storage
accounts, the private Foundry account, the worker, its six identities and the airrec database.
main.bicep keeps only what ARROW uses: the workload network, private SQL, the registry,
monitoring and the budget. The apps and their own resources live in infra/ops-app,
infra/systems, infra/testbench and infra/foundry.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPILED_TEMPLATE = ROOT / ".local" / "infra-contract" / "main.json"
DEV_COMPILED_PARAMETERS = ROOT / ".local" / "infra-contract" / "dev.parameters.json"
DEV_PARAMETER_FILE = ROOT / "infra" / "dev.bicepparam"
MODULE_DIRECTORY = ROOT / "infra" / "modules"

SUFFIX = "uniqueString(subscription().subscriptionId, resourceGroup().id)"
ACR_PUSH = "8311e382-0749-4cb8-b61a-304f252e45ec"
REGISTRY = "Microsoft.ContainerRegistry/registries"
NETWORK_SECURITY_GROUP = "Microsoft.Network/networkSecurityGroups"
ROLE_ASSIGNMENT = "Microsoft.Authorization/roleAssignments"

PLATFORM_MODULES = {
    "airrecNetwork",
    "airrecAcr",
    "airrecSql",
    "airrecBudget",
    "airrecMonitoring",
}
RETIRED_PROVIDERS = (
    "Microsoft.Fabric/",
    "Microsoft.DocumentDB/",
    "Microsoft.Storage/",
    "Microsoft.CognitiveServices/",
    "Microsoft.App/",
    "Microsoft.Maps/",
)
RETIRED_NAMES = (
    "fabric",
    "cosmos",
    "foundry",
    "identityNames",
    "deployWorker",
    "deployContainerApps",
    "containerApps",
    "workerImageTag",
    "scenarioId",
    "developerEgressIp",
    "snet-aca-int",
    "acaInternalSubnetPrefix",
    "id-airrec-worker-dev",
)
ACA_DELEGATION = [
    {
        "name": "Microsoft.App.environments",
        "properties": {"serviceName": "Microsoft.App/environments"},
    }
]


def _template_resources(template: dict[str, Any]) -> list[dict[str, Any]]:
    """Deployed resources of one template; symbolic-name (languageVersion 2.0) aware."""
    resources = template.get("resources", [])
    if isinstance(resources, dict):
        resources = list(resources.values())
    return [resource for resource in resources if not resource.get("existing", False)]


def _deployments_at_any_depth(template: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for resource in _template_resources(template):
        if resource.get("type", "").casefold() != "microsoft.resources/deployments":
            continue
        found.append(resource)
        nested = resource.get("properties", {}).get("template")
        if isinstance(nested, dict):
            found.extend(_deployments_at_any_depth(nested))
    return found


@pytest.fixture(scope="module")
def template() -> dict[str, Any]:
    assert COMPILED_TEMPLATE.is_file(), (
        f"compiled ARM template missing: {COMPILED_TEMPLATE}; "
        "follow the offline contract build in infra\\README.md before running these tests"
    )
    return json.loads(COMPILED_TEMPLATE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def modules(template: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        resource["name"]: resource
        for resource in _template_resources(template)
        if resource.get("type") == "Microsoft.Resources/deployments"
    }


def _module_template(modules: dict[str, dict[str, Any]], name: str) -> dict[str, Any]:
    assert name in modules, f"module {name} is not deployed by main.bicep"
    nested = modules[name]["properties"]["template"]
    assert isinstance(nested, dict)
    return nested


def _of_type(template: dict[str, Any], resource_type: str) -> list[dict[str, Any]]:
    return [
        resource
        for resource in _template_resources(template)
        if resource.get("type", "").casefold() == resource_type.casefold()
    ]


def _single(template: dict[str, Any], resource_type: str) -> dict[str, Any]:
    matches = _of_type(template, resource_type)
    assert len(matches) == 1, f"expected one {resource_type}, found {len(matches)}"
    return matches[0]


def _subnet(network: dict[str, Any], name: str) -> dict[str, Any]:
    vnet = _single(network, "Microsoft.Network/virtualNetworks")
    matches = [subnet for subnet in vnet["properties"]["subnets"] if subnet["name"] == name]
    assert len(matches) == 1, f"expected one subnet {name}"
    return matches[0]


def test_main_deploys_only_the_retained_platform_modules(
    modules: dict[str, dict[str, Any]],
) -> None:
    assert set(modules) == PLATFORM_MODULES
    referenced = {
        match
        for path in (ROOT / "infra").glob("*.bicep")
        for match in re.findall(r"'modules/([\w-]+)\.bicep'", path.read_text(encoding="utf-8"))
    }
    # Every module file is used, either by main.bicep or by the separate peering record.
    assert {path.stem for path in MODULE_DIRECTORY.glob("*.bicep")} == referenced


def test_the_retired_platform_is_not_declared(template: dict[str, Any]) -> None:
    identities = []
    for deployment in _deployments_at_any_depth(template):
        nested = deployment["properties"]["template"]
        for resource in _template_resources(nested):
            assert not resource["type"].startswith(RETIRED_PROVIDERS), resource["type"]
            if resource["type"].startswith("Microsoft.ManagedIdentity/"):
                identities.append(resource)
    # The retired worker identities stay gone; the one identity is the SQL admin a new
    # environment's migration job runs as, and only when sqlAdministrator is 'identity'.
    assert [(item["name"], item.get("condition")) for item in identities] == [
        ("[variables('adminIdentityName')]", "[variables('byIdentity')]")
    ]
    compiled = json.dumps(template)
    dev_source = DEV_PARAMETER_FILE.read_text(encoding="utf-8")
    dev_compiled = DEV_COMPILED_PARAMETERS.read_text(encoding="utf-8")
    for name in RETIRED_NAMES:
        assert name.casefold() not in compiled.casefold(), name
        assert name.casefold() not in dev_source.casefold(), name
        assert name.casefold() not in dev_compiled.casefold(), name


def test_every_deployment_is_incremental(template: dict[str, Any]) -> None:
    """main.bicep deploys at subscription scope, which has no Complete mode, and Bicep compiles
    every module to a nested Incremental deployment, which leaves resources missing from the
    template untouched (Learn: deployment-modes). Removing one takes an explicit delete."""
    assert template["$schema"].endswith("/subscriptionDeploymentTemplate.json#")
    deployments = _deployments_at_any_depth(template)
    assert deployments
    for deployment in deployments:
        assert deployment["properties"]["mode"] == "Incremental", deployment["name"]


def test_acr_is_basic_without_admin_user(modules: dict[str, dict[str, Any]]) -> None:
    acr = _module_template(modules, "airrecAcr")
    assert acr["variables"]["registryName"] == (
        f"[format('crairrec{{0}}{{1}}', parameters('env'), {SUFFIX})]"
    )
    registry = _single(acr, REGISTRY)
    assert registry["name"] == "[variables('registryName')]"
    assert registry["sku"] == {"name": "Basic"}
    assert registry["location"] == "[parameters('location')]"
    assert registry["tags"] == "[parameters('tags')]"
    properties = registry["properties"]
    assert properties["adminUserEnabled"] is False
    assert properties["anonymousPullEnabled"] is False
    # Documented exception (spec section 11.3): Basic has no private link; access is RBAC-only.
    assert properties["publicNetworkAccess"] == "Enabled"
    # The live provider defaults, so what-if reports no change.
    assert properties["dataEndpointEnabled"] is False
    assert properties["encryption"] == {"status": "disabled"}
    assert "listcredentials" not in json.dumps(acr).casefold()


def test_only_the_developer_is_granted_a_role(modules: dict[str, dict[str, Any]]) -> None:
    """The retired identities' AcrPull and Metrics Publisher rows are gone; the apps grant
    their own roles in their templates. The platform grants only the developer's AcrPush."""
    assignments = [
        (name, assignment)
        for name in modules
        for assignment in _of_type(_module_template(modules, name), ROLE_ASSIGNMENT)
    ]
    assert [name for name, _ in assignments] == ["airrecAcr"]
    assignment = assignments[0][1]
    acr = _module_template(modules, "airrecAcr")
    assert assignment["apiVersion"] == "2022-04-01"
    assert assignment["scope"] == (
        "[resourceId('Microsoft.ContainerRegistry/registries', variables('registryName'))]"
    )
    assert ACR_PUSH == acr["variables"]["acrPushRoleId"]
    assert "variables('acrPushRoleId')" in assignment["properties"]["roleDefinitionId"]
    assert assignment["properties"]["principalId"] == "[parameters('developerObjectId')]"
    assert assignment["properties"]["principalType"] == "User"
    assert modules["airrecAcr"]["properties"]["parameters"]["developerObjectId"] == {
        "value": "[parameters('developerObjectId')]"
    }


def test_platform_outputs(template: dict[str, Any]) -> None:
    expected = {
        "resourceGroupId": None,
        "sqlServerId": ("airrecSql", "sqlServerId"),
        "sqlServerFqdn": ("airrecSql", "sqlServerFqdn"),
        "acrName": ("airrecAcr", "name"),
        "acrLoginServer": ("airrecAcr", "loginServer"),
        "logAnalyticsWorkspaceId": ("airrecMonitoring", "logAnalyticsWorkspaceId"),
        "logAnalyticsCustomerId": ("airrecMonitoring", "logAnalyticsCustomerId"),
        "appInsightsName": ("airrecMonitoring", "appInsightsName"),
    }
    outputs = template["outputs"]
    assert set(outputs) == set(expected)
    for name, source in expected.items():
        assert outputs[name]["type"] == "string", name
        if source is None:
            continue
        module_name, module_output = source
        assert f"'{module_name}')" in outputs[name]["value"], name
        assert outputs[name]["value"].endswith(f".outputs.{module_output}.value]"), name


def test_monitoring_is_entra_only_and_capped(modules: dict[str, dict[str, Any]]) -> None:
    monitoring = _module_template(modules, "airrecMonitoring")
    assert set(modules["airrecMonitoring"]["properties"]["parameters"]) == {
        "location",
        "tags",
        "env",
        "regionCode",
        "dailyCapGb",
    }
    workspace = _single(monitoring, "Microsoft.OperationalInsights/workspaces")
    assert (
        workspace["name"]
        == "[format('log-airrec-{0}-{1}', parameters('env'), parameters('regionCode'))]"
    )
    assert workspace["apiVersion"] == "2025-07-01"
    assert workspace["location"] == "[parameters('location')]"
    assert workspace["tags"] == "[parameters('tags')]"
    assert workspace["properties"]["sku"] == {"name": "PerGB2018"}
    assert workspace["properties"]["retentionInDays"] == 30
    assert workspace["properties"]["workspaceCapping"] == {
        "dailyQuotaGb": "[parameters('dailyCapGb')]"
    }
    assert monitoring["parameters"]["dailyCapGb"]["defaultValue"] == 1
    assert workspace["properties"]["features"]["disableLocalAuth"] is True

    component = _single(monitoring, "Microsoft.Insights/components")
    assert (
        component["name"]
        == "[format('appi-airrec-{0}-{1}', parameters('env'), parameters('regionCode'))]"
    )
    assert component["apiVersion"] == "2020-02-02"
    assert component["kind"] == "web"
    assert component["location"] == "[parameters('location')]"
    assert component["tags"] == "[parameters('tags')]"
    properties = component["properties"]
    assert properties["Application_Type"] == "web"
    assert properties["DisableLocalAuth"] is True
    assert properties["IngestionMode"] == "LogAnalytics"
    assert "Microsoft.OperationalInsights/workspaces" in properties["WorkspaceResourceId"]
    assert "log-airrec-{0}-{1}" in properties["WorkspaceResourceId"]

    outputs = monitoring["outputs"]
    assert set(outputs) == {"logAnalyticsWorkspaceId", "logAnalyticsCustomerId", "appInsightsName"}
    assert "customerId" in outputs["logAnalyticsCustomerId"]["value"]
    assert not any(
        "connectionstring" in json.dumps(output).casefold() for output in outputs.values()
    )


def test_aca_subnet_is_delegated_with_its_own_nsg(
    template: dict[str, Any],
    modules: dict[str, dict[str, Any]],
) -> None:
    """snet-aca hosts the environment's Container Apps environment
    (infra\\systems\\environment.bicep), so the template keeps the subnet exactly as deployed and
    a VNet PUT never modifies it."""
    network = _module_template(modules, "airrecNetwork")
    vnet = _single(network, "Microsoft.Network/virtualNetworks")
    assert [subnet["name"] for subnet in vnet["properties"]["subnets"]] == [
        "snet-private-endpoints",
        "snet-aca",
    ]
    properties = _subnet(network, "snet-aca")["properties"]
    assert set(properties) == {
        "addressPrefix",
        "defaultOutboundAccess",
        "delegations",
        "networkSecurityGroup",
    }
    assert properties["addressPrefix"] == "[parameters('acaSubnetPrefix')]"
    assert properties["delegations"] == ACA_DELEGATION
    assert properties["defaultOutboundAccess"] is False
    aca_nsg = "[format('nsg-airrec-aca-{0}-{1}', parameters('env'), parameters('regionCode'))]"
    assert aca_nsg[1:-1] in properties["networkSecurityGroup"]["id"]

    nsgs = [nsg for nsg in _of_type(network, NETWORK_SECURITY_GROUP) if nsg["name"] == aca_nsg]
    assert len(nsgs) == 1
    assert nsgs[0]["properties"]["securityRules"] == []
    assert nsgs[0]["location"] == "[parameters('location')]"
    assert nsgs[0]["tags"] == "[parameters('tags')]"

    assert modules["airrecNetwork"]["properties"]["parameters"]["acaSubnetPrefix"] == {
        "value": "[parameters('acaSubnetPrefix')]"
    }
    assert template["parameters"]["acaSubnetPrefix"]["defaultValue"] == "10.60.0.64/26"
    dev_parameters = json.loads(DEV_COMPILED_PARAMETERS.read_text(encoding="utf-8"))
    assert dev_parameters["parameters"]["acaSubnetPrefix"]["value"] == "10.60.0.64/26"


def test_no_nested_deployment_leaves_the_dev_subscription(template: dict[str, Any]) -> None:
    # Ruling R1: the Dev template still names the developer VNet for its DNS links and the
    # workload-side peering, so "devnet" is checked in deployment names and links only.
    deployments = _deployments_at_any_depth(template)
    assert deployments
    for deployment in deployments:
        name = str(deployment.get("name", ""))
        assert "subscriptionId" not in deployment, name
        assert "devnet" not in name.casefold(), name
        link = deployment.get("properties", {}).get("templateLink")
        assert link is None or "devnet" not in json.dumps(link).casefold(), name
    for deployment in _template_resources(template):
        if deployment.get("type") == "Microsoft.Resources/deployments":
            assert deployment["resourceGroup"] == "[parameters('resourceGroupName')]"
