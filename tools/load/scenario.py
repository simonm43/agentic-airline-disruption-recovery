"""A day of disruption reports for ARROW's load test, drawn from the generated 900-flight day.

The volume follows public European figures for a day of 900 departures; the figures are
EUROCONTROL's, the mapping to ARROW's reports is our assumption:
- EUROCONTROL CODA Digest 2024 (all causes, ECAC): 30.8% of departures left more than 15 minutes
  late, and reactionary delay (knock-on from earlier flights) was 46% of the delay minutes.
  900 x 30.8% is about 277 late departures; taking 54% of them as primary gives about 150 primary
  delay reports a day. Reactionary delays are the knock-on ARROW predicts, so they are not sent.
- 4 aircraft out of service (about 2% of the 180 aircraft) and 1 crosswind forecast for Lisbon.
- 20% of reports are a person's own words (the Detection agent reads them); the rest arrive typed,
  as the airline's systems send them.
- Delay length: half 15-30 min, 30% 35-60, 15% 65-120, 5% 125-180 (mean about 47 min, above the
  CODA 34.2 min per delayed departure because only delays over 15 minutes are reported).

Every report names a different aircraft, so no report repeats another's disruption, and only
flights after ARROW's demo clock (09:45 Lisbon), which the sample systems accept. The day phase
replays the reports in departure order at a chosen speed; the burst phase sends one report for
each aircraft left over, all at once, to find where ARROW's 15-minute assessment window is missed.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from airline_systems.operating_day import build_operating_day
from testbench.presets import PRESETS, validate

SEED = 20261125
DELAYS = 150
DEFECTS = 4
FREE_TEXT_SHARE = 0.2
DELAY_BINS = ((15, 30, 0.50), (35, 60, 0.30), (65, 120, 0.15), (125, 180, 0.05))
CAUSES = (
    ("staff_shortage", 0.30),
    ("late_inbound", 0.20),
    ("other", 0.20),
    ("baggage", 0.15),
    ("border_control", 0.15),
)
CLOCK = time(9, 45)  # ARROW's demo clock: earlier flights have "already departed"
EARLIEST = time(10, 0)
LEAD_MINUTES = (30, 90)  # a delay is reported this long before departure
SYSTEMS = {
    "staff_shortage": "Ground handling",
    "late_inbound": "Ops control",
    "other": "Ops control",
    "baggage": "Baggage services",
    "border_control": "Airport operations",
}
WRITTEN = {
    "staff_shortage": "Ground handling is short-staffed; {flight} will leave about {minutes}"
    " minutes late.",
    "late_inbound": "Late inbound aircraft: {flight} will leave {minutes} minutes late.",
    "other": "{flight} is expected to depart {minutes} minutes late (operational reasons).",
    "baggage": "Baggage loading problem for {flight}: departure about {minutes} minutes late.",
    "border_control": "Border-control queues are holding passengers for {flight}: it will leave"
    " about {minutes} minutes late.",
}
TYPED = {
    "staff_shortage": "Ground handling short-staffed",
    "late_inbound": "Late arrival of the inbound aircraft",
    "other": "Operational delay",
    "baggage": "Baggage loading",
    "border_control": "Border-control queues",
}


@dataclass(frozen=True)
class Report:
    """One report: when to send it (seconds after its phase starts), its CloudEvent kind and
    data, and the flight or aircraft it is about."""

    offset_seconds: float
    kind: str
    data: dict[str, Any]
    subject: str

    def checked(self) -> dict[str, Any]:
        """The data as the Test Bench sends it (the same validation)."""
        return validate(self.kind, self.data)


def _minutes(rng: random.Random) -> int:
    low, high, _ = rng.choices(DELAY_BINS, weights=[w for *_, w in DELAY_BINS])[0]
    return rng.randrange(low, high + 1, 5)


def _delay(rng: random.Random, flight: str, at: float) -> Report:
    minutes = _minutes(rng)
    cause = rng.choices([c for c, _ in CAUSES], weights=[w for _, w in CAUSES])[0]
    if rng.random() < FREE_TEXT_SHARE:
        text = WRITTEN[cause].format(flight=flight, minutes=minutes)
        return Report(at, "message", {"text": text, "system": SYSTEMS[cause]}, flight)
    data = {
        "flight_no": flight,
        "delay_minutes": minutes,
        "cause": cause,
        "description": f"{TYPED[cause]}: {flight} {minutes} min late",
    }
    return Report(at, "flight_delay", data, flight)


def _defect(rng: random.Random, registration: str, at: float) -> Report:
    hours = rng.choice((1, 2, 3, 4, 5))
    if rng.random() < FREE_TEXT_SHARE:
        text = (
            f"Technical fault found on {registration} during the turnaround check. Maintenance"
            f" expects it out of service for about {hours} hours."
        )
        return Report(at, "message", {"text": text, "system": "Maintenance"}, registration)
    data = {
        "aircraft_reg": registration,
        "unavailable_minutes": hours * 60,
        "description": f"Technical fault on {registration}, about {hours} h out of service",
    }
    return Report(at, "aircraft_defect", data, registration)


def _later_flights(day: date) -> dict[str, list[Any]]:
    """Each aircraft's flights from 10:00 Lisbon, which the demo clock still allows."""
    by_aircraft: dict[str, list[Any]] = {}
    for flight in build_operating_day(day).flights:
        if flight.std_local.time() >= EARLIEST:
            by_aircraft.setdefault(flight.aircraft_reg, []).append(flight)
    return by_aircraft


def day_reports(day: date, *, speed: float, seed: int = SEED) -> tuple[list[Report], list[str]]:
    """The day phase (delays, defects and the crosswind forecast) and the aircraft it left
    unused. Offsets are the report times after the demo clock, divided by ``speed``."""
    rng = random.Random(seed)
    clock = datetime.combine(day, CLOCK)
    by_aircraft = _later_flights(day)
    aircraft = sorted(by_aircraft)
    rng.shuffle(aircraft)
    delayed, defective = aircraft[:DELAYS], aircraft[DELAYS : DELAYS + DEFECTS]
    reports = []
    for registration in delayed:
        flight = rng.choice(by_aircraft[registration])
        reported = max(flight.std_local - timedelta(minutes=rng.uniform(*LEAD_MINUTES)), clock)
        reports.append(_delay(rng, flight.flight_no, (reported - clock).total_seconds() / speed))
    for registration in defective:
        first = min(by_aircraft[registration], key=lambda f: f.std_local)
        reported = max(first.std_local - timedelta(minutes=45), clock)
        reports.append(_defect(rng, registration, (reported - clock).total_seconds() / speed))
    taf = next(p for p in PRESETS if p.key == "lppt-taf")
    reports.append(Report(60.0, "message", taf.data_on(day), "LPPT"))
    return sorted(reports, key=lambda r: r.offset_seconds), aircraft[DELAYS + DEFECTS :]


def burst_reports(day: date, unused: list[str], *, seed: int = SEED) -> list[Report]:
    """One delay report for each aircraft the day phase left alone, all sent at once."""
    rng = random.Random(seed + 1)
    by_aircraft = _later_flights(day)
    return [_delay(rng, rng.choice(by_aircraft[reg]).flight_no, 0.0) for reg in unused]
