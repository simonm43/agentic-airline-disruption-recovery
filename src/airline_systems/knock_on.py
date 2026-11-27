"""What a recorded disruption does if nothing changes: its knock-on, read from the systems' own
delay option (for weather, the flights the reduced rate moves)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

# The knock-on names at most this many flights, the first by scheduled departure; its counts
# cover them all.
LISTED_FLIGHTS = 25


def _minutes(later: Any, earlier: Any) -> int:
    span = datetime.fromisoformat(str(later)) - datetime.fromisoformat(str(earlier))
    return round(span.total_seconds() / 60)


def knock_on(options: Mapping[str, Any], flights: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The flights the systems' delay option makes late, against ``flights`` as scheduled now:
    each with its route, its departure as scheduled and as it would then be, and how late it
    would be (the later of departure and arrival, so a flight held in the air counts); how many,
    the most minutes late and the aircraft involved. No delay option: nothing moves."""
    scheduled = {str(flight.get("flight_no")): flight for flight in flights}
    delay = next(
        (
            o
            for o in options.get("options") or []
            if isinstance(o, Mapping) and o.get("kind") == "delay"
        ),
        None,
    )
    late: list[dict[str, Any]] = []
    aircraft: set[str] = set()
    for flight in (delay or {}).get("flights") or []:
        was = scheduled.get(str(flight.get("flight_no")))
        if was is None or flight.get("status") == "cancelled":
            continue
        minutes = max(
            _minutes(flight["std_local"], was["std_local"]),
            _minutes(flight["sta_local"], was["sta_local"]),
        )
        if minutes <= 0:
            continue
        aircraft.add(str(flight.get("aircraft_reg")))
        late.append(
            {
                "flight_no": flight["flight_no"],
                "from": was.get("origin"),
                "to": was.get("destination"),
                "std_local": was["std_local"],
                "new_std_local": flight["std_local"],
                "minutes_late": minutes,
            }
        )
    late.sort(key=lambda item: (item["std_local"], item["flight_no"]))
    return {
        "flights": late[:LISTED_FLIGHTS],
        "flights_affected": len(late),
        "max_minutes_late": max((item["minutes_late"] for item in late), default=0),
        "aircraft": sorted(aircraft),
    }
