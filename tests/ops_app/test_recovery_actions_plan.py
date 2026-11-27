"""Which messages and updates ARROW would send for a validated option (knowledge base §6.3)."""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from typing import Any

from arrow_scenario import ScenarioStore

from airline_systems import facts
from airline_systems.defects import recovery_options
from ops_app.actions import plan_actions, plan_hash
from ops_app.policy import RELEASE_LIMITS, classify
from ops_app.validator import validate

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
REBOOKING = {
    "disrupted_passengers": 7,
    "rebooked_passengers": 7,
    "unplaced_passengers": 0,
    "plan_digest": "d" * 64,
}
DUTIES = {
    "duties_changed": 1,
    "plan_digest": "c" * 64,
    "message": "Crew duties: D-LIS-001",
    "breaches_unresolved": 0,
}


def scenario(option_id: str) -> tuple[ScenarioStore, dict[str, Any], dict[str, Any]]:
    store = ScenarioStore(TRACE)
    defect = {"type": "aircraft_defect", "aircraft_reg": "CS-TQA", "unavailable_minutes": 240}
    found = facts.reported(store, facts.facts_of(defect))
    offered = recovery_options(store, found, inventory=facts.inventory(store, found))
    option = next(item for item in offered["options"] if item["option_id"] == option_id)
    originals = {
        flight["flight_no"]: flight for flight in store.recovery_inventory(store.day)["flights"]
    }
    return store, option, originals


def check_four_hour_delay_sends_each_coordination_message_once() -> None:
    store, option, originals = scenario("delay")
    airports = {rule["iata"]: rule for rule in store.airport_rules()}
    checked = validate(
        option, originals=originals, rebooking=REBOOKING, airports=airports, duty_plan=DUTIES
    )

    actions = plan_actions(
        TRACE,
        option,
        originals=originals,
        validation=checked,
        airports=airports,
        plan_hash=plan_hash(option, 3),
    )

    kinds = [action.kind for action in actions]
    assert kinds[0] == "retime_flights"
    delays = [a.payload["flight_no"] for a in actions if a.kind == "network_manager.fpl_delay"]
    assert sorted(delays) == ["AR101", "AR102", "AR131", "AR132"]
    tobt = [a.payload["flight_no"] for a in actions if a.kind == "airport_cdm.tobt_update"]
    assert sorted(tobt) == ["AR101", "AR131"]
    assert not any(kind.startswith("slot_coordinator") for kind in kinds)
    # Owner: the airline's reservation system notifies passengers downstream, not ARROW.
    assert "update_duties" in kinds and kinds[-1] == "rebook_passengers"
    assert "notify_passengers" not in kinds
    assert not any("rea" == kind.split(".")[-1] for kind in kinds)
    assert all(actions[0].action_id in a.depends_on for a in actions[1:])
    assert len({action.action_id for action in actions}) == len(actions)


def check_night_departure_after_midnight_needs_occ() -> None:
    day = date(2026, 11, 8)
    original = {
        "flight_no": "AR163",
        "origin": "LIS",
        "destination": "LHR",
        "std_local": f"{day}T21:30:00",
        "sta_local": f"{day}T23:55:00",
        "status": "scheduled",
    }
    late = {
        **original,
        "std_local": f"{day + timedelta(days=1)}T00:10:00",
        "sta_local": f"{day + timedelta(days=1)}T02:35:00",
    }
    option = {
        "option_id": "delay",
        "kind": "delay",
        "flights": [late],
        "domain_assessments": [
            {"domain": "crew", "constraint_status": "pass"},
            {"domain": "booking", "constraint_status": "pass"},
            {"domain": "slots", "constraint_status": "pass", "capacity_fit": True},
        ],
    }
    airports = {rule["iata"]: rule for rule in ScenarioStore(TRACE).airport_rules()}

    checked = validate(
        option,
        originals={"AR163": original},
        rebooking=REBOOKING,
        airports=airports,
        duty_plan=DUTIES,
    )
    verdict = classify(
        option, originals={"AR163": original}, validation=checked, limits=RELEASE_LIMITS
    )

    assert checked["airport_requests"] == ["LIS night quota: AR163 off-block 00:10"]
    assert verdict.route == "occ"
    assert "LIS night quota: AR163 off-block 00:10" in verdict.reasons, verdict


class CoordinationPlanTests(unittest.TestCase):
    def test_four_hour_delay_sends_each_coordination_message_once(self) -> None:
        check_four_hour_delay_sends_each_coordination_message_once()

    def test_night_departure_after_midnight_needs_occ(self) -> None:
        check_night_departure_after_midnight_needs_occ()
