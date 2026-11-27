"""Deterministic validation of a recovery option before policy: rebooking coverage, night rules.

Airport rules are station data read from the slot system; nothing here is airport-specific code.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, time
from typing import Any


def _within(moment: datetime, start: str | None, end: str | None) -> bool:
    if not start or not end:
        return False
    begin, finish, at = time.fromisoformat(start), time.fromisoformat(end), moment.time()
    return begin <= at or at <= finish if begin > finish else begin <= at <= finish


def _moved(flight: Mapping[str, Any], original: Mapping[str, Any] | None) -> bool:
    return original is None or any(
        flight.get(key) != original.get(key) for key in ("std_local", "sta_local")
    )


def validate(
    option: Mapping[str, Any],
    *,
    originals: Mapping[str, Mapping[str, Any]],
    rebooking: Mapping[str, Any],
    airports: Mapping[str, Mapping[str, Any]],
    duty_plan: Mapping[str, Any],
) -> dict[str, Any]:
    requests: list[str] = []
    unknown: set[str] = set()
    for flight in option.get("flights") or []:
        cancelled = flight.get("status") == "cancelled"
        original = originals.get(flight["flight_no"])
        if not cancelled and not _moved(flight, original):
            continue
        # A station without a rule row is unknown, not "no rule" (unknown is not clearance);
        # cancellations need the rules too, to release coordinated slots.
        unknown.update(
            str(code)
            for code in (flight.get("origin"), flight.get("destination"))
            if code not in airports
        )
        if cancelled:
            continue
        origin = airports.get(flight.get("origin", ""), {})
        destination = airports.get(flight.get("destination", ""), {})
        departure = datetime.fromisoformat(flight["std_local"])
        arrival = datetime.fromisoformat(flight["sta_local"])
        if _within(
            departure, origin.get("night_departure_from"), origin.get("night_departure_until")
        ):
            requests.append(
                f"{flight['origin']} night quota: {flight['flight_no']} off-block {departure:%H:%M}"
            )
        if _within(
            arrival, destination.get("night_arrival_from"), destination.get("night_arrival_until")
        ):
            requests.append(
                f"{flight['destination']} night quota: {flight['flight_no']} in-block "
                f"{arrival:%H:%M}"
            )
    return {
        "rebooking": {
            key: rebooking.get(key)
            for key in (
                "disrupted_passengers",
                "rebooked_passengers",
                "unplaced_passengers",
                "plan_digest",
                "transferred_passengers",
                "transfers",
            )
        },
        "airport_requests": requests,
        "airport_rules_unknown": sorted(unknown),
        "crew_plan": {
            key: duty_plan.get(key)
            for key in (
                "duties_changed",
                "plan_digest",
                "message",
                "breaches_unresolved",
                "reserve_option_id",
                # Every standby crew the plan calls, and the complete standby crews of each base
                # and qualification left today after it (the Standby crews rule).
                "reserve_option_ids",
                "standby_left",
            )
        },
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
