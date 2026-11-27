"""Orchestrated recovery end to end: intake, a scripted orchestrator using the ARROW tools, the
policy engine, OCC decisions and execution against the mocked systems.

Real intake, tools, planner, policy, store semantics, executor and system services; the
orchestrator agent is a script and HTTP is replaced by direct calls.
"""

from __future__ import annotations

import base64
import json
import unittest
import uuid
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any

from arrow_scenario import CaseMemory, ScenarioStore
from fastapi.testclient import TestClient
from test_execution_workflow import SystemsGateway
from test_operator_flow import headers

from airline_systems.operating_day import max_daily_fdp_minutes
from ops_app.app import create_app
from ops_app.engine import (
    CloudDependencyError,
    Operations,
    planned,
    rotation_prepared,
    storm_prepared,
)
from ops_app.events import Delivery, Rejections
from ops_app.intake import Intake
from ops_app.planner import CaseRef, disruption_facts
from ops_app.rules import RuleStore
from ops_app.tools import ArrowTools, ToolRefused

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
OPERATOR = "00000000-0000-4000-8000-000000000010"
HOST = "11111111-2222-3333-4444-555555555555"
AGENT = "22222222-3333-4444-5555-666666666666"
CASE_ASSISTANT = "33333333-4444-5555-6666-777777777777"
DEFECT = {
    "type": "aircraft_defect",
    "aircraft_reg": "CS-TQA",
    "description": "Hydraulic leak",
    "unavailable_minutes": 240,
}
STAFF_DELAY = {
    "type": "flight_delay",
    "flight_no": "AR131",
    "delay_minutes": 20,
    "cause": "staff_shortage",
    "description": "Ground staff shortage at LIS",
}
Script = Callable[[ArrowTools, str, dict[str, Any]], dict[str, Any]]
# The Case Assistant as a script: (trace, brief) -> {"answer", "trail"}, or it raises.
Assistant = Callable[[str, dict[str, Any]], dict[str, Any]]


class ScriptedGateway(SystemsGateway):
    """The mocked systems in-process, an orchestrator that follows a script, and a Case
    Assistant that follows another."""

    def __init__(self, store: ScenarioStore, script: Script) -> None:
        super().__init__(store)
        self.script = script
        self.briefs: list[dict[str, Any]] = []
        self.tools: ArrowTools | None = None
        self.assistant: Assistant | None = None
        self.questions: list[dict[str, Any]] = []

    def prepare(self, document: dict[str, Any], progress: Any) -> dict[str, Any]:
        """As CloudGateway.prepare: the systems are only read, by the disruption's facts, and
        nothing is recorded in them before a plan runs."""
        trace, disruption = document["trace_id"], planned(document["disruption"])
        case = CaseRef(trace, disruption_facts(disruption, document.get("defect")))
        candidates = self.options(case, 0)
        if disruption["type"] == "weather":
            # Weather names no aircraft: no rotation is read.
            return storm_prepared(disruption, candidates, self.network(trace)["flights"])
        registration = str(
            case.disruption.get("aircraft_reg") or candidates["aircraft"]["registration"]
        )
        return rotation_prepared(
            disruption, registration, self.rotation(trace, registration), candidates
        )

    def orchestrate(
        self, trace: str, brief: dict[str, Any], *, timeout: float = 600
    ) -> dict[str, Any]:
        assert self.tools is not None
        self.briefs.append(brief)
        return self.script(self.tools, trace, brief)

    def ask_case_assistant(
        self, trace: str, brief: dict[str, Any], *, timeout: float
    ) -> dict[str, Any]:
        assert self.assistant is not None
        self.questions.append(brief)
        return self.assistant(trace, brief)


def operate(
    store: ScenarioStore, script: Script, rules: RuleStore | None = None
) -> tuple[Operations, CaseMemory, ScriptedGateway]:
    cases = CaseMemory()
    gateway = ScriptedGateway(store, script)
    operations = Operations(cases, gateway, rules)
    gateway.tools = ArrowTools(
        cases,
        gateway,
        execute=operations.execute,
        rules=operations.rules,
        record=operations.record_reading,
    )
    return operations, cases, gateway


def submitted(plan_id: str) -> dict[str, Any]:
    return {"outcome": "submitted", "summary": "Plan submitted", "submitted_plan_id": plan_id}


def insist(tools: ArrowTools, trace: str, plan_id: str, reason: str) -> dict[str, Any]:
    """Submit as the orchestrator may: a plan ARROW sends back for reconsideration (offered
    options not assessed yet, or another legal plan disrupts fewer passengers; each once) is
    submitted again with the same reason."""
    result = tools.submit_plan(trace, plan_id, reason)
    for _ in range(2):
        if result.get("status") != "reconsider":
            break
        result = tools.submit_plan(trace, plan_id, reason)
    return result


def check_occ_case_ranks_plans_and_waits_for_the_duty_manager() -> None:
    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        tools.record_case(
            trace,
            "CS-TQA grounded until 13:45: four rotations at risk",
            [{"agent": "aircraft-recovery-agent", "text": "Delay or cancel the LIS-OPO pair"}],
        )
        cancel = tools.build_plan(trace, {"aircraft_option_id": "cancel"})
        delay = tools.build_plan(trace, {"aircraft_option_id": "delay"})
        # Schedule also offered a swap, with no plan yet: ARROW sends the delay back once for it.
        unassessed = tools.submit_plan(trace, delay["plan_id"], "Delay keeps every passenger")
        assert unassessed["status"] == "reconsider", unassessed
        assert [o["option_id"] for o in unassessed["unassessed_options"]] == ["swap"], unassessed
        # The delay disrupts 7 passengers, the cancel 5: ARROW sends the delay back once with
        # the comparison; the orchestrator keeps it, and its reason stands.
        first = tools.submit_plan(trace, delay["plan_id"], "Delay keeps every passenger travelling")
        assert first["status"] == "reconsider", first
        assert first["better_plan_id"] == cancel["plan_id"], first
        assert "5 passengers lose their flight or connection; with this plan, 7" in first["message"]
        assert "recommended" not in str(tools.store.get(trace)["options"]), "nothing recorded"
        insist(tools, trace, delay["plan_id"], "Delay keeps every passenger travelling")
        return submitted(delay["plan_id"])

    operations, cases, gateway = operate(ScenarioStore(TRACE), script)
    trace = operations.open(DEFECT, OPERATOR)["trace_id"]
    operations.run(trace)

    case = cases.get(trace)
    assert case["status"] == "assessment_ready", case["status"]
    # A typed report is recorded on ARROW's case before the run, and in no airline system: the
    # brief names its fields and no event id.
    assert gateway.briefs[0]["disruption"]["type"] == "aircraft_defect", gateway.briefs[0]
    assert "event_id" not in gateway.briefs[0], gateway.briefs[0]
    assert gateway.store.defect["trace_id"] == TRACE, "Maintenance holds only the scenario's"
    plans = case["options"]
    costs = [plan["cost"]["total_eur"] for plan in plans]
    assert costs == sorted(costs), costs
    # Priced per booking (passenger cost spec), the delay (€14,621) is cheaper than cancelling
    # AR101/AR102 (€26,785: two day-of-operation cancellations plus care and rebooking).
    assert [plan["kind"] for plan in plans] == ["delay", "cancel"]
    [recommended] = [plan for plan in plans if plan.get("recommended")]
    assert recommended["kind"] == "delay"
    assert recommended["recommendation_reason"] == "Delay keeps every passenger travelling"
    assert recommended["policy"]["route"] == "occ" and "execution" not in case
    assert case["summary"].startswith("CS-TQA grounded")

    # Before OCC decides, each plan says exactly what approving it would change.
    claims = {"claims": [{"typ": "oid", "val": OPERATOR}]}
    header = {"X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(claims).encode()).decode()}
    app = create_app(cases, operations, frozenset({OPERATOR}), frozenset({HOST}), "https://a.test")
    with TestClient(app) as client:
        shown = client.get(f"/api/cases/{trace}", headers=header).json()["options"]
    previews = {plan["kind"]: [a["summary"] for a in plan["planned_actions"]] for plan in shown}
    # Approving either plan first records the defect in Maintenance.
    assert {previews[kind][0] for kind in previews} == {
        "Record the CS-TQA defect: out of service 4 h"
    }
    assert previews["cancel"][1].startswith("Cancel AR101"), previews
    assert any(s.startswith("CNL AR101") for s in previews["cancel"]), previews
    assert previews["delay"][1].startswith("Retime AR101"), previews

    operations.record_decision(
        trace,
        option_id=recommended["option_id"],
        decision="approve",
        expected_version=case["version"],
        idempotency_key="decision-approve-occ-0001",
        actor_oid=OPERATOR,
    )
    operations.execute(trace)

    execution = cases.get(trace)["execution"]
    assert execution["status"] == "completed", execution
    assert execution["approved_by"] == OPERATOR
    assert {action["status"] for action in execution["actions"]} == {"confirmed"}
    assert execution["actions"][0]["kind"] == "record_disruption", execution["actions"][0]
    assert gateway.store.defect["trace_id"] == trace, "Maintenance recorded the case's defect"


def check_auto_case_executes_without_a_human() -> None:
    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        plan = tools.build_plan(trace, {"aircraft_option_id": "delay"})
        result = tools.submit_plan(trace, plan["plan_id"], "Within every domain's limits")
        assert result["route"] == "auto", result
        return submitted(plan["plan_id"])

    store = ScenarioStore(TRACE)
    operations, cases, gateway = operate(store, script)
    trace = operations.open(STAFF_DELAY, OPERATOR)["trace_id"]
    operations.run(trace)

    case = cases.get(trace)
    assert gateway.briefs[0]["disruption"] == {
        "type": "flight_delay",
        "flight_no": "AR131",
        "delay_minutes": 20,
        "cause": "staff_shortage",
        "aircraft_reg": "CS-TQA",
    }, gateway.briefs[0]
    assert "event_id" not in gateway.briefs[0], gateway.briefs[0]
    # Owner: the orchestrator names the case by the id OCC sees, not the trace id.
    assert gateway.briefs[0]["case_id"] == case["display_id"]
    assert "trace_id" not in gateway.briefs[0]
    assert case["disruption"]["scope"]["aircraft_reg"] == "CS-TQA"
    assert case["execution"]["status"] == "completed", case["execution"]
    assert case["execution"]["approved_by"] == "policy"
    events = [event["event"] for event in case["events"]]
    assert "auto_execution_authorised" in events
    assert "duty_manager_decision" not in events
    assert store.flights["AR131"]["std_local"].endswith("T14:30:00")
    # The plan ran by itself, so it recorded the delay in operations control first, with its
    # receipt and the event id operations control issued.
    record = case["execution"]["actions"][0]
    assert (record["kind"], record["status"]) == ("record_disruption", "confirmed"), record
    assert record["calls"][0]["event_id"].startswith("EV-"), record["calls"]
    assert store.delays[trace]["cause"] == "staff_shortage", store.delays


def check_orchestrator_without_submission_leaves_the_decision_to_occ() -> None:
    def builds_then_stops(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        tools.build_plan(trace, {"aircraft_option_id": "delay"})
        return {"outcome": "no_plan", "summary": "Unsure", "submitted_plan_id": None}

    def builds_then_times_out(
        tools: ArrowTools, trace: str, brief: dict[str, Any]
    ) -> dict[str, Any]:
        tools.build_plan(trace, {"aircraft_option_id": "delay"})
        raise CloudDependencyError("Cloud dependency could not be reached; retry shortly.")

    def times_out(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        raise CloudDependencyError("Cloud dependency could not be reached; retry shortly.")

    for script in (builds_then_stops, builds_then_times_out):
        operations, cases, _ = operate(ScenarioStore(TRACE), script)
        trace = operations.open(STAFF_DELAY, OPERATOR)["trace_id"]
        operations.run(trace)
        case = cases.get(trace)
        assert case["status"] == "assessment_ready", (script.__name__, case["status"])
        assert "execution" not in case
        assert case["events"][-1]["message"] == "Orchestrator made no submission; OCC decides"
        # OCC can act on what the orchestrator left: the automatic plan is now its decision.
        [plan] = case["options"]
        operations.record_decision(
            trace,
            option_id=plan["option_id"],
            decision="approve",
            expected_version=case["version"],
            idempotency_key=f"decision-{script.__name__[:20]}-0001",
            actor_oid=OPERATOR,
        )
        operations.execute(trace)
        assert cases.get(trace)["execution"]["status"] == "completed", script.__name__

    def builds_only_blocked(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        tools.build_plan(trace, {"aircraft_option_id": "delay", "reserve_option_id": "R9"})
        return {"outcome": "no_plan", "summary": "Nothing usable", "submitted_plan_id": None}

    for script in (times_out, builds_only_blocked):
        operations, cases, _ = operate(ScenarioStore(TRACE), script)
        trace = operations.open(STAFF_DELAY, OPERATOR)["trace_id"]
        operations.run(trace)
        case = cases.get(trace)
        assert case["status"] == "failed", (script.__name__, case["status"])
        assert case["stage"].startswith("Assessment stopped"), case["stage"]


def check_plans_built_at_the_same_time_are_all_kept() -> None:
    # A second build lands while the first is still being planned (parallel tool calls).
    from ops_app import tools as tools_module

    real = tools_module.build_plan
    built: dict[str, Any] = {}
    nested = {"done": False}

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        def interleaved(
            gateway: Any, document: Any, levers: Any, limits: Any, **options: Any
        ) -> dict[str, Any]:
            if not nested["done"]:
                nested["done"] = True
                # Both are OCC plans: code builds nothing after an automatic plan, and levers the
                # systems did not offer are refused.
                built["second"] = tools.build_plan(trace, {"aircraft_option_id": "cancel"})
            return real(gateway, document, levers, limits, **options)

        tools_module.build_plan = interleaved  # type: ignore[assignment]
        try:
            built["first"] = tools.build_plan(trace, {"aircraft_option_id": "delay"})
        finally:
            tools_module.build_plan = real
        return {"outcome": "no_plan", "summary": "Compared", "submitted_plan_id": None}

    operations, cases, _ = operate(ScenarioStore(TRACE), script)
    trace = operations.open(DEFECT, OPERATOR)["trace_id"]
    operations.run(trace)

    stored = {plan["plan_id"] for plan in cases.get(trace)["options"]}
    assert stored == {built["first"]["plan_id"], built["second"]["plan_id"]}, (stored, built)


def check_same_event_reported_twice_opens_one_case() -> None:
    operations, cases, _ = operate(ScenarioStore(TRACE), lambda *_: submitted("x"))
    event_id = str(uuid.uuid4())

    first = operations.open({**STAFF_DELAY, "event_id": event_id}, OPERATOR)
    second = operations.open({**STAFF_DELAY, "event_id": event_id}, OPERATOR)

    assert first["trace_id"] == second["trace_id"]
    assert first["created"] is True and second["created"] is False
    assert len(cases.docs) == 1


def check_options_not_offered_are_refused_and_never_become_plans() -> None:
    # Owner ("why do I see the same plan twice?"): a plan from levers the systems did
    # not offer (a standby crew they never named) is refused with its reason and never lands on
    # the case. Delay messages are not such a lever: ARROW sets them from the plan's flights, so
    # the same option asked with them off is the same plan (live run 4), never a second one.
    seen: dict[str, Any] = {}

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        refused: list[str] = []
        try:
            tools.build_plan(trace, {"aircraft_option_id": "delay", "reserve_option_id": "R9"})
        except ToolRefused as refusal:
            refused.append(str(refusal))
        seen["refused"] = refused
        good = tools.build_plan(trace, {"aircraft_option_id": "delay"})
        seen["off"] = tools.build_plan(
            trace, {"aircraft_option_id": "delay", "delay_messages": False}
        )
        seen["good"] = tools.submit_plan(trace, good["plan_id"], "Within limits")
        seen["good_id"] = good["plan_id"]
        return submitted(good["plan_id"])

    operations, cases, _ = operate(ScenarioStore(TRACE), script)
    trace = operations.open(STAFF_DELAY, OPERATOR)["trace_id"]
    operations.run(trace)

    [refused] = seen["refused"]
    assert refused.startswith("Not built: ") and "R9" in refused, refused
    # AR131 leaves 20 min late: a delay message is required whatever the lever asked.
    off = seen["off"]
    assert (
        off["plan_id"],
        off["delay_messages_required"],
        off["departures_moved_over_15_min"],
    ) == (
        seen["good_id"],
        True,
        1,
    ), off
    assert seen["good"]["route"] == "auto"
    case = cases.get(trace)
    assert [plan["plan_id"] for plan in case["options"]] == [seen["good_id"]], case["options"]
    assert case["options"][0]["levers"]["delay_messages"] is True
    assert case["execution"]["option_id"] == seen["good_id"]


def standby_case() -> tuple[Operations, CaseMemory, ScenarioStore, str, list[str]]:
    """AR131 60 min late breaks D-LIS-001; Lisbon has two standby crews and R1 keeps the duty
    legal, leaving one, so the plan runs automatically. Its execution waits (the executor
    starts later), as when cases are assessed two at a time (D110)."""
    deferred: list[str] = []

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        plan = tools.build_plan(trace, {"aircraft_option_id": "delay", "reserve_option_id": "R1"})
        assert (
            tools.submit_plan(trace, plan["plan_id"], "Reserve keeps D-LIS-001 legal")["route"]
            == "auto"
        )
        return submitted(plan["plan_id"])

    store = ScenarioStore(TRACE)
    store.duties["D-LIS-001"]["max_fdp_minutes"] = 450
    # Task 2: a second standby crew, so calling R1 leaves Lisbon one (the last goes to OCC).
    store.reserves += [
        {**member, "crew_id": f"R1{member['crew_id'][1:]}"} for member in store.reserves
    ]
    operations, cases, gateway = operate(store, script)
    gateway.tools = ArrowTools(cases, gateway, execute=deferred.append, rules=operations.rules)
    trace = operations.open({**STAFF_DELAY, "delay_minutes": 60}, OPERATOR)["trace_id"]
    operations.run(trace)
    return operations, cases, store, trace, deferred


def check_reserve_used_before_execution_stops_with_a_clear_reason() -> None:
    # Review Focus 4 at the executor: the reserve went to another case after submission and
    # before the pre-flight recheck. Crew no longer offers it; nothing is written.
    operations, cases, store, trace, deferred = standby_case()
    store.reserves = []  # another case assigned them
    operations.execute(deferred[0])

    case = cases.get(trace)
    assert case["execution"]["status"] == "needs_attention", case["execution"]
    assert not case["execution"].get("interrupted_runs"), case["execution"]
    assert "R1" in case["events"][-1]["message"], case["events"][-1]
    assert store.actions == {}


def check_a_standby_crew_taken_since_an_automatic_plan_stops_it_for_occ() -> None:
    # Review (D110: cases are assessed two at a time): before this automatic plan is
    # carried out, another case takes Lisbon's other standby crew. R1 is still offered and the
    # duties are unchanged, but the plan would now use Lisbon's last crew: it stops for OCC.
    operations, cases, store, trace, deferred = standby_case()
    store.reserves = [r for r in store.reserves if not r["crew_id"].startswith("R1")]
    operations.execute(deferred[0])

    case = cases.get(trace)
    assert case["execution"]["status"] == "needs_attention", case["execution"]
    message = case["events"][-1]["message"]
    assert "standby crews changed since the plan: OCC decides" in message, message
    assert store.actions == {}


def check_a_slots_outage_while_recording_does_not_block_the_plans() -> None:
    # Review: the case kept an empty set of airport rules when Slots was down as the
    # delay was recorded, so every plan moving a flight was blocked "airport rules unknown".
    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        plan = tools.build_plan(trace, {"aircraft_option_id": "delay"})
        tools.submit_plan(trace, plan["plan_id"], "Within every domain's limits")
        return submitted(plan["plan_id"])

    operations, cases, gateway = operate(ScenarioStore(TRACE), script)
    airports = gateway.airports
    calls: list[str] = []

    def slots_down_once(trace: str) -> dict[str, Any]:
        calls.append(trace)
        if len(calls) == 1:
            raise CloudDependencyError("Slots returned HTTP 503", status_code=503, retryable=True)
        return airports(trace)

    gateway.airports = slots_down_once  # type: ignore[method-assign]
    trace = operations.open(STAFF_DELAY, OPERATOR)["trace_id"]
    operations.run(trace)

    case = cases.get(trace)
    [plan] = case["options"]
    assert plan["policy"]["route"] == "auto", plan["policy"]
    assert "LIS" in case["airports"], "the rules read for the plan are kept on the case"
    kinds = {action["kind"] for action in case["execution"]["actions"]}
    assert "airport_cdm.tobt_update" in kinds, kinds  # Lisbon's A-CDM rule reached execution
    assert case["execution"]["status"] == "completed", case["execution"]


class TightLisbon(ScenarioStore):
    """CS-TQB has crew too, and Lisbon takes three movements an hour."""

    def __init__(self) -> None:
        super().__init__(TRACE)

        def at(hour: int, minute: int) -> str:
            return datetime.combine(self.day, time(hour, minute)).isoformat()

        self.duties["D-LIS-002"] = {
            **self.duties["D-LIS-001"],
            "duty_id": "D-LIS-002",
            "report_local": at(9, 50),
            "release_local": at(13, 30),
            "fdp_minutes": 190,  # report 09:50 to AR112 on-blocks 13:00
            "max_fdp_minutes": max_daily_fdp_minutes(time(9, 50), 2, 2),
            "flight_nos": ["AR111", "AR112"],
            "members": [
                {"crew_id": f"C1{n}", "rank": rank}
                for n, rank in enumerate(("CPT", "FO", "SC", "CC", "CC", "CC"))
            ],
        }

    def domain_inventory(self, day: date, domain: str) -> dict[str, Any]:
        inventory = super().domain_inventory(day, domain)
        if domain == "slots":
            inventory["capacity"]["LIS"] = 3
        return inventory


def check_two_approved_plans_cannot_overfill_a_lisbon_hour() -> None:
    # Review: AR101 +70 and AR111 +60 each land a flight in Lisbon's 14:00 hour. Each
    # plan fits alone; after the first is carried out, the second would put four movements in
    # an hour that takes three. It stops when rechecked and writes nothing.
    def builds(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        tools.build_plan(trace, {"aircraft_option_id": "delay"})
        return {"outcome": "no_plan", "summary": "OCC decides", "submitted_plan_id": None}

    store = TightLisbon()
    operations, cases, _ = operate(store, builds)
    traces = [
        operations.open({**STAFF_DELAY, "flight_no": flight, "delay_minutes": minutes}, OPERATOR)[
            "trace_id"
        ]
        for flight, minutes in (("AR101", 70), ("AR111", 60))
    ]
    for number, trace in enumerate(traces):
        operations.run(trace)
        case = cases.get(trace)
        [plan] = case["options"]
        assert plan["policy"]["route"] != "blocked", plan["policy"]
        operations.record_decision(
            trace,
            option_id=plan["option_id"],
            decision="approve",
            expected_version=case["version"],
            idempotency_key=f"decision-lisbon-hour-{number:04d}",
            actor_oid=OPERATOR,
        )
    second_rotation = {n: dict(store.flights[n]) for n in ("AR111", "AR112")}
    for trace in traces:
        operations.execute(trace)

    first, second = (cases.get(trace) for trace in traces)
    assert first["execution"]["status"] == "completed", first["execution"]
    assert second["execution"]["status"] == "needs_attention", second["execution"]
    assert second["execution"]["actions"] == []
    stop = second["events"][-1]["message"]
    assert stop.startswith("Changed since approval: Lisbon capacity 14:00"), stop
    assert "4 movements, limit 3" in stop, stop
    assert {n: store.flights[n] for n in second_rotation} == second_rotation


def check_mcp_tools_refuse_other_identities() -> None:
    def principal(oid: str) -> dict[str, str]:
        claims = {"claims": [{"typ": "oid", "val": oid}]}
        return {
            "X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(claims).encode()).decode(),
            "Accept": "application/json, text/event-stream",
        }

    operations, cases, _ = operate(ScenarioStore(TRACE), lambda *_: submitted("x"))
    app = create_app(
        cases,
        operations,
        frozenset({OPERATOR}),
        frozenset({HOST}),
        "https://arrow.test",
        orchestrator_oids=frozenset({AGENT}),
        assistant_oids=frozenset({CASE_ASSISTANT}),
    )
    listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    with TestClient(app) as client:
        refused = client.post("/mcp", json=listing, headers=principal(OPERATOR))
        allowed = client.post("/mcp", json=listing, headers=principal(AGENT))
        assistant = client.post("/mcp", json=listing, headers=principal(CASE_ASSISTANT))

    assert refused.status_code == 403, refused.text
    assert (allowed.status_code, assistant.status_code) == (200, 200), assistant.text
    names = {tool["name"] for tool in allowed.json()["result"]["tools"]}
    assert names == {
        "record_aircraft_defect",
        "record_flight_delay",
        "record_weather_forecast",
        "get_compensation_exposure",
        "record_case",
        "get_playbook",
        "build_plan",
        "submit_plan",
        "read_case",
        "what_if",
    }, names


def check_the_playbook_is_read_by_the_orchestrator_and_the_case_assistant_only() -> None:
    # Playbook Task 1: both agents may read the actions that apply to their own case;
    # a domain agent's identity is not admitted to ARROW's tools at all.
    def principal(oid: str, trace: str) -> dict[str, str]:
        claims = {"claims": [{"typ": "oid", "val": oid}]}
        return {
            "X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(claims).encode()).decode(),
            "Accept": "application/json, text/event-stream",
            "traceparent": f"00-{trace}-{'1' * 16}-01",
        }

    operations, cases, _ = operate(ScenarioStore(TRACE), lambda *_: submitted("x"))
    first = operations.open(DEFECT, OPERATOR)
    second = operations.open(STAFF_DELAY, OPERATOR)
    app = create_app(
        cases,
        operations,
        frozenset({OPERATOR}),
        frozenset({HOST}),
        "https://arrow.test",
        orchestrator_oids=frozenset({AGENT}),
        assistant_oids=frozenset({CASE_ASSISTANT}),
    )
    crew_agent = "44444444-5555-6666-7777-888888888888"

    def read(oid: str, case_id: str) -> Any:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_playbook", "arguments": {"case_id": case_id}},
        }
        return client.post("/mcp", json=body, headers=principal(oid, first["trace_id"]))

    with TestClient(app) as client:
        orchestrator, assistant, other, domain = (
            read(AGENT, first["display_id"]),
            read(CASE_ASSISTANT, first["display_id"]),
            read(AGENT, second["display_id"]),
            read(crew_agent, first["display_id"]),
        )

    for response in (orchestrator, assistant):
        result = response.json()["result"]
        assert not result.get("isError"), result
        playbook = json.loads(result["content"][0]["text"])
        assert playbook["disruption_type"] == "aircraft_defect", playbook
        assert "MX-ETR" in {action["id"] for action in playbook["actions"]}, playbook
    assert "another case" in other.json()["result"]["content"][0]["text"], other.json()
    assert domain.status_code == 403, domain.text


def check_orchestrator_acts_only_on_its_own_case_by_case_id() -> None:
    # Owner: agents name the case by the id OCC sees. The run's trace (Foundry carries
    # it into each tool call) binds the call to its own case; a wrong id is told why.
    def principal(oid: str, trace: str) -> dict[str, str]:
        claims = {"claims": [{"typ": "oid", "val": oid}]}
        return {
            "X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(claims).encode()).decode(),
            "Accept": "application/json, text/event-stream",
            "traceparent": f"00-{trace}-{'1' * 16}-01",
        }

    operations, cases, _ = operate(ScenarioStore(TRACE), lambda *_: submitted("x"))
    first = operations.open(STAFF_DELAY, OPERATOR)
    second = operations.open({**STAFF_DELAY, "flight_no": "AR259"}, OPERATOR)
    app = create_app(
        cases,
        operations,
        frozenset({OPERATOR}),
        frozenset({HOST}),
        "https://arrow.test",
        orchestrator_oids=frozenset({AGENT}),
    )

    def record(case_id: str, run: str) -> dict[str, Any]:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "record_case",
                "arguments": {"case_id": case_id, "summary": "AR131 is late", "findings": []},
            },
        }
        response = client.post("/mcp", json=body, headers=principal(AGENT, run))
        assert response.status_code == 200, response.text
        result: dict[str, Any] = response.json()["result"]
        return result

    with TestClient(app) as client:
        own = record(first["display_id"], first["trace_id"])
        other = record(second["display_id"], first["trace_id"])
        malformed = record("dsr-123", first["trace_id"])
        unknown = record(f"{first['display_id'][:13]}000000", first["trace_id"])

    assert not own.get("isError"), own
    assert first["trace_id"] not in json.dumps(own), own
    assert other.get("isError") and "another case" in other["content"][0]["text"], other
    assert "Not a case id" in malformed["content"][0]["text"], malformed
    assert "No case" in unknown["content"][0]["text"], unknown
    assert cases.get(second["trace_id"]).get("summary") is None


def check_delay_intake_rejects_invalid_minutes() -> None:
    operations, cases, _ = operate(ScenarioStore(TRACE), lambda *_: submitted("x"))
    app = create_app(
        cases, operations, frozenset({OPERATOR}), frozenset({HOST}), "https://arrow.test"
    )
    claims = {"claims": [{"typ": "oid", "val": OPERATOR}]}
    headers = {
        "X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(claims).encode()).decode(),
        "X-ARROW-Request": "operator",
        "Origin": "https://arrow.test",
    }
    with TestClient(app) as client:
        responses = [
            client.post(
                "/api/cases", json={**STAFF_DELAY, "delay_minutes": minutes}, headers=headers
            )
            for minutes in (7, 200)
        ]

    assert [response.status_code for response in responses] == [422, 422]
    assert cases.docs == {}


def check_a_flight_just_over_3_h_late_lands_under_3_h_and_owes_no_compensation() -> None:
    # Owner: speed up only when the minutes are worth it (cost index and time value research §5).
    # From 3 h late at the destination EU261 owes every passenger compensation, so a flight just
    # over it flies faster to land under 3 h. AR151 LIS-MAD is 115 min in the air: it makes up 4 min
    # when late, and stretches to 6 to reach that step. It carries SYN002's 3 passengers from Oporto
    # to Madrid; AR161 later is under 3 h late either way.
    for late, made_up, fuel, co2, eur in (
        (182, 4, 341.6, 70.56, 412.16),
        (185, 6, 512.4, 105.84, 618.24),
    ):
        store = ScenarioStore(TRACE)
        for duty_id, flight_no, report, release, fdp in (
            ("D-LIS-002", "AR151", (13, 0), (16, 45), 195),
            ("D-LIS-003", "AR161", (16, 30), (20, 30), 210),
        ):
            store.duties[duty_id] = {
                **store.duties["D-LIS-001"],
                "duty_id": duty_id,
                "report_local": datetime.combine(store.day, time(*report)).isoformat(),
                "release_local": datetime.combine(store.day, time(*release)).isoformat(),
                "fdp_minutes": fdp,
                "max_fdp_minutes": max_daily_fdp_minutes(time(*report), 1, 2),
                "flight_nos": [flight_no],
                "members": [
                    {"crew_id": f"C{duty_id[-1]}{number}", "rank": rank}
                    for number, rank in enumerate(("CPT", "FO", "SC", "CC", "CC", "CC"))
                ],
            }

        def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
            plan = tools.build_plan(trace, {"aircraft_option_id": "delay"})
            tools.submit_plan(trace, plan["plan_id"], "Fly CS-TQC's flights once it is repaired")
            return submitted(plan["plan_id"])

        operations, cases, _ = operate(store, script)
        # Reported at 09:45; AR151 is due off at 14:00, 255 min later.
        defect = {**DEFECT, "aircraft_reg": "CS-TQC", "unavailable_minutes": 255 + late}
        trace = operations.open(defect, OPERATOR)["trace_id"]
        operations.run(trace)

        [plan] = cases.get(trace)["options"]
        [flight] = [f for f in plan["flights"] if f["flight_no"] == "AR151"]
        leaves = datetime.combine(store.day, time(14, 0)) + timedelta(minutes=late)
        lands = datetime.combine(store.day, time(16, 15)) + timedelta(minutes=late - made_up)
        assert (flight["std_local"], flight["sta_local"]) == (
            leaves.isoformat(),
            lands.isoformat(),
        ), flight
        [line] = [
            f for f in plan["cost"]["workings"]["airline"]["fuel"] if f["flight_no"] == "AR151"
        ]
        # Fuel at €85.40 a minute (70 kg) and, within the EEA, CO2 allowances at €17.64.
        assert line == {
            "flight_no": "AR151",
            "made_up_minutes": made_up,
            "made_up_possible": made_up,
            "reason": "arrives under 3 h late: EU261 avoided",
            "aircraft": "narrow",
            "fuel_eur": fuel,
            "co2_eur": co2,
            "eur": eur,
        }, line
        cost = plan["cost"]
        assert (cost["compensation_eur"], cost["owed_compensation_passengers"]) == (0, 0), cost
        # Stretching past 3.5% of the time in the air says so (owner); AR161 keeps to it.
        basis = cost["workings"]["airline"]["fuel_basis"]
        stretch = (
            "; up to 6% for AR151, the top of the published 4–6% range, to land under 3 h late"
            " (EU261)."
        )
        assert basis.startswith("3.5% of each flight's time in the air"), basis
        assert (stretch in basis) == (made_up == 6), basis


def check_control_tower_lists_urgent_cases_first_with_their_impact() -> None:
    def occ(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        tools.build_plan(trace, {"aircraft_option_id": "cancel"})
        delay = tools.build_plan(trace, {"aircraft_option_id": "delay"})
        insist(tools, trace, delay["plan_id"], "Delay keeps every passenger travelling")
        return submitted(delay["plan_id"])

    def auto(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        plan = tools.build_plan(trace, {"aircraft_option_id": "delay"})
        tools.submit_plan(trace, plan["plan_id"], "Within every domain's limits")
        return submitted(plan["plan_id"])

    def unsubmitted(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        tools.build_plan(trace, {"aircraft_option_id": "delay"})
        return {"outcome": "no_plan", "summary": "Unsure", "submitted_plan_id": None}

    operations, cases, _ = operate(ScenarioStore(TRACE), occ)
    defect = operations.open(DEFECT, OPERATOR)["trace_id"]
    operations.run(defect)
    delay_operations, delay_cases, _ = operate(ScenarioStore(TRACE), auto)
    delay = delay_operations.open(STAFF_DELAY, OPERATOR)["trace_id"]
    delay_operations.run(delay)
    cases.docs[delay] = delay_cases.get(delay)
    # An automatic plan left unsubmitted goes to OCC, who approves it: OCC, not policy, ran it.
    handed_operations, handed_cases, _ = operate(ScenarioStore(TRACE), unsubmitted)
    handed = handed_operations.open(STAFF_DELAY, OPERATOR)["trace_id"]
    handed_operations.run(handed)
    waiting = handed_cases.get(handed)
    handed_operations.record_decision(
        handed,
        option_id=waiting["options"][0]["option_id"],
        decision="approve",
        expected_version=waiting["version"],
        idempotency_key="decision-handed-to-occ-0001",
        actor_oid=OPERATOR,
    )
    handed_operations.execute(handed)
    cases.docs[handed] = handed_cases.get(handed)
    # A malformed document never blanks the start screen: it is listed for OCC to open.
    malformed = {
        **delay_cases.get(delay),
        "trace_id": uuid.uuid4().hex,
        "display_id": "DSR-MALFORMED",
        "disruption": "oops",
        "options": "oops",
    }
    cases.docs[malformed["trace_id"]] = malformed
    # An assessment that stopped before any plan: no plan and nothing executed.
    failed = {
        **delay_cases.get(delay),
        "status": "failed",
        "options": [],
        "trace_id": uuid.uuid4().hex,
        "display_id": "DSR-FAILED",
    }
    failed.pop("execution")
    cases.docs[failed["trace_id"]] = failed
    # A message still being read: no scope, no rotation, no plan.
    reading = operations.open({"type": "message", "text": "AR205 crew sick"}, OPERATOR)

    claims = {"claims": [{"typ": "oid", "val": OPERATOR}]}
    header = {"X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(claims).encode()).decode()}
    app = create_app(cases, operations, frozenset({OPERATOR}), frozenset({HOST}), "https://a.test")
    with TestClient(app) as client:
        response = client.get("/api/cases", headers=header)
    assert response.status_code == 200, response.text
    rows = {row["trace_id"]: row for row in response.json()["cases"]}

    occ_row = rows[defect]
    assert occ_row["urgency"] == 1, occ_row
    assert occ_row["first_departure"]["flight_no"] == "AR101", occ_row
    assert occ_row["first_departure"]["std_local"].endswith("T10:30:00"), occ_row
    assert occ_row["impact"]["plans"] == 2 and occ_row["impact"]["route"] == "occ", occ_row
    recommended = next(p for p in cases.get(defect)["options"] if p.get("recommended"))
    rebooked = recommended["validation"]["rebooking"]["rebooked_passengers"]
    assert isinstance(rebooked, int) and occ_row["impact"]["rebooked"] == rebooked, occ_row
    assert occ_row["lane"]["unavailable"]["until"].endswith("13:45:00"), occ_row

    auto_row = rows[delay]
    assert auto_row["urgency"] in {3, 5}, auto_row
    assert auto_row["impact"]["actions_done"] == auto_row["impact"]["actions_total"] >= 1
    assert auto_row["impact"]["authorised_by"] == "policy", auto_row

    handed_row = rows[handed]
    assert handed_row["impact"]["route"] == "auto", handed_row
    assert handed_row["impact"]["authorised_by"] == "occ", handed_row

    malformed_row = rows[malformed["trace_id"]]
    assert malformed_row["urgency"] == 1 and malformed_row["impact"] is None, malformed_row
    assert malformed_row["label"] == "Case could not be read", malformed_row

    failed_row = rows[failed["trace_id"]]
    assert failed_row["urgency"] == 2 and failed_row["impact"]["plans"] == 0, failed_row
    assert failed_row["impact"]["passengers"] is None, failed_row
    assert failed_row["first_departure"]["flight_no"] == "AR131", failed_row

    reading_row = rows[reading["trace_id"]]
    assert reading_row["urgency"] == 4 and reading_row["first_departure"] is None, reading_row
    assert reading_row["lane"] == {"flights": [], "unavailable": None}, reading_row

    ordered = sorted(
        rows.values(),
        key=lambda row: (row["urgency"], (row["first_departure"] or {}).get("std_local") or "~"),
    )
    assert ordered[0]["trace_id"] == defect, [row["trace_id"] for row in ordered]


def check_the_passenger_agent_reports_the_exposure_arrow_prices_plans_by() -> None:
    """Scenario: the Passenger Re-accommodation Agent computes EU261 exposure. ARROW computes it
    from Booking's journeys for the option, with the code that prices every plan: asked at 0
    minutes, as the orchestrator asks, it settles on the delay the plan built from the same
    option needs, and the exposure the agent reports is that plan's compensation. Only the
    Passenger agent's identity may ask; the call is kept on the case under that agent."""

    def builds(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        tools.build_plan(trace, {"aircraft_option_id": "delay"})
        return {"outcome": "no_plan", "summary": "OCC decides", "submitted_plan_id": None}

    operations, cases, _ = operate(ScenarioStore(TRACE), builds)
    opened = operations.open(DEFECT, OPERATOR)
    run, case_id = opened["trace_id"], opened["display_id"]
    operations.run(run)
    [plan] = cases.get(run)["options"]
    passenger = "66666666-7777-8888-9999-aaaaaaaaaaaa"
    app = create_app(
        cases,
        operations,
        frozenset({OPERATOR}),
        frozenset({HOST}),
        "https://arrow.test",
        orchestrator_oids=frozenset({AGENT}),
        passenger_oids=frozenset({passenger}),
    )
    asked = {"case_id": case_id, "option_id": "delay", "additional_delay_minutes": 0}

    def ask(oid: str) -> dict[str, Any]:
        claims = {"claims": [{"typ": "oid", "val": oid}]}
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "get_compensation_exposure", "arguments": asked},
            },
            headers={
                "X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(claims).encode()).decode(),
                "Accept": "application/json, text/event-stream",
                "traceparent": f"00-{run}-{'1' * 16}-01",
            },
        )
        assert response.status_code == 200, response.text
        result: dict[str, Any] = response.json()["result"]
        return result

    with TestClient(app) as client:
        exposure, orchestrator = ask(passenger), ask(AGENT)

    assert not exposure.get("isError"), exposure
    found, cost = exposure["structuredContent"], plan["cost"]
    # CS-TQA's four-hour defect delays SYN001 and SYN004 past 3 h: €2,100 owed (as plan pricing).
    assert (
        (found["compensation_eur"], found["passengers_owed_compensation"])
        == (
            cost["compensation_eur"],
            cost["owed_compensation_passengers"],
        )
        == (2100, 6)
    ), (found, cost)
    assert found["compensation_by_band"] == cost["workings"]["compensation"], found
    assert (found["care_eur"], found["rebooking_eur"]) == (cost["care_eur"], cost["rebooking_eur"])
    assert found["priced_additional_delay_minutes"] == plan["adjustment_minutes"], found
    assert found["extraordinary_circumstances"] is False and "event_id" not in found, found
    assert orchestrator["content"][0]["text"].endswith("not available to the orchestrator")
    [kept] = [e for e in cases.get(run)["events"] if e["event"] == "agent_tool_call"]
    assert (kept["actor"], kept["autonomy"], kept["access"], kept["ok"]) == (
        "passenger",
        "L3",
        "read",
        True,
    ), kept
    assert kept["response"]["compensation_eur"] == 2100, kept


def check_an_approval_in_the_operator_app_runs_only_through_the_workers() -> None:
    # Three container apps spec §4: the operator app records the OCC's decision and hands the
    # plan to the workers with a wake on the execution queue; only the workers' executor writes.
    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        tools.build_plan(trace, {"aircraft_option_id": "delay"})
        return {"outcome": "no_plan", "summary": "OCC decides", "submitted_plan_id": None}

    operations, cases, gateway = operate(ScenarioStore(TRACE), script)  # the workers
    trace = operations.open(DEFECT, OPERATOR)["trace_id"]
    operations.run(trace)
    case = cases.get(trace)
    [plan] = case["options"]
    woken: list[str] = []
    operator_app = create_app(
        cases,
        Operations(cases, gateway),  # the operator app's own process
        frozenset({OPERATOR}),
        frozenset({HOST}),
        "https://arrow.test",
        surfaces=frozenset({"operator"}),
        execute=woken.append,
    )
    decision = {
        "option_id": plan["option_id"],
        "expected_version": case["version"],
        "decision": "approve",
        "idempotency_key": "decision-approve-split-0001",
    }
    listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    with TestClient(operator_app) as client:
        approved = client.post(f"/api/cases/{trace}/approval", json=decision, headers=headers())
        tools = client.post("/mcp", json=listing, headers=headers(AGENT))

    assert approved.status_code == 200, approved.text
    assert tools.status_code in (404, 405), "the operator app serves no agent tools"
    assert woken == [trace]
    assert cases.get(trace)["execution"]["status"] == "approved", "recorded, not carried out"
    assert gateway.store.defect["trace_id"] == TRACE and not gateway.store.actions, "no write"
    # The workers take the wake from the execution queue and carry the plan out.
    intake = Intake(operations, cases, Rejections(), OPERATOR, lambda *_: None)
    wake = Delivery(json.dumps({"trace_id": trace}).encode(), "execute", "m1", 1, None, 1)
    assert intake.handle(wake) == "execute"
    execution = cases.get(trace)["execution"]
    assert execution["status"] == "completed", execution
    assert execution["actions"][0]["kind"] == "record_disruption", execution["actions"][0]
    assert gateway.store.defect["trace_id"] == trace, "Maintenance recorded the case's defect"


def check_an_automatic_plan_from_the_tools_app_runs_only_through_the_workers() -> None:
    # Three container apps spec §4: the orchestrator submits an automatic plan through the tools
    # app, which authorises it in the case and hands it to the workers; it runs there, once.
    woken: list[str] = []

    def call(tool: str, arguments: dict[str, Any], run: str) -> dict[str, Any]:
        claims = {"claims": [{"typ": "oid", "val": AGENT}]}
        response = tools_client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            },
            headers={
                "X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(claims).encode()).decode(),
                "Accept": "application/json, text/event-stream",
                "traceparent": f"00-{run}-{'1' * 16}-01",
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert not result.get("isError"), result
        answer: dict[str, Any] = json.loads(result["content"][0]["text"])
        return answer

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        levers = {"aircraft_option_id": "delay"}
        plan = call("build_plan", {"case_id": brief["case_id"], "levers": levers}, trace)
        reason = "Within every domain's limits"
        result = call(
            "submit_plan",
            {"case_id": brief["case_id"], "plan_id": plan["plan_id"], "reason": reason},
            trace,
        )
        assert result["route"] == "auto", result
        return submitted(plan["plan_id"])

    operations, cases, gateway = operate(ScenarioStore(TRACE), script)  # the workers
    tools_app = create_app(
        cases,
        Operations(cases, gateway),  # the tools app's own process
        frozenset(),
        frozenset(),
        "",
        surfaces=frozenset({"tools"}),
        execute=woken.append,
        orchestrator_oids=frozenset({AGENT}),
    )
    with TestClient(tools_app) as tools_client:
        trace = operations.open(STAFF_DELAY, OPERATOR)["trace_id"]
        operations.run(trace)  # the workers' assessment; the orchestrator calls the tools app
        screen = tools_client.get("/", headers=headers())

    assert screen.status_code == 404, "the tools app serves no operator screen"
    execution = cases.get(trace)["execution"]
    assert (execution["status"], execution["approved_by"]) == ("approved", "policy"), execution
    assert woken == [trace]
    assert gateway.store.delays == {}, "the tools app wrote nothing"
    intake = Intake(operations, cases, Rejections(), OPERATOR, lambda *_: None)
    wake = Delivery(json.dumps({"trace_id": trace}).encode(), "execute", "m1", 1, None, 1)
    assert intake.handle(wake) == "execute"
    assert cases.get(trace)["execution"]["status"] == "completed"
    assert gateway.store.delays[trace]["cause"] == "staff_shortage", gateway.store.delays


class OrchestratedFlowTests(unittest.TestCase):
    def test_an_approval_in_the_operator_app_runs_only_through_the_workers(self) -> None:
        check_an_approval_in_the_operator_app_runs_only_through_the_workers()

    def test_an_automatic_plan_from_the_tools_app_runs_only_through_the_workers(self) -> None:
        check_an_automatic_plan_from_the_tools_app_runs_only_through_the_workers()

    def test_the_passenger_agent_reports_the_exposure_arrow_prices_plans_by(self) -> None:
        check_the_passenger_agent_reports_the_exposure_arrow_prices_plans_by()

    def test_occ_case_ranks_plans_and_waits_for_the_duty_manager(self) -> None:
        check_occ_case_ranks_plans_and_waits_for_the_duty_manager()

    def test_control_tower_lists_urgent_cases_first_with_their_impact(self) -> None:
        check_control_tower_lists_urgent_cases_first_with_their_impact()

    def test_auto_case_executes_without_a_human(self) -> None:
        check_auto_case_executes_without_a_human()

    def test_orchestrator_without_submission_leaves_the_decision_to_occ(self) -> None:
        check_orchestrator_without_submission_leaves_the_decision_to_occ()

    def test_plans_built_at_the_same_time_are_all_kept(self) -> None:
        check_plans_built_at_the_same_time_are_all_kept()

    def test_same_event_reported_twice_opens_one_case(self) -> None:
        check_same_event_reported_twice_opens_one_case()

    def test_options_not_offered_are_refused_and_never_become_plans(self) -> None:
        check_options_not_offered_are_refused_and_never_become_plans()

    def test_mcp_tools_refuse_other_identities(self) -> None:
        check_mcp_tools_refuse_other_identities()

    def test_the_playbook_is_read_by_the_orchestrator_and_the_case_assistant_only(self) -> None:
        check_the_playbook_is_read_by_the_orchestrator_and_the_case_assistant_only()

    def test_orchestrator_acts_only_on_its_own_case_by_case_id(self) -> None:
        check_orchestrator_acts_only_on_its_own_case_by_case_id()

    def test_reserve_used_before_execution_stops_with_a_clear_reason(self) -> None:
        check_reserve_used_before_execution_stops_with_a_clear_reason()

    def test_a_standby_crew_taken_since_an_automatic_plan_stops_it_for_occ(self) -> None:
        check_a_standby_crew_taken_since_an_automatic_plan_stops_it_for_occ()

    def test_two_approved_plans_cannot_overfill_a_lisbon_hour(self) -> None:
        check_two_approved_plans_cannot_overfill_a_lisbon_hour()

    def test_a_slots_outage_while_recording_does_not_block_the_plans(self) -> None:
        check_a_slots_outage_while_recording_does_not_block_the_plans()

    def test_delay_intake_rejects_invalid_minutes(self) -> None:
        check_delay_intake_rejects_invalid_minutes()

    def test_a_flight_just_over_3_h_late_lands_under_3_h_and_owes_no_compensation(
        self,
    ) -> None:
        check_a_flight_just_over_3_h_late_lands_under_3_h_and_owes_no_compensation()
