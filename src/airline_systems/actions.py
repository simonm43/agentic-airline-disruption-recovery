"""Checked writes to the sample airline systems: one action per id, receipts, capabilities.

Only the ARROW executor's identity may write. Each write compares the rows it changes with what
ARROW last read (compare-and-set); a mismatch is refused, never silently overwritten. External
counterparties (network manager, airport CDM, slot coordinator, ground handling) are sample
services, and say so.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from airline_systems.recovery import DutyPlanService, RebookingService, passenger_notice
from airline_systems.reserves import (
    ReserveService,
    StandbyAlertStore,
    chosen,
    standby_alert,
    standby_call,
    standby_release,
    too_late,
)
from airline_systems.schedule import ScheduleStore, lisbon_today

ActionKind = Literal[
    "retime_flights",
    "cancel_flights",
    "change_tail",
    "update_duties",
    "assign_reserve_crew",
    "alert_standby",
    "call_standby",
    "release_standby",
    "rebook_passengers",
    "notify_passengers",
    "network_manager.fpl_delay",
    "network_manager.fpl_cancel",
    "network_manager.fpl_change",
    "airport_cdm.tobt_update",
    "slot_coordinator.slot_non_use",
    "slot_coordinator.slot_change_request",
    "ground_handling.direct_transfer",
]

_SYSTEM_KINDS = {
    "schedule": {"retime_flights", "cancel_flights", "change_tail"},
    "crew": {
        "update_duties",
        "assign_reserve_crew",
        "alert_standby",
        "call_standby",
        "release_standby",
    },
    "booking": {"rebook_passengers", "notify_passengers", "ground_handling.direct_transfer"},
    "slots": {
        "network_manager.fpl_delay",
        "network_manager.fpl_cancel",
        "network_manager.fpl_change",
        "airport_cdm.tobt_update",
        "slot_coordinator.slot_non_use",
        "slot_coordinator.slot_change_request",
    },
}
# The OCC playbook's writes (OCC playbook plan) and the storm's standby alerts (case summary and
# held storm plan): each receipt also carries the recipient's answer.
STANDBY_KINDS = frozenset({"alert_standby", "call_standby", "release_standby"})
PLAYBOOK_KINDS = frozenset({"notify_passengers", *STANDBY_KINDS})


def _declared(subscription: bool, conditional: bool, rate: int) -> dict[str, Any]:
    return {
        "query": True,
        "subscription": subscription,
        "conditional_write": conditional,
        "rate_limit_per_minute": rate,
    }


CAPABILITIES: dict[str, dict[str, dict[str, Any]]] = {
    "schedule": {"ops_control": _declared(False, True, 120)},
    "maintenance": {"maintenance": _declared(False, False, 60)},
    "crew": {"crew": _declared(False, True, 60)},
    "booking": {
        "passenger_service": _declared(False, True, 60),
        "ground_handling": _declared(False, False, 60),
    },
    "slots": {
        "network_manager": _declared(True, False, 30),
        "airport_cdm": _declared(True, False, 60),
        "slot_coordinator": _declared(False, False, 10),
    },
}

TRANSPARENCY = "An automated system proposed this change; contact the airline to speak to a person."
_EXTERNAL = {"slot_coordinator.slot_change_request"}
# What each passenger notice kind tells, and how a flight already told of it today is named.
_TOLD = {
    "possible_delay": "of possible delays",
    "new_times": "their new times",
    "no_change": "that no change is expected",
}
_ALREADY = {
    "possible_delay": "warned",
    "new_times": "told",
    "no_change": "told no change is expected",
}

# The only columns an action may compare or change, per table. A request key reaches SQL only as
# one of these names. The published timetable (published_std_local, published_sta_local) is not
# writable: a retime moves the estimate, never what was published.
WRITABLE_COLUMNS: dict[str, frozenset[str]] = {
    "flight": frozenset({"std_local", "sta_local", "status", "aircraft_reg"}),
    "duty": frozenset(
        {
            "base",
            "start_airport",
            "qualification",
            "report_local",
            "release_local",
            "fdp_minutes",
            "max_fdp_minutes",
            "pilots_required",
            "cabin_required",
        }
    ),
    "duty_flight": frozenset({"duty_id"}),
    "assignment": frozenset({"duty_id", "rank"}),
    "reserve": frozenset({"base", "start_local", "end_local"}),
    "segment": frozenset({"flight_no"}),
    # The OCC playbook's passenger notices (sql/017_playbook.sql, keyed by kind since sql/019):
    # inserted once each, never changed; moved_early is always 0 (earlier-flight offers were
    # removed).
    "passenger_notice": frozenset({"notice_id", "trace_id", "passengers", "moved_early"}),
    # A storm case's standby alerts (sql/019): inserted alerted, then called or released.
    "standby_alert": frozenset(
        {"trace_id", "reserve_option_id", "crew_ids", "call_by_local", "status"}
    ),
}
_OPERATIONS = {"update", "insert", "delete"}


def describe(table: str, key: dict[str, Any]) -> str:
    """A record named without personal data: booking locators never leave Booking."""
    if table == "segment":
        return f"booking segment on {key.get('flight_no', 'a flight')}"
    if table == "passenger_notice":
        return f"passenger notice on {key.get('flight_no', 'a flight')}"
    if table == "standby_alert":
        return f"standby alert {key.get('alert_id', '')}".strip()
    return f"{table} {' '.join(str(value) for value in key.values())}"


def source_changed(table: str, key: dict[str, Any]) -> str:
    """Refusal text for a failed conditional write."""
    return f"Source changed: {describe(table, key)}"


def seats_gone(flight_no: str) -> str:
    """Refusal text when a rebooking no longer fits its flight: another action took the seats."""
    return f"Source changed: not enough free seats on {flight_no}"


def check_changes(changes: list[dict[str, Any]]) -> None:
    """Refuse a change that names an unknown table or column, or an update or delete that
    compares nothing (it would overwrite whatever is there)."""
    for change in changes:
        table, op = str(change.get("table")), change.get("op", "update")
        expected, values = change.get("expected"), change.get("values")
        allowed = WRITABLE_COLUMNS.get(table)
        if allowed is None or op not in _OPERATIONS:
            raise ValueError(f"Unknown change: {table} {op}")
        record = describe(table, change["key"])
        if not isinstance(expected, dict) or not isinstance(values, dict):
            raise ValueError(f"Malformed change to {record}")
        unknown = sorted(str(name) for name in {*expected, *values} - allowed)
        if unknown:
            raise ValueError(f"{record} has no column {unknown[0]!r}")
        if op != "insert" and not expected:
            raise ValueError(f"A write to {record} must compare a column")


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(pattern=r"^[a-z0-9-]{8,80}$")
    kind: ActionKind
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    plan_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    proposed_by: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{2,63}$")
    expected: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)


class ActionStore(Protocol):
    def find_action(self, action_id: str) -> dict[str, Any] | None: ...
    def apply_action(
        self, request: dict[str, Any], changes: list[dict[str, Any]], receipt: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]: ...
    def update_action(self, action_id: str, receipt: dict[str, Any]) -> None: ...


class PlaybookStore(Protocol):
    """What the OCC playbook's passenger notice reads back: today's flights notified of one kind
    (flight to the notice that notified it)."""

    def passenger_notices(self, day: date, kind: str = "new_times") -> dict[str, str]: ...


class _Refused(Exception):
    """The source no longer matches what ARROW validated; nothing is written."""


def _hhmm(value: str) -> str:
    return datetime.fromisoformat(value).strftime("%H:%M")


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}{'' if number == 1 else 's'}"


class ActionService:
    def __init__(
        self,
        store: ScheduleStore,
        system: str,
        *,
        confirm_seconds: float = 5.0,
        refuse_kinds: frozenset[str] = frozenset(),
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self.actions: ActionStore = store  # type: ignore[assignment]
        self.system = system
        self.confirm_seconds = confirm_seconds
        self.refuse_kinds = refuse_kinds
        self.now = now

    def capabilities(self) -> dict[str, Any]:
        return {"system": self.system, "capabilities": CAPABILITIES.get(self.system, {})}

    def apply(self, request: ActionRequest) -> tuple[dict[str, Any], bool]:
        if request.kind not in _SYSTEM_KINDS.get(self.system, set()):
            raise ValueError(f"The {self.system} system does not accept {request.kind}")
        body = request.model_dump(mode="json")
        prior = self.actions.find_action(request.action_id)
        if prior is not None:
            if prior["request"] != body:
                raise ValueError("Action id was already used for a different request")
            return prior["receipt"], False
        status = "pending" if request.kind in _EXTERNAL else "confirmed"
        answer: dict[str, Any] | None = None
        try:
            if request.kind in PLAYBOOK_KINDS:
                changes, message, answer = self._playbook(request)
            else:
                changes, message = self._changes(request)
        except _Refused as refusal:
            changes, message, status = [], f"Source changed: {refusal}", "refused"
        except KeyError as missing:
            # A 409 is final for ARROW's executor; a 500 would be retried as temporary.
            raise ValueError(
                f"Malformed {request.kind} action: missing {missing.args[0]!r}"
            ) from missing
        except (TypeError, AttributeError) as error:
            raise ValueError(f"Malformed {request.kind} action: {error}") from error
        check_changes(changes)
        receipt = {
            "action_id": request.action_id,
            "system": self.system,
            "capability": request.kind.split(".")[0]
            if "." in request.kind
            else next(iter(CAPABILITIES[self.system])),
            "kind": request.kind,
            "status": status,
            "message": message,
            "at": self.now().isoformat(),
            **({"answer": answer} if answer is not None else {}),
        }
        return self.actions.apply_action(body, changes, receipt)

    def status(self, action_id: str) -> dict[str, Any] | None:
        found = self.actions.find_action(action_id)
        # Action ids are unique across systems; a system answers only for its own receipts.
        if found is None or found["receipt"].get("system") != self.system:
            return None
        receipt: dict[str, Any] = found["receipt"]
        if receipt["status"] != "pending":
            return receipt
        waited = (self.now() - datetime.fromisoformat(receipt["at"])).total_seconds()
        if receipt["kind"] in self.refuse_kinds:
            receipt = {
                **receipt,
                "status": "refused",
                "message": "Counterparty refused the request",
            }
        elif waited >= self.confirm_seconds:
            receipt = {
                **receipt,
                "status": "confirmed",
                "message": f"{receipt['message']} confirmed",
            }
        else:
            return receipt
        self.actions.update_action(action_id, receipt)
        return receipt

    def _playbook(self, request: ActionRequest) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
        """The rows, message and answer of an OCC playbook write (the passenger notice) or a
        standby alert, decided by code from what the recipient holds now; a refusal is a
        ValueError (409), and nothing is written."""
        if request.kind in STANDBY_KINDS:
            return self._standby(request)
        if request.kind != "notify_passengers":
            raise ValueError(f"{request.kind} is not an OCC playbook action")
        kind = request.payload.get("kind", "new_times")
        notices = cast(PlaybookStore, self.store).passenger_notices(lisbon_today(), kind)
        changes, answer = passenger_notice(
            self.store,
            request.action_id,
            request.trace_id,
            request.payload["flights"],
            notices,
            kind,
        )
        already = answer["already_notified"]
        if not answer["notified_flights"]:
            message = (
                f"{_count(len(already), 'flight')} already {_ALREADY[kind]} today:"
                " nobody told twice"
            )
            return changes, message, answer
        again = (
            f"; {_count(len(already), 'flight')} already {_ALREADY[kind]} today" if already else ""
        )
        message = (
            f"Told {answer['notified_passengers']:,} passengers on"
            f" {_count(answer['notified_flights'], 'flight')} {_TOLD[kind]}{again}."
            f" {TRANSPARENCY}"
        )
        return changes, message, answer

    def _standby(self, request: ActionRequest) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
        """A storm case's standby alert, call or release (sql/019): records only, no duty or
        standby row changes."""
        day = lisbon_today()
        alerts = cast(StandbyAlertStore, self.store).standby_alerts(day)
        payload = request.payload
        if request.kind == "alert_standby":
            changes, answer, message = standby_alert(
                self.store.domain_inventory(day, "crew"),
                alerts,
                request.action_id,
                request.trace_id,
                payload["crews"],
            )
        elif request.kind == "call_standby":
            changes, answer, message = standby_call(
                self.store.domain_inventory(day, "crew"),
                alerts,
                request.trace_id,
                payload["reserve_option_ids"],
            )
        else:
            changes, answer, message = standby_release(alerts, request.trace_id)
        return changes, message, answer

    def _changes(self, request: ActionRequest) -> tuple[list[dict[str, Any]], str]:
        kind, payload, expected = request.kind, request.payload, request.expected
        if kind == "retime_flights":
            flights = payload["flights"]
            changes = [
                {
                    "table": "flight",
                    "key": {"flight_no": flight["flight_no"]},
                    "expected": expected["flights"][flight["flight_no"]],
                    "values": {"std_local": flight["std_local"], "sta_local": flight["sta_local"]},
                }
                for flight in flights
            ]
            moved = ", ".join(f"{f['flight_no']} {_hhmm(f['std_local'])}" for f in flights)
            return changes, f"Retimed {moved}; MVT sent"
        if kind == "cancel_flights":
            numbers = payload["flight_nos"]
            changes = [
                {
                    "table": "flight",
                    "key": {"flight_no": number},
                    "expected": expected["flights"][number],
                    "values": {"status": "cancelled"},
                }
                for number in numbers
            ]
            return changes, f"Cancelled {', '.join(numbers)}; ASM CNL sent"
        if kind == "change_tail":
            changes = [
                {
                    "table": "flight",
                    "key": {"flight_no": flight["flight_no"]},
                    "expected": expected["flights"][flight["flight_no"]],
                    "values": {"aircraft_reg": flight["aircraft_reg"]},
                }
                for flight in payload["flights"]
            ]
            tails = ", ".join(f"{f['flight_no']} → {f['aircraft_reg']}" for f in payload["flights"])
            return changes, f"Tail change {tails}; ASM sent"
        if kind == "update_duties":
            duties = DutyPlanService(self.store).plan_for(
                payload["flights"], originals=payload["originals"]
            )
            if duties["plan_digest"] != expected.get("plan_digest"):
                raise _Refused("duty plan differs from the validated plan")
            return duties["changes"], duties["message"]
        if kind == "assign_reserve_crew":
            # Every standby crew the plan calls, in one write under one digest; a single
            # reserve_option_id (ARROW before several standby crews) is a one-item list.
            ids = chosen(payload.get("reserve_option_ids"), payload.get("reserve_option_id"))
            if not ids:
                raise KeyError("reserve_option_ids")
            # A standby crew another case has called is not offered to this one.
            offered = ReserveService(self.store).options_for(
                payload["flights"], originals=payload["originals"], trace_id=request.trace_id
            )
            by_id = {option["reserve_option_id"]: option for option in offered["options"]}
            gone = [item for item in ids if item not in by_id]
            if gone:
                raise _Refused(f"reserve option {gone[0]} is no longer offered")
            late = [reason for item in ids if (reason := too_late(by_id[item]))]
            if late:
                # Never confirm a crew assignment the crew cannot fly; ARROW must move it first.
                raise ValueError(f"Standby crew {late[0]}")
            duties = DutyPlanService(self.store).plan_for(
                payload["flights"],
                originals=payload["originals"],
                reserves=[by_id[item] for item in ids],
            )
            if duties["plan_digest"] != expected.get("plan_digest"):
                raise _Refused("duty plan differs from the validated plan")
            return duties["changes"], duties["message"]
        if kind == "rebook_passengers":
            plan = RebookingService(self.store).plan_for(
                payload["flights"], originals=payload["originals"]
            )
            if plan["plan_digest"] != expected.get("plan_digest"):
                raise _Refused("rebooking plan differs from the validated plan")
            changes = [
                {
                    "table": "segment",
                    "key": {"locator": item["locator"], "flight_no": item["from_flight_no"]},
                    "expected": {"flight_no": item["from_flight_no"]},
                    "values": {"flight_no": item["to_flight_no"]},
                }
                for item in plan["assignments"]
            ]
            return changes, (
                f"Rebooked {plan['rebooked_passengers']} passengers in "
                f"{len(plan['assignments'])} bookings"
            )
        if kind == "ground_handling.direct_transfer":
            # One line for OCC; the flight pairs stay in the request payload.
            transfers = payload["transfers"]
            passengers = sum(item["passengers"] for item in transfers)
            inbound = sorted({item["from_flight_no"] for item in transfers})
            connections = f"{len(transfers)} connection{'' if len(transfers) == 1 else 's'}"
            return [], (
                f"Direct transfers requested: {passengers} passengers, {connections} from "
                f"{', '.join(inbound)}"
            )
        flight = payload.get("flight_no", "")
        if kind == "network_manager.fpl_delay":
            return [], f"DLA {flight} EOBT {_hhmm(payload['eobt'])}"
        if kind == "network_manager.fpl_cancel":
            return [], f"CNL {flight}"
        if kind == "network_manager.fpl_change":
            return [], f"CHG {flight} REG {payload['aircraft_reg']}"
        if kind == "airport_cdm.tobt_update":
            return [], f"TOBT {flight} {_hhmm(payload['tobt'])} at {payload['airport']}"
        if kind == "slot_coordinator.slot_non_use":
            return [], f"Slot non-use {flight} at {payload['airport']}"
        return [], f"Slot request {payload.get('request', flight)}"
