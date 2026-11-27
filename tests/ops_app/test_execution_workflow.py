"""ARROW acts: business workflows from a validated option to receipts in the mocked systems.

Real policy, planner, executor and mocked-system write code; only HTTP is replaced by direct calls
to each recipient's own route.
"""

from __future__ import annotations

import unittest
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from arrow_scenario import CaseMemory, ScenarioStore

from airline_systems import event_ids, facts
from airline_systems.actions import ActionService
from airline_systems.defects import recovery_options
from airline_systems.native import NativeRoutes
from airline_systems.recovery import (
    DutyPlanService,
    RebookingService,
    RecoveryAssessmentService,
)
from airline_systems.reserves import ReserveService
from airline_systems.schedule import ScheduleService
from airline_systems.weather import dispatch_advisories
from ops_app.engine import CloudDependencyError, Operations
from ops_app.executor import Executor, authorise_execution
from ops_app.planner import CaseRef
from ops_app.policy import RELEASE_LIMITS, classify
from ops_app.validator import validate

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
# The scenario's defect as ARROW asks the systems about it: by its facts, never its case.
DEFECT_FACTS = {"type": "aircraft_defect", "aircraft_reg": "CS-TQA", "unavailable_minutes": 240}


def _matches(key: str, system: str, path: str) -> bool:
    return key == system or path.startswith(key)


class SystemsGateway:
    """The airline systems in-process at their own routes, answered as over HTTP: a reply of 400
    or more raises as CloudGateway does.

    Faults, keyed by system or path: ``unavailable`` (a system cannot be reached),
    ``crash_after`` (the container recycles as ARROW sends its next write after that many),
    ``lose_response_once`` (the call is applied but its reply is lost once), ``no_token_once``
    (ARROW gets no token once, so nothing is sent), ``hold`` (the counterparty has not answered
    yet) and ``after_call`` (told the number of writes received).
    """

    def __init__(self, store: ScenarioStore, *, unavailable: str | None = None) -> None:
        self.store = store
        self.unavailable = unavailable
        self.crash_after: int | None = None
        self.calls: list[str] = []
        self.sent: list[dict[str, Any]] = []
        self.lose_response_once: set[str] = set()
        self.no_token_once: set[str] = set()
        self.hold: set[str] = set()
        self.after_call: Any = None
        self.clock_reads = 0
        # The facts every read named (airline_systems.facts): never a case, trace or record.
        self.asked: list[dict[str, Any]] = []
        self.services = {
            name: ActionService(store, name, confirm_seconds=0)
            for name in ("schedule", "maintenance", "crew", "booking", "slots")
        }
        self.routes = {
            name: NativeRoutes(service, store) for name, service in self.services.items()
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
        if system == self.unavailable:
            raise CloudDependencyError(
                "Cloud dependency could not be reached; retry shortly.", retryable=True
            )
        unsent = next((k for k in self.no_token_once if _matches(k, system, path)), None)
        if unsent is not None:
            self.no_token_once.discard(unsent)
            raise CloudDependencyError(
                "ARROW could not get a token for a cloud dependency; retry shortly.",
                retryable=True,
            )
        write = method != "GET"
        if write and self.crash_after is not None and len(self.calls) == self.crash_after:
            raise RuntimeError("container recycled")
        received = {**headers, "traceparent": f"00-{trace}-{'0' * 15}1-01"}
        status, reply = self.routes[system].handle(method, path, received, body)
        if write:
            self.calls.append(next(v for k, v in headers.items() if k.lower() == "x-request-id"))
            self.sent.append({"path": path, "headers": dict(headers), "body": body})
            if self.after_call is not None:
                self.after_call(len(self.calls))
        lost = next((k for k in self.lose_response_once if _matches(k, system, path)), None)
        if lost is not None:
            self.lose_response_once.discard(lost)
            raise CloudDependencyError(
                "Cloud dependency did not answer in time.", retryable=True, may_continue=True
            )
        if status >= 400:
            raise CloudDependencyError(
                f"Cloud dependency returned HTTP {status}; no result accepted",
                status_code=status,
                detail=reply.get("detail"),
            )
        if not write and any(_matches(key, system, path) for key in self.hold):
            return {**reply, "status": "pending"}
        return reply

    def rotation(self, trace: str, aircraft_reg: str) -> dict[str, Any]:
        flights = self.store.recovery_inventory(self.store.day)["flights"]
        return {"flights": [f for f in flights if f["aircraft_reg"] == aircraft_reg]}

    def network(self, trace: str) -> dict[str, Any]:
        return ScheduleService(self.store).network(self.store.day)

    def clock(self) -> str:
        """Schedule's clock, read outside any case (the held storm's call-by check)."""
        self.clock_reads += 1
        return str(ScheduleService(self.store).network(self.store.day)["as_of_local"])

    def rebooking(
        self, case: CaseRef, option_id: str, additional_delay_minutes: int
    ) -> dict[str, Any]:
        plan = self.read(
            case,
            lambda found: RebookingService(self.store).plan(
                found, option_id, additional_delay_minutes
            ),
        )
        return {key: value for key, value in plan.items() if key != "assignments"}

    def duty_plan(
        self,
        case: CaseRef,
        option_id: str,
        additional_delay_minutes: int,
        reserve_option_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        plan = self.read(
            case,
            lambda found: DutyPlanService(self.store).plan(
                found,
                option_id,
                additional_delay_minutes,
                reserve_option_ids=reserve_option_ids,
                caller=case.trace,
            ),
        )
        return {key: value for key, value in plan.items() if key != "changes"}

    def options(self, case: CaseRef, additional_delay_minutes: int) -> dict[str, Any]:
        return self.read(
            case,
            lambda found: recovery_options(
                self.store,
                found,
                additional_delay_minutes,
                inventory=facts.inventory(self.store, found),
            ),
        )

    def assessment(
        self, domain: str, case: CaseRef, option_id: str, additional_delay_minutes: int
    ) -> dict[str, Any]:
        return self.read(
            case,
            lambda found: RecoveryAssessmentService(self.store).assess(
                domain, found, option_id, additional_delay_minutes
            ),
        )

    def reserve_options(
        self, case: CaseRef, option_id: str, additional_delay_minutes: int
    ) -> dict[str, Any]:
        return self.read(
            case,
            lambda found: ReserveService(self.store).options(
                found, option_id, additional_delay_minutes, caller=case.trace
            ),
        )

    # The OCC playbook's read: Schedule's dispatch advice, as it answers over HTTP.
    def dispatch_advisories(self, case: CaseRef) -> dict[str, Any]:
        return self.read(case, lambda found: dispatch_advisories(self.store, found))

    def read(self, case: CaseRef, work: Any) -> dict[str, Any]:
        """A read about the case's disruption by its facts, answered as each system's facts
        routes answer it: 422 for facts that name no disruption, 409 for facts that do not fit
        today's operation. ARROW's case and trace are never what a system reads by."""
        self.asked.append(dict(case.disruption))
        try:
            result: dict[str, Any] = work(
                facts.reported(self.store, facts.facts_of(case.disruption))
            )
        except ValueError as error:
            status = 422 if isinstance(error, facts.FactsRefused) else 409
            raise CloudDependencyError(
                f"Cloud dependency returned HTTP {status}; no result accepted",
                status_code=status,
                detail=str(error),
            ) from error
        return result

    def airports(self, trace: str) -> dict[str, Any]:
        return {"airports": self.store.airport_rules()}


def assessed_case(
    store: ScenarioStore, cases: CaseMemory, *, airports: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The case as the ops app holds it after the agents' assessment and validation: CS-TQA's
    defect recorded on ARROW's case only, read by its facts, so the plan records it first."""
    schedule = store.recovery_inventory(store.day)
    rotation = [f for f in schedule["flights"] if f["aircraft_reg"] == "CS-TQA"]
    originals = {flight["flight_no"]: flight for flight in rotation}
    if airports is None:
        airports = {rule["iata"]: rule for rule in store.airport_rules()}
    defect = {**DEFECT_FACTS, "unavailable_minutes": store.defect["unavailable_minutes"]}
    found = facts.reported(store, facts.facts_of(defect))
    options = recovery_options(store, found, inventory=facts.inventory(store, found))["options"]
    for option in options:
        option["domain_assessments"] = [
            RecoveryAssessmentService(store).assess(domain, found, option["option_id"])
            for domain in ("crew", "booking", "slots")
        ]
        plan = RebookingService(store).plan(found, option["option_id"])
        duties = DutyPlanService(store).plan(found, option["option_id"])
        option["validation"] = validate(
            option, originals=originals, rebooking=plan, airports=airports, duty_plan=duties
        )
        option["policy"] = classify(
            option, originals=originals, validation=option["validation"], limits=RELEASE_LIMITS
        ).as_dict()
    document = {
        "trace_id": TRACE,
        "display_id": "DSR-TEST",
        "version": 5,
        "status": "assessment_ready",
        "operating_date": store.day.isoformat(),
        "defect": {
            "aircraft_reg": "CS-TQA",
            "description": "Hydraulic leak",
            "unavailable_minutes": defect["unavailable_minutes"],
            "reported_at": found["reported_at"],
            "unavailable_until": found["unavailable_until"],
        },
        # Recorded on ARROW's case only: every re-check names its facts.
        "disruption": {
            "type": "aircraft_defect",
            "cause": "aircraft_defect",
            "scope": {"flights": [], "aircraft_reg": "CS-TQA"},
        },
        "rotation": {"flights": rotation},
        "airports": airports,
        "options": options,
        "events": [],
    }
    cases.create(document)
    return document


def approve(cases: CaseMemory, option_id: str, route: str) -> None:
    document = cases.get(TRACE)
    execution = authorise_execution(
        document, option_id, route=route, approved_by="policy" if route == "auto" else "dm-oid"
    )
    cases.update_execution(TRACE, execution, None)


def statuses(cases: CaseMemory) -> dict[str, str]:
    return {action["kind"]: action["status"] for action in cases.get(TRACE)["execution"]["actions"]}


def sent_calls(execution: dict[str, Any]) -> list[dict[str, Any]]:
    """Every call ARROW made for the plan's actions, in order."""
    return [call for action in execution["actions"] for call in action.get("calls") or []]


def ledger_calls(execution: dict[str, Any]) -> list[dict[str, Any]]:
    """The calls the recipients' action ledgers keep: all but the reported disruption's record,
    which Maintenance keeps as the defect itself."""
    return [
        call for call in sent_calls(execution) if not call["call_id"].endswith("-record-disruption")
    ]


def check_approved_four_hour_aog_executes_each_system_once_in_order() -> None:
    store, cases = ScenarioStore(TRACE, 240), CaseMemory()
    document = assessed_case(store, cases)
    delay = next(o for o in document["options"] if o["option_id"] == "delay")
    assert delay["policy"]["route"] == "occ"
    approve(cases, "delay", "occ")
    gateway = SystemsGateway(store)

    Executor(cases, gateway, poll_seconds=0).run(TRACE)

    execution = cases.get(TRACE)["execution"]
    assert execution["status"] == "completed", execution
    assert set(statuses(cases).values()) == {"confirmed"}
    # Owner: the defect reaches Maintenance only now, as the plan's first action, with a receipt
    # and the event id Maintenance issues as it records it; the retime follows.
    record = execution["actions"][0]
    [recorded] = record["calls"]
    assert (record["kind"], recorded["recipient"], recorded["path"]) == (
        "record_disruption",
        "Maintenance",
        "/api/defects",
    ), record
    assert recorded["event_id"] == event_ids.event_id(TRACE), recorded
    assert record["message"].startswith(f"Defect recorded as event {recorded['event_id']}")
    assert (store.defect["trace_id"], store.defect["unavailable_until"][11:16]) == (TRACE, "13:45")
    assert gateway.calls[0].endswith("record-disruption")
    assert gateway.calls[1].endswith("retime-flights-ar101")
    assert len(gateway.calls) == len(set(gateway.calls)) == len(sent_calls(execution))
    assert store.flights["AR101"]["std_local"].endswith("T13:45:00")
    assert store.segments["SYN002"] == ["AR112", "AR151"]
    assert store.segments["SYN004"] == ["AR132", "AR163"]
    assert store.duties["D-LIS-001"]["release_local"].endswith("T19:20:00")
    events = [event["event"] for event in cases.get(TRACE)["events"]]
    assert events[0] == "execution_started" and events[-1] == "execution_completed"


def check_approved_cancellation_executes_and_rebooks_its_passengers() -> None:
    # Option B in the demo: cancel the round trip; slots are released and passengers rebooked.
    store, cases = ScenarioStore(TRACE, 240), CaseMemory()
    document = assessed_case(store, cases)
    cancel = next(o for o in document["options"] if o["option_id"] == "cancel")
    assert cancel["policy"]["route"] == "occ", cancel["policy"]
    approve(cases, "cancel", "occ")
    gateway = SystemsGateway(store)

    Executor(cases, gateway, poll_seconds=0).run(TRACE)

    execution = cases.get(TRACE)["execution"]
    assert execution["status"] == "completed", execution
    assert set(statuses(cases).values()) == {"confirmed"}
    assert {"cancel_flights", "rebook_passengers"} <= set(statuses(cases))
    assert "notify_passengers" not in statuses(cases)
    assert {store.flights[n]["status"] for n in ("AR101", "AR102")} == {"cancelled"}
    assert len(store.actions) == len(ledger_calls(execution))


def check_source_changed_after_approval_stops_before_any_write() -> None:
    store, cases = ScenarioStore(TRACE, 240), CaseMemory()
    assessed_case(store, cases)
    approve(cases, "delay", "occ")
    store.flights["AR102"]["std_local"] = store.flights["AR102"]["std_local"].replace(
        "12:00", "12:30"
    )

    Executor(cases, SystemsGateway(store), poll_seconds=0).run(TRACE)

    state = cases.get(TRACE)
    assert state["execution"]["status"] == "needs_attention"
    assert "AR102" in state["events"][-1]["message"]
    assert store.actions == {}


def check_unreachable_system_leaves_the_outcome_unknown_and_escalates() -> None:
    store, cases = ScenarioStore(TRACE, 240), CaseMemory()
    assessed_case(store, cases)
    approve(cases, "delay", "occ")

    Executor(cases, SystemsGateway(store, unavailable="schedule"), poll_seconds=0).run(TRACE)

    state = cases.get(TRACE)
    kinds = statuses(cases)
    assert state["execution"]["status"] == "needs_attention"
    # Maintenance recorded the defect before operations control was tried.
    assert kinds.pop("record_disruption") == "confirmed", kinds
    assert kinds["retime_flights"] == "unknown"
    assert {kinds[k] for k in kinds if k != "retime_flights"} == {"blocked"}
    assert state["events"][-1]["event"] == "execution_needs_attention"
    assert "Ops control outcome unknown" in state["events"][-1]["message"]
    assert "lease" not in state["execution"]
    assert store.actions == {}


def check_an_unclear_flight_update_is_resent_and_applied_once() -> None:
    # Operations control applied AR101's new times but the reply never arrived: ARROW sends the
    # same update again under the same Idempotency-Key, and the flight is moved once.
    store, cases = ScenarioStore(TRACE, 240), CaseMemory()
    assessed_case(store, cases)
    approve(cases, "delay", "occ")
    gateway = SystemsGateway(store)
    gateway.lose_response_once = {"schedule"}

    Executor(cases, gateway, poll_seconds=0).run(TRACE)

    execution = cases.get(TRACE)["execution"]
    assert execution["status"] == "completed", execution
    assert statuses(cases)["retime_flights"] == "confirmed"
    first, again = gateway.sent[1], gateway.sent[2]
    assert first["path"] == again["path"] == "/api/flights/AR101", (first, again)
    assert first["headers"]["Idempotency-Key"].endswith("-ar101"), first
    assert first["headers"] == again["headers"] and first["body"] == again["body"]
    assert store.flights["AR101"]["std_local"].endswith("T13:45:00")
    assert len(set(gateway.calls)) == len(sent_calls(execution))
    assert len(store.actions) == len(ledger_calls(execution))


def check_an_unclear_message_is_not_resent() -> None:
    # A message to EUROCONTROL is never sent twice: its reply was lost, so its outcome is
    # unknown and OCC checks with the Network Manager before acting.
    store, cases = ScenarioStore(TRACE, 240), CaseMemory()
    assessed_case(store, cases)
    approve(cases, "delay", "occ")
    gateway = SystemsGateway(store)
    gateway.lose_response_once = {"/nm/messages"}

    Executor(cases, gateway, poll_seconds=0).run(TRACE)

    state = cases.get(TRACE)
    execution = state["execution"]
    lost = next(a for a in execution["actions"] if a["kind"] == "network_manager.fpl_delay")
    [call] = lost["calls"]
    assert (lost["status"], call["status"]) == ("unknown", "unknown"), lost
    assert call["recipient"] == "EUROCONTROL Network Manager", call
    assert gateway.calls.count(call["call_id"]) == 1, gateway.calls
    assert store.actions[call["call_id"]]["request"]["kind"] == "network_manager.fpl_delay"
    assert execution["status"] == "needs_attention"
    assert "outcome unknown" in state["events"][-1]["message"], state["events"][-1]


def check_a_message_that_never_left_is_sent_on_the_next_try() -> None:
    # Final review: ARROW could not get a token for its first delay message, so nothing
    # reached EUROCONTROL. The message goes on the next try, once, and is confirmed; OCC is not
    # asked to check an outcome that was never in doubt.
    store, cases = ScenarioStore(TRACE, 240), CaseMemory()
    assessed_case(store, cases)
    approve(cases, "delay", "occ")
    gateway = SystemsGateway(store)
    gateway.no_token_once = {"/nm/messages"}

    Executor(cases, gateway, poll_seconds=0).run(TRACE)

    execution = cases.get(TRACE)["execution"]
    first = next(a for a in execution["actions"] if a["kind"] == "network_manager.fpl_delay")
    [call] = first["calls"]
    assert (first["status"], call["status"]) == ("confirmed", "confirmed"), first
    assert gateway.calls.count(call["call_id"]) == 1, gateway.calls
    assert store.actions[call["call_id"]]["request"]["kind"] == "network_manager.fpl_delay"
    assert execution["status"] == "completed", execution


def check_a_claimed_case_is_not_executed_twice() -> None:
    store, cases = ScenarioStore(TRACE, 240), CaseMemory()
    assessed_case(store, cases)
    approve(cases, "delay", "occ")
    assert cases.claim_execution(TRACE, "other-replica") is not None
    gateway = SystemsGateway(store)

    Executor(cases, gateway, poll_seconds=0).run(TRACE)

    assert gateway.calls == []
    assert cases.get(TRACE)["execution"]["status"] == "approved"


def check_expert_review_recorded_during_execution_is_kept() -> None:
    store, cases = ScenarioStore(TRACE, 240, seats={"AR163": 2}), CaseMemory()
    assessed_case(store, cases)
    approve(cases, "delay", "occ")
    gateway = SystemsGateway(store)

    def expert_acts(calls: int) -> None:
        if calls == 1:
            cases.record_review(TRACE, "passenger_services", "handled", "expert-oid")

    gateway.after_call = expert_acts

    Executor(cases, gateway, poll_seconds=0).run(TRACE)

    execution = cases.get(TRACE)["execution"]
    assert execution["status"] == "completed"
    assert execution["reviews"][0]["status"] == "handled", execution["reviews"]


def approved_night_quota_case(store: ScenarioStore, cases: CaseMemory) -> None:
    """The four-hour AOG with FAO's night quota from 14:00: OCC approved the delay, which asks
    the slot coordinator before the schedule changes."""
    airports = {rule["iata"]: rule for rule in store.airport_rules()}
    airports["FAO"] = {
        **airports["FAO"],
        "night_arrival_from": "14:00",
        "night_arrival_until": "23:00",
    }
    document = assessed_case(store, cases, airports=airports)
    delay = next(o for o in document["options"] if o["option_id"] == "delay")
    assert any("FAO night quota" in r for r in delay["validation"]["airport_requests"]), delay
    assert delay["policy"]["route"] == "occ", delay["policy"]
    approve(cases, "delay", "occ")


def check_a_slot_request_waits_then_the_retime_follows() -> None:
    # Owner: a plan needing a night-quota slot is OCC's decision; after approval ARROW asks
    # the slot coordinator first and changes the schedule only once it confirms. The coordinator
    # answers later; the supervisor picks the case up again and completes it.
    store, cases = ScenarioStore(TRACE, 240), CaseMemory()
    scheduled = store.flights["AR131"]["std_local"]
    approved_night_quota_case(store, cases)
    gateway = SystemsGateway(store)
    gateway.hold = {"/slot-coordinator/requests"}

    Executor(cases, gateway, poll_seconds=0, pending_timeout_seconds=0).run(TRACE)

    execution = cases.get(TRACE)["execution"]
    assert execution["status"] == "pending_external"
    record, request, retime = execution["actions"][:3]
    assert (record["kind"], record["status"]) == ("record_disruption", "confirmed"), record
    [asked] = request["calls"]
    assert (asked["recipient"], asked["method"], asked["path"]) == (
        "Slot coordinator",
        "POST",
        "/slot-coordinator/requests",
    ), asked
    assert (request["status"], asked["status"]) == ("pending", "pending"), request
    assert asked["request_id"], asked
    assert retime["kind"] == "retime_flights" and retime["status"] == "not_started", retime
    assert not retime.get("calls"), retime
    assert store.flights["AR131"]["std_local"] == scheduled

    gateway.hold = set()
    Operations(cases, gateway).resume()  # type: ignore[arg-type]

    execution = cases.get(TRACE)["execution"]
    assert execution["status"] == "completed", execution
    assert {call["status"] for call in sent_calls(execution)} == {"confirmed"}
    assert set(statuses(cases).values()) == {"confirmed"}
    assert store.flights["AR131"]["std_local"] != scheduled
    assert len(store.actions) == len(ledger_calls(execution))


def check_a_refused_slot_request_stops_the_retime() -> None:
    # Native dispatch spec §6.4: the slot coordinator refuses the night-quota slot. The flights keep
    # their times, no update or message that depends on the slot is sent, and OCC is told.
    store, cases = ScenarioStore(TRACE, 240), CaseMemory()
    scheduled = store.flights["AR131"]["std_local"]
    approved_night_quota_case(store, cases)
    gateway = SystemsGateway(store)
    gateway.services["slots"].refuse_kinds = frozenset({"slot_coordinator.slot_change_request"})

    Executor(cases, gateway, poll_seconds=0).run(TRACE)

    state = cases.get(TRACE)
    execution = state["execution"]
    record, *planned = execution["actions"]
    assert (record["kind"], record["status"]) == ("record_disruption", "confirmed"), record
    requests = [a for a in planned if a["kind"].startswith("slot_coordinator.")]
    rest = [a for a in planned if a not in requests]
    assert requests and all(
        (a["status"], a["calls"][0]["status"]) == ("refused", "refused") for a in requests
    ), requests
    assert requests[0]["message"] == (
        "The slot coordinator rejected the slot request for AR131 at FAO: "
        "Counterparty refused the request"
    ), requests[0]
    assert rest and all(a["status"] == "blocked" and not a.get("calls") for a in rest), rest
    assert {sent["path"] for sent in gateway.sent} == {"/api/defects", "/slot-coordinator/requests"}
    assert store.flights["AR131"]["std_local"] == scheduled
    assert execution["status"] == "needs_attention"
    assert "Slot coordinator refused" in state["events"][-1]["message"], state["events"][-1]


def check_a_slot_request_never_answered_is_unknown_for_occ() -> None:
    # The slot coordinator has not answered after 30 minutes: ARROW stops waiting, the request's
    # outcome is unknown, the flights keep their times and OCC takes over.
    store, cases = ScenarioStore(TRACE, 240), CaseMemory()
    scheduled = store.flights["AR131"]["std_local"]
    approved_night_quota_case(store, cases)
    gateway = SystemsGateway(store)
    gateway.hold = {"/slot-coordinator/requests"}
    clock = [datetime.now(UTC)]

    def executor() -> Executor:
        return Executor(
            cases, gateway, poll_seconds=0, pending_timeout_seconds=0, now=lambda: clock[0]
        )

    executor().run(TRACE)
    clock[0] += timedelta(minutes=31)
    executor().run(TRACE)

    state = cases.get(TRACE)
    execution = state["execution"]
    request, retime = execution["actions"][1], execution["actions"][2]
    [asked] = request["calls"]
    assert (request["status"], asked["status"]) == ("unknown", "unknown"), request
    assert asked["message"] == "No answer from Slot coordinator after 30 min", asked
    assert retime["status"] == "blocked" and not retime.get("calls"), retime
    assert execution["status"] == "needs_attention"
    assert store.flights["AR131"]["std_local"] == scheduled


class RejectingNetworkManager(SystemsGateway):
    """EUROCONTROL rejects every message: it has no flight plan for the flight."""

    def send(
        self,
        system: str,
        method: str,
        path: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
        trace: str,
    ) -> dict[str, Any]:
        if path == "/nm/messages":
            return {"status": "REJ", "reason": "No flight plan for this flight"}
        return super().send(system, method, path, headers, body, trace)


def check_a_rejected_message_is_refused_with_its_reason() -> None:
    # The Network Manager answers REJ: the message is refused with EUROCONTROL's reason, and OCC
    # is told; the schedule change already made stays in place.
    store, cases = ScenarioStore(TRACE, 240), CaseMemory()
    assessed_case(store, cases)
    approve(cases, "delay", "occ")

    Executor(cases, RejectingNetworkManager(store), poll_seconds=0).run(TRACE)

    state = cases.get(TRACE)
    execution = state["execution"]
    message = next(a for a in execution["actions"] if a["kind"] == "network_manager.fpl_delay")
    assert (message["status"], message["calls"][0]["status"]) == ("refused", "refused"), message
    # The case's timeline says what happened, not the reply code (final review).
    rejected = "EUROCONTROL rejected the delay message for AR101: No flight plan for this flight"
    assert message["message"] == rejected, message
    assert rejected in [event["message"] for event in state["events"]], state["events"]
    assert statuses(cases)["retime_flights"] == "confirmed"
    assert execution["status"] == "needs_attention"
    assert "No flight plan for this flight" in state["events"][-1]["message"]


def check_a_crash_midway_sends_each_flight_update_once() -> None:
    # Review focus 2: the container recycles after operations control confirmed AR101's update.
    # On the re-run the confirmed update is skipped and each other flight's is sent once.
    store, cases = ScenarioStore(TRACE, 240), CaseMemory()
    assessed_case(store, cases)
    approve(cases, "delay", "occ")
    gateway = SystemsGateway(store)
    gateway.crash_after = 2

    try:
        Executor(cases, gateway, poll_seconds=0).run(TRACE)
    except RuntimeError:
        pass
    else:
        raise AssertionError("the injected container recycle did not interrupt execution")
    record, first = gateway.calls
    assert record.endswith("record-disruption") and first.endswith("retime-flights-ar101"), first
    gateway.crash_after = None
    Executor(cases, gateway, poll_seconds=0).run(TRACE)

    execution = cases.get(TRACE)["execution"]
    assert execution["status"] == "completed", execution
    retime = execution["actions"][1]
    assert [call["path"] for call in retime["calls"]] == [
        f"/api/flights/{flight_no}" for flight_no in ("AR101", "AR102", "AR131", "AR132")
    ], retime["calls"]
    assert gateway.calls[:2] == [record, first], gateway.calls
    assert gateway.calls.count(record) == gateway.calls.count(first) == 1
    assert len(gateway.calls) == len(set(gateway.calls)) == len(sent_calls(execution))
    assert len(store.actions) == len(ledger_calls(execution))


class KilledReplica(CaseMemory):
    """The case store as a killed container leaves it: nothing written after the kill lands."""

    killed = False

    def update_execution(self, *args: Any, **kwargs: Any) -> None:
        if not self.killed:
            super().update_execution(*args, **kwargs)


def check_a_message_in_flight_when_arrow_dies_is_not_sent_again() -> None:
    # The container is killed just after EUROCONTROL took a delay message, before ARROW saw the
    # reply. The replica that takes the case over once the lease runs out does not send it
    # again: its outcome is unknown and OCC checks with the Network Manager.
    store, cases = ScenarioStore(TRACE, 240), KilledReplica()
    assessed_case(store, cases)
    approve(cases, "delay", "occ")
    gateway = SystemsGateway(store)

    def container_killed(writes: int) -> None:
        if gateway.sent[-1]["path"] == "/nm/messages":
            cases.killed = True
            raise RuntimeError("container killed")

    gateway.after_call = container_killed
    try:
        Executor(cases, gateway, poll_seconds=0).run(TRACE)
    except RuntimeError:
        pass
    else:
        raise AssertionError("the injected kill did not interrupt execution")
    [message] = [sent["headers"]["X-Request-Id"] for sent in gateway.sent if "/nm/" in sent["path"]]
    gateway.after_call, cases.killed = None, False
    cases.docs[TRACE]["execution"]["lease"]["until"] = "2000-01-01T00:00:00+00:00"

    Executor(cases, gateway, poll_seconds=0).run(TRACE)

    state = cases.get(TRACE)
    execution = state["execution"]
    [unclear] = [a for a in execution["actions"] if a["action_id"] == message]
    assert (unclear["status"], unclear["calls"][0]["status"]) == ("unknown", "unknown"), unclear
    assert gateway.calls.count(message) == 1, gateway.calls
    assert execution["status"] == "needs_attention"
    assert "outcome unknown" in state["events"][-1]["message"], state["events"][-1]


def check_a_flight_changed_before_its_update_is_refused_and_nothing_after_it_runs() -> None:
    # Review focus 1: operations control moves AR102 after ARROW's recheck, before AR102's update
    # arrives. The version ARROW validated no longer matches, so the update is refused, the rest
    # of the retime is not sent, nothing that depends on it starts, and OCC is told which flight.
    store, cases = ScenarioStore(TRACE, 240), CaseMemory()
    assessed_case(store, cases)
    approve(cases, "delay", "occ")
    gateway = SystemsGateway(store)
    later = {n: store.flights[n]["std_local"] for n in ("AR131", "AR132")}

    def operations_control_moves_ar102(writes: int) -> None:
        if writes == 2:  # the defect's record, then AR101's update
            flight = store.flights["AR102"]
            flight["std_local"] = flight["std_local"].replace("12:00", "12:30")

    gateway.after_call = operations_control_moves_ar102

    Executor(cases, gateway, poll_seconds=0).run(TRACE)

    state = cases.get(TRACE)
    execution = state["execution"]
    record, retime, *dependents = execution["actions"]
    assert (record["kind"], record["status"]) == ("record_disruption", "confirmed"), record
    assert retime["kind"] == "retime_flights"
    assert retime["status"] == "refused" and "Source changed" in retime["message"], retime
    assert [(call["path"], call["status"]) for call in retime["calls"]] == [
        ("/api/flights/AR101", "confirmed"),
        ("/api/flights/AR102", "refused"),
    ], retime["calls"]
    assert dependents and all(a["status"] == "blocked" for a in dependents), statuses(cases)
    assert execution["status"] == "needs_attention"
    # Final review: AR101 has already moved, so OCC is told which flight moved, which
    # was refused and which were not sent.
    told = "AR101 moved; AR102 refused: Source changed: flight AR102; AR131 and AR132 not sent"
    assert retime["message"] == told, retime
    alert = state["events"][-1]["message"]
    assert alert == (
        f"OCC attention: Ops control partly carried out ({told}). "
        "Dependent actions were not sent; what was done stays in place."
    ), alert
    assert all(a["message"].startswith("Not sent: ") for a in dependents), dependents
    assert len(gateway.calls) == 3, gateway.calls
    assert {n: store.flights[n]["std_local"] for n in later} == later


def check_passengers_outside_policy_await_passenger_services() -> None:
    store, cases = ScenarioStore(TRACE, 240, seats={"AR163": 2}), CaseMemory()
    document = assessed_case(store, cases)
    delay = next(o for o in document["options"] if o["option_id"] == "delay")
    assert "4 passengers need Passenger Services review" in delay["policy"]["reasons"]
    approve(cases, "delay", "occ")

    Executor(cases, SystemsGateway(store), poll_seconds=0).run(TRACE)

    execution = cases.get(TRACE)["execution"]
    assert execution["status"] == "completed"
    assert execution["reviews"] == [
        {"kind": "passenger_services", "passengers": 4, "status": "awaiting_expert"}
    ]
    assert store.segments["SYN002"] == ["AR112", "AR151"]
    assert store.segments["SYN004"] == ["AR132", "AR161"]


def check_approved_plan_sends_a_car_for_the_passengers_it_keeps() -> None:
    # D108: 120 min out of service leaves the Faro-Lisbon-London connection 40 min (passport
    # control, 20 needed): ARROW asks ground handling for a car instead of rebooking them.
    store, cases = ScenarioStore(TRACE, 120), CaseMemory()
    document = assessed_case(store, cases)
    delay = next(o for o in document["options"] if o["option_id"] == "delay")
    assert delay["validation"]["rebooking"]["transferred_passengers"] == 4, delay["validation"]
    approve(cases, "delay", delay["policy"]["route"])

    Executor(cases, SystemsGateway(store), poll_seconds=0).run(TRACE)

    execution = cases.get(TRACE)["execution"]
    assert execution["status"] == "completed", execution
    [car] = [a for a in execution["actions"] if a["kind"] == "ground_handling.direct_transfer"]
    assert (car["status"], car["proposed_by"], car["label"]) == (
        "confirmed",
        "passenger-reaccommodation-agent",
        "Ground handling",
    ), car
    receipt = "Direct transfers requested: 4 passengers, 1 connection from AR132"
    assert car["message"] == receipt, car
    assert store.segments["SYN004"] == ["AR132", "AR161"]  # kept, not rebooked
    assert store.segments["SYN002"] == ["AR112", "AR151"]  # missed AR151, rebooked


def check_unknown_airport_rules_block_every_moved_option() -> None:
    # The Slots read failed, so the ops app has no airport rules: unknown is not clearance.
    store, cases = ScenarioStore(TRACE, 240), CaseMemory()
    document = assessed_case(store, cases, airports={})

    delay = next(o for o in document["options"] if o["option_id"] == "delay")
    assert delay["policy"]["route"] == "blocked", delay["policy"]
    assert any(
        reason.startswith("airport rules unknown: ") and "LIS" in reason
        for reason in delay["policy"]["reasons"]
    ), delay["policy"]
    cancel = next(o for o in document["options"] if o["option_id"] == "cancel")
    assert cancel["policy"]["route"] == "blocked", cancel["policy"]


def check_idle_supervisor_leaves_the_database_alone() -> None:
    # The serverless database pauses when idle; with nothing to run, no further scans.
    class CountingCases(CaseMemory):
        scans = 0

        def runnable_traces(self) -> list[str]:
            self.scans += 1
            return super().runnable_traces()

    store, cases = ScenarioStore(TRACE, 240), CountingCases()
    operations = Operations(cases, SystemsGateway(store))  # type: ignore[arg-type]

    operations.supervise(0, rounds=5, idle_timeout=0.01)

    assert cases.scans == 1, cases.scans


class ExecutionWorkflowTests(unittest.TestCase):
    def test_idle_supervisor_leaves_the_database_alone(self) -> None:
        check_idle_supervisor_leaves_the_database_alone()

    def test_unknown_airport_rules_block_every_moved_option(self) -> None:
        check_unknown_airport_rules_block_every_moved_option()

    def test_approved_four_hour_aog_executes_each_system_once_in_order(self) -> None:
        check_approved_four_hour_aog_executes_each_system_once_in_order()

    def test_approved_cancellation_executes_and_rebooks_its_passengers(self) -> None:
        check_approved_cancellation_executes_and_rebooks_its_passengers()

    def test_source_changed_after_approval_stops_before_any_write(self) -> None:
        check_source_changed_after_approval_stops_before_any_write()

    def test_unreachable_system_leaves_the_outcome_unknown_and_escalates(self) -> None:
        check_unreachable_system_leaves_the_outcome_unknown_and_escalates()

    def test_an_unclear_flight_update_is_resent_and_applied_once(self) -> None:
        check_an_unclear_flight_update_is_resent_and_applied_once()

    def test_an_unclear_message_is_not_resent(self) -> None:
        check_an_unclear_message_is_not_resent()

    def test_a_message_that_never_left_is_sent_on_the_next_try(self) -> None:
        check_a_message_that_never_left_is_sent_on_the_next_try()

    def test_a_claimed_case_is_not_executed_twice(self) -> None:
        check_a_claimed_case_is_not_executed_twice()

    def test_expert_review_recorded_during_execution_is_kept(self) -> None:
        check_expert_review_recorded_during_execution_is_kept()

    def test_a_slot_request_waits_then_the_retime_follows(self) -> None:
        check_a_slot_request_waits_then_the_retime_follows()

    def test_a_slot_request_never_answered_is_unknown_for_occ(self) -> None:
        check_a_slot_request_never_answered_is_unknown_for_occ()

    def test_a_refused_slot_request_stops_the_retime(self) -> None:
        check_a_refused_slot_request_stops_the_retime()

    def test_a_rejected_message_is_refused_with_its_reason(self) -> None:
        check_a_rejected_message_is_refused_with_its_reason()

    def test_a_crash_midway_sends_each_flight_update_once(self) -> None:
        check_a_crash_midway_sends_each_flight_update_once()

    def test_a_message_in_flight_when_arrow_dies_is_not_sent_again(self) -> None:
        check_a_message_in_flight_when_arrow_dies_is_not_sent_again()

    def test_a_flight_changed_before_its_update_is_refused_and_nothing_after_it_runs(
        self,
    ) -> None:
        check_a_flight_changed_before_its_update_is_refused_and_nothing_after_it_runs()

    def test_passengers_outside_policy_await_passenger_services(self) -> None:
        check_passengers_outside_policy_await_passenger_services()

    def test_approved_plan_sends_a_car_for_the_passengers_it_keeps(self) -> None:
        check_approved_plan_sends_a_car_for_the_passengers_it_keeps()
