"""The environment wiring (tools/env/wiring.py) gives each template what a person used to type:
for Dev it reproduces the hand-maintained caller lists, so a new environment gets the same
access model with its own identities. With ARROW in three container apps (spec 2026-11-25) only
the workers' identity writes, the operator and tools apps read only what they use, and the tools
app admits exactly the agents that call ARROW's tools."""

from __future__ import annotations

import json
from functools import cached_property
from pathlib import Path
from typing import Any

import pytest

from tools.env import wiring
from tools.env.environment import SYSTEMS, load
from tools.systems import app_roles

ROOT = Path(__file__).resolve().parents[2]
# Dev's identities, as Foundry and Azure hold them.
DEV_AGENTS = {
    "aircraft-recovery-agent": "00000000-0000-4000-8000-000000000011",
    "disruption-detection-agent": "00000000-0000-4000-8000-000000000015",
    "crew-recovery-agent": "00000000-0000-4000-8000-000000000004",
    "passenger-reaccommodation-agent": "00000000-0000-4000-8000-000000000009",
    "slot-negotiation-agent": "00000000-0000-4000-8000-000000000012",
    "recovery-orchestrator-agent": "00000000-0000-4000-8000-000000000016",
    "case-assistant-agent": "00000000-0000-4000-8000-000000000001",
}
DEV_OPS = wiring.Identity(
    principal_id="00000000-0000-4000-8000-000000000006",
    client_id="00000000-0000-4000-8000-000000000003",
)
# The operator and tools apps' identities do not exist in Dev before the cutover.
ARROW = wiring.ArrowIdentities(
    workers=DEV_OPS,
    operator=wiring.Identity(
        "11111111-0000-4000-8000-000000000001", "11111111-0000-4000-8000-0000000000c1"
    ),
    tools=wiring.Identity(
        "22222222-0000-4000-8000-000000000001", "22222222-0000-4000-8000-0000000000c1"
    ),
)
# Owner and spec: the operator app reads the network map and flight details (Schedule, Booking);
# the tools app reads what plans, what-ifs, exposures and readings need.
READERS = {
    "schedule": [ARROW.operator, ARROW.tools],
    "maintenance": [],
    "crew": [ARROW.tools],
    "booking": [ARROW.operator, ARROW.tools],
    "slots": [ARROW.tools],
}


def _agents() -> dict[str, dict[str, Any]]:
    return {name: {"principal_id": oid, "client_id": oid} for name, oid in DEV_AGENTS.items()}


@pytest.mark.parametrize("system", SYSTEMS)
def test_each_system_admits_its_readers_and_only_the_workers_write(system: str) -> None:
    committed = json.loads(
        (ROOT / "infra" / "ops-app" / f"{system}.parameters.json").read_text(encoding="utf-8")
    )["parameters"]
    callers = wiring.system_callers(_agents(), ARROW, system)
    # Dev's lists from one app stay first, unchanged: redeploying a system only adds entries.
    for key in ("allowedCallerOids", "allowedCallerClientIds"):
        assert callers[key][: len(committed[key]["value"])] == committed[key]["value"], key
    added = callers["allowedCallerOids"][len(committed["allowedCallerOids"]["value"]) :]
    assert added == [identity.principal_id for identity in READERS[system]], added
    assert callers["writerOids"] == committed["writerOids"]["value"] == [DEV_OPS.principal_id]
    # Booking gives journey facts to the tools app, which prices plans without writing.
    readers = [ARROW.tools.principal_id] if system == "booking" else []
    assert callers["journeyReaderOids"] == readers


def test_only_the_workers_identity_holds_the_write_role() -> None:
    names = load("dev").names()
    declared = app_roles.callers(_agents(), ARROW, names)
    roles = {oid: set(held) for oid, _, held in declared.values()}
    assert roles[DEV_OPS.principal_id] == {
        "Schedule.Read",
        "Crew.Read",
        "Booking.Read",
        "Slots.Read",
        "Actions.Write",
    }
    assert roles[ARROW.operator.principal_id] == {"Schedule.Read", "Booking.Read"}
    assert roles[ARROW.tools.principal_id] == {
        "Schedule.Read",
        "Crew.Read",
        "Booking.Read",
        "Slots.Read",
    }
    assert [oid for oid, held in roles.items() if "Actions.Write" in held] == [DEV_OPS.principal_id]
    # Each ARROW identity is checked by its own name before any role is assigned.
    fragments = {oid: fragment for oid, fragment, _ in declared.values()}
    assert fragments[ARROW.operator.principal_id] == names["identity.operator"]
    assert fragments[ARROW.tools.principal_id] == names["identity.tools"]


class _Live(wiring.Live):
    """Dev's names with the agents and app registrations as Foundry and Entra hold them."""

    def __init__(self) -> None:
        super().__init__(None, load("dev"))  # type: ignore[arg-type]

    def app_id(self, kind: str) -> str:
        return {"arrow": "arrow-app", "systems": "systems-app"}[kind]

    @cached_property
    def agents(self) -> dict[str, dict[str, Any]]:
        return _agents()


def test_the_tools_app_admits_exactly_the_agents_that_call_arrows_tools() -> None:
    calling = []
    for name, file in wiring.AGENT_FILES.items():
        body = json.loads((ROOT / "infra" / "ops-app" / file).read_text(encoding="utf-8"))
        if any(
            tool.get("project_connection_id") == "arrow-tools"
            for tool in body["definition"].get("tools") or []
        ):
            calling.append(DEV_AGENTS[name])
    admitted = wiring.BUILDERS["tools"](_Live())["agentOids"]
    assert calling and sorted(admitted) == sorted(calling), (admitted, calling)


class _Held(_Live):
    """The live operator app holding ``held`` besides its own identity; no Test Bench yet."""

    def __init__(self, held: list[str], *, cutover: bool = False) -> None:
        super().__init__()
        self.cutover, self._held = cutover, held

    def held_identity_names(self, app: str, own: str) -> list[str]:
        return list(self._held)

    def optional_identity(self, component: str) -> wiring.Identity | None:
        return None


def test_the_operator_app_keeps_the_writer_identity_only_during_the_cutover() -> None:
    # The internet-facing operator app may hold the workers' identity, the writer, only while
    # the guide's "From one ARROW app to three" moves it to its own; any other deployment stops
    # and names the command that removes it.
    writer = load("dev").names()["identity.ops"]
    with pytest.raises(ValueError, match="az containerapp identity remove"):
        wiring.BUILDERS["ops"](_Held([writer]))
    assert wiring.BUILDERS["ops"](_Held([writer], cutover=True))["heldIdentityNames"] == [writer]
    assert wiring.BUILDERS["ops"](_Held([]))["heldIdentityNames"] == []
