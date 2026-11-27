"""Free-text disruptions (business UI and free-text spec §3, D94): the Disruption Detection Agent
reads the report and records it through a typed ARROW tool (owner); code checks the reading and
the rest runs as before.

Real intake, tools, planner, policy, store semantics, executor and system services; the
orchestrator and the Detection agent are a script.
"""

from __future__ import annotations

import base64
import json
import unittest
from typing import Any
from unittest import mock

from arrow_scenario import ScenarioStore
from day_store import OperatingDayStore
from fastapi.testclient import TestClient
from test_four_cases import STORM_DAY, StormDayClock, storm_reading, taf
from test_orchestrated_flow import (
    AGENT,
    CASE_ASSISTANT,
    HOST,
    OPERATOR,
    STAFF_DELAY,
    TRACE,
    operate,
    submitted,
)

from airline_systems.operating_day import build_operating_day
from ops_app.app import create_app
from ops_app.tools import ArrowTools, ToolRefused

DETECTION = "55555555-6666-7777-8888-999999999999"
MESSAGE = {
    "type": "message",
    "text": "Ground staff shortage at the gate: AR131 will leave about 20 minutes late.",
}
READING = {
    "kind": "flight_delay",
    "flight_no": "AR131",
    "delay_minutes": 20,
    "cause": "staff_shortage",
    "reading": "AR131 departs 20 minutes late because of a ground staff shortage.",
}


def use(client: TestClient, oid: str, tool: str, arguments: dict[str, Any], run: str) -> Any:
    """One MCP tool call by an agent identity, from a run carrying ``run``'s trace."""
    claims = {"claims": [{"typ": "oid", "val": oid}]}
    response = client.post(
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
    return response.json()["result"]


def check_the_detection_agent_records_the_report_and_no_other_agent_may() -> None:
    """Owner: the Disruption Detection Agent reads the report and records it with the
    typed tool for its kind of disruption, only that kind's fields. ARROW checks every value and
    records it on its own case: no airline system has it, and no event id exists, until a plan
    for it runs. The answer gives the knock-on if nothing changes. The orchestrator and the Case
    Assistant may not record it, and the orchestrator then reads the recorded disruption, and
    the facts the systems are asked by, with the playbook."""
    operations, cases, gateway = operate(ScenarioStore(TRACE), lambda *_: submitted("x"))
    opened = operations.open(MESSAGE, OPERATOR)
    run, case_id = opened["trace_id"], opened["display_id"]
    app = create_app(
        cases,
        operations,
        frozenset({OPERATOR}),
        frozenset({HOST}),
        "https://arrow.test",
        orchestrator_oids=frozenset({AGENT}),
        assistant_oids=frozenset({CASE_ASSISTANT}),
        detection_oids=frozenset({DETECTION}),
    )
    delay = {
        "case_id": case_id,
        "flight_no": "AR131",
        "delay_minutes": 20,
        "cause": "staff_shortage",
        "reading": READING["reading"],
    }
    with TestClient(app) as client:
        orchestrator = use(client, AGENT, "record_flight_delay", delay, run)
        assistant = use(client, CASE_ASSISTANT, "record_flight_delay", delay, run)
        recorded = use(client, DETECTION, "record_flight_delay", delay, run)
        playbook = use(client, AGENT, "get_playbook", {"case_id": case_id}, run)
        not_detection = use(client, DETECTION, "get_playbook", {"case_id": case_id}, run)

    for refused, who in ((orchestrator, "orchestrator"), (assistant, "Case Assistant")):
        assert refused.get("isError"), refused
        assert refused["content"][0]["text"].endswith(f"not available to the {who}"), refused
    assert not recorded.get("isError"), recorded
    result = recorded["structuredContent"]
    case = cases.get(run)
    assert "event_id" not in result and "event_id" not in case["disruption"], result
    assert gateway.store.delays == {}, "operations control has no delay before a plan runs"
    assert result["disruption"] == {
        "type": "flight_delay",
        "flight_no": "AR131",
        "delay_minutes": 20,
        "cause": "staff_shortage",
        "aircraft_reg": "CS-TQA",
    }, result["disruption"]
    # If nothing changes, AR131 leaves 20 min late and CS-TQA carries it into AR132 (+15).
    knock_on = result["knock_on"]
    assert [(f["flight_no"], f["minutes_late"]) for f in knock_on["flights"]] == [
        ("AR131", 20),
        ("AR132", 15),
    ], knock_on
    assert (knock_on["max_minutes_late"], knock_on["aircraft"]) == (20, ["CS-TQA"]), knock_on
    assert run not in json.dumps(result), result
    # The orchestrator reads the recorded disruption with the playbook; never Detection.
    read = json.loads(playbook["content"][0]["text"])
    assert (read["disruption"]["type"], read["disruption"]["flight_no"]) == (
        "flight_delay",
        "AR131",
    ), read["disruption"]
    assert "event_id" not in read["disruption"], read["disruption"]
    assert read["facts"] == {
        "type": "flight_delay",
        "flight_no": "AR131",
        "delay_minutes": 20,
        "cause": "staff_shortage",
    }, read["facts"]
    assert read["disruption"]["knock_on"] == {
        "flights_affected": 2,
        "max_minutes_late": 20,
        "aircraft_affected": 1,
    }, read["disruption"]
    assert read["allowed_aircraft_kinds"] == ["cancel", "delay", "swap"], read
    assert not_detection["content"][0]["text"].endswith("not available to the Detection agent")
    # The case keeps the Detection agent's call, a write, under that agent; refusals of other
    # identities are theirs to explain, not the case's.
    [kept] = [e for e in case["events"] if e["event"] == "agent_tool_call"]
    assert {key: kept[key] for key in ("actor", "autonomy", "tool", "access", "ok")} == {
        "actor": "detection",
        "autonomy": "L4",
        "tool": "record_flight_delay",
        "access": "write",
        "ok": True,
    }, kept
    assert kept["request"] == {k: v for k, v in delay.items() if k != "case_id"}, kept
    assert "event_id" not in kept["response"] and kept["response"]["flights_affected"] == 2
    assert kept["message"].startswith("Recorded flight delay · AR131 · 20 min"), kept


def check_message_is_read_planned_and_runs_automatically() -> None:
    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        # The Detection agent records the report; the orchestrator then reads the playbook.
        recorded = tools.record_disruption(trace, READING)
        assert recorded["disruption"]["aircraft_reg"] == "CS-TQA", recorded
        assert "event_id" not in recorded, recorded
        read = tools.get_playbook(trace)
        assert read["allowed_aircraft_kinds"] == ["cancel", "delay", "swap"], read
        assert read["facts"]["flight_no"] == recorded["disruption"]["flight_no"], read
        plan = tools.build_plan(trace, {"aircraft_option_id": "delay"})
        tools.submit_plan(trace, plan["plan_id"], "Within every domain's limits")
        return submitted(plan["plan_id"])

    store = ScenarioStore(TRACE)
    operations, cases, gateway = operate(store, script)
    trace = operations.open(MESSAGE, OPERATOR)["trace_id"]
    operations.run(trace)

    brief = gateway.briefs[0]
    assert brief["message"] == MESSAGE["text"] and "cause" not in brief, brief
    case = cases.get(trace)
    disruption = case["disruption"]
    assert disruption["type"] == "flight_delay", disruption
    assert disruption["flight_no"] == "AR131" and disruption["delay_minutes"] == 20
    assert disruption["message"] == MESSAGE["text"]
    assert disruption["reading"] == READING["reading"]
    assert case["execution"]["status"] == "completed", case.get("execution")
    assert case["execution"]["approved_by"] == "policy"
    assert store.flights["AR131"]["std_local"].endswith("T14:30:00")
    # The delay the Detection agent read reached operations control only as the plan ran.
    record = case["execution"]["actions"][0]
    assert (record["kind"], record["status"]) == ("record_disruption", "confirmed"), record
    assert store.delays[trace]["flight_no"] == "AR131", store.delays
    events = [event["event"] for event in case["events"]]
    assert events.index("message_received") < events.index("disruption_read"), events


def check_unreadable_message_changes_nothing() -> None:
    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        return {
            "outcome": "unreadable",
            "summary": "The message names no flight or aircraft.",
            "submitted_plan_id": None,
        }

    store = ScenarioStore(TRACE)
    operations, cases, _ = operate(store, script)
    trace = operations.open({"type": "message", "text": "Things are going wrong at LIS"}, OPERATOR)[
        "trace_id"
    ]
    operations.run(trace)

    case = cases.get(trace)
    assert case["status"] == "failed", case["status"]
    assert case["stage"] == (
        "ARROW could not read this message: The message names no flight or aircraft."
    ), case["stage"]
    assert case["options"] == [] and "execution" not in case
    assert case["disruption"]["type"] == "message"
    assert store.get_delay(trace) is None


def check_reading_is_checked_and_cannot_change() -> None:
    outcomes: list[str] = []

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        for wrong in (
            {**READING, "flight_no": "AR9999"},
            {**READING, "delay_minutes": 7},
            {**READING, "kind": "aircraft_defect", "aircraft_reg": "TQA"},
        ):
            try:
                tools.record_disruption(trace, wrong)
            except ToolRefused as refused:
                outcomes.append(str(refused))
        tools.record_disruption(trace, READING)
        tools.record_disruption(trace, READING)  # the same reading again is accepted
        try:
            tools.record_disruption(trace, {**READING, "delay_minutes": 30})
        except ToolRefused as refused:
            outcomes.append(str(refused))
        return {"outcome": "no_plan", "summary": "Stopped for the test", "submitted_plan_id": None}

    operations, cases, _ = operate(ScenarioStore(TRACE), script)
    trace = operations.open(MESSAGE, OPERATOR)["trace_id"]
    operations.run(trace)

    assert len(outcomes) == 4, outcomes
    assert "AR9999" in outcomes[0] and "not in the message" in outcomes[0], outcomes[0]
    assert "delay_minutes" in outcomes[1], outcomes[1]
    assert "aircraft_reg" in outcomes[2], outcomes[2]
    assert "already read" in outcomes[3], outcomes[3]
    assert cases.get(trace)["disruption"]["delay_minutes"] == 20

    # The same weather reading again is accepted too, answered with what the first recorded,
    # although the case now also holds the reduced rate the operations control system planned.
    answers: list[dict[str, Any]] = []

    def twice(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        for _ in range(2):
            try:
                answers.append(tools.record_disruption(trace, storm_reading(STORM_DAY)))
            except ToolRefused as refused:
                answers.append({"refused": str(refused)})
        return {"outcome": "no_plan", "summary": "Stopped for the test", "submitted_plan_id": None}

    with mock.patch("airline_systems.schedule.datetime", StormDayClock):
        store = OperatingDayStore(build_operating_day(STORM_DAY))
        operations, cases, _ = operate(store, twice)
        weather = operations.open({"type": "message", "text": taf(STORM_DAY)}, OPERATOR)
        operations.run(weather["trace_id"])

    first, again = answers
    assert again == first and "event_id" not in first and "refused" not in first, answers
    assert not store.weather, "a forecast is recorded in no airline system"


def check_no_disruption_is_refused_when_schedule_shows_affected_flights() -> None:
    def read_then_deny(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        tools.record_disruption(trace, READING)
        return {"outcome": "no_disruption", "summary": "No report found", "submitted_plan_id": None}

    def deny_unread(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        return {"outcome": "no_disruption", "summary": "Nothing to do", "submitted_plan_id": None}

    for script in (read_then_deny, deny_unread):
        operations, cases, _ = operate(ScenarioStore(TRACE), script)
        trace = operations.open(MESSAGE, OPERATOR)["trace_id"]
        operations.run(trace)

        case = cases.get(trace)
        assert case["status"] == "failed", (script.__name__, case["status"])
        assert case["stage"].startswith("ARROW could not confirm"), case["stage"]


def plan_unless_repeated(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
    reading = {**READING, "delay_minutes": 40} if "40 minutes" in brief["message"] else READING
    recorded = tools.record_disruption(trace, reading)
    if not recorded["recorded"]:
        return {"outcome": "no_plan", "summary": recorded["message"], "submitted_plan_id": None}
    plan = tools.build_plan(trace, {"aircraft_option_id": "delay"})
    tools.submit_plan(trace, plan["plan_id"], "Within every domain's limits")
    return submitted(plan["plan_id"])


def check_a_repeated_report_is_added_to_the_open_case() -> None:
    # An owner test: AR259 +20 sent four times opened four cases, each recording the delay
    # again in Schedule, so the delays stacked.
    store = ScenarioStore(TRACE)
    operations, cases, _ = operate(store, plan_unless_repeated)
    first = operations.open(MESSAGE, OPERATOR)["trace_id"]
    operations.run(first)
    again = operations.open(MESSAGE, OPERATOR, source={"kind": "fabric", "system": "Gate"})
    operations.run(again["trace_id"])

    original, repeat = cases.get(first), cases.get(again["trace_id"])
    assert original["execution"]["status"] == "completed", original.get("execution")
    assert store.flights["AR131"]["std_local"].endswith("T14:30:00")  # delayed once, not twice
    assert store.get_delay(again["trace_id"]) is None
    assert repeat["status"] == "closed", repeat["status"]
    assert repeat["repeat_of"]["display_id"] == original["display_id"], repeat.get("repeat_of")
    assert again["trace_id"] not in [c["trace_id"] for c in cases.list(original["operating_date"])]
    [note] = [e for e in original["events"] if e["event"] == "repeat_report"]
    assert again["display_id"] in note["message"] and "Gate" in note["message"], note


def check_a_report_that_changes_an_open_case_stops_for_occ() -> None:
    # A second report of the same flight with another delay is an update, not a second delay:
    # ARROW does not add it on top; the new case stops with both figures for OCC.
    store = ScenarioStore(TRACE)
    operations, cases, _ = operate(store, plan_unless_repeated)
    first = operations.open(MESSAGE, OPERATOR)["trace_id"]
    operations.run(first)
    update = {"type": "message", "text": "Update: AR131 will now leave 40 minutes late."}
    again = operations.open(update, OPERATOR)["trace_id"]
    operations.run(again)

    original, stopped = cases.get(first), cases.get(again)
    assert store.flights["AR131"]["std_local"].endswith("T14:30:00")  # still the first +20
    assert store.get_delay(again) is None
    assert stopped["status"] == "failed" and "execution" not in stopped, stopped["status"]
    assert stopped["conflicts_with"]["display_id"] == original["display_id"]
    assert "repeat_of" not in stopped  # a Reset must not turn a stopped update into a repeat
    assert original["display_id"] in stopped["stage"], stopped["stage"]
    assert "20 min" in stopped["stage"] and "40 min" in stopped["stage"], stopped["stage"]
    [note] = [e for e in original["events"] if e["event"] == "conflicting_report"]
    assert stopped["display_id"] in note["message"] and "40 min" in note["message"], note

    # The same +20 once more is a repeat of the case that recorded it, not of the stopped update.
    third = operations.open(MESSAGE, OPERATOR)["trace_id"]
    operations.run(third)
    repeat = cases.get(third)
    assert repeat["status"] == "closed", repeat["stage"]
    assert repeat["repeat_of"]["display_id"] == original["display_id"], repeat["repeat_of"]
    assert store.flights["AR131"]["std_local"].endswith("T14:30:00")


def check_a_report_is_recorded_when_its_first_case_failed_before_recording() -> None:
    # Review: a form or Fabric report holds its figures from the start; a case that never
    # recorded them must not absorb a resend.
    def plan(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        built = tools.build_plan(trace, {"aircraft_option_id": "delay"})
        tools.submit_plan(trace, built["plan_id"], "Within every domain's limits")
        return submitted(built["plan_id"])

    store = ScenarioStore(TRACE)
    operations, cases, gateway = operate(store, plan)
    prepare = gateway.prepare
    calls: list[str] = []

    def schedule_down_once(document: dict[str, Any], progress: Any) -> Any:
        calls.append(document["trace_id"])
        if len(calls) == 1:
            raise RuntimeError("Schedule unavailable")
        return prepare(document, progress)

    gateway.prepare = schedule_down_once  # type: ignore[method-assign]
    first = operations.open(STAFF_DELAY, OPERATOR)["trace_id"]
    operations.run(first)
    again = operations.open(STAFF_DELAY, OPERATOR)["trace_id"]
    operations.run(again)

    assert cases.get(first)["status"] == "failed", cases.get(first)["status"]
    resent = cases.get(again)
    assert "repeat_of" not in resent, resent.get("repeat_of")
    assert resent["execution"]["status"] == "completed", resent.get("execution")
    assert store.flights["AR131"]["std_local"].endswith("T14:30:00")


def check_simultaneous_readings_record_only_one() -> None:
    refused: list[str] = []
    retried: list[dict[str, Any]] = []

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        tools.record_disruption(trace, READING)
        return {"outcome": "no_plan", "summary": "Stopped for the test", "submitted_plan_id": None}

    operations, cases, gateway = operate(ScenarioStore(TRACE), script)
    prepare = gateway.prepare
    calls: list[str] = []

    def prepare_while_another_reading_arrives(document: dict[str, Any], progress: Any) -> Any:
        calls.append(document["trace_id"])
        if len(calls) == 1:
            # While the first reading is being recorded: a different reading is refused, and
            # the same reading again (a retry) is accepted without a second, different record.
            assert gateway.tools is not None
            try:
                gateway.tools.record_disruption(
                    document["trace_id"], {**READING, "delay_minutes": 30}
                )
            except ToolRefused as error:
                refused.append(str(error))
            retried.append(gateway.tools.record_disruption(document["trace_id"], READING))
        return prepare(document, progress)

    gateway.prepare = prepare_while_another_reading_arrives  # type: ignore[method-assign]
    trace = operations.open(MESSAGE, OPERATOR)["trace_id"]
    operations.run(trace)

    assert len(refused) == 1 and "already" in refused[0], refused
    assert retried and retried[0]["recorded"].startswith("flight delay · AR131 · 20 min"), retried
    case = cases.get(trace)
    assert case["disruption"]["type"] == "flight_delay"
    assert [e["event"] for e in case["events"]].count("disruption_read") == 1


def check_a_reading_names_only_what_the_message_names() -> None:
    # Coordinator: the reading's flight or aircraft must be written in the message, so a
    # model that infers another flight is refused in code; it may correct itself once.
    refused: list[str] = []

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        try:
            tools.record_disruption(trace, {**READING, "flight_no": "AR132"})
        except ToolRefused as error:
            refused.append(str(error))
        tools.record_disruption(trace, READING)  # "ar 131" in the message is AR131
        plan = tools.build_plan(trace, {"aircraft_option_id": "delay"})
        tools.submit_plan(trace, plan["plan_id"], "Within every domain's limits")
        return submitted(plan["plan_id"])

    store = ScenarioStore(TRACE)
    operations, cases, _ = operate(store, script)
    text = "Ground staff shortage at the gate: ar 131 will leave about 20 minutes late."
    trace = operations.open({"type": "message", "text": text}, OPERATOR)["trace_id"]
    operations.run(trace)

    assert refused == ["Not recorded: flight_no: AR132 is not in the message"], refused
    assert [delay["flight_no"] for delay in store.delays.values()] == ["AR131"], store.delays
    assert cases.get(trace)["execution"]["status"] == "completed"

    def defect(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        reading = {**READING, "kind": "aircraft_defect", "unavailable_minutes": 240}
        try:
            tools.record_disruption(trace, {**reading, "aircraft_reg": "CS-TQB"})
        except ToolRefused as error:
            refused.append(str(error))
        tools.record_disruption(trace, {**reading, "aircraft_reg": "CS-TQA"})
        return {"outcome": "no_plan", "summary": "Stopped for the test", "submitted_plan_id": None}

    operations, cases, _ = operate(ScenarioStore(TRACE), defect)
    text = "Hydraulic leak on cs tqa during the walk-around; out of service about 4 hours."
    trace = operations.open({"type": "message", "text": text}, OPERATOR)["trace_id"]
    operations.run(trace)

    assert refused[1] == "Not recorded: aircraft_reg: CS-TQB is not in the message", refused
    assert cases.get(trace)["disruption"]["scope"]["aircraft_reg"] == "CS-TQA"


class MessageIntakeTests(unittest.TestCase):
    def test_the_detection_agent_records_the_report_and_no_other_agent_may(self) -> None:
        check_the_detection_agent_records_the_report_and_no_other_agent_may()

    def test_a_reading_names_only_what_the_message_names(self) -> None:
        check_a_reading_names_only_what_the_message_names()

    def test_a_repeated_report_is_added_to_the_open_case(self) -> None:
        check_a_repeated_report_is_added_to_the_open_case()

    def test_a_report_that_changes_an_open_case_stops_for_occ(self) -> None:
        check_a_report_that_changes_an_open_case_stops_for_occ()

    def test_a_report_is_recorded_when_its_first_case_failed_before_recording(self) -> None:
        check_a_report_is_recorded_when_its_first_case_failed_before_recording()

    def test_no_disruption_is_refused_when_schedule_shows_affected_flights(self) -> None:
        check_no_disruption_is_refused_when_schedule_shows_affected_flights()

    def test_simultaneous_readings_record_only_one(self) -> None:
        check_simultaneous_readings_record_only_one()

    def test_message_is_read_planned_and_runs_automatically(self) -> None:
        check_message_is_read_planned_and_runs_automatically()

    def test_unreadable_message_changes_nothing(self) -> None:
        check_unreadable_message_changes_nothing()

    def test_reading_is_checked_and_cannot_change(self) -> None:
        check_reading_is_checked_and_cannot_change()
