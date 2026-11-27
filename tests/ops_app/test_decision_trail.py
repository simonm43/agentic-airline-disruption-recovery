"""The decision trail (business UI and free-text spec §4, D95): opening an assessment shows each
agent, what the orchestrator asked it, what it answered, the systems it read, its prompt, the rules
each plan met and the outcome, in plain words.

Real intake, tools, policy, executor and API; the orchestrator is a script that also returns the
steps Foundry would have recorded.
"""

from __future__ import annotations

import base64
import json
import unittest
from collections.abc import Sequence
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote

from arrow_scenario import CaseMemory, ScenarioStore, SystemView
from day_store import OperatingDayStore
from fastapi.testclient import TestClient
from test_case_chat import DUTY_MANAGER, answered, read_case
from test_four_cases import CASE_1, delay, one_plan
from test_message_intake import MESSAGE, READING
from test_orchestrated_flow import (
    DEFECT,
    HOST,
    OPERATOR,
    TRACE,
    Script,
    ScriptedGateway,
    operate,
    submitted,
)

from airline_systems.app import create_app as system_app
from ops_app.app import create_app
from ops_app.chat import Chat
from ops_app.engine import (
    CASE_ASSISTANT_AGENT,
    ORCHESTRATOR_AGENT,
    CloudDependencyError,
    CloudGateway,
    Operations,
)
from ops_app.planner import CaseRef, case_ref
from ops_app.tools import ArrowTools

ARROW = "33333333-4444-5555-6666-777777777777"
# The tools app's identity (three container apps spec): it reads the systems for the plans it
# builds, and the trail shows its calls as ARROW's own too.
ARROW_TOOLS = "88888888-1111-2222-3333-444444444444"
DETECTION = "aaaaaaaa-1111-2222-3333-444444444444"
CREW = "bbbbbbbb-1111-2222-3333-444444444444"
PASSENGER = "cccccccc-1111-2222-3333-444444444444"
ORCHESTRATOR = "dddddddd-1111-2222-3333-444444444444"
ASSISTANT = "eeeeeeee-1111-2222-3333-444444444444"
SLOT = "ffffffff-1111-2222-3333-444444444444"


def principal(oid: str) -> dict[str, str]:
    claims = {"claims": [{"typ": "oid", "val": oid}]}
    return {"X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(claims).encode()).decode()}


def system_caller(oid: str, roles: tuple[str, ...], trace: str) -> dict[str, str]:
    """A caller as Easy Auth passes it to an airline system, in the case's trace."""
    claims = {"claims": [{"typ": "oid", "val": oid}, *({"typ": "roles", "val": r} for r in roles)]}
    return {
        "X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(claims).encode()).decode(),
        "traceparent": f"00-{trace}-00f067aa0ba902b7-01",
        "Accept": "application/json, text/event-stream",
    }


class FoundryProject(CloudGateway):
    """Foundry's agent API as it answers GET {project}/agents/{name} for the Dev agents: the
    latest version's definition and the agent's own identity; ``models`` names a deployment per
    agent (gpt-4.1 when not named)."""

    def __init__(self, identities: dict[str, str], models: dict[str, str] | None = None) -> None:
        super().__init__(
            None,  # type: ignore[arg-type]
            "systems-app",
            "",
            "",
            "https://foundry.test/api/projects/arrow",
            booking_url="",
        )
        self.identities = identities
        self.models = models or {}

    def request(
        self,
        url: str,
        scope: str,
        trace: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float = 240,
    ) -> dict[str, Any]:
        name = unquote(url.split("/agents/")[1].split("?")[0])
        if name not in self.identities:
            raise CloudDependencyError("Foundry could not be reached; retry shortly.")
        identity = {"principal_id": self.identities[name], "client_id": self.identities[name]}
        return {
            "object": "agent",
            "name": name,
            "versions": {
                "latest": {
                    "name": name,
                    "version": "7",
                    "definition": {
                        "kind": "prompt",
                        "model": self.models.get(name, "gpt-4.1"),
                        "instructions": f"You are {name}. Use only your tools.",
                    },
                    "instance_identity": identity,
                }
            },
            "instance_identity": identity,
        }


def foundry_steps(trace: str, plan_id: str) -> list[dict[str, Any]]:
    """Output items as the Foundry Responses API returns them for one orchestrator run."""
    return [
        {"type": "mcp_list_tools", "server_label": "arrow"},
        {
            "type": "mcp_call",
            "id": "mcp_1",
            "name": "record_disruption",
            "server_label": "arrow",
            "arguments": json.dumps({**READING, "trace_id": trace}),
            "output": json.dumps({"recorded": True}),
            "status": "completed",
        },
        {
            "type": "a2a_preview_call",
            "id": "a2a_1",
            "name": "a2a-detection",
            "arguments": json.dumps({"message": f"Confirm the disruption for trace {trace}"}),
            "status": "completed",
        },
        {
            "type": "a2a_preview_call_output",
            "call_id": "a2a_1",
            "name": "a2a-detection",
            "output": "AR131 departs 20 minutes late; one flight is affected.",
            "status": "completed",
        },
        {
            "type": "a2a_preview_call",
            "id": "a2a_2",
            "name": "a2a-crew",
            "arguments": json.dumps({"message": "Check crew limits for option delay"}),
            "status": "completed",
        },
        {
            "type": "a2a_preview_call_output",
            "call_id": "a2a_2",
            "name": "a2a-crew",
            "output": json.dumps(
                {
                    "recommendation": {"reserve_option_id": None, "reason": "No breach"},
                    "facts": {"constraint_status": "pass", "breaches": []},
                }
            ),
            "status": "completed",
        },
        {
            "type": "a2a_preview_call",
            "id": "a2a_3",
            "name": "a2a-slots",
            "arguments": json.dumps({"message": "Check LIS capacity for option delay"}),
            "status": "completed",
        },
        {
            "type": "a2a_preview_call_output",
            "call_id": "a2a_3",
            "name": "a2a-slots",
            "output": json.dumps(["AR131 fits LIS capacity"]),
            "status": "completed",
        },
        {
            "type": "mcp_call",
            "id": "mcp_2",
            "name": "build_plan",
            "server_label": "arrow",
            "arguments": json.dumps({"trace_id": trace, "levers": {"aircraft_option_id": "delay"}}),
            "output": json.dumps({"plan_id": plan_id, "route": "auto"}),
            "status": "completed",
        },
        {
            "type": "mcp_call",
            "id": "mcp_3",
            "name": "submit_plan",
            "server_label": "arrow",
            "arguments": json.dumps(
                {"trace_id": trace, "plan_id": plan_id, "reason": "Within every limit"}
            ),
            "output": json.dumps({"route": "auto"}),
            "status": "completed",
        },
        {
            "type": "message",
            "content": [{"type": "output_text", "text": '{"outcome": "submitted"}'}],
        },
    ]


def check_trail_shows_agents_lookups_prompts_rules_and_outcome() -> None:
    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        tools.record_disruption(trace, READING)
        plan = tools.build_plan(trace, {"aircraft_option_id": "delay"})
        tools.submit_plan(trace, plan["plan_id"], "Within every limit")
        return {
            **submitted(plan["plan_id"]),
            "trail": {
                "items": foundry_steps(trace, plan["plan_id"]),
                "usage": {"input_tokens": 5000, "output_tokens": 400, "total_tokens": 5400},
                "model": "gpt-4.1",
            },
        }

    operations, cases, gateway = operate(ScenarioStore(TRACE), script)

    def definition(name: str) -> dict[str, Any]:
        if name == "crew-recovery-agent":
            raise RuntimeError("Foundry unavailable")
        found: dict[str, Any] = {
            "version": "5",
            "instructions": f"You are {name}. Use only your tools.",
        }
        if name == CASE_ASSISTANT_AGENT:
            found["identity"] = {"principal_id": ASSISTANT, "client_id": ASSISTANT}
        return found

    gateway.agent_definition = definition  # type: ignore[attr-defined]
    trace = operations.open(MESSAGE, OPERATOR)["trace_id"]
    cases.calls[trace] = [
        {
            "at": "2026-11-10T09:46:01Z",
            "system": "schedule",
            "operation": "record_delay",
            "caller_oid": ARROW,
            "outcome": "ok",
            "detail": None,
        },
        # A storm's record as Schedule audits it (Case 5): a write, named in plain words.
        {
            "at": "2026-11-10T09:46:02Z",
            "system": "schedule",
            "operation": "record_weather",
            "caller_oid": ARROW,
            "outcome": "ok",
            "detail": None,
            "request": json.dumps(
                {
                    "airport": "LIS",
                    "window_start": "16:00",
                    "window_end": "18:00",
                    "hazard": "thunderstorm",
                }
            ),
        },
        {
            "at": "2026-11-10T09:46:05Z",
            "system": "schedule",
            "operation": "get_disruption",
            "caller_oid": DETECTION,
            "outcome": "ok",
            "detail": None,
        },
        {
            "at": "2026-11-10T09:46:09Z",
            "system": "crew",
            "operation": "assess_crew",
            "caller_oid": CREW,
            "outcome": "ok",
            "detail": '{"option_id":"delay","added_minutes":0}',
        },
        # The Case Assistant holds no system role; were its identity ever recorded on a
        # system, the trail names it and credits it to no domain agent.
        {
            "at": "2026-11-10T09:50:00Z",
            "system": "schedule",
            "operation": "get_disruption",
            "caller_oid": ASSISTANT,
            "outcome": "ok",
            "detail": None,
        },
    ]
    operations.run(trace)

    client = TestClient(
        create_app(
            cases,
            operations,
            frozenset({OPERATOR}),
            frozenset({HOST}),
            "https://arrow.test",
            arrow_oids=frozenset({ARROW}),
        )
    )
    with client:
        response = client.get(f"/api/cases/{trace}/trail", headers=principal(OPERATOR))
        case = client.get(f"/api/cases/{trace}", headers=principal(OPERATOR)).json()
        anonymous = client.get(f"/api/cases/{trace}/trail")

    assert response.status_code == 200, response.text
    assert anonymous.status_code == 401
    assert not any("trail" in key for key in case), case.keys()
    trail = response.json()

    assert trail["message"]["text"] == MESSAGE["text"]
    assert "AR131" in trail["message"]["reading"] and "20 min" in trail["message"]["reading"]

    agents = {agent["key"]: agent for agent in trail["agents"]}
    assert list(agents) == ["detection", "aircraft", "crew", "passenger", "slot"]
    detection, crew = agents["detection"], agents["crew"]
    assert detection["asked"] == f"Confirm the disruption for trace {trace}"
    assert detection["answered"].startswith("AR131 departs 20 minutes late")
    assert detection["autonomy"]["level"] == "L4"
    assert [call["label"] for call in detection["calls"]] == ["Disruption"]
    assert detection["calls"][0]["system"] == "Operations control" and detection["calls"][0]["ok"]
    # Rows recorded by earlier versions hold no endpoint, request or answer: the endpoint follows
    # from who made the call (agents reach the systems only through MCP, ARROW only over REST), the
    # rest shows as not recorded, and an identity ARROW cannot name stays unknown.
    looked = detection["calls"][0]
    assert looked["endpoint"] == "MCP get_disruption", looked
    assert looked["request"] is None and looked["response"] is None, looked
    assert looked["caller"] == {"kind": "unknown", "name": "Unknown caller", "id": DETECTION}
    assert crew["calls"][0]["endpoint"] == "MCP assess_crew_recovery", crew["calls"]
    # Owner: "why do I have a write action in the rules execution?" ARROW recording the
    # report is intake, listed apart from its checks, which only read.
    reported = trail["reported_calls"]
    assert reported[0]["endpoint"] == "POST /api/delays", reported
    assert reported[0]["caller"] == {"kind": "arrow", "name": "ARROW", "id": ARROW}
    assert [call["access"] for call in trail["arrow_checks"]] == ["read"], trail["arrow_checks"]
    assert trail["arrow_checks"][-1]["caller"] == {
        "kind": "agent",
        "name": "Case Assistant",
        "id": ASSISTANT,
    }, trail["arrow_checks"]
    assert crew["identity"] == {"client_id": None, "name": "crew-recovery-agent"}, crew
    assert crew["model"] is None, crew
    assert detection["prompt"] == {
        "available": True,
        "version": "5",
        "instructions": "You are disruption-detection-agent. Use only your tools.",
    }
    assert crew["calls"][0]["label"] == "Crew impact", crew["calls"]
    assert "Fly AR131" in crew["calls"][0]["detail"] or "delay" in crew["calls"][0]["detail"]
    assert crew["prompt"] == {"available": False}
    assert agents["passenger"]["status"] == "not asked" and agents["passenger"]["calls"] == []

    steps = trail["orchestrator"]["steps"]
    assert steps[0]["title"] == "Read the message", steps[0]
    asks = [step.get("agent") for step in steps if step["kind"] == "ask"]
    assert asks == ["detection", "crew", "slot"], steps
    answers = {step["agent"]: step for step in steps if step["kind"] == "answer"}
    assert answers["crew"]["facts"]["facts"]["constraint_status"] == "pass", answers["crew"]
    # Plain text or a JSON list is not an object: no facts, the text is still shown.
    assert answers["detection"]["facts"] is None, answers["detection"]
    assert (
        answers["slot"]["facts"] is None and answers["slot"]["detail"] == "AR131 fits LIS capacity"
    )
    assert trail["orchestrator"]["brief"]["message"] == MESSAGE["text"]
    assert trail["orchestrator"]["tokens"] == 5400
    # Owner: recording the report is a write of a fact, not a recovery action, and
    # each call says what it was for in plain words.
    assert reported[0]["label"] == "Flight delay"
    storm = reported[1]
    assert (storm["label"], storm["access"], storm["detail"]) == (
        "Weather forecast",
        "write",
        "Thunderstorms at Lisbon · 16:00–18:00",
    ), storm

    [plan] = trail["plans"]
    assert plan["route"] == "auto" and plan["recommended"] is True
    assert plan["reason"] == "Within every limit"
    # Its own levers as ARROW stored them: the orchestrator's panel shows the recommended plan's
    # (review), never those of the last plan built.
    assert plan["levers"]["aircraft_option_id"] == "delay", plan
    # The same four cost parts and counts the plan cards show (cancellation and rebooking cost
    # research §5).
    assert set(plan["cost_parts"]) == {"airline", "compensation", "care", "rebooking"}, plan
    assert sum(plan["cost_parts"].values()) == plan["cost_eur"], plan
    assert isinstance(plan["passengers_owed_compensation"], int), plan
    rules = {rule["rule"]: rule for rule in plan["rules"]}
    assert rules["Aircraft change or standby"]["result"] == "pass", rules
    assert all(rule["result"] in {"pass", "expert"} for rule in plan["rules"]), plan["rules"]
    checks = {check["key"]: check for check in plan["checks"]}
    assert checks["cost"]["result"] == "pass", plan["checks"]
    assert plan["rules_version"] == 1, plan

    assert trail["outcome"]["state"] == "executed", trail["outcome"]
    assert trail["outcome"]["actions"], trail["outcome"]
    stats = trail["stats"]
    assert stats["agents_asked"] == 3 and stats["lookups"] == 2 and stats["plans"] == 1
    assert (stats["reported_calls"], stats["arrow_checks"]) == (2, 1), stats
    assert stats["rule_checks"] == len(plan["rules"]) and stats["actions"] >= 1


def crew_item(kind: str, number: int, option: str, trace: str) -> dict[str, Any]:
    """A Crew question or answer as live Foundry records it: the answer's call_id is the
    question's call_id, not its id."""
    if kind == "call":
        return {
            "type": "a2a_preview_call",
            "id": f"fc_{number}",
            "call_id": f"call_{number}",
            "name": "a2a-crew",
            "arguments": json.dumps(
                {"message": f"Assess crew for trace_id {trace}, option_id {option}."}
            ),
            "status": "completed",
        }
    return {
        "type": "a2a_preview_call_output",
        "id": f"fco_{number}",
        "call_id": f"call_{number}",
        "name": "a2a-crew",
        "output": json.dumps({"facts": {"constraint_status": "pass", "option": option}}),
        "status": "completed",
    }


def check_parallel_answers_stay_with_their_own_question() -> None:
    """Crew is asked about delay and cancel at once and the cancel answer returns first: each
    answer still belongs to its own question, so no crew result is shown against another plan."""

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        tools.record_disruption(trace, READING)
        plan = tools.build_plan(trace, {"aircraft_option_id": "delay"})
        tools.submit_plan(trace, plan["plan_id"], "Within every limit")
        items = [
            crew_item("call", 1, "delay", trace),
            crew_item("call", 2, "cancel", trace),
            crew_item("output", 2, "cancel", trace),
            crew_item("output", 1, "delay", trace),
        ]
        return {**submitted(plan["plan_id"]), "trail": {"items": items}}

    operations, cases, _ = operate(ScenarioStore(TRACE), script)
    trace = operations.open(MESSAGE, OPERATOR)["trace_id"]
    operations.run(trace)
    client = TestClient(
        create_app(
            cases, operations, frozenset({OPERATOR}), frozenset({HOST}), "https://arrow.test"
        )
    )
    with client:
        response = client.get(f"/api/cases/{trace}/trail", headers=principal(OPERATOR))
    assert response.status_code == 200, response.text
    steps = response.json()["orchestrator"]["steps"]
    asks = {step["call_id"]: step for step in steps if step["kind"] == "ask"}
    answers = [step for step in steps if step["kind"] == "answer"]
    assert sorted(asks) == ["call_1", "call_2"], steps
    assert [answer["call_id"] for answer in answers] == ["call_2", "call_1"], answers
    for answer in answers:
        option = answer["facts"]["facts"]["option"]
        assert f"option_id {option}" in asks[answer["call_id"]]["detail"], (answer, asks)
        assert asks[answer["call_id"]]["answer"] == answer["detail"], (answer, asks)


def check_a_failed_tool_call_shows_as_failed() -> None:
    """An AR444 run: Foundry returned record_case's refusal as ordinary output ("Error
    executing tool ..."), and the trail showed it as done. The refusal came from the orchestrator
    naming an agent "Detection"; a finding now accepts the agent's name in any case."""

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        tools.record_disruption(trace, READING)
        tools.record_case(trace, "AR131 is late", [{"agent": "Detection", "text": "1 flight"}])
        plan = tools.build_plan(trace, {"aircraft_option_id": "delay"})
        tools.submit_plan(trace, plan["plan_id"], "Within every limit")
        refused = {
            "type": "mcp_call",
            "id": "mcp_9",
            "name": "record_case",
            "server_label": "arrow",
            "arguments": json.dumps({"trace_id": trace, "summary": "AR131 is late"}),
            "output": "Error executing tool record_case: Invalid findings",
            "status": "completed",
        }
        items = foundry_steps(trace, plan["plan_id"])
        return {**submitted(plan["plan_id"]), "trail": {"items": [*items[:2], refused, *items[2:]]}}

    operations, cases, _ = operate(ScenarioStore(TRACE), script)
    trace = operations.open(MESSAGE, OPERATOR)["trace_id"]
    operations.run(trace)
    assert cases.get(trace)["findings"] == [{"agent": "detection", "text": "1 flight"}]
    client = TestClient(
        create_app(
            cases, operations, frozenset({OPERATOR}), frozenset({HOST}), "https://arrow.test"
        )
    )
    with client:
        response = client.get(f"/api/cases/{trace}/trail", headers=principal(OPERATOR))
    assert response.status_code == 200, response.text
    tools_ok = {
        (step["tool"], step["ok"])
        for step in response.json()["orchestrator"]["steps"]
        if step["kind"] == "tool"
    }
    assert ("record_case", False) in tools_ok and ("record_disruption", True) in tools_ok, tools_ok


def trail_of(cases: Any, operations: Any, trace: str) -> dict[str, Any]:
    client = TestClient(
        create_app(
            cases,
            operations,
            frozenset({OPERATOR}),
            frozenset({HOST}),
            "https://arrow.test",
            arrow_oids=frozenset({ARROW, ARROW_TOOLS}),
        )
    )
    with client:
        response = client.get(f"/api/cases/{trace}/trail", headers=principal(OPERATOR))
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def check_each_system_call_shows_its_endpoint_caller_request_and_answer() -> None:
    """Owner: clicking an agent shows its system calls; each call opens to who called
    which endpoint with what, and what the system answered. The agents read their systems
    through MCP during the run and ARROW checks the same option over REST; each call names
    the disruption by its facts, never ARROW's case id or trace, and Booking's answer is counts
    only."""
    store = ScenarioStore(TRACE)
    stack = ExitStack()
    systems = {
        name: stack.enter_context(
            TestClient(
                system_app(
                    SystemView(store, name),  # type: ignore[arg-type]
                    frozenset({CREW, PASSENGER, ARROW, ARROW_TOOLS}),
                    system=name,
                    writer_oids=frozenset({ARROW}),
                )
            )
        )
        for name in ("crew", "booking")
    }
    option = {"option_id": "delay", "additional_delay_minutes": 0}

    def ask(system: str, oid: str, role: str, tool: str, trace: str) -> None:
        # Agents name the disruption by its facts (owner), never the case.
        facts = case_ref(cases.get(trace)).disruption
        response = systems[system].post(
            "/mcp",
            headers=system_caller(oid, (role,), trace),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": {"disruption": facts, **option}},
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert not result.get("isError"), response.json()
        assert trace not in json.dumps(result), result

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        tools.record_disruption(trace, READING)
        ask("crew", CREW, "Crew.Read", "assess_crew_recovery", trace)
        ask("booking", PASSENGER, "Booking.Read", "get_rebooking_plan", trace)
        # ARROW checks the same option over REST, from the tools app that builds the plan.
        checked = systems["crew"].get(
            "/api/recovery-options/delay/impact",
            params={**case_ref(cases.get(trace)).disruption, "additional_delay_minutes": 0},
            headers=system_caller(ARROW_TOOLS, ("Crew.Read",), trace),
        )
        assert checked.status_code == 200, checked.text
        plan = tools.build_plan(trace, {"aircraft_option_id": "delay"})
        tools.submit_plan(trace, plan["plan_id"], "Within every limit")
        steps = foundry_steps(trace, plan["plan_id"])
        return {**submitted(plan["plan_id"]), "trail": {"items": steps}}

    operations, cases, gateway = operate(store, script)
    foundry = FoundryProject(
        {
            "crew-recovery-agent": CREW,
            "passenger-reaccommodation-agent": PASSENGER,
            ORCHESTRATOR_AGENT: ORCHESTRATOR,
        }
    )
    gateway.agent_definition = foundry.agent_definition  # type: ignore[attr-defined]
    gateway.system_urls = {  # type: ignore[attr-defined]
        "crew": "https://crew-sys.test",
        "booking": "https://booking-sys.test",
    }
    with stack:
        trace = operations.open(MESSAGE, OPERATOR)["trace_id"]
        operations.run(trace)
    cases.calls[trace] = store.system_calls(trace)
    trail = trail_of(cases, operations, trace)
    case = trail["display_id"]
    facts = case_ref(cases.get(trace)).disruption
    impact = "GET /api/recovery-options/{option_id}/impact"

    agents = {agent["key"]: agent for agent in trail["agents"]}
    crew, passenger = agents["crew"], agents["passenger"]
    assert crew["identity"] == {"client_id": CREW, "name": "crew-recovery-agent"}, crew
    assert crew["model"] == "GPT-4.1", crew
    assert trail["orchestrator"]["identity"] == {
        "client_id": ORCHESTRATOR,
        "name": ORCHESTRATOR_AGENT,
    }, trail["orchestrator"]
    assert trail["orchestrator"]["model"] == "GPT-4.1", trail["orchestrator"]

    [assessed] = crew["calls"]
    assert assessed["endpoint"] == "MCP assess_crew_recovery", assessed
    assert assessed["caller"] == {"kind": "agent", "name": "Crew", "id": CREW}, assessed
    assert assessed["request"] == {**facts, **option}, assessed
    assert assessed["response"]["constraint_status"] == "pass", assessed
    assert assessed["response"]["breaches"] == 0, assessed
    [planned] = passenger["calls"]
    assert planned["endpoint"] == "MCP get_rebooking_plan", planned
    assert planned["caller"] == {"kind": "agent", "name": "Passenger", "id": PASSENGER}
    assert planned["request"] == {**facts, **option}, planned
    assert all(isinstance(count, int) for count in planned["response"].values()), planned
    assert planned["response"]["disrupted_passengers"] >= 0, planned

    [checked] = [call for call in trail["arrow_checks"] if call["operation"] == "assess_crew"]
    assert checked["endpoint"] == impact, checked
    assert checked["caller"] == {"kind": "arrow", "name": "ARROW", "id": ARROW_TOOLS}, checked
    assert checked["request"] == {**facts, **option}, checked
    assert checked["response"] == assessed["response"], checked
    # Owner: read or write, and never a host. ARROW's own read is the system's route filled in
    # with the disruption's facts; the agent's tool call is the tool only, with no route it never
    # called.
    assert "host" not in assessed and "host" not in checked, (assessed, checked)
    assert assessed["tool"] == "assess_crew_recovery", assessed
    route = (
        "GET /api/recovery-options/delay/impact"
        "?type=flight_delay&flight_no=AR131&delay_minutes=20&cause=staff_shortage"
    )
    assert checked["api"] == route and assessed["api"] is None, (checked, assessed)
    assert assessed["transport"] == "mcp" and checked["transport"] == "rest"
    assert assessed["access"] == checked["access"] == "read"

    shown = json.dumps([crew["calls"], passenger["calls"], trail["arrow_checks"]])
    assert trace not in shown and case not in shown and "SYN0" not in shown, shown


def check_each_action_shows_its_own_request_and_receipt() -> None:
    """The outcome shows what ARROW sent each system and what each answered, matched by action
    id. Crew control re-planned the duty after ARROW validated the plan, so Crew refused its
    update and says why, while the retime and the other actions were confirmed."""

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        tools.record_disruption(trace, READING)
        plan = tools.build_plan(trace, {"aircraft_option_id": "delay"})
        tools.submit_plan(trace, plan["plan_id"], "Within every limit")
        return submitted(plan["plan_id"])

    store = ScenarioStore(TRACE)
    operations, cases, gateway = operate(store, script)

    def replanned(sent: int) -> None:
        if sent == 1:  # once the retime is confirmed, crew control moves the duty's release
            duty = store.duties["D-LIS-001"]
            duty["release_local"] = duty["release_local"].replace("16:50", "17:20")

    gateway.after_call = replanned
    trace = operations.open(MESSAGE, OPERATOR)["trace_id"]
    operations.run(trace)
    trail = trail_of(cases, operations, trace)

    executed = {a["action_id"]: a for a in cases.get(trace)["execution"]["actions"]}
    shown = {action["action_id"]: action for action in trail["outcome"]["actions"]}
    assert list(shown) == list(executed), (list(shown), list(executed))
    kinds = {action["request"]["kind"]: action for action in shown.values()}
    assert {"retime_flights", "update_duties"} <= set(kinds), kinds
    for action_id, action in shown.items():
        assert action["request"]["kind"] == executed[action_id]["kind"], action
        assert action["receipt"]["status"] == executed[action_id]["status"], action
        assert action["receipt"]["at"] == executed[action_id]["receipt"]["at"], action

    retime = kinds["retime_flights"]
    assert [f["flight_no"] for f in retime["request"]["flights"]] == ["AR131", "AR132"], retime
    assert retime["request"]["flights"][0]["std_local"].endswith("T14:30:00"), retime
    assert retime["receipt"]["status"] == "confirmed" and retime["receipt"]["reason"] is None
    crew = kinds["update_duties"]
    assert crew["receipt"]["status"] == "refused", crew
    assert crew["receipt"]["reason"] == "Source changed: duty plan differs from the validated plan"
    assert "originals" not in json.dumps(crew["request"]), crew


class AuditedSystems(ScriptedGateway):
    """The scripted orchestrator and in-process reads, but each call ARROW sends to carry out a
    plan reaches its system's own app, which audits it as in Dev: the caller's identity, the
    call's X-Request-Id, what was sent and what it answered."""

    def __init__(
        self,
        store: ScenarioStore,
        script: Script,
        stack: ExitStack,
        callers: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(store, script)
        self.apps = {
            name: stack.enter_context(
                TestClient(
                    system_app(
                        SystemView(store, name),  # type: ignore[arg-type]
                        frozenset({ARROW, *callers}),
                        system=name,
                        writer_oids=frozenset({ARROW}),
                        mock_confirm_seconds=0,
                    )
                )
            )
            for name in ("schedule", "maintenance", "crew", "booking", "slots")
        }

    def send(
        self,
        system: str,
        method: str,
        path: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
        trace: str,
    ) -> dict[str, Any]:
        roles = ("Actions.Write", f"{system.capitalize()}.Read")
        response = self.apps[system].request(
            method, path, headers={**headers, **system_caller(ARROW, roles, trace)}, json=body
        )
        if method != "GET":
            self.calls.append(headers["X-Request-Id"])
            if self.after_call is not None:
                self.after_call(len(self.calls))
        if response.status_code >= 400:
            raise CloudDependencyError(
                f"Cloud dependency returned HTTP {response.status_code}; no result accepted",
                status_code=response.status_code,
                detail=response.json().get("detail"),
            )
        result: dict[str, Any] = response.json()
        return result


def through_the_systems(
    store: ScenarioStore, script: Script, stack: ExitStack
) -> tuple[Operations, CaseMemory, AuditedSystems]:
    cases = CaseMemory()
    gateway = AuditedSystems(store, script, stack)
    operations = Operations(cases, gateway)
    gateway.tools = ArrowTools(
        cases,
        gateway,
        execute=operations.execute,
        rules=operations.rules,
        record=operations.record_reading,
    )
    return operations, cases, gateway


def check_each_call_is_one_row_named_by_its_recipient() -> None:
    """Owner: the trail showed each write twice, ARROW's action and the system's audit
    row. Case 1 now lists one row per call, named by its recipient and what it says, with what
    was sent, the reply and the identity the recipient recorded; no audit row repeats it."""
    store = OperatingDayStore()
    with ExitStack() as stack:
        operations, cases, _ = through_the_systems(
            store, one_plan({"aircraft_option_id": "delay"}), stack
        )
        trace = operations.open(delay(CASE_1), OPERATOR)["trace_id"]
        operations.run(trace)
    cases.calls[trace] = store.system_calls(trace)
    trail = trail_of(cases, operations, trace)

    calls = [call for action in trail["outcome"]["actions"] for call in action["calls"]]
    assert [call["recipient"] for call in calls] == [
        "Operations control",
        "Operations control",
        "EUROCONTROL Network Manager",
        "Lisbon airport (A-CDM)",
        "Crew system",
    ], calls
    flight = store.flights["AR259"]
    was, now = (
        datetime.fromisoformat(flight[key]).strftime("%H:%M")
        for key in ("published_std_local", "std_local")
    )
    record, retime, message = calls[0], calls[1], calls[2]
    # The plan first records the reported delay in operations control, which issues its event id.
    assert (record["says"], record["method"], record["path"], record["status"]) == (
        "Record the AR259 delay: 20 min, border-control queue",
        "POST",
        "/api/delays",
        "confirmed",
    ), record
    assert record["reply"]["event_id"] == f"EV-{trace[6:12].upper()}", record
    assert retime["says"] == f"AR259 departs {now} (was {was})", retime
    assert (retime["method"], retime["path"], retime["played_by"]) == (
        "PATCH",
        "/api/flights/AR259",
        "Schedule",
    ), retime
    assert message["sent"]["text"].startswith("(DLA-AR259-LPPT"), message
    assert (message["status"], message["message"], message["reply"]) == (
        "confirmed",
        "ACK",
        {"status": "ACK"},
    ), message
    for call in calls:
        [recorded] = call["recorded"]
        assert recorded["caller"] == {"kind": "arrow", "name": "ARROW", "id": ARROW}, recorded
        assert recorded["request"]["request_id"] == call["call_id"], recorded
        # One vocabulary (owner): the recipient's own record names the system as the row.
        assert recorded["system"] == call["recipient"], recorded
    # What was sent, in named fields: never the original rows the crew system checks against.
    assert "originals" not in json.dumps(calls), calls
    sent = {call["call_id"] for call in calls}
    repeated = [c for c in trail["arrow_checks"] if (c["request"] or {}).get("request_id") in sent]
    assert not repeated, repeated
    assert trail["stats"]["calls"] == 5, trail["stats"]


def check_a_refused_call_reads_as_refused() -> None:
    """Crew control re-planned the duty after ARROW validated the plan: the crew system refuses
    the change (HTTP 412) and records it as a failed call. The trail shows that call once,
    refused with the crew system's reason, never as a failed system call."""

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        tools.record_disruption(trace, READING)
        plan = tools.build_plan(trace, {"aircraft_option_id": "delay"})
        tools.submit_plan(trace, plan["plan_id"], "Within every limit")
        return submitted(plan["plan_id"])

    store = ScenarioStore(TRACE)

    def replanned(sent: int) -> None:
        if sent == 1:  # once the first flight is retimed, crew control moves the duty's release
            duty = store.duties["D-LIS-001"]
            duty["release_local"] = duty["release_local"].replace("16:50", "17:20")

    with ExitStack() as stack:
        operations, cases, gateway = through_the_systems(store, script, stack)
        gateway.after_call = replanned
        trace = operations.open(MESSAGE, OPERATOR)["trace_id"]
        operations.run(trace)
    cases.calls[trace] = store.system_calls(trace)
    trail = trail_of(cases, operations, trace)

    calls = [call for action in trail["outcome"]["actions"] for call in action["calls"]]
    [crew] = [call for call in calls if call["recipient"] == "Crew system"]
    assert crew["status"] == "refused", crew
    assert crew["message"] == "Source changed: duty plan differs from the validated plan", crew
    assert len(crew["recorded"]) == 1, crew
    assert not [c for c in trail["arrow_checks"] if not c["ok"]], trail["arrow_checks"]


class AuditedReads(AuditedSystems):
    """As in Dev, every read is a call the system answering it audits: ARROW plans through each
    system's REST routes, and an agent reads through the system's MCP tools."""

    def read(self, system: str, path: str, trace: str, **params: Any) -> dict[str, Any]:
        roles = ("Actions.Write", f"{system.capitalize()}.Read")
        response = self.apps[system].get(
            path,
            params={key: value for key, value in params.items() if value is not None},
            headers=system_caller(ARROW, roles, trace),
        )
        assert response.status_code == 200, response.text
        result: dict[str, Any] = response.json()
        return result

    def options(self, case: CaseRef, additional_delay_minutes: int) -> dict[str, Any]:
        return self.read(
            "schedule",
            "/api/recovery-options",
            case.trace,
            **case.disruption,
            additional_delay_minutes=additional_delay_minutes,
        )

    def assessment(
        self, domain: str, case: CaseRef, option_id: str, additional_delay_minutes: int
    ) -> dict[str, Any]:
        return self.read(
            domain,
            f"/api/recovery-options/{option_id}/impact",
            case.trace,
            **case.disruption,
            additional_delay_minutes=additional_delay_minutes,
        )

    def rebooking(
        self, case: CaseRef, option_id: str, additional_delay_minutes: int
    ) -> dict[str, Any]:
        return self.read(
            "booking",
            f"/api/recovery-options/{option_id}/rebooking-plan",
            case.trace,
            **case.disruption,
            additional_delay_minutes=additional_delay_minutes,
        )

    def duty_plan(
        self,
        case: CaseRef,
        option_id: str,
        additional_delay_minutes: int,
        reserve_option_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        return self.read(
            "crew",
            f"/api/recovery-options/{option_id}/duty-plan",
            case.trace,
            **case.disruption,
            additional_delay_minutes=additional_delay_minutes,
            reserve_option_ids=list(reserve_option_ids) or None,
        )

    def reserve_options(
        self, case: CaseRef, option_id: str, additional_delay_minutes: int
    ) -> dict[str, Any]:
        return self.read(
            "crew",
            f"/api/recovery-options/{option_id}/standby-crews",
            case.trace,
            **case.disruption,
            additional_delay_minutes=additional_delay_minutes,
        )

    def airports(self, trace: str) -> dict[str, Any]:
        return self.read("slots", "/api/airports", trace)

    def agent_reads(
        self, system: str, oid: str, tool: str, trace: str, disruption: dict[str, Any]
    ) -> None:
        """A domain agent's MCP tool call in the case's trace, as Foundry makes it: the
        disruption by its facts."""
        arguments = {"disruption": disruption, "option_id": "delay", "additional_delay_minutes": 0}
        response = self.apps[system].post(
            "/mcp",
            headers=system_caller(oid, (f"{system.capitalize()}.Read",), trace),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            },
        )
        assert response.status_code == 200, response.text
        assert not response.json()["result"].get("isError"), response.json()


def a2a(tool: str, number: int, question: str, answer: dict[str, Any]) -> list[dict[str, Any]]:
    """A question to a domain agent and its JSON answer, as Foundry records them."""
    return [
        {
            "type": "a2a_preview_call",
            "id": f"fc_{tool}_{number}",
            "call_id": f"call_{tool}_{number}",
            "name": tool,
            "arguments": json.dumps({"message": question}),
            "status": "completed",
        },
        {
            "type": "a2a_preview_call_output",
            "call_id": f"call_{tool}_{number}",
            "name": tool,
            "output": json.dumps(answer),
            "status": "completed",
        },
    ]


def check_the_chat_stays_out_of_how_arrow_decided() -> None:
    """The Case Assistant runs in the case's own trace: the agents it asks and the what-if ARROW
    builds for it read the airline systems under the case. How ARROW decided shows the
    assessment as it was; the chat's system calls are listed apart, each with its turn."""
    store = ScenarioStore(TRACE)
    question = "What if maintenance needs 30 more minutes?"
    no_breach = {"recommendation": {"reason": "No duty breach."}, "facts": {"breaches": []}}
    fits = {"recommendation": {"reason": "Fits at LIS."}, "facts": {"capacity_fit": True}}

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        facts = dict(case_ref(cases.get(trace)).disruption)
        gateway.agent_reads("crew", CREW, "assess_crew_recovery", trace, facts)
        # Asked while ARROW still assesses, the Case Assistant asks Crew too. The crew system's
        # audit cannot tell the two runs apart, so the assessment keeps the calls Crew made
        # meanwhile; only an agent the orchestrator never asked is the chat's.
        during = chat.ask(trace, "Is the crew within limits?", DUTY_MANAGER)
        chat.answer(trace, during["id"])
        cancel = tools.build_plan(trace, {"aircraft_option_id": "cancel"})
        tools.build_plan(trace, {"aircraft_option_id": "delay"})
        tools.submit_plan(trace, cancel["plan_id"], "Fewest passengers disrupted")
        items = a2a("a2a-crew", 1, "Check crew limits for option delay", no_breach)
        return {**submitted(cancel["plan_id"]), "trail": {"items": items}}

    def assistant(trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        case_id, facts = brief["case_id"], dict(case_ref(cases.get(trace)).disruption)
        gateway.agent_reads("crew", CREW, "assess_crew_recovery", trace, facts)
        items = [read_case(case_id), *a2a("a2a-crew", 2, "Are the duties legal?", no_breach)]
        if brief["question"] == question:
            gateway.agent_reads("slots", SLOT, "assess_slot_recovery", trace, facts)
            # The Passenger agent the Case Assistant asked reads ARROW's EU261 exposure; ARROW
            # logs the call on the case as it happens (this store's own event clock is fixed).
            cases.docs[trace]["events"].append(
                {
                    "at": datetime.now(UTC).isoformat(),
                    "actor": "passenger",
                    "autonomy": "L3",
                    "event": "agent_tool_call",
                    "tool": "get_compensation_exposure",
                    "access": "read",
                    "request": {"option_id": "delay", "additional_delay_minutes": 30},
                    "response": {"option_id": "delay", "compensation_eur": 86850},
                    "ok": True,
                    "message": "EU261 exposure read",
                }
            )
            [turn] = [t for t in cases.get(trace)["chat"] if t["status"] == "pending"]
            levers = {"aircraft_option_id": "delay", "extra_delay_minutes": 30}
            assert gateway.tools is not None
            gateway.tools.what_if(trace, levers, question, turn["asked_by"], turn["id"])
            items += a2a("a2a-slots", 3, "Does LIS fit 30 min later?", fits)
        return answered("Plan 3 waits 30 minutes more; it needs OCC.", *items)

    with ExitStack() as stack:
        cases = CaseMemory()
        gateway = AuditedReads(store, script, stack, frozenset({CREW, SLOT}))
        operations = Operations(cases, gateway)
        gateway.tools = ArrowTools(
            cases, gateway, execute=operations.execute, rules=operations.rules
        )
        gateway.assistant = assistant
        foundry = FoundryProject(
            {
                ORCHESTRATOR_AGENT: ORCHESTRATOR,
                CASE_ASSISTANT_AGENT: ASSISTANT,
                "crew-recovery-agent": CREW,
                "passenger-reaccommodation-agent": PASSENGER,
                "slot-negotiation-agent": SLOT,
            }
        )
        gateway.agent_definition = foundry.agent_definition  # type: ignore[attr-defined]
        chat = Chat(cases, gateway)
        trace = operations.open(DEFECT, OPERATOR)["trace_id"]
        operations.run(trace)
        assert cases.get(trace)["status"] == "assessment_ready", cases.get(trace)["stage"]
        cases.calls[trace] = store.system_calls(trace)
        assessed = len(cases.calls[trace])
        before = trail_of(cases, operations, trace)

        entry = chat.ask(trace, question, DUTY_MANAGER)
        chat.answer(trace, entry["id"])
    assert cases.get(trace)["chat"][-1]["status"] == "answered", cases.get(trace)["chat"][-1]
    cases.calls[trace] = store.system_calls(trace)
    after = trail_of(cases, operations, trace)

    agents = {agent["key"]: agent for agent in after["agents"]}
    assert len(agents["crew"]["calls"]) == 2 and before["chat_calls"] == [], before["chat_calls"]
    assert agents["slot"]["status"] == "not asked" and agents["slot"]["calls"] == [], agents["slot"]
    assert [a["status"] for a in after["agents"]] == [a["status"] for a in before["agents"]]
    assert after["stats"]["lookups"] == before["stats"]["lookups"] == 2, after["stats"]
    assert after["arrow_checks"] == before["arrow_checks"] and before["arrow_checks"]
    assert after["orchestrator"]["steps"] == before["orchestrator"]["steps"]

    # Every call the turn caused, and only those, listed apart under the turn: the systems'
    # audit, and the ARROW tool the Passenger agent called for it.
    chat_calls = after["chat_calls"]
    assert len(chat_calls) == len(cases.calls[trace]) - assessed + 1, chat_calls
    assert {call["entry_id"] for call in chat_calls} == {entry["id"]}, chat_calls
    assert [
        (call["caller"]["name"], call["endpoint"])
        for call in chat_calls
        if call["caller"]["kind"] == "agent"
    ] == [
        ("Crew", "MCP assess_crew_recovery"),
        ("Slot", "MCP assess_slot_recovery"),
        ("Passenger", "MCP get_compensation_exposure"),
    ], chat_calls
    assert agents["passenger"]["calls"] == [] and before["agents"] == after["agents"], agents
    built = {call["operation"] for call in chat_calls if call["caller"]["kind"] == "arrow"}
    assert {"get_recovery_options", "assess_slots", "get_rebooking_plan"} <= built, built

    # The what-if is marked as asked in chat, after ARROW's two plans.
    assert [plan["what_if"] for plan in before["plans"]] == [None, None], before["plans"]
    asked = after["plans"][2]["what_if"]
    assert [plan["what_if"] is None for plan in after["plans"][:2]] == [True, True]
    assert (asked["asked_by"], asked["entry_id"], asked["question"]) == (
        "Duty Manager",
        entry["id"],
        question,
    ), asked


AIRCRAFT = "abababab-1111-2222-3333-444444444444"
EVENT = "EV-9C2D41"
RECORD_ID = "0123456789ab-0a1b2c3d-01-record-disruption"
RECORDED = "Defect recorded as event EV-9C2D41: CS-TQA out of service until 13:45"
REPORT = (
    "Hydraulic leak found on CS-TQA during the walk-around. "
    "Maintenance expects it out of service for about 4 hours."
)


def audited(
    at: str, system: str, operation: str, oid: str, endpoint: str, request: Any, response: Any
) -> dict[str, Any]:
    """A system's audit row for one call, as store.system_calls returns it."""
    return {
        "at": at,
        "system": system,
        "operation": operation,
        "caller_oid": oid,
        "outcome": "ok",
        "detail": None,
        "endpoint": endpoint,
        "request": json.dumps(request),
        "response": json.dumps(response),
    }


def check_a_repeated_report_reads_as_not_recorded_again() -> None:
    """Another open case already holds the disruption: ARROW records nothing again and says so.
    The Detection agent's call reads as what happened, never as a completed write."""
    operations, cases, _ = operate(ScenarioStore(TRACE), lambda tools, trace, brief: {})
    trace = operations.open({"type": "message", "text": REPORT}, OPERATOR)["trace_id"]
    said = "Already recorded for DSR-20261123-AB12CD: not recorded again; OCC sees both cases."
    cases.docs[trace]["events"].append(
        {
            "at": "2026-11-23T09:46:01Z",
            "actor": "detection",
            "autonomy": "L4",
            "event": "agent_tool_call",
            "tool": "record_aircraft_defect",
            "request": {"aircraft_reg": "CS-TQA", "unavailable_minutes": 240},
            "response": {"recorded": False, "repeat": True},
            "ok": True,
            "message": said,
        }
    )
    trail = trail_of(cases, operations, trace)
    [call] = next(a for a in trail["agents"] if a["key"] == "detection")["calls"]
    assert (call["summary"], call["access"]) == (said, None), call
    assert trail["recorded_via"] != "detection", trail["recorded_via"]


def check_a_crosswind_says_gusts_only_when_the_wind_gusts() -> None:
    """Code computes the crosswind from the forecast's gust when it has one, else from its mean
    wind; the case says "gusts" only then, as its title does."""
    operations, cases, _ = operate(ScenarioStore(TRACE), lambda tools, trace, brief: {})
    trace = operations.open({"type": "message", "text": REPORT}, OPERATOR)["trace_id"]
    crosswind = {}
    for wind, knots in (("27030KT", 28), ("27022G35KT", 33)):
        cases.docs[trace]["disruption"] = {
            "type": "weather",
            "airport": "LIS",
            "hazard": "crosswind",
            "window_start": "16:00",
            "window_end": "18:00",
            "wind": wind,
            "crosswind_kt": knots,
            "runway": "02/20",
        }
        fields = dict(trail_of(cases, operations, trace)["disruption"]["fields"])
        crosswind[wind] = fields["Crosswind"]
    assert crosswind == {
        "27030KT": "28 kt across runway 02/20",
        "27022G35KT": "gusts 33 kt across runway 02/20",
    }, crosswind


def check_the_new_agent_flow_reads_in_plain_words() -> None:
    """Owner: "how should I explain that to a normal user? why is it calling read aircraft
    options ... reading airport rules, returns 70"; "dev hardcoded in the url"; "why is it always
    api/defects"; "I again see repetitive calls"; "prompt vxx what is that". The Detection agent
    reads the report and records it on ARROW's case; Maintenance has it only once the plan runs,
    as the plan's first action, and issues its event id then. The Passenger agent asks ARROW for
    the EU261 exposure. Each call reads as the system's name, what it was for and its result in
    words; its method and path are the system's own API with the disruption's facts, never a
    host or MCP; the executor's re-reads before it writes stand apart from the planning reads;
    models read by their names. What ARROW and the systems record under the new flow is written
    into the case."""
    operations, cases, gateway = operate(ScenarioStore(TRACE), lambda tools, trace, brief: {})
    trace = operations.open({"type": "message", "text": REPORT}, OPERATOR)["trace_id"]
    case = cases.docs[trace]
    knock_on = {
        "flights": [
            {
                "flight_no": f,
                "from": a,
                "to": b,
                "std_local": s,
                "new_std_local": n,
                "minutes_late": 240,
            }
            for f, a, b, s, n in (
                ("AR101", "LIS", "OPO", "2026-11-23T10:30:00", "2026-11-23T14:30:00"),
                ("AR102", "OPO", "LIS", "2026-11-23T12:00:00", "2026-11-23T16:00:00"),
                ("AR131", "LIS", "FAO", "2026-11-23T14:10:00", "2026-11-23T18:10:00"),
                ("AR132", "FAO", "LIS", "2026-11-23T15:40:00", "2026-11-23T19:40:00"),
            )
        ],
        "flights_affected": 4,
        "max_minutes_late": 240,
        "aircraft": ["CS-TQA"],
    }
    case.update(
        status="assessment_ready",
        created_at="2026-11-23T09:45:30+00:00",
        disruption={
            "type": "aircraft_defect",
            "cause": "aircraft_defect",
            "scope": {"flights": ["AR101", "AR102"], "aircraft_reg": "CS-TQA"},
            "description": REPORT,
            "message": REPORT,
            "reported_at": "2026-11-23T09:45:00",
            "knock_on": knock_on,
        },
        defect={
            "aircraft_reg": "CS-TQA",
            "unavailable_minutes": 240,
            "reported_at": "2026-11-23T09:45:00",
            "unavailable_until": "2026-11-23T13:45:00",
        },
        airports={
            "LIS": {
                "iata": "LIS",
                "coordination_level": 3,
                "a_cdm": True,
                "night_departure_from": "23:55",
                "night_departure_until": "05:50",
                "night_arrival_from": "00:05",
                "night_arrival_until": "06:00",
            }
        },
        execution={
            "route": "auto",
            "status": "completed",
            "approved_by": "policy",
            "actions": [
                {
                    "action_id": RECORD_ID,
                    "system": "maintenance",
                    "label": "Maintenance",
                    "kind": "record_disruption",
                    "stage": "now",
                    "status": "confirmed",
                    "summary": "Record the CS-TQA defect: out of service 4 h",
                    "payload": {"aircraft_reg": "CS-TQA", "unavailable_minutes": 240},
                    "message": RECORDED,
                    "receipt": {"status": "confirmed", "at": "2026-11-23T09:48:03Z"},
                    "calls": [
                        {
                            "call_id": RECORD_ID,
                            "request_id": RECORD_ID,
                            "recipient": "Maintenance",
                            "played_by": "Maintenance",
                            "says": "Record the CS-TQA defect: out of service 4 h",
                            "method": "POST",
                            "path": "/api/defects",
                            "status": "confirmed",
                            "message": RECORDED,
                            "event_id": EVENT,
                            "at": "2026-11-23T09:48:03Z",
                        }
                    ],
                }
            ],
        },
    )
    case["events"] += [
        {
            "at": "2026-11-23T09:46:01Z",
            "actor": "detection",
            "autonomy": "L4",
            "event": "agent_tool_call",
            "tool": "record_aircraft_defect",
            "request": {
                "aircraft_reg": "CS-TQA",
                "unavailable_minutes": 240,
                "reading": "CS-TQA is out of service for 240 minutes.",
            },
            "response": {"recorded": "aircraft defect · CS-TQA", "flights_affected": 4},
            "ok": True,
            "message": "The Detection agent recorded the aircraft defect.",
        },
        {
            "at": "2026-11-23T09:46:40Z",
            "actor": "passenger",
            "autonomy": "L3",
            "event": "agent_tool_call",
            "tool": "get_compensation_exposure",
            "request": {
                "case_id": case["display_id"],
                "option_id": "delay",
                "additional_delay_minutes": 15,
            },
            "response": {
                "option_id": "delay",
                "additional_delay_minutes": 15,
                "passengers_owed_compensation": 331,
                "compensation_eur": 85050,
                "care_eur": 12026,
                "rebooking_eur": 6698,
                "extraordinary_circumstances": False,
            },
            "ok": True,
            "message": "The Passenger agent read the EU261 exposure.",
        },
        {
            "at": "2026-11-23T09:48:00Z",
            "actor": "recovery-orchestrator",
            "autonomy": "orchestrator",
            "event": "auto_execution_authorised",
            "message": "Within every domain's limits; ARROW carries it out and informs OCC.",
        },
    ]
    facts = {"type": "aircraft_defect", "aircraft_reg": "CS-TQA", "unavailable_minutes": 240}
    impact = "GET /api/recovery-options/{option_id}/impact"
    cases.calls[trace] = [
        audited(
            "2026-11-23T09:46:05Z",
            "schedule",
            "get_disruption",
            DETECTION,
            "MCP get_disruption",
            facts,
            {
                "cause": "aircraft_defect",
                "aircraft_reg": "CS-TQA",
                "unavailable_minutes": 240,
                "flights_affected": 4,
                "max_minutes_late": 240,
            },
        ),
        audited(
            "2026-11-23T09:46:20Z",
            "schedule",
            "get_recovery_options",
            AIRCRAFT,
            "MCP get_recovery_options",
            facts,
            {"affected_flights": ["AR101", "AR102"], "options": ["delay", "cancel"]},
        ),
        audited(
            "2026-11-23T09:47:00Z",
            "schedule",
            "get_recovery_options",
            ARROW,
            "GET /api/recovery-options",
            {**facts, "additional_delay_minutes": 15},
            {"affected_flights": ["AR101", "AR102"], "options": ["delay", "cancel"]},
        ),
        audited(
            "2026-11-23T09:47:01Z",
            "slots",
            "get_airport_rules",
            ARROW,
            "GET /api/airports",
            {},
            {"airports": 70},
        ),
        # After the plan was authorised: the executor reads its sources again before it writes.
        audited(
            "2026-11-23T09:48:01Z",
            "schedule",
            "get_aircraft_rotation",
            ARROW,
            "GET /api/aircraft/{aircraft_reg}/rotation",
            {"aircraft_reg": "CS-TQA"},
            {"aircraft_reg": "CS-TQA", "flights": ["AR101", "AR102", "AR131", "AR132"]},
        ),
        audited(
            "2026-11-23T09:48:02Z",
            "crew",
            "assess_crew",
            ARROW,
            impact,
            {**facts, "option_id": "delay", "additional_delay_minutes": 15},
            {"constraint_status": "pass", "duties": 1, "breaches": 0, "changed_flights": 4},
        ),
        # The plan's first write: Maintenance records the defect and issues its event id.
        audited(
            "2026-11-23T09:48:03Z",
            "maintenance",
            "record_defect",
            ARROW,
            "POST /api/defects",
            {
                "event_id": EVENT,
                "request_id": RECORD_ID,
                "aircraft_reg": "CS-TQA",
                "unavailable_minutes": 240,
            },
            {
                "status": "recorded",
                "event_id": EVENT,
                "aircraft_reg": "CS-TQA",
                "unavailable_minutes": 240,
                "unavailable_until": "2026-11-23T13:45:00",
                "message": RECORDED,
            },
        ),
    ]
    foundry = FoundryProject(
        {
            "disruption-detection-agent": DETECTION,
            "aircraft-recovery-agent": AIRCRAFT,
            "passenger-reaccommodation-agent": PASSENGER,
            ORCHESTRATOR_AGENT: ORCHESTRATOR,
        },
        {"disruption-detection-agent": "gpt-4.1-eval", "aircraft-recovery-agent": "gpt-4.1-mini"},
    )
    gateway.agent_definition = foundry.agent_definition  # type: ignore[attr-defined]
    trail = trail_of(cases, operations, trace)

    # What the screen reads never names a host, the MCP endpoint or the trace.
    shown = json.dumps(trail)
    calls = json.dumps([trail["agents"], trail["arrow_checks"], trail["reported_calls"]])
    assert '"host"' not in shown and "/mcp" not in shown and trace not in calls, shown[:400]

    # The disruption the Detection agent recorded on ARROW's case: its type's own fields and what
    # happens if nothing changes; no airline system had it, so no event id.
    disruption = trail["disruption"]
    assert (disruption["kind"], disruption["title"], disruption["event_id"]) == (
        "Aircraft defect",
        "CS-TQA out of service 4 h",
        None,
    ), disruption
    assert trail["recorded_in"] == "arrow", trail["recorded_in"]
    assert disruption["fields"] == [["Aircraft", "CS-TQA"], ["Out of service", "4 h"]], disruption
    assert disruption["knock_on_words"] == "4 flights affected, up to +4 h if nothing changes"
    assert trail["recorded_via"] == "detection", trail["recorded_via"]

    agents = {agent["key"]: agent for agent in trail["agents"]}
    detection, aircraft, passenger = agents["detection"], agents["aircraft"], agents["passenger"]
    # Detection's own calls in order: its record through ARROW (a write), then its read.
    rows = [(c["system"], c["label"], c["access"]) for c in detection["calls"]]
    assert rows == [
        ("ARROW", "Aircraft defect", "write"),
        ("Operations control", "Disruption", "read"),
    ], rows
    recorded, read = detection["calls"]
    assert recorded["summary"] == "CS-TQA out of service 4 h", recorded
    assert (recorded["tool"], recorded["api"], recorded["transport"]) == (
        "record_aircraft_defect",
        None,
        "mcp",
    )
    assert read["summary"] == "CS-TQA out of service 4 h · 4 flights affected, up to +4 h", read
    # An agent's read is a tool call to the system: the tool and what it sent, never a route, a
    # host or MCP's address.
    asked = "type=aircraft_defect&aircraft_reg=CS-TQA&unavailable_minutes=240"
    assert (read["tool"], read["transport"], read["api"]) == ("get_disruption", "mcp", None), read
    # Nothing was recorded in an airline system as the report arrived.
    assert trail["reported_calls"] == [], trail["reported_calls"]
    [options] = aircraft["calls"]
    assert (options["label"], options["summary"], options["tool"]) == (
        "Recovery options",
        "2: delay, cancel · AR101, AR102 affected",
        "get_recovery_options",
    ), options
    assert options["api"] is None, options
    # The Passenger agent reports the EU261 exposure ARROW computed for an option.
    [exposure] = passenger["calls"]
    assert (exposure["system"], exposure["label"], exposure["access"]) == (
        "ARROW",
        "EU261 exposure",
        "read",
    )
    assert (
        exposure["summary"]
        == "Delay +15 min · 331 owed compensation · €85,050 · care €12,026 · rebooking €6,698"
    )

    # ARROW's planning reads, and the executor's re-reads before it wrote, apart.
    assert [c["operation"] for c in trail["arrow_checks"]] == [
        "get_recovery_options",
        "get_airport_rules",
    ]
    checked, rules = trail["arrow_checks"]
    assert checked["api"] == f"GET /api/recovery-options?{asked}&additional_delay_minutes=15"
    assert (rules["label"], rules["summary"]) == (
        "Airport rules",
        "70 airports · Lisbon coordinated, A-CDM, night period 23:55–06:00",
    ), rules
    assert [c["operation"] for c in trail["rechecks"]] == ["get_aircraft_rotation", "assess_crew"]
    rotation, crew = trail["rechecks"]
    assert rotation["summary"] == "CS-TQA · 4 flights: AR101, AR102, AR131, AR132", rotation
    assert (crew["label"], crew["summary"]) == ("Crew impact", "Within duty limits · 1 duty"), crew
    assert crew["api"] == (
        f"GET /api/recovery-options/delay/impact?{asked}&additional_delay_minutes=15"
    ), crew
    assert (trail["stats"]["arrow_checks"], trail["stats"]["rechecks"]) == (2, 2), trail["stats"]
    # The plan's record in Maintenance is its first action, its receipt with the event id.
    [action] = trail["outcome"]["actions"]
    assert action["request"] == {
        "kind": "record_disruption",
        "aircraft_reg": "CS-TQA",
        "unavailable_minutes": 240,
    }, action
    [call] = action["calls"]
    [row] = call["recorded"]
    assert (call["recipient"], row["system"], row["access"], row["api"]) == (
        "Maintenance",
        "Maintenance",
        "write",
        "POST /api/defects",
    ), row
    assert row["summary"] == "CS-TQA out of service 4 h, until 13:45 · event EV-9C2D41", row

    # Models by their names; each agent's Foundry name and version for its link.
    assert (detection["model"], aircraft["model"]) == ("GPT-4.1", "GPT-4.1 mini")
    assert detection["foundry"] == {"name": "disruption-detection-agent", "version": "7"}
    assert trail["orchestrator"]["foundry"] == {"name": ORCHESTRATOR_AGENT, "version": "7"}


class DecisionTrailTests(unittest.TestCase):
    def test_a_repeated_report_reads_as_not_recorded_again(self) -> None:
        check_a_repeated_report_reads_as_not_recorded_again()

    def test_a_crosswind_says_gusts_only_when_the_wind_gusts(self) -> None:
        check_a_crosswind_says_gusts_only_when_the_wind_gusts()

    def test_the_new_agent_flow_reads_in_plain_words(self) -> None:
        check_the_new_agent_flow_reads_in_plain_words()

    def test_the_chat_stays_out_of_how_arrow_decided(self) -> None:
        check_the_chat_stays_out_of_how_arrow_decided()

    def test_each_call_is_one_row_named_by_its_recipient(self) -> None:
        check_each_call_is_one_row_named_by_its_recipient()

    def test_a_refused_call_reads_as_refused(self) -> None:
        check_a_refused_call_reads_as_refused()

    def test_each_system_call_shows_its_endpoint_caller_request_and_answer(self) -> None:
        check_each_system_call_shows_its_endpoint_caller_request_and_answer()

    def test_each_action_shows_its_own_request_and_receipt(self) -> None:
        check_each_action_shows_its_own_request_and_receipt()

    def test_a_failed_tool_call_shows_as_failed(self) -> None:
        check_a_failed_tool_call_shows_as_failed()

    def test_trail_shows_agents_lookups_prompts_rules_and_outcome(self) -> None:
        check_trail_shows_agents_lookups_prompts_rules_and_outcome()

    def test_parallel_answers_stay_with_their_own_question(self) -> None:
        check_parallel_answers_stay_with_their_own_question()
