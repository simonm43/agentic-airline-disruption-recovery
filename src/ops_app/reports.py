"""What OCC (or a system event) reports: an aircraft defect, a flight delay, weather read from a
Terminal Aerodrome Forecast (TAF) or a message."""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag, ValidationError

from airline_systems.operating_day import CITY, HUB, ICAO

LISBON = ZoneInfo("Europe/Lisbon")
# The hazards the weather playbook plans for, as a weather case names them (Case 5).
HAZARD_WORDS = {
    "thunderstorm": "Thunderstorms",
    "crosswind": "Strong crosswind",
    "low_visibility": "Low visibility",
}
Hazard = Literal["thunderstorm", "crosswind", "low_visibility"]
DelayCause = Literal["staff_shortage", "baggage", "late_inbound", "border_control", "other"]
# Lisbon has one runway in use, 02/20 (headings 020° and 200°). A wind across it slows the
# arrival rate; the runway stays open. Code computes the crosswind from the TAF's wind: the
# component across the runway of the gust (or the mean wind without one),
# speed × |sin(direction − 200°)|, never the model.
RUNWAY = "02/20"
RUNWAY_HEADING = 200
# The crosswind (kt) from which the OCC playbook plans the reduced rate on Lisbon's runway.
CROSSWIND_FROM_KT = 25
# An airline system's own event id (EV- and six hex digits), as a typed event that its source
# had recorded already carries it.
SOURCE_EVENT_ID = r"^EV-[0-9A-F]{6}$"


class DefectReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["aircraft_defect"] = "aircraft_defect"
    aircraft_reg: str = Field(pattern=r"^CS-[A-Z]{3}$")
    description: str = Field(min_length=3, max_length=1000)
    unavailable_minutes: int = Field(strict=True, ge=1, le=720)
    event_id: uuid.UUID | None = None
    # The event id of the system that published it, when it has recorded the defect already
    # (Maintenance): ARROW keeps it and never records the defect again.
    source_event_id: str | None = Field(default=None, pattern=SOURCE_EVENT_ID)


class DelayReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["flight_delay"]
    flight_no: str = Field(pattern=r"^AR[0-9]{1,4}$")
    delay_minutes: int = Field(strict=True, ge=5, le=180, multiple_of=5)
    # border_control: border-control queues (Entry/Exit System checks), IATA delay code 86.
    cause: DelayCause
    description: str = Field(min_length=3, max_length=1000)
    event_id: uuid.UUID | None = None
    # The event id of the system that published it, when it has recorded the delay already
    # (operations control): ARROW keeps it and never records the delay again.
    source_event_id: str | None = Field(default=None, pattern=SOURCE_EVENT_ID)


class WeatherReport(BaseModel):
    """A weather forecast for an airport as the TAF writes it: the ICAO code and the change
    group's validity in UTC (DDHH/DDHH). ``storm_window`` converts both in code."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["weather"]
    airport: str = Field(pattern=r"^[A-Z]{4}$")
    taf_window: str = Field(pattern=r"^[0-9]{4}/[0-9]{4}$")
    hazard: Hazard
    description: str = Field(min_length=3, max_length=1000)
    event_id: uuid.UUID | None = None


def storm_window(airport: str, taf_window: str, day: date) -> dict[str, str]:
    """The forecast's airport (IATA) and its window as Lisbon wall-clock times on the operating
    day ``day``. The TAF names only the day of the month, today's or tomorrow's, and gives UTC;
    Europe/Lisbon decides the offset (UTC+0 in winter, UTC+1 in summer). ValueError says in
    plain words why the weather cannot be planned."""
    iata = {code: name for name, code in ICAO.items()}.get(airport)
    if iata != HUB:
        raise ValueError(f"ARROW plans weather at {CITY[HUB]} ({ICAO[HUB]}) only, not {airport}")
    days = {on.day: on for on in (day, day + timedelta(days=1))}

    def moment(day_of_month: str, hour: str) -> datetime | None:
        on = days.get(int(day_of_month))
        if on is None or int(hour) > 24:
            return None
        return datetime(on.year, on.month, on.day, tzinfo=UTC) + timedelta(hours=int(hour))

    group = re.fullmatch(r"([0-9]{2})([0-9]{2})/([0-9]{2})([0-9]{2})", taf_window)
    start, end = (moment(group[1], group[2]), moment(group[3], group[4])) if group else (None, None)
    if start is None or end is None:
        raise ValueError(f"taf_window: {taf_window} is not a TAF period of today or tomorrow")
    local = [at.astimezone(LISBON) for at in (start, end)]
    if any(at.date() != day for at in local):
        raise ValueError(
            f"taf_window: {taf_window} does not start and end on today's operating day"
            f" ({day.day} {day:%B})"
        )
    return {"airport": iata, "window_start": f"{local[0]:%H:%M}", "window_end": f"{local[1]:%H:%M}"}


def storm_subject(disruption: Mapping[str, Any]) -> str:
    """What a weather case is about, from its hazard and airport: "Strong crosswind at
    Lisbon"."""
    hazard, airport = str(disruption.get("hazard")), str(disruption.get("airport"))
    return f"{HAZARD_WORDS.get(hazard, hazard)} at {CITY.get(airport, airport)}"


def storm_period(disruption: Mapping[str, Any]) -> str:
    """A weather window in Lisbon wall-clock time: "16:00–18:00"."""
    return f"{disruption.get('window_start')}–{disruption.get('window_end')}"


def storm_title(disruption: Mapping[str, Any]) -> str:
    """A weather case's title: "Strong crosswind at Lisbon · 16:00–18:00 · gusts 33 kt across
    runway 02/20 · 24 movements an hour (normally 40)", the crosswind as code computed it and
    the rate once the operations control system recorded the forecast."""
    title = f"{storm_subject(disruption)} · {storm_period(disruption)}"
    across = disruption.get("crosswind_kt")
    if isinstance(across, int) and not isinstance(across, bool):
        gusting = "G" in str(disruption.get("wind") or "")
        title += f" · {'gusts ' if gusting else ''}{across} kt across runway {RUNWAY}"
    rate, normal = disruption.get("hourly_rate"), disruption.get("normal_rate")
    if isinstance(rate, int) and isinstance(normal, int):
        title += f" · {rate} movements an hour (normally {normal})"
    return title


WIND = re.compile(r"([0-9]{3}|VRB)([0-9]{2,3})(?:G([0-9]{2,3}))?KT")


def crosswind(group: str) -> tuple[str, int] | None:
    """The TAF group's wind with the strongest crosswind on Lisbon's runway 02/20, and that
    crosswind in knots (rounded): the component across the runway of the gust, or of the mean
    wind without one. A variable wind (VRB) has no direction across the runway; None without a
    wind."""
    found: tuple[str, int] | None = None
    for token in group.split():
        wind = WIND.fullmatch(token)
        if wind is None or wind[1] == "VRB":
            continue
        speed = int(wind[3] or wind[2])
        across = round(speed * abs(math.sin(math.radians(int(wind[1]) - RUNWAY_HEADING))))
        if found is None or across > found[1]:
            found = (token, across)
    return found


def crosswind_details(text: str, taf_window: str) -> dict[str, Any]:
    """The crosswind a TAF's group for ``taf_window`` forecasts on runway 02/20, as code
    computed it: the wind as written, the crosswind in knots and the runway; empty if the text
    holds no such group or wind."""
    group = _group(text, taf_window)
    found = crosswind(group) if group is not None else None
    if found is None:
        return {}
    return {"wind": found[0], "crosswind_kt": found[1], "runway": RUNWAY}


def _forecasts(hazard: str, group: str) -> bool:
    """True if a TAF group's tokens forecast ``hazard``: thunderstorm TS (TSRA, +TSRA, VCTS…) or
    CB cloud; crosswind a wind across runway 02/20 of 25 kt or more (code computes it);
    low_visibility under 800 m or fog at the aerodrome (FG, FZFG, PRFG; not patches BCFG,
    shallow MIFG or nearby VCFG)."""
    tokens = group.split()
    if hazard == "thunderstorm":
        return any(
            re.fullmatch(r"[+-]?(VC)?TS[A-Z]*", token)
            or re.fullmatch(r"(FEW|SCT|BKN|OVC)[0-9]{3}CB", token)
            for token in tokens
        )
    if hazard == "crosswind":
        found = crosswind(group)
        return found is not None and found[1] >= CROSSWIND_FROM_KT
    return any(
        (re.fullmatch(r"[0-9]{4}", token) and int(token) < 800)
        or re.fullmatch(r"(FZ|PR)?FG", token)
        for token in tokens
    )


_HAZARD_MISSING = {
    "thunderstorm": "no thunderstorm (TS or CB)",
    "crosswind": f"no wind across runway {RUNWAY} of {CROSSWIND_FROM_KT} kt or more",
    "low_visibility": "no visibility under 800 m or fog",
}


def _missing(hazard: str, group: str) -> str:
    """What a TAF group that does not forecast ``hazard`` says instead, in words. A variable
    wind (VRB) has no direction, so code computes no crosswind from it."""
    if hazard == "crosswind":
        for token in group.split():
            wind = WIND.fullmatch(token)
            if wind is not None and wind[1] == "VRB":
                return (
                    f"a variable wind ({token}): ARROW cannot compute a crosswind on runway"
                    f" {RUNWAY} without a direction"
                )
    return _HAZARD_MISSING[hazard]


def _group(text: str, period: str) -> str | None:
    """The TAF group whose period is ``period``, as written: its tokens up to the next change
    group; None if the message does not hold that period."""
    found = re.search(rf"(?<![0-9/]){re.escape(period)}(?![0-9/])", text)
    if found is None:
        return None
    rest = text[found.end() :]
    following = re.search(r"\b(?:BECMG|TEMPO|PROB[0-9]{2}|FM[0-9]{6})\b", rest)
    return rest[: following.start()] if following else rest


class MessageReport(BaseModel):
    """A disruption in someone's own words; the Detection agent reads it (business UI and
    free-text spec §3, owner)."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["message"]
    text: str = Field(min_length=3, max_length=2000)
    event_id: uuid.UUID | None = None


class Reading(BaseModel):
    """What the Detection agent read in a report: the typed values ARROW checks and records."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["flight_delay", "aircraft_defect", "weather"]
    flight_no: str = ""
    delay_minutes: int = 0
    cause: str = ""
    aircraft_reg: str = ""
    unavailable_minutes: int = 0
    airport: str = ""
    taf_window: str = ""
    hazard: str = ""
    reading: str = Field(min_length=1, max_length=300)

    def report(self, text: str, day: date) -> dict[str, Any]:
        """The structured report this reading stands for on the operating day ``day``;
        ValueError names each wrong field."""
        description = text.strip()[:1000]
        if self.kind == "flight_delay":
            values: dict[str, Any] = {
                "type": "flight_delay",
                "flight_no": self.flight_no.strip().upper(),
                "delay_minutes": self.delay_minutes,
                "cause": self.cause,
                "description": description,
            }
            model: type[BaseModel] = DelayReport
        elif self.kind == "weather":
            values = {
                "type": "weather",
                "airport": self.airport.strip().upper(),
                "taf_window": self.taf_window.strip(),
                "hazard": self.hazard.strip().lower(),
                "description": description,
            }
            model = WeatherReport
        else:
            values = {
                "type": "aircraft_defect",
                "aircraft_reg": self.aircraft_reg.strip().upper(),
                "unavailable_minutes": self.unavailable_minutes,
                "description": description,
            }
            model = DefectReport
        try:
            report = model.model_validate(values).model_dump(mode="json", exclude_none=True)
        except ValidationError as error:
            raise ValueError(
                "; ".join(
                    f"{'.'.join(str(p) for p in issue['loc'])}: {issue['msg']}"
                    for issue in error.errors()
                )
            ) from None
        if self.kind == "weather":
            # The airport and the period are copied from the TAF; code converts them.
            if not _written(report["airport"], text):
                raise ValueError(f"airport: {report['airport']} is not in the message")
            hazard, period = report["hazard"], report["taf_window"]
            group = _group(text, period)
            if group is None:
                raise ValueError(f"taf_window: {period} is not in the message")
            storm_window(report["airport"], period, day)
            # The hazard must be one the chosen group forecasts, not one the model inferred.
            if not _forecasts(hazard, group):
                raise ValueError(f"hazard: the {period} group forecasts {_missing(hazard, group)}")
            return report
        # The flight or aircraft must be one the message names, not one the model inferred.
        field = "flight_no" if self.kind == "flight_delay" else "aircraft_reg"
        if not _written(report[field], text):
            raise ValueError(f"{field}: {report[field]} is not in the message")
        return report


def _written(value: str, text: str) -> bool:
    """True if ``value`` (AR259, CS-TQA) is in ``text``, whatever its case and spacing
    ("AR 259", "cs-tqa"), and not as part of a longer code (AR2590)."""
    parts = re.findall(r"[A-Z]+|[0-9]+", value.upper())
    pattern = r"(?<![A-Z0-9])" + r"[\s-]*".join(map(re.escape, parts)) + r"(?![A-Z0-9])"
    return bool(parts) and re.search(pattern, text, re.IGNORECASE) is not None


def _report_type(value: Any) -> str:
    # Reports without a type are aircraft defects (the original intake).
    if isinstance(value, dict):
        return str(value.get("type", "aircraft_defect"))
    return str(getattr(value, "type", "aircraft_defect"))


OperatorReport = Annotated[
    Annotated[DefectReport, Tag("aircraft_defect")]
    | Annotated[DelayReport, Tag("flight_delay")]
    | Annotated[MessageReport, Tag("message")],
    Discriminator(_report_type),
]
