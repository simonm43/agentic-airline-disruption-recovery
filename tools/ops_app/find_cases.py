"""Find the four demo cases on today's generated operating day (spec §11).

Prints candidates for case 1 (a LIS departure +20 min: nothing to rebook, crew legal), case 2
(+60 min: 1-10 rebookings within policy, cost within the cap, crew legal) and case 4 (an evening
delay of about 75 min that breaks a duty's limit, resolved by a reserve, at most 10 rebookings,
cost within the cap), each run through the real planner and policy. Passengers outside rebooking
policy go to the Passenger Services expert (D23); the fewest is preferred. Since D108 a car keeps
connections a delay leaves at least 15 minutes (20 through passport control), so a case may need
fewer rebookings than before. Case 3 is the CS-TQA 240-minute AOG. The day is the same pattern
every day, so pinned flights stay valid.

    $env:PYTHONPATH="src;tests;tests/ops_app"; .venv/Scripts/python.exe tools/ops_app/find_cases.py
"""

from __future__ import annotations

import secrets
import sys
from collections.abc import Iterator
from datetime import datetime, time
from typing import Any

from day_store import OperatingDayStore
from test_execution_workflow import SystemsGateway

from airline_systems.schedule import DEMO_CLOCK
from ops_app.planner import Levers, build_plan
from ops_app.policy import COST_CAP_EUR, REBOOK_LIMIT, RELEASE_LIMITS


def departures(
    store: OperatingDayStore, earliest: time, *, origin: str | None = None
) -> Iterator[dict[str, Any]]:
    start = datetime.combine(store.day, earliest)
    demo = datetime.combine(store.day, DEMO_CLOCK)
    for flight in sorted(store.flights.values(), key=lambda f: (f["std_local"], f["flight_no"])):
        departure = datetime.fromisoformat(flight["std_local"])
        if (
            (origin is None or flight["origin"] == origin)
            and flight["status"] == "scheduled"
            and departure >= start
            and departure > demo
        ):
            yield flight


def plan_for(
    store: OperatingDayStore, flight_no: str, minutes: int, reserve: str | None = None
) -> dict[str, Any]:
    trace = secrets.token_hex(16)
    registration = store.flights[flight_no]["aircraft_reg"]
    rotation = [f for f in store.flights.values() if f["aircraft_reg"] == registration]
    # The delay as ARROW holds it on its case: the systems are asked by its facts.
    delay = {
        "type": "flight_delay",
        "flight_no": flight_no,
        "delay_minutes": minutes,
        "cause": "staff_shortage",
    }
    document = {
        "trace_id": trace,
        "version": 0,
        "disruption": delay,
        "rotation": {"flights": rotation},
    }
    levers = Levers(aircraft_option_id="delay", reserve_option_id=reserve)
    return build_plan(SystemsGateway(store), document, levers, RELEASE_LIMITS)


def facts(plan: dict[str, Any]) -> tuple[str, int, int, float, int]:
    rebooking = plan["validation"]["rebooking"]
    return (
        plan["policy"]["route"],
        rebooking["rebooked_passengers"],
        rebooking["unplaced_passengers"],
        plan["cost"]["total_eur"] if plan["cost"] else float("inf"),
        plan["validation"]["crew_plan"]["breaches_unresolved"],
    )


def within_limits(
    route: str, rebooked: int, cost: float, breaches: int, *, fewest_rebooked: int = 1
) -> bool:
    """Spec §11: automatic, 1-10 rebookings within policy (case 4: 0-10), cost within the cap,
    crew legal.

    Passengers outside rebooking policy go to the Passenger Services expert (D23).
    """
    return (
        route == "auto"
        and fewest_rebooked <= rebooked <= REBOOK_LIMIT
        and cost <= COST_CAP_EUR
        and not breaches
    )


def main() -> None:
    store = OperatingDayStore()
    print(f"Operating day {store.day}")
    later = {
        f["flight_no"]: sum(
            1
            for g in store.flights.values()
            if g["aircraft_reg"] == f["aircraft_reg"] and g["std_local"] > f["std_local"]
        )
        for f in store.flights.values()
    }
    for flight in departures(store, time(10, 30), origin="LIS"):
        route, rebooked, unplaced, cost, breaches = facts(plan_for(store, flight["flight_no"], 20))
        if route == "auto" and rebooked == unplaced == 0 and breaches == 0:  # no missed connections
            print("CASE_1", (flight["flight_no"], 20, "staff_shortage"), f"cost €{cost:,.0f}")
            break
    matches = []
    for flight in departures(store, time(10, 30)):
        route, rebooked, unplaced, cost, breaches = facts(plan_for(store, flight["flight_no"], 60))
        if within_limits(route, rebooked, cost, breaches):
            matches.append((unplaced, flight["flight_no"], rebooked, cost))
    if matches:
        unplaced, flight_no, rebooked, cost = min(matches)
        print("CASE_2", (flight_no, 60, "staff_shortage"), f"rebooked {rebooked},", end=" ")
        print(f"to Passenger Services {unplaced}, cost €{cost:,.0f}; {len(matches)} matches")
    else:
        print("CASE_2 no candidate")
    duties = {no: d for d in store.duties.values() for no in d["flight_nos"]}
    found = []
    for minutes in (75, 60, 90):
        for flight in departures(store, time(17, 0)):
            duty = duties.get(flight["flight_no"])
            if not duty or duty["max_fdp_minutes"] - duty["fdp_minutes"] >= minutes:
                continue
            if facts(plan_for(store, flight["flight_no"], minutes))[4] == 0:
                continue
            route, rebooked, unplaced, cost, breaches = facts(
                plan_for(store, flight["flight_no"], minutes, "R1")
            )
            if within_limits(route, rebooked, cost, breaches, fewest_rebooked=0):
                found.append((unplaced, flight["flight_no"], minutes, rebooked, cost))
        if found:
            break
    if found:
        unplaced, flight_no, minutes, rebooked, cost = min(found)
        print("CASE_4", (flight_no, minutes, "staff_shortage"), f"rebooked {rebooked},", end=" ")
        print(f"to Passenger Services {unplaced}, cost €{cost:,.0f}, later legs {later[flight_no]}")
    else:
        print("CASE_4 no candidate")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
