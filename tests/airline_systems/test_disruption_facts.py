"""The airline systems read a disruption by its facts (owner: nothing is written to them before
a plan runs), at their own routes, and answer exactly as they did when the disruption had to be
recorded first.

``event_based_answers.json`` holds the SHA-256 of every answer the five Test Bench cases read at
0 minutes on 22 November 2026, captured from main at 9f5992f: the disruption recorded in its
system, then read by the event id it issued. The same reads by facts must give the same answers.
On 26 November the wording of the assumptions in the options, passenger and slot answers changed
and those digests were re-recorded; every option, figure and count stayed the same.
"""

from __future__ import annotations

import base64
import hashlib
import json
import unittest
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any
from unittest import mock

from day_store import OperatingDayStore
from fastapi.testclient import TestClient
from system_callers import ARROW, role_claims

from airline_systems import event_ids
from airline_systems.app import create_app
from airline_systems.defects import DefectInput, DefectService
from airline_systems.delays import DelayInput, DelayService
from airline_systems.operating_day import build_operating_day
from airline_systems.schedule import LISBON

GOLDEN = json.loads((Path(__file__).parent / "event_based_answers.json").read_text("utf-8"))
OPS = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
# Fields an answer about one record carried that a read by facts has no reason to repeat.
DROP = {"trace_id", "event_id", "assessment_id", "checked_at", "assignments"}


class Golden(datetime):
    """The golden day's demo clock."""

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> Golden:
        moment = datetime(2026, 11, 22, 9, 45, tzinfo=LISBON).astimezone(tz)
        return cls.fromisoformat(moment.isoformat())


def as_arrow() -> dict[str, str]:
    claims = {"claims": [{"typ": "oid", "val": OPS}, *role_claims(ARROW)]}
    return {
        "X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(claims).encode()).decode(),
        "traceparent": f"00-{TRACE}-00f067aa0ba902b7-01",
    }


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items() if key not in DROP}
    if isinstance(value, list):
        return [clean(item) for item in value]
    return value


def digest(value: Any) -> str:
    text = json.dumps(clean(value), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def read_by_facts(facts: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Every read the planner and the agents make for one case, by its facts, as ARROW makes
    them over REST: a readable summary and each answer's SHA-256."""
    store = OperatingDayStore(build_operating_day(Golden.now().date()))
    apps = {
        system: TestClient(
            create_app(store, frozenset({OPS}), system=system, writer_oids=frozenset({OPS}))
        )
        for system in ("schedule", "crew", "booking", "slots")
    }

    def get(system: str, path: str) -> dict[str, Any]:
        response = apps[system].get(path, params=facts, headers=as_arrow())
        assert response.status_code == 200, (system, path, response.text)
        found: dict[str, Any] = response.json()
        return found

    options = get("schedule", "/api/recovery-options")
    disruption = get("schedule", "/api/disruption")
    answers = {"recovery_options": digest(options), "knock_on": digest(disruption["knock_on"])}
    if facts["type"] == "weather":
        answers["dispatch_advisories"] = digest(get("schedule", "/api/dispatch-advisories"))
    rebooking: dict[str, list[int]] = {}
    standby: dict[str, list[str]] = {}
    for option_id in sorted(option["option_id"] for option in options["options"]):
        base = f"/api/recovery-options/{option_id}"
        for domain, system in (("booking", "booking"), ("crew", "crew"), ("slots", "slots")):
            answers[f"{option_id}/impact/{domain}"] = digest(get(system, f"{base}/impact"))
        plan = get("booking", f"{base}/rebooking-plan")
        crews = get("crew", f"{base}/standby-crews")
        answers[f"{option_id}/rebooking_plan"] = digest(plan)
        answers[f"{option_id}/standby_crews"] = digest(crews)
        answers[f"{option_id}/duty_plan"] = digest(get("crew", f"{base}/duty-plan"))
        rebooking[option_id] = [
            plan["disrupted_passengers"],
            plan["rebooked_passengers"],
            plan["unplaced_passengers"],
        ]
        standby[option_id] = [crew["reserve_option_id"] for crew in crews["options"]]
    summary = {
        "options": [
            [o["option_id"], o["kind"], o["title"], len(o["flights"])] for o in options["options"]
        ],
        "knock_on": [
            disruption["knock_on"]["flights_affected"],
            disruption["knock_on"]["max_minutes_late"],
        ],
        "rebooking": rebooking,
        "standby_crews": standby,
    }
    for client in apps.values():
        client.close()
    return summary, answers


def check_the_five_test_bench_cases_read_by_facts_as_they_read_by_event() -> None:
    assert GOLDEN["day"] == "2026-11-22"
    with mock.patch("airline_systems.schedule.datetime", Golden):
        for name, case in GOLDEN["cases"].items():
            summary, answers = read_by_facts(case["facts"])
            # The options and figures first, so a difference reads as one; then every answer.
            assert summary == case["summary"], (name, summary)
            assert answers == case["sha256"], (
                name,
                sorted(key for key in answers if answers[key] != case["sha256"].get(key)),
            )


def check_a_read_takes_the_disruption_as_the_brief_gives_it_and_names_what_is_missing() -> None:
    # An agent may pass the disruption with another type's fields (the brief's aircraft of a
    # delayed flight, say): only its own type's fields are read. A missing one is refused, by name,
    # and recorded as a refused read without any case reference.
    case_1 = GOLDEN["cases"]["case1"]
    with mock.patch("airline_systems.schedule.datetime", Golden):
        store = OperatingDayStore(build_operating_day(Golden.now().date()))
        app = create_app(store, frozenset({OPS}), system="schedule", writer_oids=frozenset({OPS}))
        with TestClient(app) as client:
            extra = client.get(
                "/api/recovery-options",
                params={**case_1["facts"], "aircraft_reg": "CS-TQZ", "unavailable_minutes": 9},
                headers=as_arrow(),
            )
            missing = client.get(
                "/api/recovery-options",
                params={"type": "flight_delay", "flight_no": "AR259", "delay_minutes": 20},
                headers=as_arrow(),
            )
            departed = client.get(
                "/api/recovery-options",
                params={**case_1["facts"], "flight_no": "AR1"},
                headers=as_arrow(),
            )

            # An agent's tool call takes the same disruption, its schema written out (no
            # references); one missing a fact is refused by name and audited like a REST read.
            def mcp(method: str, params: dict[str, Any]) -> dict[str, Any]:
                response = client.post(
                    "/mcp",
                    headers={**as_arrow(), "Accept": "application/json, text/event-stream"},
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                )
                assert response.status_code == 200, response.text
                result: dict[str, Any] = response.json()["result"]
                return result

            listed = {tool["name"]: tool for tool in mcp("tools/list", {})["tools"]}
            schema = listed["get_recovery_options"]["inputSchema"]
            incomplete = {"type": "flight_delay", "flight_no": "AR259", "delay_minutes": 20}
            refused_tool = mcp(
                "tools/call",
                {"name": "get_recovery_options", "arguments": {"disruption": incomplete}},
            )

    assert "$ref" not in json.dumps(schema) and "$defs" not in json.dumps(schema), schema
    assert schema["properties"]["disruption"]["properties"]["cause"]["enum"][0] == "staff_shortage"
    assert refused_tool.get("isError"), refused_tool
    assert "missing cause" in refused_tool["content"][0]["text"], refused_tool
    [audited] = [
        call
        for call in store.calls
        if call["outcome"] == "failed" and call["endpoint"] == "MCP get_recovery_options"
    ]
    assert json.loads(audited["request"]) == incomplete, audited

    assert extra.status_code == 200, extra.text
    assert digest(extra.json()) == case_1["sha256"]["recovery_options"]
    assert missing.status_code == 422, missing.text
    assert "missing cause" in missing.json()["detail"], missing.json()
    assert departed.status_code == 409, departed.text
    [refused, *_] = [call for call in store.calls if call["outcome"] == "failed"]
    assert json.loads(refused["request"]) == {
        "type": "flight_delay",
        "flight_no": "AR259",
        "delay_minutes": "20",
        "additional_delay_minutes": 0,
    }, refused
    assert (
        TRACE not in str(refused["request"]) and refused["endpoint"] == "GET /api/recovery-options"
    )


def check_a_record_sent_again_is_answered_as_recorded_after_the_operation_changed() -> None:
    # ARROW's executor sends a record again when its reply was lost; by then another plan may
    # have cancelled the flight or moved the aircraft's flights. The system answers it as
    # recorded, under the same event id; a different record under that trace is refused.
    store = OperatingDayStore()
    late = DelayInput(
        trace_id=TRACE,
        flight_no="AR259",
        delay_minutes=20,
        cause="border_control",
        description="Border-control queues",
    )
    first = DelayService(store).record(late)
    store.flights["AR259"] = {**store.flights["AR259"], "status": "cancelled"}
    assert DelayService(store).record(late) == first
    assert first["event_id"] == event_ids.event_id(TRACE), first
    try:
        DelayService(store).record(late.model_copy(update={"delay_minutes": 30}))
    except ValueError as error:
        assert "different delay" in str(error), error
    else:
        raise AssertionError("a different delay under the same trace must be refused")

    other = "5" * 32
    registration = store.flights["AR444"]["aircraft_reg"]
    defect = DefectInput(
        trace_id=other,
        aircraft_reg=registration,
        description="Hydraulic leak",
        unavailable_minutes=240,
    )
    recorded = DefectService(store).record(defect)
    for number, flight in store.flights.items():
        if flight["aircraft_reg"] == registration:
            store.flights[number] = {**flight, "aircraft_reg": "CS-TKH"}
    assert DefectService(store).record(defect) == recorded
    assert recorded["event_id"] == event_ids.event_id(other), recorded


class DisruptionFactsTests(unittest.TestCase):
    def test_a_record_sent_again_is_answered_as_recorded_after_the_operation_changed(
        self,
    ) -> None:
        check_a_record_sent_again_is_answered_as_recorded_after_the_operation_changed()

    def test_the_five_test_bench_cases_read_by_facts_as_they_read_by_event(self) -> None:
        check_the_five_test_bench_cases_read_by_facts_as_they_read_by_event()

    def test_a_read_takes_the_disruption_as_the_brief_gives_it_and_names_what_is_missing(
        self,
    ) -> None:
        check_a_read_takes_the_disruption_as_the_brief_gives_it_and_names_what_is_missing()
