"""Grounded aircraft candidates, deliberately not yet cleared for execution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from airline_systems import event_ids, facts
from airline_systems.facts import reported_defect
from airline_systems.operating_day import (
    AIRPORTS,
    CO2_T_PER_T_FUEL,
    EU261_AREA,
    EUA_EUR_PER_T,
    FUEL_EUR_PER_KG,
    FUEL_KG_PER_MINUTE,
    WIDE_BODY_FROM_SEATS,
    distance_km,
    emissions_trading,
)
from airline_systems.schedule import ScheduleStore, lisbon_today


class DefectInput(BaseModel):
    """A defect ARROW's executor records in Maintenance, once a plan for it runs."""

    model_config = ConfigDict(extra="forbid")
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    aircraft_reg: str = Field(pattern=r"^CS-[A-Z]{3}$")
    description: str = Field(min_length=3, max_length=1000)
    unavailable_minutes: int = Field(strict=True, ge=1, le=720)
    user_oid: str | None = Field(default=None, pattern=r"^[0-9a-f-]{36}$")


class DefectService:
    def __init__(self, store: ScheduleStore) -> None:
        self.store = store

    def record(self, report: DefectInput) -> dict[str, Any]:
        """Maintenance records the defect and marks the aircraft out of service until its
        estimate (the store does both in one transaction). The same record sent again (a retry
        after a lost reply) is answered as recorded whatever has changed in the operation
        since; a different one under the same trace is refused."""
        if not report.description.strip():
            raise ValueError("A defect description is required")
        try:
            existing: dict[str, Any] | None = self.store.get_defect(report.trace_id)
        except ValueError:
            existing = None
        if existing is not None:
            facts.same_record(
                existing, report, ("aircraft_reg", "unavailable_minutes", "description"), "defect"
            )
            return {**existing, "event_id": event_ids.event_id(report.trace_id)}
        found = reported_defect(self.store, report.aircraft_reg, report.unavailable_minutes)
        recorded = self.store.record_defect(
            {
                **report.model_dump(),
                "operating_date": found["operating_date"],
                "reported_at": found["reported_at"],
                "unavailable_until": found["unavailable_until"],
            }
        )
        # Maintenance's own reference for the defect, issued as it records it.
        return {**recorded, "event_id": event_ids.event_id(report.trace_id)}

    def read(self, trace: str) -> dict[str, Any]:
        """A defect Maintenance recorded (its get_defect tool)."""
        defect = self.store.get_defect(trace)
        if defect["operating_date"] != lisbon_today().isoformat():
            raise ValueError("This defect belongs to an earlier operating day; open a new report")
        return defect

    def options(
        self,
        defect: Mapping[str, Any],
        additional_delay_minutes: int = 0,
        *,
        inventory: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        check_adjustment(additional_delay_minutes)
        day = lisbon_today()
        inventory = inventory if inventory is not None else self.store.recovery_inventory(day)
        start = datetime.fromisoformat(defect["reported_at"])
        until = datetime.fromisoformat(defect["unavailable_until"])
        rotation = sorted(
            (f for f in inventory["flights"] if f["aircraft_reg"] == defect["aircraft_reg"]),
            key=lambda f: f["std_local"],
        )
        affected = [f for f in rotation if start <= datetime.fromisoformat(f["std_local"]) < until]
        options: list[dict[str, Any]] = []
        checked: list[dict[str, Any]] = []
        fleet = {a["registration"]: a for a in inventory["aircraft"]}
        aircraft = fleet[defect["aircraft_reg"]]
        if affected:
            turn = turnaround(aircraft["fleet_type"])
            future = [f for f in rotation if datetime.fromisoformat(f["std_local"]) >= start]
            seats = aircraft["seats"]
            delayed = shift(
                propagate(future, until, turn, seats), additional_delay_minutes, turn, seats
            )
            options.append(
                _candidate(
                    "delay",
                    "Wait for maintenance and delay the rotation",
                    delayed,
                    additional_delay_minutes=additional_delay_minutes,
                )
            )
            # Use a real ground window without displacing the donor aircraft's flights.
            donor, why = find_swap(inventory, fleet, defect["aircraft_reg"], affected, turn)
            checked.append({"kind": "swap", "available": donor is not None, "why": why})
            if donor is not None:
                swapped = [{**f, "aircraft_reg": donor} for f in affected]
                options.append(
                    _candidate("swap", f"Use {donor} during its scheduled ground window", swapped)
                )
            cancelled = list(affected)
            position = affected[0]["origin"]
            for flight in future[len(affected) :]:
                if flight["origin"] == position:
                    break
                cancelled.append(flight)
            options.append(
                _candidate(
                    "cancel",
                    "Cancel through a safe aircraft-position boundary; retain later flights",
                    [{**f, "status": "cancelled"} for f in cancelled],
                )
            )
            kept = [f["flight_no"] for f in future[len(cancelled) :]]
            checked.append(
                {
                    "kind": "cancel",
                    "available": True,
                    "why": f"Cancelling {_span(cancelled)} keeps "
                    + (f"{_names(kept)} on time" if kept else "no later flight"),
                }
            )
        return {
            "cause": "aircraft_defect",
            "affected_flight_ids": [f["flight_no"] for f in affected],
            "aircraft": {
                "registration": defect["aircraft_reg"],
                "fleet_type": aircraft["fleet_type"],
                "seats": aircraft["seats"],
                # Maintenance's estimate, not an airworthiness release.
                "available_from": defect["unavailable_until"],
            },
            "options": options,
            "checked": checked,
        }


def _names(flight_nos: list[str]) -> str:
    if len(flight_nos) > 1:
        return ", ".join(flight_nos[:-1]) + f" and {flight_nos[-1]}"
    return flight_nos[0] if flight_nos else ""


def _span(flights: list[dict[str, Any]]) -> str:
    first, last = flights[0]["flight_no"], flights[-1]["flight_no"]
    return first if first == last else f"{first}–{last}"


def find_swap(
    inventory: dict[str, Any],
    fleet: dict[str, dict[str, Any]],
    aircraft_reg: str,
    legs: list[dict[str, Any]],
    turn: timedelta,
) -> tuple[str | None, str]:
    """A serviceable aircraft of the same type with at least the seats that is on the ground at
    the legs' origin for their scheduled times and back for its own next flight; else why not.
    The donor's own flights never move."""
    aircraft = fleet[aircraft_reg]
    origin = legs[0]["origin"]
    departure = datetime.fromisoformat(legs[0]["std_local"])
    arrival = datetime.fromisoformat(legs[-1]["sta_local"])
    window = f"{origin} {departure:%H:%M}–{arrival:%H:%M}"
    if origin != legs[-1]["destination"]:
        return None, f"{_span(legs)} does not return to {origin}, so no aircraft can hand back"
    same = 0
    for reg, candidate in sorted(fleet.items()):
        if (
            reg == aircraft_reg
            or candidate["status"] != "serviceable"
            or candidate["fleet_type"] != aircraft["fleet_type"]
            or candidate["seats"] < aircraft["seats"]
        ):
            continue
        same += 1
        other = sorted(
            (f for f in inventory["flights"] if f["aircraft_reg"] == reg),
            key=lambda f: f["std_local"],
        )
        before = [f for f in other if datetime.fromisoformat(f["sta_local"]) <= departure]
        after = [f for f in other if datetime.fromisoformat(f["std_local"]) >= arrival]
        if len(before) + len(after) != len(other):
            continue
        location = before[-1]["destination"] if before else candidate["base"]
        if location != origin:
            continue
        if before and datetime.fromisoformat(before[-1]["sta_local"]) + turn > departure:
            continue
        if after and (
            datetime.fromisoformat(after[0]["std_local"]) < arrival + turn
            or after[0]["origin"] != legs[-1]["destination"]
        ):
            continue
        return reg, f"{reg} is on the ground at {window} and can fly {_span(legs)}"
    return None, (
        f"No other serviceable {aircraft['fleet_type']} with {aircraft['seats']} or more seats"
        f" is free at {window} ({same} checked)"
    )


def check_adjustment(additional_delay_minutes: int) -> None:
    if (
        additional_delay_minutes < 0
        or additional_delay_minutes > 180
        or additional_delay_minutes % 5
    ):
        raise ValueError("Additional delay must be a five-minute increment from 0 to 180.")


def turnaround(fleet_type: str) -> timedelta:
    return timedelta(minutes=60 if fleet_type == "A330neo" else 35)


# Making up time in the air only when the minutes are worth it (owner; delay recovery research
# and cost index and time value research §5).
# A late flight flies faster at a higher cost index: about 3.5% of airborne time (Airbus,
# "Getting to grips with the cost index", 1998, Tables 21-22; Cook et al., EUROCONTROL 2007,
# Table 4), airborne being 85% of block (EUROCONTROL Data Snapshot #40), at most 10 minutes and
# none under an hour in the air; up to 6% and 15 minutes only to reach a step (landing under
# 3 h late). ATC acceptance is assumed; later flights are not counted in the choice.
AIRBORNE_SHARE = 0.85
MADE_UP_SHARE = 0.035
MADE_UP_CAP_MINUTES = 10
STRETCH_SHARE = 0.06
STRETCH_CAP_MINUTES = 15
MADE_UP_FROM_AIRBORNE_MINUTES = 60
# Buying a minute back costs its fuel and, when the flight pays for its carbon, allowances for
# its CO2 (operating_day), less the crew and maintenance time it saves (University of
# Westminster for EUROCONTROL, 2022 prices).
TIME_EUR_PER_MINUTE = {"narrow": 14.4, "wide": 23.5}
# What the minutes are worth, used to decide only and never added to a plan's cost: passenger
# goodwill per passenger-minute by the delay band the minute leaves (Westminster Table 18, 2022
# prices; weakly evidenced), for passengers on board assumed at 85% of seats; and the EU261 step,
# landing under 3 h late (179 min, case law), at least €250 a passenger (the lowest band;
# extraordinary circumstances not assumed).
GOODWILL_EUR = ((15, 0.16), (30, 0.53), (60, 1.46), (90, 1.74), (120, 1.43), (180, 1.28))
GOODWILL_OVER_3_H_EUR = 1.25
LOAD_FACTOR = 0.85
EU261_LATEST_MINUTES = 179
EU261_STEP_EUR = 250
LATE_BANDS = (
    (15, "up to 15 min"),
    (30, "15–30 min"),
    (60, "30–60 min"),
    (90, "60–90 min"),
    (120, "90–120 min"),
    (180, "2–3 h"),
)
MADE_UP_KEYS = ("made_up_minutes", "made_up_possible", "made_up_reason")


@dataclass(frozen=True)
class MadeUp:
    minutes: int
    possible: int
    reason: str


def _goodwill(late: int, minutes: int, passengers: float) -> float:
    """What cutting the arrival delay from ``late`` by ``minutes`` is worth to the passengers."""
    return passengers * sum(
        next((rate for upper, rate in GOODWILL_EUR if delay <= upper), GOODWILL_OVER_3_H_EUR)
        for delay in range(late, late - minutes, -1)
    )


def made_up(
    block: timedelta, late: timedelta, seats: int, ets: bool, *, weather: bool = False
) -> MadeUp:
    """The minutes a flight leaving ``late`` makes up in the air on a sector of ``block``, of
    those it could, and why: none, the usual cap or the minutes to land under 3 h late, whichever
    is worth most beyond its fuel (ties to fewer minutes). ``weather``: a storm's plan, which owes
    no EU261 compensation (extraordinary circumstances), so landing under 3 h late saves nothing
    and it never stretches past the usual cap for it (review)."""
    airborne = block.total_seconds() / 60 * AIRBORNE_SHARE
    late_minutes = round(late.total_seconds() / 60)
    if late_minutes <= 0:
        return MadeUp(0, 0, "")
    if airborne < MADE_UP_FROM_AIRBORNE_MINUTES:
        return MadeUp(0, 0, "under an hour in the air")
    cap = min(int(airborne * MADE_UP_SHARE), MADE_UP_CAP_MINUTES, late_minutes)
    stretch = min(int(airborne * STRETCH_SHARE), STRETCH_CAP_MINUTES, late_minutes)
    body = "wide" if seats >= WIDE_BODY_FROM_SEATS else "narrow"
    carbon = CO2_T_PER_T_FUEL * EUA_EUR_PER_T / 1000 if ets else 0.0
    cost = FUEL_KG_PER_MINUTE[body] * (FUEL_EUR_PER_KG + carbon) - TIME_EUR_PER_MINUTE[body]
    passengers = seats * LOAD_FACTOR
    step = 0 if weather else late_minutes - EU261_LATEST_MINUTES

    def worth(minutes: int) -> float:
        avoided = EU261_STEP_EUR * passengers if 0 < step <= minutes else 0.0
        return _goodwill(late_minutes, minutes, passengers) + avoided - minutes * cost

    minutes = max(sorted({0, cap} | ({step} if 0 < step <= stretch else set())), key=worth)
    band = next((words for upper, words in LATE_BANDS if late_minutes <= upper), "over 3 h")
    if not minutes:
        reason = f"not worth its fuel at {band} late"
    elif 0 < step <= minutes:
        reason = "arrives under 3 h late: EU261 avoided"
    else:
        reason = f"worth more than its fuel at {band} late"
    return MadeUp(minutes, stretch if minutes > cap else cap, reason)


def propagate(
    flights: list[dict[str, Any]],
    available: datetime,
    turn: timedelta,
    seats: int,
    *,
    weather: bool = False,
) -> list[dict[str, Any]]:
    """Each flight leaves no earlier than the aircraft of ``seats`` is available; a late flight
    makes up minutes in the air when they are worth more than the fuel, otherwise keeps its
    block time. Each late flight says how many minutes it makes up, of how many, and why.
    ``weather``: a storm's plan, which never flies faster to dodge EU261 compensation."""
    moved = []
    for flight in flights:
        flight = {key: value for key, value in flight.items() if key not in MADE_UP_KEYS}
        old_departure = datetime.fromisoformat(flight["std_local"])
        block = datetime.fromisoformat(flight["sta_local"]) - old_departure
        # After an earlier retime, speed and lateness still count from the published timetable.
        published = flight.get("published_std_local")
        if published and flight.get("published_sta_local"):
            scheduled = datetime.fromisoformat(published)
            block = datetime.fromisoformat(flight["published_sta_local"]) - scheduled
        else:
            scheduled = old_departure
        departure = max(old_departure, available)
        arrival = departure + block
        if departure > scheduled:
            ets = emissions_trading(flight["origin"], flight["destination"])
            speed = made_up(block, departure - scheduled, seats, ets, weather=weather)
            arrival -= timedelta(minutes=speed.minutes)
            flight.update(
                made_up_minutes=speed.minutes,
                made_up_possible=speed.possible,
                made_up_reason=speed.reason,
            )
        moved.append(
            {**flight, "std_local": departure.isoformat(), "sta_local": arrival.isoformat()}
        )
        available = arrival + turn
    return moved


def shift(
    flights: list[dict[str, Any]], minutes: int, turn: timedelta, seats: int
) -> list[dict[str, Any]]:
    """The flights ``minutes`` later, each late flight's minutes in the air chosen again for its
    new lateness (a later flight never leaves before the aircraft is back and turned round)."""
    if not minutes or not flights:
        return list(flights)
    extra = timedelta(minutes=minutes)
    later = [
        {
            **flight,
            "std_local": (datetime.fromisoformat(flight["std_local"]) + extra).isoformat(),
            "sta_local": (datetime.fromisoformat(flight["sta_local"]) + extra).isoformat(),
        }
        for flight in flights
    ]
    return propagate(later, datetime.fromisoformat(later[0]["std_local"]), turn, seats)


def recovery_options(
    store: ScheduleStore,
    disruption: Mapping[str, Any],
    additional_delay_minutes: int = 0,
    *,
    inventory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The grounded options for a disruption (airline_systems.facts): a flight delay, a weather
    forecast, otherwise an aircraft defect."""
    from airline_systems.delays import DelayService
    from airline_systems.weather import WeatherService

    kind = disruption.get("type")
    if kind == "flight_delay":
        return DelayService(store).options(
            disruption, additional_delay_minutes, inventory=inventory
        )
    if kind == "weather":
        return WeatherService(store).options(
            disruption, additional_delay_minutes, inventory=inventory
        )
    if kind != "aircraft_defect":
        raise ValueError(f"Not a disruption the systems read: {kind!r}")
    return DefectService(store).options(disruption, additional_delay_minutes, inventory=inventory)


def _enrich(flight: dict[str, Any]) -> dict[str, Any]:
    origin, destination = flight.get("origin"), flight.get("destination")
    if origin not in AIRPORTS or destination not in AIRPORTS:
        return flight
    return {
        **flight,
        "distance_km": distance_km(origin, destination),
        "intra_eu": origin in EU261_AREA and destination in EU261_AREA,
        "ets": emissions_trading(origin, destination),
    }


def _candidate(
    kind: str,
    title: str,
    flights: list[dict[str, Any]],
    *,
    additional_delay_minutes: int = 0,
    option_id: str | None = None,
) -> dict[str, Any]:
    flights = [_enrich(flight) for flight in flights]
    return {
        "option_id": option_id or kind,
        "kind": kind,
        "additional_delay_minutes": additional_delay_minutes,
        "title": title,
        "feasible": None,
        "approval_ready": False,
        "reason": "Aircraft candidate only; crew, passenger and slot validation is pending.",
        "flights": flights,
        "delay_minutes": None,
        "crew_changes": None,
        "misconnected_passengers": None,
        "eu261_eur": None,
        "care_eur": None,
        "operating_eur": None,
        "total_eur": None,
        "domain_impacts": {
            "aircraft": title,
            "crew": "Not assessed",
            "booking": "Not assessed",
            "slots": "Not assessed",
        },
        "assumptions": [
            "Single-day operation; this is not an executable recovery plan.",
            "Maintenance ETA is not an airworthiness release.",
        ],
    }
