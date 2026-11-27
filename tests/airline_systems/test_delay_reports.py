"""A flight delay reported by OCC becomes one grounded delay option in Schedule."""

from __future__ import annotations

import base64
import json
import unittest
from typing import Any

from arrow_scenario import ScenarioStore
from fastapi.testclient import TestClient
from system_callers import ARROW, READS, role_claims

from airline_systems.app import create_app

TRACE = "0123456789abcdef0123456789abcdef"
OPERATOR = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
AGENT = "11111111-2222-3333-4444-555555555555"


def principal(oid: str) -> dict[str, str]:
    claims = {
        "claims": [{"typ": "oid", "val": oid}, *role_claims(ARROW if oid == OPERATOR else READS)]
    }
    return {
        "X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(claims).encode()).decode(),
        "traceparent": f"00-{TRACE}-00f067aa0ba902b7-01",
    }


def client_for(store: ScenarioStore) -> TestClient:
    return TestClient(
        create_app(
            store,
            frozenset({OPERATOR, AGENT}),
            system="schedule",
            writer_oids=frozenset({OPERATOR}),
        )
    )


def delay(flight_no: str = "AR131", minutes: int = 20) -> dict[str, Any]:
    """The delay as ARROW's executor records it in operations control."""
    return {
        "trace_id": TRACE,
        "flight_no": flight_no,
        "delay_minutes": minutes,
        "cause": "staff_shortage",
        "description": "Ground staff shortage at LIS",
        "user_oid": OPERATOR,
    }


def delay_facts(flight_no: str = "AR131", minutes: int = 20) -> dict[str, Any]:
    """The delay as ARROW and the agents name it to Schedule: its facts."""
    return {
        "type": "flight_delay",
        "flight_no": flight_no,
        "delay_minutes": minutes,
        "cause": "staff_shortage",
    }


def check_delay_report_offers_one_propagated_delay_option() -> None:
    # AR131 LIS-FAO 14:10-14:55 +20 min -> 14:30-15:15; AR132 FAO-LIS 15:35 propagates to 15:50
    store = ScenarioStore(TRACE)
    with client_for(store) as client:
        response = client.get(
            "/api/recovery-options", params=delay_facts(), headers=principal(AGENT)
        )
        # ARROW's executor records the delay once a plan for it runs; Schedule issues its event
        # id then.
        recorded = client.post("/api/delays", json=delay(), headers=principal(OPERATOR))

    assert recorded.status_code == 201, recorded.text
    assert (recorded.json()["event_id"], "trace_id" in recorded.json()) == ("EV-6789AB", False)
    assert response.status_code == 200, response.text
    options = response.json()
    assert options["cause"] == "staff_shortage"
    assert [o["kind"] for o in options["options"]] == ["delay"]
    flights = {f["flight_no"]: f for f in options["options"][0]["flights"]}
    assert flights["AR131"]["std_local"].endswith("T14:30:00"), flights["AR131"]
    assert flights["AR132"]["std_local"].endswith("T15:50:00"), flights["AR132"]
    assert flights["AR131"]["distance_km"] > 0 and flights["AR131"]["intra_eu"] is True


def check_a_short_delay_is_made_up_only_when_the_minutes_repay_the_fuel() -> None:
    # Owner: speed up only when the minutes are worth it (cost index and time value research §5).
    # Within the EEA a minute bought back costs about €89 (fuel and CO2, less the crew time saved);
    # at 15-30 min late it is worth €0.53 to each passenger (85% of seats). AR151 LIS-MAD in 80 min
    # (68 in the air, so 2 min at most) 20 min late: an A320neo's passengers do not repay the fuel;
    # an A321neo's do.
    outcomes = {}
    for fleet_type, seats in (("A320neo", 174), ("A321neo", 216)):
        store = ScenarioStore(TRACE)
        for key in ("sta_local", "published_sta_local"):
            store.flights["AR151"][key] = store.flights["AR151"][key].replace("16:15", "15:20")
        store.aircraft["CS-TQC"].update(fleet_type=fleet_type, seats=seats)
        with client_for(store) as client:
            response = client.get(
                "/api/recovery-options", params=delay_facts("AR151"), headers=principal(AGENT)
            )
        assert response.status_code == 200, response.text
        [delayed] = [o for o in response.json()["options"] if o["option_id"] == "delay"]
        flight = next(f for f in delayed["flights"] if f["flight_no"] == "AR151")
        outcomes[fleet_type] = tuple(
            flight[key][11:16] if key.endswith("_local") else flight[key]
            for key in (
                "std_local",
                "sta_local",
                "made_up_minutes",
                "made_up_possible",
                "made_up_reason",
            )
        )

    assert outcomes == {
        "A320neo": ("14:20", "15:40", 0, 2, "not worth its fuel at 15–30 min late"),
        "A321neo": ("14:20", "15:38", 2, 2, "worth more than its fuel at 15–30 min late"),
    }, outcomes


def check_more_delay_is_flown_again_to_land_under_3_h() -> None:
    # Owner (cost index and time value research §5): AR151 LIS-MAD (115 min in the air) 180 min late
    # makes up its usual 4 min and lands under 3 h late. When OCC or another system adds 5 min, it
    # is 185 min late and stretches to 6 min, still landing under 3 h, so EU261 owes nothing.
    store = ScenarioStore(TRACE)
    with client_for(store) as client:
        responses = [
            client.get(
                "/api/recovery-options",
                params={**delay_facts("AR151", 180), "additional_delay_minutes": extra},
                headers=principal(AGENT),
            )
            for extra in (0, 5)
        ]

    flown = []
    for response in responses:
        assert response.status_code == 200, response.text
        [delayed] = [o for o in response.json()["options"] if o["option_id"] == "delay"]
        flight = next(f for f in delayed["flights"] if f["flight_no"] == "AR151")
        flown.append(
            (
                flight["std_local"][11:16],
                flight["sta_local"][11:16],
                flight["made_up_minutes"],
                flight["made_up_possible"],
                flight["made_up_reason"],
            )
        )
    assert flown == [
        ("17:00", "19:11", 4, 4, "arrives under 3 h late: EU261 avoided"),
        ("17:05", "19:14", 6, 6, "arrives under 3 h late: EU261 avoided"),
    ], flown


def check_a_flight_from_the_uk_pays_for_its_co2() -> None:
    # Ruling (cost index and time value research §5, an assumption of low confidence): flights from
    # the UK or Switzerland pay for their CO2 under those countries' own schemes, priced at the EU
    # allowance price, so the option tells ARROW to count it.
    store = ScenarioStore(TRACE)
    store.flights["AR161"].update(origin="LHR", destination="LIS")
    with client_for(store) as client:
        response = client.get(
            "/api/recovery-options", params=delay_facts("AR161"), headers=principal(AGENT)
        )

    assert response.status_code == 200, response.text
    [delayed] = [o for o in response.json()["options"] if o["option_id"] == "delay"]
    [flight] = [f for f in delayed["flights"] if f["flight_no"] == "AR161"]
    assert (flight["origin"], flight["ets"]) == ("LHR", True), flight


def check_delay_for_departed_or_unknown_flight_is_refused() -> None:
    store = ScenarioStore(TRACE)
    store.flights["AR111"]["std_local"] = store.flights["AR111"]["std_local"].replace(
        "10:40", "09:00"
    )
    store.flights["AR112"]["status"] = "cancelled"
    with client_for(store) as client:
        responses = [
            client.post("/api/delays", json=delay(flight_no), headers=principal(OPERATOR))
            for flight_no in ("AR111", "AR112", "AR999")
        ]
        agent = client.post("/api/delays", json=delay(), headers=principal(AGENT))

    assert [r.status_code for r in responses] == [409, 409, 409], [r.text for r in responses]
    assert agent.status_code == 403
    assert store.delays == {}


def check_an_hour_already_over_capacity_fails_only_an_option_that_adds_to_it() -> None:
    # Review: an earlier execution left Oporto over capacity at 11:00 and 12:00 (two
    # movements, one allowed). That must not fail every later option until Reset.
    class FullOporto(ScenarioStore):
        def domain_inventory(self, day: Any, domain: str) -> dict[str, Any]:
            inventory = super().domain_inventory(day, domain)
            if domain == "slots":
                inventory["capacity"]["OPO"] = 1
            return inventory

    store = FullOporto(TRACE)
    # AR161 LIS-LHR 17:30 +60 never touches Oporto. AR111 LIS-OPO +30 lands at 12:05 and adds a
    # third movement to Oporto's full 12:00 hour.
    with TestClient(create_app(store, frozenset({AGENT}), system="slots")) as slots:
        fits, adds = (
            slots.get(
                "/api/recovery-options/delay/impact",
                params=delay_facts(flight_no, minutes),
                headers=principal(AGENT),
            )
            for flight_no, minutes in (("AR161", 60), ("AR111", 30))
        )

    assert fits.status_code == 200 and adds.status_code == 200, (fits.text, adds.text)
    assert (fits.json()["constraint_status"], fits.json()["capacity_fit"]) == ("pass", True), (
        fits.json()
    )
    assert fits.json()["slot_conflicts"] == []
    assert adds.json()["constraint_status"] == "fail", adds.json()
    assert adds.json()["slot_conflicts"] == [
        {
            "airport": "OPO",
            "hour_local": f"{store.day.isoformat()}T12:00:00",
            "movements": 3,
            "hourly_capacity": 1,
        }
    ], adds.json()


class DelayReportTests(unittest.TestCase):
    def test_delay_report_offers_one_propagated_delay_option(self) -> None:
        check_delay_report_offers_one_propagated_delay_option()

    def test_a_short_delay_is_made_up_only_when_the_minutes_repay_the_fuel(self) -> None:
        check_a_short_delay_is_made_up_only_when_the_minutes_repay_the_fuel()

    def test_more_delay_is_flown_again_to_land_under_3_h(self) -> None:
        check_more_delay_is_flown_again_to_land_under_3_h()

    def test_a_flight_from_the_uk_pays_for_its_co2(self) -> None:
        check_a_flight_from_the_uk_pays_for_its_co2()

    def test_delay_for_departed_or_unknown_flight_is_refused(self) -> None:
        check_delay_for_departed_or_unknown_flight_is_refused()

    def test_an_hour_already_over_capacity_fails_only_an_option_that_adds_to_it(self) -> None:
        check_an_hour_already_over_capacity_fails_only_an_option_that_adds_to_it()
