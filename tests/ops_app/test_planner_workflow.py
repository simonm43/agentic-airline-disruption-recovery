"""The planner computes recovery plans from the systems: agents choose levers, code does the maths.

Real planner, cost estimate, policy and mocked-system services; only HTTP is replaced by calls.
"""

from __future__ import annotations

import unittest
from typing import Any

from arrow_scenario import ScenarioStore
from test_execution_workflow import SystemsGateway

from airline_systems import facts
from airline_systems.defects import recovery_options
from airline_systems.recovery import RebookingService
from ops_app.actions import plan_actions
from ops_app.dispatch import calls
from ops_app.planner import CaseRef, Levers, build_plan
from ops_app.policy import RELEASE_LIMITS, originals_of

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
DEFECT = {"type": "aircraft_defect", "aircraft_reg": "CS-TQA", "unavailable_minutes": 240}


def case_document(store: ScenarioStore, delay_minutes: int | None = None) -> dict[str, Any]:
    """The case once its disruption is recorded on it: CS-TQA's defect or, given
    ``delay_minutes``, AR131 late for a staff shortage. The systems are asked by its facts."""
    flights = store.recovery_inventory(store.day)["flights"]
    rotation = [flight for flight in flights if flight["aircraft_reg"] == "CS-TQA"]
    if delay_minutes is None:
        recorded: dict[str, Any] = {
            "disruption": {
                "type": "aircraft_defect",
                "cause": "aircraft_defect",
                "scope": {"flights": [], "aircraft_reg": "CS-TQA"},
            },
            "defect": {"aircraft_reg": "CS-TQA", "unavailable_minutes": 240},
        }
    else:
        recorded = {
            "disruption": {
                "type": "flight_delay",
                "cause": "staff_shortage",
                "flight_no": "AR131",
                "delay_minutes": delay_minutes,
                "scope": {"flights": ["AR131"], "aircraft_reg": "CS-TQA"},
            }
        }
    return {"trace_id": TRACE, "version": 3, **recorded, "rotation": {"flights": rotation}}


def check_plan_is_computed_from_the_systems_not_the_levers_text() -> None:
    # CS-TQA is unavailable until 13:45: AR101 and AR102 leave 195 min late, AR131 155 and
    # AR132 150 (35-min turns). Airline cost: €17.78/min x 695 min. Per booking (Booking's
    # journeys): SYN001 reaches OPO 195 min late (€250 x 2) and waits 195 min (meals €10 x 2);
    # SYN003 waits 155 min (€7); SYN004 misses AR161 and reaches LHR 235 min late on AR163
    # (€400 x 4) after a 240-min wait at LIS (€13 x 4); SYN002 is rebooked onto AR112 at 12:05.
    # Rebooking on our own flights: SYN004 €13 x 4 and SYN002 €11 x 3.
    store = ScenarioStore(TRACE)
    plan = build_plan(
        SystemsGateway(store),
        case_document(store),
        Levers(aircraft_option_id="delay"),
        RELEASE_LIMITS,
    )

    found = facts.reported(store, facts.facts_of(DEFECT))
    offered = recovery_options(store, found, inventory=facts.inventory(store, found))
    schedule = next(o for o in offered["options"] if o["kind"] == "delay")
    booking = RebookingService(store).plan(found, "delay")
    assert [(f["flight_no"], f["std_local"]) for f in plan["flights"]] == [
        (f["flight_no"], f["std_local"]) for f in schedule["flights"]
    ]
    rebooking = plan["validation"]["rebooking"]
    assert rebooking["disrupted_passengers"] == booking["disrupted_passengers"]
    assert rebooking["rebooked_passengers"] == booking["rebooked_passengers"]
    cost = plan["cost"]
    assert (cost["airline_eur"], cost["compensation_eur"], cost["care_eur"]) == (12357, 2100, 79)
    assert cost["rebooking_eur"] == 85, cost
    assert cost["total_eur"] == cost["low_eur"] == cost["high_eur"] == 14621, cost
    assert cost["overnight_passengers"] == 0
    assert cost["basis"].startswith("indicative")
    assert "journeys" not in plan and "journeys" not in rebooking
    assert len(plan["plan_id"]) == 16 and plan["conflict"] is False
    assert plan["policy"]["route"] == "occ", plan["policy"]


def check_cancel_plan_prices_cancellations_and_care() -> None:
    # AR101 and AR102 cancelled (174 seats): €13,320 each. SYN001 and SYN002 are rebooked onto
    # AR111 and AR112, arriving under 2 h late: no compensation; meals €18 x 5 for the cancellation;
    # rebooking on our own flights €11 x 5.
    store = ScenarioStore(TRACE)
    plan = build_plan(
        SystemsGateway(store),
        case_document(store),
        Levers(aircraft_option_id="cancel"),
        RELEASE_LIMITS,
    )

    cost = plan["cost"]
    assert (cost["airline_eur"], cost["compensation_eur"], cost["care_eur"]) == (26640, 0, 90), cost
    assert cost["rebooking_eur"] == 55 and cost["total_eur"] == 26785, cost


def check_unknown_journeys_send_the_plan_to_occ() -> None:
    # A Booking release without journey facts must not price passengers at zero.
    class OldBooking(SystemsGateway):
        def rebooking(
            self, case: CaseRef, option_id: str, additional_delay_minutes: int
        ) -> dict[str, Any]:
            plan = super().rebooking(case, option_id, additional_delay_minutes)
            return {key: value for key, value in plan.items() if key != "journeys"}

    store = ScenarioStore(TRACE)
    plan = build_plan(
        OldBooking(store),
        case_document(store, 20),
        Levers(aircraft_option_id="delay"),
        RELEASE_LIMITS,
    )

    assert plan["cost"] is None
    assert any(item.startswith("estimated cost") for item in plan["unknowns"]), plan["unknowns"]
    assert plan["policy"]["route"] != "auto", plan["policy"]


def check_declined_rebooking_counts_passengers_as_not_rebooked() -> None:
    store = ScenarioStore(TRACE)
    gateway, document = SystemsGateway(store), case_document(store)
    rebooked = build_plan(gateway, document, Levers(aircraft_option_id="delay"), RELEASE_LIMITS)
    declined = build_plan(
        gateway, document, Levers(aircraft_option_id="delay", rebook=False), RELEASE_LIMITS
    )

    # SYN002 now misses AR151 and SYN004 AR161 with no rebooking: full compensation.
    assert declined["cost"]["compensation_eur"] > rebooked["cost"]["compensation_eur"], (
        declined["cost"],
        rebooked["cost"],
    )
    # Final review: not rebooked, SYN002 flies the delayed AR102 (lands 16:10) and waits at LIS
    # for AR153 at 19:00: 300 minutes, meals €13 x 3, on top of the rebooked plan's €79.
    assert declined["cost"]["care_eur"] == rebooked["cost"]["care_eur"] + 39, (
        declined["cost"],
        rebooked["cost"],
    )


def check_short_staff_delay_plan_runs_automatically() -> None:
    # Case 1: AR131 +20 min (staff shortage) -> AR132 +15. Airline cost €17.78/min x 35 min.
    # SYN004 now has 55 min from AR132 to AR161 for London: short of 60, but a car keeps it
    # through passport control (D108), so no one is rebooked or compensated. Every domain stays
    # within its limits.
    store = ScenarioStore(TRACE)
    plan = build_plan(
        SystemsGateway(store),
        case_document(store, 20),
        Levers(aircraft_option_id="delay"),
        RELEASE_LIMITS,
    )

    assert plan["cost"]["airline_eur"] == round(17.78 * 35), plan["cost"]
    assert plan["cost"]["compensation_eur"] == 0, plan["cost"]
    rebooking = plan["validation"]["rebooking"]
    assert (rebooking["disrupted_passengers"], rebooking["transferred_passengers"]) == (0, 4)
    assert plan["policy"]["route"] == "auto", plan["policy"]


def check_reserve_reporting_late_adds_delay_until_domains_agree() -> None:
    # AR131 +60 breaches D-LIS-001 (FDP 465, limit 450). The LIS reserves stand by at home from
    # 14:25; called then, they report at 15:25 (Task 1: home standby answers in 60 min,
    # was 14:25), so the plan delays AR131 75 min more for a 60-min report (was 15).
    store = ScenarioStore(TRACE)
    store.duties["D-LIS-001"]["max_fdp_minutes"] = 450
    for reserve in store.reserves:
        reserve["start_local"] = reserve["start_local"].replace("T12:00", "T14:25")
    gateway, document = SystemsGateway(store), case_document(store, 60)
    plan = build_plan(
        gateway,
        document,
        Levers(aircraft_option_id="delay", reserve_option_id="R1"),
        RELEASE_LIMITS,
    )

    assert plan["adjustment_minutes"] == 75, plan  # Task 1: home call-out of 60 min (was 15)
    assert plan["conflict"] is False
    # Task 1: the crew reports an hour after the call (was T14:25).
    assert plan["crew_reserve"]["report_local"].endswith("T15:25:00"), plan["crew_reserve"]
    flights = {f["flight_no"]: f for f in plan["flights"]}
    # Task 1: AR131 leaves 60 min after that report (was T15:25).
    assert flights["AR131"]["std_local"].endswith("T16:25:00"), flights["AR131"]
    assert plan["validation"]["crew_plan"]["breaches_unresolved"] == 0, plan["validation"]
    assert plan["rejected_levers"] == []

    # The planned crew write is the reserve assignment, and Crew's recompute accepts its digest.
    actions = plan_actions(
        TRACE,
        plan,
        originals=originals_of(document),
        validation=plan["validation"],
        airports={rule["iata"]: rule for rule in store.airport_rules()},
        plan_hash="a" * 64,
    )
    kinds = [action.kind for action in actions]
    assert "assign_reserve_crew" in kinds and "update_duties" not in kinds, kinds
    crew = next(action for action in actions if action.kind == "assign_reserve_crew")
    [call] = calls(crew.as_dict())
    reply = gateway.send(call.system, call.method, call.path, call.headers, call.body, TRACE)
    assert (call.recipient, call.path) == ("Crew system", "/api/duty-changes"), call
    assert reply["status"] == "accepted", reply
    assert sorted(store.duties["D-LIS-001-R1"]["flight_nos"]) == ["AR131", "AR132"]


def check_lever_not_offered_is_rejected() -> None:
    store = ScenarioStore(TRACE)
    gateway, document = SystemsGateway(store), case_document(store, 20)

    swap = build_plan(gateway, document, Levers(aircraft_option_id="swap"), RELEASE_LIMITS)
    reserve = build_plan(
        gateway,
        document,
        Levers(aircraft_option_id="delay", reserve_option_id="R9"),
        RELEASE_LIMITS,
    )

    assert any("swap" in lever for lever in swap["rejected_levers"]), swap
    assert swap["policy"]["route"] == "blocked"
    assert any("R9" in lever for lever in reserve["rejected_levers"]), reserve
    assert reserve["policy"]["route"] == "blocked"


def check_a_standby_crew_without_its_earliest_departure_blocks_the_plan() -> None:
    # Review: a Crew release that does not say when a standby crew can take its first
    # departure gives no gap to plan by; the plan is blocked with a plain reason, never a crash.
    class OldCrew(SystemsGateway):
        def reserve_options(
            self, case: CaseRef, option_id: str, additional_delay_minutes: int
        ) -> dict[str, Any]:
            offered = super().reserve_options(case, option_id, additional_delay_minutes)
            return {
                **offered,
                "options": [
                    {k: v for k, v in option.items() if k != "earliest_departure_local"}
                    for option in offered["options"]
                ],
            }

    store = ScenarioStore(TRACE)
    store.duties["D-LIS-001"]["max_fdp_minutes"] = 450
    plan = build_plan(
        OldCrew(store),
        case_document(store, 60),
        Levers(aircraft_option_id="delay", reserve_option_ids=["R1"]),
        RELEASE_LIMITS,
    )

    assert plan["policy"]["route"] == "blocked", plan["policy"]
    assert "standby crew report time not established" in plan["policy"]["reasons"], plan["policy"]


class PlannerWorkflowTests(unittest.TestCase):
    def test_plan_is_computed_from_the_systems_not_the_levers_text(self) -> None:
        check_plan_is_computed_from_the_systems_not_the_levers_text()

    def test_cancel_plan_prices_cancellations_and_care(self) -> None:
        check_cancel_plan_prices_cancellations_and_care()

    def test_unknown_journeys_send_the_plan_to_occ(self) -> None:
        check_unknown_journeys_send_the_plan_to_occ()

    def test_declined_rebooking_counts_passengers_as_not_rebooked(self) -> None:
        check_declined_rebooking_counts_passengers_as_not_rebooked()

    def test_short_staff_delay_plan_runs_automatically(self) -> None:
        check_short_staff_delay_plan_runs_automatically()

    def test_reserve_reporting_late_adds_delay_until_domains_agree(self) -> None:
        check_reserve_reporting_late_adds_delay_until_domains_agree()

    def test_lever_not_offered_is_rejected(self) -> None:
        check_lever_not_offered_is_rejected()

    def test_a_standby_crew_without_its_earliest_departure_blocks_the_plan(self) -> None:
        check_a_standby_crew_without_its_earliest_departure_blocks_the_plan()
