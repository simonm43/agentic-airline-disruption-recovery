"""The demo cases as one-click messages, and the form's limits (business UI and free-text
spec §6)."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

Kind = Literal["message", "flight_delay", "aircraft_defect", "weather_update"]
# border_control: border-control queues (Entry/Exit System checks), IATA delay code 86.
CAUSES = ("staff_shortage", "baggage", "late_inbound", "border_control", "other")
LISBON = ZoneInfo("Europe/Lisbon")
# Case 5: an amended Lisbon Terminal Aerodrome Forecast (TAF), written for the demo, not issued by
# IPMA. A TAF names only days of the month, so it is written for the day it is sent: a westerly
# gale gusting 35 kt from 16:00 to 18:00 UTC today, across Lisbon's single runway 02/20 (ARROW
# computes 33 kt of crosswind in the gusts), with showers and no thunderstorm. The runway stays open
# at a reduced rate.
TAF = (
    "TAF AMD LPPT {today}0920Z {today}10/{tomorrow}12 19012KT 9999 SCT020 BKN040"
    " BECMG {today}13/{today}15 21018G28KT 6000 RA BKN012 TEMPO {today}16/{today}18 27022G35KT"
    " 6000 SHRA BKN015 BECMG {today}18/{today}20 30015KT 9999 NSW SCT020"
)
WEATHER_UTC_HOURS = (16, 18)  # the TEMPO group above
WEATHER_RATE = 24  # Lisbon's movements an hour in the crosswind, as ARROW plans it
CROSSWIND_KT = 33  # the TEMPO group's gusts across runway 02/20, as ARROW computes them
# What settles Case 5 once OCC approved its plan (case summary and held storm spec §2), written for
# the demo: an observation (METAR) at 14:20 UTC with the westerly gusts already across the runway
# and stronger gusts expected within two hours confirms it; a TAF amended at 13:10 UTC whose winds
# stay along the runway drops it.
METAR_CONFIRMED = (
    "METAR LPPT {today}1420Z 26021G34KT 9999 -SHRA FEW012 BKN025 15/11 Q1002"
    " TEMPO 27022G35KT 6000 SHRA"
)
TAF_DROPPED = (
    "TAF AMD LPPT {today}1310Z {today}13/{tomorrow}12 21015KT 9999 SCT025 BKN040"
    " TEMPO {today}15/{today}19 22015G25KT 8000 -RA BKN015 BECMG {today}19/{today}21 30012KT NSW"
    " SCT030"
)


def _days(day: date) -> dict[str, str]:
    """A TAF's day fields for Lisbon operating day ``day``: its day and the next, month end
    included."""
    return {"today": f"{day.day:02d}", "tomorrow": f"{(day + timedelta(days=1)).day:02d}"}


def _taf(day: date) -> str:
    """The TAF for Lisbon operating day ``day``."""
    return TAF.format(**_days(day))


def _weather_local(day: date) -> tuple[datetime, datetime]:
    """The forecast's window in Lisbon time on ``day``, converted as ARROW converts the TAF
    (Europe/Lisbon): 16:00–18:00 in winter, 17:00–19:00 in summer."""
    start, end = (
        datetime.combine(day, time(hour), tzinfo=UTC).astimezone(LISBON)
        for hour in WEATHER_UTC_HOURS
    )
    return start, end


def _taf_title(day: date) -> str:
    """Case 5's summary with the forecast in Lisbon time on ``day``. TAF spelled out (owner:
    "write out what TAF is")."""
    start, end = _weather_local(day)
    return (
        f"Terminal Aerodrome Forecast (TAF) · Lisbon · strong crosswind {start:%H:%M}–{end:%H:%M}"
        f" · gusts {CROSSWIND_KT} kt across runway 02/20 · {WEATHER_RATE} movements an hour"
        " · plans for OCC"
    )


def _update(text: str) -> Callable[[date], dict[str, Any]]:
    """A weather update's fields written for the day it is sent: the forecast's TAF window
    (days of the month, UTC hours) and the report it quotes."""

    def on(day: date) -> dict[str, Any]:
        days = _days(day)
        start, end = WEATHER_UTC_HOURS
        return {
            "taf_window": f"{days['today']}{start:02d}/{days['today']}{end:02d}",
            "description": text.format(**days),
        }

    return on


def _confirmed_title(day: date) -> str:
    start, end = _weather_local(day)
    return (
        f"Meteorological Aerodrome Report (METAR) · Lisbon · strong crosswind {start:%H:%M}–"
        f"{end:%H:%M} confirmed · held actions sent after a re-check"
    )


def _dropped_title(day: date) -> str:
    start, end = _weather_local(day)
    return (
        f"Terminal Aerodrome Forecast (TAF) amended · Lisbon · no strong crosswind {start:%H:%M}–"
        f"{end:%H:%M} · standby crews released, passengers told no change"
    )


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    kind: Kind
    data: dict[str, Any]
    case: int = 0
    title: str = ""
    expected: str = ""  # the outcome under the release rules (rules v1)
    # A message and summary written for the day it is sent (Case 5's TAF); None when fixed.
    written: Callable[[date], str] | None = field(default=None, compare=False)
    titled: Callable[[date], str] | None = field(default=None, compare=False)
    # Fields written for the day it is sent (Case 5's weather updates); None when fixed.
    dated: Callable[[date], dict[str, Any]] | None = field(default=None, compare=False)

    def data_on(self, day: date) -> dict[str, Any]:
        """The event's data as sent on Lisbon operating day ``day``."""
        data = {**self.data, "text": self.written(day)} if self.written else dict(self.data)
        return {**data, **self.dated(day)} if self.dated else data

    def title_on(self, day: date) -> str:
        """The summary as shown on Lisbon operating day ``day``."""
        return self.titled(day) if self.titled else self.title


def _delay(flight: str, minutes: int, cause: str, description: str) -> dict[str, Any]:
    return {
        "flight_no": flight,
        "delay_minutes": minutes,
        "cause": cause,
        "description": description,
    }


def _message(text: str, system: str) -> dict[str, Any]:
    return {"text": text, "system": system}


# The demo cases as free text, as the airline's systems would write it (D94); ARROW's
# orchestrator reads them. Expected outcomes hold under the release rules (rules v1).
PRESETS: tuple[Preset, ...] = (
    Preset(
        "ar259-20",
        "Case 1 · Simple delay",
        "message",
        # AR259 flies Lisbon to Belém, outside the Schengen area: its passengers pass exit
        # border control, where the Entry/Exit System's checks queue them.
        _message(
            "Border-control queues (Entry/Exit System checks) are holding passengers for AR259:"
            " it will leave about 20 minutes late.",
            "Airport operations",
        ),
        1,
        "AR259 · 20 min late · border-control queue · retimed automatically",
        "Executed automatically",
    ),
    Preset(
        "ar444-70",
        "Case 2 · Delay with rebooking",
        "message",
        _message(
            "Ground handling in Hamburg is short-staffed; AR444 is expected to depart 70 minutes"
            " late.",
            "Ground handling",
        ),
        2,
        "AR444 · 70 min late · rebooking and cars within policy",
        "Executed automatically",
    ),
    Preset(
        "cstqa-aog",
        "Case 3 · Aircraft defect",
        "message",
        _message(
            "Hydraulic leak found on CS-TQA during the walk-around. Maintenance expects it out of"
            " service for about 4 hours.",
            "Maintenance",
        ),
        3,
        "CS-TQA · out of service 4 h · ranked plans for OCC",
        "Waiting for OCC",
    ),
    Preset(
        "ar689-60",
        "Case 4 · Crew limit",
        "message",
        _message("Late inbound aircraft: AR689 will leave 60 minutes late.", "Ops control"),
        4,
        "AR689 · 60 min late · crew over limit · last Ponta Delgada standby crew · OCC decides",
        "Waiting for OCC",
    ),
    Preset(
        "lppt-taf",
        "Case 5 · Crosswind forecast",
        "message",
        {"system": "IPMA (weather)"},
        5,
        "",
        "Waiting for OCC",
        written=_taf,
        titled=_taf_title,
    ),
    # Sent after OCC approves the weather plan (owner: "inform passengers ... and have
    # some alternative plans" before the weather is known): typed weather updates ARROW matches
    # to the open weather case at that airport and window.
    Preset(
        "storm-confirmed",
        "Case 5 · Crosswind confirmed (METAR)",
        "weather_update",
        {
            "airport": "LPPT",
            "hazard": "crosswind",
            "status": "confirmed",
            "rate_per_hour": WEATHER_RATE,
        },
        5,
        "",
        "Executed after approval",
        titled=_confirmed_title,
        dated=_update(METAR_CONFIRMED),
    ),
    Preset(
        "storm-dropped",
        "Case 5 · Forecast dropped (TAF amended)",
        "weather_update",
        {"airport": "LPPT", "hazard": "crosswind", "status": "dropped"},
        5,
        "",
        "Stood down",
        titled=_dropped_title,
        dated=_update(TAF_DROPPED),
    ),
)


def _minutes(value: Any, low: int, high: int, step: int) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and low <= value <= high
        and value % step == 0
    )


def validate(kind: str, data: dict[str, Any]) -> dict[str, Any]:
    """The event's data with only its type's fields; ValueError lists every field error."""
    errors: list[str] = []
    if kind == "message":
        text = str(data.get("text", "")).strip()
        system = str(data.get("system") or "").strip()
        if not 3 <= len(text) <= 2000:
            errors.append("text: 3–2,000 characters")
        if len(system) > 100:
            errors.append("system: at most 100 characters")
        if errors:
            raise ValueError("; ".join(errors))
        return {"text": text, **({"system": system} if system else {})}
    description = str(data.get("description", "")).strip()
    if not 3 <= len(description) <= 1000:
        errors.append("description: 3–1,000 characters")
    if kind == "flight_delay":
        clean = {key: data.get(key) for key in ("flight_no", "delay_minutes", "cause")}
        if not re.fullmatch(r"AR[0-9]{1,4}", str(clean["flight_no"] or "")):
            errors.append("flight_no: AR and 1–4 digits")
        if not _minutes(clean["delay_minutes"], 5, 180, 5):
            errors.append("delay_minutes: 5–180, multiple of 5")
        if clean["cause"] not in CAUSES:
            errors.append(f"cause: one of {', '.join(CAUSES)}")
    elif kind == "aircraft_defect":
        clean = {key: data.get(key) for key in ("aircraft_reg", "unavailable_minutes")}
        if not re.fullmatch(r"CS-[A-Z]{3}", str(clean["aircraft_reg"] or "")):
            errors.append("aircraft_reg: CS- and 3 letters")
        if not _minutes(clean["unavailable_minutes"], 1, 720, 1):
            errors.append("unavailable_minutes: 1–720")
    else:
        raise ValueError("kind: flight_delay or aircraft_defect")
    if errors:
        raise ValueError("; ".join(errors))
    return {**clean, "description": description}
