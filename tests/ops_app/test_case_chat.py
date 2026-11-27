"""Case chat (case chat spec): the conversation kept with a case, and the what-if plans the Case
Assistant asks ARROW for.

The what-ifs run on the demo cases on the generated operating day, as in test_four_cases; the
conversation on a small case waiting for OCC, as in test_orchestrated_flow. The orchestrator and
the Case Assistant are scripts; the planner, policy, store semantics, executor and mocked systems
are real. The Case Assistant's tool calls are made directly.
"""

from __future__ import annotations

import base64
import json
import time
import unittest
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

from arrow_scenario import PNRS, CaseMemory, ScenarioStore
from azure.identity import ManagedIdentityCredential
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_four_cases import (
    CASE_1,
    CASE_2,
    CASE_3,
    StormDayClock,
    delay,
    one_plan,
    recommended,
    run,
    storm_case,
)
from test_operator_flow import EXPERT, headers
from test_orchestrated_flow import (
    AGENT,
    CASE_ASSISTANT,
    OPERATOR,
    STAFF_DELAY,
    TRACE,
    Assistant,
    ScriptedGateway,
    insist,
    operate,
    submitted,
)

from airline_systems.caller import Caller
from ops_app.app import CLOSED_BY_TEST_RESET, create_app
from ops_app.case_digest import case_digest, plan_number
from ops_app.chat import MAX_ANSWER, Chat, ChatBusy
from ops_app.engine import CloudDependencyError, CloudGateway, Operations
from ops_app.runlog import record_run
from ops_app.store import DecisionConflict
from ops_app.tools import MAX_WHAT_IFS, WHAT_IF_REASON, ArrowTools, ToolRefused

ASKED_BY = {"oid": OPERATOR, "name": "Duty Manager"}
DUTY_MANAGER = Caller(OPERATOR, "Duty Manager")
DEFECT = {
    "type": "aircraft_defect",
    "aircraft_reg": CASE_3[0],
    "description": "Hydraulic leak",
    "unavailable_minutes": CASE_3[1],
}


def fewest_disrupted(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
    """Case 3's orchestrator: delay and cancel, recommending the one disrupting fewer people."""
    for kind in ("delay", "cancel"):
        tools.build_plan(trace, {"aircraft_option_id": kind})
    best = min(
        tools.store.get(trace)["options"],
        key=lambda p: (
            p["validation"]["rebooking"]["disrupted_passengers"],
            p["cost"]["total_eur"],
        ),
    )
    insist(tools, trace, best["plan_id"], "Fewest passengers disrupted")
    return submitted(best["plan_id"])


def assistant(case: dict[str, Any]) -> ArrowTools:
    tools: ArrowTools | None = case["_operations"].gateway.tools
    assert tools is not None
    return tools


def refusal(ask: Callable[[], object]) -> str:
    try:
        ask()
    except ToolRefused as error:
        return str(error)
    raise AssertionError("ARROW built a what-if plan it should have refused")


def check_a_what_if_adds_a_plan_only_occ_can_approve() -> None:
    case = run(DEFECT, fewest_disrupted)
    trace, operations, cases = case["trace_id"], case["_operations"], case["_cases"]
    chosen = recommended(case)
    delay_plan = next(plan for plan in case["options"] if plan["kind"] == "delay")
    question = "What if maintenance needs 30 more minutes?"

    result = assistant(case).what_if(
        trace, {"aircraft_option_id": "delay", "extra_delay_minutes": 30}, question, ASKED_BY, "e1"
    )

    doc = cases.get(trace)
    [added] = [plan for plan in doc["options"] if plan.get("what_if")]
    assert result["what_if"] is True and result["plan_id"] == added["plan_id"], result
    # The chat names plans by number: the orchestrator's keep theirs and the what-if comes last.
    before = [plan["option_id"] for plan in case["options"]]
    assert [plan["option_id"] for plan in doc["options"]] == [*before, added["option_id"]]
    assert len(doc["options"]) == 3 and added["policy"]["route"] == "occ", added["policy"]
    assert added["recommended"] is False and recommended(doc)["plan_id"] == chosen["plan_id"]
    # Maintenance needs 30 more minutes: the delay is at least 30 minutes, and later than the
    # orchestrator's (Slots needed 15 there; 30 minutes later LIS has room).
    assert added["adjustment_minutes"] >= 30, added["adjustment_minutes"]
    assert added["adjustment_minutes"] > delay_plan["adjustment_minutes"], (
        added["adjustment_minutes"],
        delay_plan["adjustment_minutes"],
    )
    asked = added["what_if"]
    assert (asked["question"], asked["asked_by"], asked["entry_id"]) == (question, ASKED_BY, "e1")
    assert asked["asked_at"], asked
    [built] = [event for event in doc["events"] if event["event"] == "what_if_built"]
    assert (built["actor"], built["autonomy"], built["plan_id"], built["entry_id"]) == (
        "case-assistant",
        "L2",
        added["plan_id"],
        "e1",
    ), built
    # The decision log names the turn; the question stays in the conversation.
    assert question not in json.dumps(doc["events"]), built
    assert doc["version"] > case["version"], (doc["version"], case["version"])
    # The Case Assistant names each plan as its card does, the system's wording beside it.
    plans = case_digest(doc)["plans"]
    assert [(p["number"], p["title"]) for p in plans] == [
        (1, "Cancel AR101 + AR102"),
        (2, "Delay the rotation"),
        (3, "Delay the rotation, 30 min more"),
    ], [(p["number"], p["title"]) for p in plans]
    assert [p["description"] for p in plans] == [p["title"] for p in doc["options"]], plans
    # A live check names the option in words and by the references the domain agents' systems
    # take (the disruption's facts, never ARROW's case), never by its card number.
    facts = '{"type": "aircraft_defect", "aircraft_reg": "CS-TQA", "unavailable_minutes": 240}'
    assert [p["live_check"] for p in plans] == [
        f"{option['title']}: disruption {facts}, option_id"
        f" {option['levers']['aircraft_option_id']},"
        f" additional_delay_minutes {option.get('adjustment_minutes') or 0}"
        for option in doc["options"]
    ], plans
    assert doc["display_id"] not in json.dumps([p["live_check"] for p in plans]), plans
    assert plans[2]["live_check"].endswith(
        f"additional_delay_minutes {added['adjustment_minutes']}"
    )

    # An approval opened before the what-if was added is stale and must be confirmed again.
    try:
        operations.record_decision(
            trace,
            option_id=delay_plan["option_id"],
            decision="approve",
            expected_version=case["version"],
            idempotency_key="decision-before-what-if-1",
            actor_oid=OPERATOR,
        )
    except DecisionConflict:
        pass
    else:
        raise AssertionError("OCC approved a plan against a case version before the what-if")
    operations.record_decision(
        trace,
        option_id=added["option_id"],
        decision="approve",
        expected_version=doc["version"],
        idempotency_key="decision-what-if-0001",
        actor_oid=OPERATOR,
    )
    operations.execute(trace)

    execution = cases.get(trace)["execution"]
    assert execution["status"] == "completed", execution
    assert (execution["option_id"], execution["approved_by"]) == (added["option_id"], OPERATOR)


def check_a_what_if_the_rules_would_run_automatically_waits_for_occ() -> None:
    # Case 2 (AR444, 70 min): the delay runs automatically when the orchestrator submits it.
    # Here the orchestrator submitted only the cancellation, so the case waits for OCC; the
    # delay asked for in chat is within every limit, yet waits for OCC too.
    def cancel_only(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        plan = tools.build_plan(trace, {"aircraft_option_id": "cancel"})
        tools.submit_plan(trace, plan["plan_id"], "Cancel the round trip")
        return submitted(plan["plan_id"])

    case = run(delay(CASE_2), cancel_only)
    trace, operations, cases = case["trace_id"], case["_operations"], case["_cases"]
    assert case["status"] == "assessment_ready" and "execution" not in case, case["status"]

    result = assistant(case).what_if(
        trace, {"aircraft_option_id": "delay"}, "What if we only delay it?", ASKED_BY, "e1"
    )

    doc = cases.get(trace)
    [added] = [plan for plan in doc["options"] if plan.get("what_if")]
    assert result["route"] == "occ" and result["reasons"] == [WHAT_IF_REASON], result
    assert added["policy"]["route"] == "occ", added["policy"]
    # Re-routed by code: no automatic wording ("OCC is informed") is left with it.
    assert added["policy"]["reasons"] == [WHAT_IF_REASON], added["policy"]["reasons"]
    assert "execution" not in doc and doc["status"] == "assessment_ready"
    operations.record_decision(
        trace,
        option_id=added["option_id"],
        decision="approve",
        expected_version=doc["version"],
        idempotency_key="decision-what-if-auto-1",
        actor_oid=OPERATOR,
    )
    operations.execute(trace)
    execution = cases.get(trace)["execution"]
    assert (execution["status"], execution["approved_by"]) == ("completed", OPERATOR), execution


def check_what_ifs_are_refused_outside_their_rules() -> None:
    ask = "What if it left 10 minutes later?"
    executed = run(delay(CASE_1), one_plan({"aircraft_option_id": "delay"}))
    message = refusal(
        lambda: assistant(executed).what_if(
            executed["trace_id"],
            {"aircraft_option_id": "delay", "extra_delay_minutes": 10},
            ask,
            ASKED_BY,
            "e1",
        )
    )
    assert message.startswith("Only a case waiting for OCC takes what-if plans"), message
    # Case 1 ran by itself: no plan was approved, yet one is being or has been carried out.
    assert message.endswith("a plan is already being or has been carried out"), message

    case = run(DEFECT, fewest_disrupted)
    tools, trace, cases = assistant(case), case["trace_id"], case["_cases"]

    def what_if(levers: dict[str, Any]) -> dict[str, Any]:
        return tools.what_if(trace, levers, ask, ASKED_BY, "e2")

    message = refusal(lambda: what_if({"aircraft_option_id": "standby"}))
    assert message.startswith("Not built: aircraft option standby not offered"), message
    message = refusal(lambda: what_if({"aircraft_option_id": "cancel", "extra_delay_minutes": 30}))
    assert message == "Not built: extra delay applies only to a delay option", message
    # The same levers as a plan already built: that plan, and nothing added.
    cancel = next(plan for plan in case["options"] if plan["kind"] == "cancel")
    assert what_if({"aircraft_option_id": "cancel"})["plan_id"] == cancel["plan_id"]
    assert cases.get(trace)["version"] == case["version"], "nothing was stored"

    # Asked longest delay first, so the order asked is not the order of cost.
    asked = [
        what_if({"aircraft_option_id": "delay", "extra_delay_minutes": minutes})["plan_id"]
        for minutes in range(10 * MAX_WHAT_IFS, 0, -10)
    ]
    message = refusal(lambda: what_if({"aircraft_option_id": "delay", "extra_delay_minutes": 60}))
    assert f"{MAX_WHAT_IFS} what-if plans" in message, message
    options = cases.get(trace)["options"]
    assert [plan["plan_id"] for plan in options] == [
        *(plan["plan_id"] for plan in case["options"]),
        *asked,
    ], [(plan["plan_id"], plan["cost"]["total_eur"]) for plan in options]

    assert cases.close(trace, {"event": "case_closed", "message": "Closed for the test"})
    message = refusal(lambda: what_if({"aircraft_option_id": "delay", "extra_delay_minutes": 5}))
    assert message.startswith("Only a case waiting for OCC takes what-if plans"), message
    assert "closed" in message, message


class Clock:
    def __init__(self) -> None:
        self.at = datetime(2026, 11, 21, 9, 45, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.at


def waiting_case() -> tuple[Operations, CaseMemory, ScriptedGateway, str]:
    """CS-TQA grounded on the small day: delay and cancel built, the cancellation (fewest
    passengers disrupted) recommended, and the case waiting for OCC."""

    def script(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        cancel = tools.build_plan(trace, {"aircraft_option_id": "cancel"})
        tools.build_plan(trace, {"aircraft_option_id": "delay"})
        insist(tools, trace, cancel["plan_id"], "Fewest passengers disrupted")
        return submitted(cancel["plan_id"])

    operations, cases, gateway = operate(ScenarioStore(TRACE), script)
    trace = operations.open(DEFECT, OPERATOR)["trace_id"]
    operations.run(trace)
    assert cases.get(trace)["status"] == "assessment_ready", cases.get(trace)["status"]
    return operations, cases, gateway, trace


def read_case(case_id: str) -> dict[str, Any]:
    return {
        "type": "mcp_call",
        "id": "mcp_1",
        "name": "read_case",
        "server_label": "arrow",
        "arguments": json.dumps({"case_id": case_id}),
        "output": json.dumps({"case_id": case_id, "plans": [{"number": 1}, {"number": 2}]}),
        "status": "completed",
    }


def answered(answer: str, *items: dict[str, Any]) -> dict[str, Any]:
    """The Case Assistant's answer with its run, as ARROW keeps a Foundry response."""
    message = {"type": "output_text", "text": json.dumps({"answer": answer})}
    response = {
        "id": "resp_chat_1",
        "model": "gpt-4.1-mini",
        "status": "completed",
        "output": [*items, {"type": "message", "content": [message]}],
    }
    return {"answer": answer, "refused": False, "trail": record_run(response)}


class Foundry(CloudGateway):
    """ARROW's real Case Assistant call, with Foundry's answers scripted at the HTTP boundary."""

    def __init__(self, *texts: str | list[str]) -> None:
        super().__init__(
            ManagedIdentityCredential(client_id=OPERATOR),
            "systems",
            "https://schedule",
            "https://maintenance",
            "https://foundry.test/api/projects/arrow",
            booking_url="https://booking",
        )
        self.texts = list(texts)
        self.sent: list[tuple[str, str, str, Any, float]] = []

    def request(
        self,
        url: str,
        scope: str,
        trace: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float = 240,
        in_case: bool = True,
        method: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        self.sent.append((url, scope, trace, body, timeout))
        # A run may say something before its answer (seen live): one message per text.
        said = self.texts.pop(0)
        texts = said if isinstance(said, list) else [said]
        return {
            "id": f"resp_foundry_{len(self.sent)}",
            "status": "completed",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": text}]}
                for text in texts
            ],
        }


def replies(*turns: str | Exception) -> Assistant:
    """A Case Assistant answering each call in turn after reading the case, or failing."""
    queue = list(turns)

    def assistant(trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        turn = queue.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return answered(turn, read_case(brief["case_id"]))

    return assistant


def chat_events(case: dict[str, Any]) -> list[tuple[str, str]]:
    return [(e["event"], e["entry_id"]) for e in case["events"] if e["event"].startswith("chat_")]


def busy(ask: Callable[[], object]) -> str:
    try:
        ask()
    except ChatBusy as error:
        return str(error)
    raise AssertionError("The chat took a question it should have refused")


def check_a_question_is_answered_and_kept_with_the_case() -> None:
    _, cases, gateway, trace = waiting_case()
    before = cases.get(trace)
    case_id = before["display_id"]
    crew = [
        {
            "type": "a2a_preview_call",
            "id": "a2a_1",
            "name": "a2a-crew",
            "arguments": json.dumps({"message": f"Are plan 2's duties within limits? {case_id}"}),
            "status": "completed",
        },
        {
            "type": "a2a_preview_call_output",
            "call_id": "a2a_1",
            "name": "a2a-crew",
            # A domain agent answers in JSON, as in Dev.
            "output": json.dumps(
                {
                    "recommendation": {"reserve_option_id": None, "reason": "No duty breach."},
                    "facts": {"constraint_status": "pass", "breaches": []},
                }
            ),
            "status": "completed",
        },
    ]
    added: list[str] = []

    def assistant(trace_id: str, brief: dict[str, Any]) -> dict[str, Any]:
        if not brief["question"].startswith("What if"):
            return answered(
                "Plan 2 adds up to the delay's care and compensation.", read_case(case_id), *crew
            )
        # Asked for a what-if: ARROW builds it for the turn that asked (Task 3 reads the turn).
        [turn] = [t for t in cases.get(trace_id)["chat"] if t["status"] == "pending"]
        levers = {"aircraft_option_id": "delay", "extra_delay_minutes": 30}
        assert gateway.tools is not None
        plan = gateway.tools.what_if(trace_id, levers, turn["question"], ASKED_BY, turn["id"])
        added.append(plan["plan_id"])
        # The tool call as Foundry records it: the agent's long question in the arguments, and
        # ARROW's answer (the plan's number, route and figures) as JSON.
        what_if = {
            "type": "mcp_call",
            "id": "mcp_2",
            "name": "what_if",
            "server_label": "arrow",
            "arguments": json.dumps(
                {"case_id": case_id, "levers": levers, "question": f"{turn['question']} " * 9}
            ),
            # ARROW's answer, as the MCP tool sends it: indented, number first, and as long as a
            # real one, so the run's record clips it (seen live).
            "output": json.dumps(
                {
                    "number": plan_number(cases.get(trace_id), plan["plan_id"]),
                    **plan,
                    "notes": ["Passenger and crew figures as ARROW priced them."] * 120,
                },
                indent=2,
            ),
            "status": "completed",
        }
        return answered(
            'I asked ARROW for the what-if: the new plan is number 3, "Delay the rotation, 30 min'
            ' more", and it waits for OCC.',
            read_case(case_id),
            what_if,
        )

    gateway.assistant = assistant
    chat = Chat(cases, gateway, now=Clock(), sleep=lambda seconds: None)

    entry = chat.ask(trace, "  What makes up the cost?  ", DUTY_MANAGER)
    assert (entry["status"], entry["question"]) == ("pending", "What makes up the cost?"), entry
    chat.answer(trace, entry["id"])

    case = cases.get(trace)
    [kept] = case["chat"]
    assert kept["status"] == "answered" and kept["answer"].startswith("Plan 2 adds up"), kept
    assert (kept["asked_by"], kept["response_id"], kept["error"]) == (ASKED_BY, "resp_chat_1", None)
    assert kept["answered_at"] and kept["what_if_plan_ids"] == [], kept
    assert [(c["kind"], c["name"], c["access"]) for c in kept["calls"]] == [
        ("tool", "read_case", "read"),
        ("agent", "crew-recovery-agent", "read"),
    ], kept["calls"]
    # Each call in words with its endpoint: the agent's answer by its reason, never its JSON.
    assert [(c["endpoint"], c["detail"]) for c in kept["calls"]] == [
        ("MCP read_case", case_id),
        ("A2A a2a-crew", f"Are plan 2's duties within limits? {case_id} → No duty breach."),
    ], kept["calls"]
    assert gateway.questions == [
        {
            "case_id": case_id,
            "case": case_digest(cases.get(trace)),
            "question": "What makes up the cost?",
            "asked_by": "Duty Manager",
            "earlier": [],
        }
    ]
    # A question never changes the case version: an approval opened before it stays valid.
    assert case["version"] == before["version"], (case["version"], before["version"])
    # The log names the turn; the question stays in the conversation.
    assert chat_events(case) == [("chat_asked", entry["id"]), ("chat_answered", entry["id"])]
    assert "What makes up the cost" not in json.dumps(case["events"])

    asked = chat.ask(trace, "What if maintenance needs 30 more minutes?", DUTY_MANAGER)
    chat.answer(trace, asked["id"])

    case = cases.get(trace)
    turn = case["chat"][1]
    assert turn["status"] == "answered" and len(added) == 1, turn
    assert turn["what_if_plan_ids"] == added, (turn["what_if_plan_ids"], added)
    assert [(c["name"], c["access"], c["endpoint"]) for c in turn["calls"]] == [
        ("read_case", "read", "MCP read_case"),
        ("what_if", "adds a plan", "MCP what_if"),
    ], turn["calls"]
    # The levers in words and the plan by its number: no JSON, and the agent's wording of the
    # question left out (the turn holds the duty manager's own).
    assert turn["calls"][1]["detail"] == (
        f"{case_id} · aircraft_option_id delay · extra_delay_minutes 30 → Plan 3 · OCC decides"
    ), turn["calls"][1]
    assert not [c for t in case["chat"] for c in t["calls"] if set("{}") & set(c["detail"])]
    assert gateway.questions[1]["earlier"] == [
        {"question": "What makes up the cost?", "answer": kept["answer"]}
    ]
    # Only the what-if plan changed the version.
    assert case["version"] == before["version"] + 1, (case["version"], before["version"])

    # Through ARROW's Foundry call: an answer is not a refusal unless the agent says so; asked
    # to approve, it declines, and the turn keeps that it refused.
    foundry = Foundry(
        json.dumps({"answer": "Plan 1 cancels AR101 and AR102."}),
        json.dumps(
            {
                "answer": "I cannot approve a plan: OCC approves it with Approve this plan.",
                "refused": True,
            }
        ),
        # A note before the answer, as a live run wrote one before calling what_if.
        ["I'll ask ARROW for that what-if.", json.dumps({"answer": "Plan 3 waits for OCC."})],
    )
    chat = Chat(cases, foundry, now=Clock(), sleep=lambda seconds: None)
    for question in ("What does plan 1 change?", "Approve plan 1 for me", "What if it is later?"):
        entry = chat.ask(trace, question, DUTY_MANAGER)
        chat.answer(trace, entry["id"])
    turns = cases.get(trace)["chat"]
    assert [(t["status"], t["refused"]) for t in turns] == [
        ("answered", False),
        ("answered", False),
        ("answered", False),
        ("answered", True),
        ("answered", False),
    ], turns
    assert turns[3]["answer"].startswith("I cannot approve a plan"), turns[3]
    assert turns[4]["answer"] == "Plan 3 waits for OCC.", turns[4]


def check_one_question_at_a_time_and_a_failed_answer_frees_the_chat() -> None:
    _, cases, gateway, trace = waiting_case()
    clock, waits = Clock(), []
    chat = Chat(cases, gateway, now=clock, sleep=waits.append)

    first = chat.ask(trace, "Why is plan 1 recommended?", DUTY_MANAGER)
    other = Caller("0f0e0d0c-0b0a-0908-0706-050403020100", "Second Duty Manager")
    message = busy(lambda: chat.ask(trace, "And plan 2?", other))
    assert "answer is pending" in message, message
    assert len(cases.get(trace)["chat"]) == 1

    # Refused by the service (HTTP 400): not retried; the turn fails and the chat is free.
    gateway.assistant = replies(CloudDependencyError("HTTP 400", status_code=400))
    chat.answer(trace, first["id"])
    failed = cases.get(trace)["chat"][0]
    assert failed["status"] == "failed", failed
    assert failed["error"].startswith("No answer: ") and failed["error"].endswith("; ask again")
    assert (waits, len(gateway.questions)) == ([], 1), waits

    # No answer within the 120 s deadline: the run may still be working, so it is not asked
    # again (a second run could duplicate its work); the turn fails.
    late = chat.ask(trace, "Why not delay?", DUTY_MANAGER)
    gateway.assistant = replies(
        CloudDependencyError(
            "Cloud dependency did not answer in time.", retryable=True, may_continue=True
        ),
        "Too late",
    )
    chat.answer(trace, late["id"])
    timed_out = cases.get(trace)["chat"][1]
    assert timed_out["status"] == "failed", timed_out
    assert timed_out["error"] == "No answer: the Case Assistant did not answer in time; ask again"
    assert (waits, len(gateway.questions)) == ([], 2), waits

    # Busy (429): asked once more after 15 s; busy again, the turn fails.
    second = chat.ask(trace, "And plan 2?", DUTY_MANAGER)
    gateway.assistant = replies(
        CloudDependencyError("HTTP 429", status_code=429, retryable=True),
        CloudDependencyError("HTTP 429", status_code=429, retryable=True),
        "Too late",
    )
    chat.answer(trace, second["id"])
    assert cases.get(trace)["chat"][2]["status"] == "failed"
    assert (waits, len(gateway.questions)) == ([15.0], 4), waits

    # A live check the Case Assistant asked for failed (Foundry answers HTTP 424): the turn says
    # so, and it is not asked again.
    unchecked = chat.ask(trace, "Is the crew rested enough?", DUTY_MANAGER)
    gateway.assistant = replies(CloudDependencyError("HTTP 424", status_code=424), "Too late")
    chat.answer(trace, unchecked["id"])
    turn = cases.get(trace)["chat"][3]
    assert turn["error"] == "No answer: a live check it asked for failed; ask again", turn
    assert (waits, len(gateway.questions)) == ([15.0], 5), waits

    # An answer naming a plan the case does not have (a live run described a what-if it never
    # asked ARROW for) is not kept: the turn fails and says why.
    invented = chat.ask(trace, "What if maintenance needs 30 more minutes?", DUTY_MANAGER)
    gateway.assistant = replies("Plan 3 adds 30 minutes and costs €116,299; it waits for OCC.")
    chat.answer(trace, invented["id"])
    turn = cases.get(trace)["chat"][4]
    assert (turn["status"], turn["answer"]) == ("failed", None), turn
    assert turn["error"] == (
        "No answer: the Case Assistant named a plan this case does not have; ask again"
    ), turn

    # A turn pending for more than 5 minutes (ARROW restarted) fails when the next one arrives.
    stuck = chat.ask(trace, "Is the crew within limits?", DUTY_MANAGER)
    clock.at += timedelta(minutes=5, seconds=1)
    fresh = chat.ask(trace, "Is the crew within limits?", DUTY_MANAGER)
    turns = {turn["id"]: turn for turn in cases.get(trace)["chat"]}
    assert turns[stuck["id"]]["status"] == "failed", turns[stuck["id"]]
    assert turns[stuck["id"]]["error"] == "No answer (interrupted); ask again"
    assert turns[fresh["id"]]["status"] == "pending"
    failures = [entry for event, entry in chat_events(cases.get(trace)) if event == "chat_failed"]
    assert failures == [
        first["id"],
        late["id"],
        second["id"],
        unchecked["id"],
        invented["id"],
        stuck["id"],
    ]

    # Foundry answers with something other than {"answer": "<text>", "refused": <bool>}: the
    # turn fails and says so.
    foundry = Foundry(
        "Plan 1 cancels AR101 and AR102.",
        json.dumps({"answer": "Plan 1 cancels AR101 and AR102.", "refused": "no"}),
    )
    chat = Chat(cases, foundry, now=clock, sleep=waits.append)
    chat.answer(trace, fresh["id"])
    unsupported = chat.ask(trace, "What does plan 1 change?", DUTY_MANAGER)
    chat.answer(trace, unsupported["id"])
    for entry in (fresh, unsupported):
        turn = next(t for t in cases.get(trace)["chat"] if t["id"] == entry["id"])
        assert turn["status"] == "failed", turn
        assert turn["error"] == "No answer: the Case Assistant returned no answer; ask again"
    url, scope, trace_id, body, timeout = foundry.sent[0]
    assert url == (
        "https://foundry.test/api/projects/arrow/agents/case-assistant-agent/endpoint"
        "/protocols/openai/responses?api-version=v1"
    ), url
    assert (scope, trace_id, timeout, body["store"]) == (
        "https://ai.azure.com/.default",
        trace,
        120,
        True,
    )
    assert json.loads(body["input"][0]["content"])["question"] == "Is the crew within limits?"

    # A case keeps at most 50 turns.
    cases.update_chat(
        trace, lambda kept: [*kept, *({**turn, "id": f"old-{n}"} for n in range(50 - len(kept)))]
    )
    message = busy(lambda: chat.ask(trace, "One more?", DUTY_MANAGER))
    assert "50" in message, message


def check_earlier_turns_go_with_the_question() -> None:
    _, cases, gateway, trace = waiting_case()
    chat = Chat(cases, gateway, now=Clock(), sleep=lambda seconds: None)
    gateway.assistant = replies(
        *(
            CloudDependencyError("HTTP 400", status_code=400) if n == 4 else f"Answer {n}"
            for n in range(1, 10)
        )
    )
    for n in range(1, 10):
        entry = chat.ask(trace, f"Question {n}", DUTY_MANAGER)
        chat.answer(trace, entry["id"])

    # Seven turns answered before the ninth (the fourth failed): the last six go with it.
    assert gateway.questions[-1]["earlier"] == [
        {"question": f"Question {n}", "answer": f"Answer {n}"} for n in (2, 3, 5, 6, 7, 8)
    ], gateway.questions[-1]["earlier"]
    # The case goes with every question, so an answer never rests on earlier turns alone.
    assert gateway.questions[-1]["case"] == case_digest(cases.get(trace)), gateway.questions[-1]


def check_an_answer_after_reset_is_still_recorded() -> None:
    _, cases, gateway, trace = waiting_case()
    gateway.assistant = replies("Plan 1 cancels AR101 and AR102.")
    chat = Chat(cases, gateway, now=Clock(), sleep=lambda seconds: None)
    entry = chat.ask(trace, "What does plan 1 change?", DUTY_MANAGER)

    assert cases.close(trace, CLOSED_BY_TEST_RESET)
    chat.answer(trace, entry["id"])

    case = cases.get(trace)
    [kept] = case["chat"]
    assert case["status"] == "closed", case["status"]
    assert (kept["status"], kept["answer"]) == ("answered", "Plan 1 cancels AR101 and AR102.")


def check_the_chat_api() -> None:
    operations, cases, gateway, trace = waiting_case()
    gateway.assistant = replies(
        "Plan 1 cancels AR101 and AR102.", "Plan 1 rebooks the passengers of AR101 and AR102."
    )
    app = create_app(
        cases,
        operations,
        frozenset({OPERATOR}),
        frozenset(),
        "https://arrow.test",
        expert_oids=frozenset({EXPERT}),
    )
    url = f"/api/cases/{trace}/chat"
    with TestClient(app) as client:
        for question in ("", "   ", "x" * 501):
            refused = client.post(url, json={"question": question}, headers=headers())
            assert refused.status_code == 400, (len(question), refused.text)
        cross_site = {k: v for k, v in headers().items() if k != "X-ARROW-Request"}
        refused = client.post(url, json={"question": "Why?"}, headers=cross_site)
        assert refused.status_code == 403, refused.text
        outsider = headers("5e5e5e5e-0000-4000-8000-000000000001")
        refused = client.post(url, json={"question": "Why?"}, headers=outsider)
        assert refused.status_code == 403, refused.text
        assert "chat" not in cases.get(trace)

        accepted = client.post(
            url, json={"question": "What does plan 1 change?"}, headers=headers()
        )
        assert accepted.status_code == 202, accepted.text
        # The answer ran as a background task once the 202 was sent; the case shows it.
        [kept] = client.get(f"/api/cases/{trace}", headers=headers()).json()["chat"]
        assert (kept["id"], kept["status"]) == (accepted.json()["entry_id"], "answered"), kept
        assert kept["answer"] == "Plan 1 cancels AR101 and AR102."
        # Passenger Services may ask too: asking grants no authority.
        accepted = client.post(url, json={"question": "Who is rebooked?"}, headers=headers(EXPERT))
        assert accepted.status_code == 202, accepted.text
        kept = cases.get(trace)["chat"][1]
        assert (kept["asked_by"]["oid"], kept["status"]) == (EXPERT, "answered"), kept

        Chat(cases, gateway).ask(trace, "And plan 2?", DUTY_MANAGER)
        refused = client.post(url, json={"question": "Is the crew fine?"}, headers=headers())
        assert refused.status_code == 409 and "answer is pending" in refused.json()["detail"]
        assert len(cases.get(trace)["chat"]) == 3


def agents_app(operations: Operations, cases: CaseMemory) -> FastAPI:
    """ARROW admitting the orchestrator and the Case Assistant on /mcp, each by its identity."""
    return create_app(
        cases,
        operations,
        frozenset({OPERATOR}),
        frozenset(),
        "https://arrow.test",
        orchestrator_oids=frozenset({AGENT}),
        assistant_oids=frozenset({CASE_ASSISTANT}),
    )


def use(client: TestClient, oid: str, tool: str, arguments: dict[str, Any], run: str) -> Any:
    """One MCP tool call by an agent identity, from a run carrying ``run``'s trace."""
    claims = {"claims": [{"typ": "oid", "val": oid}]}
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        headers={
            "X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(claims).encode()).decode(),
            "Accept": "application/json, text/event-stream",
            "traceparent": f"00-{run}-{'1' * 16}-01",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


def error_text(result: Mapping[str, Any]) -> str:
    assert result.get("isError"), result
    return str(result["content"][0]["text"])


def output(result: Mapping[str, Any]) -> dict[str, Any]:
    assert not result.get("isError"), result
    found: dict[str, Any] = json.loads(result["content"][0]["text"])
    return found


def read(client: TestClient, case_id: str, run: str) -> dict[str, Any]:
    """The case as the Case Assistant reads it: the digest once, as one text of at most 16,000
    characters, the text the model is given."""
    result = use(client, CASE_ASSISTANT, "read_case", {"case_id": case_id}, run)
    assert not result.get("isError") and "structuredContent" not in result, sorted(result)
    [content] = result["content"]
    text = str(content["text"])
    assert len(text) <= 16_000, len(text)
    found: dict[str, Any] = json.loads(text)
    return found


def check_the_case_assistant_reads_its_case_and_cannot_plan_or_submit() -> None:
    operations, cases, _, trace = waiting_case()
    # One identity is never both agents: ARROW refuses to start with it in both lists.
    try:
        create_app(
            cases,
            operations,
            frozenset({OPERATOR}),
            frozenset(),
            "https://arrow.test",
            orchestrator_oids=frozenset({AGENT}),
            assistant_oids=frozenset({CASE_ASSISTANT, AGENT}),
        )
    except ValueError as error:
        assert AGENT in str(error), error
    else:
        raise AssertionError("ARROW started with one identity as both agents")
    case = cases.get(trace)
    case_id = case["display_id"]
    other = operations.open(STAFF_DELAY, OPERATOR)
    # A plan recorded infeasible before ARROW priced plans: the duty manager never sees it, so
    # it has no number, and "Plan 2" is the card OCC sees as 2.
    cases.docs[trace]["options"].insert(
        0, {**case["options"][0], "plan_id": "legacy", "option_id": "legacy", "feasible": False}
    )
    shown = case["options"]
    cancel = next(plan for plan in shown if plan["kind"] == "cancel")
    delayed = next(plan for plan in shown if plan["kind"] == "delay")

    with TestClient(agents_app(operations, cases)) as client:
        digest = read(client, case_id, trace)
        refusals = {
            tool: error_text(use(client, CASE_ASSISTANT, tool, {"case_id": case_id, **rest}, trace))
            for tool, rest in (
                ("build_plan", {"levers": {"aircraft_option_id": "delay"}}),
                ("submit_plan", {"plan_id": delayed["plan_id"], "reason": "Cheaper"}),
                ("record_case", {"summary": "CS-TQA is grounded", "findings": []}),
                (
                    "record_aircraft_defect",
                    {"aircraft_reg": "CS-TQA", "unavailable_minutes": 240, "reading": "Defect"},
                ),
            )
        }
        what_if = {
            "case_id": case_id,
            "levers": {"aircraft_option_id": "delay", "extra_delay_minutes": 30},
            "question": "What if maintenance needs 30 more minutes?",
        }
        orchestrator = error_text(use(client, AGENT, "what_if", what_if, trace))
        elsewhere = use(
            client, CASE_ASSISTANT, "read_case", {"case_id": other["display_id"]}, trace
        )

    text = json.dumps(digest)
    assert trace not in text and not [p[0] for p in PNRS if p[0] in text], text
    assert (digest["case_id"], digest["status"]) == (case_id, "assessment_ready"), digest
    assert digest["report"] == "Hydraulic leak" and "CS-TQA" in digest["reading"], digest
    assert [(p["number"], p["kind"], p["description"]) for p in digest["plans"]] == [
        (number, plan["kind"], plan["title"]) for number, plan in enumerate(shown, 1)
    ], digest["plans"]
    plan = digest["plans"][shown.index(cancel)]
    assert (plan["recommended"], plan["reason"]) == (True, "Fewest passengers disrupted"), plan
    assert (plan["route"], plan["reasons"]) == (
        cancel["policy"]["route"],
        cancel["policy"]["reasons"],
    )
    cost, rebooking = cancel["cost"], cancel["validation"]["rebooking"]
    assert plan["cost"] == {
        "total": cost["total_eur"],
        "airline": cost["airline_eur"],
        "compensation": cost["compensation_eur"],
        "care": cost["care_eur"],
        "rebooking": cost["rebooking_eur"],
        "workings": cost["workings"],
    }, plan["cost"]
    assert plan["passengers"] == {
        "disrupted": rebooking["disrupted_passengers"],
        "owed_compensation": cost["owed_compensation_passengers"],
        "meals": cost["meals_passengers"],
        "rebooked": rebooking["rebooked_passengers"],
        "passenger_services": rebooking["unplaced_passengers"],
        "by_car": rebooking["transferred_passengers"],
    }, plan["passengers"]
    assert [(f["flight_no"], f["status"]) for f in plan["flights"][:2]] == [
        ("AR101", "cancelled"),
        ("AR102", "cancelled"),
    ], plan["flights"]
    # The delay's flights with their old and new times; its crew and slot checks in words.
    late = digest["plans"][shown.index(delayed)]
    assert (late["flights"][0]["departs_was"], late["flights"][0]["departs"]) == (
        "10:30",
        "13:45",
    ), late["flights"][0]
    assert "D-LIS-001" in late["crew"] and "fits" in late["slots"], (late["crew"], late["slots"])
    assert late["what_if"] is None and not late["recommended"], late

    # Code decides which tools each identity may call; the run's case binds both.
    assert all(
        text.endswith("This tool is not available to the Case Assistant")
        for text in refusals.values()
    ), refusals
    assert orchestrator.endswith("This tool is not available to the orchestrator"), orchestrator
    assert "another case" in error_text(elsewhere), elsewhere
    after = cases.get(trace)
    assert (after["version"], after["plans_submitted"]) == (
        case["version"],
        case["plans_submitted"],
    ), after["version"]
    assert "summary" not in after or after["summary"] == case.get("summary")

    # Once OCC approves, the Case Assistant reads what ARROW carried out, with the receipts.
    operations.record_decision(
        trace,
        option_id=cancel["option_id"],
        decision="approve",
        expected_version=case["version"],
        idempotency_key="decision-read-case-01",
        actor_oid=OPERATOR,
    )
    operations.execute(trace)
    actions = cases.get(trace)["execution"]["actions"]
    with TestClient(agents_app(operations, cases)) as client:
        executed = read(client, case_id, trace)
    done = executed["executed"]
    assert len(done) == len(actions) and {a["status"] for a in done} == {"confirmed"}, done
    # The defect first reached Maintenance, then the flights changed.
    assert done[0]["label"] == "Maintenance", done[0]
    assert done[0]["message"].endswith(": CS-TQA out of service until 13:45"), done[0]
    assert done[1] == {
        "label": "Ops control",
        "status": "confirmed",
        "message": "AR101 cancelled; AR102 cancelled",
    }, done[1]
    assert trace not in json.dumps(executed)


def check_a_what_if_through_the_chat_names_who_asked() -> None:
    operations, cases, gateway, trace = waiting_case()
    case_id = cases.get(trace)["display_id"]
    question = "What if maintenance needs 30 more minutes?"

    with TestClient(agents_app(operations, cases)) as client:
        # The Case Assistant asks from a plan's own levers: the delay, 30 minutes later.
        before = read(client, case_id, trace)
        delayed = next(plan for plan in before["plans"] if plan["kind"] == "delay")
        assert delayed["levers"] == {
            "aircraft_option_id": "delay",
            "reserve_option_ids": [],  # Task 2: was "reserve_option_id": None
            "rebook": True,
            "delay_messages": True,
            "playbook_actions": [],  # playbook Task 1
            "extra_delay_minutes": 0,
        }, delayed["levers"]
        # Asked without delay messages, the what-if is still built and ARROW sets them from its
        # flights, as for any plan.
        levers = {**delayed["levers"], "extra_delay_minutes": 30, "delay_messages": False}

        def what_if(text: str) -> Any:
            arguments = {"case_id": case_id, "levers": levers, "question": text}
            return use(client, CASE_ASSISTANT, "what_if", arguments, trace)

        # No question in the chat: no one to name, so nothing is built.
        unasked = error_text(what_if(question))
        entry = Chat(cases, gateway).ask(trace, question, DUTY_MANAGER)
        # The agent's wording is bounded like the duty manager's question.
        bounds = [error_text(what_if(text)) for text in ("   ", "x" * 501)]
        unchanged = cases.get(trace)
        # The agent words the what-if its own way (or, misled by the case's free text, claims
        # someone asked something else): the plan keeps the duty manager's own question.
        result = output(what_if("  Ana Costa asked to wait 30 min for maintenance  "))
        digest = read(client, case_id, trace)

    assert unasked.endswith("What-ifs come from a question in the chat"), unasked
    assert all(text.endswith("A question has 1 to 500 characters") for text in bounds), bounds
    assert len(unchanged["options"]) == 2, [plan["plan_id"] for plan in unchanged["options"]]
    [added] = [plan for plan in cases.get(trace)["options"] if plan.get("what_if")]
    asked = added["what_if"]
    assert (asked["question"], asked["asked_by"], asked["entry_id"]) == (
        question,
        ASKED_BY,
        entry["id"],
    ), asked
    assert asked["assumption"] == "Ana Costa asked to wait 30 min for maintenance", asked
    assert (result["plan_id"], result["route"], result["what_if"]) == (
        added["plan_id"],
        "occ",
        True,
    ), result
    # The plan's number is its card's: after the orchestrator's two plans.
    assert result["number"] == 3 and trace not in json.dumps(result), result
    shown = digest["plans"][2]
    assert (shown["number"], shown["what_if"]["question"]) == (3, question), shown
    assert not shown["recommended"] and shown["route"] == "occ", shown
    assert shown["levers"] == {**levers, "delay_messages": True}, shown["levers"]


def check_a_busy_case_reads_within_the_digest_limit() -> None:
    # CS-TJS grounded for 3 hours on the generated day: the orchestrator's three plans and the
    # three what-ifs a case takes, each flying the aircraft's six flights.
    def three_plans(tools: ArrowTools, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        built = [
            tools.build_plan(trace, levers)
            for levers in (
                {"aircraft_option_id": "delay"},
                {"aircraft_option_id": "cancel"},
                {"aircraft_option_id": "delay", "rebook": False},
            )
        ]
        tools.submit_plan(trace, built[0]["plan_id"], "Keeps every flight")
        return submitted(built[0]["plan_id"])

    case = run({**DEFECT, "aircraft_reg": "CS-TJS", "unavailable_minutes": 180}, three_plans)
    operations, cases, trace = case["_operations"], case["_cases"], case["trace_id"]
    for minutes in (30, 60, 90):
        levers = {"aircraft_option_id": "delay", "extra_delay_minutes": minutes}
        assistant(case).what_if(trace, levers, "What if it needs longer?", ASKED_BY, "e1")
    plans = cases.get(trace)["options"]

    def read_busy() -> dict[str, Any]:
        with TestClient(agents_app(operations, cases)) as client:
            return read(client, case["display_id"], trace)

    # Within the limit on the text the Case Assistant is given (read checks it).
    digest = read_busy()
    assert [p["number"] for p in digest["plans"]] == [1, 2, 3, 4, 5, 6], digest["plans"]
    # The cost workings go first: every plan keeps its cost parts and lists all its flights.
    assert [(p["cost"]["total"], "workings" in p["cost"]) for p in digest["plans"]] == [
        (plan["cost"]["total_eur"], False) for plan in plans
    ]
    assert [len(p["flights"]) for p in digest["plans"]] == [6] * 6, digest["plans"]

    # Longer rotations: each plan lists its first 8 flights and says how many more it flies.
    for plan in cases.docs[trace]["options"]:
        plan["flights"] = plan["flights"] * 3
    digest = read_busy()
    assert [(len(p["flights"]), p.get("flights_not_listed")) for p in digest["plans"]] == [
        (8, 10)
    ] * 6, digest["plans"]


def check_an_answer_is_shown_only_for_plans_arrow_has() -> None:
    # Code keeps each answer to the plans ARROW has (case chat spec §3). Live (Dev, Case 3) the
    # Case Assistant answered "What if maintenance needs 30 more minutes?" with "The new plan is
    # number 3" and figures it never asked ARROW for; a review then found declines, and
    # follow-ups about a what-if ARROW had built, not shown. Each row is one chat turn: what the
    # Case Assistant's run did (no tool; a what_if ARROW built with that many extra minutes; a
    # what_if ARROW refused; one that never reached ARROW), its answer, and whether the chat
    # shows it or why not.
    _, cases, gateway, trace = waiting_case()
    before = cases.get(trace)
    shown = None
    named = "No answer: the Case Assistant named a plan this case does not have; ask again"
    made = "No answer: the Case Assistant reported a what-if plan ARROW did not build; ask again"
    rows: list[tuple[str | int, str, str | None]] = [
        # Two plans and no what-if yet: answers about them, declines and a refusal are shown.
        (
            "none",
            "Plans 1 and 2 both wait for OCC: the first plan cancels AR101 + AR102 and plan 2"
            " delays the rotation 240 minutes.",
            shown,
        ),
        ("none", "I cannot approve a plan: OCC approves it with Approve this plan.", shown),
        (
            "none",
            "Plan 2 already waits for maintenance's estimate, so a new plan cannot be built for"
            " 30 more minutes.",
            shown,
        ),
        (
            "none",
            "Weather plans take no added delay, so a new plan holding the crosswind flights 30"
            " minutes more is not possible.",
            shown,
        ),
        ("none", "ARROW hasn’t built a new plan: this case has plans 1 and 2.", shown),
        ("none", "I cannot ask ARROW for a new plan with 30 more minutes.", shown),
        ("none", "Plan 2 is recommended; a new plan was not needed.", shown),
        ("none", "Ask a what-if if you want a new plan with more delay.", shown),
        (
            "refused",
            "ARROW refused the what-if: the cancellation takes no added delay, so the new plan"
            " was not added.",
            shown,
        ),
        # Invented what-ifs: no tool called, and a plan or figures ARROW never built.
        (
            "none",
            "I have run a what-if for the delay plan with 30 more minutes of maintenance (total"
            ' 270 min). The new plan is number 3, "Delay the rotation +30 min". Its cost is'
            " €123,242. 77 passengers lose their flight or connection.",
            named,
        ),
        ("none", "Plan #3 delays the rotation 30 minutes more for €123,242.", named),
        ("none", "The what-if is plan no. 3: €123,242, and it waits for OCC.", named),
        (
            "none",
            "I have run a what-if for 30 more minutes of maintenance: the delay grows to 270"
            " minutes and costs €123,242; it waits for OCC.",
            made,
        ),
        ("none", "I’ve run a what-if for 30 more minutes of maintenance: it costs €123,242.", made),
        (
            "none",
            "I asked ARROW for the what-if: the delay grows to 270 minutes and costs €123,242.",
            made,
        ),
        (
            "none",
            "With 30 more minutes of maintenance, a what-if plan would be added: €123,242.",
            made,
        ),
        (
            "none",
            "The new plan delays the rotation 30 minutes more, costs €123,242 and waits for OCC"
            " to approve or decline it.",
            made,
        ),
        # Second review: a negation, offer or refusal outside the claim's own clause excuses
        # nothing, nor does a modal before a verb other than ask or request.
        ("none", "Plan 2 is not recommended, but the new plan costs €123,242.", made),
        (
            "none",
            "Plan 2 isn’t recommended — the new plan costs €123,242 and waits for OCC.",
            made,
        ),
        ("none", "No, the new plan costs €123,242 and waits for OCC.", made),
        (
            "none",
            "Without a standby crew, the new plan delays the rotation to 270 minutes and costs"
            " €123,242.",
            made,
        ),
        ("none", "If maintenance doesn’t finish on time, the new plan costs €123,242.", made),
        (
            "none",
            "Plan 2 can’t absorb 30 more minutes, so the new plan delays the rotation to 270"
            " minutes for €123,242.",
            made,
        ),
        (
            "none",
            "Neither plan covers 30 more minutes, so the new plan delays the rotation to 270"
            " minutes for €123,242.",
            made,
        ),
        ("none", "The new plan costs €123,242, and a standby crew isn’t needed.", made),
        (
            "none",
            "The new plan costs €123,242 and I can compare it with Plan 2 if you like.",
            made,
        ),
        (
            "none",
            "The new plan costs €123,242 and waits for OCC to approve or decline the what-if.",
            made,
        ),
        ("none", "**Plan 2 is not recommended.** The new plan costs €123,242.", made),
        ("none", "I can confirm the new plan costs €123,242 and waits for OCC.", made),
        (
            "none",
            "OCC should approve the new plan, which costs €123,242 less than Plan 1.",
            made,
        ),
        ("none", "You can find the new plan in Decision, priced at €123,242.", made),
        ("none", "This would add a new plan costing €123,242 that waits for OCC.", made),
        ("none", "ARROW will add a new plan: it costs €123,242.", made),
        ("none", "With 30 more minutes, you get a new plan costing €123,242.", made),
        ("none", "I’ll create a new plan for 30 more minutes, priced at €123,242.", made),
        (
            "none",
            "With 30 more minutes of maintenance, a what-if plan “Delay the rotation +30 min”"
            " would be added: €123,242.",
            made,
        ),
        # A long run of spaces is read as one (it once took 20 s), within the 4,000 characters
        # the chat keeps of an answer.
        ("none", "plan" + " " * 20_000 + "x", shown),
        # Plain declines: an indefinite new plan that points to a what-if question, or says when
        # one is possible.
        (
            "none",
            "A new plan is only possible while the case waits for OCC; this case is closed.",
            shown,
        ),
        ("none", "For a new plan, ask me a what-if question.", shown),
        ("none", "A new plan would need a what-if: ask me one.", shown),
        # A what_if call that never reached ARROW (the connection failed) built nothing; one
        # ARROW refused lets the answer say so.
        ("failed", "I’ve run a what-if: the new plan costs €123,242.", made),
        (
            "refused",
            "I asked ARROW for the what-if: the cancellation takes no added delay.",
            shown,
        ),
        # ARROW builds a what-if, Plan 3: follow-ups about it, asked twice, are shown.
        (
            30,
            'I asked ARROW for the what-if: the new plan is number 3, "Delay the rotation, 30 min'
            ' more", and it waits for OCC.',
            shown,
        ),
        (
            "none",
            "The new plan, Plan 3 (Delay the rotation, 30 min more), costs more than Plan 2 and"
            " waits for OCC.",
            shown,
        ),
        ("none", "The new plan, Plan 3, delays the rotation 30 minutes more than Plan 2.", shown),
        ("none", "The new plan is number 4 and costs €250,000.", named),
        # Two more what-ifs, Plans 4 and 5, then one past the limit, which ARROW refuses.
        (60, "I asked ARROW for the what-if: the new plan is number 4 and waits for OCC.", shown),
        (90, "I asked ARROW for the what-if: the new plan is number 5 and waits for OCC.", shown),
        (
            "refused",
            "ARROW refused the what-if: this case already has 3 what-if plans (Plans 3, 4 and 5);"
            " the new plan was not added.",
            shown,
        ),
    ]
    steps = [step for step, _, _ in rows]
    did: list[tuple[str, Any]] = []

    def assistant(trace_id: str, brief: dict[str, Any]) -> dict[str, Any]:
        step, answer, _ = rows[len(cases.get(trace_id)["chat"]) - 1]
        if step == "none":
            return answered(answer, read_case(brief["case_id"]))
        # A what-if through ARROW's own tool, for the turn that asked; a cancellation takes no
        # added delay, so ARROW refuses that one.
        [turn] = [t for t in cases.get(trace_id)["chat"] if t["status"] == "pending"]
        levers: dict[str, Any] = {
            "aircraft_option_id": "delay" if isinstance(step, int) else "cancel",
            "extra_delay_minutes": step if isinstance(step, int) else 30,
        }
        arguments = {"case_id": brief["case_id"], "levers": levers, "question": turn["question"]}
        call: dict[str, Any] = {
            "type": "mcp_call",
            "id": f"mcp_{turn['id']}",
            "name": "what_if",
            "server_label": "arrow",
            "arguments": json.dumps(arguments),
            "status": "completed",
        }
        if step == "failed":
            # As Foundry records a call it could not deliver: ARROW never received it.
            call.update(status="failed", error="Connection reset by peer")
            return answered(answer, read_case(brief["case_id"]), call)
        assert gateway.tools is not None
        try:
            plan = gateway.tools.what_if(trace_id, levers, turn["question"], ASKED_BY, turn["id"])
        except ToolRefused as error:
            did.append(("refused", str(error)))
            call["output"] = f"Error executing tool what_if: {error}"
        else:
            number = plan_number(cases.get(trace_id), plan["plan_id"])
            did.append(("built", number))
            call["output"] = json.dumps({"number": number, **plan})
        return answered(answer, read_case(brief["case_id"]), call)

    gateway.assistant = assistant
    chat = Chat(cases, gateway, now=Clock(), sleep=lambda seconds: None)
    seconds: list[float] = []
    for step in steps:
        asked = f"What if maintenance needs {step} more minutes?" if isinstance(step, int) else None
        entry = chat.ask(trace, asked or "And the plans now?", DUTY_MANAGER)
        started = time.perf_counter()
        chat.answer(trace, entry["id"])
        seconds.append(time.perf_counter() - started)

    case = cases.get(trace)
    assert [(t["status"], t["answer"], t["error"]) for t in case["chat"]] == [
        ("answered", answer[:MAX_ANSWER], None) if why is None else ("failed", None, why)
        for _, answer, why in rows
    ], [(t["status"], t["answer"], t["error"]) for t in case["chat"]]
    assert max(seconds) < 2, max(seconds)
    # ARROW refused the cancellation's added delay twice, built Plans 3, 4 and 5, then refused a
    # fourth what-if; each built plan is kept on the turn that asked for it.
    assert did[:5] == [
        ("refused", "Not built: extra delay applies only to a delay option"),
        ("refused", "Not built: extra delay applies only to a delay option"),
        ("built", 3),
        ("built", 4),
        ("built", 5),
    ], did
    assert did[5][0] == "refused" and "3 what-if plans" in did[5][1], did
    assert [len(t["what_if_plan_ids"]) for t in case["chat"]] == [
        1 if isinstance(step, int) else 0 for step in steps
    ]
    assert [bool(plan.get("what_if")) for plan in case["options"]] == [
        False,
        False,
        True,
        True,
        True,
    ]
    assert case["version"] == before["version"] + 3, (case["version"], before["version"])


def check_a_weather_what_if_is_declined_in_plain_words() -> None:
    # Case 5: the crosswind plans follow the operations control system's sequence at the reduced
    # rate and take no added delay, so the Case Assistant declines a what-if on them without
    # calling what_if; its plain decline is shown.
    with mock.patch("airline_systems.schedule.datetime", StormDayClock):
        _, cases, gateway, _, trace = storm_case()
        before = cases.get(trace)
        decline = (
            "Plan 1, Accept the reduced Lisbon rate, follows the operations control system's"
            " sequence at 24 movements an hour and takes no added delay, so I cannot build a"
            " what-if holding the crosswind flights 30 minutes more: no new plan is added."
        )
        gateway.assistant = replies(decline)
        chat = Chat(cases, gateway, now=Clock(), sleep=lambda seconds: None)
        question = "What if we hold the crosswind flights 30 minutes more?"
        entry = chat.ask(trace, question, DUTY_MANAGER)
        chat.answer(trace, entry["id"])
        case = cases.get(trace)

    [turn] = case["chat"]
    assert (turn["status"], turn["answer"], turn["what_if_plan_ids"]) == (
        "answered",
        decline,
        [],
    ), turn
    assert [call["name"] for call in turn["calls"]] == ["read_case"], turn["calls"]
    assert case["options"] == before["options"], [plan["plan_id"] for plan in case["options"]]


class CaseChatTests(unittest.TestCase):
    def test_a_what_if_adds_a_plan_only_occ_can_approve(self) -> None:
        check_a_what_if_adds_a_plan_only_occ_can_approve()

    def test_a_what_if_the_rules_would_run_automatically_waits_for_occ(self) -> None:
        check_a_what_if_the_rules_would_run_automatically_waits_for_occ()

    def test_what_ifs_are_refused_outside_their_rules(self) -> None:
        check_what_ifs_are_refused_outside_their_rules()

    def test_a_question_is_answered_and_kept_with_the_case(self) -> None:
        check_a_question_is_answered_and_kept_with_the_case()

    def test_one_question_at_a_time_and_a_failed_answer_frees_the_chat(self) -> None:
        check_one_question_at_a_time_and_a_failed_answer_frees_the_chat()

    def test_earlier_turns_go_with_the_question(self) -> None:
        check_earlier_turns_go_with_the_question()

    def test_an_answer_after_reset_is_still_recorded(self) -> None:
        check_an_answer_after_reset_is_still_recorded()

    def test_the_chat_api(self) -> None:
        check_the_chat_api()

    def test_the_case_assistant_reads_its_case_and_cannot_plan_or_submit(self) -> None:
        check_the_case_assistant_reads_its_case_and_cannot_plan_or_submit()

    def test_a_what_if_through_the_chat_names_who_asked(self) -> None:
        check_a_what_if_through_the_chat_names_who_asked()

    def test_a_busy_case_reads_within_the_digest_limit(self) -> None:
        check_a_busy_case_reads_within_the_digest_limit()

    def test_an_answer_is_shown_only_for_plans_arrow_has(self) -> None:
        check_an_answer_is_shown_only_for_plans_arrow_has()

    def test_a_weather_what_if_is_declined_in_plain_words(self) -> None:
        check_a_weather_what_if_is_declined_in_plain_words()
