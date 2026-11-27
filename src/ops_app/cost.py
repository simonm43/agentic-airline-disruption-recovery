"""Indicative recovery cost, bottom-up (passenger cost spec): airline cost + compensation +
care + rebooking.

Each part is counted once, from Booking's journey facts for the plan:
- Airline cost: EUROCONTROL Standard Inputs v10.0.1 (2022 prices). Delays at the strategic at-gate
  cost, Table 16.2, which holds no passenger cost and no knock-on delay to other aircraft (the
  plan's own later flights are priced one by one). Cancellations at the day-of-operation cost
  less its passenger share, Table 14.1. That remainder already holds average refunds and lost
  future custom, so neither is added again (cancellation and rebooking cost research §1-§2). A
  late flight that makes up minutes in the air adds their fuel and, when it pays for its carbon
  (the EU Emissions Trading System; the UK and Swiss schemes for arrivals from there), the CO2
  allowances (cost index and time value research); operations control decides the minutes.
- Compensation: Regulation (EC) 261/2004 Arts 5(1)(c)(iii), 7(1), 7(2) and 7(4), per booking, by
  great-circle distance to the final destination. Extraordinary circumstances are not assumed,
  except for a storm's plans, which owe none: weather incompatible with operating the flight, and
  the capacity cut it causes, is an extraordinary circumstance (recitals 14 and 15, Art 5(3)),
  and so is it for the same aircraft's later flights it directly delays (C-74/19). Care and
  rebooking are still owed (C-12/11 McDonagh). The workings say what would otherwise be owed.
- Care: Art 9 meals by waiting time (Steer for the Commission, 2020, Table C.1, 2018 prices);
  hotel, two taxi trips and evening meal and breakfast for passengers who stay overnight (rooms:
  INE Grande Lisboa average room rate; taxis: Lisbon airport fixed fares). Lisbon rates apply
  everywhere; communication is left out.
- Rebooking (cancellation and rebooking cost research §5): per passenger by the journey's EU261
  distance band, from Steer 2020 Table C.1 (2018 prices). Rebooked on the airline's own flights:
  the handling cost (a sourced assumption, low confidence). Left for Passenger Services: getting
  them to their destination on another airline, the average fare with a third at double
  (derived, low-medium confidence).

Indicative only: these are published averages and labelled assumptions, not the airline's
contracted rates or a quote. Decisions use the base rates; low and high vary hotel and taxi.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, time
from typing import Any

from airline_systems.defects import AIRBORNE_SHARE, MADE_UP_CAP_MINUTES, MADE_UP_SHARE
from airline_systems.operating_day import (
    CO2_T_PER_T_FUEL,
    EUA_EUR_PER_T,
    FUEL_EUR_PER_KG,
    FUEL_KG_PER_MINUTE,
    WIDE_BODY_FROM_SEATS,
)

BASIS = (
    "indicative: EUROCONTROL Standard Inputs v10.0.1 Tables 14.1 (cancellation less passenger "
    "share) and 16.2 (strategic at-gate), 2022 prices; EU261 Arts 5 and 7 per booking; care at "
    "Steer 2020 (2018 prices), INE Grande Lisboa room rates, Lisbon airport taxi fares; "
    "rebooking at Steer 2020 Table C.1 (2018 prices): own flights a sourced assumption, another "
    "airline derived; weather plans owe no compensation (extraordinary circumstances)"
)
AIRLINE_EUR_PER_MINUTE = 17.78
# Fuel and CO2 to make up a minute in the air, from the figures operations control decides with
# (airline_systems.operating_day; delay recovery research and cost index and time value
# research). The at-gate rate above holds only APU fuel, not this. CO2 counts when the flight pays
# for its carbon. The crew and maintenance time a faster flight saves is not subtracted
# (conservative); the passenger goodwill that decides whether to speed up is never counted.
FUEL_EUR_PER_MINUTE = {
    body: round(kg * FUEL_EUR_PER_KG, 2) for body, kg in FUEL_KG_PER_MINUTE.items()
}
CO2_EUR_PER_MINUTE = {
    body: round(kg * CO2_T_PER_T_FUEL * EUA_EUR_PER_T / 1000, 2)
    for body, kg in FUEL_KG_PER_MINUTE.items()
}
CANCELLATION_EUR = {"narrow": 25_720 - 12_400, "wide": 85_570 - 40_500}
# A flight holding in the air for its arrival slot (a storm's reduced rate) burns fuel, priced at
# the cruise rate above: conservative, as holding burns less than cruise.
HOLDING_BASIS = "fuel at cruise burn (conservative)"
# Why a late flight makes up only a few minutes in the air (owner: "14minutes faster only,
# that's weird"), delay recovery research: 3.5% of airborne time realistic, up to 6% to land
# under 3 h late; A320 family MMO 0.82 against M0.78 cruise.
SPEED_USUAL = "3.5% of each flight's time in the air, where the minutes are worth their fuel"
SPEED_STRETCH = "the top of the published 4–6% range"
SPEED_CEILING = (
    "An A320 cruises at Mach 0.78 and may not exceed 0.82: about 5% faster, in cruise only."
)
# Up to this many flights a speed-up line names each one; beyond, as the plan cards do for a
# plan moving many flights, it gives their count and range of minutes.
NAMED_FLIGHTS = 6
# EU261 recitals 14 and 15 and Art 5(3), C-74/19 for the same aircraft's later flights: a storm,
# and the capacity cut it causes, is not the airline's doing, so its plans owe no compensation;
# care and rebooking are still owed (C-12/11 McDonagh). TAF weather case research §3.
WEATHER_NOTE = (
    "Weather is an extraordinary circumstance: EU261 compensation is not owed. Care and"
    " rebooking are."
)
NOT_OWED_REASON = "Extraordinary circumstances: weather. Care and rebooking are still owed."
COMPENSATION_FROM_MINUTES = 180
REROUTED_EXEMPT_UNDER_MINUTES = 120
# EU261 distance bands: compensation, and the care threshold and halving limit in minutes.
BANDS = ((250, 120), (400, 180), (600, 240))
STRANDED_MEALS_EUR = 18  # Steer: delays over 5 h and cancellations; meals from the care threshold
# are €7, €10, €13 and €18 for waits from 120, 180 and 240 minutes and over 300 (see _meals)
OVERNIGHT_MEALS_EUR = 50
# Per passenger, by EU261 band (compensation 250, 400, 600): rebooked on our own flights, or
# rerouted on another airline when we cannot place them (cancellation and rebooking cost
# research §5, rules 2 and 3).
REBOOKED_EUR = {250: 11, 400: 13, 600: 45}
REROUTED_EUR = {250: 150, 400: 168, 600: 595}
BAND_WORDS = {250: "up to 1,500 km", 400: "1,500–3,500 km or within the EU", 600: "over 3,500 km"}
ROOM_EUR = {"low": 118, "base": 150, "high": 182}
TAXI_EUR = {"low": 16, "base": 19, "high": 23}
HOTEL_FROM = time(5, 0)
JOURNEY_KEYS = (
    "party_size",
    "first_flight",
    "cancelled",
    "rebooked",
    "departs",
    "departs_new",
    "next_departure",
    "arrives",
    "arrives_new",
    "flight_km",
    "flight_eu",
    "journey_km",
    "journey_eu",
)


def _minutes(later: Any, earlier: Any) -> int:
    span = datetime.fromisoformat(str(later)) - datetime.fromisoformat(str(earlier))
    return round(span.total_seconds() / 60)


def _band(km: Any, eu: Any) -> tuple[int, int]:
    """EU261 band (Arts 6 and 7): up to 1,500 km; up to 3,500 km or intra-EU; longer."""
    if not isinstance(km, int | float) or isinstance(km, bool) or not isinstance(eu, bool):
        raise ValueError("distance unknown")
    return BANDS[0] if km <= 1500 else BANDS[1] if km <= 3500 or eu else BANDS[2]


def _body(flight_no: str, seats: Mapping[str, Any]) -> str:
    count = seats.get(flight_no)
    if not isinstance(count, int) or isinstance(count, bool):
        raise ValueError(f"{flight_no} has no seat count")
    return "wide" if count >= WIDE_BODY_FROM_SEATS else "narrow"


def _airline(
    flights: list[Mapping[str, Any]],
    originals: Mapping[str, Mapping[str, Any]],
    seats: Mapping[str, Any],
) -> tuple[float, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    """The airline's own cost, with one line per flight, one per flight making up time in the
    air (its fuel and CO2, the minutes it could make up and why) and one per flight holding in
    the air for its arrival slot, for the plan's workings; and the flights that stretch past the
    usual share of their time in the air to land under 3 h late."""
    total = 0.0
    lines: list[dict[str, Any]] = []
    fuel: list[dict[str, Any]] = []
    holding: list[dict[str, Any]] = []
    stretched: set[str] = set()
    for flight in flights:
        flight_no = str(flight.get("flight_no"))
        original = originals.get(flight_no)
        if original is None:
            raise ValueError(f"{flight_no} has no original schedule")
        if flight.get("status") == "cancelled":
            body = _body(flight_no, seats)
            total += CANCELLATION_EUR[body]
            lines.append(
                {
                    "flight_no": flight_no,
                    "cancelled": True,
                    "aircraft": body,
                    "eur": CANCELLATION_EUR[body],
                }
            )
            continue
        late = max(0, _minutes(flight["std_local"], original["std_local"]))
        total += late * AIRLINE_EUR_PER_MINUTE
        if late:
            lines.append(
                {
                    "flight_no": flight_no,
                    "delay_minutes": late,
                    "eur": round(late * AIRLINE_EUR_PER_MINUTE, 2),
                }
            )
        held = flight.get("held_airborne_minutes")
        if isinstance(held, int) and not isinstance(held, bool) and held > 0:
            body = _body(flight_no, seats)
            fuel_eur = held * FUEL_EUR_PER_MINUTE[body]
            co2_eur = held * CO2_EUR_PER_MINUTE[body] if flight.get("ets") is True else 0.0
            total += fuel_eur + co2_eur
            holding.append(
                {
                    "flight_no": flight_no,
                    "held_minutes": held,
                    "aircraft": body,
                    "fuel_eur": round(fuel_eur, 2),
                    "co2_eur": round(co2_eur, 2),
                    "eur": round(fuel_eur + co2_eur, 2),
                    "basis": HOLDING_BASIS,
                }
            )
        made_up = late - _minutes(flight["sta_local"], original["sta_local"])
        if made_up > 0:
            body = _body(flight_no, seats)
            fuel_eur = made_up * FUEL_EUR_PER_MINUTE[body]
            # Operations control says whether the flight pays for its CO2; plans built before it
            # said so price the fuel alone.
            co2_eur = made_up * CO2_EUR_PER_MINUTE[body] if flight.get("ets") is True else 0.0
            total += fuel_eur + co2_eur
            line: dict[str, Any] = {"flight_no": flight_no, "made_up_minutes": made_up}
            # Of how many minutes it could, and why, as operations control chose them.
            possible, why = flight.get("made_up_possible"), flight.get("made_up_reason")
            if isinstance(possible, int) and not isinstance(possible, bool):
                line["made_up_possible"] = possible
            if isinstance(why, str) and why:
                line["reason"] = why
            # Past the usual share (defects.made_up): the stretch to land under 3 h late.
            leaves = original.get("published_std_local") or original["std_local"]
            lands = original.get("published_sta_local") or original["sta_local"]
            airborne = _minutes(lands, leaves) * AIRBORNE_SHARE
            if made_up > min(int(airborne * MADE_UP_SHARE), MADE_UP_CAP_MINUTES):
                stretched.add(flight_no)
            fuel.append(
                {
                    **line,
                    "aircraft": body,
                    "fuel_eur": round(fuel_eur, 2),
                    "co2_eur": round(co2_eur, 2),
                    "eur": round(fuel_eur + co2_eur, 2),
                }
            )
    return total, lines, fuel, holding, stretched


def _euros(amount: float) -> str:
    """Whole euros as the plan cards show them, half up: "€1,196"."""
    return f"€{math.floor(amount + 0.5):,}"


def _speed_words(fuel: list[dict[str, Any]], stretched: set[str]) -> tuple[str, str] | None:
    """The plan's speed-up in one line, each flight's minutes named, and why no more: "Fly
    faster: AR689 3 min, AR690 3 min (6 min) · fuel €512"; beyond NAMED_FLIGHTS, the flights
    and their range of minutes. None when no flight speeds up."""
    if not fuel:
        return None
    each_minutes = [int(line["made_up_minutes"]) for line in fuel]
    minutes = sum(each_minutes)
    if len(fuel) > NAMED_FLIGHTS:
        low, high = min(each_minutes), max(each_minutes)
        each = f"{len(fuel)} flights, {low}{f'–{high}' if high > low else ''} min each"
    else:
        each = ", ".join(f"{line['flight_no']} {line['made_up_minutes']} min" for line in fuel)
    paid = sum(float(line["eur"]) for line in fuel)
    co2 = sum(float(line["co2_eur"]) for line in fuel)
    summary = f"Fly faster: {each}{f' ({minutes} min)' if len(fuel) > 1 else ''}"
    summary += f" · fuel {_euros(paid - co2)}"
    if co2 > 0:
        summary += f" + CO2 {_euros(co2)} = {_euros(paid)}"
    further = [str(line["flight_no"]) for line in fuel if line["flight_no"] in stretched]
    if not further:
        share = f"{SPEED_USUAL}."
    elif len(further) == len(fuel):
        share = (
            f"Up to 6% of each flight's time in the air, {SPEED_STRETCH}, only to land under 3 h"
            " late (EU261)."
        )
    else:
        names = ", ".join(further) if len(further) <= NAMED_FLIGHTS else f"{len(further)} flights"
        share = (
            f"{SPEED_USUAL}; up to 6% for {names}, {SPEED_STRETCH}, to land under 3 h late (EU261)."
        )
    return summary, f"{share} {SPEED_CEILING}"


def _compensation(journey: Mapping[str, Any]) -> int:
    return _compensation_why(journey)[0]


def _compensation_why(journey: Mapping[str, Any]) -> tuple[int, str]:
    """The EU261 compensation per passenger and why, in OCC's words."""
    amount, limit = _band(journey["journey_km"], journey["journey_eu"])
    band = BAND_WORDS[amount]
    if journey["arrives_new"] is None:
        return amount, f"no replacement flight · {band}"
    late = _minutes(journey["arrives_new"], journey["arrives"])
    if journey["cancelled"] and late < REROUTED_EXEMPT_UNDER_MINUTES:
        return 0, ""
    if not journey["cancelled"] and late < COMPENSATION_FROM_MINUTES:
        return 0, ""
    what = "cancelled, rebooked" if journey["cancelled"] else "arrives 3 h or more late"
    if late <= limit:
        return amount // 2, f"{what} · {band} · halved: arrives within {limit // 60} h"
    return amount, f"{what} · {band}"


def _rebooking_why(journey: Mapping[str, Any]) -> tuple[int, str]:
    """What getting one passenger to their destination costs beyond the fare, and why: rebooked
    on our own flights, or rerouted on another airline when no seat of ours was found."""
    amount, _ = _band(journey["journey_km"], journey["journey_eu"])
    if journey["arrives_new"] is None:
        return REROUTED_EUR[amount], f"rerouted on another airline · {BAND_WORDS[amount]}"
    if journey["rebooked"] is True:
        return REBOOKED_EUR[amount], f"rebooked on our own flights · {BAND_WORDS[amount]}"
    return 0, ""


MEAL_WHY = {
    7: "waits past the care threshold, under 3 h",
    10: "waits 3–4 h",
    13: "waits 4–5 h",
    18: "waits over 5 h, or cancelled or without an onward flight",
}


def _meals(journey: Mapping[str, Any]) -> int:
    """Meals per passenger while waiting: from the care threshold (Art 6), by waiting time."""
    leaves = journey["departs_new"] or journey["next_departure"]
    if journey["cancelled"] or leaves is None:
        return STRANDED_MEALS_EUR
    wait = _minutes(leaves, journey["departs"])
    _, threshold = _band(journey["flight_km"], journey["flight_eu"])
    if wait < threshold:
        return 0
    return 18 if wait > 300 else 13 if wait >= 240 else 10 if wait >= 180 else 7


def _overnight(journey: Mapping[str, Any]) -> bool:
    if journey["departs_new"] is None:
        return journey["next_departure"] is None
    before = datetime.fromisoformat(str(journey["departs"]))
    after = datetime.fromisoformat(str(journey["departs_new"]))
    return after.date() > before.date() and after.time() >= HOTEL_FROM


def estimate(
    flights: list[Mapping[str, Any]],
    originals: Mapping[str, Mapping[str, Any]],
    journeys: list[Mapping[str, Any]] | None,
    seats: Mapping[str, Any] | None,
    *,
    weather: bool = False,
) -> dict[str, Any]:
    """Raises ValueError when an original, the journeys, a seat count or a journey fact is
    missing: the cost is then unknown, never low. ``weather``: a storm's plan, which owes no
    EU261 compensation (extraordinary circumstances; care and rebooking are still owed): its
    compensation and owed passengers are 0, and its workings say what would otherwise have been
    owed (``compensation_not_owed``), never counted in the total."""
    if journeys is None or seats is None:
        raise ValueError("Booking returned no journey facts")
    exact, airline_lines, fuel_lines, holding_lines, stretched = _airline(flights, originals, seats)
    airline = round(exact)
    compensation = meals = rebooking = 0
    paid: dict[tuple[int, str], int] = {}
    moved: dict[tuple[int, str], int] = {}
    fed: dict[int, int] = {}
    # Rooms and passengers staying overnight: at a connection, and starting their journey here
    # (half of those are assumed to go home, Steer Davies Gleave 2012).
    rooms = {"connection": 0, "start": 0}
    sleepers = {"connection": 0, "start": 0}
    for journey in journeys:
        missing = [key for key in JOURNEY_KEYS if key not in journey]
        if missing:
            raise ValueError(f"journey facts missing: {', '.join(missing)}")
        party = journey["party_size"]
        if not isinstance(party, int) or isinstance(party, bool) or party < 1:
            raise ValueError("party size unknown")
        overnight = _overnight(journey)
        each, why = _compensation_why(journey)
        compensation += party * each
        if each:
            paid[(each, why)] = paid.get((each, why), 0) + party
        each, why = _rebooking_why(journey)
        rebooking += party * each
        if each:
            moved[(each, why)] = moved.get((each, why), 0) + party
        where = "start" if journey["first_flight"] else "connection"
        if overnight:
            rooms[where] += math.ceil(party / 2)
            sleepers[where] += party
        meal = _meals(journey)
        meals += party * meal
        if meal:
            fed[meal] = fed.get(meal, 0) + party
    hotel_rooms = rooms["connection"] + math.ceil(rooms["start"] / 2)
    overnight_passengers = sleepers["connection"] + math.ceil(sleepers["start"] / 2)
    meals += overnight_passengers * OVERNIGHT_MEALS_EUR

    def care(level: str) -> dict[str, int]:
        return {
            "meals_eur": meals,
            "hotel_eur": hotel_rooms * ROOM_EUR[level],
            "taxi_eur": hotel_rooms * 2 * TAXI_EUR[level],
        }

    base, low, high = care("base"), care("low"), care("high")
    care_eur = sum(base.values())
    owed = sum(paid.values())
    # A storm's plan owes no compensation (extraordinary circumstances): what would otherwise
    # have been owed is said beside the cost, never counted in it.
    not_owed = (
        {"passengers": owed, "eur": compensation, "reason": NOT_OWED_REASON}
        if weather and owed
        else None
    )
    if weather:
        compensation, owed, paid = 0, 0, {}
    total = airline + compensation + care_eur + rebooking
    speed = _speed_words(fuel_lines, stretched)
    return {
        "airline_eur": airline,
        "compensation_eur": compensation,
        "care_eur": care_eur,
        "rebooking_eur": rebooking,
        "care": base,
        "overnight_passengers": overnight_passengers,
        # Who the plan affects beside its cost (cancellation and rebooking cost research §5): owed
        # compensation, fed.
        "owed_compensation_passengers": owed,
        "meals_passengers": sum(fed.values()),
        "total_eur": total,
        "low_eur": airline + compensation + sum(low.values()) + rebooking,
        "high_eur": airline + compensation + sum(high.values()) + rebooking,
        "basis": BASIS,
        # How each part was reached, for OCC (owner: "how do we come up with 6,894 eur").
        "workings": {
            "airline": {
                "rate_eur_per_minute": AIRLINE_EUR_PER_MINUTE,
                "flights": airline_lines,
                "fuel_eur_per_minute": FUEL_EUR_PER_MINUTE,
                "co2_eur_per_minute": CO2_EUR_PER_MINUTE,
                "fuel": fuel_lines,
                # The speed-up in one line, each flight's minutes named, and why no more
                # (owner); None when no flight speeds up.
                "fuel_summary": speed[0] if speed else None,
                "fuel_basis": speed[1] if speed else None,
                "holding": holding_lines,
            },
            "compensation": [
                {"eur_each": each, "passengers": count, "why": why}
                for (each, why), count in sorted(paid.items(), key=lambda item: -item[0][0])
            ],
            **({"compensation_not_owed": not_owed} if not_owed else {}),
            "meals": [
                {"eur_each": each, "passengers": count, "why": MEAL_WHY.get(each, "")}
                for each, count in sorted(fed.items())
            ],
            "overnight": {
                "passengers": overnight_passengers,
                "rooms": hotel_rooms,
                "room_eur": ROOM_EUR["base"],
                "taxis": hotel_rooms * 2,
                "taxi_eur": TAXI_EUR["base"],
                "meals_eur_each": OVERNIGHT_MEALS_EUR,
            }
            if overnight_passengers
            else None,
            "rebooking": [
                {"eur_each": each, "passengers": count, "why": why}
                for (each, why), count in sorted(moved.items(), key=lambda item: -item[0][0])
            ],
            **({"note": WEATHER_NOTE} if weather else {}),
        },
    }
