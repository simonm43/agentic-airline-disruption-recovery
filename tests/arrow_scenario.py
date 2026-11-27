"""In-memory airline systems for business-flow tests: one small, realistic Lisbon morning.

CS-TQA flies AR101/AR102/AR131/AR132. CS-TQB and CS-TQC fly the alternatives that real
rebooking would use. Writes behave like the SQL store: one action per id, compare-and-set on the
rows an action changes, and an audit record for every outcome.
"""

from __future__ import annotations

import copy
import threading
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from airline_systems.actions import seats_gone, source_changed
from airline_systems.operating_day import max_daily_fdp_minutes
from airline_systems.schedule import Flight, lisbon_today

FLIGHTS = [
    ("AR101", "LIS", "OPO", (10, 30), (11, 25), "CS-TQA"),
    ("AR102", "OPO", "LIS", (12, 0), (12, 55), "CS-TQA"),
    ("AR131", "LIS", "FAO", (14, 10), (14, 55), "CS-TQA"),
    ("AR132", "FAO", "LIS", (15, 35), (16, 20), "CS-TQA"),
    ("AR111", "LIS", "OPO", (10, 40), (11, 35), "CS-TQB"),
    ("AR112", "OPO", "LIS", (12, 5), (13, 0), "CS-TQB"),
    ("AR151", "LIS", "MAD", (14, 0), (16, 15), "CS-TQC"),
    ("AR161", "LIS", "LHR", (17, 30), (20, 0), "CS-TQC"),
    ("AR153", "LIS", "MAD", (19, 0), (21, 15), "CS-TQD"),
    ("AR163", "LIS", "LHR", (21, 30), (23, 55), "CS-TQD"),
]
PNRS = [
    ("SYN001", 2, ["AR101"]),
    ("SYN002", 3, ["AR102", "AR151"]),
    ("SYN003", 1, ["AR131"]),
    ("SYN004", 4, ["AR132", "AR161"]),
]
SEATS = {"AR111": 20, "AR112": 20, "AR153": 10, "AR163": 10}
CAPACITY = {"LIS": 40, "OPO": 24, "FAO": 20}
# The OCC playbook's records (sql/017_playbook.sql) and a storm case's standby alerts
# (sql/019_storm_held.sql), each row inserted once under its key; an alert's status then changes.
RECORD_TABLES = ("passenger_notice", "standby_alert")


def empty_records() -> dict[str, dict[tuple[Any, ...], dict[str, Any]]]:
    return {table: {} for table in RECORD_TABLES}


class ScenarioStore:
    def __init__(
        self, trace: str, unavailable_minutes: int = 240, *, seats: dict[str, int] | None = None
    ) -> None:
        self.day = lisbon_today()
        self.seats = {**SEATS, **(seats or {})}

        def at(hour_minute: tuple[int, int]) -> str:
            return datetime.combine(self.day, time(*hour_minute)).isoformat()

        self.flights: dict[str, dict[str, Any]] = {
            number: {
                "flight_date": self.day.isoformat(),
                "flight_no": number,
                "origin": origin,
                "destination": destination,
                "std_local": at(std),
                "sta_local": at(sta),
                "aircraft_reg": reg,
                "status": "scheduled",
                "published_std_local": at(std),
                "published_sta_local": at(sta),
            }
            for number, origin, destination, std, sta, reg in FLIGHTS
        }
        self.aircraft = {
            reg: {
                "registration": reg,
                "fleet_type": "A320neo",
                "seats": 174,
                "base": "LIS",
                "status": "unserviceable" if reg == "CS-TQA" else "serviceable",
            }
            for reg in ("CS-TQA", "CS-TQB", "CS-TQC", "CS-TQD")
        }
        self.segments = {locator: list(flights) for locator, _, flights in PNRS}
        self.party = {locator: size for locator, size, _ in PNRS}
        self.duties = {
            "D-LIS-001": {
                "duty_id": "D-LIS-001",
                "base": "LIS",
                "start_airport": "LIS",
                "qualification": "A320",
                "report_local": at((9, 30)),
                "release_local": at((16, 50)),
                "fdp_minutes": 410,  # report 09:30 to AR132 on-blocks 16:20
                "max_fdp_minutes": max_daily_fdp_minutes(time(9, 30), 4, 2),
                "pilots_required": 2,
                "cabin_required": 4,
                "flight_nos": ["AR101", "AR102", "AR131", "AR132"],
                "members": [
                    {"crew_id": "C001", "rank": "CPT"},
                    {"crew_id": "C002", "rank": "FO"},
                    {"crew_id": "C003", "rank": "SC"},
                    {"crew_id": "C004", "rank": "CC"},
                    {"crew_id": "C005", "rank": "CC"},
                    {"crew_id": "C006", "rank": "CC"},
                ],
            }
        }
        reported = datetime.combine(self.day, time(9, 45))
        midnight = datetime.combine(self.day + timedelta(days=1), time(0, 0)).isoformat()
        self.reserves: list[dict[str, Any]] = [
            {
                "crew_id": crew_id,
                "rank": rank,
                "base": "LIS",
                "qualification": "A320",
                "start_local": at((12, 0)),
                "end_local": midnight,
                "standby_type": "home",
            }
            for crew_id, rank in (
                ("R001", "CPT"),
                ("R002", "FO"),
                ("R003", "SC"),
                ("R004", "CC"),
                ("R005", "CC"),
                ("R006", "CC"),
            )
        ]
        self.defect = {
            "trace_id": trace,
            "aircraft_reg": "CS-TQA",
            "description": "Hydraulic leak",
            "unavailable_minutes": unavailable_minutes,
            "operating_date": self.day.isoformat(),
            "reported_at": reported.isoformat(),
            "unavailable_until": (reported + timedelta(minutes=unavailable_minutes)).isoformat(),
        }
        self.actions: dict[str, dict[str, Any]] = {}
        self.delays: dict[str, dict[str, Any]] = {}
        self.weather: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self.records: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = empty_records()

    # Reads, as the SQL store provides them.
    def has_day(self, day: date) -> bool:
        return day == self.day

    def flights_for_aircraft(self, day: date, aircraft_reg: str) -> list[Flight]:
        return [
            Flight(
                day,
                flight["flight_no"],
                flight["origin"],
                flight["destination"],
                datetime.fromisoformat(flight["std_local"]),
                datetime.fromisoformat(flight["sta_local"]),
                flight["aircraft_reg"],
                flight["status"],
                datetime.fromisoformat(flight["published_std_local"]),
                datetime.fromisoformat(flight["published_sta_local"]),
            )
            for flight in self.flights.values()
            if flight["aircraft_reg"] == aircraft_reg
        ]

    def recovery_inventory(self, day: date) -> dict[str, Any]:
        return {
            "flights": copy.deepcopy(list(self.flights.values())),
            "aircraft": copy.deepcopy(list(self.aircraft.values())),
        }

    def domain_inventory(self, day: date, domain: str) -> dict[str, Any]:
        if domain == "crew":
            return {
                "duties": copy.deepcopy(list(self.duties.values())),
                "reserves": copy.deepcopy(self.reserves),
            }
        if domain == "booking":
            passengers: dict[str, int] = {}
            for locator, flights in self.segments.items():
                for flight_no in flights:
                    passengers[flight_no] = passengers.get(flight_no, 0) + self.party[locator]
            return {
                "pnrs": [
                    {
                        "locator": locator,
                        "party_size": self.party[locator],
                        "flight_nos": list(flights),
                        "synthetic_recovery_opt_in": False,
                    }
                    for locator, flights in sorted(self.segments.items())
                ],
                "inventory": [
                    {
                        "flight_no": flight_no,
                        "seats": self.seats.get(flight_no, 174),
                        "distance_km": 300,
                        "passengers": passengers.get(flight_no, 0),
                        "held_seats": 0,
                    }
                    for flight_no in self.flights
                ],
            }
        if domain == "slots":
            slots = []
            for flight in self.flights.values():
                for airport, movement, key in (
                    (flight["origin"], "D", "std_local"),
                    (flight["destination"], "A", "sta_local"),
                ):
                    if airport in CAPACITY:
                        slots.append(
                            {
                                "airport": airport,
                                "flight_no": flight["flight_no"],
                                "movement": movement,
                                "slot_local": flight[key],
                            }
                        )
            return {"capacity": dict(CAPACITY), "slots": slots}
        raise ValueError(f"Unknown domain: {domain}")

    def get_defect(self, trace: str) -> dict[str, Any]:
        if trace != self.defect["trace_id"]:
            raise ValueError("Defect not found")
        return copy.deepcopy(self.defect)

    def record_defect(self, defect: dict[str, Any]) -> dict[str, Any]:
        self.defect = copy.deepcopy(defect)
        return defect

    def record_delay(self, delay: dict[str, Any]) -> dict[str, Any]:
        stored = self.delays.setdefault(delay["trace_id"], copy.deepcopy(delay))
        if stored != delay:
            raise ValueError("A different delay already uses this trace")
        return copy.deepcopy(stored)

    def get_delay(self, trace: str) -> dict[str, Any] | None:
        found = self.delays.get(trace)
        return copy.deepcopy(found) if found else None

    def record_weather(self, report: dict[str, Any]) -> dict[str, Any]:
        stored = self.weather.setdefault(report["trace_id"], copy.deepcopy(report))
        if stored != report:
            raise ValueError("A different weather report already uses this trace")
        return copy.deepcopy(stored)

    def get_weather(self, trace: str) -> dict[str, Any] | None:
        found = self.weather.get(trace)
        return copy.deepcopy(found) if found else None

    def event_traces(self, day: Any, digits: str) -> list[str]:
        """The day's recorded delays, weather reports and defects whose trace has ``digits`` as
        its seventh to twelfth characters: the record behind an operations event id."""
        recorded = [*self.delays.values(), *self.weather.values(), self.defect]
        return [
            str(item["trace_id"])
            for item in recorded
            if str(item.get("trace_id") or "")[6:12] == digits
            and item.get("operating_date") in (None, day.isoformat())
        ]

    def airport_rules(self) -> list[dict[str, Any]]:
        return [
            {
                "iata": "LIS",
                "coordination_level": 3,
                "a_cdm": True,
                "night_departure_from": "23:55",
                "night_departure_until": "05:50",
                "night_arrival_from": "00:05",
                "night_arrival_until": "06:00",
                "source": "LIS night period per KB §12.2",
            },
            *(
                {
                    "iata": iata,
                    "coordination_level": None,
                    "a_cdm": False,
                    "night_departure_from": None,
                    "night_departure_until": None,
                    "night_arrival_from": None,
                    "night_arrival_until": None,
                    "source": "no rule modelled",
                }
                for iata in ("FAO", "LHR", "MAD", "OPO")
            ),
        ]

    def log_call(
        self,
        operation: str,
        caller_oid: str | None,
        caller_name: str | None,
        trace_id: str | None,
        outcome: str,
        detail: str | None = None,
        *,
        endpoint: str | None = None,
        request: str | None = None,
        response: str | None = None,
        system: str | None = None,
    ) -> None:
        """One audit.system_call row as the SQL store writes it; ``system`` comes from SystemView,
        as each system's SQL store names itself."""
        self.calls.append(
            {
                "at": datetime.now(UTC).isoformat(),
                "system": system,
                "operation": operation,
                "caller_oid": caller_oid,
                "caller_name": caller_name,
                "trace_id": trace_id,
                "outcome": outcome,
                "detail": detail,
                "endpoint": endpoint,
                "request": request,
                "response": response,
            }
        )

    def system_calls(self, trace: str) -> list[dict[str, Any]]:
        """A case's audit rows as ARROW reads them back (ops_app SqlCaseStore.system_calls)."""
        return [
            {key: value for key, value in row.items() if key != "trace_id"}
            for row in copy.deepcopy(self.calls)
            if row["trace_id"] == trace
        ]

    # Writes, with the SQL store's transaction semantics.
    def find_action(self, action_id: str) -> dict[str, Any] | None:
        found = self.actions.get(action_id)
        return copy.deepcopy(found) if found else None

    def passenger_notices(self, day: date, kind: str = "new_times") -> dict[str, str]:
        return {
            row["flight_no"]: row["notice_id"]
            for row in self.records["passenger_notice"].values()
            if row["kind"] == kind
        }

    def standby_alerts(self, day: date) -> list[dict[str, Any]]:
        return [
            {**copy.deepcopy(row), "crew_ids": row["crew_ids"].split(",")}
            for row in self.records["standby_alert"].values()
        ]

    def _row(self, change: dict[str, Any]) -> dict[str, Any] | None:
        key = change["key"]
        if change["table"] in RECORD_TABLES:
            return self.records[change["table"]].get(tuple(key.values()))
        if change["table"] == "flight":
            return self.flights.get(key["flight_no"])
        if change["table"] == "duty":
            return self.duties.get(key["duty_id"])
        if change["table"] == "duty_flight":
            return next(
                (
                    {"duty_id": duty["duty_id"]}
                    for duty in self.duties.values()
                    if key["flight_no"] in duty["flight_nos"]
                ),
                None,
            )
        if change["table"] == "assignment":
            return next(
                (
                    {"duty_id": duty["duty_id"], "rank": member["rank"]}
                    for duty in self.duties.values()
                    for member in duty["members"]
                    if member["crew_id"] == key["crew_id"]
                ),
                None,
            )
        if change["table"] == "reserve":
            return next((r for r in self.reserves if r["crew_id"] == key["crew_id"]), None)
        if change["table"] == "segment":
            flights = self.segments.get(key["locator"], [])
            if key["flight_no"] in flights:
                return {"flight_no": key["flight_no"]}
            return None
        raise ValueError(f"Unknown table: {change['table']}")

    def _write(self, change: dict[str, Any]) -> None:
        key, values, op = change["key"], change["values"], change.get("op", "update")
        table = change["table"]
        if table in RECORD_TABLES:
            row = tuple(key.values())
            if op == "update":
                self.records[table][row].update(copy.deepcopy(values))
            else:
                self.records[table][row] = {**key, **copy.deepcopy(values)}
        elif table == "flight":
            self.flights[key["flight_no"]].update(values)
        elif table == "duty" and op == "insert":
            self.duties[key["duty_id"]] = {
                **key,
                **copy.deepcopy(values),
                "flight_nos": [],
                "members": [],
            }
        elif table == "duty":
            duty = self.duties[key["duty_id"]]
            if "release_local" in values:  # as SQL: the plan carries FDP to on-blocks, kept legal
                if "fdp_minutes" not in values:
                    raise ValueError(f"A release change must carry its FDP: {key['duty_id']}")
                if values["fdp_minutes"] > duty["max_fdp_minutes"]:
                    raise ValueError(f"Could not update duty {key['duty_id']}")
            duty.update(values)
        elif table == "duty_flight":
            for duty in self.duties.values():
                if key["flight_no"] in duty["flight_nos"]:
                    duty["flight_nos"].remove(key["flight_no"])
            self.duties[values["duty_id"]]["flight_nos"].append(key["flight_no"])
        elif table == "assignment":
            self.duties[values["duty_id"]]["members"].append(
                {"crew_id": key["crew_id"], "rank": values["rank"]}
            )
        elif table == "reserve":
            self.reserves = [r for r in self.reserves if r["crew_id"] != key["crew_id"]]
        else:
            flights = self.segments[key["locator"]]
            flights[flights.index(key["flight_no"])] = values["flight_no"]

    def _full_flight(self, moves: list[dict[str, Any]]) -> str | None:
        """As SQL: the first flight these segment moves would overbook."""
        for flight_no in sorted({move["values"]["flight_no"] for move in moves}):
            booked = sum(
                self.party[pnr] for pnr, legs in self.segments.items() if flight_no in legs
            )
            leaving = sum(
                self.party[m["key"]["locator"]] for m in moves if m["key"]["flight_no"] == flight_no
            )
            arriving = sum(
                self.party[m["key"]["locator"]]
                for m in moves
                if m["values"]["flight_no"] == flight_no
            )
            if booked - leaving + arriving > self.seats.get(flight_no, 174):
                return flight_no
        return None

    def apply_action(
        self, request: dict[str, Any], changes: list[dict[str, Any]], receipt: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        existing = self.actions.get(request["action_id"])
        if existing is not None:
            if existing["request"] != request:
                raise ValueError("Action id was already used for a different request")
            return copy.deepcopy(existing["receipt"]), False
        for change in changes:
            row = self._row(change)
            if change.get("op", "update") == "insert":
                conflict = row is not None
            else:
                conflict = row is None or any(
                    row.get(k) != v for k, v in change["expected"].items()
                )
            if conflict:
                receipt = {
                    **receipt,
                    "status": "refused",
                    "message": source_changed(change["table"], change["key"]),
                }
                self.actions[request["action_id"]] = {"request": request, "receipt": receipt}
                return copy.deepcopy(receipt), True
        full = self._full_flight([c for c in changes if c["table"] == "segment"])
        if full is not None:
            receipt = {**receipt, "status": "refused", "message": seats_gone(full)}
            self.actions[request["action_id"]] = {"request": request, "receipt": receipt}
            return copy.deepcopy(receipt), True
        # Rolled back on a failed write, as a SQL transaction; only the tables written are kept
        # aside (a storm plan sends a few hundred actions over the whole day's bookings).
        written = {change["table"] for change in changes}
        saved = {
            name: copy.deepcopy(getattr(self, name))
            for name, tables in (
                ("flights", {"flight"}),
                ("duties", {"duty", "duty_flight", "assignment"}),
                ("reserves", {"reserve"}),
                ("segments", {"segment"}),
                ("records", set(RECORD_TABLES)),
            )
            if written & tables
        }
        try:
            for change in changes:
                self._write(change)
        except Exception:
            for name, rows in saved.items():
                setattr(self, name, rows)
            raise
        self.actions[request["action_id"]] = {"request": request, "receipt": receipt}
        return copy.deepcopy(receipt), True

    def update_action(self, action_id: str, receipt: dict[str, Any]) -> None:
        self.actions[action_id]["receipt"] = copy.deepcopy(receipt)


class SystemView:
    """One airline system's handle on the shared scenario: every row it audits carries its name,
    as each system's SQL store writes its own (SqlScheduleStore._system)."""

    def __init__(self, scenario: ScenarioStore, system: str) -> None:
        self._scenario = scenario
        self._system = system

    def __getattr__(self, name: str) -> Any:
        return getattr(self._scenario, name)

    def log_call(self, *args: Any, **kwargs: Any) -> None:
        self._scenario.log_call(*args, **kwargs, system=self._system)


class CaseMemory:
    """The ops case store, in memory: documents plus the append-only decision log.

    Imports ops_app lazily: this module is also copied into the systems image's test context.
    """

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.calls: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.RLock()
        self._subjects: dict[str, threading.RLock] = {}

    def subject_lock(self, key: str) -> threading.RLock:
        """As SQL's application lock: one per subject, shared by every app using this store."""
        with self._lock:
            return self._subjects.setdefault(key, threading.RLock())

    def create(self, document: dict[str, Any]) -> None:
        from ops_app.store import DuplicateEvent

        event_id = document.get("event_id")
        if event_id and self.find_by_event(event_id) is not None:
            raise DuplicateEvent(event_id)
        self.docs[document["trace_id"]] = copy.deepcopy(document)

    def find_by_event(self, event_id: str) -> dict[str, Any] | None:
        return next(
            (
                self.get(trace)
                for trace, doc in self.docs.items()
                if doc.get("event_id") == event_id
            ),
            None,
        )

    def case_traces(self, day: str, prefix: str) -> list[str]:
        return [
            trace
            for trace, doc in self.docs.items()
            if trace.startswith(prefix) and doc.get("operating_date") == day
        ]

    def event_traces(self, day: str, digits: str) -> list[str]:
        return [
            trace
            for trace, doc in self.docs.items()
            if trace[6:12] == digits and doc.get("operating_date") == day
        ]

    def get(self, trace: str) -> dict[str, Any]:
        return copy.deepcopy(self.docs[trace])

    def list(self, day: str) -> list[dict[str, Any]]:
        """Newest first, as SqlCaseStore.list orders by created_at DESC."""
        return [
            self.get(trace)
            for trace in reversed(self.open_traces())
            if self.docs[trace].get("operating_date") == day
        ]

    def open_traces(self) -> list[str]:
        return [
            trace
            for trace, doc in self.docs.items()
            if doc["status"] != "closed" and not str(doc.get("display_id") or "").startswith("QA-")
        ]

    def assessing(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    key: copy.deepcopy(doc.get(key))
                    for key in ("trace_id", "assessment", "created_at", "_deadline_at")
                }
                for doc in self.docs.values()
                if doc["status"] == "assessing"
            ]

    def received_events(self, day: str) -> list[dict[str, Any]]:
        """Newest first, as SqlCaseStore.received_events, with only the delivery records."""
        found = [
            self.get(trace)
            for trace, doc in reversed(list(self.docs.items()))
            if doc.get("event_id") and doc.get("operating_date") == day
        ]
        return [
            {**doc, "events": [e for e in doc["events"] if e["event"] == "event_received"]}
            for doc in found[:100]
        ]

    def close(self, trace: str, event: dict[str, Any]) -> bool:
        from ops_app.store import RUNNABLE

        with self._lock:
            doc = self.docs[trace]
            if (doc.get("execution") or {}).get("status") in RUNNABLE:
                return False
            doc["status"] = "closed"
            doc["version"] += 1
            self._event(doc, event)
        return True

    def reopen(self, trace: str, values: dict[str, Any], event: dict[str, Any]) -> bool:
        from ops_app.store import reassessable

        with self._lock:
            doc = self.docs[trace]
            if not reassessable(doc):
                return False
            doc.update(copy.deepcopy(values))
            doc["version"] += 1
            self._event(doc, event)
        return True

    def record_decision(
        self,
        trace: str,
        *,
        option_id: str,
        decision: str,
        expected_version: int,
        idempotency_key: str,
        actor_oid: str,
        comment: str | None = None,
    ) -> dict[str, Any]:
        from ops_app.store import apply_decision, decision_receipt, prepare_decision

        doc = self.docs[trace]
        previous = next(
            (e for e in doc["events"] if e.get("idempotency_key") == idempotency_key), None
        )
        event, is_new = prepare_decision(
            doc,
            option_id=option_id,
            decision=decision,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            actor_oid=actor_oid,
            previous_event=previous,
            comment=comment,
        )
        if is_new:
            apply_decision(doc, event)
            self._event(doc, event)
        return decision_receipt(trace, event)

    def runnable_traces(self) -> list[str]:
        from ops_app.store import RUNNABLE

        return [
            trace
            for trace, doc in self.docs.items()
            if (doc.get("execution") or {}).get("status") in RUNNABLE
        ]

    def claim_execution(self, trace: str, owner: str) -> dict[str, Any] | None:
        from ops_app.store import claim

        claimed = claim(self.docs[trace].get("execution"), owner, datetime.now(UTC))
        if claimed is None:
            return None
        self.docs[trace]["execution"] = claimed
        return self.get(trace)

    def record_review(self, trace: str, kind: str, outcome: str, actor_oid: str) -> None:
        from ops_app.store import prepare_review

        doc = self.docs[trace]
        execution, event = prepare_review(doc, kind, outcome, actor_oid)
        doc["execution"] = execution
        doc["version"] += 1
        self._event(doc, event)

    def _event(self, doc: dict[str, Any], event: dict[str, Any]) -> None:
        doc["events"].append(
            {
                **copy.deepcopy(event),
                "sequence": len(doc["events"]) + 1,
                "at": "2026-11-08T10:00:00Z",
            }
        )

    def change(self, trace: str, values: dict[str, Any], event: dict[str, Any]) -> None:
        from ops_app.store import AssessmentInactive

        doc = self.docs[trace]
        if doc["status"] != "assessing":  # as SQL: only an active assessment changes
            raise AssessmentInactive("This assessment is no longer active")
        doc.update(copy.deepcopy(values))
        doc["version"] += 1
        self._event(doc, event)

    def append_event(self, trace: str, event: dict[str, Any]) -> None:
        with self._lock:
            self._event(self.docs[trace], event)

    def save_trail(self, trace: str, trail: dict[str, Any]) -> None:
        with self._lock:
            self.docs[trace]["_trail"] = copy.deepcopy(trail)

    def update_chat(self, trace: str, update: Any) -> None:
        """As SQL: the conversation changes under the case's lock whatever its status; the
        version stays; a failed update writes nothing."""
        with self._lock:
            doc = self.docs[trace]
            doc["chat"] = copy.deepcopy(update(copy.deepcopy(doc.get("chat") or [])))

    def system_calls(self, trace: str) -> list[dict[str, Any]]:
        return copy.deepcopy(self.calls.get(trace, []))

    def mutate(
        self, trace: str, update: Any, *, statuses: frozenset[str] = frozenset({"assessing"})
    ) -> None:
        """As SQL: read, decide and write under the case's lock; a failed update writes nothing."""
        from ops_app.store import AssessmentInactive, apply_change

        with self._lock:
            doc = self.docs[trace]
            if doc["status"] not in statuses:
                raise AssessmentInactive("This assessment is no longer active")
            change = update(copy.deepcopy(doc))
            updated = copy.deepcopy(doc)
            apply_change(updated, copy.deepcopy(change), datetime.now(UTC))
            for event in change.events:
                self._event(updated, event)
            self.docs[trace] = updated

    def update_execution(
        self,
        trace: str,
        execution: dict[str, Any],
        event: dict[str, Any] | None,
        *,
        owner: str | None = None,
        release: bool = False,
        create: bool = False,
    ) -> None:
        from ops_app.store import merge_execution

        doc = self.docs[trace]
        doc["execution"] = merge_execution(
            doc.get("execution"),
            copy.deepcopy(execution),
            owner=owner,
            release=release,
            create=create,
            now=datetime.now(UTC),
        )
        doc["version"] += 1
        if event is not None:
            self._event(doc, event)
