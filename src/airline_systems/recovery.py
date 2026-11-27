"""Read-only, deterministic recovery impact checks for Crew, Booking, and Slots, and the OCC
playbook's passenger notice (decided here, written through the action service)."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Protocol, cast

from airline_systems.defects import recovery_options
from airline_systems.facts import inventory as inventory_of
from airline_systems.facts import recorded
from airline_systems.operating_day import (
    DIRECT_TRANSFER_MINUTES,
    DIRECT_TRANSFER_PASSPORT_MINUTES,
    EU261_AREA,
    HUB,
    MAX_CONNECTION,
    MIN_CONNECTION,
    distance_km,
    max_daily_fdp_minutes,
    passport_control,
)
from airline_systems.reserves import (
    ReserveService,
    StandbyAlertStore,
    called_crew,
    chosen,
    moved_duty,
    standby_left,
    too_late,
)
from airline_systems.schedule import DEMO_CLOCK, ScheduleStore, lisbon_today
from airline_systems.weather import storm_hours

_LIMITATIONS = {
    "crew": [
        "Checks only the modeled single-day FDP table and current duty assignments.",
        "Missing recorded report times or incomplete assigned crew make the duty check unknown.",
        "Rest, cumulative duty limits, days off, acclimatization, and full operating "
        "rules are not modeled.",
        "No reserve assignment or crew-change plan is generated.",
    ],
    "booking": [
        "Passenger counts, the 60-minute minimum connection and the Lisbon direct transfer "
        "(15 minutes, 20 through passport control) are scenario assumptions.",
        "No alternative itinerary, seat inventory, care, hotel, or compensation "
        "eligibility is assessed.",
    ],
    "slots": [
        "Hourly capacity is the airline's own allocation; other carriers and airport "
        "decisions are not modeled.",
        "A capacity fit does not confirm an allocated slot or airport acceptance.",
    ],
}


class RecoveryAssessmentService:
    def __init__(self, store: ScheduleStore) -> None:
        self.store = store

    def assess(
        self,
        domain: str,
        disruption: Mapping[str, Any],
        option_id: str,
        additional_delay_minutes: int = 0,
    ) -> dict[str, Any]:
        if domain not in _LIMITATIONS:
            raise ValueError(f"Unknown recovery domain: {domain}")
        if (
            additional_delay_minutes < 0
            or additional_delay_minutes > 180
            or additional_delay_minutes % 5
        ):
            raise ValueError("Additional delay must be a five-minute increment from 0 to 180.")
        day = lisbon_today()
        schedule = inventory_of(self.store, disruption, day)
        candidates = recovery_options(
            self.store, disruption, additional_delay_minutes, inventory=schedule
        )
        option = next(
            (item for item in candidates["options"] if item["option_id"] == option_id), None
        )
        if option is None:
            raise ValueError(f"Recovery option {option_id!r} is not available")
        if additional_delay_minutes and option["kind"] != "delay":
            raise ValueError("Only a delay candidate can use an additional slot adjustment.")

        inventory = self.store.domain_inventory(day, domain)
        limitations = list(_LIMITATIONS[domain])
        weather = candidates.get("weather")
        if domain == "slots" and weather:
            # Forecast weather lowers the airport's hourly capacity in its window (playbook).
            inventory = {
                **inventory,
                "reduced": {
                    (weather["airport"], hour): weather["hourly_rate"]
                    for hour in storm_hours(weather)
                },
            }
            limitations.append(
                f"In the weather window {weather['airport']} capacity is the weather playbook's"
                f" reduced rate ({weather['basis']})."
            )
        assess = {
            "crew": _assess_crew,
            "booking": _assess_booking,
            "slots": _assess_slots,
        }[domain]
        impact = assess(schedule, inventory, option, additional_delay_minutes)
        assessment = {
            "domain": domain,
            "option_id": option_id,
            "additional_delay_minutes": additional_delay_minutes,
            "constraint_status": impact.pop("constraint_status"),
            "operational_feasible": None,
            "approval_ready": False,
            "total_eur": None,
            "limitations": limitations,
            **impact,
        }
        fingerprint = json.dumps(assessment, sort_keys=True, separators=(",", ":"), default=str)
        assessment["assessment_id"] = hashlib.sha256(fingerprint.encode()).hexdigest()[:24]
        return assessment


class RecoveryHoldStore(Protocol):
    def find_recovery_hold(
        self, day: Any, trace_id: str, idempotency_key: str
    ) -> dict[str, Any] | None: ...

    def create_recovery_hold(
        self,
        *,
        day: Any,
        trace_id: str,
        option_id: str,
        source_flight_no: str,
        replacement_flight_no: str,
        locator: str,
        party_size: int,
        expected_flight_nos: tuple[str, ...],
        assessment_version: int,
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]: ...


class PassengerHoldService:
    def __init__(self, store: ScheduleStore) -> None:
        self.store = store

    def create(
        self,
        *,
        trace_id: str,
        option_id: str,
        source_flight_no: str,
        assessment_version: int,
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        day = lisbon_today()
        hold_store = cast(RecoveryHoldStore, self.store)
        prior = hold_store.find_recovery_hold(day, trace_id, idempotency_key)
        if prior is not None:
            if (
                prior["option_id"] != option_id
                or prior["source_flight_no"] != source_flight_no
                or prior["assessment_version"] != assessment_version
            ):
                raise ValueError("Idempotency key was already used for a different request")
            return prior["receipt"], False

        disruption = recorded(self.store, trace_id)
        schedule = inventory_of(self.store, disruption, day)
        candidates = recovery_options(self.store, disruption, inventory=schedule)
        option = next(
            (item for item in candidates["options"] if item["option_id"] == option_id), None
        )
        if option is None:
            raise ValueError("The requested recovery option is not available")
        baseline, proposed = _flight_maps(schedule, option)
        source = baseline.get(source_flight_no)
        proposed_source = proposed.get(source_flight_no)
        if (
            source is None
            or proposed_source is None
            or not _flight_changed(source, proposed_source)
        ):
            raise ValueError("The proposed source flight is not affected by this recovery option")

        inventory = self.store.domain_inventory(day, "booking")
        pnrs = inventory.get("pnrs")
        seat_inventory = inventory.get("inventory")
        if not isinstance(pnrs, list) or not isinstance(seat_inventory, list):
            raise ValueError("Booking inventory is incomplete; no hold was created")
        inventory_by_flight = {row["flight_no"]: row for row in seat_inventory}
        opted_in = [
            pnr
            for pnr in pnrs
            if pnr.get("synthetic_recovery_opt_in") is True
            and source_flight_no in pnr.get("flight_nos", [])
        ]
        if not opted_in:
            raise ValueError("No explicitly opted-in party is affected")

        changed_passenger_flights = {
            flight_no
            for flight_no in _changed_flights(baseline, proposed)
            if _passenger_flight_changed(baseline[flight_no], proposed[flight_no])
        }
        disrupted = [
            pnr
            for pnr in opted_in
            if _itinerary_disrupted(pnr["flight_nos"], proposed, changed_passenger_flights, day)
        ]
        if not disrupted:
            raise ValueError("The opted-in party's itinerary remains within the scenario rules")
        party = sorted(disrupted, key=lambda pnr: pnr["locator"])[0]
        party_size = party["party_size"]
        if not isinstance(party_size, int) or isinstance(party_size, bool) or party_size < 1:
            raise ValueError("Booking party size is missing; no hold was created")
        itinerary = tuple(party["flight_nos"])
        if any(flight_no not in baseline for flight_no in itinerary):
            raise ValueError("Booking itinerary data is incomplete; no hold was created")

        aircraft = {item["registration"]: item for item in schedule["aircraft"]}
        earliest = max(
            datetime.combine(day, DEMO_CLOCK), datetime.fromisoformat(source["std_local"])
        )
        alternatives = sorted(
            (
                flight
                for flight_no, flight in baseline.items()
                if flight_no != source_flight_no
                and flight.get("origin") == source.get("origin")
                and flight.get("destination") == source.get("destination")
                and flight.get("flight_date") == day.isoformat()
                and proposed[flight_no].get("status") == "scheduled"
                and flight_no not in changed_passenger_flights
                and datetime.fromisoformat(flight["std_local"]) >= earliest
                and aircraft.get(flight["aircraft_reg"], {}).get("status") == "serviceable"
            ),
            key=lambda flight: (flight["std_local"], flight["flight_no"]),
        )
        replacement = None
        for flight in alternatives:
            row = inventory_by_flight.get(flight["flight_no"])
            if row is None:
                continue
            seats = row.get("seats")
            passengers = row.get("passengers")
            held_seats = row.get("held_seats")
            if any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in (seats, passengers, held_seats)
            ):
                continue
            if seats - passengers - held_seats < party_size:
                continue
            replacement_itinerary = tuple(
                flight["flight_no"] if flight_no == source_flight_no else flight_no
                for flight_no in itinerary
            )
            replacement_schedule = {**proposed, flight["flight_no"]: flight}
            if _valid_itinerary(replacement_itinerary, replacement_schedule, day):
                replacement = flight
                break
        if replacement is None:
            raise ValueError(
                "No same-day alternative with available seats preserves the itinerary rules"
            )

        return hold_store.create_recovery_hold(
            day=day,
            trace_id=trace_id,
            option_id=option_id,
            source_flight_no=source_flight_no,
            replacement_flight_no=replacement["flight_no"],
            locator=party["locator"],
            party_size=party_size,
            expected_flight_nos=itinerary,
            assessment_version=assessment_version,
            idempotency_key=idempotency_key,
        )


# Placeholder policy limit (spec D12): same day, own flights, departing at most 6 h later.
# Passengers beyond it go to a Passenger Services expert instead of automatic rebooking.
REBOOK_WITHIN = timedelta(hours=6)
# Time a crew needs to change aircraft between two legs (scenario assumption; the generated
# day keeps each crew with its aircraft).
CREW_AIRCRAFT_CHANGE = timedelta(minutes=30)


class RebookingService:
    """Same-day rebooking plan for passengers a recovery option disrupts. Deterministic.

    Parties are placed in locator order on the earliest valid alternative with free seats,
    replacing either the changed flight or the connection it now misses.
    """

    def __init__(self, store: ScheduleStore) -> None:
        self.store = store

    def plan(
        self, disruption: Mapping[str, Any], option_id: str, additional_delay_minutes: int = 0
    ) -> dict[str, Any]:
        day = lisbon_today()
        schedule = inventory_of(self.store, disruption, day)
        candidates = recovery_options(
            self.store, disruption, additional_delay_minutes, inventory=schedule
        )
        option = next(
            (item for item in candidates["options"] if item["option_id"] == option_id), None
        )
        if option is None:
            raise ValueError(f"Recovery option {option_id!r} is not available")
        result = self.plan_for(option["flights"], schedule=schedule)
        return {
            **result,
            "option_id": option_id,
            "additional_delay_minutes": additional_delay_minutes,
        }

    def plan_for(
        self,
        flights: list[dict[str, Any]],
        *,
        originals: list[dict[str, Any]] | None = None,
        schedule: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Plan for explicit proposed flights against their originals (or today's schedule)."""
        day = lisbon_today()
        schedule = schedule or self.store.recovery_inventory(day)
        current = {flight["flight_no"]: flight for flight in schedule["flights"]}
        before = {
            **current,
            **{f["flight_no"]: {**current.get(f["flight_no"], {}), **f} for f in originals or []},
        }
        baseline, proposed = _flight_maps({"flights": list(before.values())}, {"flights": flights})
        changed = {
            flight_no
            for flight_no in _changed_flights(baseline, proposed)
            if _passenger_flight_changed(baseline[flight_no], proposed[flight_no])
        }
        inventory = self.store.domain_inventory(day, "booking")
        free = {
            row["flight_no"]: row["seats"] - row["passengers"] - row.get("held_seats", 0)
            for row in inventory["inventory"]
        }
        serviceable = {
            item["registration"]
            for item in schedule["aircraft"]
            if item.get("status") == "serviceable"
        }
        earliest = datetime.combine(day, DEMO_CLOCK)
        assignments: list[dict[str, Any]] = []
        unplaced: list[dict[str, Any]] = []
        # Each affected booking's journey facts for ARROW's cost estimate (no booking reference);
        # None when any of them cannot be established, so the cost is unknown rather than low.
        journeys: list[dict[str, Any]] | None = []
        # Connections a car keeps (D108), per flight pair; no booking reference.
        cars: dict[tuple[str, str], dict[str, Any]] = {}
        disrupted_passengers = 0
        for pnr in sorted(inventory["pnrs"], key=lambda item: item["locator"]):
            itinerary = tuple(pnr["flight_nos"])
            party = pnr["party_size"]
            try:
                disrupted = _itinerary_broken(itinerary, proposed, changed, day)
            except ValueError:
                disrupted, replacement = True, None
                journeys = None
            else:
                replacement = (
                    _replacement(
                        itinerary,
                        party,
                        proposed,
                        baseline,
                        changed,
                        free,
                        serviceable,
                        earliest,
                        day,
                    )
                    if disrupted
                    else None
                )
            if journeys is not None and (disrupted or changed.intersection(itinerary)):
                try:
                    journeys.append(
                        _journey(itinerary, party, baseline, proposed, replacement, disrupted)
                    )
                except (KeyError, ValueError):
                    journeys = None
            if not disrupted:
                if changed.intersection(itinerary):
                    for inbound, outbound, minutes, passport in _transfers(itinerary, proposed):
                        group = cars.setdefault(
                            (inbound, outbound),
                            {
                                "from_flight_no": inbound,
                                "to_flight_no": outbound,
                                "passengers": 0,
                                "minutes": minutes,
                                "passport_control": passport,
                            },
                        )
                        group["passengers"] += party
                continue
            disrupted_passengers += party
            if replacement is None:
                unplaced.append({"locator": pnr["locator"], "party_size": party})
                continue
            position, flight_no = replacement
            free[flight_no] -= party
            assignments.append(
                {
                    "locator": pnr["locator"],
                    "party_size": party,
                    "from_flight_no": itinerary[position],
                    "to_flight_no": flight_no,
                }
            )
        digest = hashlib.sha256(
            json.dumps(assignments, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        seats = {row["flight_no"]: row["seats"] for row in inventory["inventory"]}
        return {
            "disrupted_passengers": disrupted_passengers,
            "rebooked_passengers": sum(item["party_size"] for item in assignments),
            "unplaced_passengers": sum(item["party_size"] for item in unplaced),
            "transferred_passengers": sum(group["passengers"] for group in cars.values()),
            "transfers": [cars[key] for key in sorted(cars)],
            "plan_digest": digest,
            "assignments": assignments,
            "journeys": journeys,
            "seats": {
                flight_no: seats[flight_no] for flight_no in sorted(changed) if flight_no in seats
            },
            "checked_at": datetime.now().astimezone().isoformat(),
        }


class DutyPlanService:
    """Crew duty changes a recovery option implies, with a digest the crew write depends on.

    Each affected duty's release moves by its last flown leg's change; emptied duties are only
    flagged for re-planning. A plan may call several standby crews, one per breached duty and
    nobody twice; one digest binds the write to every duty ARROW validated. ``standby_left``
    says how many complete standby crews of each base and qualification stay callable after it.
    """

    def __init__(self, store: ScheduleStore) -> None:
        self.store = store

    def plan(
        self,
        disruption: Mapping[str, Any],
        option_id: str,
        additional_delay_minutes: int = 0,
        reserve_option_id: str | None = None,
        *,
        reserve_option_ids: Sequence[str] = (),
        caller: str | None = None,
    ) -> dict[str, Any]:
        """``caller``: the case asking (its trace, from the call's traceparent); a standby crew
        another case has called is not offered to it."""
        schedule = inventory_of(self.store, disruption)
        candidates = recovery_options(
            self.store, disruption, additional_delay_minutes, inventory=schedule
        )
        option = next(
            (item for item in candidates["options"] if item["option_id"] == option_id), None
        )
        if option is None:
            raise ValueError(f"Recovery option {option_id!r} is not available")
        ids = chosen(reserve_option_ids, reserve_option_id)
        reserves: list[dict[str, Any]] = []
        if ids:
            offered = ReserveService(self.store).options_for(
                option["flights"], schedule=schedule, trace_id=caller
            )
            by_id = {item["reserve_option_id"]: item for item in offered["options"]}
            missing = [item for item in ids if item not in by_id]
            if missing:
                raise ValueError(f"Reserve option {missing[0]!r} is not available")
            reserves = [by_id[item] for item in ids]
        return {
            **self.plan_for(option["flights"], schedule=schedule, reserves=reserves),
            "option_id": option_id,
            "additional_delay_minutes": additional_delay_minutes,
            # The first, for ARROW before several standby crews; reserve_option_ids has them all.
            "reserve_option_id": ids[0] if ids else None,
            "reserve_option_ids": ids,
        }

    def plan_for(
        self,
        flights: list[dict[str, Any]],
        *,
        originals: list[dict[str, Any]] | None = None,
        schedule: dict[str, Any] | None = None,
        reserves: Sequence[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        day = lisbon_today()
        schedule = schedule or self.store.recovery_inventory(day)
        proposed = {flight["flight_no"]: flight for flight in flights}
        before = {flight["flight_no"]: flight for flight in originals or []}
        current = {flight["flight_no"]: flight for flight in schedule["flights"]}
        crew = self.store.domain_inventory(day, "crew")
        by_duty = {reserve["duty_id"]: reserve for reserve in reserves}
        called = [member["crew_id"] for reserve in reserves for member in reserve["crew"]]
        if len(by_duty) != len(reserves) or len(set(called)) != len(called):
            raise ValueError("A plan calls one standby crew per duty and each member once")
        changes: list[dict[str, Any]] = []
        notes: list[str] = []
        assigned: list[str] = []
        breaches = 0
        for duty in crew["duties"]:
            legs = duty["flight_nos"]
            if not proposed.keys() & set(legs):
                continue
            moved = moved_duty(duty, proposed, current, before)
            if moved is None:
                notes.append(f"{duty['duty_id']} needs re-plan")
                continue
            reserve = by_duty.pop(duty["duty_id"], None)
            if reserve is not None:
                late = too_late(reserve)
                if late is None:
                    split, note, unresolved = _reserve_changes(
                        duty, moved, reserve, crew["reserves"]
                    )
                    changes.extend(split)
                    notes.append(note)
                    assigned += [member["crew_id"] for member in reserve["crew"]]
                    breaches += unresolved
                    continue
                # The crew cannot make the departure: the duty keeps its breach and its crew.
                notes.append(late)
                breaches += 1
            elif moved["fdp_minutes"] > moved["max_fdp_minutes"]:
                breaches += 1
            release = moved["release"]
            if release.isoformat() == duty["release_local"]:
                notes.append(f"{duty['duty_id']} unchanged")
                continue
            changes.append(
                {
                    "table": "duty",
                    "key": {"duty_id": duty["duty_id"]},
                    "expected": {"release_local": duty["release_local"]},
                    "values": {
                        "release_local": release.isoformat(),
                        "fdp_minutes": moved["fdp_minutes"],
                    },
                }
            )
            notes.append(f"{duty['duty_id']} release {release:%H:%M}")
        if by_duty:
            raise ValueError(f"No duty of this plan for standby crew {sorted(by_duty)[0]}")
        digest = hashlib.sha256(
            json.dumps(changes, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        # A standby crew a case has called is used; one only on alert is not (sql/019).
        called = called_crew(cast(StandbyAlertStore, self.store).standby_alerts(day))
        return {
            "changes": changes,
            "duties_changed": sum(1 for change in changes if change["table"] == "duty"),
            "breaches_unresolved": breaches,
            "message": f"Crew duties: {'; '.join(notes) or 'none affected'}",
            "plan_digest": digest,
            "standby_left": standby_left(
                crew, [*assigned, *called], datetime.combine(day, DEMO_CLOCK)
            ),
        }


def _reserve_changes(
    duty: dict[str, Any],
    moved: dict[str, Any],
    reserve: dict[str, Any],
    standby: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str, bool]:
    """Rows that hand the late legs to the reserve crew and shorten the original duty."""
    duty_id = duty["duty_id"]
    reserve_duty = f"{duty_id}-{reserve['reserve_option_id']}"
    rows = {row["crew_id"]: row for row in standby}
    changes: list[dict[str, Any]] = []
    unresolved = False
    split = reserve["split_after_flight"]
    if split is not None:
        last = next(leg for leg in moved["legs"] if leg["flight_no"] == split)
        on_blocks = datetime.fromisoformat(last["sta_local"])
        release = on_blocks + moved["post_flight"]
        fdp = int((on_blocks - datetime.fromisoformat(duty["report_local"])).total_seconds() // 60)
        unresolved = fdp > moved["max_fdp_minutes"]
        if not isinstance(duty.get("fdp_minutes"), int):
            raise ValueError(f"Crew data has no FDP for duty {duty_id}")
        changes.append(
            {
                "op": "update",
                "table": "duty",
                "key": {"duty_id": duty_id},
                "expected": {
                    "release_local": duty["release_local"],
                    "fdp_minutes": duty["fdp_minutes"],
                },
                "values": {"release_local": release.isoformat(), "fdp_minutes": fdp},
            }
        )
    first = next(
        leg for leg in moved["legs"] if leg["flight_no"] == reserve["flights_for_reserve"][0]
    )
    changes.append(
        {
            "op": "insert",
            "table": "duty",
            "key": {"duty_id": reserve_duty},
            "expected": {},
            "values": {
                "base": duty["base"],
                "start_airport": first["origin"],
                "qualification": duty["qualification"],
                "report_local": reserve["report_local"],
                "release_local": reserve["release_local"],
                "fdp_minutes": reserve["fdp_minutes"],
                "max_fdp_minutes": reserve["max_fdp_minutes"],
                "pilots_required": duty["pilots_required"],
                "cabin_required": duty["cabin_required"],
            },
        }
    )
    for flight_no in reserve["flights_for_reserve"]:
        changes.append(
            {
                "op": "update",
                "table": "duty_flight",
                "key": {"flight_no": flight_no},
                "expected": {"duty_id": duty_id},
                "values": {"duty_id": reserve_duty},
            }
        )
    for member in reserve["crew"]:
        row = rows.get(member["crew_id"])
        if row is None:
            raise ValueError(f"Reserve {member['crew_id']} is no longer on standby")
        changes.append(
            {
                "op": "insert",
                "table": "assignment",
                "key": {"crew_id": member["crew_id"]},
                "expected": {},
                "values": {"duty_id": reserve_duty, "rank": member["rank"]},
            }
        )
        changes.append(
            {
                "op": "delete",
                "table": "reserve",
                "key": {"crew_id": member["crew_id"]},
                "expected": {
                    "base": row["base"],
                    "start_local": row["start_local"],
                    "end_local": row["end_local"],
                },
                "values": {},
            }
        )
    note = (
        f"{duty_id} hands {', '.join(reserve['flights_for_reserve'])} to reserve duty "
        f"{reserve_duty} ({reserve['report_local'][11:16]}-{reserve['release_local'][11:16]})"
    )
    return changes, note, unresolved


def _replacement(
    itinerary: tuple[str, ...],
    party: int,
    proposed: dict[str, dict[str, Any]],
    baseline: dict[str, dict[str, Any]],
    changed: set[str],
    free: dict[str, int],
    serviceable: set[str],
    earliest: datetime,
    day: Any,
) -> tuple[int, str] | None:
    for position, flight_no in enumerate(itinerary):
        previous_changed = position > 0 and itinerary[position - 1] in changed
        if flight_no not in changed and not previous_changed:
            continue
        original = baseline[flight_no]
        not_before = max(earliest, datetime.fromisoformat(original["std_local"]))
        policy_limit = datetime.fromisoformat(original["std_local"]) + REBOOK_WITHIN
        alternatives = sorted(
            (
                flight
                for number, flight in proposed.items()
                if number not in itinerary
                and number not in changed
                and flight["origin"] == original["origin"]
                and flight["destination"] == original["destination"]
                and flight.get("status") == "scheduled"
                and flight.get("aircraft_reg") in serviceable
                and not_before <= datetime.fromisoformat(flight["std_local"]) <= policy_limit
                and free.get(number, 0) >= party
            ),
            key=lambda flight: (flight["std_local"], flight["flight_no"]),
        )
        for flight in alternatives:
            candidate = (*itinerary[:position], flight["flight_no"], *itinerary[position + 1 :])
            if _valid_itinerary(candidate, proposed, day):
                return position, flight["flight_no"]
    return None


def _journey(
    itinerary: tuple[str, ...],
    party: int,
    baseline: dict[str, dict[str, Any]],
    proposed: dict[str, dict[str, Any]],
    replacement: tuple[int, str] | None,
    broken: bool,
) -> dict[str, Any]:
    """One affected booking's journey facts for ARROW's cost estimate (passenger cost spec §3).

    The disrupted position is the replaced flight; for a broken journey without a rebooking,
    the first flight the passenger can no longer take; otherwise the most delayed flight. A
    rebooked journey also carries what the passenger would face without the rebooking.
    ``departs`` and ``arrives`` are the published times (review): after an earlier retime
    the passenger's delay still counts from the timetable they booked (EU261).
    """
    before = [baseline[flight_no] for flight_no in itinerary]
    if broken and replacement is None:
        stranded = _stranded(itinerary, baseline, proposed)
        return {
            "party_size": party,
            **stranded,
            "cancelled": any(proposed[number]["status"] == "cancelled" for number in itinerary),
            "rebooked": False,
            "departs_new": None,
            "arrives": before[-1]["published_sta_local"],
            "arrives_new": None,
            **_distances(before[0], before[-1], "journey"),
        }
    after = [proposed[number] for number in itinerary]
    if replacement is not None:
        position, flight_no = replacement
        after[position] = proposed[flight_no]
    else:
        position = max(
            range(len(itinerary)),
            key=lambda i: (
                _minutes_late(after[i]["std_local"], before[i]["published_std_local"]),
                -i,
            ),
        )
    flight = before[position]
    journey = {
        "party_size": party,
        "first_flight": position == 0,
        "cancelled": any(proposed[number]["status"] == "cancelled" for number in itinerary),
        "rebooked": replacement is not None,
        "departs": flight["published_std_local"],
        "departs_new": after[position]["std_local"],
        "next_departure": None,
        "arrives": before[-1]["published_sta_local"],
        "arrives_new": after[-1]["sta_local"],
        **_distances(flight, flight, "flight"),
        **_distances(before[0], before[-1], "journey"),
    }
    if replacement is not None:
        journey["not_rebooked"] = _stranded(itinerary, baseline, proposed)
    return journey


def _stranded(
    itinerary: tuple[str, ...],
    baseline: dict[str, dict[str, Any]],
    proposed: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Where a passenger without a rebooking is stuck: the first flight they can no longer take,
    and the next flight on its route they could still reach (60-minute connection), if any."""
    position = _first_broken(itinerary, proposed)
    missed = baseline[itinerary[position]]
    ready = missed["std_local"]
    if position:
        landed = datetime.fromisoformat(proposed[itinerary[position - 1]]["sta_local"])
        ready = max(ready, (landed + timedelta(minutes=MIN_CONNECTION)).isoformat())
    return {
        "first_flight": position == 0,
        "departs": missed["published_std_local"],
        "next_departure": _next_departure(missed, itinerary, proposed, ready),
        **_distances(missed, missed, "flight"),
    }


def _distances(start: dict[str, Any], end: dict[str, Any], prefix: str) -> dict[str, Any]:
    origin, destination = start["origin"], end["destination"]
    return {
        f"{prefix}_km": distance_km(origin, destination),
        f"{prefix}_eu": origin in EU261_AREA and destination in EU261_AREA,
    }


def _minutes_late(later: str, earlier: str) -> int:
    return round(
        (datetime.fromisoformat(later) - datetime.fromisoformat(earlier)).total_seconds() / 60
    )


def _first_broken(itinerary: tuple[str, ...], proposed: dict[str, dict[str, Any]]) -> int:
    flights = [proposed[flight_no] for flight_no in itinerary]
    for index, flight in enumerate(flights):
        if flight["status"] != "scheduled":
            return index
        if index and _connection(flights[index - 1], flight) == "broken":
            return index
    raise ValueError("Broken journey without a cancelled flight or short connection")


def _connection(inbound: dict[str, Any], outbound: dict[str, Any]) -> str:
    """``held``, ``car`` (a direct transfer at the hub keeps it, D108) or ``broken``."""
    minutes = _minutes_late(outbound["std_local"], inbound["sta_local"])
    if minutes >= MIN_CONNECTION:
        return "held"
    needed = (
        DIRECT_TRANSFER_PASSPORT_MINUTES
        if passport_control(inbound["origin"], outbound["destination"])
        else DIRECT_TRANSFER_MINUTES
    )
    return "car" if inbound["destination"] == HUB and minutes >= needed else "broken"


def _transfers(
    itinerary: tuple[str, ...], proposed: dict[str, dict[str, Any]]
) -> list[tuple[str, str, int, bool]]:
    """The connections of a journey only a car keeps: inbound, outbound, minutes, passport."""
    flights = [proposed[flight_no] for flight_no in itinerary]
    return [
        (
            inbound["flight_no"],
            outbound["flight_no"],
            _minutes_late(outbound["std_local"], inbound["sta_local"]),
            passport_control(inbound["origin"], outbound["destination"]),
        )
        for inbound, outbound in zip(flights, flights[1:])
        if _connection(inbound, outbound) == "car"
    ]


def _next_departure(
    flight: dict[str, Any],
    itinerary: tuple[str, ...],
    proposed: dict[str, dict[str, Any]],
    ready: str,
) -> str | None:
    """The next scheduled departure on the flight's route the passenger can still reach (from
    ``ready``), whatever its free seats."""
    later = [
        other["std_local"]
        for number, other in proposed.items()
        if number not in itinerary
        and other["origin"] == flight["origin"]
        and other["destination"] == flight["destination"]
        and other.get("status") == "scheduled"
        and other["std_local"] >= ready
    ]
    return min(later, default=None)


def _flight_changed(baseline: dict[str, Any], proposed: dict[str, Any]) -> bool:
    fields = ("status", "std_local", "sta_local")
    if any(field not in baseline or field not in proposed for field in fields):
        raise ValueError("Flight impact data is incomplete")
    return any(baseline[field] != proposed[field] for field in fields)


def _passenger_flight_changed(baseline: dict[str, Any], proposed: dict[str, Any]) -> bool:
    fields = ("status", "std_local", "sta_local")
    if any(field not in baseline or field not in proposed for field in fields):
        raise ValueError("Passenger flight impact data is incomplete")
    return any(baseline[field] != proposed[field] for field in fields)


def _itinerary_broken(
    flight_nos: list[str] | tuple[str, ...],
    proposed: dict[str, dict[str, Any]],
    changed_flights: set[str],
    day: Any,
) -> bool:
    """A cancelled segment or a connection neither the minimum nor a car keeps (D108); a longer
    layover still travels."""
    if not changed_flights.intersection(flight_nos):
        return False
    flights = [proposed.get(flight_no) for flight_no in flight_nos]
    if any(flight is None for flight in flights):
        raise ValueError("Booking itinerary data is incomplete")
    itinerary = [flight for flight in flights if flight is not None]
    if any(flight.get("flight_date") != day.isoformat() for flight in itinerary):
        raise ValueError("Booking itinerary is not confined to the operating day")
    if any(flight.get("status") != "scheduled" for flight in itinerary):
        return True
    return any(
        _connection(current, following) == "broken"
        for current, following in zip(itinerary, itinerary[1:])
    )


def _itinerary_disrupted(
    flight_nos: list[str] | tuple[str, ...],
    proposed: dict[str, dict[str, Any]],
    changed_flights: set[str],
    day: Any,
) -> bool:
    if not changed_flights.intersection(flight_nos):
        return False
    flights = [proposed.get(flight_no) for flight_no in flight_nos]
    if any(flight is None for flight in flights):
        raise ValueError("Booking itinerary data is incomplete; no hold was created")
    itinerary = [flight for flight in flights if flight is not None]
    if any(flight.get("flight_date") != day.isoformat() for flight in itinerary):
        raise ValueError("Booking itinerary is not confined to the operating day")
    if any(flight.get("status") != "scheduled" for flight in itinerary):
        return True
    return not _valid_itinerary(tuple(flight_nos), proposed, day)


def _valid_itinerary(
    flight_nos: tuple[str, ...],
    flights_by_no: dict[str, dict[str, Any]],
    day: Any,
) -> bool:
    flights = [flights_by_no.get(flight_no) for flight_no in flight_nos]
    if any(flight is None for flight in flights):
        return False
    itinerary = [flight for flight in flights if flight is not None]
    if any(
        flight.get("flight_date") != day.isoformat() or flight.get("status") != "scheduled"
        for flight in itinerary
    ):
        return False
    for current, following in zip(itinerary, itinerary[1:]):
        if current["destination"] != following["origin"]:
            return False
        connection = datetime.fromisoformat(following["std_local"]) - datetime.fromisoformat(
            current["sta_local"]
        )
        if not timedelta(minutes=MIN_CONNECTION) <= connection <= timedelta(minutes=MAX_CONNECTION):
            return False
    return True


def _flight_maps(
    schedule: dict[str, Any], option: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    baseline = {flight["flight_no"]: flight for flight in schedule["flights"]}
    proposed = {**baseline}
    for flight in option["flights"]:
        proposed[flight["flight_no"]] = {**baseline[flight["flight_no"]], **flight}
    return baseline, proposed


def _changed_flights(
    baseline: dict[str, dict[str, Any]], proposed: dict[str, dict[str, Any]]
) -> set[str]:
    return {
        flight_no
        for flight_no, flight in proposed.items()
        if (
            flight["status"] != baseline[flight_no]["status"]
            or flight["aircraft_reg"] != baseline[flight_no]["aircraft_reg"]
            or flight["std_local"] != baseline[flight_no]["std_local"]
            or flight["sta_local"] != baseline[flight_no]["sta_local"]
        )
    }


def _broken_connection(legs: list[dict[str, Any]]) -> str | None:
    """The first leg the crew cannot reach: it leaves from another airport, or before the
    previous leg lands (plus CREW_AIRCRAFT_CHANGE when the aircraft changes); else None."""
    ordered = sorted(legs, key=lambda leg: leg["std_local"])
    for before, after in zip(ordered, ordered[1:], strict=False):
        landed = datetime.fromisoformat(before["sta_local"])
        leaves = datetime.fromisoformat(after["std_local"])
        change = (
            CREW_AIRCRAFT_CHANGE
            if after.get("aircraft_reg") != before.get("aircraft_reg")
            else timedelta()
        )
        if after["origin"] != before["destination"] or leaves < landed + change:
            return (
                f"{before['flight_no']} lands at {before['destination']} {landed:%H:%M};"
                f" {after['flight_no']} leaves {after['origin']} {leaves:%H:%M}"
                f" on {after.get('aircraft_reg')}"
            )
    return None


def _assess_crew(
    schedule: dict[str, Any],
    inventory: dict[str, Any],
    option: dict[str, Any],
    _: int,
) -> dict[str, Any]:
    baseline, proposed = _flight_maps(schedule, option)
    changed = _changed_flights(baseline, proposed)
    duties: list[dict[str, Any]] = []
    statuses: list[str] = []
    covered_flights = {
        flight_no for duty in inventory["duties"] for flight_no in duty["flight_nos"]
    }
    for duty in inventory["duties"]:
        flight_nos = set(duty["flight_nos"])
        if not flight_nos.intersection(changed):
            continue
        legs = [
            proposed[flight_no]
            for flight_no in duty["flight_nos"]
            if proposed[flight_no]["status"] != "cancelled"
        ]
        if not legs:
            duties.append(
                {
                    "duty_id": duty["duty_id"],
                    "status": "removed_by_cancellation",
                    "sectors": None,
                    "pilots": None,
                    "modeled_fdp_minutes": None,
                    "modeled_fdp_limit_minutes": None,
                }
            )
            statuses.append("unknown")
            continue
        members = duty.get("members", [])
        ranks = Counter(member["rank"] for member in members)
        pilots = ranks["CPT"] + ranks["FO"]
        required_pilots = duty.get("pilots_required")
        required_cabin = duty.get("cabin_required")
        complete = (
            isinstance(required_pilots, int)
            and required_pilots >= 2
            and isinstance(required_cabin, int)
            and required_cabin >= 1
            and ranks["CPT"] == 1
            and ranks["FO"] == required_pilots - 1
            and ranks["SC"] == 1
            and ranks["CC"] == required_cabin - 1
            and len({member["crew_id"] for member in members}) == len(members)
        )
        report_value = duty.get("report_local")
        try:
            report = datetime.fromisoformat(report_value) if isinstance(report_value, str) else None
        except ValueError:
            report = None
        fdp = max_fdp = None
        status = "unknown"
        first_departure = min(datetime.fromisoformat(f["std_local"]) for f in legs)
        # Each leg leaves from where the crew's previous leg landed, after it landed (plus time
        # to change aircraft): a swap can hand a flight to an aircraft the crew cannot reach
        # (review). Only breaks the option makes count.
        connection = _broken_connection(legs)
        if connection and _broken_connection(
            [baseline[f["flight_no"]] for f in legs if f["flight_no"] in baseline]
        ):
            connection = None
        if complete and report is not None and report <= first_departure:
            # The existing duty has already started; delay is not a new reporting agreement.
            last_arrival = max(datetime.fromisoformat(f["sta_local"]) for f in legs)
            fdp = int((last_arrival - report).total_seconds() // 60)
            max_fdp = max_daily_fdp_minutes(report.time(), len(legs), pilots)
            status = "pass" if fdp <= max_fdp else "fail"
        if connection:
            status = "fail"
        statuses.append(status)
        duties.append(
            {
                "duty_id": duty["duty_id"],
                "status": status,
                "sectors": len(legs),
                "pilots": pilots,
                "modeled_fdp_minutes": fdp,
                "modeled_fdp_limit_minutes": max_fdp,
                "connection": connection,
            }
        )
    if changed.difference(covered_flights):
        statuses.append("unknown")
    schedule_changed = any(
        flight["status"] != baseline[flight_no]["status"]
        or flight["std_local"] != baseline[flight_no]["std_local"]
        or flight["sta_local"] != baseline[flight_no]["sta_local"]
        for flight_no, flight in proposed.items()
        if flight_no in changed
    )
    overall = (
        "fail"
        if "fail" in statuses
        else "unknown"
        if "unknown" in statuses or (schedule_changed and not duties)
        else "pass"
    )
    return {
        "constraint_status": overall,
        "changed_flight_ids": sorted(changed),
        "duties": duties,
        "crew_changes": None,
    }


def _assess_booking(
    schedule: dict[str, Any],
    inventory: dict[str, Any],
    option: dict[str, Any],
    _: int,
) -> dict[str, Any]:
    baseline, proposed = _flight_maps(schedule, option)
    changed = _changed_flights(baseline, proposed)
    passenger_changed = {
        flight_no
        for flight_no in changed
        if (
            proposed[flight_no]["status"] != baseline[flight_no]["status"]
            or proposed[flight_no]["std_local"] != baseline[flight_no]["std_local"]
            or proposed[flight_no]["sta_local"] != baseline[flight_no]["sta_local"]
        )
    }
    affected: set[str] = set()
    disrupted: set[str] = set()
    missing_flight_data = False
    for pnr in inventory["pnrs"]:
        itinerary = pnr["flight_nos"]
        if not passenger_changed.intersection(itinerary):
            continue
        affected.add(pnr["locator"])
        flights = [proposed.get(flight_no) for flight_no in itinerary]
        if any(flight is None for flight in flights):
            missing_flight_data = True
            continue
        itinerary_flights = [flight for flight in flights if flight is not None]
        for index, flight in enumerate(itinerary_flights):
            if flight["status"] == "cancelled":
                disrupted.add(pnr["locator"])
                break
            if index == len(itinerary_flights) - 1:
                continue
            following = itinerary_flights[index + 1]
            if flight["destination"] != following["origin"]:
                missing_flight_data = True
                continue
            if _connection(flight, following) == "broken":
                disrupted.add(pnr["locator"])
                break
    party_sizes = {pnr["locator"]: pnr["party_size"] for pnr in inventory["pnrs"]}
    status = "unknown" if missing_flight_data else "fail" if disrupted else "pass"
    return {
        "constraint_status": status,
        "changed_flight_ids": sorted(changed),
        "affected_passengers": sum(party_sizes[locator] for locator in affected),
        "misconnected_passengers": sum(party_sizes[locator] for locator in disrupted),
        "disrupted_booking_count": len(disrupted),
        "reaccommodation_status": "not_assessed",
    }


def _slot_conflicts(
    inventory: dict[str, Any],
    baseline: dict[str, dict[str, Any]],
    proposed: dict[str, dict[str, Any]],
    changed: set[str],
) -> tuple[list[dict[str, Any]], bool]:
    """Over-capacity airport-hours this option causes: an hour one of its changed flights uses,
    over capacity and busier than before the option. An hour already full from earlier changes
    is this option's conflict only if it adds to it. A storm's reduced hours (``reduced`` in the
    inventory) are always checked, against the reduced rate, however full they were before."""
    reduced: dict[tuple[str, datetime], int] = inventory.get("reduced", {})
    slots_by_flight: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for slot in inventory["slots"]:
        slots_by_flight[slot["flight_no"]].append(slot)
    missing = False

    def hourly(flights: dict[str, dict[str, Any]]) -> Counter[tuple[str, datetime]]:
        nonlocal missing
        counts: Counter[tuple[str, datetime]] = Counter()
        for flight_no, flight_slots in slots_by_flight.items():
            flight = flights.get(flight_no)
            if flight is None:
                missing = True
                continue
            if flight["status"] == "cancelled":
                continue
            for slot in flight_slots:
                counts[_slot_hour(slot, flight)] += 1
        return counts

    after, before = hourly(proposed), hourly(baseline)
    for flight_no, flight in proposed.items():
        if flight["status"] == "cancelled":
            continue
        expected = (
            (flight["origin"], "D", "std_local"),
            (flight["destination"], "A", "sta_local"),
        )
        for airport, movement, _ in expected:
            if airport in inventory["capacity"] and not any(
                slot["airport"] == airport and slot["movement"] == movement
                for slot in slots_by_flight.get(flight_no, [])
            ):
                missing = True
    touched = {
        _slot_hour(slot, proposed[flight_no])
        for flight_no in changed
        if flight_no in proposed and proposed[flight_no]["status"] != "cancelled"
        for slot in slots_by_flight.get(flight_no, [])
    } | set(reduced)
    conflicts = []
    for airport, hour in sorted(touched):
        capacity = reduced.get((airport, hour), inventory["capacity"][airport])
        movements = after[(airport, hour)]
        if movements > capacity and (
            movements > before[(airport, hour)] or (airport, hour) in reduced
        ):
            conflicts.append(
                {
                    "airport": airport,
                    "hour_local": hour.isoformat(),
                    "movements": movements,
                    "hourly_capacity": capacity,
                }
            )
    return conflicts, missing


def _slot_hour(slot: dict[str, Any], flight: dict[str, Any]) -> tuple[str, datetime]:
    """The airport-hour a flight's slot uses at the flight's (proposed) time."""
    at = datetime.fromisoformat(flight["std_local" if slot["movement"] == "D" else "sta_local"])
    return slot["airport"], at.replace(minute=0, second=0, microsecond=0)


# A flight leaving more than this many minutes after its scheduled off-block time needs a delay
# message (an ICAO DLA to EUROCONTROL's Network Manager); ARROW checks a plan's delay_messages
# lever against the same threshold (ops_app.actions.EOBT_MESSAGE_MINUTES).
DELAY_MESSAGE_MINUTES = 15


def _assess_slots(
    schedule: dict[str, Any],
    inventory: dict[str, Any],
    option: dict[str, Any],
    additional_delay_minutes: int,
) -> dict[str, Any]:
    baseline, proposed = _flight_maps(schedule, option)
    changed = _changed_flights(baseline, proposed)
    conflicts, missing = _slot_conflicts(inventory, baseline, proposed, changed)
    recommended = additional_delay_minutes if not conflicts and not missing else None
    # The flights at the delay the option needs to fit capacity, the one a plan settles on.
    fitted = proposed
    if conflicts and not missing and option["kind"] == "delay":
        for extra in range(5, 181 - additional_delay_minutes, 5):
            shifted = {
                **proposed,
                **{
                    flight_no: {
                        **flight,
                        "std_local": (
                            datetime.fromisoformat(flight["std_local"]) + timedelta(minutes=extra)
                        ).isoformat(),
                        "sta_local": (
                            datetime.fromisoformat(flight["sta_local"]) + timedelta(minutes=extra)
                        ).isoformat(),
                    }
                    for flight_no, flight in proposed.items()
                    if flight_no in changed and flight["status"] != "cancelled"
                },
            }
            candidate_conflicts, candidate_missing = _slot_conflicts(
                inventory, baseline, shifted, changed
            )
            if not candidate_missing and not candidate_conflicts:
                recommended = additional_delay_minutes + extra
                fitted = shifted
                break
    # The departures leaving later than the delay-message threshold there: a fact of the
    # option's flights, so the Slot agent never has to judge it from a description.
    moved = [
        flight_no
        for flight_no in changed
        if fitted[flight_no]["status"] != "cancelled"
        and _minutes_late(fitted[flight_no]["std_local"], baseline[flight_no]["std_local"])
        > DELAY_MESSAGE_MINUTES
    ]
    status = "unknown" if missing else "fail" if conflicts else "pass"
    result: dict[str, Any] = {
        "constraint_status": status,
        "changed_flight_ids": sorted(changed),
        "slot_conflicts": conflicts,
        "required_additional_delay_minutes": recommended,
        "capacity_fit": not conflicts and not missing,
        "delay_messages_required": bool(moved),
        "departures_moved_over_15_min": len(moved),
    }
    if reduced := inventory.get("reduced"):
        result["reduced_capacity"] = [
            {"airport": airport, "hour_local": hour.isoformat(), "hourly_capacity": capacity}
            for (airport, hour), capacity in sorted(reduced.items())
        ]
    return result


NOTICE_KINDS = ("possible_delay", "new_times", "no_change")


def passenger_notice(
    store: ScheduleStore,
    notice_id: str,
    trace_id: str,
    flight_nos: Sequence[str],
    notified_before: Collection[str],
    kind: str = "new_times",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The OCC playbook's passenger notice: Booking tells the passengers of flights yet to leave
    of possible delays (``possible_delay``, a storm plan's heads-up, no new times), their new
    times (``new_times``, after the retime) or that no change is expected (``no_change``, a storm
    stood down), and records one notice of each kind per flight and day; no booking changes. A
    booking with a cancelled flight is not counted: it is rebooked, not told. A flight in
    ``notified_before`` (already notified of this kind today) is not notified again and is
    reported as such. Refused (ValueError) for a flight that is not ours today, is cancelled or
    has left by the clock."""
    if kind not in NOTICE_KINDS:
        raise ValueError(f"Unknown passenger notice {kind!r}")
    day = lisbon_today()
    now = datetime.combine(day, DEMO_CLOCK)
    flights = {f["flight_no"]: f for f in store.recovery_inventory(day)["flights"]}
    for number in flight_nos:
        flight = flights.get(number)
        if flight is None:
            raise ValueError(f"No flight {number} today")
        if flight["status"] == "cancelled":
            raise ValueError(
                f"{number} is cancelled: it has no new time to tell"
                if kind == "new_times"
                else f"{number} is cancelled: a notice reaches only a flight that operates"
            )
        if datetime.fromisoformat(flight["std_local"]) <= now:
            raise ValueError(f"{number} has left: a notice reaches only a flight yet to leave")
    fresh = [number for number in flight_nos if number not in notified_before]
    told = set(fresh)
    on_flight: Counter[str] = Counter()
    notified_passengers = 0
    for pnr in store.domain_inventory(day, "booking")["pnrs"]:
        legs = told.intersection(pnr["flight_nos"])
        if not legs or any(
            flights.get(number, {}).get("status") == "cancelled" for number in pnr["flight_nos"]
        ):
            continue
        notified_passengers += pnr["party_size"]
        for number in legs:
            on_flight[number] += pnr["party_size"]
    # One notice of each kind per flight and day: a concurrent notice of the same flight and kind
    # is refused. Nobody moves to another flight (sql/017's moved_early stays 0).
    changes = [
        {
            "op": "insert",
            "table": "passenger_notice",
            "key": {"flight_no": number, "kind": kind},
            "expected": {},
            "values": {
                "notice_id": notice_id,
                "trace_id": trace_id,
                "passengers": on_flight[number],
                "moved_early": 0,
            },
        }
        for number in sorted(told)
    ]
    return changes, {
        "notified_flights": len(told),
        "already_notified": sorted(set(flight_nos) - told),
        "notified_passengers": notified_passengers,
    }
