"""What each resource-group template needs from an environment, looked up live and read-only.

Every value that differs between environments comes from here: names from infra/environments,
identities, the apps' domain and app registrations from Azure and Entra, the agents' own
identities from Foundry. Dev's values reproduce the templates' defaults, so a Dev what-if is
unchanged; a new environment gets its own. tools/env/deploy_group.py deploys with them.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cached_property
from typing import Any
from urllib.parse import quote

from tools.env.environment import ROOT, SYSTEMS, Environment
from tools.env.rest import Rest, RestError

IDENTITY_API = "2023-01-31"
APPS_API = "2024-03-01"
ORCHESTRATOR = "recovery-orchestrator-agent"
ASSISTANT = "case-assistant-agent"
DETECTION = "disruption-detection-agent"
PASSENGER = "passenger-reaccommodation-agent"
# The domain agents in publishing order (they answer the orchestrator and the Case Assistant).
DOMAIN_AGENTS = (
    "disruption-detection-agent",
    "aircraft-recovery-agent",
    "crew-recovery-agent",
    "passenger-reaccommodation-agent",
    "slot-negotiation-agent",
)
AGENTS = (*DOMAIN_AGENTS, ORCHESTRATOR, ASSISTANT)
# Each agent's committed definition (Dev's), in infra/ops-app.
AGENT_FILES: Mapping[str, str] = {
    "disruption-detection-agent": "detection.json",
    "aircraft-recovery-agent": "aircraft.json",
    "crew-recovery-agent": "crew.json",
    "passenger-reaccommodation-agent": "passenger.json",
    "slot-negotiation-agent": "slots.json",
    ORCHESTRATOR: "orchestrator-agent.json",
    ASSISTANT: "case-assistant.json",
}
DEFINITIONS = ROOT / "infra" / "ops-app"
# The MCP connection to ARROW's tools as the committed definitions name it; each environment's
# own name is names()["toolsConnection"].
ARROW_CONNECTION = "arrow-tools"
# The agents each airline system admits, besides ARROW (which also writes).
SYSTEM_AGENTS: Mapping[str, tuple[str, ...]] = {
    "schedule": ("aircraft-recovery-agent", "disruption-detection-agent"),
    "maintenance": (),
    "crew": ("crew-recovery-agent",),
    "booking": ("passenger-reaccommodation-agent",),
    "slots": ("slot-negotiation-agent",),
}
# What ARROW's operator app and tools app read (three container apps spec §3): the network map
# and flight details; and what plans, what-ifs, exposures and readings need. The workers read
# Schedule, Crew, Booking and Slots and alone write.
ARROW_READS: Mapping[str, tuple[str, ...]] = {
    "operator": ("schedule", "booking"),
    "tools": ("schedule", "crew", "booking", "slots"),
}


def arrow_tool_agents() -> tuple[str, ...]:
    """The agents whose committed definitions call ARROW's tools: the tools app admits exactly
    these (a definition that adds ARROW's connection is admitted at the next deployment)."""
    calling = []
    for name in AGENTS:
        body = json.loads((DEFINITIONS / AGENT_FILES[name]).read_text(encoding="utf-8"))
        if any(
            tool.get("project_connection_id") == ARROW_CONNECTION
            for tool in body["definition"].get("tools") or []
        ):
            calling.append(name)
    return tuple(calling)


@dataclass(frozen=True)
class Template:
    path: str
    needs_image: bool = False


TEMPLATES: Mapping[str, Template] = {
    "foundry": Template("infra/foundry/isolated.bicep"),
    "foundry-monitoring": Template("infra/foundry/monitoring.bicep"),
    "model": Template("infra/ops-app/model-deployment.bicep"),
    "prerequisites": Template("infra/ops-app/prerequisites.bicep"),
    "database": Template("infra/ops-app/database.bicep"),
    "environment": Template("infra/systems/environment.bicep"),
    "telemetry": Template("infra/ops-app/telemetry.bicep"),
    "migration": Template("infra/systems/migration-job.bicep", needs_image=True),
    "jobs": Template("infra/systems/day-loader-job.bicep", needs_image=True),
    "testbench": Template("infra/testbench/main.bicep", needs_image=True),
    "servicebus": Template("infra/ops-app/servicebus.bicep"),
    "connections": Template("infra/ops-app/connections.bicep"),
    "orchestrator-connections": Template("infra/ops-app/orchestrator-connections.bicep"),
    "access-orchestrator": Template("infra/ops-app/orchestrator-access.bicep"),
    "access-assistant": Template("infra/ops-app/orchestrator-access.bicep"),
    **{f"system-{name}": Template("infra/systems/system.bicep", True) for name in SYSTEMS},
    # ARROW's three apps (three container apps spec): the operator app, the agents' tools and
    # the workers, one image.
    "ops": Template("infra/ops-app/main.bicep", needs_image=True),
    "tools": Template("infra/ops-app/tools.bicep", needs_image=True),
    "worker": Template("infra/ops-app/worker.bicep", needs_image=True),
}


@dataclass(frozen=True)
class Identity:
    principal_id: str
    client_id: str


@dataclass(frozen=True)
class ArrowIdentities:
    """ARROW's apps' managed identities: the workers' (id-airrec-ops-<env>, the only writer),
    the operator app's and the tools app's."""

    workers: Identity
    operator: Identity
    tools: Identity


class Live:
    """Read-only lookups of one environment's values, each made once. ``cutover``: deploying the
    operator app in the deployment guide's "From one ARROW app to three", the one deployment
    that may keep the workers' identity on it until the guide's next command removes it."""

    def __init__(self, rest: Rest, environment: Environment, *, cutover: bool = False) -> None:
        self.rest, self.env = rest, environment
        self.names = environment.names()
        self.cutover = cutover

    def _group(self, provider_path: str) -> str:
        return f"{self.env.group_id}/providers/{provider_path}"

    @cached_property
    def deployer(self) -> dict[str, str]:
        me = self.rest.graph("GET", "/me?$select=id,userPrincipalName").body
        return {"id": me["id"], "upn": me["userPrincipalName"]}

    def identity(self, component: str) -> Identity:
        name = self.names[f"identity.{component}"]
        body = self.rest.arm(
            "GET",
            self._group(f"Microsoft.ManagedIdentity/userAssignedIdentities/{name}"),
            api_version=IDENTITY_API,
        ).body
        return Identity(body["properties"]["principalId"], body["properties"]["clientId"])

    def optional_identity(self, component: str) -> Identity | None:
        try:
            return self.identity(component)
        except RestError as error:
            if error.status == 404:
                return None
            raise

    @cached_property
    def arrow(self) -> ArrowIdentities:
        return ArrowIdentities(
            self.identity("ops"), self.identity("operator"), self.identity("tools")
        )

    def held_identity_names(self, app: str, own: str) -> list[str]:
        """The user-assigned identities the live app holds besides ``own``, by name; none when
        the app does not exist yet. The operator app keeps the one it held while it moves to its
        own (from one ARROW app to three), so the what-if never shows a delete."""
        path = self._group(f"Microsoft.App/containerApps/{self.names[app]}")
        try:
            body = self.rest.arm("GET", path, api_version=APPS_API).body
        except RestError as error:
            if error.status == 404:
                return []
            raise
        held = (body.get("identity") or {}).get("userAssignedIdentities") or {}
        names = sorted(str(key).rsplit("/", 1)[-1] for key in held)
        return [name for name in names if name.lower() != own.lower()]

    @cached_property
    def apps_domain(self) -> str:
        body = self.rest.arm(
            "GET",
            self._group(f"Microsoft.App/managedEnvironments/{self.names['containerEnvironment']}"),
            api_version=APPS_API,
        ).body
        return str(body["properties"]["defaultDomain"])

    def app_id(self, kind: str) -> str:
        """The app registration's client ID: pinned in the environment file (Dev), else looked
        up by its unique name (tools/env/entra_apps.py creates it)."""
        pinned = self.env.arrow_app_id if kind == "arrow" else self.env.systems_app_id
        if pinned:
            return pinned
        name = self.names["arrowAppRegistration" if kind == "arrow" else "systemsAppRegistration"]
        query = quote(f"uniqueName eq '{name}'", safe="'")
        found = self.rest.graph_items(f"/applications?$filter={query}&$select=appId")
        if len(found) != 1:
            raise LookupError(f"app registration {name}: found {len(found)}; run entra_apps.py")
        return str(found[0]["appId"])

    def agent(self, name: str) -> dict[str, Any]:
        """The agent as Foundry holds it: its latest version and its own identity."""
        url = f"{self.names['projectEndpoint']}/agents/{quote(name)}?api-version=v1"
        body = self.rest.foundry("GET", url).body
        identity = body.get("instance_identity") or {}
        latest = (body.get("versions") or {}).get("latest") or {}
        return {
            "name": name,
            "version": latest.get("version"),
            "principal_id": identity.get("principal_id"),
            "client_id": identity.get("client_id"),
        }

    @cached_property
    def agents(self) -> dict[str, dict[str, Any]]:
        return {name: self.agent(name) for name in AGENTS}


def system_callers(
    agent_ids: Mapping[str, Mapping[str, Any]], arrow: ArrowIdentities, system: str
) -> dict[str, list[str]]:
    """Who the system admits: its agents' identities (an agent identity's client id equals its
    object id), then ARROW's workers, the only writer, then ARROW's operator and tools apps where
    they read this system (appended, so a redeploy of an existing system only adds entries)."""
    agents = [agent_ids[name] for name in SYSTEM_AGENTS[system]]
    readers = [arrow.workers] + [
        getattr(arrow, app) for app, systems in ARROW_READS.items() if system in systems
    ]
    return {
        "allowedCallerOids": [str(a["principal_id"]) for a in agents]
        + [identity.principal_id for identity in readers],
        "allowedCallerClientIds": [str(a["client_id"]) for a in agents]
        + [identity.client_id for identity in readers],
        "writerOids": [arrow.workers.principal_id],
        # Booking gives each journey of a rebooking plan to the writers and to the tools app,
        # which prices plans (build_plan, what_if, the exposure) and never writes.
        "journeyReaderOids": [arrow.tools.principal_id] if system == "booking" else [],
    }


def _common(env: Environment) -> dict[str, Any]:
    return {**env.common_parameters(), "envLabel": env.label}


def _foundry(live: Live) -> dict[str, Any]:
    env = live.env
    mini = next(model for model in env.models if model.model == "gpt-4.1-mini")
    values: dict[str, Any] = {
        "operatorPrincipalId": live.deployer["id"],
        "miniCapacity": mini.capacity,
    }
    if env.name != "dev":  # Dev keeps the strings it was created with.
        values.update(
            purpose="recovery-agents",
            projectDisplayName=f"Airline disruption recovery ({env.label})",
            projectDescription=(
                "Prompt agents of the disruption-recovery reference implementation on a generated"
                " operating day. Public endpoint, Entra-only access."
            ),
        )
    return values


def _ops(live: Live) -> dict[str, Any]:
    """The operator app (three container apps spec): sign-in for the operators and the Test
    Bench; no agent calls it. It never keeps the workers' identity, the writer, except during the
    cutover (``Live.cutover``)."""
    env = live.env
    testbench = live.optional_identity("testbench")
    app, writer = live.names["app.ops"], live.names["identity.ops"]
    held = live.held_identity_names("app.ops", live.names["identity.operator"])
    if not live.cutover and any(name.lower() == writer.lower() for name in held):
        raise ValueError(
            f"{app} still holds {writer}, the identity that writes to the airline systems. Remove"
            f" it first: az containerapp identity remove --subscription <subscription> -g"
            f" <resource group> -n {app} --user-assigned <{writer} resource id>. Only the"
            " deployment guide's 'From one ARROW app to three' deploys ops with --cutover."
        )
    return {
        "apiAppId": live.app_id("arrow"),
        "systemsAppId": live.app_id("systems"),
        "operatorOid": env.operators[0],
        "additionalOperatorOids": list(env.operators[1:]),
        "foundryPortalUrl": live.names["foundryPortalUrl"],
        "testbenchOid": testbench.principal_id if testbench else "",
        "testbenchClientId": testbench.client_id if testbench else "",
        "heldIdentityNames": held,
    }


def _tools(live: Live) -> dict[str, Any]:
    """The agents' tools app: Easy Auth admits the agents whose definitions call ARROW's tools;
    code then decides which tools each may call by its role."""
    agents = live.agents
    return {
        "apiAppId": live.app_id("arrow"),
        "systemsAppId": live.app_id("systems"),
        "orchestratorAgentOid": agents[ORCHESTRATOR]["principal_id"],
        "assistantAgentOid": agents[ASSISTANT]["principal_id"],
        "detectionAgentOid": agents[DETECTION]["principal_id"],
        "passengerAgentOid": agents[PASSENGER]["principal_id"],
        "agentOids": [agents[name]["principal_id"] for name in arrow_tool_agents()],
    }


def _system(live: Live, system: str) -> dict[str, Any]:
    return {
        "system": system,
        "apiAppId": live.app_id("systems"),
        **system_callers(live.agents, live.arrow, system),
    }


BUILDERS: Mapping[str, Callable[[Live], dict[str, Any]]] = {
    "foundry": _foundry,
    "model": lambda live: {
        "capacity": next(m for m in live.env.models if m.name == "gpt-4.1-eval").capacity
    },
    "testbench": lambda live: {
        "ownerOids": ",".join(live.env.testbench_owners),
        "opsApiAppId": live.app_id("arrow"),
    },
    "connections": lambda live: {
        "appsDomain": live.apps_domain,
        "systemsAppId": live.app_id("systems"),
    },
    "orchestrator-connections": lambda live: {
        "appsDomain": live.apps_domain,
        "arrowAppId": live.app_id("arrow"),
        "toolsConnectionName": live.names["toolsConnection"],
    },
    "access-orchestrator": lambda live: {
        "orchestratorPrincipalId": live.agent(ORCHESTRATOR)["principal_id"]
    },
    "access-assistant": lambda live: {
        "orchestratorPrincipalId": live.agent(ASSISTANT)["principal_id"]
    },
    "ops": _ops,
    "tools": _tools,
    "worker": lambda live: {"systemsAppId": live.app_id("systems")},
    **{
        f"system-{name}": (lambda live, name=name: _system(live, name))  # type: ignore[misc]
        for name in SYSTEMS
    },
}


def parameters(
    live: Live, key: str, declared: Mapping[str, Any], *, image: str | None
) -> dict[str, Any]:
    """The template's parameters for this environment: the common ones it declares, its own
    looked-up values and the image digest."""
    template = TEMPLATES[key]
    values = {name: value for name, value in _common(live.env).items() if name in declared}
    builder = BUILDERS.get(key)
    if builder is not None:
        values.update(builder(live))
    if template.needs_image:
        if not image or not image.startswith("sha256:") or len(image) != 71:
            raise ValueError(f"{key} needs --image sha256:<64 hex digits>")
        values["imageDigest"] = image
    unknown = sorted(set(values) - set(declared))
    if unknown:
        raise ValueError(f"{key}: {TEMPLATES[key].path} declares no {', '.join(unknown)}")
    return values
