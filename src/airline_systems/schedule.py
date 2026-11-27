"""Schedule system: today's flights per aircraft and the demo clock.

The first request of a Lisbon day loads the 900-flight operating day into every system.
Times are Lisbon local wall-clock times.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Any, Protocol
from zoneinfo import ZoneInfo

from airline_systems.versions import flight_version

if TYPE_CHECKING:
    from airline_systems.operating_day import OperatingDay

LISBON = ZoneInfo("Europe/Lisbon")
DEMO_CLOCK = time(9, 45)


@dataclass(frozen=True)
class Flight:
    flight_date: date
    flight_no: str
    origin: str
    destination: str
    std_local: datetime
    sta_local: datetime
    aircraft_reg: str
    status: str = "scheduled"
    # The published timetable, set by the day load. A retime moves std/sta (the current
    # estimate) and never these; a passenger's delay (EU261) is measured from them.
    published_std_local: datetime | None = None
    published_sta_local: datetime | None = None

    def __post_init__(self) -> None:
        # A flight built without a published time (the day load) is published as scheduled.
        if self.published_std_local is None:
            object.__setattr__(self, "published_std_local", self.std_local)
        if self.published_sta_local is None:
            object.__setattr__(self, "published_sta_local", self.sta_local)

    def as_json(self) -> dict[str, Any]:
        data = asdict(self)
        for key in (
            "flight_date",
            "std_local",
            "sta_local",
            "published_std_local",
            "published_sta_local",
        ):
            data[key] = data[key].isoformat()
        return data


class ScheduleStore(Protocol):
    def has_day(self, day: date) -> bool: ...
    def load_operating_day(
        self, day: date, op_day: OperatingDay, *, force: bool = False
    ) -> bool: ...
    def flights_for_aircraft(self, day: date, aircraft_reg: str) -> list[Flight]: ...
    def record_defect(self, defect: dict[str, Any]) -> dict[str, Any]: ...
    def get_defect(self, trace: str) -> dict[str, Any]: ...
    def record_delay(self, delay: dict[str, Any]) -> dict[str, Any]: ...
    def get_delay(self, trace: str) -> dict[str, Any] | None: ...
    def event_traces(self, day: date, digits: str) -> list[str]: ...
    def recovery_inventory(self, day: date) -> dict[str, Any]: ...
    def domain_inventory(self, day: date, domain: str) -> dict[str, Any]: ...
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
    ) -> None: ...


def lisbon_today(now: datetime | None = None) -> date:
    return (now or datetime.now(LISBON)).astimezone(LISBON).date()


def _versioned(flight: dict[str, Any]) -> dict[str, Any]:
    """A served flight with its version, which a change to it must send as If-Match."""
    return {**flight, "version": flight_version(flight)}


class ScheduleService:
    def __init__(self, store: ScheduleStore) -> None:
        self._store = store

    def ensure_day(self, day: date) -> None:
        if not self._store.has_day(day):
            from airline_systems.operating_day import build_operating_day

            self._store.load_operating_day(day, build_operating_day(day))

    def aircraft_rotation(self, aircraft_reg: str, day: date) -> dict[str, Any]:
        reg = aircraft_reg.strip().upper()
        self.ensure_day(day)
        flights = sorted(self._store.flights_for_aircraft(day, reg), key=lambda f: f.std_local)
        return {
            "aircraft_reg": reg,
            "date": day.isoformat(),
            "as_of_local": datetime.combine(day, DEMO_CLOCK).isoformat(),
            "flights": [_versioned(flight.as_json()) for flight in flights],
        }

    def network(self, day: date) -> dict[str, Any]:
        from airline_systems.operating_day import AIRPORTS

        self.ensure_day(day)
        inventory = self._store.recovery_inventory(day)
        flights = sorted(inventory["flights"], key=lambda f: (f["std_local"], f["flight_no"]))
        airports = {f[key] for f in flights for key in ("origin", "destination")}
        return {
            "operating_date": day.isoformat(),
            "as_of_local": datetime.combine(day, DEMO_CLOCK).isoformat(),
            "timezone": "Europe/Lisbon",
            "flights": [_versioned(flight) for flight in flights],
            "aircraft": inventory["aircraft"],
            "airports": [
                {"iata": code, "position": [AIRPORTS[code][1], AIRPORTS[code][0]]}
                for code in sorted(airports)
            ],
        }
