"""The operations map's data (operations map spec §4): where each disruption is, the
affected aircraft, and what each plan would change, from the same figures as the control tower.

The four demo cases on the generated operating day; the orchestrator is scripted, the planner,
policy, executor, mocked systems and API are real.
"""

from __future__ import annotations

import unittest
from typing import Any

from fastapi.testclient import TestClient
from test_decision_trail import principal
from test_four_cases import CASE_1, CASE_3, PDL_BREACH, delay, one_plan, run
from test_orchestrated_flow import HOST, OPERATOR, submitted

from ops_app.app import create_app
from ops_app.policy import originals_of
from ops_app.tools import ArrowTools


def client(case: dict[str, Any]) -> TestClient:
    return TestClient(
        create_app(
            case["_cases"],
            case["_operations"],
            frozenset({OPERATOR}),
            frozenset({HOST}),
            "https://arrow.test",
        )
    )


def get_map(case: dict[str, Any]) -> dict[str, Any]:
    with client(case) as http:
        response = http.get(f"/api/cases/{case['trace_id']}/map", headers=principal(OPERATOR))
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def get_row(case: dict[str, Any]) -> dict[str, Any]:
    with client(case) as http:
        rows = http.get("/api/cases", headers=principal(OPERATOR)).json()["cases"]
    [row] = [row for row in rows if row["trace_id"] == case["trace_id"]]
    return dict(row)


def both_plans(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
    tools.build_plan(trace, {"aircraft_option_id": "cancel"})
    plan = tools.build_plan(trace, {"aircraft_option_id": "delay"})
    tools.submit_plan(trace, plan["plan_id"], "Keeps every flight")
    return submitted(plan["plan_id"])


def no_reserve(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
    plan = tools.build_plan(trace, {"aircraft_option_id": "delay"})
    tools.submit_plan(trace, plan["plan_id"], "Only option")
    return {"outcome": "no_plan", "summary": "No reserve crew", "submitted_plan_id": None}


def check_defect_waiting_for_occ_shows_both_plans() -> None:
    registration, minutes = CASE_3
    case = run(
        {
            "type": "aircraft_defect",
            "aircraft_reg": registration,
            "description": "Hydraulic leak",
            "unavailable_minutes": minutes,
        },
        both_plans,
    )
    view, row = get_map(case), get_row(case)
    day = case["operating_date"]

    assert (view["station"], view["aircraft_reg"]) == ("LIS", "CS-TQA"), view
    assert view["unavailable"] == {"from": f"{day}T09:45:00", "until": f"{day}T13:45:00"}
    assert (view["state"], view["label"], view["report"]) == (
        "needs",
        "Needs your decision",
        "Hydraulic leak",
    )
    assert view["disruption_type"] == "aircraft_defect"
    assert row["station"] == "LIS"
    assert sum(plan["recommended"] for plan in view["plans"]) == 1
    assert all(not p["executed"] and p["route"] == "occ" for p in view["plans"]), view["plans"]
    originals = originals_of(case)
    for plan in view["plans"]:
        for flight in plan["flights"]:
            assert flight["std_local"] == originals[flight["flight_no"]]["std_local"], flight
    [cancel] = [p for p in view["plans"] if any(f["status"] == "cancelled" for f in p["flights"])]
    cancelled = {f["flight_no"] for f in cancel["flights"] if f["status"] == "cancelled"}
    assert {"AR101", "AR102"} <= cancelled, cancelled
    assert all(f["planned_local"] is None for f in cancel["flights"] if f["status"] == "cancelled")
    assert any(r.startswith("Cancellation: ") and "AR101" in r for r in cancel["reasons"]), cancel
    # A Dev run: the card read "Cost limit for automatic plans: €118,305", the plan's own
    # cost under the limit's name. Each limit rule shows the plan's value against the limit.
    for shown in view["plans"]:
        assert not any(r.startswith("Cost limit for") for r in shown["reasons"]), shown["reasons"]
        assert f"Cost €{shown['cost_eur']:,.0f} · limit €20,000" in shown["reasons"], shown
    assert {shown["kind"] for shown in view["plans"]} == {"cancel", "delay"}, view["plans"]
    # The tower row names the same plan the map and case page name (plan kind, cancelled flights).
    [best] = [plan for plan in view["plans"] if plan["recommended"]]
    cancelled_in_best = [f["flight_no"] for f in best["flights"] if f["status"] == "cancelled"]
    assert (row["impact"]["plan_kind"], row["impact"]["plan_cancelled"]) == (
        best["kind"],
        cancelled_in_best,
    ), row["impact"]
    [best] = [plan for plan in view["plans"] if plan["recommended"]]
    assert (best["passengers"], best["cost_eur"]) == (
        row["impact"]["passengers"],
        row["impact"]["cost_eur"],
    )


def check_delay_that_ran_automatically_is_executed_without_reasons() -> None:
    case = run(delay(CASE_1), one_plan({"aircraft_option_id": "delay"}))
    view = get_map(case)

    [plan] = view["plans"]
    assert plan["executed"] and plan["authorised_by"] == "policy", plan
    assert plan["route"] == "auto" and plan["reasons"] == [], plan
    assert plan["passengers"] == 0 and plan["cost_eur"] > 0, plan
    assert view["aircraft_reg"] == originals_of(case)["AR259"]["aircraft_reg"]
    assert view["station"] == "LIS"


def check_blocked_plan_shows_the_crew_limit() -> None:
    case = run(
        {**delay(PDL_BREACH), "cause": "late_inbound", "description": "Late inbound aircraft"},
        no_reserve,
    )
    view = get_map(case)

    assert (view["station"], view["label"]) == ("PDL", "Needs manual action"), view
    [plan] = view["plans"]
    assert plan["route"] == "blocked" and not plan["executed"], plan
    assert any(r.startswith("Crew flight-time limits: ") for r in plan["reasons"]), plan


def check_carried_out_plan_is_named_and_the_other_is_not_a_proposal() -> None:
    # Final review: OCC approved the plan that was not recommended; the map must show what ran.
    registration, minutes = CASE_3
    case = run(
        {
            "type": "aircraft_defect",
            "aircraft_reg": registration,
            "description": "Hydraulic leak",
            "unavailable_minutes": minutes,
        },
        both_plans,
    )
    [cancel] = [p for p in case["options"] if p["kind"] == "cancel"]
    operations = case["_operations"]
    operations.record_decision(
        case["trace_id"],
        option_id=cancel["option_id"],
        decision="approve",
        expected_version=case["version"],
        idempotency_key="decision-map-cancel-0001",
        actor_oid=OPERATOR,
    )
    operations.execute(case["trace_id"])
    view, row = get_map(case), get_row(case)

    plans = {plan["option_id"]: plan for plan in view["plans"]}
    ran = plans[cancel["option_id"]]
    assert ran["execution"]["status"] == "completed", ran
    assert ran["execution"]["actions_done"] == ran["execution"]["actions_total"] > 0, ran
    assert ran["executed"] and ran["authorised_by"] == "occ" and not ran["recommended"], ran
    [other] = [plan for key, plan in plans.items() if key != cancel["option_id"]]
    assert other["execution"] is None and not other["executed"], other
    assert (ran["passengers"], ran["cost_eur"]) == (
        row["impact"]["passengers"],
        row["impact"]["cost_eur"],
    )


def check_unknown_and_anonymous_requests_are_refused() -> None:
    case = run(delay(CASE_1), one_plan({"aircraft_option_id": "delay"}))
    with client(case) as http:
        unknown = http.get(f"/api/cases/{'f' * 32}/map", headers=principal(OPERATOR))
        anonymous = http.get(f"/api/cases/{case['trace_id']}/map")

    assert unknown.status_code == 404, unknown.text
    assert anonymous.status_code == 401, anonymous.text


class OperationsMapTests(unittest.TestCase):
    def test_defect_waiting_for_occ_shows_both_plans(self) -> None:
        check_defect_waiting_for_occ_shows_both_plans()

    def test_delay_that_ran_automatically_is_executed_without_reasons(self) -> None:
        check_delay_that_ran_automatically_is_executed_without_reasons()

    def test_blocked_plan_shows_the_crew_limit(self) -> None:
        check_blocked_plan_shows_the_crew_limit()

    def test_carried_out_plan_is_named_and_the_other_is_not_a_proposal(self) -> None:
        check_carried_out_plan_is_named_and_the_other_is_not_a_proposal()

    def test_unknown_and_anonymous_requests_are_refused(self) -> None:
        check_unknown_and_anonymous_requests_are_refused()
