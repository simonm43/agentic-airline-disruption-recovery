"""Offline contract for ARROW's three apps (three container apps spec): who runs where, who is
admitted and who may write. The compiled templates come from the offline build in
infra\\README.md ("Offline infrastructure contract checks")."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tools.env import wiring

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / ".local" / "infra-contract"
APP = "Microsoft.App/containerApps"
AUTH = "Microsoft.App/containerApps/authConfigs"
ROLE_ASSIGNMENT = "Microsoft.Authorization/roleAssignments"
ACR_PULL = "7f951dda-4ed3-4680-a7ca-43fe172d538d"
AZURE_AI_USER = "53ca6127-db72-4b80-b1b0-d745d6d5456d"
SERVICE_BUS_DATA_OWNER = "090c5cfd-751d-490a-894a-3ce6f1109419"
# Each role in code that an agent calling ARROW's tools holds, by the tools app's parameter.
ROLE_PARAMETERS = {
    wiring.DETECTION: "detectionAgentOid",
    wiring.PASSENGER: "passengerAgentOid",
    wiring.ORCHESTRATOR: "orchestratorAgentOid",
    wiring.ASSISTANT: "assistantAgentOid",
}


def _compiled(name: str) -> dict[str, Any]:
    path = CONTRACT / f"ops-app-{name}.json"
    assert path.is_file(), (
        f"compiled ARM template missing: {path}; "
        "follow the offline contract build in infra\\README.md before running these tests"
    )
    return dict(json.loads(path.read_text(encoding="utf-8")))


def _one(template: dict[str, Any], resource_type: str) -> dict[str, Any]:
    found = [r for r in template["resources"] if r["type"] == resource_type]
    assert len(found) == 1, f"{resource_type}: {len(found)}"
    return dict(found[0])


def _settings(app: dict[str, Any]) -> dict[str, str]:
    (container,) = app["properties"]["template"]["containers"]
    return {item["name"]: item["value"] for item in container["env"]}


def _identity(name: str) -> str:
    return f"format('id-airrec-{name}-{{0}}', parameters('env'))"


@pytest.fixture(scope="module")
def apps() -> dict[str, dict[str, Any]]:
    return {name: _compiled(name) for name in ("main", "tools", "worker")}


def test_only_the_workers_run_as_the_writer_with_no_ingress_on_one_replica(
    apps: dict[str, dict[str, Any]],
) -> None:
    worker = _one(apps["worker"], APP)
    assert list(worker["identity"]["userAssignedIdentities"]) == [
        f"[format('{{0}}', resourceId('Microsoft.ManagedIdentity/userAssignedIdentities',"
        f" {_identity('ops')}))]"
    ]
    configuration = worker["properties"]["configuration"]
    assert "ingress" not in configuration
    assert _identity("ops") in configuration["registries"][0]["identity"]
    scale = worker["properties"]["template"]["scale"]
    assert (scale["minReplicas"], scale["maxReplicas"]) == (1, 1)
    assert "rules" not in scale
    (container,) = worker["properties"]["template"]["containers"]
    assert container["probes"] == [
        {"type": "Liveness", "httpGet": {"path": "/healthz", "port": 8080}, "periodSeconds": 30}
    ]
    assert _settings(worker)["APP_ROLE"] == "workers"
    assert not any(r["type"] == AUTH for r in apps["worker"]["resources"])
    # Neither app with ingress runs as the writer.
    for name, own in (("main", "operator"), ("tools", "tools")):
        app = _one(apps[name], APP)
        held = json.dumps(app["identity"]["userAssignedIdentities"])
        assert _identity(own) in held and _identity("ops") not in held, name
        assert _settings(app)["APP_ROLE"] == {"main": "operator", "tools": "tools"}[name]
        assert _identity(own) in _settings(app)["AZURE_CLIENT_ID"], name


def test_the_tools_app_admits_only_the_agents_that_call_arrows_tools(
    apps: dict[str, dict[str, Any]],
) -> None:
    template = apps["tools"]
    auth = _one(template, AUTH)["properties"]
    assert auth["globalValidation"] == {
        "unauthenticatedClientAction": "Return401",
        "excludedPaths": ["/healthz"],
    }
    provider = auth["identityProviders"]["azureActiveDirectory"]
    assert "clientSecretSettingName" not in provider["registration"]
    policy = provider["validation"]["defaultAuthorizationPolicy"]
    assert policy == {
        "allowedApplications": "[parameters('agentOids')]",
        "allowedPrincipals": {"identities": "[parameters('agentOids')]"},
    }
    # Dev's default is what the wiring derives, and every admitted agent has a role in code.
    parameters = template["parameters"]
    calling = wiring.arrow_tool_agents()
    assert set(calling) <= set(ROLE_PARAMETERS), "map the new agent's role in code first"
    assert parameters["agentOids"]["defaultValue"] == [
        parameters[ROLE_PARAMETERS[name]]["defaultValue"] for name in calling
    ]
    settings = _settings(_one(template, APP))
    for parameter in ROLE_PARAMETERS.values():
        assert f"[parameters('{parameter}')]" in settings.values()
    assert "PUBLIC_URL" not in settings


def test_the_operator_app_admits_operators_and_the_test_bench_but_no_agent(
    apps: dict[str, dict[str, Any]],
) -> None:
    template = apps["main"]
    assert not [name for name in template["parameters"] if name.endswith("AgentOid")]
    policy = _one(template, AUTH)["properties"]["identityProviders"]["azureActiveDirectory"][
        "validation"
    ]["defaultAuthorizationPolicy"]
    assert set(re.findall(r"parameters\('(\w+)'\)", json.dumps(policy))) == {
        "apiAppId",
        "testbenchClientId",
        "operatorOid",
        "testbenchOid",
        "additionalOperatorOids",
    }
    settings = _settings(_one(template, APP))
    assert not [name for name in settings if name.endswith("_AGENT_OIDS")]
    assert "ASSESSMENT_WORKERS" not in settings
    # ARROW's own calls in the trail: the workers' identity and both apps' identities.
    assert _identity("ops") in settings["OPS_PRINCIPAL_ID"]
    assert _identity("operator") in settings["OPERATOR_PRINCIPAL_ID"]
    assert _identity("tools") in settings["TOOLS_PRINCIPAL_ID"]
    assert settings["EXECUTION_QUEUE"] == "executions"


def test_only_the_workers_receive_and_no_one_owns_the_namespace() -> None:
    template = _compiled("servicebus")
    queues = [r["name"] for r in template["resources"] if r["type"].endswith("namespaces/queues")]
    assert len(queues) == 2 and any("'executions'" in name for name in queues)
    grants = [
        (r["scope"], r["properties"]["roleDefinitionId"], r["properties"]["principalId"])
        for r in template["resources"]
        if r["type"] == ROLE_ASSIGNMENT
    ]
    receivers = {
        (scope.split("'")[-2], principal.split("parameters('")[1].split("'")[0])
        for scope, role, principal in grants
        if "receiverRole" in role
    }
    assert receivers == {
        ("assessments", "opsIdentityName"),
        ("assessment", "opsIdentityName"),
        ("executions", "opsIdentityName"),
    }
    senders = {
        (scope.split("'")[-2], principal.split("parameters('")[1].split("'")[0])
        for scope, role, principal in grants
        if "senderRole" in role
    }
    for identity in ("operatorIdentityName", "toolsIdentityName"):
        assert {("assessments", identity), ("executions", identity)} <= senders
    assert SERVICE_BUS_DATA_OWNER not in json.dumps(template)


def test_the_new_identities_pull_images_and_only_the_operator_app_runs_agents() -> None:
    template = _compiled("prerequisites")
    created = {
        r["name"]
        for r in template["resources"]
        if r["type"] == "Microsoft.ManagedIdentity/userAssignedIdentities"
    }
    assert {f"[{_identity('operator')}]", f"[{_identity('tools')}]"} <= created
    grants = {
        (r["properties"]["roleDefinitionId"].split("'")[-2], r["properties"]["principalId"])
        for r in template["resources"]
        if r["type"] == ROLE_ASSIGNMENT
    }

    def holds(role: str, name: str) -> bool:
        return any(r == role and _identity(name) in principal for r, principal in grants)

    assert holds(ACR_PULL, "operator") and holds(ACR_PULL, "tools")
    assert holds(AZURE_AI_USER, "operator") and holds(AZURE_AI_USER, "ops")
    assert not holds(AZURE_AI_USER, "tools")
