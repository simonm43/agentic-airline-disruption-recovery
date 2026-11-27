"""Event intake: a disruption CloudEvent from the assessment queue opens a case that runs like an
OCC report (Service Bus intake spec; formerly the Fabric Eventstream).

Real intake, store semantics, policy and executor; the orchestrator is a script (the same one
the orchestrated-flow tests use). The Service Bus client itself is exercised in Dev.
"""

from __future__ import annotations

import base64
import json
import unittest
import uuid
from datetime import UTC, datetime
from typing import Any

from arrow_scenario import ScenarioStore
from fastapi.testclient import TestClient
from test_orchestrated_flow import HOST, OPERATOR, STAFF_DELAY, TRACE, operate, submitted

from airline_systems.schedule import lisbon_today
from ops_app.app import create_app
from ops_app.events import Delivery, Rejections, decode
from ops_app.intake import Intake
from ops_app.tools import ArrowTools

ARROW = "33333333-4444-5555-6666-777777777777"  # ARROW's own identity, the reporter
TESTBENCH = "44444444-5555-6666-7777-888888888888"
ENQUEUED = datetime(2026, 11, 10, 9, 12, 1, tzinfo=UTC)
DELAY_DATA = {key: value for key, value in STAFF_DELAY.items() if key != "type"}


def cloud_event(kind: str, data: dict[str, Any], *, event_id: str | None = None) -> dict[str, Any]:
    # The test bench's CloudEvent (src/testbench/events.py), rebuilt here: this file also runs
    # inside the ops image's test step, which does not ship the test bench package.
    return {
        "specversion": "1.0",
        "id": event_id or str(uuid.uuid4()),
        "source": "arrow-testbench",
        "type": f"com.arrow.disruption.{kind}",
        "time": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "datacontenttype": "application/json",
        "data": dict(data),
    }


def principal(oid: str) -> dict[str, str]:
    claims = {"claims": [{"typ": "oid", "val": oid}]}
    return {"X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(claims).encode()).decode()}


def auto_plan(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
    plan = tools.build_plan(trace, {"aircraft_option_id": "delay"})
    tools.submit_plan(trace, plan["plan_id"], "Within every domain's limits")
    return submitted(plan["plan_id"])


class Stream:
    """ARROW's side of the assessment queue: every message goes through the real intake."""

    def __init__(self) -> None:
        self.operations, self.cases, self.gateway = operate(ScenarioStore(TRACE), auto_plan)
        self.rejections = Rejections()
        # The queue delivers each wake at once: the case is assessed before read() returns.
        self.intake = Intake(self.operations, self.cases, self.rejections, ARROW, self.wake)

    def wake(self, trace: str, at: Any = None) -> None:
        if at is None:
            self.operations.run(trace)

    def read(self, message: Any, *, sequence: int) -> str:
        body = message if isinstance(message, bytes) else json.dumps(message).encode()
        return self.intake.handle(Delivery(body, None, f"m{sequence}", sequence, ENQUEUED, 1))

    def client(self) -> TestClient:
        return TestClient(
            create_app(
                self.cases,
                self.operations,
                frozenset({OPERATOR}),
                frozenset({HOST}),
                "https://arrow.test",
                testbench_oids=frozenset({TESTBENCH}),
                rejections=self.rejections,
            )
        )

    def case_for(self, event_id: str) -> dict[str, Any]:
        found = self.cases.find_by_event(event_id)
        assert found is not None, event_id
        return found


def deliveries(case: dict[str, Any]) -> list[dict[str, Any]]:
    return [event for event in case["events"] if event["event"] == "event_received"]


def check_stream_event_opens_a_case_that_runs_like_a_report() -> None:
    stream = Stream()
    sent = cloud_event("flight_delay", DELAY_DATA)

    assert stream.read(sent, sequence=1) == "new_case"

    case = stream.case_for(sent["id"])
    assert case["source"] == {
        "kind": "event",
        "event_id": sent["id"],
        "event_time": sent["time"],
        "origin": "arrow-testbench",
    }, case["source"]
    assert stream.gateway.briefs[0]["disruption"]["cause"] == "staff_shortage"
    assert case["execution"]["status"] == "completed", case["execution"]
    assert case["execution"]["approved_by"] == "policy"
    # The Test Bench's event says nothing of its source: the plan records the delay first.
    assert case["execution"]["actions"][0]["kind"] == "record_disruption"
    assert stream.gateway.store.delays[case["trace_id"]]["flight_no"] == "AR131"
    # The log says a system event reported it, not a person.
    assert case["events"][0]["actor"] == "system-event", case["events"][0]
    assert case["events"][0]["autonomy"] == "system"
    [delivery] = deliveries(case)
    assert delivery["delivery"] == "new_case", delivery
    assert (delivery["message_id"], delivery["sequence_number"]) == ("m1", 1)
    assert delivery["body"] == sent
    assert delivery["message"] == "Event received from the queue (new case)"


def check_an_event_recorded_at_its_source_is_not_recorded_again() -> None:
    # Owner: an event a system published that says it is already recorded at its source, by
    # carrying that system's own event id, is never recorded again; the case keeps that id.
    stream = Stream()
    sent = cloud_event("flight_delay", {**DELAY_DATA, "source_event_id": "EV-1A2B3C"})

    assert stream.read(sent, sequence=1) == "new_case"

    case = stream.case_for(sent["id"])
    disruption = case["disruption"]
    assert (disruption["event_id"], disruption["recorded_at_source"]) == ("EV-1A2B3C", True)
    assert stream.gateway.briefs[0]["event_id"] == "EV-1A2B3C", stream.gateway.briefs[0]
    execution = case["execution"]
    assert execution["status"] == "completed", execution
    kinds = [action["kind"] for action in execution["actions"]]
    assert "record_disruption" not in kinds and "retime_flights" in kinds, kinds
    assert stream.gateway.store.delays == {}, "operations control records nothing again"


def check_repeated_event_keeps_one_case_and_reread_adds_nothing() -> None:
    stream = Stream()
    sent = cloud_event("flight_delay", DELAY_DATA)

    # A deliberate resend (new message, sequence 2), then a restart re-reading both messages.
    outcomes = [stream.read(sent, sequence=sequence) for sequence in (1, 2, 1, 2)]

    assert outcomes == ["new_case", "repeat", "skipped", "skipped"], outcomes
    assert len(stream.cases.docs) == 1
    assert len(stream.gateway.briefs) == 1
    case = stream.case_for(sent["id"])
    assert [d["delivery"] for d in deliveries(case)] == ["new_case", "repeat"]
    assert deliveries(case)[1]["message"] == "Event received from the queue (repeat)"


def check_foreign_and_invalid_messages_never_stop_the_consumer() -> None:
    stream = Stream()
    invalid = cloud_event("flight_delay", {**DELAY_DATA, "delay_minutes": 7})
    position = {**cloud_event("flight_delay", {}), "type": "com.arrow.position.updated"}
    porto = cloud_event(
        "weather",
        {
            "airport": "LPPR",
            "taf_window": "0116/0118",
            "hazard": "thunderstorm",
            "description": "TAF LPPR TEMPO 0116/0118 +TSRA",
        },
    )
    valid = cloud_event("flight_delay", DELAY_DATA)

    assert decode(b"{not json") is None
    assert decode(b"[" * 100_000) is None  # too deep for json: RecursionError, not a stall
    outcomes = [
        stream.read(b"{not json", sequence=1),
        stream.read(position, sequence=2),
        stream.read(invalid, sequence=3),
        stream.read(porto, sequence=4),
        stream.read(valid, sequence=5),
    ]
    with stream.client() as client:
        rejected = client.get(f"/api/events/{invalid['id']}", headers=principal(TESTBENCH))

    assert outcomes == ["rejected", "rejected", "rejected", "rejected", "new_case"], outcomes
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
    assert "delay_minutes" in rejected.json()["reason"], rejected.json()
    # Review: the Test Bench lists every message that opened no case, and why.
    reasons = {item["event_id"]: item["reason"] for item in stream.rejections.on(lisbon_today())}
    assert reasons[position["id"]] == "Not a disruption event: type 'com.arrow.position.updated'"
    # A storm ARROW cannot plan (another airport than Lisbon) opens no case.
    assert reasons[porto["id"]] == "ARROW plans weather at Lisbon (LPPT) only, not LPPR"
    assert "Not a JSON CloudEvent" in reasons.values(), reasons
    assert len(stream.cases.docs) == 1


def check_test_reset_closes_every_open_case_and_today_lists_only_today() -> None:
    stream = Stream()
    done_event = cloud_event("flight_delay", DELAY_DATA)
    # Another flight: a second report of the same flight would be added to the first case.
    running_event = cloud_event("flight_delay", {**DELAY_DATA, "flight_no": "AR132"})
    stream.read(done_event, sequence=1)
    stream.read(running_event, sequence=2)
    done = stream.case_for(done_event["id"])
    running = stream.case_for(running_event["id"])
    execution = stream.cases.docs[running["trace_id"]].get("execution") or {}
    stream.cases.docs[running["trace_id"]]["execution"] = {**execution, "status": "executing"}
    reported = stream.operations.open(STAFF_DELAY, OPERATOR)  # reported in the ARROW app
    old = stream.operations.open({**STAFF_DELAY, "description": "Yesterday"}, OPERATOR)
    stream.cases.docs[old["trace_id"]].update(operating_date="2026-01-01", status="no_disruption")
    qa = stream.operations.open({**STAFF_DELAY, "description": "Evaluation"}, OPERATOR)
    stream.cases.docs[qa["trace_id"]].update(display_id="QA-ORCH-CASE1-ABCDEF")

    with stream.client() as client:
        before = client.get("/api/cases", headers=principal(OPERATOR))
        reset = client.post("/api/test/close-cases", headers=principal(TESTBENCH))
        after = client.get("/api/cases", headers=principal(OPERATOR))
        closed_case = client.get(f"/api/cases/{done['trace_id']}", headers=principal(OPERATOR))
        refused = client.post("/api/test/close-cases", headers=principal(OPERATOR))

    listed = {case["trace_id"] for case in before.json()["cases"]}
    assert old["trace_id"] not in listed, "Today must not list an earlier day's case"
    assert {done["trace_id"], running["trace_id"], reported["trace_id"]} <= listed, listed
    assert reset.status_code == 200, reset.text
    assert sorted(reset.json()["closed"]) == sorted(
        [done["display_id"], reported["display_id"], old["display_id"]]
    ), reset.json()
    assert reset.json()["skipped"] == [{"display_id": running["display_id"], "reason": "executing"}]
    assert [case["trace_id"] for case in after.json()["cases"]] == [running["trace_id"]]
    assert closed_case.json()["status"] == "closed"
    assert closed_case.json()["events"][-1]["event"] == "closed_by_test_reset"
    assert stream.cases.docs[qa["trace_id"]]["status"] != "closed", "Evaluation cases are evidence"
    assert refused.status_code == 403


def check_event_status_shows_every_message() -> None:
    stream = Stream()
    sent = cloud_event("flight_delay", DELAY_DATA)
    stream.read(sent, sequence=1)
    stream.read(sent, sequence=2)

    with stream.client() as client:
        status = client.get(f"/api/events/{sent['id']}", headers=principal(TESTBENCH))
        as_operator = client.get(f"/api/events/{sent['id']}", headers=principal(OPERATOR))
        unknown = client.get(f"/api/events/{uuid.uuid4()}", headers=principal(TESTBENCH))

    assert status.status_code == 200, status.text
    view = status.json()
    assert view["source"]["event_id"] == sent["id"]
    assert [d["delivery"] for d in view["deliveries"]] == ["new_case", "repeat"]
    assert [d["sequence_number"] for d in view["deliveries"]] == [1, 2]
    assert all(d["body"] == sent and d["delivery_count"] == 1 for d in view["deliveries"])
    assert [plan["route"] for plan in view["plans"]] == ["auto"], view["plans"]
    assert view["plans"][0]["recommended"] is True and view["plans"][0]["cost_eur"] is not None
    assert view["execution"]["status"] == "completed"
    assert view["execution"]["approved_by"] == "policy"
    assert view["execution"]["actions"], view["execution"]
    assert {action["status"] for action in view["execution"]["actions"]} == {"confirmed"}
    assert view["manual_intervention"] is False
    assert not any(key.startswith("_") for key in view), view.keys()
    assert as_operator.status_code == 403
    assert unknown.status_code == 404


def check_message_event_opens_a_case_and_empty_text_is_rejected() -> None:
    stream = Stream()
    message = {
        **cloud_event("flight_delay", {}),
        "type": "com.arrow.disruption.reported",
        "data": {
            "text": "Ground staff shortage at LIS: AR131 about 20 minutes late",
            "system": "Ground handling",
        },
    }
    empty = {**message, "id": str(uuid.uuid4()), "data": {"text": "  "}}

    outcomes = [stream.read(message, sequence=1), stream.read(empty, sequence=2)]

    assert outcomes == ["new_case", "rejected"], outcomes
    case = stream.case_for(message["id"])
    assert case["disruption"]["type"] in {"message", "flight_delay"}, case["disruption"]
    assert (
        case["disruption"].get("message", case["disruption"].get("text"))
        == (message["data"]["text"])
    )
    assert case["source"]["system"] == "Ground handling", case["source"]
    assert "text" in stream.rejections.get(empty["id"])["reason"]


def check_a_typed_report_cannot_take_an_event_id() -> None:
    # Review: the report box accepted an event_id, so a typed report could take the id a
    # later Service Bus event carries; that event would then count as a repeat of the report.
    stream = Stream()
    sent = cloud_event("flight_delay", {**DELAY_DATA, "flight_no": "AR132"})
    write = {**principal(OPERATOR), "X-ARROW-Request": "operator", "Origin": "https://arrow.test"}
    with stream.client() as client:
        typed = client.post(
            "/api/cases", json={**STAFF_DELAY, "event_id": sent["id"]}, headers=write
        )

    assert typed.status_code == 202, typed.text
    assert stream.read(sent, sequence=1) == "new_case"
    assert stream.case_for(sent["id"])["trace_id"] != typed.json()["trace_id"]
    assert stream.cases.get(typed.json()["trace_id"])["event_id"] is None


def check_a_weather_update_never_opens_a_case() -> None:
    # Case summary and held storm spec §2: a weather update (METAR or amended TAF) settles the storm
    # case it matches; it never opens one. With no storm case at that airport and window, or without
    # the rate a confirmed storm must give, it is listed for the Test Bench with the reason.
    stream = Stream()
    today = f"{lisbon_today().day:02d}"
    data = {
        "airport": "LPPT",
        "taf_window": f"{today}16/{today}18",
        "hazard": "thunderstorm",
        "status": "confirmed",
        "rate_per_hour": 24,
        "description": f"METAR LPPT {today}1420Z 21020G32KT 9999 VCTS SCT025CB",
    }
    confirmed = {**cloud_event("weather", data), "type": "com.arrow.weather.update"}
    no_rate = {
        **cloud_event("weather", {k: v for k, v in data.items() if k != "rate_per_hour"}),
        "type": "com.arrow.weather.update",
    }

    outcomes = [stream.read(confirmed, sequence=1), stream.read(no_rate, sequence=2)]

    assert outcomes == ["rejected", "rejected"], outcomes
    assert not stream.cases.docs and not stream.gateway.briefs
    reasons = {item["event_id"]: item["reason"] for item in stream.rejections.on(lisbon_today())}
    assert reasons[confirmed["id"]] == "No weather case waits for this update", reasons
    assert reasons[no_rate["id"]] == (
        "data.rate_per_hour: a confirmed forecast gives its rate (movements an hour)"
    ), reasons


class EventIntakeTests(unittest.TestCase):
    def test_an_event_recorded_at_its_source_is_not_recorded_again(self) -> None:
        check_an_event_recorded_at_its_source_is_not_recorded_again()

    def test_a_weather_update_never_opens_a_case(self) -> None:
        check_a_weather_update_never_opens_a_case()

    def test_a_typed_report_cannot_take_an_event_id(self) -> None:
        check_a_typed_report_cannot_take_an_event_id()

    def test_message_event_opens_a_case_and_empty_text_is_rejected(self) -> None:
        check_message_event_opens_a_case_and_empty_text_is_rejected()

    def test_stream_event_opens_a_case_that_runs_like_a_report(self) -> None:
        check_stream_event_opens_a_case_that_runs_like_a_report()

    def test_repeated_event_keeps_one_case_and_reread_adds_nothing(self) -> None:
        check_repeated_event_keeps_one_case_and_reread_adds_nothing()

    def test_foreign_and_invalid_messages_never_stop_the_consumer(self) -> None:
        check_foreign_and_invalid_messages_never_stop_the_consumer()

    def test_test_reset_closes_every_open_case_and_today_lists_only_today(self) -> None:
        check_test_reset_closes_every_open_case_and_today_lists_only_today()

    def test_event_status_shows_every_message(self) -> None:
        check_event_status_shows_every_message()
