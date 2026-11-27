"""Offline guard for the tenant's Azure Policy Modify effects on public network access (D28).

The management-group assignment ``GovDeployPolicies`` applies the Modify policies
``AzureSQL_PublicNetwork_Modify`` and ``StorageAccount_PublicNetwork_Modify``. During every
deployment they rewrite ``publicNetworkAccess`` to ``Disabled`` on SQL servers and storage
accounts. What-if does not evaluate Modify effects, so a template asking for anything else
previews cleanly, then drifts or fails at apply time: the A2 Step 10 developer-access apply
failed on the SQL firewall rule with ``DenyPublicEndpointEnabled``. This test
is the only offline guard against that.
"""

import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPILED_TEMPLATE = ROOT / ".local" / "infra-contract" / "main.json"

POLICY_ASSIGNMENT = "GovDeployPolicies"
GUARDED_TYPES = {
    "microsoft.sql/servers": "AzureSQL_PublicNetwork_Modify",
    "microsoft.storage/storageaccounts": "StorageAccount_PublicNetwork_Modify",
}


def _resources(template: dict[str, Any]) -> list[dict[str, Any]]:
    resources = template.get("resources", [])
    items = resources.values() if isinstance(resources, dict) else resources
    return [item for item in items if isinstance(item, dict)]


def _walk(template: dict[str, Any], where: str = "main") -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    for resource in _resources(template):
        found.append((where, resource))
        if resource.get("type", "").casefold() == "microsoft.resources/deployments":
            nested = resource.get("properties", {}).get("template")
            if isinstance(nested, dict):
                found.extend(_walk(nested, f"{where}/{resource.get('name', '?')}"))
    return found


def policy_violations(template: dict[str, Any]) -> list[str]:
    """Guarded resources, at any nesting depth, whose public access is not the literal Disabled."""
    violations: list[str] = []
    for where, resource in _walk(template):
        policy = GUARDED_TYPES.get(resource.get("type", "").casefold())
        if policy is None or resource.get("existing") is True:
            continue
        value = resource.get("properties", {}).get("publicNetworkAccess")
        if value != "Disabled":
            violations.append(
                f"{where}: {resource['type']} {resource.get('name')} sets publicNetworkAccess "
                f"{value!r}; {POLICY_ASSIGNMENT} / {policy} forces 'Disabled'"
            )
    return violations


@pytest.fixture(scope="module")
def template() -> dict[str, Any]:
    assert COMPILED_TEMPLATE.is_file(), (
        f"compiled ARM template missing: {COMPILED_TEMPLATE}; "
        "follow the offline contract build in infra\\README.md before running these tests"
    )
    return json.loads(COMPILED_TEMPLATE.read_text(encoding="utf-8"))


def test_policy_forced_public_network_access_is_always_disabled(
    template: dict[str, Any],
) -> None:
    assert policy_violations(template) == []
    guarded = [
        resource["type"].casefold()
        for _, resource in _walk(template)
        if resource.get("type", "").casefold() in GUARDED_TYPES and not resource.get("existing")
    ]
    assert guarded.count("microsoft.sql/servers") == 1
    # The storage accounts were retired; the guard still covers any new one.
    assert guarded.count("microsoft.storage/storageaccounts") == 0


def test_no_sql_firewall_rule_is_deployed(template: dict[str, Any]) -> None:
    # Public network access is Disabled, so ARM refuses any firewall rule
    # (DenyPublicEndpointEnabled).
    rules = [
        resource
        for _, resource in _walk(template)
        if resource.get("type", "").casefold() == "microsoft.sql/servers/firewallrules"
    ]
    assert rules == []


def _deployment(name: str, *resources: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "Microsoft.Resources/deployments",
        "name": name,
        "properties": {"template": {"resources": list(resources)}},
    }


@pytest.mark.parametrize(
    ("resource", "policy"),
    [
        (
            {
                "type": "Microsoft.Sql/servers",
                "name": "sql",
                "properties": {"publicNetworkAccess": "Enabled"},
            },
            "AzureSQL_PublicNetwork_Modify",
        ),
        (
            {"type": "Microsoft.Sql/servers", "name": "sql", "properties": {}},
            "AzureSQL_PublicNetwork_Modify",
        ),
        (
            {
                "type": "Microsoft.Storage/storageAccounts",
                "name": "st",
                "properties": {
                    "publicNetworkAccess": "[if(empty(parameters('ip')), 'Disabled', 'Enabled')]"
                },
            },
            "StorageAccount_PublicNetwork_Modify",
        ),
    ],
    ids=["sql-enabled", "sql-omitted", "storage-expression"],
)
def test_the_guard_finds_violations_in_nested_deployments(
    resource: dict[str, Any], policy: str
) -> None:
    template = {"resources": [_deployment("outer", _deployment("inner", resource))]}
    violations = policy_violations(template)
    assert len(violations) == 1
    assert violations[0].startswith("main/outer/inner: ")
    assert POLICY_ASSIGNMENT in violations[0]
    assert policy in violations[0]


def test_the_guard_ignores_existing_references() -> None:
    reference = {"type": "Microsoft.Storage/storageAccounts", "name": "st", "existing": True}
    assert policy_violations({"resources": {"st": reference}}) == []
