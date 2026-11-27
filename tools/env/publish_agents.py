"""Publish the environment's seven Foundry prompt agents in order, then read them back.

    python -m tools.env.publish_agents <environment>                  # read-only: what would change
    python -m tools.env.publish_agents <environment> --apply          # publish, then read back
    python -m tools.env.publish_agents <environment> --agent <name>   # only that agent (repeatable)

The definitions in infra/ops-app/*.json are Dev's. Each run renders them for the environment (its
apps' MCP addresses, its Foundry project's A2A addresses, the name of its connection to ARROW's
tools) and compares them with the agent's latest version: a missing agent is created (POST /agents),
a changed one gets a new immutable version (POST /agents/{name}/versions), an unchanged one is left
alone. The five domain agents answer the orchestrator and the Case Assistant over A2A, so each also
gets its agent card (infra/ops-app/agent-cards.json) and the A2A protocol (PATCH /agents/{name}).
Order: the domain agents, then the orchestrator, then the Case Assistant, so no caller asks an
agent that does not yet understand its question. An agent's endpoint serves its latest version at
once: publish with no case in flight.

Agents are published over Foundry's Agents REST API (v1): a prompt agent's definition and
versions are data-plane objects of the project, which no ARM resource type (and so no Bicep)
declares.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from tools.env import wiring
from tools.env.environment import Environment, load
from tools.env.rest import Rest, RestError

ROOT = Path(__file__).resolve().parents[2]
DEFINITIONS = ROOT / "infra" / "ops-app"
CARDS = DEFINITIONS / "agent-cards.json"
FILES: Mapping[str, str] = wiring.AGENT_FILES
# The app behind each MCP connection the agents use, by the name Dev's definitions give it.
# ARROW's tools are served by the tools app (three container apps spec); the connection is named
# per environment (names()["toolsConnection"]).
TOOLS_CONNECTION = wiring.ARROW_CONNECTION
MCP_APPS: Mapping[str, str] = {
    TOOLS_CONNECTION: "tools",
    "schedule-mcp": "schedule",
    "maintenance-mcp": "maintenance",
    "crew-mcp": "crew",
    "booking-mcp": "booking",
    "slots-mcp": "slots",
}
A2A_PATH = re.compile(r"/agents/(?P<agent>[a-z0-9-]+)/endpoint/protocols/a2a$")
A2A: dict[str, dict[str, Any]] = {"responses": {}, "a2a": {}}
# A new agent's own Entra identity appears shortly after it is created.
IDENTITY_SECONDS = 120


def definition(name: str) -> dict[str, Any]:
    return dict(json.loads((DEFINITIONS / FILES[name]).read_text(encoding="utf-8")))


def cards() -> dict[str, dict[str, Any]]:
    return dict(json.loads(CARDS.read_text(encoding="utf-8")))


def render(body: Mapping[str, Any], names: Mapping[str, str], apps_domain: str) -> dict[str, Any]:
    """The committed body with this environment's MCP and A2A addresses."""
    rendered = copy.deepcopy(dict(body))
    for tool in rendered["definition"].get("tools", []):
        if tool.get("type") == "mcp":
            connection = tool["project_connection_id"]
            if connection not in MCP_APPS:
                raise ValueError(f"no app is known for the MCP connection {connection}")
            tool["server_url"] = f"https://{names['app.' + MCP_APPS[connection]]}.{apps_domain}/mcp"
            if connection == TOOLS_CONNECTION:
                tool["project_connection_id"] = names["toolsConnection"]
        elif tool.get("type") == "a2a":
            match = A2A_PATH.search(urlsplit(tool["base_url"]).path)
            if match is None:
                raise ValueError(f"unexpected A2A address {tool['base_url']}")
            agent = match.group("agent")
            tool["base_url"] = f"{names['projectEndpoint']}/agents/{agent}/endpoint/protocols/a2a"
    return rendered


def covers(wanted: Any, have: Any) -> bool:
    """Whether the live value holds everything wanted (Foundry may add defaults of its own)."""
    if isinstance(wanted, dict):
        return isinstance(have, dict) and all(covers(v, have.get(k)) for k, v in wanted.items())
    if isinstance(wanted, list):
        return (
            isinstance(have, list)
            and len(wanted) == len(have)
            and all(covers(w, h) for w, h in zip(wanted, have, strict=True))
        )
    return bool(wanted == have)


def decide(
    name: str, body: Mapping[str, Any], live: Mapping[str, Any] | None, card: Any
) -> list[str]:
    """What --apply would do for one agent."""
    steps: list[str] = []
    if live is None:
        steps.append("create")
    else:
        latest = (live.get("versions") or {}).get("latest") or {}
        current = {"description": latest.get("description"), "definition": latest.get("definition")}
        if not covers(dict(body), current):
            steps.append("new version")
    if card is not None:
        endpoint = (live or {}).get("agent_endpoint") or {}
        configured = endpoint.get("protocol_configuration") or {}
        if (live or {}).get("agent_card") != card or "a2a" not in configured:
            steps.append("agent card and A2A")
    return steps


class Publisher:
    def __init__(self, rest: Rest, env: Environment) -> None:
        self.rest, self.env = rest, env
        self.live = wiring.Live(rest, env)
        self.names = env.names()
        self.project = self.names["projectEndpoint"]

    def url(self, path: str) -> str:
        return f"{self.project}{path}?api-version=v1"

    def get(self, name: str) -> dict[str, Any] | None:
        try:
            return dict(self.rest.foundry("GET", self.url(f"/agents/{quote(name)}")).body)
        except RestError as error:
            if error.status == 404:
                return None
            raise

    def plan(self, agents: Sequence[str]) -> list[dict[str, Any]]:
        domain = self.live.apps_domain
        all_cards = cards()
        steps = []
        for name in agents:
            body = render(definition(name), self.names, domain)
            live = self.get(name)
            latest = ((live or {}).get("versions") or {}).get("latest") or {}
            steps.append(
                {
                    "agent": name,
                    "version": latest.get("version"),
                    "steps": decide(name, body, live, all_cards.get(name)),
                }
            )
        return steps

    def publish(self, name: str) -> None:
        body = render(definition(name), self.names, self.live.apps_domain)
        card = cards().get(name)
        live = self.get(name)
        steps = decide(name, body, live, card)
        if "create" in steps:
            self.rest.foundry("POST", self.url("/agents"), {"name": name, **body})
        elif "new version" in steps:
            self.rest.foundry("POST", self.url(f"/agents/{quote(name)}/versions"), body)
        if card is not None and "agent card and A2A" in steps:
            self.rest.foundry(
                "PATCH",
                self.url(f"/agents/{quote(name)}"),
                {"agent_card": card, "agent_endpoint": {"protocol_configuration": A2A}},
            )

    def read_back(self, name: str) -> dict[str, Any]:
        """The agent's latest version and own identity, waiting for a new agent's identity."""
        deadline = time.monotonic() + IDENTITY_SECONDS
        while True:
            live = self.get(name) or {}
            identity = live.get("instance_identity") or {}
            if identity.get("principal_id") or time.monotonic() > deadline:
                break
            time.sleep(10)
        latest = (live.get("versions") or {}).get("latest") or {}
        endpoint = live.get("agent_endpoint") or {}
        return {
            "agent": name,
            "version": latest.get("version"),
            "status": latest.get("status"),
            "state": live.get("state"),
            "model": (latest.get("definition") or {}).get("model"),
            "principal_id": identity.get("principal_id"),
            "protocols": sorted((endpoint.get("protocol_configuration") or {}).keys()),
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.env.publish_agents", description=__doc__)
    parser.add_argument("environment")
    parser.add_argument("--agent", action="append", choices=wiring.AGENTS)
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args(argv)
    env = load(arguments.environment)
    publisher = Publisher(Rest(env.tenant_id), env)
    agents = [name for name in wiring.AGENTS if name in (arguments.agent or wiring.AGENTS)]
    plan = publisher.plan(agents)
    print(json.dumps(plan, indent=2))
    if not arguments.apply:
        print("Read-only: nothing changed. Re-run with --apply.")
        return 0
    for step in plan:
        if step["steps"]:
            publisher.publish(step["agent"])
    result = [publisher.read_back(name) for name in agents]
    print(json.dumps(result, indent=2))
    remaining = [step for step in publisher.plan(agents) if step["steps"]]
    missing = [item["agent"] for item in result if not item["principal_id"]]
    if remaining or missing:
        print(f"Still differs: {remaining}; no identity yet: {missing}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
