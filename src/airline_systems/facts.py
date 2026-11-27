"""A disruption as the airline systems are asked about it: by its facts (owner).

During an assessment ARROW and its agents only read the airline's systems, and each read names
the disruption by its facts: a delay's flight, minutes and cause; a defect's aircraft and time
out of service; a weather forecast's airport, hazard, window and reduced hourly rate. A read never
names an ARROW case, a trace or a record. The system answers as the disruption would stand if it
were recorded now: a reported defect's aircraft is out of service in what it computes, as
Maintenance marks it once the defect is recorded.

Only ARROW's executor records a disruption, as the first action of a plan it carries out (a delay
in operations control, a defect in Maintenance), and the system issues its event id then. A
weather forecast is recorded in no airline system: the met service owns it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from airline_systems.operating_day import HUB, SLOT_CAPACITY, WEATHER_PLAYBOOK_PERCENT
from airline_systems.schedule import DEMO_CLOCK, ScheduleStore, lisbon_today

# border_control: border-control queues (Entry/Exit System checks), IATA delay code 86
# (immigration, customs, health).
DelayCause = Literal["staff_shortage", "baggage", "late_inbound", "border_control", "other"]
Kind = Literal["flight_delay", "aircraft_defect", "weather"]
# The facts of each kind of disruption, in the order a read names them.
FIELDS: dict[str, tuple[str, ...]] = {
    "flight_delay": ("flight_no", "delay_minutes", "cause"),
    "aircraft_defect": ("aircraft_reg", "unavailable_minutes"),
    "weather": ("airport", "hazard", "window_start", "window_end", "hourly_rate"),
}
_CLOCK = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
SHORTEST_WINDOW = timedelta(minutes=30)
LONGEST_WINDOW = timedelta(hours=6)


class FactsRefused(ValueError):
    """The facts do not name a disruption a system can read (422), in plain words."""


class Facts(BaseModel):
    """One disruption's facts: its type and that type's own fields. Fields of another type are
    read and ignored, so an agent may pass the disruption as its brief gives it."""

    model_config = ConfigDict(extra="ignore")
    type: Kind
    flight_no: str | None = Field(default=None, pattern=r"^AR[0-9]{1,4}$")
    delay_minutes: int | None = Field(default=None, ge=5, le=180, multiple_of=5)
    cause: DelayCause | None = None
    aircraft_reg: str | None = Field(default=None, pattern=r"^CS-[A-Z]{3}$")
    unavailable_minutes: int | None = Field(default=None, ge=1, le=720)
    airport: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    hazard: str | None = Field(default=None, pattern=r"^[a-z_]{1,32}$")
    window_start: str | None = Field(default=None, pattern=_CLOCK)
    window_end: str | None = Field(default=None, pattern=_CLOCK)
    hourly_rate: int | None = Field(default=None, ge=1, le=120)

    @model_validator(mode="before")
    @classmethod
    def _only_its_type(cls, values: Any) -> Any:
        """Another type's fields are not this disruption's: they are not checked either."""
        if not isinstance(values, Mapping) or values.get("type") not in FIELDS:
            return values
        own = FIELDS[str(values["type"])]
        return {key: value for key, value in values.items() if key == "type" or key in own}

    @model_validator(mode="after")
    def _complete(self) -> Self:
        missing = [name for name in FIELDS[self.type] if getattr(self, name) is None]
        if missing:
            raise ValueError(
                f"a {self.type} names {_listed(FIELDS[self.type])}; missing {_listed(missing)}"
            )
        return self

    def named(self) -> dict[str, Any]:
        """The type and its own fields, as a read names them (and its audit records them)."""
        return {"type": self.type, **{name: getattr(self, name) for name in FIELDS[self.type]}}


def _listed(names: tuple[str, ...] | list[str]) -> str:
    items = list(names)
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def facts_of(values: Mapping[str, Any]) -> Facts:
    """The facts in ``values`` (a read's query parameters or a tool's argument); FactsRefused
    names each field at fault."""
    try:
        return Facts.model_validate(dict(values))
    except ValidationError as error:
        raise FactsRefused(
            "; ".join(
                f"{'.'.join(str(part) for part in issue['loc']) or 'disruption'}:"
                f" {str(issue['msg']).removeprefix('Value error, ')}"
                for issue in error.errors()
            )
        ) from None


def _now(day: date) -> datetime:
    return datetime.combine(day, DEMO_CLOCK)


def reported_delay(
    store: ScheduleStore, flight_no: str, delay_minutes: int, cause: str
) -> dict[str, Any]:
    """A flight delay as operations control would record it now: its flight is today's, still
    scheduled and yet to leave at the operating clock; ValueError says why not."""
    day = lisbon_today()
    flight = next(
        (f for f in store.recovery_inventory(day)["flights"] if f["flight_no"] == flight_no), None
    )
    if flight is None:
        raise ValueError(f"{flight_no} is not in today's operating day")
    if flight["status"] != "scheduled":
        raise ValueError(f"{flight_no} is {flight['status']}; a delay cannot apply")
    now = _now(day)
    if datetime.fromisoformat(flight["std_local"]) <= now:
        raise ValueError(f"{flight_no} has already departed at the demo clock")
    return {
        "type": "flight_delay",
        "flight_no": flight_no,
        "delay_minutes": delay_minutes,
        "cause": cause,
        "aircraft_reg": flight["aircraft_reg"],
        "operating_date": day.isoformat(),
        "reported_at": now.isoformat(),
    }


def reported_defect(
    store: ScheduleStore, aircraft_reg: str, unavailable_minutes: int
) -> dict[str, Any]:
    """An aircraft defect as Maintenance would record it now: the aircraft flies today and is
    out of service from the operating clock for maintenance's estimate; ValueError says why
    not. Every system reads the day's flights from the inventory it holds a grant on (only
    operations control and Maintenance may read the operating day itself)."""
    day = lisbon_today()
    flights = store.recovery_inventory(day)["flights"]
    if not any(flight["aircraft_reg"] == aircraft_reg for flight in flights):
        raise ValueError("Aircraft has no rotation in today's operating day")
    start = _now(day)
    return {
        "type": "aircraft_defect",
        "aircraft_reg": aircraft_reg,
        "unavailable_minutes": unavailable_minutes,
        "operating_date": day.isoformat(),
        "reported_at": start.isoformat(),
        "unavailable_until": (start + timedelta(minutes=unavailable_minutes)).isoformat(),
    }


def reported_weather(
    airport: str, hazard: str, window_start: str, window_end: str, hourly_rate: int
) -> dict[str, Any]:
    """A weather forecast as operations control plans it: at Lisbon, a hazard the playbook plans
    for, a window from 30 min to 6 h after the operating clock today, at a reduced rate no higher
    than the airport's normal one; ValueError says why not."""
    if airport != HUB:
        raise ValueError(f"Weather is planned for Lisbon (LIS) only, not {airport}")
    if hazard not in WEATHER_PLAYBOOK_PERCENT:
        *others, last = WEATHER_PLAYBOOK_PERCENT
        raise ValueError(f"The hazard must be {', '.join(others)} or {last}")
    day = lisbon_today()
    now = _now(day)
    start = datetime.combine(day, time.fromisoformat(window_start))
    end = datetime.combine(day, time.fromisoformat(window_end))
    if start <= now:
        raise ValueError(f"The weather window must start after the operating clock ({now:%H:%M})")
    if not SHORTEST_WINDOW <= end - start <= LONGEST_WINDOW:
        raise ValueError("The weather window must last from 30 min to 6 h and end today")
    normal = SLOT_CAPACITY[airport]
    if hourly_rate > normal:
        raise ValueError(
            f"The reduced rate must not exceed {airport}'s normal {normal} movements an hour"
        )
    return {
        "type": "weather",
        "airport": airport,
        "hazard": hazard,
        "window_start": window_start,
        "window_end": window_end,
        "window_start_local": start.isoformat(),
        "window_end_local": end.isoformat(),
        "hourly_rate": hourly_rate,
        "normal_rate": normal,
        "operating_date": day.isoformat(),
        "reported_at": now.isoformat(),
    }


def reported(store: ScheduleStore, facts: Facts) -> dict[str, Any]:
    """The disruption these facts report, as the systems compute from it today; ValueError when
    it does not fit today's operation."""
    if facts.type == "flight_delay":
        assert facts.flight_no and facts.delay_minutes and facts.cause
        return reported_delay(store, facts.flight_no, facts.delay_minutes, facts.cause)
    if facts.type == "aircraft_defect":
        assert facts.aircraft_reg and facts.unavailable_minutes
        return reported_defect(store, facts.aircraft_reg, facts.unavailable_minutes)
    assert facts.airport and facts.hazard and facts.window_start and facts.window_end
    assert facts.hourly_rate
    return reported_weather(
        facts.airport, facts.hazard, facts.window_start, facts.window_end, facts.hourly_rate
    )


def inventory(
    store: ScheduleStore, disruption: Mapping[str, Any], day: date | None = None
) -> dict[str, Any]:
    """Today's flights and aircraft as a read about ``disruption`` computes them: a reported
    defect's aircraft is out of service, as Maintenance marks it when the defect is recorded."""
    found = store.recovery_inventory(day or lisbon_today())
    if disruption.get("type") != "aircraft_defect":
        return found
    registration = disruption.get("aircraft_reg")
    return {
        **found,
        "aircraft": [
            {**aircraft, "status": "unserviceable"}
            if aircraft.get("registration") == registration
            else aircraft
            for aircraft in found["aircraft"]
        ],
    }


def same_record(
    existing: Mapping[str, Any], report: BaseModel, fields: tuple[str, ...], kind: str
) -> None:
    """A record sent again under its trace reports what the stored one reports, in the fields
    the report gave (never those the system derived as it recorded it, which the operation may
    have changed since); ValueError when it reports something else."""
    if any(existing.get(name) != getattr(report, name) for name in fields):
        raise ValueError(f"A different {kind} already uses this trace")


def recorded(store: ScheduleStore, trace: str) -> dict[str, Any]:
    """The disruption ARROW's executor recorded under ``trace`` today (a delay, else a defect),
    in the shape its options are computed from; ValueError when there is none."""
    delay = store.get_delay(trace)
    found = {**delay, "type": "flight_delay"} if delay else {**store.get_defect(trace)}
    found.setdefault("type", "aircraft_defect")
    if found.get("operating_date") != lisbon_today().isoformat():
        raise ValueError("This disruption belongs to an earlier operating day")
    return found
