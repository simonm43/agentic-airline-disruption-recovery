"""A forecast weather hazard at an airport (Case 5, read from a TAF) and the plans that respect
it.

One case covers the whole forecast, so it names no aircraft. A read names the forecast by its
facts: the airport, the hazard, the window in Lisbon time and the reduced hourly rate the weather
playbook plans while it lasts (airline_systems.facts); no airline system records a forecast (the
met service owns it). Each plan sequences the airport's movements into hourly capacity: the
reduced rate in every clock hour the window touches (the hourly allocation's unit), the normal
rate after.

The OCC playbook (OCC playbook plan) adds dispatch advisories: holding fuel and alternates,
advice only.
"""

from __future__ import annotations

import heapq
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from airline_systems.defects import (
    MADE_UP_KEYS,
    _candidate,
    check_adjustment,
    propagate,
    turnaround,
)
from airline_systems.operating_day import (
    AIRPORTS,
    CITY,
    EXPOSURE_AFTER,
    EXPOSURE_BEFORE,
    SLOT_CAPACITY,
    STORM_ALTERNATES,
    WEATHER_RATE_BASIS,
    distance_km,
)
from airline_systems.schedule import ScheduleStore, lisbon_today

HOUR = timedelta(hours=1)
MOST_ROUND_TRIPS_CANCELLED = 2
# The longest an arrival holds in the air to keep the origin hour it may leave in, rather than
# waiting on the ground for a later one (assumption).
AIRBORNE_HOLD_LIMIT = timedelta(minutes=15)
HOLDING_FUEL = "carry holding fuel"
HOLDING_NOTE = "holding expected; dispatcher sets the fuel"
NO_ALTERNATE_HAZARD = "no forecast hazard in this demo"


def storm_hours(weather: Mapping[str, Any]) -> list[datetime]:
    """Every clock hour the weather window touches; the reduced rate applies to all of each."""
    hour = _hour(datetime.fromisoformat(weather["window_start_local"]))
    end = datetime.fromisoformat(weather["window_end_local"])
    hours = []
    while hour < end:
        hours.append(hour)
        hour += HOUR
    return hours


def exposure_window(weather: Mapping[str, Any]) -> tuple[datetime, datetime]:
    """The weather window widened as dispatch plans for it (assumption): holding before, the
    queue clearing after."""
    return (
        datetime.fromisoformat(weather["window_start_local"]) - EXPOSURE_BEFORE,
        datetime.fromisoformat(weather["window_end_local"]) + EXPOSURE_AFTER,
    )


class WeatherService:
    def __init__(self, store: ScheduleStore) -> None:
        self.store = store

    def options(
        self,
        weather: Mapping[str, Any],
        additional_delay_minutes: int = 0,
        *,
        inventory: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        check_adjustment(additional_delay_minutes)
        if additional_delay_minutes:
            raise ValueError(
                "A weather plan follows the reduced hourly rate; it takes no added delay"
            )
        inventory = (
            inventory if inventory is not None else self.store.recovery_inventory(lisbon_today())
        )
        flights = inventory["flights"]
        fleet = {a["registration"]: a for a in inventory["aircraft"]}
        now = datetime.fromisoformat(weather["reported_at"])
        start = datetime.fromisoformat(weather["window_start_local"])
        end = datetime.fromisoformat(weather["window_end_local"])
        place = CITY.get(weather["airport"], weather["airport"])
        rate = (
            f"{place} at {weather['hourly_rate']} movements an hour from {start:%H:%M} to"
            f" {end:%H:%M} ({weather['normal_rate']} after)"
        )
        basis = WEATHER_RATE_BASIS[weather["hazard"]]
        late, waited = _sequence(flights, fleet, weather, now)
        options: list[dict[str, Any]] = []
        checked: list[dict[str, Any]] = []
        if not late:
            checked.append(
                {
                    "kind": "delay",
                    "available": False,
                    "why": f"With {rate}, every {place} movement fits; no flight moves",
                }
            )
        else:
            original = {f["flight_no"]: f for f in flights}
            most = max(_minutes_late(f, original[f["flight_no"]]) for f in late.values())
            options.append(
                _weather_candidate(
                    "delay",
                    f"Accept the reduced {place} rate and fly the affected flights late",
                    list(late.values()),
                    rate,
                    basis,
                )
            )
            waits = "waits" if waited == 1 else "wait"
            checked.append(
                {
                    "kind": "delay",
                    "available": True,
                    "why": f"With {rate}, {_count(waited, f'{place} movement')} {waits} for a"
                    f" later hour and {_count(len(late), 'flight')} fly late, by up to {most} min",
                }
            )
            trips = _round_trips(flights, weather, now)
            if not trips:
                checked.append(
                    {
                        "kind": "cancel",
                        "available": False,
                        "why": f"No domestic round trip yet to leave {place} leaves or returns"
                        " there in the weather window",
                    }
                )
            else:
                legs = [leg for trip in trips for leg in trip]
                cancelled = frozenset(leg["flight_no"] for leg in legs)
                rest, _ = _sequence(flights, fleet, weather, now, cancelled)
                # Among the flights both plans fly: late in the delay plan but on time here,
                # and the reverse; their difference is the net gain in flights on time.
                saved = set(late) - set(rest) - cancelled
                worse = set(rest) - set(late)
                net = len(saved) - len(worse)
                spans = " and ".join(
                    f"{out['flight_no']}/{back['flight_no']}" for out, back in trips
                )
                if not saved:
                    checked.append(
                        {
                            "kind": "cancel",
                            "available": False,
                            "why": f"Cancelling {spans} would keep no flight on time that the"
                            " weather makes late",
                        }
                    )
                else:
                    names = " and ".join(
                        dict.fromkeys(
                            CITY.get(out["destination"], out["destination"]) for out, _ in trips
                        )
                    )
                    round_trips = "round trips" if len(trips) > 1 else "round trip"
                    options.append(
                        _weather_candidate(
                            "cancel",
                            f"Cancel the {names} {round_trips} in the weather window, then"
                            " sequence the rest",
                            [*rest.values(), *({**leg, "status": "cancelled"} for leg in legs)],
                            rate,
                            basis,
                        )
                    )
                    checked.append(
                        {
                            "kind": "cancel",
                            "available": True,
                            "why": f"Cancelling {spans} keeps {_count(net, 'more flight')} on time"
                            if net > 0
                            else f"Cancelling {spans} puts {_count(len(saved), 'flight')} back"
                            f" on time but makes {_count(len(worse), 'other')} late",
                        }
                    )
        listed = {f["flight_no"] for option in options for f in option["flights"]}
        return {
            "cause": "weather",
            "affected_flight_ids": [
                f["flight_no"]
                for f in sorted(flights, key=lambda f: (f["std_local"], f["flight_no"]))
                if f["flight_no"] in listed
            ],
            "aircraft": None,
            "weather": {
                "airport": weather["airport"],
                "window_start_local": weather["window_start_local"],
                "window_end_local": weather["window_end_local"],
                "hazard": weather["hazard"],
                "hourly_rate": weather["hourly_rate"],
                "normal_rate": weather["normal_rate"],
                "basis": basis,
            },
            "options": options,
            "checked": checked,
        }


class _Hours:
    """Movements per coordinated airport and clock hour as a plan places them, against each
    hour's capacity. Flights that left by the operating clock are counted first and never move.
    The storm airport's other movements from the storm's first hour are placed afresh, first
    come, first served; elsewhere a flight keeps its hour unless it moves."""

    def __init__(
        self, active: list[dict[str, Any]], weather: Mapping[str, Any], fixed: frozenset[str]
    ) -> None:
        self.airport = str(weather["airport"])
        self.reduced = set(storm_hours(weather))
        self.first = min(self.reduced)
        self.rate = int(weather["hourly_rate"])
        self.baseline = Counter(key for flight in active for _, key in _slots(flight))
        self.count = Counter(
            key
            for flight in active
            for _, key in _slots(flight)
            if flight["flight_no"] in fixed or not self.afresh(key)
        )
        self.joined: Counter[tuple[str, datetime]] = Counter()

    def afresh(self, key: tuple[str, datetime]) -> bool:
        return key[0] == self.airport and key[1] >= self.first

    def capacity(self, key: tuple[str, datetime]) -> int:
        if key[0] == self.airport and key[1] in self.reduced:
            return self.rate
        # An hour already above its rate before the storm may stay as full: Slots fails only an
        # hour a plan makes busier.
        return max(SLOT_CAPACITY[key[0]], self.baseline[key])

    def full(self, key: tuple[str, datetime], held: tuple[str, datetime] | None) -> bool:
        """Whether a movement cannot use the hour; one keeps the hour it already holds."""
        if key == held and not self.afresh(key):
            return False
        return self.count[key] >= self.capacity(key)

    def next_room(self, key: tuple[str, datetime]) -> datetime:
        """When the next hour with room starts."""
        airport, hour = key
        hour += HOUR
        while self.count[(airport, hour)] >= self.capacity((airport, hour)):
            hour += HOUR
        return hour

    def spaced(self, hour: datetime) -> datetime:
        """The next place in a storm-airport hour for a movement that waited, at the hour's
        rate's spacing, in the order they came."""
        places = self.joined[(self.airport, hour)]
        return hour + timedelta(minutes=places * 60 // self.capacity((self.airport, hour)))

    def take(self, original: dict[str, Any], placed: dict[str, Any]) -> None:
        for _, key in _slots(original):
            if not self.afresh(key):
                self.count[key] -= 1
        for _, key in _slots(placed):
            self.count[key] += 1


def _sequence(
    flights: list[dict[str, Any]],
    fleet: dict[str, dict[str, Any]],
    weather: Mapping[str, Any],
    now: datetime,
    cancelled: frozenset[str] = frozenset(),
) -> tuple[dict[str, dict[str, Any]], int]:
    """The storm airport's movements from the storm's first hour, first come, first served (a
    flight on time comes at its scheduled time) into each hour's capacity; one that does not fit
    waits for the next hour with room. A flight that has left by the operating clock keeps its
    times and comes first in its hours (air traffic control sequences airborne traffic first). A
    flight that moves must also fit the hour it now uses at its other airport. Each aircraft's
    later flights follow it, turnaround and minutes made up in the air included. Every flight
    that moves, by number, and how many storm-airport movements waited for a later hour."""
    airport = weather["airport"]
    active = [f for f in flights if f["status"] != "cancelled" and f["flight_no"] not in cancelled]
    fixed = frozenset(
        f["flight_no"] for f in active if datetime.fromisoformat(f["std_local"]) <= now
    )
    hours = _Hours(active, weather, fixed)
    rotations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for flight in sorted(active, key=lambda f: (f["std_local"], f["flight_no"])):
        rotations[flight["aircraft_reg"]].append(flight)
    queue: list[tuple[datetime, str, str, str, int]] = []
    pending: dict[tuple[str, int], dict[str, Any]] = {}

    def ready(reg: str, index: int, flight: dict[str, Any]) -> None:
        pending[(reg, index)] = flight
        at = _movement(flight, airport) or datetime.fromisoformat(flight["std_local"])
        first = flight.get("published_std_local") or flight["std_local"]
        heapq.heappush(queue, (at, first, flight["flight_no"], reg, index))

    for reg, rotation in rotations.items():
        start = next(
            (
                index
                for index, flight in enumerate(rotation)
                if (at := _movement(flight, airport)) is not None and at >= hours.first
            ),
            None,
        )
        if start is not None:
            ready(reg, start, rotation[start])
    moved: dict[str, dict[str, Any]] = {}
    waited = 0
    while queue:
        *_, reg, index = heapq.heappop(queue)
        asked = pending.pop((reg, index))
        original = rotations[reg][index]
        aircraft = fleet[reg]
        if original["flight_no"] in fixed:
            # Already counted in its hours; it keeps its times.
            flight = original
        else:
            flight = _placed(asked, original, aircraft, hours)
            hours.take(original, flight)
        at, wanted = _movement(flight, airport), _movement(asked, airport)
        if at is not None and wanted is not None and _hour(at) > _hour(wanted):
            hours.joined[(airport, _hour(at))] += 1
            waited += 1
        if (flight["std_local"], flight["sta_local"]) != (
            original["std_local"],
            original["sta_local"],
        ):
            moved[flight["flight_no"]] = flight
        if index + 1 < len(rotations[reg]):
            following = rotations[reg][index + 1]
            if flight["sta_local"] != original["sta_local"]:
                turn = turnaround(aircraft["fleet_type"])
                available = datetime.fromisoformat(flight["sta_local"]) + turn
                following = propagate(
                    [following], available, turn, aircraft["seats"], weather=True
                )[0]
            ready(reg, index + 1, following)
    return moved, waited


def _placed(
    asked: dict[str, Any],
    original: dict[str, Any],
    aircraft: dict[str, Any],
    hours: _Hours,
) -> dict[str, Any]:
    """The flight at the earliest times, from those it asks for, whose hours have room at both
    its airports. A movement that waited for a later storm-airport hour then takes its place at
    the hour's spacing when both its hours still have room there."""
    turn = turnaround(aircraft["fleet_type"])
    held = dict(_slots(original))
    flight = asked
    while (full := _full_hour(flight, held, hours)) is not None:
        movement, key = full
        free = hours.next_room(key)
        if flight["destination"] != hours.airport:
            if movement == "A":
                free = datetime.fromisoformat(flight["std_local"]) + (
                    free - datetime.fromisoformat(flight["sta_local"])
                )
            flight = propagate([asked], free, turn, aircraft["seats"], weather=True)[0]
        elif movement == "A":
            # An arrival at the storm airport is held so that it lands in an hour with room.
            flight = _at_slot(asked, original, free, aircraft, hours.airport)
        else:
            flight = _left_in_time(asked, original, flight, key, held.get("D"), hours) or (
                _at_slot(asked, original, free + _block(original), aircraft, hours.airport)
            )
    at, wanted = _movement(flight, hours.airport), _movement(asked, hours.airport)
    if at is None or wanted is None or _hour(at) <= _hour(wanted):
        return flight
    place = hours.spaced(_hour(at))
    if place <= at:
        return flight
    spread = _at_slot(asked, original, place, aircraft, hours.airport)
    return flight if _full_hour(spread, held, hours) is not None else spread


def _full_hour(
    flight: dict[str, Any], held: dict[str, tuple[str, datetime]], hours: _Hours
) -> tuple[str, tuple[str, datetime]] | None:
    """The first of the flight's movements whose airport-hour has no room for it."""
    return next(
        (
            (movement, key)
            for movement, key in _slots(flight)
            if hours.full(key, held.get(movement))
        ),
        None,
    )


def _left_in_time(
    asked: dict[str, Any],
    original: dict[str, Any],
    flight: dict[str, Any],
    key: tuple[str, datetime],
    held: tuple[str, datetime] | None,
    hours: _Hours,
) -> dict[str, Any] | None:
    """An arrival at the storm airport whose origin hour is full, leaving instead as late as an
    earlier origin hour with room (or the one it holds) allows and holding in the air until its
    arrival hour, when that hold is short; else None."""
    airport, hour = key
    slot = datetime.fromisoformat(flight["sta_local"])
    ready = datetime.fromisoformat(asked["std_local"])
    while hour > _hour(ready):
        hour -= HOUR
        departure = hour + HOUR - timedelta(minutes=1)
        if slot - departure - _block(original) > AIRBORNE_HOLD_LIMIT:
            return None
        if not hours.full((airport, hour), held):
            return _arriving(asked, original, departure, slot, hours.airport)
    return None


def _at_slot(
    flight: dict[str, Any],
    original: dict[str, Any],
    slot: datetime,
    aircraft: dict[str, Any],
    airport: str,
) -> dict[str, Any]:
    """The flight, yet to leave, with its movement at ``airport`` moved to ``slot``. A departure
    leaves then and may make up time in the air. An arrival is held at its origin so that it
    lands then."""
    turn = turnaround(aircraft["fleet_type"])
    if flight["origin"] == airport:
        return propagate([flight], slot, turn, aircraft["seats"], weather=True)[0]
    departure = max(datetime.fromisoformat(flight["std_local"]), slot - _block(original))
    return _arriving(flight, original, departure, slot, airport)


def _arriving(
    flight: dict[str, Any],
    original: dict[str, Any],
    departure: datetime,
    slot: datetime,
    airport: str,
) -> dict[str, Any]:
    """An arrival at ``airport`` leaving at ``departure`` and landing at ``slot``, saying why
    it is late (held on the ground, holding in the air, or still making up minutes) and how many
    minutes it holds in the air."""
    block = _block(original)
    scheduled = datetime.fromisoformat(original.get("published_std_local") or original["std_local"])
    arrived = {key: value for key, value in flight.items() if key not in MADE_UP_KEYS}
    faster = departure + block - slot
    holding = int(max(timedelta(0), -faster).total_seconds() // 60)
    arrived["held_airborne_minutes"] = holding
    place = CITY.get(airport, airport)
    if faster > timedelta(0):
        # Ready too late to fly its usual block: it still makes up these minutes of those
        # chosen when it was delayed.
        arrived.update(
            {key: flight[key] for key in MADE_UP_KEYS if key in flight},
            made_up_minutes=int(faster.total_seconds() // 60),
        )
    elif holding:
        arrived.update(
            made_up_minutes=0,
            made_up_possible=0,
            made_up_reason=f"holds {holding} min in the air for its {place} arrival slot",
        )
    elif departure > scheduled:
        origin = CITY.get(flight["origin"], flight["origin"])
        arrived.update(
            made_up_minutes=0,
            made_up_possible=0,
            made_up_reason=f"held at {origin} for its {place} arrival slot",
        )
    return {**arrived, "std_local": departure.isoformat(), "sta_local": slot.isoformat()}


def _round_trips(
    flights: list[dict[str, Any]], weather: Mapping[str, Any], now: datetime
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Up to two domestic round trips from the storm airport, yet to leave at the operating
    clock, whose movements there fall in the window: both before one (a domestic round trip
    takes over two hours, so in a two-hour storm only one of its movements can), then shortest
    sectors first. Cancelling one takes its movements out of the storm and the queue behind
    it."""
    airport = weather["airport"]
    start = datetime.fromisoformat(weather["window_start_local"])
    end = datetime.fromisoformat(weather["window_end_local"])
    rotations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for flight in sorted(
        (f for f in flights if f["status"] != "cancelled"),
        key=lambda f: (f["std_local"], f["flight_no"]),
    ):
        rotations[flight["aircraft_reg"]].append(flight)
    trips = []
    for rotation in rotations.values():
        for out, back in zip(rotation, rotation[1:], strict=False):
            inside = sum(
                start <= datetime.fromisoformat(at) < end
                for at in (out["std_local"], back["sta_local"])
            )
            if (
                inside
                and out["origin"] == airport == back["destination"]
                and out["destination"] == back["origin"]
                and out["status"] == back["status"] == "scheduled"
                and datetime.fromisoformat(out["std_local"]) > now
                # Domestic: both ends in Portugal.
                and AIRPORTS[airport][2] == "PT"
                and AIRPORTS.get(out["destination"], (0.0, 0.0, ""))[2] == "PT"
            ):
                trips.append((-inside, distance_km(airport, out["destination"]), out, back))
    trips.sort(key=lambda trip: (trip[0], trip[1], trip[2]["std_local"], trip[2]["flight_no"]))
    return [(out, back) for *_, out, back in trips[:MOST_ROUND_TRIPS_CANCELLED]]


def _weather_candidate(
    kind: str, title: str, flights: list[dict[str, Any]], rate: str, basis: str
) -> dict[str, Any]:
    candidate = _candidate(
        kind,
        title,
        sorted(
            # Every flight says how long it holds in the air, so ARROW can price its fuel.
            ({**f, "held_airborne_minutes": f.get("held_airborne_minutes", 0)} for f in flights),
            key=lambda f: (f["std_local"], f["flight_no"]),
        ),
    )
    candidate["assumptions"] = [
        candidate["assumptions"][0],
        f"{rate}: {basis}.",
    ]
    return candidate


def dispatch_advisories(store: ScheduleStore, weather: Mapping[str, Any]) -> dict[str, Any]:
    """Advice for dispatch (FUEL-FLAG, ALT-FLAG): the flights yet to leave that land at the storm
    airport in its exposure window should expect holding, and the alternates with their distance.
    No fuel figure is invented and no flight changes: the dispatcher and the commander decide."""
    if weather.get("type") != "weather":
        raise ValueError("Dispatch advice is for a weather forecast")
    airport = weather["airport"]
    now = datetime.fromisoformat(weather["reported_at"])
    opens, closes = exposure_window(weather)
    flights = sorted(
        (
            f
            for f in store.recovery_inventory(lisbon_today())["flights"]
            if f["destination"] == airport
            and f["status"] == "scheduled"
            and datetime.fromisoformat(f["std_local"]) > now
            and opens <= datetime.fromisoformat(f["sta_local"]) < closes
        ),
        key=lambda f: (f["sta_local"], f["flight_no"]),
    )
    return {
        "airport": airport,
        "exposure_window": {"start_local": opens.isoformat(), "end_local": closes.isoformat()},
        "flights": [
            {
                "flight_no": f["flight_no"],
                "origin": f["origin"],
                "std_local": f["std_local"],
                "sta_local": f["sta_local"],
                "advice": HOLDING_FUEL,
                "note": HOLDING_NOTE,
            }
            for f in flights
        ],
        "alternates": [
            {
                "iata": iata,
                "city": CITY[iata],
                "distance_km": distance_km(airport, iata),
                "hazard": NO_ALTERNATE_HAZARD,
            }
            for iata in STORM_ALTERNATES
        ],
        "basis": "Advice only: the dispatcher and the commander decide fuel and alternates.",
    }


def _movement(flight: dict[str, Any], airport: str) -> datetime | None:
    """When the flight leaves or reaches ``airport``; None if it does neither."""
    if flight["origin"] == airport:
        return datetime.fromisoformat(flight["std_local"])
    if flight["destination"] == airport:
        return datetime.fromisoformat(flight["sta_local"])
    return None


def _slots(flight: dict[str, Any]) -> list[tuple[str, tuple[str, datetime]]]:
    """The airport-hours the flight uses at coordinated airports: its departure ("D") and its
    arrival ("A")."""
    return [
        (movement, (flight[end], _hour(datetime.fromisoformat(flight[key]))))
        for movement, end, key in (("D", "origin", "std_local"), ("A", "destination", "sta_local"))
        if flight[end] in SLOT_CAPACITY
    ]


def _block(flight: dict[str, Any]) -> timedelta:
    """The flight's usual block time, from the published timetable when it has one."""
    std, sta = flight.get("published_std_local"), flight.get("published_sta_local")
    if std and sta:
        return datetime.fromisoformat(sta) - datetime.fromisoformat(std)
    return datetime.fromisoformat(flight["sta_local"]) - datetime.fromisoformat(flight["std_local"])


def _hour(at: datetime) -> datetime:
    return at.replace(minute=0, second=0, microsecond=0)


def _minutes_late(flight: dict[str, Any], original: dict[str, Any]) -> int:
    return max(
        int(
            (
                datetime.fromisoformat(flight[key]) - datetime.fromisoformat(original[key])
            ).total_seconds()
            // 60
        )
        for key in ("std_local", "sta_local")
    )


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}{'' if number == 1 else 's'}"
