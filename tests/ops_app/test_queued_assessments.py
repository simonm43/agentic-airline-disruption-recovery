"""Queued assessments (Service Bus intake spec, ADR 0025): a burst waits in the queue, temporary
failures retry with growing waits, a retry never records twice, and every case still ends visibly.

Real intake, worker loop, Operations, tools, planner and policy; the orchestrator is a script,
Service Bus is an in-memory bus and time is a controlled clock. The Azure SDK adapter itself is
exercised in Dev.
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pyodbc
from arrow_scenario import ScenarioStore
from azure.core.exceptions import ClientAuthenticationError
from fastapi.testclient import TestClient
from test_event_intake import ARROW, DELAY_DATA, TESTBENCH, cloud_event, principal
from test_message_intake import MESSAGE, READING
from test_operator_flow import headers
from test_orchestrated_flow import DEFECT, HOST, OPERATOR, STAFF_DELAY, TRACE, operate, submitted

from airline_systems.schedule import lisbon_today
from ops_app.app import create_app
from ops_app.engine import CloudDependencyError, CloudGateway, Operations
from ops_app.events import Rejections
from ops_app.intake import Delivery, Intake, serve
from ops_app.tools import ArrowTools
from ops_app.workers import start as start_workers

NO_PLAN = {"outcome": "no_plan", "summary": "OCC decides", "submitted_plan_id": None}
BURST = ("AR101", "AR102", "AR131", "AR132", "AR111", "AR151")


def busy(message: str = "Foundry returned HTTP 429", **kwargs: Any) -> CloudDependencyError:
    return CloudDependencyError(message, status_code=429, retryable=True, **kwargs)


class Clock:
    def __init__(self) -> None:
        self.at = datetime.now(UTC)
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            return self.at

    def advance_to(self, moment: datetime) -> None:
        with self._lock:
            self.at = max(self.at, moment)

    def advance(self, seconds: float) -> None:
        self.advance_to(self() + timedelta(seconds=seconds))


class Bus:
    """Service Bus in memory: one queue; a scheduled message waits until the clock reaches it,
    and time only moves on while no worker is busy."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.pending: list[tuple[datetime, int, Delivery]] = []
        self.busy = 0
        self.sequence = 0
        self.lock = threading.Condition()

    def _put(self, body: bytes, kind: str | None, at: datetime | None, count: int = 1) -> None:
        with self.lock:
            self.sequence += 1
            delivery = Delivery(
                body=body,
                kind=kind,
                message_id=f"m{self.sequence}",
                sequence_number=self.sequence,
                enqueued_at=self.clock(),
                delivery_count=count,
            )
            self.pending.append((at or self.clock(), self.sequence, delivery))
            self.lock.notify_all()

    def publish(self, event: dict[str, Any]) -> None:
        self._put(json.dumps(event).encode(), None, None)

    def wake(self, trace: str, at: datetime | None = None) -> None:
        self._put(json.dumps({"trace_id": trace}).encode(), "wake", at)

    # The receiver each worker opens.
    def __call__(self) -> Bus:
        return self

    def __enter__(self) -> Bus:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def receive(self) -> list[Delivery]:
        with self.lock:
            if not self.pending:
                self.lock.wait(0.02)
                return []
            self.pending.sort(key=lambda item: (item[0], item[1]))
            at, _, delivery = self.pending[0]
            if at > self.clock():
                if self.busy:
                    self.lock.wait(0.02)
                    return []
                self.clock.advance_to(at)
            self.pending.pop(0)
            self.busy += 1
            return [delivery]

    def complete(self, delivery: Delivery) -> None:
        with self.lock:
            self.busy -= 1
            self.lock.notify_all()

    def abandon(self, delivery: Delivery) -> None:
        with self.lock:
            self.busy -= 1
        self._put(delivery.body, delivery.kind, None, delivery.delivery_count + 1)

    def idle(self) -> bool:
        with self.lock:
            return not self.pending and self.busy == 0


def queued(
    script: Callable[[ArrowTools, str, dict[str, Any]], dict[str, Any]],
) -> tuple[Operations, Any, Any, Clock, Bus, Intake]:
    operations, cases, gateway = operate(ScenarioStore(TRACE), script)
    clock = Clock()
    operations.now = clock
    operations.rand = lambda: 0.5  # no jitter, so the waits are exact
    bus = Bus(clock)
    operations.waker = bus.wake
    intake = Intake(operations, cases, Rejections(), ARROW, bus.wake)
    return operations, cases, gateway, clock, bus, intake


def drain(bus: Bus, intake: Intake, workers: int = 2) -> None:
    stop = threading.Event()
    threads = [
        threading.Thread(target=serve, args=(bus, intake.handle), kwargs={"stop": stop})
        for _ in range(workers)
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 20
    while not bus.idle() and time.monotonic() < deadline:
        time.sleep(0.02)
    stop.set()
    for thread in threads:
        thread.join(5)
    assert bus.idle(), "the queue did not drain"


def counted(gateway: Any) -> list[str]:
    """Every disruption ARROW records in a source system (Schedule or Maintenance)."""
    recorded: list[str] = []
    prepare = gateway.prepare

    def prepare_once(document: dict[str, Any], progress: Any) -> dict[str, Any]:
        recorded.append(document["trace_id"])
        return prepare(document, progress)

    gateway.prepare = prepare_once
    return recorded


def events_of(case: dict[str, Any], name: str) -> list[dict[str, Any]]:
    return [event for event in case["events"] if event["event"] == name]


def check_a_burst_waits_in_the_queue_and_every_case_is_assessed() -> None:
    lock = threading.Lock()
    seen = {"calls": 0, "running": 0, "peak": 0}

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        with lock:
            seen["calls"] += 1
            call = seen["calls"]
            seen["running"] += 1
            seen["peak"] = max(seen["peak"], seen["running"])
        try:
            time.sleep(0.03)
            if call <= 3:
                raise busy()
            tools.build_plan(trace, {"aircraft_option_id": "delay"})
            return NO_PLAN
        finally:
            with lock:
                seen["running"] -= 1

    _, cases, gateway, _, bus, intake = queued(script)
    recorded = counted(gateway)
    sent = [cloud_event("flight_delay", {**DELAY_DATA, "flight_no": flight}) for flight in BURST]
    for event in sent:
        bus.publish(event)

    drain(bus, intake)

    found = [cases.find_by_event(event["id"]) for event in sent]
    assert all(case is not None for case in found)
    outcomes = {
        case["disruption"]["flight_no"]: (case["status"], case.get("error"))
        for case in found
        if case
    }
    # Every case ends as it would without a burst. The test day has crew only for CS-TQA's
    # rotation, so the AR111 and AR151 plans are blocked by policy, never by a rate limit.
    blocked = ("failed", "Every plan the orchestrator built was blocked by policy")
    assert outcomes == {
        **{flight: ("assessment_ready", None) for flight in BURST[:4]},
        "AR111": blocked,
        "AR151": blocked,
    }, outcomes
    assert seen["peak"] <= 2, seen
    assert seen["calls"] == 9, seen  # six assessments and three retries
    assert sorted(recorded) == sorted(case["trace_id"] for case in found if case), recorded
    retried = [case for case in found if case and events_of(case, "assessment_retry_scheduled")]
    assert len(retried) == 3, [case["display_id"] for case in retried]


def check_retry_waits_grow_and_follow_retry_after() -> None:
    answers: list[Exception | None] = [busy(retry_after=45.0), None]

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        answer = answers.pop(0)
        if answer is not None:
            raise answer
        tools.build_plan(trace, {"aircraft_option_id": "delay"})
        return NO_PLAN

    operations, cases, _, clock, _, _ = queued(script)
    trace = operations.open(STAFF_DELAY, OPERATOR)["trace_id"]

    retry_at = operations.run(trace)

    assert retry_at == clock() + timedelta(seconds=45), (retry_at, clock())
    case = cases.get(trace)
    assert case["status"] == "assessing", case["status"]
    assert case["assessment"]["state"] == "waiting" and case["assessment"]["attempt"] == 2
    assert "attempt 2 of 4" in case["stage"], case["stage"]
    # A wake that arrives early runs nothing and asks to be woken at the same time again.
    assert operations.run(trace) == retry_at
    assert answers == [None]
    clock.advance_to(retry_at)
    assert operations.run(trace) is None
    assert cases.get(trace)["status"] == "assessment_ready"


def check_a_failing_orchestrator_stops_the_case_after_four_attempts() -> None:
    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        raise CloudDependencyError("Foundry returned HTTP 503", status_code=503, retryable=True)

    operations, cases, gateway, clock, _, _ = queued(script)
    recorded = counted(gateway)
    trace = operations.open(STAFF_DELAY, OPERATOR)["trace_id"]
    waits = []
    while (retry_at := operations.run(trace)) is not None:
        waits.append((retry_at - clock()).total_seconds())
        clock.advance_to(retry_at)

    assert waits == [15.0, 30.0, 60.0], waits
    case = cases.get(trace)
    assert case["status"] == "failed", case["status"]
    assert case["error"].startswith("Assessment stopped after 4 attempts"), case["error"]
    assert "HTTP 503" in case["error"], case["error"]
    assert recorded == [trace], "the delay is recorded once, however often it is retried"
    assert "execution" not in case and case["options"] == []

    # Near the 15-minute deadline there is no time for another attempt: it stops at once.
    operations, cases, _, clock, _, _ = queued(script)
    late = operations.open(STAFF_DELAY, OPERATOR)["trace_id"]
    clock.advance(12 * 60)
    assert operations.run(late) is None
    case = cases.get(late)
    assert case["status"] == "failed" and "HTTP 503" in case["error"], case


def check_no_retry_once_plans_exist_or_the_run_may_still_be_working() -> None:
    def builds_then_busy(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        tools.build_plan(trace, {"aircraft_option_id": "delay"})
        raise busy()

    def lost_contact(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        raise CloudDependencyError("Read timed out", retryable=True, may_continue=True)

    def invalid_data(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        raise CloudDependencyError("Schedule returned an invalid network.")

    expected = {
        builds_then_busy: "assessment_ready",  # OCC decides the plan already built
        lost_contact: "failed",
        invalid_data: "failed",
    }
    for script, status in expected.items():
        operations, cases, _, _, _, _ = queued(script)
        trace = operations.open(STAFF_DELAY, OPERATOR)["trace_id"]
        assert operations.run(trace) is None, script.__name__
        case = cases.get(trace)
        assert case["status"] == status, (script.__name__, case["status"])
        assert not events_of(case, "assessment_retry_scheduled"), script.__name__


def check_a_second_delivery_never_runs_a_case_twice() -> None:
    calls: list[str] = []
    operations: Operations | None = None

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        calls.append(trace)
        assert operations is not None
        # The other worker receives a duplicate wake while this attempt is still running.
        assert operations.run(trace) is None
        tools.build_plan(trace, {"aircraft_option_id": "delay"})
        return NO_PLAN

    operations, cases, _, _, bus, intake = queued(script)
    event = cloud_event("flight_delay", DELAY_DATA)
    body = json.dumps(event).encode()
    first = Delivery(body, None, "m1", 1, datetime.now(UTC), 1)
    assert intake.handle(first) == "new_case"
    # The intake opened the case at once; OCC sees it queued before any worker is free.
    case = cases.find_by_event(event["id"])
    assert case is not None and case["assessment"]["state"] == "queued", case
    # The same message delivered again after its lock was lost, then a later resend.
    intake.handle(Delivery(body, None, "m1", 1, datetime.now(UTC), 2))
    intake.handle(Delivery(body, None, "m2", 2, datetime.now(UTC), 1))
    drain(bus, intake)  # three wakes for one case: it is assessed once

    case = cases.find_by_event(event["id"])
    assert case is not None and case["status"] == "assessment_ready", case
    assert len(calls) == 1, calls
    deliveries = events_of(case, "event_received")
    assert [d["delivery"] for d in deliveries] == ["new_case", "repeat"], deliveries
    assert deliveries[0]["message_id"] == "m1" and deliveries[0]["delivery_count"] == 1


def check_the_sweeper_wakes_a_case_whose_wake_was_lost() -> None:
    operations, cases, _, clock, _, _ = queued(lambda tools, trace, brief: NO_PLAN)
    woken: list[str] = []
    operations.waker = lambda trace, at=None: woken.append(trace)
    lost = operations.open(STAFF_DELAY, OPERATOR)["trace_id"]
    fresh_report = {**STAFF_DELAY, "flight_no": "AR132"}

    clock.advance(90)
    fresh = operations.open(fresh_report, OPERATOR)["trace_id"]
    assert operations.sweep() == 2
    assert woken == [lost], woken  # the fresh case still has its own wake on the way
    clock.advance(90)
    woken.clear()
    operations.sweep()
    assert sorted(woken) == sorted([lost, fresh]), woken
    for trace in (lost, fresh):
        assert cases.get(trace)["status"] == "assessing"


def check_two_reports_of_one_flight_at_once_record_it_once() -> None:
    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        tools.build_plan(trace, {"aircraft_option_id": "delay"})
        return NO_PLAN

    _, cases, gateway, _, bus, intake = queued(script)
    recorded = counted(gateway)
    prepare = gateway.prepare

    def slow(document: dict[str, Any], progress: Any) -> dict[str, Any]:
        time.sleep(0.1)  # the second report arrives while the first is being recorded
        return prepare(document, progress)

    gateway.prepare = slow
    events = [cloud_event("flight_delay", DELAY_DATA) for _ in range(2)]
    threads = [
        threading.Thread(
            target=intake.handle,
            args=(Delivery(json.dumps(event).encode(), None, f"m{n}", n, datetime.now(UTC), 1),),
        )
        for n, event in enumerate(events, start=1)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    drain(bus, intake)  # two workers take the two wakes at the same time

    found = [cases.find_by_event(event["id"]) for event in events]
    assert len(recorded) == 1, recorded
    held = [case for case in found if case and case.get("recorded_at")]
    repeats = [case for case in found if case and case.get("repeat_of")]
    assert len(held) == 1 and len(repeats) == 1, [case and case["status"] for case in found]
    assert repeats[0]["status"] == "closed"


def check_two_reports_of_one_flight_in_two_apps_record_it_once() -> None:
    # Three container apps spec §5: the workers record a typed event while the tools app records
    # the Detection agent's reading of a message about the same delay. Two processes, each with
    # its own Operations, one database: the recording lock lives in the database.
    _, cases, gateway, _, bus, intake = queued(lambda tools, trace, brief: NO_PLAN)
    tools_app = Operations(cases, gateway)
    recorded = counted(gateway)
    prepare = gateway.prepare

    def slow(document: dict[str, Any], progress: Any) -> dict[str, Any]:
        time.sleep(0.1)  # the other app's report arrives while this one is being recorded
        return prepare(document, progress)

    gateway.prepare = slow
    message = intake.operations.open(MESSAGE, OPERATOR)["trace_id"]
    event = cloud_event("flight_delay", DELAY_DATA)
    intake.handle(Delivery(json.dumps(event).encode(), None, "m1", 1, datetime.now(UTC), 1))
    readings: list[dict[str, Any]] = []
    reading = threading.Thread(
        target=lambda: readings.append(tools_app.record_reading(message, READING))
    )
    reading.start()
    drain(bus, intake)  # a worker records the typed event at the same time
    reading.join(5)

    found = [cases.find_by_event(event["id"]), cases.get(message)]
    assert len(recorded) == 1, recorded
    held = [case for case in found if case and case.get("recorded_at")]
    repeats = [case for case in found if case and case.get("repeat_of")]
    assert len(held) == 1 and len(repeats) == 1, [case and case["status"] for case in found]
    assert len(readings) == 1, "the Detection agent's tool call returned"


def check_a_free_text_retry_finishes_the_same_reading() -> None:
    attempts: list[dict[str, Any]] = []

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        if not attempts:
            attempts.append(brief)
            try:
                tools.record_disruption(trace, READING)
            except CloudDependencyError:
                pass  # the agent sees the tool fail; the run then ends with a dependency error
            raise CloudDependencyError("Foundry returned HTTP 424", status_code=424, retryable=True)
        attempts.append(brief)
        # This time the model reads the message differently; the case keeps the first reading.
        other = {**READING, "delay_minutes": 30}
        try:
            tools.record_disruption(trace, other)
        except Exception as error:
            assert "already read as" in str(error), error
        tools.build_plan(trace, {"aircraft_option_id": "delay"})
        return NO_PLAN

    operations, cases, gateway, clock, _, _ = queued(script)
    sent: list[dict[str, Any]] = []
    prepare = gateway.prepare

    def flaky(document: dict[str, Any], progress: Any) -> dict[str, Any]:
        sent.append(dict(document["disruption"]))
        result = prepare(document, progress)
        if len(sent) == 1:  # Schedule recorded it, but the answer never reached ARROW
            raise CloudDependencyError("Read timed out", retryable=True, may_continue=True)
        return result

    gateway.prepare = flaky
    trace = operations.open(MESSAGE, OPERATOR)["trace_id"]
    retry_at = operations.run(trace)
    assert retry_at is not None, cases.get(trace)["stage"]
    clock.advance_to(retry_at)
    assert operations.run(trace) is None

    case = cases.get(trace)
    assert case["status"] == "assessment_ready", case["stage"]
    assert case["disruption"]["delay_minutes"] == 20, case["disruption"]
    assert len(sent) == 2 and sent[0] == sent[1], sent


def check_a_report_after_a_stopped_recorded_case_is_assessed_without_recording_twice() -> None:
    # Review: a Foundry 504 stopped a case after it had recorded its delay; sending the
    # same report again from the Test Bench only joined the stopped case, a dead end until Reset.
    runs: list[str] = []

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        runs.append(trace)
        if len(runs) == 1:
            raise CloudDependencyError(
                "Foundry returned HTTP 504", status_code=504, retryable=True, may_continue=True
            )
        plan = tools.build_plan(trace, {"aircraft_option_id": "delay"})
        tools.submit_plan(trace, plan["plan_id"], "Within every domain's limits")
        return submitted(plan["plan_id"])

    _, cases, gateway, _, bus, intake = queued(script)
    recorded = counted(gateway)
    first = cloud_event("flight_delay", DELAY_DATA)
    bus.publish(first)
    drain(bus, intake)
    stopped = cases.find_by_event(first["id"])
    assert stopped is not None and stopped["status"] == "failed", stopped
    assert stopped.get("recorded_at"), "the delay was recorded before the orchestrator failed"

    # Other figures still stop for OCC with both; the same figures assess the case again.
    update = cloud_event("flight_delay", {**DELAY_DATA, "delay_minutes": 40})
    again = cloud_event("flight_delay", DELAY_DATA)
    for event in (update, again):
        bus.publish(event)
        drain(bus, intake)

    original = cases.get(stopped["trace_id"])
    conflict, resent = cases.find_by_event(update["id"]), cases.find_by_event(again["id"])
    assert conflict is not None and resent is not None
    assert conflict["status"] == "failed" and "execution" not in conflict, conflict["status"]
    assert "20 min" in conflict["stage"] and "40 min" in conflict["stage"], conflict["stage"]
    assert resent["status"] == "closed", resent["stage"]
    assert resent["repeat_of"]["trace_id"] == original["trace_id"], resent.get("repeat_of")
    assert original["execution"]["status"] == "completed", original.get("execution")
    assert runs == [original["trace_id"]] * 2, runs
    assert recorded == [original["trace_id"]], "the delay is recorded once"
    assert gateway.store.flights["AR131"]["std_local"].endswith("T14:30:00")  # +20 once
    assert events_of(original, "assessment_reopened"), [e["event"] for e in original["events"]]


def check_a_token_or_database_blip_is_retried_not_a_stop() -> None:
    # Review: an identity error before a call was sent, or ARROW's database dropping the
    # connection while the case was prepared, stopped the case instead of retrying it.
    class NoToken:
        def get_token(self, *scopes: str, **kwargs: Any) -> Any:
            raise ClientAuthenticationError("Managed identity endpoint not yet available")

    cloud = CloudGateway(
        NoToken(),  # type: ignore[arg-type]
        "systems",
        "https://schedule",
        "https://maintenance",
        "https://foundry",
        booking_url="https://booking",
    )
    try:
        cloud.network(TRACE)
    except CloudDependencyError as error:
        assert error.retryable and not error.may_continue, vars(error)
    else:
        raise AssertionError("a missing token must fail the call")

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        tools.build_plan(trace, {"aircraft_option_id": "delay"})
        return NO_PLAN

    operations, cases, gateway, clock, _, _ = queued(script)
    trace = operations.open(STAFF_DELAY, OPERATOR)["trace_id"]
    change = cases.change
    dropped: list[str] = []

    def drop_once(trace: str, values: dict[str, Any], event: dict[str, Any]) -> None:
        if not dropped:
            dropped.append(trace)  # the systems have answered; saving the case fails
            raise pyodbc.OperationalError("08S01", "[08S01] Communication link failure")
        change(trace, values, event)

    cases.change = drop_once  # type: ignore[method-assign]
    retry_at = operations.run(trace)
    assert retry_at is not None, cases.get(trace)["stage"]
    clock.advance_to(retry_at)
    assert operations.run(trace) is None

    case = cases.get(trace)
    assert case["status"] == "assessment_ready", case["stage"]
    # Read again on the retry; nothing was recorded in operations control before a plan runs.
    assert gateway.store.delays == {}, gateway.store.delays


def check_the_report_box_queues_and_a_failed_send_stops_the_case() -> None:
    operations, cases, gateway, _, _, _ = queued(lambda tools, trace, brief: NO_PLAN)
    woken: list[str] = []
    operations.waker = lambda trace, at=None: woken.append(trace)
    app = create_app(
        cases, operations, frozenset({OPERATOR}), frozenset({HOST}), "https://arrow.test"
    )
    with TestClient(app) as client:
        queued_case = client.post("/api/cases", json=STAFF_DELAY, headers=headers())

        def broken(trace: str, at: datetime | None = None) -> None:
            raise ConnectionError("Service Bus unreachable")

        operations.waker = broken
        refused = client.post(
            "/api/cases",
            json={**STAFF_DELAY, "flight_no": "AR132"},
            headers=headers(),
        )

    assert queued_case.status_code == 202, queued_case.text
    assert woken == [queued_case.json()["trace_id"]]
    assert gateway.briefs == [], "a typed report is queued, not assessed inline"
    assert cases.get(woken[0])["status"] == "assessing"
    assert refused.status_code == 503, refused.text
    [stopped] = [c for c in cases.docs.values() if c["disruption"].get("flight_no") == "AR132"]
    assert stopped["status"] == "failed", stopped["status"]
    assert "could not queue" in stopped["stage"], stopped["stage"]


def check_an_approval_whose_wake_was_lost_still_runs() -> None:
    # Three container apps spec §4: the OCC's approval is recorded even when its wake to the
    # workers cannot be sent; the workers' periodic wake makes their supervisor carry it out.
    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        tools.build_plan(trace, {"aircraft_option_id": "delay"})
        return NO_PLAN

    operations, cases, gateway, _, bus, _ = queued(script)
    trace = operations.open(DEFECT, OPERATOR)["trace_id"]
    operations.run(trace)
    case = cases.get(trace)
    [plan] = case["options"]

    def unreachable(trace: str) -> None:
        raise ConnectionError("Service Bus unreachable")

    queues = SimpleNamespace(
        events=bus,
        receiver=bus,
        execution_receiver=bus,
        dead_letters=bus,
        dead_events=bus,
        wake=bus.wake,
    )
    runtime = start_workers(
        operations,
        cases,
        Rejections(),
        queues,
        workers=1,
        reporter_oid=ARROW,
        backstop_seconds=0.05,
    )
    try:
        time.sleep(0.3)  # the supervisor has looked once and found nothing to carry out
        operator_app = create_app(
            cases,
            Operations(cases, gateway),
            frozenset({OPERATOR}),
            frozenset({HOST}),
            "https://arrow.test",
            surfaces=frozenset({"operator"}),
            execute=unreachable,
        )
        decision = {
            "option_id": plan["option_id"],
            "expected_version": case["version"],
            "decision": "approve",
            "idempotency_key": "decision-approve-lost-0001",
        }
        with TestClient(operator_app) as client:
            approved = client.post(f"/api/cases/{trace}/approval", json=decision, headers=headers())
        assert approved.status_code == 200, approved.text
        deadline = time.monotonic() + 5
        while cases.get(trace)["execution"]["status"] != "completed":
            assert time.monotonic() < deadline, cases.get(trace)["execution"]
            time.sleep(0.05)
    finally:
        runtime.stop()
    assert gateway.store.defect["trace_id"] == trace, "Maintenance recorded the case's defect"


def check_reset_stops_old_events_and_the_test_bench_lists_every_event() -> None:
    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        tools.build_plan(trace, {"aircraft_option_id": "delay"})
        return NO_PLAN

    operations, cases, _, _, _, intake = queued(script)
    rejections = intake.rejections
    app = create_app(
        cases,
        operations,
        frozenset({OPERATOR}),
        frozenset({HOST}),
        "https://a.test",
        testbench_oids=frozenset({TESTBENCH}),
        rejections=rejections,
    )

    def deliver(event: dict[str, Any], number: int) -> None:
        body = json.dumps(event).encode()
        intake.handle(Delivery(body, None, f"m{number}", number, datetime.now(UTC), 1))

    before = cloud_event("flight_delay", DELAY_DATA)
    deliver(before, 1)
    queued_before_reset = cloud_event("flight_delay", {**DELAY_DATA, "flight_no": "AR132"})
    queued_before_reset["time"] = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    invalid = cloud_event("flight_delay", {**DELAY_DATA, "delay_minutes": 7})
    with TestClient(app) as client:
        reset = client.post("/api/test/close-cases", headers=principal(TESTBENCH))
        deliver(queued_before_reset, 2)
        deliver(invalid, 3)
        day = lisbon_today().isoformat()
        listed = client.get(f"/api/events?day={day}", headers=principal(TESTBENCH))
        as_operator = client.get(f"/api/events?day={day}", headers=principal(OPERATOR))

    assert reset.status_code == 200, reset.text
    assert cases.find_by_event(queued_before_reset["id"]) is None
    old = rejections.get(queued_before_reset["id"])
    assert old is not None and "before the last Reset" in old["reason"], old
    assert listed.status_code == 200, listed.text
    items = {item["event_id"]: item for item in listed.json()["events"]}
    assert set(items) == {before["id"], queued_before_reset["id"], invalid["id"]}, items
    assert items[before["id"]]["case"]["status"] == "closed"
    assert items[before["id"]]["envelope"] == before
    assert items[invalid["id"]]["rejected"]["status"] == "rejected"
    assert items[invalid["id"]]["envelope"] == invalid
    assert as_operator.status_code == 403


def check_a_plan_built_while_waiting_goes_to_occ_without_a_second_run() -> None:
    calls: list[str] = []
    late: list[ArrowTools] = []

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        calls.append(trace)
        late.append(tools)
        raise CloudDependencyError("Foundry returned HTTP 503", status_code=503, retryable=True)

    operations, cases, _, clock, _, _ = queued(script)
    trace = operations.open(STAFF_DELAY, OPERATOR)["trace_id"]
    retry_at = operations.run(trace)
    assert retry_at is not None
    # The first run's late tool call lands while the case waits for its retry.
    late[0].build_plan(trace, {"aircraft_option_id": "delay"})
    clock.advance_to(retry_at)

    assert operations.run(trace) is None
    assert calls == [trace], "a case holding a plan never starts a second orchestrator run"
    case = cases.get(trace)
    assert case["status"] == "assessment_ready", case["stage"]


def check_a_gateway_timeout_from_foundry_is_not_retried() -> None:
    # A 502 or 504 from the gateway may leave the Foundry run working behind it: never retried.
    cloud = CloudGateway(
        object(),  # type: ignore[arg-type]
        "systems",
        "https://schedule",
        "https://maintenance",
        "https://foundry",
        booking_url="https://booking",
    )
    for code, may_continue in ((502, True), (504, True), (503, False), (429, False)):
        error = CloudDependencyError(f"HTTP {code}", status_code=code, retryable=True)
        with patch.object(cloud, "request", side_effect=error):
            try:
                cloud.orchestrate(TRACE, {})
            except CloudDependencyError as raised:
                assert raised.may_continue is may_continue, code
            else:
                raise AssertionError(f"HTTP {code} was not raised")


def check_a_domain_agent_failure_in_foundry_is_retried() -> None:
    """Case 2 live: a domain agent's model call failed inside Foundry (HTTP 500), so
    Foundry ended the orchestrator run with HTTP 400 tool_user_error, where it used to answer
    424. The run is over and may be tried again; any other 400 is final."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from types import SimpleNamespace

    answers = [
        {"error": {"code": "tool_user_error", "message": "Tool call failed: crew agent"}},
        {"error": {"code": "invalid_request_error", "message": "Bad input"}},
    ]

    class Refuse(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = json.dumps(answers.pop(0)).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    class Token:
        def get_token(self, *scopes: str, **kwargs: Any) -> Any:
            return SimpleNamespace(token="t")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Refuse)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        cloud = CloudGateway(
            Token(),  # type: ignore[arg-type]
            "systems",
            "https://schedule",
            "https://maintenance",
            f"http://127.0.0.1:{server.server_port}",
            booking_url="https://booking",
        )
        seen: list[CloudDependencyError] = []
        for _ in range(2):
            try:
                cloud.orchestrate(TRACE, {}, timeout=5.0)
            except CloudDependencyError as error:
                seen.append(error)
        tool_failure, bad_input = seen
        assert tool_failure.retryable and not tool_failure.may_continue, vars(tool_failure)
        assert tool_failure.detail == "tool_user_error: Tool call failed: crew agent", vars(
            tool_failure
        )
        assert not bad_input.retryable, vars(bad_input)
    finally:
        server.shutdown()


def check_a_foundry_call_that_never_finishes_frees_its_worker() -> None:
    """Case 4 on ops 55: Foundry's run ended after a 31.8 s agent call, but its answer
    never finished arriving, so the worker waited past its 600 s limit until the case's
    15-minute window stopped the case (and the worker stayed blocked). A per-read timeout never
    fires while bytes keep trickling; the whole call must end on time."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from types import SimpleNamespace

    class Trickle(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for _ in range(40):
                    self.wfile.write(b"1\r\n \r\n")
                    self.wfile.flush()
                    time.sleep(0.1)
            except OSError:
                pass  # the client hung up, as it should

        def log_message(self, *args: Any) -> None:
            pass

    class Token:
        def get_token(self, *scopes: str, **kwargs: Any) -> Any:
            return SimpleNamespace(token="t")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Trickle)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        cloud = CloudGateway(
            Token(),  # type: ignore[arg-type]
            "systems",
            "https://schedule",
            "https://maintenance",
            f"http://127.0.0.1:{server.server_port}",
            booking_url="https://booking",
        )
        started = time.monotonic()
        try:
            cloud.orchestrate(TRACE, {}, timeout=1.0)
        except CloudDependencyError as error:
            assert error.may_continue, vars(error)
        else:
            raise AssertionError("a call that never finishes must fail")
        assert time.monotonic() - started < 2.5, time.monotonic() - started
    finally:
        server.shutdown()


def check_a_rate_limited_reading_is_retried_not_refused() -> None:
    answers: list[Exception | None] = [
        CloudDependencyError("HTTP 503", status_code=503, retryable=True),
        CloudDependencyError("HTTP 429", status_code=429, retryable=True, retry_after=20.0),
        None,
    ]

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        try:
            tools.record_disruption(trace, READING)
        except CloudDependencyError:
            raise CloudDependencyError("HTTP 424", status_code=424, retryable=True) from None
        tools.build_plan(trace, {"aircraft_option_id": "delay"})
        return NO_PLAN

    operations, cases, gateway, clock, _, _ = queued(script)
    prepare = gateway.prepare

    def flaky(document: dict[str, Any], progress: Any) -> dict[str, Any]:
        answer = answers.pop(0)
        if answer is not None:
            raise answer
        return prepare(document, progress)

    gateway.prepare = flaky
    trace = operations.open(MESSAGE, OPERATOR)["trace_id"]
    first = operations.run(trace)
    assert first is not None
    clock.advance_to(first)
    second = operations.run(trace)  # finishing the claimed reading is rate-limited: retry
    assert second is not None, cases.get(trace)["stage"]
    clock.advance_to(second)
    assert operations.run(trace) is None
    case = cases.get(trace)
    assert case["status"] == "assessment_ready", case["stage"]
    assert case["disruption"]["delay_minutes"] == 20


def check_a_failing_handler_waits_before_its_message_returns() -> None:
    # The serverless database waking up: a few failures in a row must not use up every delivery
    # in a moment and dead-letter the event.
    stop = threading.Event()
    waits: list[float] = []
    seen: list[int] = []

    class Once:
        def __init__(self) -> None:
            self.items = [Delivery(b"{}", None, "m1", 1, datetime.now(UTC), 1)]
            self.settled: list[str] = []

        def __enter__(self) -> Once:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def receive(self) -> list[Delivery]:
            if not self.items:
                stop.set()
                return []
            return [self.items.pop(0)]

        def complete(self, delivery: Delivery) -> None:
            self.settled.append("complete")

        def abandon(self, delivery: Delivery) -> None:
            self.settled.append("abandon")
            self.items.append(delivery)

    receiver = Once()

    def handle(delivery: Delivery) -> None:
        seen.append(1)
        if len(seen) <= 3:
            raise ConnectionError("database is waking up")

    serve(lambda: receiver, handle, stop=stop, sleep=waits.append)

    assert receiver.settled == ["abandon", "abandon", "abandon", "complete"], receiver.settled
    assert waits == [5.0, 10.0, 20.0], waits


class QueuedAssessmentTests(unittest.TestCase):
    def test_a_burst_waits_in_the_queue_and_every_case_is_assessed(self) -> None:
        check_a_burst_waits_in_the_queue_and_every_case_is_assessed()

    def test_retry_waits_grow_and_follow_retry_after(self) -> None:
        check_retry_waits_grow_and_follow_retry_after()

    def test_a_failing_orchestrator_stops_the_case_after_four_attempts(self) -> None:
        check_a_failing_orchestrator_stops_the_case_after_four_attempts()

    def test_no_retry_once_plans_exist_or_the_run_may_still_be_working(self) -> None:
        check_no_retry_once_plans_exist_or_the_run_may_still_be_working()

    def test_a_second_delivery_never_runs_a_case_twice(self) -> None:
        check_a_second_delivery_never_runs_a_case_twice()

    def test_the_sweeper_wakes_a_case_whose_wake_was_lost(self) -> None:
        check_the_sweeper_wakes_a_case_whose_wake_was_lost()

    def test_two_reports_of_one_flight_at_once_record_it_once(self) -> None:
        check_two_reports_of_one_flight_at_once_record_it_once()

    def test_two_reports_of_one_flight_in_two_apps_record_it_once(self) -> None:
        check_two_reports_of_one_flight_in_two_apps_record_it_once()

    def test_a_free_text_retry_finishes_the_same_reading(self) -> None:
        check_a_free_text_retry_finishes_the_same_reading()

    def test_a_report_after_a_stopped_recorded_case_is_assessed_without_recording_twice(
        self,
    ) -> None:
        check_a_report_after_a_stopped_recorded_case_is_assessed_without_recording_twice()

    def test_a_token_or_database_blip_is_retried_not_a_stop(self) -> None:
        check_a_token_or_database_blip_is_retried_not_a_stop()

    def test_the_report_box_queues_and_a_failed_send_stops_the_case(self) -> None:
        check_the_report_box_queues_and_a_failed_send_stops_the_case()

    def test_reset_stops_old_events_and_the_test_bench_lists_every_event(self) -> None:
        check_reset_stops_old_events_and_the_test_bench_lists_every_event()

    def test_an_approval_whose_wake_was_lost_still_runs(self) -> None:
        check_an_approval_whose_wake_was_lost_still_runs()

    def test_a_plan_built_while_waiting_goes_to_occ_without_a_second_run(self) -> None:
        check_a_plan_built_while_waiting_goes_to_occ_without_a_second_run()

    def test_a_gateway_timeout_from_foundry_is_not_retried(self) -> None:
        check_a_gateway_timeout_from_foundry_is_not_retried()

    def test_a_domain_agent_failure_in_foundry_is_retried(self) -> None:
        check_a_domain_agent_failure_in_foundry_is_retried()

    def test_a_foundry_call_that_never_finishes_frees_its_worker(self) -> None:
        check_a_foundry_call_that_never_finishes_frees_its_worker()

    def test_a_rate_limited_reading_is_retried_not_refused(self) -> None:
        check_a_rate_limited_reading_is_retried_not_refused()

    def test_a_failing_handler_waits_before_its_message_returns(self) -> None:
        check_a_failing_handler_waits_before_its_message_returns()
