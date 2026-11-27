"""EU261 cost rule table: airline cost, compensation per booking, passenger care and rebooking.

Deterministic rules of the indicative estimate (passenger cost spec §2), one row per situation.
Each journey stands for one booking as Booking reports it.
"""

from __future__ import annotations

import unittest
from typing import Any

from ops_app.cost import estimate

DAY, NEXT = "2026-11-12", "2026-11-13"


def at(hhmm: str | None, day: str = DAY) -> str | None:
    return f"{day}T{hhmm}:00" if hhmm else None


def journey(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "party_size": 2,
        "first_flight": False,
        "cancelled": False,
        "rebooked": True,
        "departs": at("10:00"),
        "departs_new": at("13:00"),
        "next_departure": None,
        "arrives": at("12:00"),
        "arrives_new": at("15:00"),
        "flight_km": 500,
        "flight_eu": True,
        "journey_km": 500,
        "journey_eu": True,
    }
    return {**base, **over}


def stranded(**over: Any) -> dict[str, Any]:
    return journey(rebooked=False, departs_new=None, arrives_new=None, **over)


def cost(*journeys: dict[str, Any]) -> dict[str, Any]:
    return estimate([], {}, list(journeys), {})


def check_an_evening_disruption_that_strands_passengers_costs_more_than_a_morning_one() -> None:
    morning = cost(journey())
    assert morning["care"] == {"meals_eur": 20, "hotel_eur": 0, "taxi_eur": 0}, morning
    # Rebooked on our own flights: €11 each up to 1,500 km (Steer 2020, cancellation and rebooking
    # cost research §5).
    assert (morning["compensation_eur"], morning["rebooking_eur"]) == (500, 22), morning
    assert morning["total_eur"] == 542, morning
    assert morning["overnight_passengers"] == 0

    evening = cost(stranded(departs=at("21:00"), arrives=at("23:00")))
    # Meals €18 x 2 while stranded plus €50 x 2 overnight; one room €150; two taxis €19.
    assert evening["care"] == {"meals_eur": 136, "hotel_eur": 150, "taxi_eur": 38}, evening
    assert evening["compensation_eur"] == 500 and evening["care_eur"] == 324
    # Not rebooked: €150 each to get them there on another airline.
    assert evening["rebooking_eur"] == 300, evening
    assert (evening["total_eur"], evening["low_eur"], evening["high_eur"]) == (1124, 1086, 1164)
    assert evening["overnight_passengers"] == 2
    assert evening["total_eur"] > morning["total_eur"]


def check_cancellation_compensation_depends_on_the_rebooked_arrival() -> None:
    long = {"flight_km": 1800, "journey_km": 1800}
    close = cost(journey(cancelled=True, arrives_new=at("13:00"), **long))
    assert close["compensation_eur"] == 0, close  # rerouted, under 2 h late (Art 5(1)(c)(iii))
    assert close["care"]["meals_eur"] == 36  # €18 per passenger for a cancellation
    later = cost(journey(cancelled=True, arrives_new=at("14:30"), **long))
    assert later["compensation_eur"] == 400, later  # €400 halved: within 3 h (Art 7(2))


def check_half_of_the_passengers_starting_their_journey_go_home() -> None:
    result = cost(
        stranded(first_flight=True),
        stranded(first_flight=True),
        stranded(party_size=3),
    )
    # Starting here: 2 rooms and 4 passengers halved to 1 and 2; at a connection: 2 rooms, 3.
    assert result["care"]["hotel_eur"] == 450 and result["care"]["taxi_eur"] == 114, result
    assert result["overnight_passengers"] == 5


def check_only_a_wait_into_the_next_morning_needs_a_hotel() -> None:
    small_hours = cost(journey(departs=at("22:00"), departs_new=at("00:35", NEXT)))
    assert small_hours["overnight_passengers"] == 0, small_hours
    morning = cost(journey(departs=at("22:00"), departs_new=at("06:30", NEXT)))
    assert morning["overnight_passengers"] == 2 and morning["care"]["hotel_eur"] == 150, morning


def check_a_later_flight_today_means_no_hotel() -> None:
    result = cost(stranded(departs=at("13:00"), next_departure=at("19:00")))
    assert result["care"] == {"meals_eur": 36, "hotel_eur": 0, "taxi_eur": 0}, result
    assert result["compensation_eur"] == 500  # not rebooked: the full amount


def check_long_haul_compensation_is_halved_within_four_hours() -> None:
    far = {"flight_km": 4000, "flight_eu": False, "journey_km": 4000, "journey_eu": False}
    result = cost(journey(arrives_new=at("15:20"), **far))
    assert result["compensation_eur"] == 600, result  # €600 halved, x 2 (Art 7(2)(c))
    assert result["care"]["meals_eur"] == 0  # a 3-hour wait is under the 4-hour threshold
    assert result["rebooking_eur"] == 90, result  # €45 each over 3,500 km
    assert cost(stranded(**far))["rebooking_eur"] == 1190  # €595 each on another airline


def check_airline_cost_prices_delays_and_cancellations() -> None:
    def flight(no: str, std: str, status: str = "scheduled") -> dict[str, Any]:
        return {"flight_no": no, "std_local": at(std), "sta_local": at(std), "status": status}

    flights = [
        flight("AR1", "11:00"),
        flight("AR2", "12:00", "cancelled"),
        flight("AR3", "13:00", "cancelled"),
    ]
    originals = {
        "AR1": flight("AR1", "10:00"),
        "AR2": flight("AR2", "12:00"),
        "AR3": flight("AR3", "13:00"),
    }
    result = estimate(flights, originals, [], {"AR2": 174, "AR3": 298})
    assert result["airline_eur"] == 1067 + 13320 + 45070, result
    assert result["total_eur"] == result["airline_eur"]


def check_missing_facts_make_the_cost_unknown() -> None:
    incomplete = journey()
    del incomplete["journey_km"]
    for journeys in ([incomplete], None):
        try:
            estimate([], {}, journeys, {})  # type: ignore[arg-type]
        except ValueError:
            continue
        raise AssertionError(f"An estimate without facts must fail: {journeys}")


class PassengerCostRules(unittest.TestCase):
    def test_an_evening_disruption_that_strands_passengers_costs_more(self) -> None:
        check_an_evening_disruption_that_strands_passengers_costs_more_than_a_morning_one()

    def test_cancellation_compensation_depends_on_the_rebooked_arrival(self) -> None:
        check_cancellation_compensation_depends_on_the_rebooked_arrival()

    def test_half_of_the_passengers_starting_their_journey_go_home(self) -> None:
        check_half_of_the_passengers_starting_their_journey_go_home()

    def test_only_a_wait_into_the_next_morning_needs_a_hotel(self) -> None:
        check_only_a_wait_into_the_next_morning_needs_a_hotel()

    def test_a_later_flight_today_means_no_hotel(self) -> None:
        check_a_later_flight_today_means_no_hotel()

    def test_long_haul_compensation_is_halved_within_four_hours(self) -> None:
        check_long_haul_compensation_is_halved_within_four_hours()

    def test_airline_cost_prices_delays_and_cancellations(self) -> None:
        check_airline_cost_prices_delays_and_cancellations()

    def test_missing_facts_make_the_cost_unknown(self) -> None:
        check_missing_facts_make_the_cost_unknown()
