"""Entra-only operator API with major recovery execution disabled."""

from __future__ import annotations

import logging
import os
import re
import secrets
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import anyio
import pyodbc
from azure.core.exceptions import AzureError
from fastapi import APIRouter, BackgroundTasks, Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from opentelemetry.trace import SpanKind
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from airline_systems import telemetry
from airline_systems.caller import (
    Caller,
    CallerRefused,
    caller_from_headers,
    parse_allowed,
    trace_id,
)
from airline_systems.caller import trace_id as caller_trace
from airline_systems.schedule import lisbon_today
from ops_app.actions import disruption_record, plan_actions, plan_hash
from ops_app.case_digest import case_digest, digest_text, plan_number
from ops_app.chat import MAX_QUESTION, Chat, ChatBusy
from ops_app.engine import (
    AUTONOMY,
    CloudDependencyError,
    CloudGateway,
    FlightNotFoundError,
    Operations,
)
from ops_app.events import IntakeLog, Rejections, event_view
from ops_app.planner import Levers, WhatIfLevers
from ops_app.playbook import catalogue_view, checklist
from ops_app.policy import RULE_CATALOGUE, Limits, originals_of
from ops_app.reports import DelayCause, Hazard, OperatorReport
from ops_app.rules import (
    RuleConflict,
    RuleRefused,
    SqlRules,
    in_force_of,
    limits_from,
    preview_effect,
)
from ops_app.store import AssessmentInactive, CaseStore, DecisionConflict, SqlCaseStore
from ops_app.tools import ArrowTools, ToolRefused, refusal_text
from ops_app.trail import build_trail, case_card, map_view, unreadable_card

CLOSED_BY_TEST_RESET = {
    "actor": "test-bench",
    "autonomy": "system",
    "event": "closed_by_test_reset",
    "message": "Closed by test bench reset",
}
# Case chat spec §3: code, not a prompt, decides which tools each agent identity may call.
ASSISTANT_TOOLS = frozenset({"read_case", "what_if"})
# The orchestrator and the Case Assistant may read the playbook for their own case (OCC
# playbook spec §2).
SHARED_TOOLS = frozenset({"get_playbook"})
# Owner: the Disruption Detection Agent alone records what a report says, each type with
# its own fields.
DETECTION_TOOLS = frozenset(
    {"record_aircraft_defect", "record_flight_delay", "record_weather_forecast"}
)
# The Passenger Re-accommodation Agent alone reads an option's EU261 exposure.
PASSENGER_TOOLS = frozenset({"get_compensation_exposure"})
# How a refusal names the identity that called.
ROLES = {
    "orchestrator": "the orchestrator",
    "assistant": "the Case Assistant",
    "detection": "the Detection agent",
    "passenger": "the Passenger agent",
}
# What one app serves (three container apps spec): the operator app the UI and the operator API,
# the tools app ARROW's MCP tools for the agents. Offline runs serve both from one app.
SURFACES = frozenset({"operator", "tools"})
# Each app's start mode (APP_ROLE) and its role name in Application Insights.
APP_ROLES = {"operator": "arrow", "tools": "arrow-tools", "workers": "arrow-workers"}


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["handled"]


def tool_summary(name: str, result: Mapping[str, Any]) -> dict[str, Any]:
    """A domain agent's tool answer in named fields, as its case event keeps it."""
    if name in DETECTION_TOOLS:
        knock_on = result.get("knock_on") or {}
        found = {
            "recorded": result.get("recorded"),
            "event_id": result.get("event_id"),
            "repeat": result.get("repeat"),
            "flights_affected": knock_on.get("flights_affected"),
            "max_minutes_late": knock_on.get("max_minutes_late"),
        }
        return {key: value for key, value in found.items() if value is not None}
    keys = (
        "option_id",
        "additional_delay_minutes",
        "priced_additional_delay_minutes",
        "compensation_eur",
        "passengers_owed_compensation",
        "care_eur",
        "rebooking_eur",
        "extraordinary_circumstances",
    )
    return {key: result[key] for key in keys if key in result}


def tool_message(name: str, result: Mapping[str, Any]) -> str:
    """What a domain agent's successful call did, in words."""
    if name in DETECTION_TOOLS:
        knock_on = result.get("knock_on") or {}
        affected = knock_on.get("flights_affected")
        late = (
            f" · {affected} flight{'' if affected == 1 else 's'} late if nothing changes, by up"
            f" to {knock_on.get('max_minutes_late')} min"
            if affected
            else " · no flight late if nothing changes"
        )
        return f"Recorded {result.get('recorded')} in ARROW{late}"
    owed = result.get("passengers_owed_compensation")
    if result.get("extraordinary_circumstances"):
        due = "no compensation owed (extraordinary circumstances)"
    else:
        due = f"€{result.get('compensation_eur', 0):,} compensation for {owed} passengers"
    priced = result.get("priced_additional_delay_minutes")
    at = f" at +{priced} min" if isinstance(priced, int) and priced > 0 else ""
    return (
        f"EU261 exposure of option {result.get('option_id')}{at}: {due}; care"
        f" €{result.get('care_eur', 0):,}, rebooking €{result.get('rebooking_eur', 0):,}"
    )


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    option_id: str = Field(min_length=1, max_length=128)
    expected_version: int = Field(ge=0)
    decision: Literal["approve", "reject"]
    idempotency_key: str = Field(pattern=r"^[a-z0-9._:-]{16,128}$")
    # Owner: OCC can say why it decided; kept on the audited decision.
    comment: str | None = Field(default=None, max_length=300)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str  # its length is the chat's rule (400), not a validation error (422)


class RuleProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    cost_cap_eur: int
    rebook_limit: int
    reason: str


class AgentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    aircraft_reg: str | None = Field(default=None, pattern=r"^CS-[A-Z]{3}$")
    option_id: str | None = Field(default=None, min_length=1, max_length=128)
    additional_delay_minutes: int | None = Field(default=None, ge=0, le=180, multiple_of=5)


class CompletedTool(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    system: Literal["maintenance", "schedule", "crew", "booking", "slots"]
    operation: Literal[
        "get_defect",
        "get_aircraft_rotation",
        "get_recovery_options",
        "assess_crew_recovery",
        "assess_passenger_recovery",
        "assess_slot_recovery",
    ]
    status: Literal["completed"]


class AgentEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    input: AgentInput
    tools: list[CompletedTool] = Field(max_length=16)
    output: dict[str, JsonValue]


class AgentEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(
        pattern=r"^(disruption-detection-agent|aircraft-recovery-agent|crew-recovery-agent|"
        r"passenger-reaccommodation-agent|slot-negotiation-agent)$"
    )
    autonomy: str = Field(pattern=r"^L[234]$")
    event: str = Field(pattern=r"^agent_(started|completed)$")
    message: str = Field(min_length=1, max_length=2000)
    duration_ms: int | None = Field(default=None, ge=0)
    response_id: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    model: str | None = Field(default=None, min_length=1, max_length=256, strict=True)
    cached_input_tokens: int | None = Field(default=None, ge=0, strict=True)
    evidence: AgentEvidence | None = None


def create_app(
    store: CaseStore,
    operations: Operations,
    operator_oids: frozenset[str],
    host_oids: frozenset[str],
    public_url: str,
    *,
    expert_oids: frozenset[str] = frozenset(),
    orchestrator_oids: frozenset[str] = frozenset(),
    assistant_oids: frozenset[str] = frozenset(),
    detection_oids: frozenset[str] = frozenset(),
    passenger_oids: frozenset[str] = frozenset(),
    testbench_oids: frozenset[str] = frozenset(),
    rejections: IntakeLog | None = None,
    arrow_oids: frozenset[str] = frozenset(),
    trace_url_template: str | None = None,
    trace_resource_id: str | None = None,
    foundry_portal_url: str | None = None,
    surfaces: frozenset[str] = SURFACES,
    execute: Callable[[str], None] | None = None,
) -> FastAPI:
    """ARROW's HTTP app: the operator UI and API and/or the agents' MCP tools (``surfaces``).
    ``execute`` hands an authorised plan to the workers (three container apps spec §4: a wake on
    the execution queue); without it, as offline, this process carries the plan out itself.
    ``arrow_oids``: ARROW's apps' identities, whose airline-system calls the trail shows as
    ARROW's own."""
    rejections = rejections if rejections is not None else Rejections()
    # Code decides which tools each agent may call by its identity: one identity in two roles
    # would get both sets of tools.
    roles = {
        "orchestrator": frozenset(oid.lower() for oid in orchestrator_oids),
        "assistant": frozenset(oid.lower() for oid in assistant_oids),
        "detection": frozenset(oid.lower() for oid in detection_oids),
        "passenger": frozenset(oid.lower() for oid in passenger_oids),
    }
    held: dict[str, str] = {}
    for role, oids in roles.items():
        for oid in sorted(oids):
            if oid in held:
                raise ValueError(
                    f"An identity cannot be both {ROLES[held[oid]]} and {ROLES[role]}: {oid}"
                )
            held[oid] = role
    prompts: dict[str, tuple[float, dict[str, Any] | None]] = {}
    # Each case's end-to-end transaction link, once found (its first item never changes).
    transactions: dict[str, str] = {}

    def agent_prompt(name: str) -> dict[str, Any] | None:
        """An agent's prompt from Foundry, kept 10 minutes; None when it cannot be read."""
        cached = prompts.get(name)
        if cached and time.monotonic() - cached[0] < 600:
            return cached[1]
        reader = getattr(operations.gateway, "agent_definition", None)
        found: dict[str, Any] | None = None
        if reader is not None:
            try:
                found = reader(name)
            except Exception:
                logging.getLogger("ops_app").warning("Prompt of %s could not be read", name)
        prompts[name] = (time.monotonic(), found)
        return found

    def start_execution(trace: str, tasks: BackgroundTasks | None = None) -> None:
        """Carry out a plan authorised in the case (approved or automatic): the workers do it
        when ARROW runs as three apps; a send that fails leaves it to their periodic check."""
        if execute is not None:
            try:
                execute(trace)
            except Exception:
                logging.getLogger("ops_app").exception(
                    "Could not hand %s to the workers; their next check starts it", trace
                )
        elif tasks is not None:
            tasks.add_task(operations.execute, trace)
        else:
            threading.Thread(
                target=operations.execute, args=(trace,), name=f"execute-{trace[:8]}", daemon=True
            ).start()

    tools = ArrowTools(
        store,
        operations.gateway,
        execute=start_execution,
        rules=operations.rules,
        record=operations.record_reading,
    )
    chat = Chat(store, operations.gateway)
    mcp = MCPServer(
        name="arrow",
        instructions=(
            "ARROW recovery tools. The Detection agent records what a report says, with the"
            " typed tool for its kind of disruption. The orchestrator reads the recorded"
            " disruption and the playbook of standard OCC actions for it, records the case, builds"
            " costed plans from levers the airline systems offered, and submits one. The Passenger"
            " agent reads an option's EU261 exposure. The Case Assistant reads its case and"
            " playbook and asks for what-if plans OCC decides. Code checks every value, computes"
            " each plan and decides whether it runs automatically, goes to OCC or is blocked, and"
            " which tools each agent may call."
        ),
    )
    agent_oids = frozenset(held)

    def is_case(trace: str) -> bool:
        try:
            store.get(trace)
        except KeyError:
            return False
        return True

    def may_call(name: str, headers: Mapping[str, str]) -> str:
        """Which identity may call which tool (case chat spec §3, owner): the Detection
        agent records reports, the Passenger agent reads the exposure, the Case Assistant reads
        its case and asks for what-ifs, both it and the orchestrator read the playbook, and the
        orchestrator keeps the rest. Returns the caller's role."""
        try:
            oid = caller_from_headers(headers, agent_oids).oid
        except CallerRefused as error:
            raise ToolRefused(str(error)) from error
        role = held[oid]
        allowed = (
            {"detection"}
            if name in DETECTION_TOOLS
            else {"passenger"}
            if name in PASSENGER_TOOLS
            else {"assistant"}
            if name in ASSISTANT_TOOLS
            else {"orchestrator", "assistant"}
            if name in SHARED_TOOLS
            else {"orchestrator"}
        )
        if role not in allowed:
            raise ToolRefused(f"This tool is not available to {ROLES[role]}")
        return role

    def logged_call(
        trace: str,
        actor: str,
        name: str,
        request: Mapping[str, Any],
        result: Mapping[str, Any] | None,
        error: Exception | None,
    ) -> None:
        """A domain agent's call to ARROW's own tools, kept on the case so its trail shows the
        call under that agent (Foundry keeps only the orchestrator's own run). The record
        explains a call; it never changes or blocks one."""
        try:
            summary = tool_summary(name, result) if result is not None else None
            if error is not None:
                message = f"Refused: {refusal_text(error)}"
            elif result is not None and result.get("repeat"):
                message = str(result.get("message") or "Already recorded for another case")
            else:
                message = tool_message(name, result or {})
            event = {
                "actor": actor,
                "autonomy": AUTONOMY[actor],
                "event": "agent_tool_call",
                "tool": name,
                "access": "write" if name.startswith("record_") else "read",
                "request": dict(request),
                "response": summary,
                "ok": error is None,
                "message": message[:300],
            }
            store.append_event(trace, event)
        except Exception:
            logging.getLogger("ops_app").exception("Agent tool call not logged for %s", trace)

    async def call(
        name: str,
        case_id: str,
        work: Callable[[str], dict[str, Any]],
        ctx: Context,
        *,
        request: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One agent's tool call about the case it names by case id (owner). A domain
        agent's call (``request``: its named fields) is kept on the case."""
        headers = dict(ctx.headers or {})
        calling = caller_trace(headers)

        def traced() -> dict[str, Any]:
            role = may_call(name, headers)
            try:
                trace = operations.trace_of(case_id)
            except ValueError as error:
                raise ToolRefused(str(error)) from error
            # A run acts only on its own case: Foundry carries the case's trace into the run's
            # tool calls, so a call from another case's run is refused (review).
            if calling and calling != trace and is_case(calling):
                raise ToolRefused("This run belongs to another case; act only on your own case.")
            kept = role in AUTONOMY and request is not None
            # An agent's tool call joins the case's trace (a new root there if Foundry's call
            # came in its own trace).
            with telemetry.case_span(
                name, trace, root_kind=SpanKind.SERVER, attributes={"arrow.tool": name}
            ):
                try:
                    result = work(trace)
                except Exception as error:
                    if kept:
                        logged_call(trace, role, name, request or {}, None, error)
                    raise
            if kept:
                logged_call(trace, role, name, request or {}, result, None)
            # An agent sees the case id it used, never the internal trace id.
            if result.get("trace_id") == trace:
                result = {
                    **{k: v for k, v in result.items() if k != "trace_id"},
                    "case_id": case_id,
                }
            return result

        try:
            return await anyio.to_thread.run_sync(traced)
        except Exception as error:
            if not isinstance(error, ValueError | AssessmentInactive):
                logging.getLogger("ops_app").exception("ARROW tool failed")
            raise ToolError(refusal_text(error)) from error

    async def record(
        name: str, case_id: str, values: dict[str, Any], ctx: Context
    ) -> dict[str, Any]:
        """The Detection agent's reading of the report, checked and recorded by ARROW."""
        return await call(
            name,
            case_id,
            lambda trace: tools.record_disruption(trace, values),
            ctx,
            request={key: value for key, value in values.items() if key != "kind"},
        )

    @mcp.tool()
    async def record_aircraft_defect(
        case_id: str, aircraft_reg: str, unavailable_minutes: int, reading: str, ctx: Context
    ) -> dict[str, Any]:
        """Record an aircraft out of service that the report names, using only values written
        in it. case_id is the case id you were given (DSR-YYYYMMDD-XXXXXX); aircraft_reg as
        written (CS- and three letters); unavailable_minutes how long maintenance expects it out
        of service (1-720; hours become minutes; a range takes its upper bound); reading is one
        sentence (at most 300 characters) saying how you read the report. ARROW checks every
        value against the report and today's operation, records the defect on its case (no
        airline system changes until a plan for it is carried out) and returns the disruption
        as recorded and its knock_on: the flights it makes late if nothing changes."""
        return await record(
            "record_aircraft_defect",
            case_id,
            {
                "kind": "aircraft_defect",
                "aircraft_reg": aircraft_reg,
                "unavailable_minutes": unavailable_minutes,
                "reading": reading,
            },
            ctx,
        )

    @mcp.tool()
    async def record_flight_delay(
        case_id: str,
        flight_no: str,
        delay_minutes: int,
        cause: DelayCause,
        reading: str,
        ctx: Context,
    ) -> dict[str, Any]:
        """Record a flight that leaves late, as the report names it, using only values written
        in it. case_id is the case id you were given (DSR-YYYYMMDD-XXXXXX); flight_no as written
        (AR and digits); delay_minutes 5-180, rounded up to the next multiple of 5 ("about 20
        minutes" is 20); cause staff_shortage (staff, crew or handling shortage), baggage,
        late_inbound (late arriving aircraft or rotation), border_control (border-control
        queues, including Entry/Exit System checks) or other; reading is one sentence (at most
        300 characters) saying how you read the report. ARROW checks every value, records the
        delay on its case (no airline system changes until a plan for it is carried out) and
        returns the disruption as recorded and its knock_on: the flights it makes late if
        nothing changes."""
        return await record(
            "record_flight_delay",
            case_id,
            {
                "kind": "flight_delay",
                "flight_no": flight_no,
                "delay_minutes": delay_minutes,
                "cause": cause,
                "reading": reading,
            },
            ctx,
        )

    @mcp.tool()
    async def record_weather_forecast(
        case_id: str,
        airport: str,
        taf_window: str,
        hazard: Hazard,
        reading: str,
        ctx: Context,
    ) -> dict[str, Any]:
        """Record a weather forecast (TAF) for an airport, using only values written in it.
        case_id is the case id you were given (DSR-YYYYMMDD-XXXXXX); airport the ICAO code as
        written (e.g. LPPT); taf_window the change group's period exactly as written (DDHH/DDHH,
        UTC: never convert it, ARROW does); hazard thunderstorm (TS or CB), crosswind (wind or
        gusts blowing across the runway: ARROW computes the component across Lisbon's runway
        02/20 from the group's wind and records it from 25 kt) or low_visibility (under 800 m or
        fog); reading is one sentence (at most 300 characters) saying how you read the
        forecast. ARROW checks the group forecasts the hazard, records the forecast on its case
        (no airline system records a forecast) and returns the reduced hourly rate the playbook
        plans and the knock_on: the flights the reduced rate makes late."""
        return await record(
            "record_weather_forecast",
            case_id,
            {
                "kind": "weather",
                "airport": airport,
                "taf_window": taf_window,
                "hazard": hazard,
                "reading": reading,
            },
            ctx,
        )

    @mcp.tool()
    async def record_case(
        case_id: str, summary: str, findings: list[dict[str, str]], ctx: Context
    ) -> dict[str, Any]:
        """Record the case summary (max 600 characters) and each agent's finding
        ({"agent": "<agent-name>", "text": "<max 300 characters>"}) before building plans.
        case_id is the case id from the brief."""
        return await call(
            "record_case", case_id, lambda trace: tools.record_case(trace, summary, findings), ctx
        )

    @mcp.tool()
    async def get_compensation_exposure(
        case_id: str, option_id: str, ctx: Context, additional_delay_minutes: int = 0
    ) -> dict[str, Any]:
        """Read the EU261 exposure of one recovery option, read only: passengers owed
        compensation by distance band and the euros, care (meals, hotel rooms, overnight
        passengers) and rebooking, and whether extraordinary circumstances apply (weather: no
        compensation owed, with what would otherwise have been owed). ARROW computes it from
        Booking's journeys for the option with the code that prices every plan, at the delay
        the option needs once the domains agree (priced_additional_delay_minutes: more than
        asked when Lisbon's capacity needs it; give that delay with the figures). case_id is
        the case id you were given (DSR-YYYYMMDD-XXXXXX); option_id and additional_delay_minutes
        (0-180, a multiple of 5) as the operations control system's options give them."""
        return await call(
            "get_compensation_exposure",
            case_id,
            lambda trace: tools.compensation_exposure(trace, option_id, additional_delay_minutes),
            ctx,
            request={"option_id": option_id, "additional_delay_minutes": additional_delay_minutes},
        )

    @mcp.tool()
    async def get_playbook(case_id: str, ctx: Context) -> dict[str, Any]:
        """Read the case's recorded disruption and the standard OCC actions that apply to it,
        read only. disruption: its type and that type's own fields, event_id only when its
        source system had recorded it already, its scope and knock_on counts (flights late if
        nothing changes, the most minutes late). facts: the disruption's facts, which every
        airline system is asked by (give them unchanged to the domain agents as the disruption
        their systems are asked about). allowed_aircraft_kinds: the aircraft
        option kinds a plan may use. actions: each with its id, name, desk, autonomy (auto, occ
        or advice), phase (prepare or commit) and what it needs; name the ids a plan uses in
        build_plan's playbook_actions; ARROW refuses an action that does not apply. case_id is
        the case id from the brief."""
        return await call("get_playbook", case_id, tools.get_playbook, ctx)

    @mcp.tool()
    async def build_plan(case_id: str, levers: Levers, ctx: Context) -> dict[str, Any]:
        """Compute one recovery plan from levers the systems offered: aircraft_option_id from
        Schedule's options, optionally reserve_option_ids from Crew's reserve options (one
        standby crew per breached duty), rebook, delay_messages and playbook_actions (ids from
        get_playbook of the actions the plan uses; ARROW checks each and records it). ARROW
        sets delay_messages from the plan's flights (on when a departure leaves more than 15
        min late) whatever is asked, and says so in delay_messages_required. Returns plan_id,
        route (auto/occ/blocked), reasons, cost, outcomes (stranded overnight, lose their
        flight or connection, crews over their limit), refused_actions and conflict. A lever
        the systems did not offer builds no plan: the refusal says which; build again without
        it. At most 4 plans per case, and none once a plan's route is auto. case_id is the
        case id from the brief."""
        return await call(
            "build_plan", case_id, lambda trace: tools.build_plan(trace, levers.model_dump()), ctx
        )

    @mcp.tool()
    async def submit_plan(case_id: str, plan_id: str, reason: str, ctx: Context) -> dict[str, Any]:
        """Recommend one built plan with your reason (max 300 characters). An auto plan is
        carried out now; an occ plan waits for the Duty Manager; a blocked plan changes nothing
        and its reasons come back so you can build another plan. case_id is the case id from
        the brief."""
        return await call(
            "submit_plan", case_id, lambda trace: tools.submit_plan(trace, plan_id, reason), ctx
        )

    @mcp.tool(structured_output=False)
    async def read_case(case_id: str, ctx: Context) -> str:
        """Read the case you were asked about, read only: the report and ARROW's reading, its
        status and stage, each plan by the number OCC sees on its card (title, kind, the levers
        it was built from, route and reasons, cost parts and their workings, passengers
        disrupted, rebooked, to Passenger Services and kept by car, flights with old and new
        times, crew and slot checks, whether it is recommended and why, and the question a
        what-if plan answers), the agents' findings and what ARROW carried out. case_id is the
        case id from the brief."""
        digest = await call(
            "read_case", case_id, lambda trace: case_digest(operations.load(trace)), ctx
        )
        # One compact text, the one its limit is measured on: returned as a dict, the SDK would
        # send it indented and again as structured content.
        return digest_text(digest)

    @mcp.tool()
    async def what_if(
        case_id: str, levers: WhatIfLevers, question: str, ctx: Context
    ) -> dict[str, Any]:
        """Ask ARROW for a what-if plan answering the duty manager's question, with question
        your wording of the what-if (1-500 characters; the plan records the duty manager's own
        question from the chat) and levers as read_case shows a plan's: aircraft_option_id from
        Schedule's options, optionally reserve_option_ids from Crew's reserve options, rebook,
        delay_messages, and extra_delay_minutes (0-180, a multiple of 5; a delay option only)
        for a later ready time. ARROW builds, prices, checks and routes it like any plan, adds
        it after the case's plans and returns its number, route, reasons and cost. Only while
        the case waits for OCC; at most 3 a case; it never runs automatically and is never the
        recommendation: OCC decides. case_id is the case id from the brief."""

        def ask(trace: str) -> dict[str, Any]:
            # The plan names who asked and what: the duty manager behind the chat's open
            # question, in their own words; the agent's wording is kept apart from it.
            pending = [
                turn
                for turn in store.get(trace).get("chat") or []
                if turn.get("status") == "pending"
            ]
            if not pending:
                raise ToolRefused("What-ifs come from a question in the chat")
            text = question.strip()
            if not 1 <= len(text) <= MAX_QUESTION:
                raise ToolRefused(f"A question has 1 to {MAX_QUESTION} characters")
            entry = pending[-1]
            result = tools.what_if(
                trace,
                levers.model_dump(),
                entry["question"],
                entry["asked_by"],
                entry["id"],
                assumption=text,
            )
            # The number first: a long answer is clipped in the run's record (4,000 characters),
            # and the chat names the plan from it.
            return {"number": plan_number(store.get(trace), result["plan_id"]), **result}

        return await call("what_if", case_id, ask, ctx)

    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True, host="0.0.0.0"
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if "tools" not in surfaces:
            yield
            return
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="ARROW", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    # The operator app's routes; the tools app leaves them out (three container apps spec).
    operator = APIRouter()

    async def agents_only(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path.rstrip("/") == "/mcp":
            try:
                caller_from_headers(dict(request.headers), agent_oids)
            except CallerRefused as error:
                return JSONResponse({"detail": str(error)}, status_code=403)
        return await call_next(request)

    if "tools" in surfaces:
        app.middleware("http")(agents_only)

    @app.exception_handler(CloudDependencyError)
    @app.exception_handler(AzureError)
    async def dependency_unavailable(request: Request, error: Exception) -> JSONResponse:
        logging.getLogger("ops_app").error("Cloud dependency failed: %s", error)
        return JSONResponse(
            {"detail": "A cloud service is unavailable. Retry shortly."},
            status_code=503,
            headers={"Retry-After": "10", "Cache-Control": "no-store"},
        )

    @app.exception_handler(pyodbc.Error)
    async def database_unavailable(request: Request, error: pyodbc.Error) -> JSONResponse:
        logging.getLogger("ops_app").error("Database request failed: %s", error)
        state = str(error.args[0]) if error.args else ""
        if state.startswith("08") or state in {"HYT00", "HYT01"} or "(40613)" in str(error):
            return JSONResponse(
                {"detail": "Database is waking or temporarily unavailable. Retry shortly."},
                status_code=503,
                headers={"Retry-After": "10"},
            )
        return JSONResponse({"detail": "Database request failed."}, status_code=500)

    @app.exception_handler(AssessmentInactive)
    async def inactive_assessment(request: Request, error: AssessmentInactive) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(RuleRefused)
    async def rule_refused(request: Request, error: RuleRefused) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=403)

    @app.exception_handler(RuleConflict)
    async def rule_conflict(request: Request, error: RuleConflict) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=409)

    def user(request: Request, *, write: bool = False) -> Caller:
        try:
            actor = caller_from_headers(dict(request.headers), operator_oids | expert_oids)
        except CallerRefused as error:
            raise HTTPException(
                403 if request.headers.get("x-ms-client-principal") else 401, str(error)
            ) from error
        if write and (
            request.headers.get("origin") != public_url
            or request.headers.get("x-arrow-request") != "operator"
        ):
            raise HTTPException(403, "Same-origin operator request required")
        return actor

    def load(trace: str) -> dict[str, Any]:
        try:
            return operations.load(trace)
        except KeyError as error:
            raise HTTPException(404, "Operation not found") from error

    def system_hosts() -> dict[str, str]:
        """Each airline system's own service host, as ARROW calls it (its first DNS label)."""
        urls = getattr(operations.gateway, "system_urls", None) or {}
        return {
            name: str(urlsplit(url).hostname or "").split(".")[0]
            for name, url in urls.items()
            if isinstance(url, str) and url
        }

    def internal(trace: str, request: Request) -> dict[str, Any]:
        try:
            caller_from_headers(dict(request.headers), host_oids)
        except CallerRefused as error:
            raise HTTPException(403, str(error)) from error
        document = load(trace)
        if not secrets.compare_digest(
            request.headers.get("x-arrow-run", ""), document["_run_token"]
        ):
            raise HTTPException(403, "Run capability is invalid")
        if document["status"] != "assessing":
            raise HTTPException(409, "This assessment is no longer active")
        return document

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @operator.get("/api/session")
    def session(request: Request) -> dict[str, Any]:
        actor = user(request)
        duty_manager = actor.oid in operator_oids
        return {
            "user": {
                "oid": actor.oid,
                "name": actor.name or "OCC operator",
                "role": "OCC Duty Manager" if duty_manager else "Passenger Services",
            },
            "day": lisbon_today().isoformat(),
            "demo_time": "09:45",
            "mode": "simulated",
            "capabilities": {"approve": duty_manager, "review": actor.oid in expert_oids},
            # Application Insights' transaction search for one case: "{trace}" is its trace id.
            "trace_url_template": trace_url_template,
            # The Foundry portal's project, for links to an agent and its version; None: no links.
            "foundry_portal_url": foundry_portal_url or None,
        }

    @operator.get("/api/cases")
    def list_cases(request: Request) -> dict[str, Any]:
        user(request)
        cases = []
        for document in store.list(lisbon_today().isoformat()):
            try:
                document = operations.reconcile(document)
                # Evaluation cases (QA-) stay readable by trace as evidence, not in the case list.
                if str(document.get("display_id") or "").startswith("QA-"):
                    continue
                disruption = document.get("disruption") or {}
                cases.append(
                    {
                        **{
                            k: document[k]
                            for k in ("trace_id", "display_id", "status", "created_at")
                        },
                        "aircraft_reg": (disruption.get("scope") or {}).get("aircraft_reg")
                        or (document.get("defect") or {}).get("aircraft_reg"),
                        "disruption_type": disruption.get("type", "aircraft_defect"),
                        "flight_no": disruption.get("flight_no"),
                        **case_card(document),
                    }
                )
            except (CloudDependencyError, AzureError, pyodbc.Error):
                raise
            except Exception:
                # One malformed case never blanks the list: it is listed for OCC to open.
                trace = str(document.get("trace_id") or "")
                logging.getLogger("ops_app").exception("Case %s could not be listed", trace)
                if trace and not str(document.get("display_id") or "").startswith("QA-"):
                    cases.append(unreadable_card(document))
        return {"cases": cases}

    @operator.get("/api/network")
    def network(request: Request) -> JSONResponse:
        user(request)
        return JSONResponse(
            operations.gateway.network(telemetry.current_trace() or secrets.token_hex(16)),
            headers={"Cache-Control": "no-store"},
        )

    @operator.get("/api/flights/{flight_no}")
    def flight_details(flight_no: str, request: Request) -> JSONResponse:
        user(request)
        normalized = flight_no.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{2,8}", normalized):
            raise HTTPException(422, "Flight number is invalid")
        trace = trace_id(request.headers) or telemetry.current_trace() or secrets.token_hex(16)
        try:
            details = operations.gateway.flight_details(normalized, trace)
        except FlightNotFoundError as error:
            raise HTTPException(404, "Flight not found") from error
        return JSONResponse(details, headers={"Cache-Control": "no-store"})

    @operator.post("/api/cases", status_code=202)
    def open_case(
        report: Annotated[OperatorReport, Body()], request: Request, tasks: BackgroundTasks
    ) -> dict[str, str]:
        actor = user(request, write=True)
        if actor.oid not in operator_oids:
            raise HTTPException(403, "Only OCC reports a disruption")
        if not str(getattr(report, "description", None) or getattr(report, "text", "")).strip():
            raise HTTPException(422, "A description is required")
        # An event id belongs to Service Bus events only: a typed report must never take one a
        # later event will carry (that event would then count as a repeat of this report), nor
        # claim a system recorded it already (only an event its source published says so).
        opened = operations.open(
            report.model_dump(mode="json", exclude={"event_id", "source_event_id"}), actor.oid
        )
        if opened["created"]:
            trace = opened["trace_id"]
            if operations.waker is None:
                # Offline only (tests and the console walk-through); Dev always has the queue.
                tasks.add_task(operations.run, trace)
            else:
                try:
                    with telemetry.case_span(
                        "Queue assessment", trace, root_kind=SpanKind.PRODUCER
                    ):
                        operations.waker(trace, None)
                except Exception as error:
                    logging.getLogger("ops_app").exception("Could not queue %s", trace)
                    operations.stop(
                        trace,
                        "ARROW could not queue the assessment; report it again."
                        " No recovery actions were executed.",
                        "assessment_not_queued",
                    )
                    raise HTTPException(
                        503, "ARROW could not queue the assessment; report it again"
                    ) from error
        return {"trace_id": opened["trace_id"], "display_id": opened["display_id"]}

    def testbench(request: Request) -> None:
        try:
            caller_from_headers(dict(request.headers), testbench_oids)
        except CallerRefused as error:
            raise HTTPException(
                403 if request.headers.get("x-ms-client-principal") else 401, str(error)
            ) from error

    @operator.get("/api/events")
    def received_events(day: str, request: Request) -> JSONResponse:
        """Every event ARROW received on one Lisbon day, with its case, or why it has none."""
        testbench(request)
        try:
            chosen = date.fromisoformat(day)
        except ValueError as error:
            raise HTTPException(422, "day: YYYY-MM-DD") from error
        listed: list[dict[str, Any]] = []
        for document in store.received_events(chosen.isoformat()):
            first: dict[str, Any] = next(
                (e for e in document["events"] if e.get("event") == "event_received"), {}
            )
            case = operations.reconcile(document)
            listed.append(
                {
                    "event_id": document.get("event_id"),
                    "envelope": first.get("body"),
                    "queued_at": first.get("enqueued_at"),
                    "received_at": first.get("at"),
                    "case": event_view(case),
                }
            )
            # The weather updates a storm case received, each listed as its own event.
            for update in document["events"]:
                body = update.get("body")
                if update.get("delivery") != "update" or not isinstance(body, dict):
                    continue
                listed.append(
                    {
                        "event_id": body.get("id"),
                        "envelope": body,
                        "queued_at": update.get("enqueued_at"),
                        "received_at": update.get("at"),
                        "case": event_view(case, str(body.get("id"))),
                    }
                )
        for rejected in rejections.on(chosen):
            listed.append(
                {
                    "event_id": rejected["event_id"],
                    "envelope": rejected.get("body"),
                    "received_at": rejected["received_at"],
                    "rejected": {k: rejected[k] for k in ("status", "reason", "received_at")},
                }
            )
        return JSONResponse(
            {"day": chosen.isoformat(), "events": listed}, headers={"Cache-Control": "no-store"}
        )

    @operator.get("/api/events/{event_id}")
    def event_status(event_id: str, request: Request) -> JSONResponse:
        testbench(request)
        document = store.find_by_event(event_id)
        if document is None:
            rejected = rejections.get(event_id)
            if rejected is not None:
                return JSONResponse(rejected, headers={"Cache-Control": "no-store"})
            # A weather update settles a storm case of today; it opened none.
            for found in store.received_events(lisbon_today().isoformat()):
                if any(
                    event.get("delivery") == "update"
                    and isinstance(event.get("body"), dict)
                    and str(event["body"].get("id")) == event_id
                    for event in found["events"]
                ):
                    case = store.get(str(found["trace_id"]))
                    return JSONResponse(
                        event_view(operations.reconcile(case), event_id),
                        headers={"Cache-Control": "no-store"},
                    )
            raise HTTPException(404, "No case for this event")
        return JSONResponse(
            event_view(operations.reconcile(document)), headers={"Cache-Control": "no-store"}
        )

    @operator.post("/api/test/close-cases")
    def close_test_cases(request: Request) -> dict[str, Any]:
        testbench(request)
        # Events still queued from before this Reset must not replay into the reloaded day.
        rejections.mark_reset(operations.now())
        closed: list[str] = []
        skipped: list[dict[str, str]] = []
        for trace in store.open_traces():
            display_id = store.get(trace)["display_id"]
            if store.close(trace, CLOSED_BY_TEST_RESET):
                closed.append(display_id)
            else:
                skipped.append({"display_id": display_id, "reason": "executing"})
        return {"closed": closed, "skipped": skipped}

    @operator.get("/api/cases/{trace}")
    def get_case(trace: str, request: Request) -> dict[str, Any]:
        user(request)
        document = load(trace)
        view = {
            key: value
            for key, value in document.items()
            if not key.startswith("_") and key not in {"rotation", "candidates", "airports"}
        }
        # A plan built before the playbook shows the checklist read from what it does.
        originals = originals_of(document)
        # The times ARROW read for this case, the ones its plans are priced against: the screen
        # compares each plan's flights with these, never with a later executed change to the
        # timetable (review).
        view["originals"] = originals
        view["options"] = [
            {**option, "playbook": option.get("playbook") or checklist(option, originals)}
            for option in document.get("options") or []
        ]
        if not document.get("execution"):
            # What approving each plan would change, from the executor's own action builder.
            view["options"] = [
                {**option, "planned_actions": planned(trace, document, option)}
                for option in view["options"]
            ]
        return view

    @operator.get("/api/cases/{trace}/events")
    def events(trace: str, request: Request) -> dict[str, Any]:
        user(request)
        return {"events": load(trace)["events"]}

    @operator.get("/api/cases/{trace}/trail")
    def decision_trail(trace: str, request: Request) -> JSONResponse:
        user(request)
        document = load(trace)
        try:
            calls: list[dict[str, Any]] | None = store.system_calls(trace)
        except Exception:
            logging.getLogger("ops_app").warning("System calls unreadable for %s", trace)
            calls = None
        return JSONResponse(
            build_trail(document, calls, agent_prompt, arrow_oids, system_hosts()),
            headers={"Cache-Control": "no-store"},
        )

    @operator.get("/api/cases/{trace}/trace")
    def case_trace(trace: str, request: Request) -> RedirectResponse:
        """Opens the case in Application Insights: its end-to-end transaction view once the
        case's first item has arrived, else the search for the case (owner)."""
        user(request)
        if not trace_url_template:
            raise HTTPException(404, "Tracing is off")
        document = load(trace)
        link = transactions.get(trace)
        finder = getattr(operations.gateway, "first_trace_item", None)
        if link is None and trace_resource_id and finder is not None:
            try:
                opened = datetime.fromisoformat(str(document.get("created_at")))
                item = finder(trace_resource_id, trace, opened)
                if item:
                    link = telemetry.transaction_url(trace_resource_id, trace, item)
                    transactions[trace] = link
            except Exception:
                logging.getLogger("ops_app").warning("No transaction item for %s", trace)
        target = link or trace.join(trace_url_template.split("{trace}"))
        return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})

    @operator.get("/api/cases/{trace}/map")
    def case_map(trace: str, request: Request) -> JSONResponse:
        user(request)
        return JSONResponse(map_view(load(trace)), headers={"Cache-Control": "no-store"})

    @operator.post("/api/cases/{trace}/approval")
    def approve(
        trace: str, decision: ApprovalRequest, request: Request, tasks: BackgroundTasks
    ) -> dict[str, Any]:
        actor = user(request, write=True)
        if actor.oid not in operator_oids:
            raise HTTPException(403, "Only the OCC Duty Manager records recovery decisions")
        try:
            receipt = operations.record_decision(
                trace,
                option_id=decision.option_id,
                decision=decision.decision,
                expected_version=decision.expected_version,
                idempotency_key=decision.idempotency_key,
                actor_oid=actor.oid,
                comment=decision.comment,
            )
        except KeyError as error:
            raise HTTPException(404, "Operation not found") from error
        except DecisionConflict as error:
            raise HTTPException(409, str(error)) from error
        if decision.decision == "approve":
            start_execution(trace, tasks)
        return receipt

    @operator.post("/api/cases/{trace}/chat", status_code=202)
    def ask_about_case(
        trace: str, asked: ChatRequest, request: Request, tasks: BackgroundTasks
    ) -> dict[str, str]:
        """A question for the Case Assistant, kept on the case; the answer follows on the case
        (case chat spec §4). Any signed-in operator (OCC or Passenger Services) may ask;
        asking grants no authority."""
        actor = user(request, write=True)
        if actor.oid not in operator_oids | expert_oids:
            raise HTTPException(403, "Only an ARROW operator asks about a case")
        try:
            entry = chat.ask(trace, asked.question, actor)
        except KeyError as error:
            raise HTTPException(404, "Operation not found") from error
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        except ChatBusy as error:
            raise HTTPException(409, str(error)) from error
        tasks.add_task(chat.answer, trace, entry["id"])
        return {"entry_id": entry["id"]}

    @operator.post("/api/cases/{trace}/reviews/passenger_services")
    def record_review(trace: str, review: ReviewRequest, request: Request) -> dict[str, Any]:
        actor = user(request, write=True)
        if actor.oid not in expert_oids:
            raise HTTPException(403, "Only Passenger Services records this review")
        try:
            store.record_review(trace, "passenger_services", review.outcome, actor.oid)
        except KeyError as error:
            raise HTTPException(404, "Operation not found") from error
        except DecisionConflict as error:
            raise HTTPException(409, str(error)) from error
        return {"status": "recorded", "trace_id": trace}

    def rule_changer(request: Request) -> str:
        actor = user(request, write=True)
        if actor.oid not in operator_oids:
            raise HTTPException(403, "Only an OCC duty manager changes the rules")
        return actor.oid

    @operator.get("/api/rules")
    def rules(request: Request) -> JSONResponse:
        actor = user(request)
        # One read, so the version in force, the pending change and the history agree.
        history = operations.rules.history()
        current = in_force_of(history)
        pending = next((change for change in history if change.status == "proposed"), None)
        shown: dict[str, Any] | None = None
        if pending is not None:
            proposed = Limits(pending.base_version + 1, pending.cost_cap_eur, pending.rebook_limit)
            effect: list[dict[str, str]] | None
            try:
                listed = [
                    document
                    for document in store.list(lisbon_today().isoformat())
                    if not str(document.get("display_id") or "").startswith("QA-")
                ]
                effect = preview_effect(listed, proposed)
            except Exception:
                # The rules are readable; only the preview on today's plans is not.
                logging.getLogger("ops_app").exception("Rule change effect could not be computed")
                effect = None
            shown = {**pending.as_dict(), "effect": effect}
        return JSONResponse(
            {
                "in_force": {
                    **limits_from(current).as_dict(),
                    "approved_by": current.decided_by,
                    "approved_at": current.decided_at,
                },
                "pending": shown,
                "history": [change.as_dict() for change in history],
                "can_change": actor.oid in operator_oids,
                "catalogue": [rule.as_dict() for rule in RULE_CATALOGUE],
                # The playbook of standard OCC actions, read only (OCC playbook spec §1).
                "playbook": catalogue_view(),
            },
            headers={"Cache-Control": "no-store"},
        )

    @operator.post("/api/rules/proposals", status_code=201)
    def propose_rules(change: RuleProposalRequest, request: Request) -> dict[str, Any]:
        actor = rule_changer(request)
        try:
            proposal = operations.rules.propose(
                change.cost_cap_eur, change.rebook_limit, change.reason, actor
            )
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return proposal.as_dict()

    @operator.post("/api/rules/proposals/{change_id}/{action}")
    def decide_rules(
        change_id: int, action: Literal["approve", "reject", "withdraw"], request: Request
    ) -> dict[str, Any]:
        actor = rule_changer(request)
        return operations.rules.decide(change_id, action, actor).as_dict()

    @operator.get("/internal/cases/{trace}")
    def host_context(trace: str, request: Request) -> dict[str, Any]:
        doc = internal(trace, request)
        return {key: doc[key] for key in ("trace_id", "defect", "rotation", "candidates")}

    @operator.post("/internal/cases/{trace}/events")
    def host_event(trace: str, event: AgentEvent, request: Request) -> dict[str, str]:
        internal(trace, request)
        store.change(trace, {"stage": event.message}, event.model_dump(exclude_none=True))
        return {"status": "recorded"}

    # The browser checks every file again on each load (ETag, so unchanged files cost a 304): a
    # guessed cache lifetime could mix a new release's scripts with the previous one's.
    fresh = {"Cache-Control": "no-cache"}

    @operator.get("/")
    def index() -> FileResponse:
        return FileResponse(Path(__file__).parent / "static" / "index.html", headers=fresh)

    @operator.get("/{asset}")
    def asset(asset: str) -> FileResponse:
        if asset not in {
            "app.js",
            "favicon.svg",
            "tips.js",
            "land.js",
            "map_draw.js",
            "map_view.js",
            "map.css",
            "recovery_view.js",
            "plan_view.js",
            "trail_view.js",
            "chat_view.js",
            "tower_view.js",
            "rules_view.js",
            "styles.css",
            "ui.css",
            "tower.css",
        }:
            raise HTTPException(404, "Not found")
        return FileResponse(Path(__file__).parent / "static" / asset, headers=fresh)

    # Last: the MCP app answers POST /mcp; everything else is matched by the routes above.
    if "operator" in surfaces:
        app.include_router(operator)
    if "tools" in surfaces:
        app.mount("/", mcp_app)
    return app


def planned(
    trace: str, document: Mapping[str, Any], option: Mapping[str, Any]
) -> list[dict[str, str]] | None:
    """The actions the executor would take for this plan now, each with its stage (a weather
    plan's "now" or "when_confirmed"); None if they cannot be built."""
    try:
        actions = plan_actions(
            trace,
            option,
            originals=originals_of(document),
            validation=option.get("validation") or {},
            airports=document.get("airports") or {},
            plan_hash=plan_hash(option, int(document.get("version") or 0)),
            record=disruption_record(trace, document),
        )
    except (KeyError, TypeError, ValueError):
        return None
    return [
        {
            "system": a.system,
            "kind": a.kind,
            "label": a.label,
            "summary": a.summary,
            "stage": a.stage,
        }
        for a in actions
    ]


def _oids(name: str) -> frozenset[str]:
    """A comma-separated setting of Entra object ids; empty or unset admits no one."""
    value = os.getenv(name, "").strip()
    return parse_allowed(value) if value else frozenset()


def main() -> None:
    """One of ARROW's three apps (three container apps spec), by ``APP_ROLE``: the operator app
    (UI and operator API), the tools app (the agents' MCP tools) or the workers (intake,
    assessments, execution). Only the workers write to the airline systems."""
    import uvicorn
    from azure.identity import ManagedIdentityCredential

    from airline_systems.sql import SqlScheduleStore
    from ops_app.bus import ServiceBusQueue
    from ops_app.store import SqlIntakeLog

    logging.basicConfig(level=logging.INFO)
    role = os.getenv("APP_ROLE", "")
    if role not in APP_ROLES:
        raise SystemExit(f"APP_ROLE must be one of {', '.join(APP_ROLES)}, not {role!r}")
    credential = ManagedIdentityCredential(client_id=os.environ["AZURE_CLIENT_ID"])
    traced = telemetry.configure(APP_ROLES[role], credential, logger_name="ops_app")
    sql = SqlScheduleStore(
        os.environ["SQL_SERVER"], os.environ["SQL_DATABASE"], None, "ops", credential
    )
    store = SqlCaseStore(sql.connect)
    gateway = CloudGateway(
        credential,
        os.environ["SYSTEMS_API_APP_ID"],
        os.environ["SCHEDULE_URL"],
        os.environ["MAINTENANCE_URL"],
        os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        booking_url=os.environ["BOOKING_URL"],
        crew_url=os.getenv("CREW_URL", ""),
        slots_url=os.getenv("SLOTS_URL", ""),
        writes=role == "workers",
    )
    operations = Operations(store, gateway, SqlRules(sql.connect))
    intake_log = SqlIntakeLog(sql.connect)
    bus = ServiceBusQueue(
        os.environ["SERVICEBUS_NAMESPACE"],
        os.getenv("ASSESSMENT_QUEUE", "assessments"),
        credential,
        executions=os.getenv("EXECUTION_QUEUE", "executions"),
    )
    # Every app that opens or reopens a case wakes it on the assessment queue.
    operations.waker = bus.wake
    if role == "workers":
        from ops_app.workers import serve_forever

        serve_forever(
            operations,
            store,
            intake_log,
            bus,
            workers=int(os.getenv("ASSESSMENT_WORKERS", "2")),
            reporter_oid=os.environ["OPS_PRINCIPAL_ID"],
        )
        return
    resource_id = os.getenv(telemetry.RESOURCE_ID, "").strip()
    app = create_app(
        store,
        operations,
        parse_allowed(os.environ["OPERATOR_OIDS"]) if role == "operator" else frozenset(),
        # The hosted orchestrator that called back on /internal has been retired; without
        # HOST_OIDS those routes admit no one.
        _oids("HOST_OIDS"),
        os.environ["PUBLIC_URL"] if role == "operator" else "",
        expert_oids=_oids("EXPERT_OIDS"),
        # Empty admits no such agent to ARROW's tools.
        orchestrator_oids=_oids("ORCHESTRATOR_AGENT_OIDS"),
        assistant_oids=_oids("ASSISTANT_AGENT_OIDS"),
        detection_oids=_oids("DETECTION_AGENT_OIDS"),
        passenger_oids=_oids("PASSENGER_AGENT_OIDS"),
        testbench_oids=_oids("TESTBENCH_OIDS"),
        rejections=intake_log,
        arrow_oids=_oids("OPS_PRINCIPAL_ID")
        | _oids("OPERATOR_PRINCIPAL_ID")
        | _oids("TOOLS_PRINCIPAL_ID"),
        trace_url_template=telemetry.trace_url_template(resource_id)
        if traced and resource_id
        else None,
        trace_resource_id=resource_id if traced and resource_id else None,
        foundry_portal_url=os.getenv("FOUNDRY_PORTAL_URL", "").strip().rstrip("/") or None,
        surfaces=frozenset({role}),
        execute=bus.execute,
    )
    telemetry.instrument(app)
    uvicorn.run(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
