"""The Test Bench cases' trail check (tools/testbench/check_cases.py) passes a clean automatic case
and reports a case id sent to a system, an option asked about before the last one was built, a
plan built after an automatic one, a system write outside the executed plan and an executed plan
that did not record the reported disruption first."""

from __future__ import annotations

import copy
import json
from datetime import date
from typing import Any

from testbench.presets import PRESETS
from tools.testbench.check_cases import check

PLAN = "7471cda7346a6694"
RECORD = "abababababab-1a2b3c4d-01-record-disruption"
RETIME = "abababababab-1a2b3c4d-02-retime-flights-ar259"
# ARROW's workers (the only writer) and its tools app, as the systems audit them (upper case).
WORKERS = "00000000-0000-4000-8000-000000000006"
TOOLS = "6E619BBC-53E7-4746-A39D-91BA706E1811"


def _ask(agent: str, text: str) -> dict[str, Any]:
    message = {"message": {"parts": [{"kind": "text", "text": text}]}}
    return {"type": "a2a_preview_call", "name": f"a2a-{agent}", "arguments": json.dumps(message)}


def _build(option: str, plan: str, route: str) -> dict[str, Any]:
    output = {"plan_id": plan, "route": route, "refused_actions": []}
    return {
        "type": "mcp_call",
        "name": "build_plan",
        "status": "completed",
        "arguments": json.dumps({"levers": {"aircraft_option_id": option}}),
        "output": json.dumps(output),
    }


def _case() -> dict[str, Any]:
    """Case 1 as a clean run leaves it: one automatic plan, its action confirmed."""
    text = PRESETS[0].data_on(date(2026, 11, 25))["text"]
    at = "2026-11-25T10:00:{:02d}Z".format
    return {
        "document": {
            "trace_id": "ab" * 16,
            "display_id": "DSR-20261125-ABCDEF",
            "operating_date": "2026-11-25",
            "created_at": "2026-11-25T10:00:00+00:00",
            "status": "assessment_ready",
            "source": {"event_time": "2026-11-25T10:00:00+00:00"},
            "disruption": {
                "type": "flight_delay",
                "flight_no": "AR259",
                "delay_minutes": 20,
                "cause": "border_control",
                "message": text,
            },
            "options": [
                {"plan_id": PLAN, "title": "Fly AR259 late", "aircraft_option_id": "delay"}
            ],
            "execution": {
                "status": "completed",
                "approved_by": "policy",
                "actions": [
                    {
                        "kind": "record_disruption",
                        "status": "confirmed",
                        "summary": "Record the AR259 delay: 20 min, border-control queue",
                        "calls": [
                            {"call_id": RECORD, "status": "confirmed", "event_id": "EV-12AB3C"}
                        ],
                    },
                    {
                        "kind": "retime_flights",
                        "status": "confirmed",
                        "calls": [{"call_id": RETIME, "status": "confirmed"}],
                    },
                ],
            },
            "events": [
                {"event": "assessment_progress", "actor": "detection", "at": at(2)},
                {
                    "event": "disruption_recorded",
                    "actor": "arrow-intake",
                    "message": "ARROW recorded the delay in the case; no airline system changed.",
                    "at": at(4),
                },
                {
                    "event": "agent_tool_call",
                    "actor": "detection",
                    "tool": "record_flight_delay",
                    "ok": True,
                    "response": {"recorded": "flight delay · AR259 · 20 min"},
                    "at": at(4),
                },
                {
                    "event": "agent_tool_call",
                    "actor": "passenger",
                    "tool": "get_compensation_exposure",
                    "request": {"option_id": "delay"},
                    "ok": True,
                    "at": at(20),
                },
                {
                    "event": "plan_built",
                    "actor": "recovery-orchestrator",
                    "message": f"Plan {PLAN}: auto; within every domain's limits",
                    "at": at(40),
                },
                {"event": "execution_completed", "actor": "recovery-executor", "at": at(50)},
            ],
            "_trail": {
                "items": [
                    _ask("aircraft", "AR259 will leave 20 minutes late."),
                    _ask("crew", "The delay option: AR259 flies 20 min late."),
                    _build("delay", PLAN, "auto"),
                ]
            },
        },
        "calls": [
            {
                "system": "schedule",
                "operation": "record_delay",
                "caller_oid": WORKERS,
                "outcome": "ok",
                "endpoint": "POST /api/delays",
                "request": json.dumps(
                    {"event_id": "EV-12AB3C", "request_id": RECORD, "flight_no": "AR259"}
                ),
            },
            {
                "system": "schedule",
                "operation": "update_flight",
                "caller_oid": WORKERS,
                "outcome": "ok",
                "endpoint": "PATCH /api/flights/{flight_no}",
                "request": json.dumps({"request_id": RETIME, "flight_no": "AR259"}),
            },
        ],
    }


def test_a_clean_automatic_case_passes() -> None:
    found = check(_case(), lambda name: {}, frozenset(), writer_oid=WORKERS)
    assert found["case"] == 1 and found["outcome"] == "Executed automatically"
    assert found["actions"] == "2 of 2 confirmed" and found["asked"] == {
        "delay": ["aircraft", "crew"]
    }
    assert found["detection_recorded"] == {
        "tool": "record_flight_delay",
        "event_id": None,
        "ok": True,
    }, found["detection_recorded"]
    assert found["writes_outside_plan"] == [], found["writes_outside_plan"]
    assert found["writes_not_by_workers"] == [], found["writes_not_by_workers"]
    assert found["disruption_record"]["event_id"] == "EV-12AB3C", found["disruption_record"]
    assert found["passed"], found


def test_it_reports_a_write_by_any_identity_but_the_workers() -> None:
    # Three container apps: only the workers' identity may write, and the run must prove it.
    case = copy.deepcopy(_case())
    case["calls"][1]["caller_oid"] = TOOLS
    found = check(case, lambda name: {}, frozenset(), writer_oid=WORKERS.lower())
    assert found["writes_not_by_workers"] == [
        f"schedule update_flight PATCH /api/flights/{{flight_no}} by {TOOLS}"
    ], found["writes_not_by_workers"]
    assert not found["passed"]


def test_it_reports_a_write_before_the_plan_and_a_plan_that_recorded_nothing() -> None:
    # The old flow: the delay recorded in operations control as the report arrived, outside the
    # plan, and the plan itself without its record.
    case = copy.deepcopy(_case())
    document = case["document"]
    document["execution"]["actions"].pop(0)
    case["calls"][0]["request"] = json.dumps({"event_id": "EV-12AB3C", "flight_no": "AR259"})
    found = check(case, lambda name: {}, frozenset(), writer_oid=WORKERS)
    assert found["writes_outside_plan"] == ["schedule record_delay POST /api/delays"], found
    assert found["disruption_record"]["ok"] is False, found["disruption_record"]
    assert not found["passed"]


def test_it_reports_a_case_id_sent_an_option_out_of_turn_and_a_plan_after_an_automatic_one() -> (
    None
):
    case = copy.deepcopy(_case())
    case["calls"].append(
        {
            "system": "crew",
            "operation": "get_reserve_options",
            "outcome": "ok",
            "endpoint": "MCP get_reserve_options",
            "request": json.dumps({"case": "DSR-20261125-ABCDEF"}),
        }
    )
    items = case["document"]["_trail"]["items"]
    items.insert(2, _ask("crew", "The cancel option: cancel AR259."))
    items.append(_build("cancel", "5a5a5a5a5a5a5a5a", "occ"))
    found = check(case, lambda name: {}, frozenset(), writer_oid=WORKERS)
    assert found["case_id_or_trace_sent"] == ["crew get_reserve_options MCP get_reserve_options"]
    assert found["one_option_at_a_time"] == [
        "asked about cancel before building delay",
        "built delay after asking about cancel",
        "built cancel after an automatic plan",
    ]
    assert not found["passed"]
