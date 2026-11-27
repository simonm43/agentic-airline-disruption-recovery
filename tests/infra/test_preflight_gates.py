from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT = ROOT / "tools" / "env" / "preflight.ps1"
MAIN_TEMPLATE = ROOT / "infra" / "main.bicep"
COMPILED_TEMPLATE = ROOT / ".local" / "infra-contract" / "main.json"

# The retired first-phase platform must not reappear in the preflight.
RETIRED = (
    "Microsoft.Fabric",
    "Microsoft.Storage",
    "Microsoft.DocumentDB",
    "Microsoft.EventGrid",
    "Microsoft.Maps",
    "api.fabric.microsoft.com",
    "fabric",
    "ODBC",
    "GP_S_Gen5",
    "developerEgressIp",
    "foundryAllowAllNetworks",
    "deployWorker",
    "scenarioId",
    "snet-aca-int",
)


def _compiled() -> dict[str, Any]:
    assert COMPILED_TEMPLATE.is_file(), (
        f"compiled ARM template missing: {COMPILED_TEMPLATE}; "
        "follow the offline contract build in infra\\README.md before running these tests"
    )
    return json.loads(COMPILED_TEMPLATE.read_text(encoding="utf-8"))


def _allowed_inventory_types(preflight: str) -> set[str]:
    allowed_types = re.search(r"\$allowedTypes = @\((.*?)\n\s*\)", preflight, re.S)
    assert allowed_types is not None
    return {value.casefold() for value in re.findall(r"'([^']+)'", allowed_types.group(1))}


def _template_resources(template: dict[str, Any]) -> list[dict[str, Any]]:
    resources = template.get("resources", [])
    # languageVersion 2.0 (symbolic-name) templates key resources by name.
    if isinstance(resources, dict):
        resources = list(resources.values())
    return [resource for resource in resources if not resource.get("existing", False)]


def _located_types(template: dict[str, Any]) -> set[str]:
    located: set[str] = set()
    for resource in _template_resources(template):
        if "location" in resource:
            located.add(resource["type"])
        nested_template = resource.get("properties", {}).get("template")
        if isinstance(nested_template, dict):
            located |= _located_types(nested_template)
    return located


def _typed_api_versions(template: dict[str, Any]) -> set[tuple[str, str]]:
    typed: set[tuple[str, str]] = set()
    for resource in _template_resources(template):
        typed.add((resource["type"], resource["apiVersion"]))
        nested_template = resource.get("properties", {}).get("template")
        if isinstance(nested_template, dict):
            typed |= _typed_api_versions(nested_template)
    return typed


def test_preflight_parameters_cover_exactly_the_template() -> None:
    preflight = PREFLIGHT.read_text(encoding="utf-8")
    template_parameters = set(
        re.findall(r"^param (\w+) ", MAIN_TEMPLATE.read_text(encoding="utf-8"), re.M)
    )
    generated_parameters = set(re.findall(r"^\s+(\w+) = @\{ value = ", preflight, re.M))
    assert generated_parameters == template_parameters
    assert generated_parameters == set(_compiled()["parameters"])
    assert "budgetStartDate = @{ value = (Get-Date).ToUniversalTime().ToString('yyyy-MM-01') }" in (
        preflight
    )
    for retired in RETIRED:
        assert retired.casefold() not in preflight.casefold(), retired


def test_preflight_checks_every_template_type_at_its_compiled_api_version() -> None:
    preflight = PREFLIGHT.read_text(encoding="utf-8")
    providers = re.search(r"foreach \(\$provider in @\((.*?)\)\)", preflight, re.S)
    assert providers is not None
    listed_providers = set(re.findall(r"'([^']+)'", providers.group(1)))
    rows = set(
        re.findall(r"@\{ Provider = '([^']+)'; Type = '([^']+)'; Version = '([^']+)' \}", preflight)
    )
    compiled = {
        (resource_type, api_version)
        for resource_type, api_version in _typed_api_versions(_compiled())
        if resource_type.split("/", 1)[0] not in {"Microsoft.Resources", "Microsoft.Authorization"}
        or resource_type == "Microsoft.Resources/resourceGroups"
    }
    expected = {
        (resource_type.split("/", 1)[0], resource_type.split("/", 1)[1], api_version)
        for resource_type, api_version in compiled
    }
    assert rows == expected
    assert listed_providers == {provider for provider, _, _ in rows}
    # D28: no SQL firewall rule, so its API version is no longer a preflight requirement.
    assert "servers/firewallRules" not in preflight


def test_preflight_inventory_allows_every_tracked_template_type() -> None:
    template = _compiled()
    allowed = _allowed_inventory_types(PREFLIGHT.read_text(encoding="utf-8"))

    # The inventory lists resources inside the workload group, not the group itself.
    tracked = {
        resource_type
        for resource_type in _located_types(template)
        if resource_type.casefold() != "microsoft.resources/resourcegroups"
    }
    assert tracked
    assert {
        resource_type for resource_type in tracked if resource_type.casefold() not in allowed
    } == set()
    assert "microsoft.network/networkinterfaces" in allowed
    # The ARROW apps share the group: their environment, apps, jobs, identities, the SQL
    # database airrec-ops, Service Bus and the Foundry account (ops-app, systems, testbench,
    # foundry templates).
    assert {
        "microsoft.app/managedenvironments",
        "microsoft.app/containerapps",
        "microsoft.app/jobs",
        "microsoft.managedidentity/userassignedidentities",
        "microsoft.sql/servers/databases",
        "microsoft.servicebus/namespaces",
        "microsoft.cognitiveservices/accounts",
        "microsoft.cognitiveservices/accounts/projects",
    } <= allowed
    # Leftovers of the retired platform are reported, never tolerated.
    assert (
        not {
            "microsoft.fabric/capacities",
            "microsoft.storage/storageaccounts",
            "microsoft.documentdb/databaseaccounts",
            "microsoft.eventgrid/systemtopics",
        }
        & allowed
    )


def test_preflight_inventory_tolerates_only_the_smart_detection_action_group() -> None:
    """Creating Application Insights adds the default action group 'Application Insights
    Smart Detection' (Learn: alerts-smart-detections-migration; seen in Dev).
    The preflight tolerates that one resource by type and name, never the whole type."""
    preflight = PREFLIGHT.read_text(encoding="utf-8")
    assert "microsoft.insights/actiongroups" not in _allowed_inventory_types(preflight)
    platform = re.search(r"\$allowedPlatformResources = @\((.*?)\n\s*\)", preflight, re.S)
    assert platform is not None
    rows = re.findall(r"@\{ Type = '([^']+)'; Name = '([^']+)' \}", platform.group(1))
    assert [(kind.casefold(), name) for kind, name in rows] == [
        ("microsoft.insights/actiongroups", "Application Insights Smart Detection")
    ]
    assert (
        "Get-UnexpectedWorkloadResources -Resources $existingResources "
        "-AllowedTypes $allowedTypes -AllowedPlatformResources $allowedPlatformResources"
    ) in preflight


READ_ONLY_VERBS = {"show", "list", "exists"}


def test_preflight_only_reads_azure_with_an_explicit_subscription() -> None:
    """The preflight verifies identity, subscription, RBAC conditions and inventory; it changes
    nothing. Every Azure CLI call names its subscription and uses a read-only verb."""
    preflight = PREFLIGHT.read_text(encoding="utf-8")
    calls = re.findall(r"Invoke-AzJson @\((.*?)\)\s*\n", preflight, re.S)
    calls = [call for call in calls if "'" in call]
    assert len(calls) >= 10
    for call in calls:
        words = re.findall(r"'([^']*)'", call)
        command = [word for word in words if not word.startswith("-")]
        # Microsoft Graph (`az ad`) has no subscription; every ARM call names its subscription.
        if command[0] != "ad":
            assert "--subscription" in words, call
        assert READ_ONLY_VERBS & set(command), call
        for verb in ("create", "update", "delete", "set", "register", "start", "stop"):
            assert verb not in command, call
    assert "throw \"Refusing subscription '$SubscriptionId'" in preflight
    assert "$account.user.type -ne 'user'" in preflight
    assert "$owner[0].conditionVersion -ne '2.0'" in preflight
    assert "deployment sub create" not in preflight


def test_preflight_policy_and_dns_safety_gates() -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is required to exercise the preflight safety helpers.")

    script = Path(__file__).with_name("test_preflight_gates.ps1")
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "Preflight policy and DNS safety gate tests passed." in result.stdout


def test_preflight_currency_query_goes_through_the_bounded_fetch() -> None:
    """D36: the preflight's billing-currency query uses the same Cost Management bucket as the
    cost gate and runs before every gated stage, so it honours the same retry-after hints
    within the same bound, and fails closed."""
    preflight = PREFLIGHT.read_text(encoding="utf-8")
    function = preflight[preflight.index("function Get-BillingCurrency") :]
    function = function[: function.index("\n}\n") + 3]
    assert "Invoke-RestMethod" not in preflight
    assert "Start-Sleep" not in function
    fetches = [line.strip() for line in function.splitlines() if "tools.env.costgate fetch" in line]
    assert fetches == [
        "& $python -m tools.env.costgate fetch --subscription $approvedSubscription "
        '--url "https://management.azure.com/subscriptions/$approvedSubscription'
        '/providers/Microsoft.CostManagement/query?api-version=2025-03-01" '
        "--body .local\\preflight\\cost-query.json --out .local\\preflight\\cost-actual.json"
    ]
    assert function.index("tools.env.costgate bodies --out-dir .local\\preflight") < (
        function.index("tools.env.costgate fetch")
    )
    assert re.search(r"if \(\$LASTEXITCODE -ne 0\) \{\s*throw", function[function.index("fetch") :])
