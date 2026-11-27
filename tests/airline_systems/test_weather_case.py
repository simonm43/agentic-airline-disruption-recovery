"""Case 5: thunderstorms at Lisbon read from a TAF. Schedule plans the storm window it is asked
about (no airline system records a forecast) and offers plans that keep Lisbon within the reduced
hourly rate; Crew, Booking and Slots assess them."""

from __future__ import annotations

import base64
import json
import unittest
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from day_store import OperatingDayStore
from fastapi.testclient import TestClient
from system_callers import ARROW, READS, role_claims

from airline_systems.app import create_app
from airline_systems.defects import turnaround
from airline_systems.operating_day import AIRPORTS, distance_km, weather_rate
from airline_systems.weather import AIRBORNE_HOLD_LIMIT

TRACE = "7a1b2c3d4e5f60718293a4b5c6d7e8f9"
OPERATOR = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
AGENT = "11111111-2222-3333-4444-555555555555"
HOLD_LIMIT_MINUTES = AIRBORNE_HOLD_LIMIT.total_seconds() // 60


def principal(oid: str) -> dict[str, str]:
    claims = {
        "claims": [{"typ": "oid", "val": oid}, *role_claims(ARROW if oid == OPERATOR else READS)]
    }
    return {
        "X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(claims).encode()).decode(),
        "traceparent": f"00-{TRACE}-00f067aa0ba902b7-01",
    }


def system(store: OperatingDayStore, name: str = "schedule") -> TestClient:
    return TestClient(
        create_app(
            store, frozenset({OPERATOR, AGENT}), system=name, writer_oids=frozenset({OPERATOR})
        )
    )


# The research TAF (TAF weather case research): thunderstorms from 16:00 to
# 18:00 UTC, which is Lisbon time in November.
def storm(**change: str) -> dict[str, Any]:
    """The storm as ARROW and the agents name it to the systems: its facts."""
    return {
        "type": "weather",
        "airport": "LIS",
        "hazard": "thunderstorm",
        "window_start": "16:00",
        "window_end": "18:00",
        "hourly_rate": weather_rate("LIS", "thunderstorm"),
        **change,
    }


def storm_options(store: OperatingDayStore, **change: str) -> dict[str, Any]:
    with system(store) as client:
        response = client.get(
            "/api/recovery-options", params=storm(**change), headers=principal(AGENT)
        )
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


def planned(store: OperatingDayStore, option: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The day as the option would fly it."""
    return {
        **store.flights,
        **{f["flight_no"]: {**store.flights[f["flight_no"]], **f} for f in option["flights"]},
    }


def lisbon_hours(flights: dict[str, dict[str, Any]]) -> Counter[datetime]:
    """Lisbon movements, departures and arrivals, per clock hour."""
    hours: Counter[datetime] = Counter()
    for flight in flights.values():
        if flight["status"] == "cancelled":
            continue
        for end, key in (("origin", "std_local"), ("destination", "sta_local")):
            if flight[end] == "LIS":
                hours[datetime.fromisoformat(flight[key]).replace(minute=0)] += 1
    return hours


def moved(store: OperatingDayStore, flight: dict[str, Any]) -> bool:
    before = store.flights[flight["flight_no"]]
    return any(flight[key] != before[key] for key in ("status", "std_local", "sta_local"))


def late(store: OperatingDayStore, option: dict[str, Any]) -> set[str]:
    return {
        f["flight_no"] for f in option["flights"] if f["status"] != "cancelled" and moved(store, f)
    }


def check_a_storm_holds_lisbon_to_the_reduced_rate() -> None:
    store = OperatingDayStore()
    with system(store) as client:
        response = client.get("/api/recovery-options", params=storm(), headers=principal(AGENT))
        read = client.post(
            "/mcp",
            headers={**principal(AGENT), "Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "get_disruption", "arguments": {"disruption": storm()}},
            },
        )

    day = store.day.isoformat()
    window = {
        "airport": "LIS",
        "window_start_local": f"{day}T16:00:00",
        "window_end_local": f"{day}T18:00:00",
        "hazard": "thunderstorm",
        "hourly_rate": 24,
    }
    # The agent reads the weather by its facts; it names no aircraft and no flight, and says
    # which flights the reduced rate would make late if nothing changes.
    assert read.status_code == 200, read.text
    disruption = read.json()["result"]["structuredContent"]
    assert {key: disruption[key] for key in ("cause", "aircraft_reg", "flight_no", *window)} == {
        "cause": "weather",
        "aircraft_reg": None,
        "flight_no": None,
        **window,
    }, disruption
    assert "event_id" not in disruption and "trace_id" not in disruption, disruption
    knock_on = disruption["knock_on"]

    assert response.status_code == 200, response.text
    found = response.json()
    assert (found["cause"], found["aircraft"]) == ("weather", None), found
    assert found["weather"] == {
        **window,
        "normal_rate": 40,
        "basis": "set to match Lisbon's thunderstorm evenings in EUROCONTROL delay data"
        " 2015–2025 (about 25 min per delayed arrival)",
    }, found["weather"]
    options = {option["option_id"]: option for option in found["options"]}
    assert sorted(options) == ["cancel", "delay"], list(options)

    delay = options["delay"]
    flights = planned(store, delay)
    # If nothing changes, the reduced rate makes the delay option's flights late: the knock-on
    # counts every one of them and names the first by departure.
    lateness = {
        f["flight_no"]: max(
            int(
                (
                    datetime.fromisoformat(f[key])
                    - datetime.fromisoformat(store.flights[f["flight_no"]][key])
                ).total_seconds()
                // 60
            )
            for key in ("std_local", "sta_local")
        )
        for f in delay["flights"]
    }
    assert knock_on["flights_affected"] == len(late(store, delay)) == len(lateness), knock_on
    assert knock_on["max_minutes_late"] == max(lateness.values()), knock_on
    assert knock_on["aircraft"] == sorted({f["aircraft_reg"] for f in delay["flights"]})
    named = [f["flight_no"] for f in knock_on["flights"]]
    scheduled = sorted(
        delay["flights"], key=lambda f: (store.flights[f["flight_no"]]["std_local"], f["flight_no"])
    )
    assert named == [f["flight_no"] for f in scheduled][: len(named)], named
    assert 0 < len(named) <= 25, named
    assert all(f["minutes_late"] == lateness[f["flight_no"]] for f in knock_on["flights"])
    before, after = lisbon_hours(store.flights), lisbon_hours(flights)
    storm_hours = [datetime.fromisoformat(f"{day}T{hour}:00") for hour in ("16", "17")]
    cleared = datetime.fromisoformat(f"{day}T18:00")
    assert all(before[hour] > 24 for hour in storm_hours), before
    assert all(after[hour] <= 24 for hour in storm_hours), after
    assert all(count <= 40 for hour, count in after.items() if hour >= cleared), after
    assert sum(after.values()) == sum(before.values())
    # A Lisbon arrival that lands later also leaves its origin later: none is airborne at 09:45.
    for flight in delay["flights"]:
        if flight["destination"] == "LIS" and moved(store, flight):
            assert flight["std_local"] > store.flights[flight["flight_no"]]["std_local"], flight
    # Each flight says how long it holds in the air (never beyond the limit) and why, in words.
    for flight in delay["flights"]:
        assert 0 <= flight["held_airborne_minutes"] <= HOLD_LIMIT_MINUTES, flight
        reason = flight.get("made_up_reason") or ""
        assert flight["origin"] not in reason and flight["destination"] not in reason, flight
    # Every aircraft with a later flight still turns round before its next one.
    for reg in {flight["aircraft_reg"] for flight in delay["flights"]}:
        rotation = sorted(
            (f for f in flights.values() if f["aircraft_reg"] == reg),
            key=lambda f: f["std_local"],
        )
        turn = turnaround(store.aircraft[reg]["fleet_type"])
        for leg, following in zip(rotation, rotation[1:], strict=False):
            assert following["origin"] == leg["destination"], (leg, following)
            assert (
                datetime.fromisoformat(following["std_local"])
                >= datetime.fromisoformat(leg["sta_local"]) + turn
            ), (leg, following)
    # The case's flights are every flight an option moves or cancels, and only those.
    listed = {f["flight_no"] for option in options.values() for f in option["flights"]}
    assert all(moved(store, f) for option in options.values() for f in option["flights"])
    assert set(found["affected_flight_ids"]) == listed, found["affected_flight_ids"]


def check_cancelling_round_trips_in_the_storm_keeps_more_flights_on_time() -> None:
    store = OperatingDayStore()
    found = storm_options(store)
    options = {option["option_id"]: option for option in found["options"]}
    delay, cancel = options["delay"], options["cancel"]

    cancelled = sorted(
        (f for f in cancel["flights"] if f["status"] == "cancelled"),
        key=lambda f: (f["aircraft_reg"], f["std_local"]),
    )
    assert len(cancelled) in (2, 4), cancelled
    for out, back in zip(cancelled[::2], cancelled[1::2], strict=True):
        rotation = sorted(
            (f for f in store.flights.values() if f["aircraft_reg"] == out["aircraft_reg"]),
            key=lambda f: f["std_local"],
        )
        following = rotation[[f["flight_no"] for f in rotation].index(out["flight_no"]) + 1]
        assert following["flight_no"] == back["flight_no"], (out, back)
        assert (out["origin"], out["destination"]) == (back["destination"], back["origin"])
        assert out["origin"] == "LIS" and AIRPORTS[out["destination"]][2] == "PT", out
        # A domestic round trip takes over two hours, so in this storm one of its two Lisbon
        # movements falls in the window.
        assert any("16:00" <= at[11:16] < "18:00" for at in (out["std_local"], back["sta_local"]))
    after = lisbon_hours(planned(store, cancel))
    day = store.day.isoformat()
    assert all(after[datetime.fromisoformat(f"{day}T{h}:00")] <= 24 for h in ("16", "17")), after

    late_delay, late_cancel = late(store, delay), late(store, cancel)
    assert len(late_cancel) < len(late_delay), (len(late_cancel), len(late_delay))
    # Net, among the flights both plans fly: on time in the cancel plan less on time in the delay.
    net = len(late_delay - {f["flight_no"] for f in cancelled}) - len(late_cancel)
    [why] = [item for item in found["checked"] if item["kind"] == "cancel"]
    assert why["available"] is True, why
    assert all(f["flight_no"] in why["why"] for f in cancelled), why
    assert net > 0 and f"keeps {net} more flights on time" in why["why"], (net, why)
    assert "round trip" in cancel["title"], cancel["title"]


def check_an_early_storm_moves_and_cancels_only_flights_yet_to_leave() -> None:
    # Review: in a 10:00-12:00 storm some round trips touching it left Lisbon before the
    # 09:45 clock and some Lisbon arrivals are already in the air. Those keep their times and
    # count first in their hour, so no hold in the air exceeds the limit.
    store = OperatingDayStore()
    found = storm_options(store, window_start="10:00", window_end="12:00")
    clock = f"{store.day.isoformat()}T09:45:00"
    flights = [f for option in found["options"] for f in option["flights"]]

    assert flights, found["checked"]
    left = [f["flight_no"] for f in flights if store.flights[f["flight_no"]]["std_local"] <= clock]
    assert left == [], left
    assert all(0 <= f["held_airborne_minutes"] <= HOLD_LIMIT_MINUTES for f in flights), flights


def check_a_storm_the_playbook_cannot_plan_is_refused() -> None:
    store = OperatingDayStore()
    with system(store) as client:
        refused = [
            client.get("/api/recovery-options", params=storm(**change), headers=principal(OPERATOR))
            for change in (
                {"airport": "OPO"},
                {"window_start": "09:00", "window_end": "11:00"},
                {"window_start": "12:00", "window_end": "18:30"},
                {"hazard": "volcanic_ash"},
            )
        ]

    assert [r.status_code for r in refused] == [409, 409, 409, 409], [r.text for r in refused]
    words = [r.json()["detail"] for r in refused]
    assert "Lisbon" in words[0] and "09:45" in words[1] and "6 h" in words[2], words
    assert "thunderstorm" in words[3], words


def check_crew_booking_and_slots_assess_both_storm_plans() -> None:
    store = OperatingDayStore()
    found: dict[tuple[str, str], dict[str, Any]] = {}
    unresolved: dict[str, list[int]] = {}
    for name in ("crew", "booking", "slots"):
        with system(store, name) as client:
            for option_id in ("delay", "cancel"):
                response = client.get(
                    f"/api/recovery-options/{option_id}/impact",
                    params=storm(),
                    headers=principal(AGENT),
                )
                assert response.status_code == 200, (name, option_id, response.text)
                found[(option_id, name)] = response.json()
                if name != "crew" or response.json()["constraint_status"] == "pass":
                    continue
                # As ARROW does: a duty breach is covered when a reserve crew leaves none.
                reserves = client.get(
                    f"/api/recovery-options/{option_id}/standby-crews",
                    params=storm(),
                    headers=principal(AGENT),
                )
                assert reserves.status_code == 200, reserves.text
                for reserve in reserves.json()["options"]:
                    plan = client.get(
                        f"/api/recovery-options/{option_id}/duty-plan",
                        params={**storm(), "reserve_option_id": reserve["reserve_option_id"]},
                        headers=principal(AGENT),
                    )
                    assert plan.status_code == 200, plan.text
                    unresolved.setdefault(option_id, []).append(plan.json()["breaches_unresolved"])

    statuses = {key: value["constraint_status"] for key, value in found.items()}
    legal = [
        option_id
        for option_id in ("delay", "cancel")
        if statuses[(option_id, "slots")] == "pass"
        and (statuses[(option_id, "crew")] == "pass" or 0 in unresolved.get(option_id, []))
    ]
    assert legal, (statuses, unresolved)
    # Slots checks the storm's hours at the reduced rate, not at the normal 40.
    day = store.day.isoformat()
    for option_id in ("delay", "cancel"):
        assert found[(option_id, "slots")]["reduced_capacity"] == [
            {"airport": "LIS", "hour_local": f"{day}T16:00:00", "hourly_capacity": 24},
            {"airport": "LIS", "hour_local": f"{day}T17:00:00", "hourly_capacity": 24},
        ], found[(option_id, "slots")]


def check_two_duties_the_storm_breaks_get_two_standby_crews_in_one_plan() -> None:
    # Holding Lisbon to 24 movements an hour pushes two crew duties past their limit. Each gets
    # its own standby crew, nobody twice; one duty plan calls both under one digest, two fewer
    # complete Lisbon A320 crews stay on standby, and the crew system's duty change applies both.
    store = OperatingDayStore()
    delay = next(o for o in storm_options(store)["options"] if o["option_id"] == "delay")
    originals = [dict(store.flights[f["flight_no"]]) for f in delay["flights"]]
    key = "7a1b2c3d4e5f-0001-standby"
    with system(store, "crew") as client:
        offered = client.get(
            "/api/recovery-options/delay/standby-crews",
            params=storm(),
            headers=principal(AGENT),
        ).json()
        plans = {
            ids: client.get(
                "/api/recovery-options/delay/duty-plan",
                params={**storm(), "reserve_option_ids": list(ids)},
                headers=principal(AGENT),
            ).json()
            for ids in ((), ("R1",), ("R2",), ("R1", "R2"))
        }
        both = plans[("R1", "R2")]
        written = client.post(
            "/api/duty-changes",
            json={
                "flights": delay["flights"],
                "originals": originals,
                "reserve_option_ids": ["R1", "R2"],
            },
            headers={
                **principal(OPERATOR),
                "Idempotency-Key": key,
                "X-Request-Id": key,
                "If-Match": both["plan_digest"],
            },
        )

    first, second = offered["options"]
    assert len(offered["breaches"]) == 2, offered["breaches"]
    assert {first["duty_id"], second["duty_id"]} == {b["duty_id"] for b in offered["breaches"]}
    crews = [{member["crew_id"] for member in option["crew"]} for option in (first, second)]
    assert not crews[0] & crews[1], crews
    assert [plans[ids]["breaches_unresolved"] for ids in ((), ("R1",), ("R2",))] == [2, 1, 1]
    assert both["breaches_unresolved"] == 0 and both["reserve_option_ids"] == ["R1", "R2"], both
    assert len({plan["plan_digest"] for plan in plans.values()}) == 4, plans

    def left(plan: dict[str, Any]) -> int:
        [lisbon] = [
            item
            for item in plan["standby_left"]
            if (item["base"], item["qualification"]) == ("LIS", "A320F")
        ]
        crews_left: int = lisbon["crews"]
        return crews_left

    assert left(both) == left(plans[()]) - 2, (both["standby_left"], plans[()]["standby_left"])
    assert written.status_code == 200, written.text
    assert written.json()["status"] == "accepted", written.json()
    for option in (first, second):
        duty = store.duties[f"{option['duty_id']}-{option['reserve_option_id']}"]
        assert sorted(duty["flight_nos"]) == sorted(option["flights_for_reserve"]), duty
        assert {m["crew_id"] for m in duty["members"]} == {m["crew_id"] for m in option["crew"]}
    assert not (crews[0] | crews[1]) & {reserve["crew_id"] for reserve in store.reserves}


def check_dispatch_flags_holding_fuel_and_names_the_alternates() -> None:
    # FUEL-FLAG and ALT-FLAG are advice: flights yet to leave that land at Lisbon in the storm's
    # exposure window are told to expect holding (the dispatcher sets the fuel), and Porto and
    # Faro are the alternates, with no hazard forecast there in this demo. Nothing changes.
    store = OperatingDayStore()
    day = store.day.isoformat()
    before = {number: dict(flight) for number, flight in store.flights.items()}
    with system(store) as client:
        advice = client.get("/api/dispatch-advisories", params=storm(), headers=principal(AGENT))
        # A disruption that is no storm: a flight delay.
        no_storm = client.get(
            "/api/dispatch-advisories",
            params={
                "type": "flight_delay",
                "flight_no": "AR131",
                "delay_minutes": 20,
                "cause": "staff_shortage",
            },
            headers=principal(AGENT),
        )

    assert advice.status_code == 200, advice.text
    body = advice.json()
    expected = {
        number
        for number, flight in store.flights.items()
        if flight["destination"] == "LIS"
        and flight["status"] == "scheduled"
        and flight["std_local"] > f"{day}T09:45:00"
        and f"{day}T14:30:00" <= flight["sta_local"] < f"{day}T21:00:00"
    }
    assert {flight["flight_no"] for flight in body["flights"]} == expected, body["flights"][:3]
    assert all(
        (flight["advice"], flight["note"])
        == ("carry holding fuel", "holding expected; dispatcher sets the fuel")
        for flight in body["flights"]
    )
    assert body["alternates"] == [
        {
            "iata": iata,
            "city": city,
            "distance_km": distance_km("LIS", iata),
            "hazard": "no forecast hazard in this demo",
        }
        for iata, city in (("OPO", "Porto"), ("FAO", "Faro"))
    ], body["alternates"]
    assert (no_storm.status_code, no_storm.json()) == (
        409,
        {"detail": "Dispatch advice is for a weather forecast"},
    ), no_storm.text
    assert store.flights == before


def check_a_storm_never_flies_faster_to_land_under_3_h() -> None:
    # Review: weather owes no EU261 compensation, so a storm's plan never stretches past
    # the usual share of a flight's time in the air to land under 3 h late. A long Lisbon
    # departure the storm holds back, already late from an earlier delay (its published time
    # earlier), would otherwise fly up to 6% faster to land just under 3 h late.
    found = storm_options(OperatingDayStore())
    delay = next(option for option in found["options"] if option["option_id"] == "delay")
    store = OperatingDayStore()

    def block(flight: dict[str, Any]) -> int:
        span = datetime.fromisoformat(flight["sta_local"]) - datetime.fromisoformat(
            flight["std_local"]
        )
        return int(span.total_seconds() // 60)

    [flight, *_] = [
        f
        for f in delay["flights"]
        if f["origin"] == "LIS"
        and f["status"] != "cancelled"
        and block(store.flights[f["flight_no"]]) >= 150
        and f["std_local"] > store.flights[f["flight_no"]]["std_local"]
    ]
    number = flight["flight_no"]
    before = store.flights[number]
    airborne = block(before) * 0.85
    usual, stretch = min(int(airborne * 0.035), 10), min(int(airborne * 0.06), 15)
    held = datetime.fromisoformat(flight["std_local"]) - datetime.fromisoformat(before["std_local"])
    # Published early enough that the storm's departure is just past the 3 h step at the usual
    # speed.
    earlier = timedelta(minutes=179 + usual + 2) - held
    for key in ("std_local", "sta_local"):
        before[f"published_{key}"] = (datetime.fromisoformat(before[key]) - earlier).isoformat()

    again = storm_options(store)
    [flown] = [
        f
        for option in again["options"]
        if option["option_id"] == "delay"
        for f in option["flights"]
        if f["flight_no"] == number
    ]
    late = datetime.fromisoformat(flown["std_local"]) - datetime.fromisoformat(
        before["published_std_local"]
    )
    step = int(late.total_seconds() // 60) - 179
    assert usual < step <= stretch, (number, step, usual, stretch)
    assert flown["made_up_minutes"] <= usual, flown
    assert flown["made_up_possible"] <= usual, flown
    assert "EU261" not in (flown.get("made_up_reason") or ""), flown


class WeatherCaseTests(unittest.TestCase):
    def test_a_storm_holds_lisbon_to_the_reduced_rate(self) -> None:
        check_a_storm_holds_lisbon_to_the_reduced_rate()

    def test_cancelling_round_trips_in_the_storm_keeps_more_flights_on_time(self) -> None:
        check_cancelling_round_trips_in_the_storm_keeps_more_flights_on_time()

    def test_an_early_storm_moves_and_cancels_only_flights_yet_to_leave(self) -> None:
        check_an_early_storm_moves_and_cancels_only_flights_yet_to_leave()

    def test_a_storm_the_playbook_cannot_plan_is_refused(self) -> None:
        check_a_storm_the_playbook_cannot_plan_is_refused()

    def test_crew_booking_and_slots_assess_both_storm_plans(self) -> None:
        check_crew_booking_and_slots_assess_both_storm_plans()

    def test_two_duties_the_storm_breaks_get_two_standby_crews_in_one_plan(self) -> None:
        check_two_duties_the_storm_breaks_get_two_standby_crews_in_one_plan()

    def test_dispatch_flags_holding_fuel_and_names_the_alternates(self) -> None:
        check_dispatch_flags_holding_fuel_and_names_the_alternates()

    def test_a_storm_never_flies_faster_to_land_under_3_h(self) -> None:
        check_a_storm_never_flies_faster_to_land_under_3_h()
