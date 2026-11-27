"""The demo cases (spec §11; Case 5, the TAF crosswind forecast) on the generated
operating day, end to end.

The orchestrator is scripted with the levers a reasonable OCC would choose; the planner, policy,
executor and mocked systems are real. The day is the same pattern every day, so the pinned
flights stay valid; a case that stops matching its description fails here (for example if case
2 starts needing more than 10 rebookings). Pins come from tools/ops_app/find_cases.py.
"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any
from unittest import mock

from day_store import OperatingDayStore
from fastapi.testclient import TestClient
from test_event_intake import TESTBENCH, cloud_event, principal
from test_orchestrated_flow import HOST, OPERATOR, Script, insist, operate, submitted

from airline_systems.operating_day import build_operating_day
from airline_systems.schedule import LISBON
from ops_app.app import create_app
from ops_app.case_digest import case_digest
from ops_app.engine import CloudDependencyError
from ops_app.events import Delivery, Rejections, read_event
from ops_app.executor import Executor
from ops_app.intake import Intake
from ops_app.planner import CaseRef, case_ref
from ops_app.policy import COST_CAP_EUR, CREW_CONNECTION_REASON, REBOOK_LIMIT
from ops_app.store import DecisionConflict
from ops_app.tools import ArrowTools, ToolRefused
from ops_app.trail import build_trail, case_card, case_state

CASE_1 = ("AR259", 20, "border_control")
# Owner (Lisbon's Entry/Exit System queues): AR259 flies Lisbon to Belém, outside the
# Schengen area, so its passengers pass exit border control. The Test Bench's Case 1 message.
CASE_1_MESSAGE = (
    "Border-control queues (Entry/Exit System checks) are holding passengers for AR259: it will"
    " leave about 20 minutes late."
)
# D108: a car now keeps AR805's connections, so it no longer rebooks; AR444 (HAM-LIS) rebooks
# some passengers within policy and sends the rest by car. D116: 70 minutes, as AR444 now makes
# up five minutes in the air and 60 minutes late no longer breaks a connection a car cannot keep.
CASE_2 = ("AR444", 70, "staff_shortage")
CASE_3 = ("CS-TQA", 240)
CASE_4 = ("AR882", 75, "staff_shortage")
# PDL-FNC: AR689's delay breaks D-PDL-002 (731 of 720 min). Since Task 1 Ponta Delgada's
# one standby crew can take AR941-AR942: called, it is the base's last and OCC decides; not called,
# the delay is blocked with no standby crew.
PDL_BREACH = ("AR689", 60, "late_inbound")
CREW_LIMIT = "flight-time limit exceeded with no standby crew"  # Task 2: was "reserve crew"
# Owner: further plans after an automatic one are "useless analysis".
AUTOMATIC_BUILT = "An automatic plan within every limit is already built; no other plan is needed"


DESCRIPTIONS = {
    "border_control": "Border-control queues (Entry/Exit System checks) are holding passengers",
    "staff_shortage": "Ground staff shortage at the departure station",
    "late_inbound": "Late inbound aircraft",
}


def delay(case: tuple[str, int, str]) -> dict[str, Any]:
    flight_no, minutes, cause = case
    return {
        "type": "flight_delay",
        "flight_no": flight_no,
        "delay_minutes": minutes,
        "cause": cause,
        "description": DESCRIPTIONS[cause],
    }


def run(
    report: dict[str, Any], script: Script, store: OperatingDayStore | None = None
) -> dict[str, Any]:
    operations, cases, _ = operate(store or OperatingDayStore(), script)
    trace = operations.open(report, OPERATOR)["trace_id"]
    operations.run(trace)
    case = cases.get(trace)
    case["_operations"], case["_cases"] = operations, cases
    return case


def event_of(tools: ArrowTools, trace: str) -> CaseRef:
    """The case's operations event, which the agents' system reads name."""
    return case_ref(tools.store.get(trace))


def allowed(tools: ArrowTools, trace: str) -> list[str]:
    """The aircraft option kinds the playbook allows, as the orchestrator reads them right after
    the Detection agent."""
    kinds: list[str] = tools.get_playbook(trace)["allowed_aircraft_kinds"]
    return kinds


def one_plan(levers: dict[str, Any]) -> Script:
    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        plan = tools.build_plan(trace, levers)
        insist(tools, trace, plan["plan_id"], "Within every domain's limits")
        return submitted(plan["plan_id"])

    return script


def recommended(case: dict[str, Any]) -> dict[str, Any]:
    [plan] = [plan for plan in case["options"] if plan.get("recommended")]
    return plan


def assert_completed(case: dict[str, Any]) -> list[str]:
    execution = case["_cases"].get(case["trace_id"])["execution"]
    assert execution["status"] == "completed", execution
    statuses = {action["kind"]: action["status"] for action in execution["actions"]}
    assert set(statuses.values()) == {"confirmed"}, statuses
    return list(statuses)


PARTS = ("total_eur", "airline_eur", "compensation_eur", "care_eur", "rebooking_eur")
MOVED = (
    "disrupted_passengers",
    "rebooked_passengers",
    "unplaced_passengers",
    "transferred_passengers",
)


def figures(plan: dict[str, Any]) -> tuple[int, ...]:
    """A plan's cost and its parts, then who loses a flight or connection, is rebooked, goes to
    Passenger Services and goes by car."""
    rebooking = plan["validation"]["rebooking"]
    return (*(plan["cost"][key] for key in PARTS), *(rebooking[key] for key in MOVED))


def slot_check(plan: dict[str, Any]) -> tuple[bool, int]:
    """What Slots said of the plan's option: whether its departures need delay messages, and how
    many leave more than 15 minutes later than planned."""
    [slots] = [found for found in plan["domain_assessments"] if found.get("domain") == "slots"]
    return slots["delay_messages_required"], slots["departures_moved_over_15_min"]


def check_case_1_border_control_queue_20_min_runs_automatically() -> None:
    # The Detection agent reads the Test Bench's message as typed values (border_control: IATA
    # delay code 86, not an extraordinary circumstance); within every limit the plan runs
    # automatically, with no passenger impact.
    flight_no, minutes, cause = CASE_1
    reading = {
        "kind": "flight_delay",
        "flight_no": flight_no,
        "delay_minutes": minutes,
        "cause": cause,
        "reading": "AR259 leaves about 20 minutes late: border-control queues hold its passengers.",
    }

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        tools.record_disruption(trace, reading)
        plan = tools.build_plan(trace, {"aircraft_option_id": "delay"})
        tools.submit_plan(trace, plan["plan_id"], "Within every domain's limits")
        return submitted(plan["plan_id"])

    case = run({"type": "message", "text": CASE_1_MESSAGE}, script)

    disruption = case["disruption"]
    assert (disruption["flight_no"], disruption["delay_minutes"], disruption["cause"]) == (
        CASE_1
    ), disruption
    plan = recommended(case)
    rebooking = plan["validation"]["rebooking"]
    assert plan["policy"]["route"] == "auto", plan["policy"]
    assert rebooking["disrupted_passengers"] == 0 and rebooking["rebooked_passengers"] == 0
    assert plan["validation"]["crew_plan"]["breaches_unresolved"] == 0
    assert plan["cost"]["total_eur"] <= COST_CAP_EUR and plan["cost"]["compensation_eur"] == 0
    # To the euro as before Act now was dropped; no passenger notice.
    assert figures(plan) == (356, 356, 0, 0, 0, 0, 0, 0, 0), figures(plan)
    assert "PAX-NOTICE" not in {row["id"] for row in plan["playbook"]}, plan["playbook"]
    kinds = assert_completed(case)
    assert {"retime_flights", "network_manager.fpl_delay", "airport_cdm.tobt_update"} <= set(
        kinds
    ), kinds
    # Owner: the reservation system notifies passengers of the new time, not ARROW.
    assert "notify_passengers" not in kinds, kinds
    assert case["_cases"].get(case["trace_id"])["execution"]["approved_by"] == "policy"


def check_case_1_calls_land_at_their_recipients() -> None:
    # Native dispatch spec §3: ARROW records the delay in operations control, changes the
    # flight there, then tells EUROCONTROL in its own message; each call reaches its recipient's
    # interface and is confirmed there.
    case = run(delay(CASE_1), one_plan({"aircraft_option_id": "delay"}))

    execution = case["_cases"].get(case["trace_id"])["execution"]
    calls = [call for action in execution["actions"] for call in action["calls"]]
    assert [(c["recipient"], c["method"], c["path"]) for c in calls] == [
        ("Operations control", "POST", "/api/delays"),
        ("Operations control", "PATCH", "/api/flights/AR259"),
        ("EUROCONTROL Network Manager", "POST", "/nm/messages"),
        ("Lisbon airport (A-CDM)", "POST", "/acdm/flights/AR259/tobt"),
        ("Crew system", "POST", "/api/duty-changes"),
    ], calls
    assert calls[2]["sent"]["text"].startswith("(DLA-AR259-LPPT"), calls[2]
    assert all(call["status"] == "confirmed" for call in calls), calls
    flight = case["_operations"].gateway.store.flights["AR259"]
    published = datetime.fromisoformat(flight["published_std_local"])
    assert flight["std_local"] == (published + timedelta(minutes=CASE_1[1])).isoformat(), flight
    # Final review: the case's timeline says what each party did, not its reply code.
    events = case["_cases"].get(case["trace_id"])["events"]
    lines = [event["message"] for event in events if event["event"] == "action_recorded"]
    leaves = datetime.fromisoformat(flight["std_local"])
    assert "EUROCONTROL accepted the delay message for AR259" in lines, lines
    assert f"Lisbon airport accepted the target off-block time {leaves:%H:%M} for AR259" in lines


def check_a_delay_past_midnight_is_accepted_by_eurocontrol() -> None:
    # Review: AR1133 (Lisbon-Faro, 23:45) leaves 30 minutes late, at 00:15 tomorrow in
    # Lisbon. OCC approves the night-quota slot; the delay message gives that off-block in UTC
    # (in winter, tomorrow's date) and the Network Manager still finds today's flight, so nothing
    # is left for OCC to chase.
    case = run(delay(("AR1133", 30, "staff_shortage")), one_plan({"aircraft_option_id": "delay"}))
    plan = recommended(case)
    assert plan["policy"]["route"] == "occ", plan["policy"]
    operations, cases = case["_operations"], case["_cases"]
    operations.record_decision(
        case["trace_id"],
        option_id=plan["option_id"],
        decision="approve",
        expected_version=case["version"],
        idempotency_key="decision-midnight-0001",
        actor_oid=OPERATOR,
    )

    Executor(cases, operations.gateway, poll_seconds=0).run(case["trace_id"])

    execution = cases.get(case["trace_id"])["execution"]
    [message] = [a for a in execution["actions"] if a["kind"] == "network_manager.fpl_delay"]
    [call] = message["calls"]
    leaves = datetime.combine(operations.gateway.store.day + timedelta(days=1), time(0, 15))
    utc = leaves.replace(tzinfo=LISBON).astimezone(UTC)
    assert call["sent"] == {"text": f"(DLA-AR1133-LPPT{utc:%H%M}-LPFR-DOF/{utc:%y%m%d})"}, call
    assert (call["status"], call["message"]) == ("confirmed", "ACK"), call
    assert execution["status"] == "completed", execution
    flight = operations.gateway.store.flights["AR1133"]
    assert flight["std_local"] == leaves.isoformat(), flight


class FixedClock(datetime):
    """A fixed morning in Lisbon: the day the systems, ARROW and the case run on."""

    at = datetime(2026, 7, 15, 9, 45, tzinfo=LISBON)

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> FixedClock:
        moment = cls.at.astimezone(tz)
        return cls.fromisoformat(moment.isoformat())


class SummerClock(FixedClock):
    """A July morning: Lisbon is one hour ahead of UTC."""


class StormDayClock(FixedClock):
    """The research TAF's day, 22 November 2026: Lisbon keeps UTC."""

    at = datetime(2026, 11, 22, 9, 45, tzinfo=LISBON)


def check_a_summer_delay_message_reaches_eurocontrol_in_utc() -> None:
    # EUROCONTROL works in UTC. On a July day Lisbon is one hour ahead of UTC, so Case 1's delay
    # message gives AR259's new off-block an hour earlier, and the Network Manager's record of
    # the delay reads Lisbon time again (final review: a flow, not the internals).
    july = date(2026, 7, 15)
    with mock.patch("airline_systems.schedule.datetime", SummerClock):
        case = run(
            delay(CASE_1),
            one_plan({"aircraft_option_id": "delay"}),
            OperatingDayStore(build_operating_day(july)),
        )

    execution = case["_cases"].get(case["trace_id"])["execution"]
    assert execution["status"] == "completed", execution
    [message] = [a for a in execution["actions"] if a["kind"] == "network_manager.fpl_delay"]
    [call] = message["calls"]
    store = case["_operations"].gateway.store
    leaves = datetime.fromisoformat(store.flights["AR259"]["std_local"])
    utc = leaves - timedelta(hours=1)
    assert leaves.date() == july, leaves
    text = call["sent"]["text"]
    assert text.startswith(f"(DLA-AR259-LPPT{utc:%H%M}-"), text
    assert text.endswith(f"-DOF/{utc:%y%m%d})"), text
    assert (call["status"], call["message"]) == ("confirmed", "ACK"), call
    assert store.actions[call["call_id"]]["request"]["payload"]["eobt"] == leaves.isoformat()


def lateness(flight: dict[str, Any]) -> tuple[int, int]:
    """Minutes late leaving and arriving against the published timetable."""

    def minutes(now: str, published: str) -> int:
        span = datetime.fromisoformat(flight[now]) - datetime.fromisoformat(flight[published])
        return round(span.total_seconds() / 60)

    return minutes("std_local", "published_std_local"), minutes("sta_local", "published_sta_local")


def check_a_late_flight_makes_up_time_in_the_air_and_pays_for_the_fuel() -> None:
    # Owner: a late aircraft flies faster, burning more fuel, so less delay carries on.
    # Owner: only when the minutes are worth more than the fuel and its CO2 (cost index and time
    # value research §5): 70 min late, a minute is worth €1.74 to each of AR444's passengers.
    case = run(delay(CASE_2), one_plan({"aircraft_option_id": "delay"}))

    plan = recommended(case)
    late = CASE_2[1]
    flights = {flight["flight_no"]: flight for flight in plan["flights"]}
    leaves, lands = lateness(flights["AR444"])
    assert (leaves, lands) == (late, late - 6), (leaves, lands)
    later = [f for f in plan["flights"] if f["std_local"] > flights["AR444"]["std_local"]]
    assert later and lateness(later[0])[0] < late, [lateness(f) for f in later]
    airline = plan["cost"]["workings"]["airline"]
    fuel = {line["flight_no"]: line for line in airline["fuel"]}
    # AR444 HAM-LIS (174 min in the air, so 6 min at most) is inside the EU Emissions Trading
    # System: fuel at €85.40 a minute and CO2 allowances at €17.64. AR861 LIS-RAK is outside it.
    assert fuel["AR444"] == {
        "flight_no": "AR444",
        "made_up_minutes": 6,
        "made_up_possible": 6,
        "reason": "worth more than its fuel at 60–90 min late",
        "aircraft": "narrow",
        "fuel_eur": 512.4,
        "co2_eur": 105.84,
        "eur": 618.24,
    }, fuel["AR444"]
    assert fuel["AR861"]["co2_eur"] == 0 and fuel["AR861"]["fuel_eur"] > 0, fuel["AR861"]
    priced = sum(line["eur"] for line in airline["flights"]) + sum(fuel[f]["eur"] for f in fuel)
    assert plan["cost"]["airline_eur"] == round(priced), (plan["cost"]["airline_eur"], priced)


def check_a_short_delay_is_not_made_up_in_the_air() -> None:
    # Case 1's A330neo (Lisbon-Belém, outside the ETS) 20 min late: making up its 10 min would cost
    # about €1,351 in fuel net of crew time and be worth €874 to its passengers (cost index and time
    # value research §5), so it keeps its block time and burns no extra fuel. At 15-30 min late an
    # A321neo, or a narrow body that pays no CO2, can still be worth speeding up.
    case = run(delay(CASE_1), one_plan({"aircraft_option_id": "delay"}))

    plan = recommended(case)
    [reported] = [flight for flight in plan["flights"] if flight["flight_no"] == "AR259"]
    assert lateness(reported) == (20, 20), lateness(reported)
    assert plan["cost"]["workings"]["airline"]["fuel"] == []


def check_case_2_staff_shortage_60_min_rebooks_and_runs_automatically() -> None:
    # Owner: once the delay runs automatically, ARROW builds no cancellation beside it.
    refused: list[str] = []

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        plan = tools.build_plan(trace, {"aircraft_option_id": "delay"})
        try:
            tools.build_plan(trace, {"aircraft_option_id": "cancel"})
        except ToolRefused as error:
            refused.append(str(error))
        tools.submit_plan(trace, plan["plan_id"], "Within every domain's limits")
        return submitted(plan["plan_id"])

    case = run(delay(CASE_2), script)

    assert refused == [AUTOMATIC_BUILT], refused
    assert [plan["kind"] for plan in case["options"]] == ["delay"], case["options"]
    plan = recommended(case)
    rebooking = plan["validation"]["rebooking"]
    assert plan["policy"]["route"] == "auto", plan["policy"]
    assert 1 <= rebooking["rebooked_passengers"] <= REBOOK_LIMIT, rebooking
    assert plan["validation"]["crew_plan"]["breaches_unresolved"] == 0
    assert plan["cost"]["total_eur"] <= COST_CAP_EUR, plan["cost"]
    assert rebooking["transferred_passengers"] >= 1, rebooking
    # To the euro as before Act now was dropped; no passenger notice.
    assert figures(plan) == (11079, 6578, 3500, 95, 906, 11, 6, 5, 19), figures(plan)
    # Playbook Task 1: a plan that names no playbook actions shows them from what it does.
    shown = {row["id"]: row["status"] for row in plan["playbook"]}
    assert "PAX-NOTICE" not in shown, plan["playbook"]
    assert {key: shown[key] for key in ("RETIME", "REBOOK", "CAR", "SWAP-AC", "THIN")} == {
        "RETIME": "in_plan",
        "REBOOK": "in_plan",
        "CAR": "in_plan",
        "SWAP-AC": "not_needed",
        "THIN": "not_needed",
    }, plan["playbook"]
    assert {"rebook_passengers", "ground_handling.direct_transfer"} <= set(assert_completed(case))
    reviews = case["_cases"].get(case["trace_id"])["execution"].get("reviews") or []
    assert [r["passengers"] for r in reviews] == (
        [rebooking["unplaced_passengers"]] if rebooking["unplaced_passengers"] else []
    )


def check_case_3_aog_ranks_plans_for_occ() -> None:
    # Spec §5 objectives: legal and safe, protect connections, fewest passengers disrupted, then
    # lowest cost. Code ranks by cost; the orchestrator may recommend another plan with reasons.
    seen: dict[str, Any] = {}

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        offered = tools.gateway.options(event_of(tools, trace), 0)["options"]
        kinds = sorted({option["kind"] for option in offered} & set(allowed(tools, trace)))
        for kind in kinds:
            seen[kind] = tools.build_plan(trace, {"aircraft_option_id": kind})
        # The Passenger agent's EU261 exposure of the delay, asked at 0 minutes as the
        # orchestrator asks every domain agent.
        seen["exposure"] = tools.compensation_exposure(trace, "delay", 0)
        usable = [p for p in tools.store.get(trace)["options"] if p["policy"]["route"] != "blocked"]
        best = min(
            usable,
            key=lambda p: (
                p["validation"]["rebooking"]["disrupted_passengers"],
                p["cost"]["total_eur"],
            ),
        )
        disrupted = best["validation"]["rebooking"]["disrupted_passengers"]
        tools.submit_plan(
            trace, best["plan_id"], f"Fewest passengers disrupted ({disrupted}) of the plans"
        )
        return submitted(best["plan_id"])

    registration, minutes = CASE_3
    case = run(
        {
            "type": "aircraft_defect",
            "aircraft_reg": registration,
            "description": "Hydraulic leak",
            "unavailable_minutes": minutes,
        },
        script,
    )

    assert case["status"] == "assessment_ready" and "execution" not in case
    plans = {plan["kind"]: plan for plan in case["options"]}
    assert {"delay", "cancel"} <= plans.keys(), plans.keys()
    assert plans["delay"]["policy"]["route"] == plans["cancel"]["policy"]["route"] == "occ"
    # Slots says, from the plan's own flights, whether delay messages (DLA) are needed: the
    # delay moves CS-TQA's four departures over 15 min; the cancellation keeps AR131 and AR132
    # on time, so the Slot agent advises none and the orchestrator builds it without them.
    assert {kind: slot_check(plans[kind]) for kind in ("cancel", "delay")} == {
        "cancel": (False, 0),
        "delay": (True, 4),
    }
    assert plans["delay"]["adjustment_minutes"] == 15  # LIS capacity: Slots agreed +15 min
    # The exposure settles on the same +15 min as the plan, so the Passenger agent's figures
    # are the plan's, and it says which delay it priced.
    exposure, delayed = seen["exposure"], plans["delay"]["cost"]
    assert (
        exposure["priced_additional_delay_minutes"],
        exposure["compensation_eur"],
        exposure["care_eur"],
        exposure["rebooking_eur"],
    ) == (15, delayed["compensation_eur"], delayed["care_eur"], delayed["rebooking_eur"]), exposure
    # Playbook Task 3: maintenance's estimate is advice on every plan, never a change.
    defect = case["defect"]
    for plan in case["options"]:
        estimate = checklist_rows(plan)["MX-ETR"]
        assert (estimate["status"], estimate["facts"]) == (
            "advice",
            f"{defect['aircraft_reg']} back in service {defect['unavailable_until'][11:16]}"
            f" ({defect['unavailable_minutes']} min estimate)",
        ), estimate
    costs = [plan["cost"]["total_eur"] for plan in case["options"]]
    assert costs == sorted(costs), costs
    best = recommended(case)
    assert best["kind"] == "delay" and case["options"][0]["kind"] == "cancel"  # not cheapest
    assert best["recommendation_reason"].startswith("Fewest passengers disrupted")
    # Owner: "how do we come up with" a figure: each plan shows its workings, and they
    # add up to the parts OCC sees.
    for plan in case["options"]:
        cost, workings = plan["cost"], plan["cost"]["workings"]
        lines = workings["airline"]["flights"]
        assert abs(sum(line["eur"] for line in lines) - cost["airline_eur"]) <= len(lines)
        assert cost["compensation_eur"] == sum(
            group["eur_each"] * group["passengers"] for group in workings["compensation"]
        ), workings["compensation"]
        assert all(group["why"] for group in workings["compensation"] + workings["meals"])
        overnight = workings["overnight"] or {"passengers": 0, "meals_eur_each": 0, "rooms": 0}
        assert (
            cost["care"]["meals_eur"]
            == sum(group["eur_each"] * group["passengers"] for group in workings["meals"])
            + overnight["passengers"] * overnight["meals_eur_each"]
        )
        assert cost["care"]["hotel_eur"] == overnight["rooms"] * overnight.get("room_eur", 0)
        # Owner: "rebooking all the 324 passengers would be insanely more expensive no?"
        # A fourth part prices rebooking, and the parts add up to the total (cancellation and
        # rebooking cost research §5).
        parts = ("airline_eur", "compensation_eur", "care_eur", "rebooking_eur")
        assert cost["total_eur"] == sum(cost[part] for part in parts), cost
        assert cost["rebooking_eur"] == sum(
            group["eur_each"] * group["passengers"] for group in workings["rebooking"]
        ), workings["rebooking"]
        assert all(group["why"] for group in workings["rebooking"]), workings["rebooking"]
        # Who a plan affects, beside its cost: who is owed compensation, who is fed.
        assert cost["owed_compensation_passengers"] == sum(
            group["passengers"] for group in workings["compensation"]
        )
        assert cost["meals_passengers"] == sum(group["passengers"] for group in workings["meals"])
    # Case 3 on the generated day (cancellation and rebooking cost research §4-§5):
    # cancelling places 239 passengers on our flights and leaves 85 for Passenger Services; the
    # delay 38 and 39.
    rebooked = {
        kind: sorted(
            (g["eur_each"], g["passengers"]) for g in plans[kind]["cost"]["workings"]["rebooking"]
        )
        for kind in ("cancel", "delay")
    }
    assert rebooked["cancel"] == [(11, 224), (13, 15), (150, 68), (168, 17)], rebooked
    assert rebooked["delay"] == [(11, 30), (13, 8), (150, 16), (168, 23)], rebooked
    headline = {
        kind: tuple(
            plans[kind]["cost"][key]
            for key in (
                "rebooking_eur",
                "total_eur",
                "owed_compensation_passengers",
                "meals_passengers",
            )
        )
        for kind in ("cancel", "delay")
    }
    assert headline == {"cancel": (15715, 108637, 232, 324), "delay": (6698, 117697, 322, 617)}
    # To the euro as before Act now was dropped; no passenger notice.
    assert {kind: figures(plans[kind]) for kind in ("cancel", "delay")} == {
        "cancel": (108637, 26640, 60450, 5832, 15715, 324, 239, 85, 0),
        "delay": (117697, 13424, 85300, 12275, 6698, 77, 38, 39, 35),
    }
    assert not any(
        row["id"] == "PAX-NOTICE" for plan in case["options"] for row in plan["playbook"]
    )
    # The orchestrator sees both headline numbers, so its reason can cite them.
    for kind in ("cancel", "delay"):
        summary, cost = seen[kind], plans[kind]["cost"]
        assert summary["passengers_owed_compensation"] == cost["owed_compensation_passengers"]
        assert summary["cost"]["rebooking_eur"] == cost["rebooking_eur"], summary
    cancelled = [line for line in plans["cancel"]["cost"]["workings"]["airline"]["flights"]]
    assert all(line.get("cancelled") for line in cancelled), cancelled
    case["_operations"].record_decision(
        case["trace_id"],
        option_id=best["option_id"],
        decision="approve",
        expected_version=case["version"],
        idempotency_key="decision-case-3-0001",
        actor_oid=OPERATOR,
    )
    case["_operations"].execute(case["trace_id"])
    assert_completed(case)


def standby_ids(tools: ArrowTools, trace: str, kind: str) -> list[str]:
    """As the Crew agent recommends them: one offered standby crew per breached duty, in order."""
    offered = tools.gateway.reserve_options(event_of(tools, trace), kind, 0)["options"]
    return [option["reserve_option_id"] for option in offered]


def check_case_4_crew_limit_uses_a_reserve_and_runs_automatically() -> None:
    seen: dict[str, Any] = {}

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        seen["without"] = tools.build_plan(trace, {"aircraft_option_id": "delay"})
        plan = tools.build_plan(
            trace,
            {
                "aircraft_option_id": "delay",
                "reserve_option_ids": standby_ids(tools, trace, "delay"),
            },
        )
        tools.submit_plan(trace, plan["plan_id"], "A standby crew keeps the duty legal")
        return submitted(plan["plan_id"])

    case = run(delay(CASE_4), script)

    assert seen["without"]["route"] == "blocked", seen["without"]
    assert CREW_LIMIT in seen["without"]["reasons"]
    plan = recommended(case)
    rebooking = plan["validation"]["rebooking"]
    assert plan["policy"]["route"] == "auto", plan["policy"]
    assert [crew["reserve_option_id"] for crew in plan["crew_reserves"]] == ["R1"], plan
    # Playbook Task 3: the standby crew is in the plan's checklist with its call, report and
    # flights (the head start); no round trip is thinned.
    [called] = plan["crew_reserves"]
    shown = checklist_rows(plan)
    assert (shown["STANDBY"]["status"], shown["THIN"]["status"]) == ("in_plan", "not_needed")
    assert shown["STANDBY"]["facts"] == (
        f"R1 called {called['called_local'][11:16]} · reports {called['report_local'][11:16]}"
        f" · {', '.join(called['flights_for_reserve'])}"
    ), shown["STANDBY"]
    assert "PAX-NOTICE" not in shown, plan["playbook"]
    # To the euro as before Act now was dropped.
    assert figures(plan) == (3770, 1334, 1000, 836, 600, 4, 0, 4, 11), figures(plan)
    assert plan["validation"]["crew_plan"]["breaches_unresolved"] == 0
    # D108: cars keep the connections AR882's delay shortens, so it may rebook no one.
    assert rebooking["rebooked_passengers"] <= REBOOK_LIMIT, rebooking
    assert plan["cost"]["total_eur"] <= COST_CAP_EUR, plan["cost"]
    kinds = assert_completed(case)
    assert {"assign_reserve_crew", "network_manager.fpl_delay"} <= set(kinds), kinds
    actions = case["_cases"].get(case["trace_id"])["execution"]["actions"]
    [crew] = [action for action in actions if action["kind"] == "assign_reserve_crew"]
    assert crew["summary"] == "Assign standby crew R1", crew


def check_case_4_occ_may_choose_the_automatic_plan_over_the_submitted_one() -> None:
    # The orchestrator submits a plan that needs OCC (it declines rebooking); the reserve plan it
    # also built is within every limit. OCC decides between every unblocked plan, and a plan a
    # person approved is never presented as having run automatically.
    plans: dict[str, Any] = {}

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        levers = {
            "aircraft_option_id": "delay",
            "reserve_option_ids": standby_ids(tools, trace, "delay"),
        }
        plans["occ"] = tools.build_plan(trace, {**levers, "rebook": False})
        plans["reserve"] = tools.build_plan(trace, levers)
        result = tools.submit_plan(trace, plans["occ"]["plan_id"], "Hold the passengers")
        assert result["route"] == "occ", result
        return submitted(plans["occ"]["plan_id"])

    case = run(delay(CASE_4), script)

    assert "execution" not in case and plans["reserve"]["route"] == "auto"
    assert all(plan["requires_duty_manager"] for plan in case["options"]), [
        (plan["policy"]["route"], plan["requires_duty_manager"]) for plan in case["options"]
    ]
    operations, cases = case["_operations"], case["_cases"]
    operations.record_decision(
        case["trace_id"],
        option_id=plans["reserve"]["plan_id"],
        decision="approve",
        expected_version=case["version"],
        idempotency_key="decision-approve-auto-0001",
        actor_oid=OPERATOR,
    )
    operations.execute(case["trace_id"])

    done = cases.get(case["trace_id"])
    assert done["execution"]["approved_by"] == OPERATOR
    assert "assign_reserve_crew" in assert_completed({**case, "_cases": cases})
    started = [e for e in done["events"] if e["event"] == "execution_started"]
    assert started and "OCC informed" not in started[0]["message"], started


def check_the_last_standby_crew_of_a_base_goes_to_occ() -> None:
    # Task 1: AR689's delay breaks D-PDL-002 (731 of 720 min). Ponta Delgada's one
    # standby crew (home, 08:00-16:00), called by 15:54, reports 16:54 and takes AR941-AR942.
    # It is the base's last complete standby crew today, so the plan goes to OCC (Standby crews).
    # Live run 4: the orchestrator then built both cancellations with delay messages off.
    # Cancelling AR689-AR690 moves no departure; cancelling the last round trip flies AR689 and
    # AR690 late, so ARROW sets delay messages on from its flights and still builds it.
    built: dict[str, Any] = {}

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        levers = {
            "aircraft_option_id": "delay",
            "reserve_option_ids": standby_ids(tools, trace, "delay"),
        }
        plan = tools.build_plan(trace, levers)
        for option in ("cancel", "cancel-last"):
            built[option] = tools.build_plan(
                trace, {"aircraft_option_id": option, "delay_messages": False}
            )
        tools.submit_plan(trace, plan["plan_id"], "A standby crew keeps the duty legal")
        return submitted(plan["plan_id"])

    case = run(
        {**delay(PDL_BREACH), "cause": "late_inbound", "description": "Late inbound aircraft"},
        script,
    )

    assert len(case["options"]) == 3, [plan["aircraft_option_id"] for plan in case["options"]]
    messages = {
        plan["aircraft_option_id"]: (
            plan["levers"]["delay_messages"],
            plan["delay_messages_required"],
            plan["departures_moved_over_15_min"],
        )
        for plan in case["options"]
    }
    assert messages == {
        "delay": (True, True, 4),
        "cancel": (False, False, 0),
        "cancel-last": (True, True, 2),
    }, messages
    assert built["cancel-last"]["delay_messages_required"] is True, built["cancel-last"]
    plan = recommended(case)
    assert plan["policy"]["route"] == "occ", plan["policy"]
    assert plan["policy"]["reasons"] == [
        "OCC decides",
        "uses the last standby crew at Ponta Delgada A320",
    ], plan["policy"]["reasons"]
    [crew] = plan["crew_reserves"]
    assert (
        crew["reserve_option_id"],
        crew["standby_type"],
        crew["called_local"][11:16],
        crew["report_local"][11:16],
        crew["flights_for_reserve"],
    ) == ("R1", "home", "15:54", "16:54", ["AR941", "AR942"]), crew
    checks = {check["key"]: check for check in plan["policy"]["checks"]}
    assert (checks["crew"]["result"], checks["standby"]["result"]) == ("pass", "occ"), checks
    assert "R1 call by 15:54, report 16:54" in checks["standby"]["input"], checks["standby"]
    assert plan["validation"]["crew_plan"]["breaches_unresolved"] == 0
    assert "execution" not in case


def check_a_plan_stored_with_one_reserve_crew_still_reads_and_runs() -> None:
    # Plans built by earlier versions name one reserve crew (levers.reserve_option_id, crew_reserve)
    # and their crew plan counts no standby crews left. Such a case still reads on its card, in the
    # trail and in the Case Assistant's digest, and OCC can still approve and run it.
    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        levers = {
            "aircraft_option_id": "delay",
            "reserve_option_ids": standby_ids(tools, trace, "delay"),
        }
        tools.build_plan(trace, levers)
        return {"outcome": "no_plan", "summary": "Left to OCC", "submitted_plan_id": None}

    case = run(delay(CASE_4), script)
    operations, cases, trace = case["_operations"], case["_cases"], case["trace_id"]
    [plan] = cases.docs[trace]["options"]
    [reserve] = plan.pop("crew_reserves")
    plan["crew_reserve"] = {
        key: value
        for key, value in reserve.items()
        if key
        not in ("base", "qualification", "standby_type", "called_local", "earliest_departure_local")
    }
    plan["levers"] = {
        "aircraft_option_id": "delay",
        "reserve_option_id": "R1",
        "rebook": True,
        "delay_messages": True,
    }
    for key in ("reserve_option_ids", "standby_left"):
        plan["validation"]["crew_plan"].pop(key)
    plan["policy"]["checks"] = [c for c in plan["policy"]["checks"] if c["key"] != "standby"]
    # Nor did they record a playbook (Task 1).
    for key in ("playbook", "playbook_version", "playbook_actions"):
        plan.pop(key)
    stored = cases.get(trace)
    report = reserve["report_local"][11:16]

    assert case_card(stored)["impact"]["route"] == "auto", case_card(stored)["impact"]
    [shown] = case_digest(stored)["plans"]
    assert shown["levers"] == {
        "aircraft_option_id": "delay",
        "reserve_option_ids": ["R1"],
        "rebook": True,
        "delay_messages": True,
        "playbook_actions": [],
        "extra_delay_minutes": 0,
    }, shown["levers"]
    assert f"standby crew R1 ({len(reserve['crew'])} crew): report {report}" in shown["crew"], (
        shown["crew"]
    )
    [view] = build_trail(stored, None, lambda name: None, None)["plans"]
    [crew_check] = [check for check in view["checks"] if check["key"] == "crew"]
    assert crew_check["result"] == "pass", view["checks"]
    # Its playbook checklist is read from what it does, its one reserve crew included.
    statuses = {row["id"]: row["status"] for row in view["playbook"]}
    assert (statuses["RETIME"], statuses["STANDBY"], statuses["THIN"]) == (
        "in_plan",
        "in_plan",
        "not_needed",
    ), view["playbook"]
    operations.record_decision(
        trace,
        option_id=plan["option_id"],
        decision="approve",
        expected_version=stored["version"],
        idempotency_key="decision-one-reserve-0001",
        actor_oid=OPERATOR,
    )
    operations.execute(trace)

    assert "assign_reserve_crew" in assert_completed({**case, "_cases": cases})
    actions = cases.get(trace)["execution"]["actions"]
    [crew] = [action for action in actions if action["kind"] == "assign_reserve_crew"]
    [call] = crew["calls"]
    assert (crew["summary"], call["sent"]["reserve_option_ids"]) == (
        "Assign standby crew R1",
        ["R1"],
    ), crew


def escalation(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
    """The orchestrator's order (prompt): build the offered options as OCC escalates
    (delay, swap, cancel), stop once a plan runs automatically, then recommend by the objectives
    (not blocked, fewest passengers disrupted, lowest cost)."""
    offered = {
        option["kind"] for option in tools.gateway.options(event_of(tools, trace), 0)["options"]
    }
    for kind in ("delay", "swap", "cancel"):
        if kind in offered and kind in allowed(tools, trace):
            if tools.build_plan(trace, {"aircraft_option_id": kind})["route"] == "auto":
                break
    usable = [p for p in tools.store.get(trace)["options"] if p["policy"]["route"] != "blocked"]
    best = min(
        usable,
        key=lambda p: (
            p["validation"]["rebooking"]["disrupted_passengers"],
            p["cost"]["total_eur"],
        ),
    )
    insist(tools, trace, best["plan_id"], "The only legal plan")
    return submitted(best["plan_id"])


def check_crew_limit_without_a_reserve_offers_occ_the_cancellation() -> None:
    # Owner: "can't we solve it with the agent ... or recommend to cancel last flight due
    # to crew". The delay breaks the crew's flight-time limit and no reserve is free, so the
    # delay is blocked; cancelling the round trip puts the rest of the rotation, and the crew's
    # duty, back on time. OCC decides the cancellation.
    case = run(
        {**delay(PDL_BREACH), "cause": "late_inbound", "description": "Late inbound aircraft"},
        escalation,
    )

    plans = {plan["kind"]: plan for plan in case["options"]}
    assert set(plans) == {"delay", "cancel"}, plans.keys()
    assert plans["delay"]["policy"]["route"] == "blocked", plans["delay"]["policy"]
    assert CREW_LIMIT in plans["delay"]["policy"]["reasons"]
    # The delay's flights speed up but, between the Azores, Madeira and mainland Portugal, need
    # no EU emission allowances until 2030: their fuel is priced without CO2.
    fuel = plans["delay"]["cost"]["workings"]["airline"]["fuel"]
    assert {line["flight_no"] for line in fuel} == {"AR689", "AR690", "AR941", "AR942"}, fuel
    assert all(line["fuel_eur"] > 0 and line["co2_eur"] == 0 for line in fuel), fuel
    # Owner: "14minutes faster only, that's weird": the minutes each flight makes up,
    # and why no more (3.5% of its time in the air; an A320 has about 5% speed in hand).
    airline = plans["delay"]["cost"]["workings"]["airline"]
    assert airline["fuel_summary"] == (
        "Fly faster: AR689 3 min, AR690 3 min, AR941 4 min, AR942 4 min (14 min) · fuel €1,196"
    ), airline["fuel_summary"]
    assert airline["fuel_basis"] == (
        "3.5% of each flight's time in the air, where the minutes are worth their fuel. An A320"
        " cruises at Mach 0.78 and may not exceed 0.82: about 5% faster, in cruise only."
    ), airline["fuel_basis"]
    cancel = recommended(case)
    assert cancel["kind"] == "cancel" and cancel["policy"]["route"] == "occ", cancel["policy"]
    flights = {f["flight_no"]: f for f in cancel["flights"]}
    assert [n for n, f in flights.items() if f["status"] == "cancelled"] == ["AR689", "AR690"]
    originals = {f["flight_no"]: f for f in case["rotation"]["flights"]}
    for kept in ("AR941", "AR942"):
        assert flights[kept]["std_local"] == originals[kept]["std_local"], flights[kept]
    assert cancel["validation"]["crew_plan"]["breaches_unresolved"] == 0
    assert "manual_intervention" not in case and case["status"] == "assessment_ready"
    # Owner: "or recommend to cancel last flight due to crew". Schedule also offers
    # cancelling the aircraft's last round trip; in the evening it strands passengers overnight,
    # so OCC sees why the earlier cancel is the better one.
    offered = case["_operations"].gateway.options(case_ref(case), 0)
    [last] = [option for option in offered["options"] if option["option_id"] == "cancel-last"]
    assert last["kind"] == "cancel"
    assert [f["flight_no"] for f in last["flights"] if f["status"] == "cancelled"] == [
        "AR941",
        "AR942",
    ], last["flights"]
    assert any("ends the aircraft's day" in item["why"] for item in offered["checked"])

    case["_operations"].record_decision(
        case["trace_id"],
        option_id=cancel["option_id"],
        decision="approve",
        expected_version=case["version"],
        idempotency_key="decision-case-4-cancel-01",
        actor_oid=OPERATOR,
    )
    case["_operations"].execute(case["trace_id"])
    assert "cancel_flights" in assert_completed(case)


def check_a_swap_the_crew_cannot_reach_is_blocked() -> None:
    # Review: Schedule finds CS-TKH free for AR965-AR966, but the crew rostered on them
    # lands from the late AR608 after AR965 would leave. The swap is blocked for that reason,
    # not as a flight-time breach, and the other plans stay open to OCC.
    seen: dict[str, Any] = {}

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        offered = tools.gateway.options(event_of(tools, trace), 0)
        seen["checked"] = offered["checked"]
        seen["swap"] = tools.build_plan(trace, {"aircraft_option_id": "swap"})
        cancel = tools.build_plan(trace, {"aircraft_option_id": "cancel"})
        tools.submit_plan(trace, cancel["plan_id"], "The swap's crew cannot connect")
        return submitted(cancel["plan_id"])

    case = run(delay(("AR607", 70, "staff_shortage")), script)

    [swap_check] = [item for item in seen["checked"] if item["kind"] == "swap"]
    assert swap_check["available"] and "AR965" in swap_check["why"], swap_check
    assert seen["swap"]["route"] == "blocked", seen["swap"]
    assert CREW_CONNECTION_REASON in seen["swap"]["reasons"], seen["swap"]["reasons"]
    assert CREW_LIMIT not in seen["swap"]["reasons"], seen["swap"]["reasons"]
    swap = next(plan for plan in case["options"] if plan["kind"] == "swap")
    [crew] = [check for check in swap["policy"]["checks"] if check["key"] == "crew"]
    assert crew["input"].startswith("cannot connect: AR608 lands at LIS"), crew
    assert "manual_intervention" not in case and case["status"] == "assessment_ready"


def check_crew_limit_without_a_reserve_needs_manual_intervention() -> None:
    # Owner: without legal crew the aircraft cannot fly the rotation; this is flagged to
    # OCC for manual intervention, never offered as a plan to approve.
    seen: dict[str, Any] = {}

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        plan = tools.build_plan(trace, {"aircraft_option_id": "delay"})
        seen["submit"] = tools.submit_plan(trace, plan["plan_id"], "Only option")
        seen["plan_id"] = plan["plan_id"]
        return {"outcome": "no_plan", "summary": "No reserve crew", "submitted_plan_id": None}

    case = run(
        {**delay(PDL_BREACH), "cause": "late_inbound", "description": "Late inbound aircraft"},
        script,
    )

    assert seen["submit"]["route"] == "blocked", seen["submit"]
    assert CREW_LIMIT in seen["submit"]["reasons"]
    assert case["status"] == "assessment_ready" and "execution" not in case, case["status"]
    assert case["manual_intervention"]["reason"] == CREW_LIMIT
    assert case["stage"].startswith("Manual intervention"), case["stage"]
    assert not any(plan["requires_duty_manager"] for plan in case["options"])
    try:
        case["_operations"].record_decision(
            case["trace_id"],
            option_id=seen["plan_id"],
            decision="approve",
            expected_version=case["version"],
            idempotency_key="decision-approve-crew-0001",
            actor_oid=OPERATOR,
        )
    except DecisionConflict:
        pass
    else:
        raise AssertionError("OCC could approve a plan whose crew cannot legally operate")
    assert "execution" not in case["_cases"].get(case["trace_id"])


def taf(day: date, airport: str = "LPPT") -> str:
    """Case 5's amended TAF (the Test Bench's, src/testbench/presets.py), written for ``day``: a
    westerly gale gusting 35 kt across Lisbon's runway 02/20 from 16:00 to 18:00 UTC, with
    showers and no thunderstorm."""
    today, tomorrow = f"{day.day:02d}", f"{(day + timedelta(days=1)).day:02d}"
    return (
        f"TAF AMD {airport} {today}0920Z {today}10/{tomorrow}12 19012KT 9999 SCT020 BKN040"
        f" BECMG {today}13/{today}15 21018G28KT 6000 RA BKN012 TEMPO {today}16/{today}18"
        " 27022G35KT 6000 SHRA BKN015 BECMG"
        f" {today}18/{today}20 30015KT 9999 NSW SCT020"
    )


def storm_reading(day: date, **change: str) -> dict[str, Any]:
    """The Detection agent's reading of the TAF: tokens as written; code converts them and
    computes the crosswind."""
    return {
        "kind": "weather",
        "airport": "LPPT",
        "taf_window": f"{day.day:02d}16/{day.day:02d}18",
        "hazard": "crosswind",
        "reading": "Westerly gale gusting 35 kt across the runway at Lisbon from 16:00 to 18:00"
        " UTC.",
        **change,
    }


STORM_DAY = date(2026, 11, 22)
CREW_AFTER_RESERVE = "flight-time limit still exceeded after the standby crew"  # Task 2: "reserve"
CREW_AFTER_STANDBY_CREWS = "flight-time limit still exceeded after the standby crews"
WEATHER_REASON = "weather: a forecast moves flights across the network"
WEATHER_NOTE = (
    "Weather is an extraordinary circumstance: EU261 compensation is not owed. Care and"
    " rebooking are."
)
# Owner: "EU compesnation is not paid when the event is not an airline mistake".
NOT_OWED_REASON = "Extraordinary circumstances: weather. Care and rebooking are still owed."
# What the orchestrator names for a storm (after Act now was dropped): the actions
# read from what a plan does (RETIME, GROUND-HOLD, THIN, STANDBY, REBOOK, CAR) need no naming.
STORM_PLAYBOOK = ["PAX-NOTICE", "FUEL-FLAG", "ALT-FLAG"]
# The actions only Act now used, removed with it (owner: "the act now is a bit useless").
REMOVED = {"CREW-PREPOS", "CREW-NOTICE", "HOTEL-HOLD", "SLOT-SWAP"}


def storm_levers(
    tools: ArrowTools, trace: str, option_id: str, actions: list[str]
) -> dict[str, Any]:
    """A storm plan as the orchestrator builds it: an aircraft option, the standby crews Crew
    offers for it and the playbook actions the plan names."""
    return {
        "aircraft_option_id": option_id,
        "reserve_option_ids": standby_ids(tools, trace, option_id),
        "playbook_actions": actions,
    }


def checklist_rows(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["id"]: row for row in plan["playbook"]}


def weather_update(
    status: str,
    *,
    window: str | None = None,
    rate: int | None = 24,
    hazard: str = "crosswind",
    day: date = STORM_DAY,
) -> dict[str, Any]:
    """The Test Bench's weather update (src/testbench/presets.py), as a CloudEvent: a METAR that
    confirms the forecast, or an amended TAF that drops it."""
    today = f"{day.day:02d}"
    description = (
        f"METAR LPPT {today}1420Z 26021G34KT 9999 -SHRA FEW012 BKN025 15/11 Q1002"
        " TEMPO 27022G35KT 6000 SHRA"
        if status == "confirmed"
        else f"TAF AMD LPPT {today}1310Z {today}13/{today}12 22015G25KT 8000 -RA BKN015 NSW"
    )
    data: dict[str, Any] = {
        "airport": "LPPT",
        "taf_window": window or f"{today}16/{today}18",
        "hazard": hazard,
        "status": status,
        "description": description,
    }
    if status == "confirmed" and rate is not None:
        data["rate_per_hour"] = rate
    return {**cloud_event("weather", data), "type": "com.arrow.weather.update"}


def deliver(operations: Any, cases: Any, body: dict[str, Any], sequence: int) -> str:
    """One message through ARROW's real intake, as from its topic subscription: a disruption
    event's case is assessed at once."""

    def wake(trace: str, at: Any = None) -> None:
        if at is None:
            operations.run(trace)

    intake = Intake(operations, cases, Rejections(), OPERATOR, wake)
    message = json.dumps(body).encode()
    return intake.handle(Delivery(message, None, f"w{sequence}", sequence, None))


def storm_case() -> tuple[Any, Any, Any, OperatingDayStore, str]:
    """Case 5 assessed as the orchestrator builds it: the reduced rate (recommended) and the
    Faro round trips cancelled, each with its standby crews. Call under StormDayClock."""

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        tools.record_disruption(trace, storm_reading(STORM_DAY))
        built = {
            kind: tools.build_plan(trace, storm_levers(tools, trace, kind, STORM_PLAYBOOK))
            for kind in ("delay", "cancel")
        }
        reason = "No passenger loses a flight; two standby crews keep every duty legal"
        tools.submit_plan(trace, built["delay"]["plan_id"], reason)
        return submitted(built["delay"]["plan_id"])

    store = OperatingDayStore(build_operating_day(STORM_DAY))
    operations, cases, gateway = operate(store, script)
    source = {"kind": "event", "system": "IPMA (weather)"}
    message = {"type": "message", "text": taf(STORM_DAY)}
    trace = operations.open(message, OPERATOR, source=source)["trace_id"]
    operations.run(trace)
    return operations, cases, gateway, store, trace


def unchanged(store: OperatingDayStore, case: dict[str, Any]) -> bool:
    """Every flight the storm case read still leaves when and as it did: nothing reached
    Schedule."""
    return all(
        (store.flights[f["flight_no"]]["std_local"], store.flights[f["flight_no"]]["status"])
        == (f["std_local"], f["status"])
        for f in case["affected_flights"]
    )


def approve_storm(operations: Any, cases: Any, trace: str, kind: str = "delay") -> dict[str, Any]:
    """OCC approves the storm's plan of this kind once; ARROW carries out what it may now."""
    case = cases.get(trace)
    [plan] = [plan for plan in case["options"] if plan["kind"] == kind]
    operations.record_decision(
        trace,
        option_id=plan["option_id"],
        decision="approve",
        expected_version=case["version"],
        idempotency_key=f"decision-storm-{kind}",
        actor_oid=OPERATOR,
    )
    operations.execute(trace)
    return plan


def check_case_5_a_crosswind_read_from_a_taf_waits_for_occ() -> None:
    # Owner: "build in TAF ... as case 5"; then "strong crosswinds", as Lisbon has one
    # runway, which a crosswind slows and never closes. One case covers the whole forecast:
    # code computes 33 kt across runway 02/20 from the TEMPO group's 27022G35KT, and Schedule
    # holds Lisbon to 24 movements an hour in the window (60% of 40). Delaying breaks two crew
    # duties; since Task 2 a standby crew takes each (R1 home, reports 13:00, AR827-AR828; R2
    # reports 14:40, AR881-AR882), so the delay is legal and two of Lisbon's four standby crews
    # stay (was: blocked, one reserve). Cancelling the Faro round trips loses 655 passengers
    # their flight; the delay none, so it is recommended, and a weather plan is OCC's. Approved,
    # it runs across the aircraft the forecast moves, with one crew write calling both standby
    # crews.
    # Owner: "make sure ... we have a good story/ case for the storm as well".
    # ARROW acts at the forecast: both plans tell their passengers the new times (PAX-NOTICE)
    # and carry dispatch's advice, and each standby crew shows when it is called (the head start).
    seen: dict[str, Any] = {}

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        recorded = tools.record_disruption(trace, storm_reading(STORM_DAY))
        if not recorded["recorded"]:
            return {"outcome": "no_plan", "summary": recorded["message"], "submitted_plan_id": None}
        seen["recorded"] = recorded
        seen["playbook"] = tools.get_playbook(trace)
        for kind in ("delay", "cancel"):
            # Live run 4: built with delay messages off, the cancellation (which sequences the
            # rest at the reduced rate) is still built; ARROW sets them on from its flights.
            levers = storm_levers(tools, trace, kind, STORM_PLAYBOOK)
            seen[kind] = tools.build_plan(trace, {**levers, "delay_messages": kind == "delay"})
        # The Passenger agent's EU261 exposure of the cancellation, by the code that prices it.
        seen["exposure"] = tools.compensation_exposure(trace, "cancel", 0)
        reason = "No passenger loses a flight; two standby crews keep every duty legal"
        tools.submit_plan(trace, seen["delay"]["plan_id"], reason)
        return submitted(seen["delay"]["plan_id"])

    message = {"type": "message", "text": taf(STORM_DAY)}
    with mock.patch("airline_systems.schedule.datetime", StormDayClock):
        store = OperatingDayStore(build_operating_day(STORM_DAY))
        operations, cases, gateway = operate(store, script)
        source = {"kind": "event", "system": "IPMA (weather)"}
        trace = operations.open(message, OPERATOR, source=source)["trace_id"]
        operations.run(trace)

        case = cases.get(trace)
        recorded = seen["recorded"]
        assert seen["playbook"]["allowed_aircraft_kinds"] == ["cancel", "delay"], seen["playbook"]
        # Only the forecast's own fields: weather names no aircraft and no flight.
        assert recorded["disruption"]["type"] == "weather", recorded["disruption"]
        assert not {"aircraft_reg", "flight_no"} & set(recorded["disruption"]), recorded
        storm = case["disruption"]
        assert (storm["type"], storm["cause"], storm["airport"], storm["hazard"]) == (
            "weather",
            "weather",
            "LIS",
            "crosswind",
        ), storm
        assert (storm["window_start"], storm["window_end"], storm["hourly_rate"]) == (
            "16:00",
            "18:00",
            24,
        ), storm
        # Code, never the model, computed the crosswind: 35 kt × sin(270° − 200°) = 33 kt.
        assert (storm["wind"], storm["crosswind_kt"], storm["runway"]) == (
            "27022G35KT",
            33,
            "02/20",
        ), storm
        assert recorded["disruption"]["crosswind_kt"] == 33, recorded["disruption"]
        # The runway is never closed: its rate is reduced, and the plan OCC is offered first
        # cancels nothing.
        assert 0 < storm["hourly_rate"] < storm["normal_rate"] == 40, storm
        assert "operations continue with care" in storm["rate_basis"], storm["rate_basis"]
        assert storm["message"] == message["text"] and "rotation" not in case
        title = (
            "Strong crosswind at Lisbon · 16:00–18:00 · gusts 33 kt across runway 02/20"
            " · 24 movements an hour (normally 40)"
        )
        assert (case_card(case)["headline"], storm["title"]) == (title, title), storm
        assert case["status"] == "assessment_ready" and "execution" not in case, case["stage"]
        plans = {plan["kind"]: plan for plan in case["options"]}
        assert set(plans) == {"delay", "cancel"}, plans.keys()
        delay_plan, cancel = plans["delay"], plans["cancel"]
        assert (
            cancel["levers"]["delay_messages"],
            cancel["delay_messages_required"],
            cancel["departures_moved_over_15_min"],
        ) == (True, True, 68), cancel["levers"]
        assert not any(f["status"] == "cancelled" for f in delay_plan["flights"]), delay_plan
        # Both weather plans move departures over 15 min (the cancellation sequences the rest at
        # the reduced rate), so Slots says each needs delay messages (DLA).
        assert {kind: slot_check(plan) for kind, plan in plans.items()} == {
            "delay": (True, 72),
            "cancel": (True, 68),
        }
        # Task 2: legal with two standby crews (was blocked after one reserve) and recommended.
        assert delay_plan["recommended"] and delay_plan["policy"]["route"] == "occ", delay_plan[
            "policy"
        ]
        assert WEATHER_REASON in delay_plan["policy"]["reasons"]
        assert not any("standby" in reason for reason in delay_plan["policy"]["reasons"])
        assert [
            (crew["reserve_option_id"], crew["duty_id"], crew["flights_for_reserve"])
            for crew in delay_plan["crew_reserves"]
        ] == [("R1", "D-LIS-146", ["AR827", "AR828"]), ("R2", "D-LIS-164", ["AR881", "AR882"])]
        crew_plan = delay_plan["validation"]["crew_plan"]
        assert (crew_plan["breaches_unresolved"], crew_plan["reserve_option_ids"]) == (
            0,
            ["R1", "R2"],
        ), crew_plan
        left = {(e["base"], e["qualification"]): e["crews"] for e in crew_plan["standby_left"]}
        assert left[("LIS", "A320F")] == 2, left
        assert cancel["policy"]["route"] == "occ", cancel["policy"]
        assert sorted(f["flight_no"] for f in cancel["flights"] if f["status"] == "cancelled") == [
            "AR131",
            "AR132",
            "AR881",
            "AR882",
        ]
        # Code read the original of every flight a plan moves, on whichever aircraft it flies.
        originals = {flight["flight_no"]: flight for flight in case["affected_flights"]}
        assert {f["flight_no"] for p in plans.values() for f in p["flights"]} <= originals.keys()
        aircraft = {originals[f["flight_no"]]["aircraft_reg"] for f in cancel["flights"]}
        assert len(aircraft) >= 2, aircraft
        cost, workings = cancel["cost"], cancel["cost"]["workings"]
        parts = ("airline_eur", "compensation_eur", "care_eur", "rebooking_eur")
        assert cost["total_eur"] == sum(cost[part] for part in parts), cost
        assert workings["note"] == WEATHER_NOTE, workings.get("note")
        # The Case Assistant names the storm's plans as their cards do, never "Delay the rotation".
        named = {p["number"]: p["title"] for p in case_digest(case)["plans"]}
        assert sorted(named.values()) == [
            "Accept the reduced Lisbon rate",
            "Cancel AR131 + AR132 + AR881 + AR882",
        ], named
        # AR864 holds 6 min in the air for its Lisbon arrival slot: its fuel is priced too.
        airline = workings["airline"]
        assert [(line["flight_no"], line["held_minutes"]) for line in airline["holding"]] == [
            ("AR864", 6)
        ], airline["holding"]
        priced = sum(line["eur"] for key in ("flights", "fuel", "holding") for line in airline[key])
        assert abs(cost["airline_eur"] - priced) <= 1, (cost["airline_eur"], priced)
        # Many flights make up minutes: their count and range, not a list (owner).
        assert airline["fuel_summary"] == (
            "Fly faster: 19 flights, 2–5 min each (72 min) · fuel €6,149 + CO2 €917 = €7,066"
        ), airline["fuel_summary"]
        # The playbook the orchestrator read has none of the actions dropped with Act now, and
        # no plan names, shows or sends one.
        offered = {action["id"] for action in seen["playbook"]["actions"]}
        assert STORM_PLAYBOOK and set(STORM_PLAYBOOK) <= offered and not REMOVED & offered
        for plan in (delay_plan, cancel):
            shown = checklist_rows(plan)
            assert not REMOVED & {*shown, *plan["levers"]["playbook_actions"]}, shown
            assert not REMOVED & set(plan["playbook_steps"]), plan["playbook_steps"]
            # Dispatch's advice is advice: in neither cost nor route.
            assert (shown["FUEL-FLAG"]["status"], shown["FUEL-FLAG"]["facts"]) == (
                "advice",
                "114 arrivals into Lisbon 14:30–21:00: carry holding fuel",
            ), shown["FUEL-FLAG"]
            assert (shown["ALT-FLAG"]["status"], shown["ALT-FLAG"]["facts"]) == (
                "advice",
                "Porto 277 km, Faro 221 km: no forecast hazard",
            ), shown["ALT-FLAG"]
            # Both plans are OCC's under the Weather plans rule, whatever their playbook.
            assert WEATHER_REASON in plan["policy"]["reasons"], plan["policy"]["reasons"]
        waiting = checklist_rows(delay_plan)
        assert {key: waiting[key]["status"] for key in ("RETIME", "STANDBY", "PAX-NOTICE")} == {
            "RETIME": "in_plan",
            "STANDBY": "in_plan",
            "PAX-NOTICE": "in_plan",
        }, delay_plan["playbook"]
        # The head start: a home standby needs 60 min to answer and reports 60 min before an
        # A320 departure, so crew control calls R1 by 12:00 for AR827; the forecast came at 09:45.
        assert waiting["STANDBY"]["facts"] == (
            "R1 called 12:00 · reports 13:00 · AR827, AR828;"
            " R2 called 13:40 · reports 14:40 · AR881, AR882"
        ), waiting["STANDBY"]
        # Passengers on every flight the plan retimes are told their new times; nobody whose
        # booking has a cancelled flight (they are rebooked).
        assert waiting["PAX-NOTICE"]["facts"] == (
            "81 flights · 14,392 passengers told their new times"
        ), waiting["PAX-NOTICE"]
        cancelling = checklist_rows(cancel)
        assert (cancelling["THIN"]["status"], cancelling["PAX-NOTICE"]["facts"]) == (
            "in_plan",
            "75 flights · 13,167 passengers told their new times",
        ), cancel["playbook"]
        # The comparison as it comes out (not tuned): the delay keeps everyone travelling, 131 by
        # car; cancelling the Faro round trips loses 655 passengers their flight or connection
        # and costs far more. The storm is an extraordinary circumstance (EU261 recitals 14-15),
        # so neither plan owes compensation; care and rebooking stay. Nobody is stranded
        # overnight and no crew is left over its limit in either.
        assert {kind: (plan["outcomes"], figures(plan)) for kind, plan in plans.items()} == {
            "delay": (
                {"stranded_overnight": 0, "lose_flight_or_connection": 0, "crews_over_limit": 0},
                (58533, 58533, 0, 0, 0, 0, 0, 0, 131),
            ),
            "cancel": (
                {"stranded_overnight": 0, "lose_flight_or_connection": 655, "crews_over_limit": 0},
                (210377, 103174, 0, 11790, 95413, 655, 41, 614, 101),
            ),
        }
        # What would have been owed had the airline caused it, said beside the plan, never
        # counted: 653 passengers, €187,100.
        assert (cost["owed_compensation_passengers"], workings["compensation"]) == (0, []), cost
        assert workings["compensation_not_owed"] == {
            "passengers": 653,
            "eur": 187100,
            "reason": NOT_OWED_REASON,
        }, workings.get("compensation_not_owed")
        # The Passenger agent reads the same: weather is an extraordinary circumstance, nothing
        # owed, what would have been owed beside it, and the plan's care and rebooking.
        exposure = seen["exposure"]
        assert (
            exposure["extraordinary_circumstances"],
            exposure["reason"],
            exposure["compensation_eur"],
            exposure["passengers_owed_compensation"],
            exposure["compensation_not_owed"],
            exposure["care_eur"],
            exposure["rebooking_eur"],
        ) == (
            True,
            NOT_OWED_REASON,
            cost["compensation_eur"],
            0,
            workings["compensation_not_owed"],
            cost["care_eur"],
            cost["rebooking_eur"],
        ), exposure
        assert cost["low_eur"] == cost["high_eur"] == cost["total_eur"], cost
        assert "compensation_not_owed" not in delay_plan["cost"]["workings"]
        assert not any("weather_excused_eur" in plan["cost"] for plan in plans.values())
        # The Case Assistant and the trail read the same checklist, outcomes and costs.
        [waits] = [
            plan
            for plan in case_digest(case)["plans"]
            if plan["levers"]["aircraft_option_id"] == "delay"
        ]
        assert waits["outcomes"] == delay_plan["outcomes"], waits
        assert (waits["cost"]["total"], waits["cost"]["compensation"]) == (58533, 0), waits
        assert "weather_excused" not in waits["cost"], waits["cost"]
        assert {
            "id": "PAX-NOTICE",
            "status": "in_plan",
            "facts": waiting["PAX-NOTICE"]["facts"],
        } in (waits["playbook"]), waits["playbook"]
        views = build_trail(case, None, lambda name: None, None)["plans"]
        [viewed] = [view for view in views if view["plan_id"] == delay_plan["plan_id"]]
        assert viewed["outcomes"] == delay_plan["outcomes"], viewed
        assert "weather_excused_eur" not in viewed, viewed
        assert viewed["playbook"] == delay_plan["playbook"]
        # A storm plan follows the reduced rate: a what-if adding delay is refused in plain words.
        assert gateway.tools is not None
        try:
            gateway.tools.what_if(
                trace,
                {"aircraft_option_id": "delay", "extra_delay_minutes": 30},
                "What if we hold them 30 minutes more?",
                {"oid": OPERATOR, "name": "Duty Manager"},
                "e1",
            )
        except ToolRefused as error:
            assert str(error) == (
                "Not built: A weather plan follows the reduced hourly rate; it takes no added delay"
            ), error
        else:
            raise AssertionError("ARROW added delay to a storm plan")

        operations.record_decision(
            trace,
            option_id=delay_plan["option_id"],
            decision="approve",
            expected_version=case["version"],
            idempotency_key="decision-case-5-delay-1",
            actor_oid=OPERATOR,
        )
        operations.execute(trace)
        # Owner: "it does not make sense to already update eurocontrol ... we dont even
        # know if storm will hit that hard". OCC approves once and only the "now" part runs:
        # the passengers of the plan's flights are warned of possible delays and both standby
        # crews are put on alert, each to be called by its report time less 60 minutes. Nothing
        # reaches Schedule, Slots, EUROCONTROL or the airport until the storm is confirmed.
        flights = delay_plan["playbook_steps"]["PAX-NOTICE"]["flights"]
        moved = delay_plan["flights"]
        held = cases.get(trace)
        execution = held["execution"]
        assert execution["status"] == "held", (execution["status"], held["events"][-1])
        now, later = execution["actions"][:2], execution["actions"][2:]
        assert [(a["kind"], a["stage"], a["status"], a["summary"]) for a in now] == [
            (
                "warn_passengers",
                "now",
                "confirmed",
                "Warn passengers on 81 flights of possible delays",
            ),
            ("alert_standby", "now", "confirmed", "Put standby crews R1, R2 on alert"),
        ], now
        assert later and {(a["stage"], a["status"]) for a in later} == {
            ("when_confirmed", "not_started")
        }, later
        assert [sent["path"] for sent in gateway.sent] == [
            "/api/passenger-notices",
            "/api/standby-alerts",
        ], gateway.sent
        warned, alerted = (sent["body"] for sent in gateway.sent)
        assert (warned["kind"], warned["flights"]) == ("possible_delay", flights), warned
        assert [
            (crew["reserve_option_id"], crew["call_by_local"], crew["crew"])
            for crew in alerted["crews"]
        ] == [
            ("R1", "2026-11-22T12:00:00", delay_plan["crew_reserves"][0]["crew"]),
            ("R2", "2026-11-22T13:40:00", delay_plan["crew_reserves"][1]["crew"]),
        ], alerted
        storm_held = execution["storm"]
        assert (storm_held["state"], storm_held["decide_by"], storm_held["updates"]) == (
            "awaiting",
            "12:00",
            [],
        ), storm_held
        assert storm_held["confirm_by"] and storm_held["stand_down_by"], storm_held
        assert (held["events"][-1]["event"], held["events"][-1]["message"]) == (
            "storm_held",
            "Now: done · when confirmed: held · decide by 12:00",
        ), held["events"][-1]
        assert case_state(held) == ("progress", "Held · decide by 12:00")
        assert sorted(store.passenger_notices(STORM_DAY, kind="possible_delay")) == flights
        assert not store.passenger_notices(STORM_DAY)
        assert [(a["reserve_option_id"], a["status"]) for a in store.standby_alerts(STORM_DAY)] == [
            ("R1", "alerted"),
            ("R2", "alerted"),
        ]
        assert all(
            store.flights[f["flight_no"]]["std_local"] == originals[f["flight_no"]]["std_local"]
            for f in moved
        )
        assert "D-LIS-146-R1" not in store.duties
        # Review focus: ARROW restarts while the storm is held. The case stays held and nothing
        # is sent again.
        operations.resume()
        Executor(cases, gateway).run(trace)
        assert cases.get(trace)["execution"]["status"] == "held"
        assert len(gateway.sent) == 2, gateway.sent
        # A METAR confirms the crosswind in the planned window at the planned rate (the Test
        # Bench's "Crosswind confirmed"): ARROW re-checks the plan against the systems and sends
        # the held part, in order.
        confirmed = weather_update("confirmed")
        assert deliver(operations, cases, confirmed, 1) == "update"
        operations.resume()
        # Task 2: OCC approves the delay (was the cancellation); one crew write calls both crews.
        kinds = assert_completed({**case, "_cases": cases})
        assert {"retime_flights", "assign_reserve_crew"} <= set(kinds), kinds
        actions = cases.get(trace)["execution"]["actions"]
        [crew] = [action for action in actions if action["kind"] == "assign_reserve_crew"]
        [call] = crew["calls"]
        assert (crew["summary"], call["sent"]["reserve_option_ids"]) == (
            "Assign standby crews R1, R2",
            ["R1", "R2"],
        ), crew
        assert sorted(store.duties["D-LIS-146-R1"]["flight_nos"]) == ["AR827", "AR828"]
        assert sorted(store.duties["D-LIS-164-R2"]["flight_nos"]) == ["AR881", "AR882"]
        # Both standby crews are called first, then the duty change assigns them (Crew refuses a
        # call for crew no longer on standby).
        [last] = [action for action in actions if action["kind"] == "call_standby"]
        assert actions.index(last) < actions.index(crew), [a["kind"] for a in actions]
        assert (last["kind"], last["stage"], last["summary"], last["status"]) == (
            "call_standby",
            "when_confirmed",
            "Call standby crews R1, R2 · forecast confirmed",
            "confirmed",
        ), last
        assert [(a["reserve_option_id"], a["status"]) for a in store.standby_alerts(STORM_DAY)] == [
            ("R1", "called"),
            ("R2", "called"),
        ]
        assert len({originals[f["flight_no"]]["aircraft_reg"] for f in moved}) >= 2, moved
        assert all(store.flights[f["flight_no"]]["std_local"] == f["std_local"] for f in moved)
        done = cases.get(trace)
        [update] = done["execution"]["storm"]["updates"]
        assert done["execution"]["storm"]["state"] == "confirmed", done["execution"]["storm"]
        assert (update["status"], update["window"], update["rate_per_hour"]) == (
            "confirmed",
            "16:00–18:00",
            24,
        ), update
        assert update["description"].startswith("METAR LPPT") and update["at"], update
        assert [e["event"] for e in done["events"] if e["event"].startswith("storm_")] == [
            "storm_held",
            "storm_confirmed",
            "storm_released",
        ], [e["event"] for e in done["events"]]
        # The held part goes out only after the two "now" writes, in the order it was planned.
        assert [sent["path"] for sent in gateway.sent[:2]] == [
            "/api/passenger-notices",
            "/api/standby-alerts",
        ]
        # The passenger notice follows the retime and its messages and comes before the crew
        # write and the cars, which do not wait for it.
        paths = [call["path"] for action in actions[2:] for call in action["calls"]]
        assert [sent["path"] for sent in gateway.sent[2:]] == paths, gateway.sent[2:]
        told = paths.index("/api/passenger-notices")
        flight_calls = [
            index
            for index, path in enumerate(paths)
            if path.startswith(("/api/flights/", "/nm/", "/acdm/"))
        ]
        assert flight_calls and max(flight_calls) < told, paths
        assert (
            told
            < paths.index("/api/duty-changes")
            < paths.index("/ground-handling/transfer-orders")
        ), paths
        [notice] = [action for action in actions if action["kind"] == "notify_passengers"]
        [sent] = notice["calls"]
        assert (notice["summary"], sent["sent"]["flights"]) == (
            "Tell passengers on 81 flights their new times",
            flights,
        ), notice
        # Booking told as many passengers as the plan counted, and changed no booking.
        assert notice["message"].startswith(
            "Told 14,392 passengers on 81 flights their new times."
        ), notice["message"]
        assert sorted(store.passenger_notices(STORM_DAY)) == flights
        assert case_state(cases.get(trace)) == ("handled", "Approved and carried out")
        assert case_card(cases.get(trace))["impact"]["execution_status"] == "completed"
        # A second notice of the same flights the same day (another case, or a later plan) is
        # answered, and tells nobody twice.
        key = "storm-second-pax-notice"
        second = gateway.send(
            "booking",
            "POST",
            "/api/passenger-notices",
            {"Idempotency-Key": key, "X-Request-Id": key},
            {"trace_id": trace, "flights": flights},
            trace,
        )
        assert (second["status"], second["notified_flights"], second["already_notified"]) == (
            "accepted",
            0,
            flights,
        ), second
        assert sorted(store.passenger_notices(STORM_DAY)) == flights
        # Review focus: the same update event delivered twice changes nothing.
        settled = cases.get(trace)
        assert deliver(operations, cases, confirmed, 2) == "skipped"
        operations.resume()
        again_settled = cases.get(trace)
        assert (again_settled["version"], len(again_settled["events"])) == (
            settled["version"],
            len(settled["events"]),
        )
        # Review focus: the forecast is dropped after the held part went out. It is recorded,
        # nothing is reversed and OCC reads it in plain words.
        writes = len(gateway.sent)
        assert deliver(operations, cases, weather_update("dropped"), 3) == "update"
        operations.resume()
        after = cases.get(trace)
        assert (after["execution"]["status"], len(gateway.sent)) == ("completed", writes)
        assert (after["events"][-1]["event"], after["events"][-1]["message"]) == (
            "storm_update_recorded",
            "Forecast dropped after the held actions were sent: recorded; nothing is reversed.",
        ), after["events"][-1]
        assert [update["status"] for update in after["execution"]["storm"]["updates"]] == [
            "confirmed",
            "dropped",
        ]
        assert not store.passenger_notices(STORM_DAY, kind="no_change")
        assert case_state(after) == ("handled", "Approved and carried out")

        # The same TAF again is the same storm: added to its case, never recorded twice; so is
        # the storm sent as a typed event.
        again = operations.open(message, OPERATOR, source=source)["trace_id"]
        operations.run(again)
        typed = {key: storm_reading(STORM_DAY)[key] for key in ("airport", "taf_window", "hazard")}
        _, report = read_event(cloud_event("weather", {**typed, "description": taf(STORM_DAY)}))
        typed_again = operations.open(report, OPERATOR, source=source)["trace_id"]
        operations.run(typed_again)
        for repeat in (cases.get(again), cases.get(typed_again)):
            assert repeat["repeat_of"]["display_id"] == case["display_id"], repeat["stage"]
        # The same forecast for another window, or read as another hazard, is an update:
        # stopped for OCC, not recorded.
        stopped: list[str] = []
        for change in ({"taf_window": "2217/2219"}, {"hazard": "thunderstorm"}):
            _, report = read_event(
                cloud_event("weather", {**typed, **change, "description": taf(STORM_DAY)})
            )
            update = operations.open(report, OPERATOR, source=source)["trace_id"]
            operations.run(update)
            stopped.append(cases.get(update)["stage"])
        held = (
            f"Weather at Lisbon is already case {case['display_id']} at 16:00–18:00 strong"
            " crosswind"
        )
        assert stopped == [
            f"{held}; this report says 17:00–19:00 strong crosswind · stopped for OCC to update"
            " the case.",
            f"{held}; this report says 16:00–18:00 thunderstorms · stopped for OCC to update the"
            " case.",
        ], stopped
        assert not store.weather, "a forecast is recorded in no airline system"


def check_a_storm_plan_within_every_limit_still_goes_to_occ() -> None:
    # Final review: an hour of forecast fog delays a few flights within every limit,
    # yet a plan a forecast drives across the network is a major decision for OCC.
    today, tomorrow = f"{STORM_DAY.day:02d}", f"{STORM_DAY.day + 1:02d}"
    fog = (
        f"TAF LPPT {today}0500Z {today}06/{tomorrow}12 18008KT 9999 FEW020"
        f" TEMPO {today}19/{today}20 0300 FG VV001"
    )

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        reading = storm_reading(STORM_DAY, taf_window=f"{today}19/{today}20")
        tools.record_disruption(trace, {**reading, "hazard": "low_visibility"})
        plan = tools.build_plan(trace, {"aircraft_option_id": "delay"})
        insist(tools, trace, plan["plan_id"], "Fewest passengers lose their flight")
        return submitted(plan["plan_id"])

    with mock.patch("airline_systems.schedule.datetime", StormDayClock):
        operations, cases, _ = operate(OperatingDayStore(build_operating_day(STORM_DAY)), script)
        trace = operations.open({"type": "message", "text": fog}, OPERATOR)["trace_id"]
        operations.run(trace)

    case = cases.get(trace)
    assert (recommended(case)["policy"]["reasons"], "execution" in case) == (
        ["OCC decides", WEATHER_REASON],
        False,
    ), case["stage"]


def check_a_refused_passenger_notice_holds_back_nothing_else() -> None:
    # Review: nothing waits for the passenger notice. Refused, it leaves every other
    # action sent, and OCC is told so; nobody is recorded as told. Now the notice of new
    # times is in the part held until the fog is confirmed; the warning of possible delays went
    # out on approval.
    today, tomorrow = f"{STORM_DAY.day:02d}", f"{STORM_DAY.day + 1:02d}"
    fog = (
        f"TAF LPPT {today}0500Z {today}06/{tomorrow}12 18008KT 9999 FEW020"
        f" TEMPO {today}19/{today}20 0300 FG VV001"
    )

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        reading = storm_reading(STORM_DAY, taf_window=f"{today}19/{today}20")
        tools.record_disruption(trace, {**reading, "hazard": "low_visibility"})
        plan = tools.build_plan(
            trace, {"aircraft_option_id": "delay", "playbook_actions": ["PAX-NOTICE"]}
        )
        insist(tools, trace, plan["plan_id"], "Fewest passengers lose their flight")
        return submitted(plan["plan_id"])

    store = OperatingDayStore(build_operating_day(STORM_DAY))
    with mock.patch("airline_systems.schedule.datetime", StormDayClock):
        operations, cases, gateway = operate(store, script)
        trace = operations.open({"type": "message", "text": fog}, OPERATOR)["trace_id"]
        operations.run(trace)
        case = cases.get(trace)
        plan = recommended(case)
        assert plan["playbook_steps"]["PAX-NOTICE"]["flights"], plan["playbook_steps"]
        send = gateway.send

        def refuse(*args: Any, **kwargs: Any) -> Any:
            body = args[4] or {}
            if "/api/passenger-notices" in args and body.get("kind", "new_times") == "new_times":
                return {"status": "refused", "reason": "Notices are not being sent"}
            return send(*args, **kwargs)

        with mock.patch.object(gateway, "send", refuse):
            operations.record_decision(
                trace,
                option_id=plan["option_id"],
                decision="approve",
                expected_version=case["version"],
                idempotency_key="decision-fog-notice-1",
                actor_oid=OPERATOR,
            )
            operations.execute(trace)
            assert cases.get(trace)["execution"]["status"] == "held"
            # The fog is observed as forecast: ARROW's executor is told directly.
            rate = case["disruption"]["hourly_rate"]
            update = weather_update(
                "confirmed", window=f"{today}19/{today}20", rate=rate, hazard="low_visibility"
            )
            Executor(cases, gateway).confirm(trace, update["data"])

    state = cases.get(trace)
    statuses = {action["kind"]: action["status"] for action in state["execution"]["actions"]}
    assert statuses.pop("notify_passengers") == "refused", statuses
    assert statuses.pop("warn_passengers") == "confirmed", statuses
    assert statuses and set(statuses.values()) == {"confirmed"}, statuses
    assert state["execution"]["status"] == "needs_attention"
    told = len(plan["playbook_steps"]["PAX-NOTICE"]["flights"])
    assert state["events"][-1]["message"] == (
        f"OCC attention: Tell passengers on {told} flight{'' if told == 1 else 's'} their new"
        " times: refused (Notices are not being sent). Nothing depended on it; the other"
        " actions were sent."
    ), state["events"][-1]["message"]
    assert not store.passenger_notices(STORM_DAY)
    assert sorted(store.passenger_notices(STORM_DAY, kind="possible_delay")) == sorted(
        plan["playbook_steps"]["PAX-NOTICE"]["flights"]
    )


FOG_TODAY = f"{STORM_DAY.day:02d}"
FOG = (
    f"TAF LPPT {FOG_TODAY}0500Z {FOG_TODAY}06/{STORM_DAY.day + 1:02d}12 18008KT 9999 FEW020"
    f" TEMPO {FOG_TODAY}19/{FOG_TODAY}20 0300 FG VV001"
)


def check_a_storm_confirmed_before_approval_is_recorded_and_sent_once_approved() -> None:
    # Review focus: a confirmation for a storm case OCC has not approved yet is recorded on the
    # case and shown ("Storm confirmed"); nothing is sent. Approved afterwards, ARROW re-checks
    # and sends the plan at once: there is nothing left to hold. The update is listed for the
    # Test Bench with its case, as any event ARROW received.
    window = f"{FOG_TODAY}19/{FOG_TODAY}20"

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        reading = storm_reading(STORM_DAY, taf_window=window)
        tools.record_disruption(trace, {**reading, "hazard": "low_visibility"})
        plan = tools.build_plan(
            trace, {"aircraft_option_id": "delay", "playbook_actions": ["PAX-NOTICE"]}
        )
        tools.submit_plan(trace, plan["plan_id"], "Fewest passengers lose their flight")
        return submitted(plan["plan_id"])

    with mock.patch("airline_systems.schedule.datetime", StormDayClock):
        store = OperatingDayStore(build_operating_day(STORM_DAY))
        operations, cases, gateway = operate(store, script)
        report = {
            **cloud_event("reported", {"text": FOG, "system": "IPMA (weather)"}),
            "type": "com.arrow.disruption.reported",
        }
        assert deliver(operations, cases, report, 1) == "new_case"
        found = cases.find_by_event(report["id"])
        assert found is not None
        trace = found["trace_id"]
        case = cases.get(trace)
        rate = case["disruption"]["hourly_rate"]
        update = weather_update("confirmed", window=window, rate=rate, hazard="low_visibility")
        assert deliver(operations, cases, update, 2) == "update"
        operations.resume()

        waiting = cases.get(trace)
        assert "execution" not in waiting and gateway.sent == [], gateway.sent
        assert waiting["storm"]["state"] == "confirmed", waiting.get("storm")
        assert [u["status"] for u in waiting["storm"]["updates"]] == ["confirmed"]
        [said] = [e for e in waiting["events"] if e["event"] == "storm_confirmed"]
        assert said["message"] == (
            f"Low visibility confirmed: 19:00–20:00, rate {rate} an hour · recorded;"
            " nothing is sent until OCC approves a plan."
        ), said
        assert case_state(waiting) == ("needs", "Needs your decision")
        assert build_trail(waiting, None, lambda name: None, None)["outcome"]["storm"]["state"] == (
            "confirmed"
        )
        assert case_digest(waiting)["storm"]["state"] == "confirmed"
        app = create_app(
            cases,
            operations,
            frozenset({OPERATOR}),
            frozenset({HOST}),
            "https://arrow.test",
            testbench_oids=frozenset({TESTBENCH}),
            rejections=Rejections(),
        )
        with TestClient(app) as client:
            listed = client.get(f"/api/events?day={STORM_DAY}", headers=principal(TESTBENCH))
            status = client.get(f"/api/events/{update['id']}", headers=principal(TESTBENCH))
        rows = {row["event_id"]: row for row in listed.json()["events"]}
        assert {report["id"], update["id"]} <= rows.keys(), rows.keys()
        assert rows[update["id"]]["envelope"] == update, rows[update["id"]]
        assert rows[update["id"]]["case"]["trace_id"] == trace
        assert status.status_code == 200, status.text
        assert [d["body"] for d in status.json()["deliveries"]] == [update], status.json()

        plan = approve_storm(operations, cases, trace)

    done = cases.get(trace)
    execution = done["execution"]
    assert execution["status"] == "completed", (execution["status"], done["events"][-1])
    assert execution["storm"]["state"] == "confirmed"
    assert {action["status"] for action in execution["actions"]} == {"confirmed"}
    assert gateway.sent[0]["body"]["kind"] == "possible_delay", gateway.sent[0]
    assert all(
        store.flights[f["flight_no"]]["std_local"] == f["std_local"] for f in plan["flights"]
    )
    assert case_state(done) == ("handled", "Approved and carried out")


def fog_case() -> tuple[Any, Any, Any, OperatingDayStore, str]:
    """An hour of forecast fog at Lisbon (19:00-20:00): a small weather plan with the passenger
    notice and no standby crew, waiting for OCC. Call under StormDayClock."""
    window = f"{FOG_TODAY}19/{FOG_TODAY}20"

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        reading = storm_reading(STORM_DAY, taf_window=window)
        tools.record_disruption(trace, {**reading, "hazard": "low_visibility"})
        plan = tools.build_plan(
            trace, {"aircraft_option_id": "delay", "playbook_actions": ["PAX-NOTICE"]}
        )
        tools.submit_plan(trace, plan["plan_id"], "Fewest passengers lose their flight")
        return submitted(plan["plan_id"])

    store = OperatingDayStore(build_operating_day(STORM_DAY))
    operations, cases, gateway = operate(store, script)
    trace = operations.open({"type": "message", "text": FOG}, OPERATOR)["trace_id"]
    operations.run(trace)
    return operations, cases, gateway, store, trace


def fog_update(status: str, rate: int | None = None) -> dict[str, Any]:
    window = f"{FOG_TODAY}19/{FOG_TODAY}20"
    return weather_update(status, window=window, rate=rate, hazard="low_visibility")


def check_the_latest_storm_update_decides_until_the_held_part_is_released() -> None:
    # Review: until the held part goes, the latest update decides. A forecast confirmed
    # and then dropped before OCC approves stands the plan down on approval at once: nothing is
    # sent, not even the warning. Confirmed then dropped while held, the warned passengers are
    # told no change is expected. Confirmed and then confirmed worse, the plan goes back to OCC
    # and nothing held is sent.
    with mock.patch("airline_systems.schedule.datetime", StormDayClock):
        operations, cases, gateway, store, trace = fog_case()
        rate = cases.get(trace)["disruption"]["hourly_rate"]
        assert deliver(operations, cases, fog_update("confirmed", rate), 1) == "update"
        assert deliver(operations, cases, fog_update("dropped"), 2) == "update"
        assert cases.get(trace)["storm"]["state"] == "dropped"
        approve_storm(operations, cases, trace)
        before = cases.get(trace)
        assert (before["execution"]["status"], gateway.sent) == ("stood_down", []), before[
            "events"
        ][-1]
        assert {action["status"] for action in before["execution"]["actions"]} == {"stood_down"}
        assert before["events"][-1]["message"] == (
            "Stood down: the forecast was dropped before anything was sent; nothing changes."
        )

        operations, cases, gateway, store, trace = fog_case()
        plan = approve_storm(operations, cases, trace)
        assert cases.get(trace)["execution"]["status"] == "held"
        assert deliver(operations, cases, fog_update("confirmed", rate), 1) == "update"
        assert deliver(operations, cases, fog_update("dropped"), 2) == "update"
        operations.resume()
        held = cases.get(trace)
        warned = plan["playbook_steps"]["PAX-NOTICE"]["flights"]
        assert held["execution"]["status"] == "stood_down", held["events"][-1]
        assert [(sent["path"], sent["body"]["kind"]) for sent in gateway.sent] == [
            ("/api/passenger-notices", "possible_delay"),
            ("/api/passenger-notices", "no_change"),
        ], gateway.sent
        assert sorted(store.passenger_notices(STORM_DAY, kind="no_change")) == sorted(warned)
        assert unchanged(store, held)

        operations, cases, gateway, store, trace = storm_case()
        approve_storm(operations, cases, trace)
        assert deliver(operations, cases, weather_update("confirmed"), 1) == "update"
        assert deliver(operations, cases, weather_update("confirmed", rate=16), 2) == "update"
        operations.resume()
        worse = cases.get(trace)
    assert worse["execution"]["status"] == "needs_attention", worse["events"][-1]
    assert worse["events"][-1]["message"] == (
        "OCC decides: the confirmed weather is worse than planned (rate 16 an hour, planned 24)."
        " Nothing more was sent; the passengers stay warned and the standby crews on alert."
    ), worse["events"][-1]
    assert [sent["path"] for sent in gateway.sent] == [
        "/api/passenger-notices",
        "/api/standby-alerts",
    ], gateway.sent
    assert unchanged(store, worse)


def check_a_stand_down_after_an_interrupted_warning_learns_what_was_sent() -> None:
    # Review: ARROW stops just after Booking (or Crew) applied a "now" write, before it
    # read the answer. The forecast is then dropped. That write is sent again under its key to
    # learn its outcome (Booking and Crew answer it once), so the warned passengers are told no
    # change is expected and crews on alert are released. A write never sent is not sent now.
    for interrupted, released in ((1, []), (2, ["R1", "R2"])):
        with mock.patch("airline_systems.schedule.datetime", StormDayClock):
            operations, cases, gateway, store, trace = storm_case()

            def stops(writes: int, at: int = interrupted) -> None:
                if writes == at:
                    raise RuntimeError("container recycled")

            gateway.after_call = stops
            plan = approve_storm(operations, cases, trace)
            gateway.after_call = None
            assert cases.get(trace)["execution"]["status"] == "executing"
            assert deliver(operations, cases, weather_update("dropped"), 1) == "update"
            operations.resume()

        state = cases.get(trace)
        warned = plan["playbook_steps"]["PAX-NOTICE"]["flights"]
        assert state["execution"]["status"] == "stood_down", state["events"][-1]
        paths = [sent["path"] for sent in gateway.sent]
        sent_now = ["/api/passenger-notices", "/api/standby-alerts"][:interrupted]
        assert paths == [
            *sent_now,
            sent_now[-1],  # sent again under its key: the first answer
            "/api/passenger-notices",
            *(["/api/standby-alerts/release"] if released else []),
        ], paths
        assert sorted(store.passenger_notices(STORM_DAY, kind="possible_delay")) == warned
        assert sorted(store.passenger_notices(STORM_DAY, kind="no_change")) == warned
        assert [
            (alert["reserve_option_id"], alert["status"])
            for alert in store.standby_alerts(STORM_DAY)
        ] == [(crew, "released") for crew in released]
        told = "passengers on 81 flights told no change is expected"
        assert state["events"][-1]["message"] == (
            f"Stood down: {'standby crews R1, R2 released; ' if released else ''}{told};"
            " nothing held was sent."
        ), state["events"][-1]
        assert unchanged(store, state)


def check_the_latest_pre_approval_update_decides_on_approval() -> None:
    # Final review: before approval the latest update decides, and the case shows what
    # approval will do. Dropped then confirmed: approval sends the "now" part and, after the
    # re-check, the held part. Dropped then confirmed worse: the case says so, and approval goes
    # back to OCC without sending anything.
    with mock.patch("airline_systems.schedule.datetime", StormDayClock):
        operations, cases, gateway, store, trace = fog_case()
        rate = cases.get(trace)["disruption"]["hourly_rate"]
        assert deliver(operations, cases, fog_update("dropped"), 1) == "update"
        assert deliver(operations, cases, fog_update("confirmed", rate), 2) == "update"
        shown = cases.get(trace)["storm"]
        assert (shown["state"], shown.get("worse")) == ("confirmed", None), shown
        plan = approve_storm(operations, cases, trace)
        done = cases.get(trace)
        assert done["execution"]["status"] == "completed", done["events"][-1]
        assert gateway.sent[0]["body"]["kind"] == "possible_delay", gateway.sent[0]
        assert all(
            store.flights[f["flight_no"]]["std_local"] == f["std_local"] for f in plan["flights"]
        )
        applied = [e["event"] for e in done["events"] if e["event"].startswith("storm_")]
        # Recorded before approval (two), then on approval the latest only.
        assert applied == ["storm_dropped", "storm_confirmed", "storm_confirmed", "storm_released"]

        operations, cases, gateway, store, trace = fog_case()
        assert deliver(operations, cases, fog_update("dropped"), 1) == "update"
        assert deliver(operations, cases, fog_update("confirmed", rate - 1), 2) == "update"
        shown = cases.get(trace)["storm"]
        worse = (
            f"the confirmed weather is worse than planned (rate {rate - 1} an hour, planned {rate})"
        )
        assert (shown["state"], shown["worse"]) == ("confirmed", worse), shown
        approve_storm(operations, cases, trace)
        back = cases.get(trace)
    assert back["execution"]["status"] == "needs_attention", back["events"][-1]
    assert back["events"][-1]["message"] == f"OCC decides: {worse}. Nothing was sent."
    assert gateway.sent == [], gateway.sent
    assert case_state(back) == ("needs", "Needs attention")


def check_a_forecast_dropped_while_the_now_part_is_sent_is_not_lost() -> None:
    # An amended TAF arrives while ARROW is still warning the passengers. The plan is held only
    # under the case's lock, after the updates recorded meanwhile are applied, so the drop is
    # never left waiting on a held plan: the fog plan is stood down at once.
    window = f"{FOG_TODAY}19/{FOG_TODAY}20"

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        reading = storm_reading(STORM_DAY, taf_window=window)
        tools.record_disruption(trace, {**reading, "hazard": "low_visibility"})
        plan = tools.build_plan(
            trace, {"aircraft_option_id": "delay", "playbook_actions": ["PAX-NOTICE"]}
        )
        tools.submit_plan(trace, plan["plan_id"], "Fewest passengers lose their flight")
        return submitted(plan["plan_id"])

    with mock.patch("airline_systems.schedule.datetime", StormDayClock):
        store = OperatingDayStore(build_operating_day(STORM_DAY))
        operations, cases, gateway = operate(store, script)
        trace = operations.open({"type": "message", "text": FOG}, OPERATOR)["trace_id"]
        operations.run(trace)
        dropped = weather_update("dropped", window=window, hazard="low_visibility")

        def amended_taf(writes: int) -> None:
            if writes == 1:
                Executor(cases, gateway).stand_down(trace, dropped["data"])

        gateway.after_call = amended_taf
        approve_storm(operations, cases, trace)

    state = cases.get(trace)
    execution = state["execution"]
    assert execution["status"] == "stood_down", (execution["status"], state["events"][-1])
    assert [(sent["path"], sent["body"]["kind"]) for sent in gateway.sent] == [
        ("/api/passenger-notices", "possible_delay"),
        ("/api/passenger-notices", "no_change"),
    ], gateway.sent
    assert [e["event"] for e in state["events"] if e["event"].startswith("storm_")] == [
        "storm_dropped",
        "storm_stood_down",
    ]
    assert unchanged(store, state)


def check_a_storm_worse_than_planned_goes_back_to_occ() -> None:
    # The METAR confirms the storm, but at a lower rate than Schedule planned, or for longer:
    # nothing more is sent and OCC decides, told why.
    today = f"{STORM_DAY.day:02d}"
    for update, worse in (
        (weather_update("confirmed", rate=16), "rate 16 an hour, planned 24"),
        (
            weather_update("confirmed", window=f"{today}15/{today}19"),
            "15:00–19:00, planned 16:00–18:00",
        ),
    ):
        with mock.patch("airline_systems.schedule.datetime", StormDayClock):
            operations, cases, gateway, store, trace = storm_case()
            plan = approve_storm(operations, cases, trace)
            assert deliver(operations, cases, update, 1) == "update"
            operations.resume()

        state = cases.get(trace)
        execution = state["execution"]
        reason = f"the confirmed weather is worse than planned ({worse})"
        assert (execution["status"], execution["storm"]["returned"]) == ("needs_attention", reason)
        assert (state["events"][-1]["event"], state["events"][-1]["message"]) == (
            "storm_returned_to_occ",
            f"OCC decides: {reason}. Nothing more was sent; the passengers stay warned and the"
            " standby crews on alert.",
        ), state["events"][-1]
        assert [sent["path"] for sent in gateway.sent] == [
            "/api/passenger-notices",
            "/api/standby-alerts",
        ], gateway.sent
        assert all(a["status"] == "not_started" for a in execution["actions"][2:])
        assert unchanged(store, state), plan["option_id"]
        assert case_state(state) == ("needs", "Needs attention")


def check_a_dropped_forecast_stands_the_storm_down() -> None:
    # Standby crews are called at their call-by time while the storm is still unconfirmed (crew
    # only). In Dev the systems' clock stays at 09:45, so this is shown offline only. Then the
    # amended TAF drops the crosswind: ARROW releases the standby crews, tells the warned
    # passengers no change is expected, sends nothing held and the case is stood down.
    with mock.patch("airline_systems.schedule.datetime", StormDayClock):
        operations, cases, gateway, store, trace = storm_case()
        plan = approve_storm(operations, cases, trace)
        warned = plan["playbook_steps"]["PAX-NOTICE"]["flights"]
        # Review: the call-by check reads Schedule's clock once a round, outside any
        # case, so no system audits a read against the storm case.
        read_in_case: list[str] = []
        network = gateway.network

        def read(trace_id: str) -> dict[str, Any]:
            read_in_case.append(trace_id)
            return network(trace_id)

        gateway.network = read  # type: ignore[method-assign]
        with mock.patch("airline_systems.schedule.DEMO_CLOCK", time(11, 59)):
            operations.supervise(0, rounds=1)
        assert (len(gateway.sent), gateway.clock_reads) == (2, 1), gateway.sent
        with mock.patch("airline_systems.schedule.DEMO_CLOCK", time(12, 0)):
            operations.supervise(0, rounds=1)
            operations.supervise(0, rounds=1)  # the next round calls nobody twice
        called = cases.get(trace)
        assert called["execution"]["status"] == "held", called["events"][-1]
        assert [(sent["path"], sent["body"]) for sent in gateway.sent[2:]] == [
            ("/api/standby-alerts/call", {"trace_id": trace, "reserve_option_ids": ["R1"]})
        ], gateway.sent[2:]
        [event] = [e for e in called["events"] if e["event"] == "standby_called"]
        assert event["message"] == (
            "Crew: Called standby crew R1 · call-by 12:00 reached before the forecast was confirmed"
        ), event
        assert [(a["reserve_option_id"], a["status"]) for a in store.standby_alerts(STORM_DAY)] == [
            ("R1", "called"),
            ("R2", "alerted"),
        ]
        with mock.patch("airline_systems.schedule.DEMO_CLOCK", time(13, 40)):
            operations.supervise(0, rounds=1)
        assert gateway.sent[-1]["body"]["reserve_option_ids"] == ["R2"], gateway.sent[-1]
        # Every standby crew is called: nothing is left to tick, so no clock is read and the
        # supervisor may go idle.
        reads = gateway.clock_reads
        assert operations.tick_held() == 0 and gateway.clock_reads == reads == 4
        assert read_in_case == [], read_in_case
        gateway.network = network  # type: ignore[method-assign]
        assert not store.passenger_notices(STORM_DAY)

        assert deliver(operations, cases, weather_update("dropped"), 1) == "update"
        operations.resume()

    state = cases.get(trace)
    execution = state["execution"]
    assert execution["status"] == "stood_down", (execution["status"], state["events"][-1])
    assert [(sent["path"], sent["body"]) for sent in gateway.sent[4:]] == [
        (
            "/api/passenger-notices",
            {"trace_id": trace, "flights": warned, "kind": "no_change"},
        ),
        ("/api/standby-alerts/release", {"trace_id": trace}),
    ], gateway.sent[4:]
    stages = {(a["stage"], a["status"]) for a in execution["actions"]}
    assert stages == {
        ("now", "confirmed"),
        ("when_confirmed", "stood_down"),
        ("stand_down", "confirmed"),
    }, stages
    assert [(a["reserve_option_id"], a["status"]) for a in store.standby_alerts(STORM_DAY)] == [
        ("R1", "released"),
        ("R2", "released"),
    ]
    assert sorted(store.passenger_notices(STORM_DAY, kind="no_change")) == warned
    assert not store.passenger_notices(STORM_DAY)
    assert unchanged(store, state)
    assert [e["event"] for e in state["events"] if e["event"].startswith("storm_")] == [
        "storm_held",
        "storm_dropped",
        "storm_stood_down",
    ]
    assert state["events"][-1]["message"] == (
        "Stood down: standby crews R1, R2 released; passengers on 81 flights told no change is"
        " expected; nothing held was sent."
    ), state["events"][-1]
    assert case_state(state) == ("handled", "Stood down")
    assert case_card(state)["label"] == "Stood down"
    # Final review: a stood-down storm is not a plan carried out. The control tower reads
    # the execution's status; the held actions still count in the total, and apart.
    impact = case_card(state)["impact"]
    assert (impact["execution_status"], impact["actions_total"], impact["actions_stood_down"]) == (
        "stood_down",
        len(execution["actions"]),
        sum(1 for a in execution["actions"] if a["status"] == "stood_down"),
    ), impact
    assert impact["actions_done"] < impact["actions_total"], impact
    # The storm is confirmed after all: recorded, nothing is sent, and OCC is prompted.
    with mock.patch("airline_systems.schedule.datetime", StormDayClock):
        writes = len(gateway.sent)
        assert deliver(operations, cases, weather_update("confirmed"), 2) == "update"
        operations.resume()
    late = cases.get(trace)
    assert (late["execution"]["status"], len(gateway.sent)) == ("stood_down", writes)
    assert late["events"][-1]["message"] == (
        "Strong crosswind confirmed: 16:00–18:00, rate 24 an hour after the stand-down:"
        " recorded; nothing is sent. OCC decides whether to plan again."
    ), late["events"][-1]
    assert case_state(late) == ("needs", "Forecast confirmed after stand-down"), case_state(late)


def check_the_cancel_plan_holds_its_cancellations_too() -> None:
    # Review focus: OCC approves the fallback (cancel the Faro round trips) instead. Its
    # cancellations are held the same way; its passengers, the cancelled flights' too, are warned.
    with mock.patch("airline_systems.schedule.datetime", StormDayClock):
        operations, cases, gateway, store, trace = storm_case()
        plan = approve_storm(operations, cases, trace, "cancel")
        held = cases.get(trace)["execution"]
        cancelled = sorted(f["flight_no"] for f in plan["flights"] if f["status"] == "cancelled")
        [cancelling] = [a for a in held["actions"] if a["kind"] == "cancel_flights"]
        assert (held["status"], cancelling["stage"], cancelling["status"]) == (
            "held",
            "when_confirmed",
            "not_started",
        ), held["status"]
        assert all(store.flights[number]["status"] != "cancelled" for number in cancelled)
        warned = gateway.sent[0]["body"]["flights"]
        assert set(cancelled) <= set(warned), warned
        assert sorted(warned) == sorted(
            {*plan["playbook_steps"]["PAX-NOTICE"]["flights"], *cancelled}
        ), warned
        # Passenger Services reviews the passengers outside policy once the plan goes ahead.
        assert not held.get("reviews"), held.get("reviews")
        Executor(cases, gateway).stand_down(trace, weather_update("dropped")["data"])

    state = cases.get(trace)
    assert state["execution"]["status"] == "stood_down", state["events"][-1]
    assert all(store.flights[number]["status"] != "cancelled" for number in cancelled)
    assert sorted(store.passenger_notices(STORM_DAY, kind="no_change")) == sorted(warned)
    assert case_state(state) == ("handled", "Stood down")


def check_standby_crews_that_are_not_enough_need_manual_intervention() -> None:
    # Task 2 (was Case 5's delay with one reserve): a four-hour storm, 16:00-20:00 UTC,
    # breaks eleven crew duties and Lisbon's four offered standby crews cover four. The delay
    # stays blocked; when the orchestrator builds nothing else, the case is manual intervention,
    # saying standby crews were tried.
    window = f"{STORM_DAY.day:02d}16/{STORM_DAY.day:02d}20"
    seen: dict[str, Any] = {}

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        tools.record_disruption(trace, storm_reading(STORM_DAY, taf_window=window))
        seen["ids"] = standby_ids(tools, trace, "delay")
        tools.build_plan(trace, {"aircraft_option_id": "delay", "reserve_option_ids": seen["ids"]})
        return {"outcome": "no_plan", "summary": "No legal crew", "submitted_plan_id": None}

    longer = taf(STORM_DAY).replace(
        f"TEMPO {STORM_DAY.day:02d}16/{STORM_DAY.day:02d}18", f"TEMPO {window}"
    )
    with mock.patch("airline_systems.schedule.datetime", StormDayClock):
        operations, cases, _ = operate(OperatingDayStore(build_operating_day(STORM_DAY)), script)
        trace = operations.open({"type": "message", "text": longer}, OPERATOR)["trace_id"]
        operations.run(trace)

    case = cases.get(trace)
    assert seen["ids"] == ["R1", "R2", "R3", "R4"], seen["ids"]
    [plan] = case["options"]
    assert plan["policy"]["route"] == "blocked", plan["policy"]
    assert plan["validation"]["crew_plan"]["breaches_unresolved"] == 7, plan["validation"]
    assert case["manual_intervention"]["reason"] == CREW_AFTER_STANDBY_CREWS, case["stage"]
    assert case["stage"].startswith(f"Manual intervention: {CREW_AFTER_STANDBY_CREWS}."), case[
        "stage"
    ]
    assert case["status"] == "assessment_ready" and "execution" not in case


def check_a_taf_is_read_in_code_on_a_summer_day() -> None:
    # The model copies the TAF's tokens; code maps LPPT to Lisbon and converts the UTC window
    # with Europe/Lisbon, an hour ahead in July. A window off today's operating day, a hazard the
    # chosen group does not forecast, or another airport's forecast, is refused in plain words.
    july = SummerClock.at.date()
    refused: list[str] = []

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        if "LPPR" in brief["message"]:
            readings = [storm_reading(july, airport="LPPR")]
        else:
            readings = [
                storm_reading(july, taf_window="1510/1612"),
                storm_reading(july, hazard="low_visibility"),  # 6,000 m, no fog
                storm_reading(july, taf_window="1513/1515"),  # gusts 28 kt, 5 kt across
                storm_reading(july),
            ]
        for reading in readings:
            try:
                tools.record_disruption(trace, reading)
            except ToolRefused as error:
                refused.append(str(error))
        return {"outcome": "no_plan", "summary": "Stopped for the test", "submitted_plan_id": None}

    with mock.patch("airline_systems.schedule.datetime", SummerClock):
        store = OperatingDayStore(build_operating_day(july))
        operations, cases, _ = operate(store, script)
        lisbon = operations.open({"type": "message", "text": taf(july)}, OPERATOR)["trace_id"]
        operations.run(lisbon)
        porto = operations.open({"type": "message", "text": taf(july, "LPPR")}, OPERATOR)
        operations.run(porto["trace_id"])

    assert refused == [
        "Not recorded: taf_window: 1510/1612 does not start and end on today's operating day"
        " (15 July)",
        "Not recorded: hazard: the 1516/1518 group forecasts no visibility under 800 m or fog",
        "Not recorded: hazard: the 1513/1515 group forecasts no wind across runway 02/20 of 25"
        " kt or more",
        "Not recorded: ARROW plans weather at Lisbon (LPPT) only, not LPPR",
    ], refused
    storm = cases.get(lisbon)["disruption"]
    assert (storm["window_start"], storm["window_end"]) == ("17:00", "19:00"), storm
    # Schedule, asked by the forecast's facts, sets the window on that day in Lisbon time.
    assert storm["window_start_local"] == "2026-07-15T17:00:00", storm
    assert not store.weather, "a forecast is recorded in no airline system"
    assert cases.get(porto["trace_id"])["disruption"]["type"] == "message"


PLAYBOOK_VERSION = "playbook-2026-11-23"
# Every action of the playbook that applies to a weather forecast since Act now was dropped.
STORM_ACTIONS = {
    "RETIME",
    "GROUND-HOLD",
    "THIN",
    "STANDBY",
    "REBOOK",
    "CAR",
    "PAX-NOTICE",
    "FUEL-FLAG",
    "ALT-FLAG",
}


def check_the_playbook_lists_the_actions_that_apply_to_each_case() -> None:
    # Owner: the standard OCC actions are "part of a plan that the orchestrator or so can
    # use as a playbook". The orchestrator reads the ones that apply to its case: a storm's own
    # (dispatch's flags as advice), a defect's maintenance estimate, never the other's; none of
    # the actions dropped with Act now.
    read: dict[str, dict[str, Any]] = {}

    def reads(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        if "message" in brief:
            tools.record_disruption(trace, storm_reading(STORM_DAY))
        read[trace] = tools.get_playbook(trace)
        return {"outcome": "no_plan", "summary": "Read the playbook", "submitted_plan_id": None}

    with mock.patch("airline_systems.schedule.datetime", StormDayClock):
        operations, _, _ = operate(OperatingDayStore(build_operating_day(STORM_DAY)), reads)
        storm = operations.open({"type": "message", "text": taf(STORM_DAY)}, OPERATOR)["trace_id"]
        operations.run(storm)
    registration, minutes = CASE_3
    defect = run(
        {
            "type": "aircraft_defect",
            "aircraft_reg": registration,
            "description": "Hydraulic leak",
            "unavailable_minutes": minutes,
        },
        reads,
    )

    weather = read[storm]
    actions = {action["id"]: action for action in weather["actions"]}
    assert (weather["version"], weather["disruption_type"]) == (PLAYBOOK_VERSION, "weather")
    assert actions.keys() == STORM_ACTIONS, actions.keys()
    assert all(
        set(action) == {"id", "name", "desk", "autonomy", "phase", "needs"} and action["needs"]
        for action in weather["actions"]
    ), weather["actions"]
    assert (actions["FUEL-FLAG"]["autonomy"], actions["THIN"]["autonomy"]) == ("advice", "occ")
    assert actions["PAX-NOTICE"]["name"] == "Tell passengers their new times"
    aog = {action["id"] for action in read[defect["trace_id"]]["actions"]}
    assert {"MX-ETR", "SWAP-AC", "THIN"} <= aog and not {"PAX-NOTICE", *REMOVED} & aog, aog


def check_an_action_that_does_not_apply_is_refused_and_the_plan_still_built() -> None:
    # Review focus (playbook plan): PAX-NOTICE, a storm's action, on Case 1's 20-min delay is
    # refused in plain words and the plan is built without it. An id outside the playbook, such
    # as HOTEL-HOLD since Act now was dropped, refuses the build once, as a lever not
    # offered (owner: no plan from levers the systems did not offer).
    seen: dict[str, Any] = {}

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        # Named twice, it is refused once (Task 1 review).
        try:
            tools.build_plan(
                trace,
                {
                    "aircraft_option_id": "delay",
                    "playbook_actions": ["RETIME", "HOTEL-HOLD", "HOTEL-HOLD"],
                },
            )
        except ToolRefused as refusal:
            seen["removed"] = str(refusal)
        plan = tools.build_plan(
            trace, {"aircraft_option_id": "delay", "playbook_actions": ["RETIME", "PAX-NOTICE"]}
        )
        seen["named"] = plan
        tools.submit_plan(trace, plan["plan_id"], "Within every domain's limits")
        return submitted(plan["plan_id"])

    case = run(delay(CASE_1), script)

    assert seen["removed"] == "Not built: playbook action HOTEL-HOLD is not in the playbook", seen
    assert len(case["options"]) == 1, case["options"]
    assert seen["named"]["refused_actions"] == ["PAX-NOTICE: does not apply to a flight delay"]
    plan = recommended(case)
    assert plan["policy"]["route"] == "auto", plan["policy"]
    assert (plan["playbook_actions"], plan["playbook_version"]) == (["RETIME"], PLAYBOOK_VERSION)
    rows = checklist_rows(plan)
    assert (rows["PAX-NOTICE"]["status"], rows["PAX-NOTICE"]["reason"]) == (
        "refused",
        "does not apply to a flight delay",
    ), rows["PAX-NOTICE"]
    assert rows["RETIME"]["status"] == "in_plan" and "FUEL-FLAG" not in rows, plan["playbook"]
    kinds = assert_completed(case)
    assert "retime_flights" in kinds and "notify_passengers" not in kinds, kinds


def check_dispatch_advice_that_cannot_be_read_never_stops_a_plan() -> None:
    # Task 3 review (M5): advice never blocks. When dispatch's advisories cannot be read, the
    # storm plan is still built and routed as before, its fuel and alternates advice shown as
    # not established.
    seen: dict[str, Any] = {}

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        tools.record_disruption(trace, storm_reading(STORM_DAY))
        gateway: Any = tools.gateway

        def unreachable(trace_id: str) -> dict[str, Any]:
            raise CloudDependencyError(
                "Cloud dependency could not be reached; retry shortly.", retryable=True
            )

        gateway.dispatch_advisories = unreachable
        seen["plan"] = tools.build_plan(trace, storm_levers(tools, trace, "delay", STORM_PLAYBOOK))
        return {"outcome": "no_plan", "summary": "Built", "submitted_plan_id": None}

    with mock.patch("airline_systems.schedule.datetime", StormDayClock):
        operations, cases, _ = operate(OperatingDayStore(build_operating_day(STORM_DAY)), script)
        trace = operations.open({"type": "message", "text": taf(STORM_DAY)}, OPERATOR)["trace_id"]
        operations.run(trace)

    [plan] = cases.get(trace)["options"]
    assert (plan["plan_id"], plan["policy"]["route"]) == (seen["plan"]["plan_id"], "occ")
    shown = checklist_rows(plan)
    assert {
        key: (shown[key]["status"], shown[key]["facts"]) for key in ("FUEL-FLAG", "ALT-FLAG")
    } == {
        "FUEL-FLAG": ("advice", "dispatch advice not established"),
        "ALT-FLAG": ("advice", "dispatch advice not established"),
    }, plan["playbook"]
    assert shown["PAX-NOTICE"]["status"] == "in_plan", shown["PAX-NOTICE"]


def check_an_occ_plan_submitted_before_the_other_offered_options_is_sent_back_once() -> None:
    """Owner: OCC compares complete alternatives. Live, the orchestrator submitted AR689's delay
    (OCC: Ponta Delgada's last standby crew) without the two cancellations Schedule offered.
    ARROW sends it back once, naming them; once they are built it is recommended. Submitted
    early a second time, it is recorded, and OCC sees the two options not assessed."""
    seen: dict[str, Any] = {}

    def delay_plan(tools: ArrowTools, trace: str) -> str:
        levers = {
            "aircraft_option_id": "delay",
            "reserve_option_ids": standby_ids(tools, trace, "delay"),
        }
        plan_id: str = tools.build_plan(trace, levers)["plan_id"]
        return plan_id

    def complete(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        plan_id = delay_plan(tools, trace)
        seen["first"] = tools.submit_plan(trace, plan_id, "A standby crew keeps the duty legal")
        seen["recorded"] = "recommended" in str(tools.store.get(trace)["options"])
        for option in ("cancel", "cancel-last"):
            tools.build_plan(trace, {"aircraft_option_id": option})
        seen["second"] = tools.submit_plan(trace, plan_id, "A standby crew keeps the duty legal")
        return submitted(plan_id)

    def early(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        plan_id = delay_plan(tools, trace)
        seen["early"] = [
            tools.submit_plan(trace, plan_id, "A standby crew keeps the duty legal")
            for _ in range(2)
        ]
        return submitted(plan_id)

    report = {**delay(PDL_BREACH), "cause": "late_inbound", "description": "Late inbound aircraft"}
    case = run(report, complete)
    first, second = seen["first"], seen["second"]
    assert first["status"] == "reconsider" and not seen["recorded"], first
    assert [o["option_id"] for o in first["unassessed_options"]] == ["cancel", "cancel-last"]
    assert second["status"] == "assessment_ready" and "unassessed_options" not in second, second
    assert len(case["options"]) == 3 and recommended(case)["aircraft_option_id"] == "delay"
    assert not case.get("unassessed_options") and "not assessed" not in case["stage"]

    case = run(report, early)
    sent_back, recorded = seen["early"]
    assert (sent_back["status"], recorded["status"]) == ("reconsider", "assessment_ready")
    assert [o["option_id"] for o in case["unassessed_options"]] == ["cancel", "cancel-last"]
    assert len(case["options"]) == 1 and recommended(case)["policy"]["route"] == "occ"
    assert case["stage"].endswith("; 2 offered options not assessed: cancel, cancel-last")
    logged = [e["event"] for e in case["events"] if e.get("actor") == "recovery-orchestrator"]
    assert logged.count("plan_reconsider") == 1 and "options_not_assessed" in logged, logged


class FourCaseTests(unittest.TestCase):
    def test_case_1_border_control_queue_20_min_runs_automatically(self) -> None:
        check_case_1_border_control_queue_20_min_runs_automatically()

    def test_case_1_calls_land_at_their_recipients(self) -> None:
        check_case_1_calls_land_at_their_recipients()

    def test_a_delay_past_midnight_is_accepted_by_eurocontrol(self) -> None:
        check_a_delay_past_midnight_is_accepted_by_eurocontrol()

    def test_a_summer_delay_message_reaches_eurocontrol_in_utc(self) -> None:
        check_a_summer_delay_message_reaches_eurocontrol_in_utc()

    def test_case_2_staff_shortage_60_min_rebooks_and_runs_automatically(self) -> None:
        check_case_2_staff_shortage_60_min_rebooks_and_runs_automatically()

    def test_a_late_flight_makes_up_time_in_the_air_and_pays_for_the_fuel(self) -> None:
        check_a_late_flight_makes_up_time_in_the_air_and_pays_for_the_fuel()

    def test_a_short_delay_is_not_made_up_in_the_air(self) -> None:
        check_a_short_delay_is_not_made_up_in_the_air()

    def test_case_3_aog_ranks_plans_for_occ(self) -> None:
        check_case_3_aog_ranks_plans_for_occ()

    def test_case_4_crew_limit_uses_a_reserve_and_runs_automatically(self) -> None:
        check_case_4_crew_limit_uses_a_reserve_and_runs_automatically()

    def test_case_4_occ_may_choose_the_automatic_plan_over_the_submitted_one(self) -> None:
        check_case_4_occ_may_choose_the_automatic_plan_over_the_submitted_one()

    def test_the_last_standby_crew_of_a_base_goes_to_occ(self) -> None:
        check_the_last_standby_crew_of_a_base_goes_to_occ()

    def test_a_plan_stored_with_one_reserve_crew_still_reads_and_runs(self) -> None:
        check_a_plan_stored_with_one_reserve_crew_still_reads_and_runs()

    def test_crew_limit_without_a_reserve_offers_occ_the_cancellation(self) -> None:
        check_crew_limit_without_a_reserve_offers_occ_the_cancellation()

    def test_a_swap_the_crew_cannot_reach_is_blocked(self) -> None:
        check_a_swap_the_crew_cannot_reach_is_blocked()

    def test_crew_limit_without_a_reserve_needs_manual_intervention(self) -> None:
        check_crew_limit_without_a_reserve_needs_manual_intervention()

    def test_case_5_a_crosswind_read_from_a_taf_waits_for_occ(self) -> None:
        check_case_5_a_crosswind_read_from_a_taf_waits_for_occ()

    def test_a_storm_plan_within_every_limit_still_goes_to_occ(self) -> None:
        check_a_storm_plan_within_every_limit_still_goes_to_occ()

    def test_a_refused_passenger_notice_holds_back_nothing_else(self) -> None:
        check_a_refused_passenger_notice_holds_back_nothing_else()

    def test_a_storm_confirmed_before_approval_is_recorded_and_sent_once_approved(self) -> None:
        check_a_storm_confirmed_before_approval_is_recorded_and_sent_once_approved()

    def test_a_storm_worse_than_planned_goes_back_to_occ(self) -> None:
        check_a_storm_worse_than_planned_goes_back_to_occ()

    def test_a_forecast_dropped_while_the_now_part_is_sent_is_not_lost(self) -> None:
        check_a_forecast_dropped_while_the_now_part_is_sent_is_not_lost()

    def test_the_latest_storm_update_decides_until_the_held_part_is_released(self) -> None:
        check_the_latest_storm_update_decides_until_the_held_part_is_released()

    def test_the_latest_pre_approval_update_decides_on_approval(self) -> None:
        check_the_latest_pre_approval_update_decides_on_approval()

    def test_a_stand_down_after_an_interrupted_warning_learns_what_was_sent(self) -> None:
        check_a_stand_down_after_an_interrupted_warning_learns_what_was_sent()

    def test_a_dropped_forecast_stands_the_storm_down(self) -> None:
        check_a_dropped_forecast_stands_the_storm_down()

    def test_the_cancel_plan_holds_its_cancellations_too(self) -> None:
        check_the_cancel_plan_holds_its_cancellations_too()

    def test_standby_crews_that_are_not_enough_need_manual_intervention(self) -> None:
        check_standby_crews_that_are_not_enough_need_manual_intervention()

    def test_a_taf_is_read_in_code_on_a_summer_day(self) -> None:
        check_a_taf_is_read_in_code_on_a_summer_day()

    def test_the_playbook_lists_the_actions_that_apply_to_each_case(self) -> None:
        check_the_playbook_lists_the_actions_that_apply_to_each_case()

    def test_an_action_that_does_not_apply_is_refused_and_the_plan_still_built(self) -> None:
        check_an_action_that_does_not_apply_is_refused_and_the_plan_still_built()

    def test_dispatch_advice_that_cannot_be_read_never_stops_a_plan(self) -> None:
        check_dispatch_advice_that_cannot_be_read_never_stops_a_plan()

    def test_an_occ_plan_submitted_before_the_other_offered_options_is_sent_back_once(
        self,
    ) -> None:
        check_an_occ_plan_submitted_before_the_other_offered_options_is_sent_back_once()
