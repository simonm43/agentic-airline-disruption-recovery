"""Editable autonomy limits: a rule change applies only when a second OCC duty manager approves,
and only to plans built after it; a plan keeps the rule version it was built with.

Real app, rule store and four-eyes checks (MemoryRules), with the scripted orchestrator of the
orchestrated flow. SqlRules runs the same checks and is verified in Dev.
"""

from __future__ import annotations

import unittest
from typing import Any

from arrow_scenario import ScenarioStore
from fastapi.testclient import TestClient
from test_operator_flow import EXPERT, headers
from test_orchestrated_flow import (
    DEFECT,
    HOST,
    OPERATOR,
    STAFF_DELAY,
    TRACE,
    insist,
    operate,
    submitted,
)

from ops_app.app import create_app
from ops_app.policy import RELEASE_LIMITS, Limits
from ops_app.rules import MemoryRules
from ops_app.tools import ArrowTools, ToolRefused

SECOND = "00000000-0000-4000-8000-000000000014"  # Test User, the second duty manager (D98)
POLICY_STOP = "no longer passes the authority policy"


def delay_submitted(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
    plan = tools.build_plan(trace, {"aircraft_option_id": "delay"})
    tools.submit_plan(trace, plan["plan_id"], "Short delay, every passenger travels")
    return submitted(plan["plan_id"])


def check_limits_change_only_with_a_second_duty_manager() -> None:
    operations, cases, _ = operate(ScenarioStore(TRACE), delay_submitted)
    # Today's staff delay, built under version 1 and carried out automatically.
    trace = operations.open(STAFF_DELAY, OPERATOR)["trace_id"]
    operations.run(trace)
    today = cases.get(trace)
    [plan] = today["options"]
    assert plan["policy"]["route"] == "auto" and plan["policy"]["version"] == 1, plan["policy"]
    app = create_app(
        cases,
        operations,
        frozenset({OPERATOR, SECOND}),
        frozenset({HOST}),
        "https://arrow.test",
        expert_oids=frozenset({EXPERT}),
    )
    with TestClient(app) as client:

        def rules(oid: str = OPERATOR) -> dict[str, Any]:
            response = client.get("/api/rules", headers=headers(oid))
            assert response.status_code == 200, response.text
            shown: dict[str, Any] = response.json()
            return shown

        def propose(oid: str, cost: int, rebook: int, reason: str) -> Any:
            body = {"cost_cap_eur": cost, "rebook_limit": rebook, "reason": reason}
            return client.post("/api/rules/proposals", headers=headers(oid), json=body)

        def decide(oid: str, change_id: int, action: str) -> Any:
            return client.post(f"/api/rules/proposals/{change_id}/{action}", headers=headers(oid))

        shown = rules()
        limits = {
            key: shown["in_force"][key] for key in ("version", "cost_cap_eur", "rebook_limit")
        }
        assert limits == {"version": 1, "cost_cap_eur": 20000, "rebook_limit": 10}, shown
        assert shown["pending"] is None and shown["can_change"] is True, shown
        assert rules(EXPERT)["can_change"] is False

        assert propose(EXPERT, 15000, 10, "Winter schedule").status_code == 403
        same = propose(OPERATOR, 20000, 10, "same")
        assert same.status_code == 422 and "changes nothing" in same.json()["detail"], same.text
        winter = propose(OPERATOR, 15000, 10, "Winter schedule")
        assert winter.status_code == 201 and winter.json()["status"] == "proposed", winter.text
        change = winter.json()["change_id"]
        assert propose(SECOND, 18000, 10, "Another change").status_code == 409
        # Today's €1,575 plan stays within €15,000, so the preview lists no change.
        assert rules()["pending"]["effect"] == [], rules()["pending"]

        own = decide(OPERATOR, change, "approve")
        assert own.status_code == 403 and "You proposed this change" in own.json()["detail"]
        approved = decide(SECOND, change, "approve")
        assert approved.status_code == 200 and approved.json()["version"] == 2, approved.text
        shown = rules()
        assert shown["in_force"]["cost_cap_eur"] == 15000 and shown["pending"] is None, shown
        assert shown["in_force"]["approved_by"] == SECOND, shown

        tighter = propose(SECOND, 0, 0, "Every plan to OCC")
        assert tighter.status_code == 201, tighter.text
        # The approver sees which of today's plans would change route under the proposal.
        effect = rules()["pending"]["effect"]
        assert effect == [
            {
                "display_id": today["display_id"],
                "headline": "AR131 · 20 min late",
                "plan": plan["plan_id"],
                "now": "auto",
                "proposed": "occ",
            }
        ], effect

        # Today's cases cannot be read: the rules still show, without the effect preview.
        def unreadable(day: str) -> list[dict[str, Any]]:
            raise RuntimeError("case store unreachable")

        cases.list = unreadable  # type: ignore[method-assign]
        try:
            shown = rules()
        finally:
            del cases.list
        assert shown["pending"]["effect"] is None, shown["pending"]
        assert shown["in_force"]["version"] == 2 and shown["in_force"]["cost_cap_eur"] == 15000
        withdrawn = decide(SECOND, tighter.json()["change_id"], "withdraw")
        assert withdrawn.status_code == 200 and withdrawn.json()["status"] == "withdrawn"
        # The approver's page was stale: the change was withdrawn meanwhile, so nothing changes.
        stale = decide(OPERATOR, tighter.json()["change_id"], "approve")
        assert stale.status_code == 409, stale.text

        shown = rules()
        assert shown["in_force"]["version"] == 2, shown
        history = [(change["status"], change["version"]) for change in shown["history"]]
        assert history == [("withdrawn", None), ("approved", 2), ("approved", 1)], history


def check_a_rule_change_applies_only_to_plans_built_after_it() -> None:
    rules = MemoryRules()

    def defect_plans(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        tools.build_plan(trace, {"aircraft_option_id": "cancel"})
        delay = tools.build_plan(trace, {"aircraft_option_id": "delay"})
        insist(tools, trace, delay["plan_id"], "Delay keeps every passenger travelling")
        return submitted(delay["plan_id"])

    # (a) Built under version 1: the defect's plans wait for OCC, and a short staff delay is
    # authorised to run automatically but has not run yet when the rules change.
    defect_ops, defect_cases, _ = operate(ScenarioStore(TRACE), defect_plans, rules)
    defect = defect_ops.open(DEFECT, OPERATOR)["trace_id"]
    defect_ops.run(defect)
    built = defect_cases.get(defect)
    routes = [(p["kind"], p["policy"]["version"], p["policy"]["route"]) for p in built["options"]]
    assert sorted(routes) == [("cancel", 1, "occ"), ("delay", 1, "occ")], routes

    deferred: list[str] = []
    auto_ops, auto_cases, auto_gateway = operate(ScenarioStore(TRACE), delay_submitted, rules)
    auto_gateway.tools = ArrowTools(auto_cases, auto_gateway, execute=deferred.append, rules=rules)
    auto = auto_ops.open(STAFF_DELAY, OPERATOR)["trace_id"]
    auto_ops.run(auto)
    authorised = auto_cases.get(auto)
    assert authorised["options"][0]["policy"]["version"] == 1, authorised["options"]
    assert authorised["execution"]["status"] == "approved" and deferred == [auto], deferred

    # (b) A second duty manager approves version 2: every plan built from now on goes to OCC.
    change = rules.propose(0, 0, "Every plan to OCC", OPERATOR)
    assert rules.decide(change.change_id, "approve", SECOND).version == 2

    # (c) Plans built under version 1 keep it: OCC's approval and the automatic plan both run.
    [recommended] = [plan for plan in built["options"] if plan.get("recommended")]
    defect_ops.record_decision(
        defect,
        option_id=recommended["option_id"],
        decision="approve",
        expected_version=built["version"],
        idempotency_key="decision-approve-rules-v1-0001",
        actor_oid=OPERATOR,
    )
    defect_ops.execute(defect)
    auto_ops.execute(auto)
    for cases, trace, approver in ((defect_cases, defect, OPERATOR), (auto_cases, auto, "policy")):
        done = cases.get(trace)
        assert done["execution"]["status"] == "completed", done["execution"]
        assert done["execution"]["approved_by"] == approver, done["execution"]
        assert not any(POLICY_STOP in str(e.get("message")) for e in done["events"]), done["events"]

    # (d) The same staff delay reported now is built under version 2 and waits for OCC.
    later_store = ScenarioStore(TRACE)
    later_ops, later_cases, later_gateway = operate(later_store, delay_submitted, rules)
    later = later_ops.open(STAFF_DELAY, OPERATOR)["trace_id"]
    later_ops.run(later)
    case = later_cases.get(later)
    [plan] = case["options"]
    assert plan["policy"]["route"] == "occ" and plan["policy"]["version"] == 2, plan["policy"]
    assert plan["policy"]["limits"] == {"version": 2, "cost_cap_eur": 0, "rebook_limit": 0}
    assert "execution" not in case
    assert later_gateway.briefs[-1]["limits"]["rules_version"] == 2, later_gateway.briefs[-1]
    assert later_gateway.briefs[-1]["limits"]["cost_cap_eur"] == 0
    # The plan hash binds the recorded limits: relabelling an approved plan stops it unexecuted.
    later_ops.record_decision(
        later,
        option_id=plan["option_id"],
        decision="approve",
        expected_version=case["version"],
        idempotency_key="decision-approve-rules-v2-relabel",
        actor_oid=OPERATOR,
    )
    later_cases.docs[later]["options"][0]["policy"]["limits"] = RELEASE_LIMITS.as_dict()
    later_ops.execute(later)
    stopped = later_cases.get(later)
    assert stopped["execution"]["status"] == "needs_attention", stopped["execution"]
    assert stopped["events"][-1]["message"].startswith(
        "The approved plan no longer matches the case"
    ), stopped["events"][-1]
    assert later_store.actions == {}

    # (e) Version 3 is approved while a case is being assessed: each plan keeps its own version,
    # and OCC can still approve the plan built before the change.
    def rules_change_midway(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        first = tools.build_plan(trace, {"aircraft_option_id": "delay"})
        change = rules.propose(20_000, 10, "Back to the release limits", SECOND)
        rules.decide(change.change_id, "approve", OPERATOR)
        tools.build_plan(trace, {"aircraft_option_id": "delay", "rebook": False})
        tools.submit_plan(trace, first["plan_id"], "Every passenger travels")
        return submitted(first["plan_id"])

    mid_ops, mid_cases, _ = operate(ScenarioStore(TRACE), rules_change_midway, rules)
    mid = mid_ops.open(STAFF_DELAY, OPERATOR)["trace_id"]
    mid_ops.run(mid)
    case = mid_cases.get(mid)
    versions = {plan["levers"]["rebook"]: plan["policy"]["version"] for plan in case["options"]}
    assert versions == {True: 2, False: 3}, versions
    [first] = [plan for plan in case["options"] if plan.get("recommended")]
    assert first["policy"]["route"] == "occ", first["policy"]
    mid_ops.record_decision(
        mid,
        option_id=first["option_id"],
        decision="approve",
        expected_version=case["version"],
        idempotency_key="decision-approve-rules-v2-0001",
        actor_oid=OPERATOR,
    )
    mid_ops.execute(mid)
    done = mid_cases.get(mid)
    assert done["execution"]["status"] == "completed", done["execution"]
    assert done["execution"]["route"] == "occ", done["execution"]  # its own version's route
    assert not any(POLICY_STOP in str(e.get("message")) for e in done["events"]), done["events"]

    # (f) Rules that cannot be read build no plan; nothing runs and the case stops for OCC.
    seen: dict[str, str] = {}

    def rules_unreadable(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        def unreadable() -> Limits:
            raise RuntimeError("rule store unreachable")

        down_ops.rules.in_force = unreadable  # type: ignore[method-assign]
        try:
            tools.build_plan(trace, {"aircraft_option_id": "delay"})
        except ToolRefused as refusal:
            seen["refusal"] = str(refusal)
        return {"outcome": "no_plan", "summary": "Rules unavailable", "submitted_plan_id": None}

    store = ScenarioStore(TRACE)
    down_ops, down_cases, _ = operate(store, rules_unreadable)
    down = down_ops.open(STAFF_DELAY, OPERATOR)["trace_id"]
    down_ops.run(down)
    case = down_cases.get(down)
    assert "Rules unavailable" in seen.get("refusal", ""), seen
    assert case["status"] == "failed", case["status"]
    assert case["stage"].startswith("Assessment stopped"), case["stage"]
    assert not case.get("options") and "execution" not in case
    assert store.actions == {}


class RulesFlowTests(unittest.TestCase):
    def test_limits_change_only_with_a_second_duty_manager(self) -> None:
        check_limits_change_only_with_a_second_duty_manager()

    def test_a_rule_change_applies_only_to_plans_built_after_it(self) -> None:
        check_a_rule_change_applies_only_to_plans_built_after_it()
