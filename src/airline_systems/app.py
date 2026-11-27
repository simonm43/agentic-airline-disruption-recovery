"""Schedule system service: REST for the ARROW app, MCP for the agents, one port.

Settings: SYSTEM=schedule, SQL_SERVER, SQL_DATABASE, AZURE_CLIENT_ID (managed identity),
ALLOWED_CALLER_OIDS (comma-separated Entra object ids admitted besides Easy Auth's own check).
Reads also need the caller's token to carry this system's app role (e.g. Schedule.Read); writes
need Actions.Write and an object id in WRITER_OIDS. JOURNEY_READER_OIDS (optional): the readers,
besides the writers, that Booking gives each journey of a rebooking plan (ARROW's tools app,
which prices plans and never writes).
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, Any, Protocol, cast

import anyio
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from opentelemetry.trace import SpanKind
from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema

from airline_systems import audit, event_ids, telemetry
from airline_systems.actions import ActionService
from airline_systems.caller import (
    WRITE_ROLE,
    CallerRefused,
    caller_with_role,
    parse_allowed,
    read_role,
    trace_id,
)
from airline_systems.defects import DefectService
from airline_systems.defects import recovery_options as grounded_options
from airline_systems.delays import disruption_with_knock_on
from airline_systems.facts import FIELDS, Facts, FactsRefused, facts_of, reported
from airline_systems.facts import inventory as inventory_of
from airline_systems.native import BodyRefused, NativeRoutes, playbook_body
from airline_systems.recovery import (
    DutyPlanService,
    PassengerHoldService,
    RebookingService,
    RecoveryAssessmentService,
)
from airline_systems.reserves import ReserveService
from airline_systems.schedule import ScheduleService, ScheduleStore, lisbon_today
from airline_systems.weather import dispatch_advisories


class BookingSummaryStore(Protocol):
    def booking_flight_summary(self, day: date, flight_no: str) -> dict[str, Any] | None: ...


class RecoveryHoldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    option_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")
    source_flight_no: str = Field(pattern=r"^[A-Za-z0-9]{2,8}$")
    assessment_version: int = Field(strict=True, ge=0)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class _Declined(ValueError):
    """A native route's error reply: audited as a failed call with its reason."""

    def __init__(self, status: int, answer: dict[str, Any]) -> None:
        super().__init__(str(answer.get("detail") or answer.get("reason") or status))
        self.status = status
        self.answer = answer


RESERVE_OPTION_ID = re.compile(r"R[0-9]{1,2}")
# Every name a disruption's facts may have (airline_systems.facts), as a refused read records it.
FACT_NAMES = ("type", *dict.fromkeys(name for names in FIELDS.values() for name in names))


def _plain(schema: Mapping[str, Any]) -> dict[str, Any]:
    """One fact's schema without its null alternative, title or default."""
    branches = [item for item in schema.get("anyOf", [schema]) if item.get("type") != "null"]
    found = dict(branches[0]) if branches else dict(schema)
    return {key: value for key, value in found.items() if key not in {"title", "default"}}


# The disruption a facts-based tool takes, its schema written out with no references (as the
# agents' model reads it). The call itself checks it (facts_of), so a refused read is audited
# after the caller is checked, like every other read.
DisruptionArgument = Annotated[
    dict[str, Any],
    WithJsonSchema(
        {
            "type": "object",
            "description": "The disruption's facts: its type and that type's own fields.",
            "properties": {
                name: _plain(item) for name, item in Facts.model_json_schema()["properties"].items()
            },
            "required": ["type"],
        }
    ),
]
# An operations event id (Maintenance's own reference for a defect it recorded) and the systems'
# own option ids.
EventId = Annotated[str, Path(pattern=r"^EV-[0-9A-Fa-f]{6}$")]
OptionId = Annotated[str, Path(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
# Minutes added to an option's delay, as a read's query parameter.
Delay = Annotated[int, Query(ge=0, le=180)]
# What a read about a disruption takes besides its facts (airline_systems.facts).
DISRUPTION_ARGUMENT = (
    "disruption: the disruption as you were given it, its type and that type's own fields:"
    " flight_delay with flight_no, delay_minutes and cause; aircraft_defect with aircraft_reg and"
    " unavailable_minutes; or weather with airport, hazard, window_start, window_end (Lisbon"
    " time, HH:MM) and hourly_rate."
)
# The facts-based tools' descriptions, each with what its disruption argument holds.
DESCRIPTIONS = {
    "get_disruption": (
        "Read the disruption as operations control sees it (its cause, aircraft, flight, delay"
        " or time out of service, or a weather forecast's airport, window, hazard and reduced"
        " hourly rate) and its knock_on: the flights it makes late if nothing changes, how many,"
        f" the most minutes late and the aircraft involved. {DISRUPTION_ARGUMENT}"
    ),
    "get_rebooking_plan": (
        f"Read rebooking-plan counts and digest for an option; no passenger records."
        f" {DISRUPTION_ARGUMENT}"
    ),
    "get_recovery_options": (
        "Read the aircraft options for the disruption (delay, swap, cancel), each with its"
        " flights, the levers checked but not available and why, and when the aircraft is"
        " available. A weather disruption's options span many aircraft and name none. Other"
        f" domains have NOT cleared these for execution. {DISRUPTION_ARGUMENT}"
    ),
    "get_reserve_options": (
        "Read the standby crews for duties the option pushes past their flight duty period (FDP)"
        f" limit. {DISRUPTION_ARGUMENT}"
    ),
}


def _named(result: Any, event: str) -> Any:
    """An answer about one recorded event names it by its event id, never the trace this
    system recorded it under."""
    if isinstance(result, dict):
        return {"event_id": event, **{k: v for k, v in result.items() if k != "trace_id"}}
    return result


_INSTRUCTIONS = {
    "schedule": (
        "Schedule system of the airline. Read today's flights of one aircraft and the demo clock "
        "(as_of_local). Times are Lisbon local time."
    ),
    "maintenance": (
        "Maintenance system. ARROW's executor records a defect once a plan for it runs; agents"
        " can only read recorded defects."
    ),
    "crew": (
        "Crew system. Read-only single-day FDP impact checks and crew reserve options; reading "
        "options does not assign reserve crew or establish full operational legality."
    ),
    "booking": (
        "Booking system. Read-only passenger and connection impact checks and rebooking-plan "
        "counts; no rebooking, care, or compensation is authorized."
    ),
    "slots": (
        "Slots system. Read-only hourly capacity checks; a capacity fit is not an allocated slot."
    ),
}
_DOMAIN_TOOLS = {
    "crew": "assess_crew_recovery",
    "booking": "assess_passenger_recovery",
    "slots": "assess_slot_recovery",
}


def create_app(
    store: ScheduleStore,
    allowed_oids: frozenset[str],
    *,
    system: str = "schedule",
    writer_oids: frozenset[str] = frozenset(),
    journey_reader_oids: frozenset[str] = frozenset(),
    passenger_holds_enabled: bool = False,
    mock_confirm_seconds: float = 5.0,
    mock_refuse_kinds: frozenset[str] = frozenset(),
) -> FastAPI:
    service = ScheduleService(store)
    defects = DefectService(store)
    reserves = ReserveService(store)
    assessments = RecoveryAssessmentService(store)
    passenger_holds = PassengerHoldService(store)
    rebooking = RebookingService(store)
    actions = ActionService(
        store, system, confirm_seconds=mock_confirm_seconds, refuse_kinds=mock_refuse_kinds
    )
    native = NativeRoutes(actions, store)
    reader = read_role(system)
    log = logging.getLogger("airline_systems")

    def answer(operation: str, result: Any) -> str | None:
        try:
            return audit.summary(operation, result)
        except Exception:
            # The call itself succeeded; its row says the answer was not recorded.
            log.warning("No recorded answer for %s", operation, exc_info=True)
            return None

    def refused(
        operation: str,
        headers: Mapping[str, str],
        reason: Exception | str,
        request: Mapping[str, Any] | None = None,
    ) -> None:
        """Audit a write refused before it reached ``guarded``."""
        store.log_call(
            operation,
            None,
            None,
            trace_id(headers),
            "refused",
            endpoint=audit.endpoint(operation, mcp=False),
            request=audit.as_json(dict(request or {}), audit.REQUEST_LIMIT),
            response=audit.failure(reason),
        )

    def guarded(
        operation: str,
        headers: Mapping[str, str],
        work: Callable[[], Any],
        *,
        mcp: bool = False,
        request: Mapping[str, Any] | None = None,
        event_trace: str | None = None,
        detail: Mapping[str, Any] | None = None,
        role: str = reader,
    ) -> Any:
        # A call about an operations event is filed under the trace this system recorded the
        # event under; an agent's traceparent belongs to its Foundry run, so the event wins.
        trace = (
            event_trace
            if event_trace and re.fullmatch(r"[0-9a-f]{32}", event_trace)
            else trace_id(headers)
        )
        note = json.dumps(dict(detail), separators=(",", ":"))[:400] if detail else None
        # The decision trail's view of the call (owner): endpoint, inputs, short answer.
        recorded: dict[str, Any] = {
            "endpoint": audit.endpoint(operation, mcp=mcp),
            "request": audit.as_json(dict(request or {}), audit.REQUEST_LIMIT),
        }
        # The handling joins the case's trace (a new root there for an agent's call).
        with telemetry.case_span(
            operation,
            trace,
            root_kind=SpanKind.SERVER,
            attributes={"airrec.system": system, "airrec.operation": operation},
        ):
            try:
                caller = caller_with_role(headers, allowed_oids, role)
            except CallerRefused as error:
                store.log_call(
                    operation,
                    None,
                    None,
                    trace,
                    "refused",
                    note,
                    **recorded,
                    response=audit.failure(error),
                )
                raise
            try:
                result = work()
            except Exception as error:
                store.log_call(
                    operation,
                    caller.oid,
                    caller.name,
                    trace,
                    "failed",
                    note,
                    **recorded,
                    response=audit.failure(error),
                )
                raise
            store.log_call(
                operation,
                caller.oid,
                caller.name,
                trace,
                "ok",
                note,
                **recorded,
                response=answer(operation, result),
            )
            return result

    if system not in _INSTRUCTIONS:
        raise ValueError(f"Unknown system: {system}")
    mcp = MCPServer(name=f"airrec-{system}", instructions=_INSTRUCTIONS[system])

    async def get_aircraft_rotation(aircraft_reg: str, ctx: Context) -> dict[str, Any]:
        headers = dict(ctx.headers or {})
        try:
            result: dict[str, Any] = await anyio.to_thread.run_sync(
                lambda: guarded(
                    "get_aircraft_rotation",
                    headers,
                    lambda: service.aircraft_rotation(aircraft_reg, lisbon_today()),
                    mcp=True,
                    request={"aircraft_reg": aircraft_reg},
                )
            )
        except CallerRefused as error:
            raise ToolError(f"refused: {error}") from error
        return result

    def by_facts(
        operation: str,
        headers: Mapping[str, str],
        values: Mapping[str, Any],
        work: Callable[[dict[str, Any]], Any],
        *,
        mcp: bool = False,
        option: tuple[str, int] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> Any:
        """A read about one disruption by its facts (owner: the systems are only read during
        an assessment, and never by ARROW's case): the facts as this system would record the
        disruption now, checked after the caller, audited under the caller's trace (Foundry
        carries the case's traceparent into the agents' calls). Facts that name no disruption
        today are refused (FactsRefused, or ValueError when they do not fit today's
        operation), an agent's tool call as a REST read."""
        refusal: ValueError | None = None
        facts: Facts | None = None
        try:
            facts = facts_of(values)
            request: dict[str, Any] = facts.named()
        except FactsRefused as error:
            refusal = error
            # What was asked, in the facts' own names only.
            request = {key: values[key] for key in FACT_NAMES if key in values}
        detail = None
        if option is not None:
            request |= {"option_id": option[0], "additional_delay_minutes": option[1]}
            detail = {"option_id": option[0], "added_minutes": option[1]}
        request |= dict(extra or {})

        def answered() -> Any:
            if refusal is not None:
                raise refusal
            assert facts is not None
            return work(reported(store, facts))

        return guarded(operation, headers, answered, mcp=mcp, request=request, detail=detail)

    def for_event(
        operation: str,
        headers: Mapping[str, str],
        event: str,
        work: Callable[[str], Any],
    ) -> Any:
        """A read about one defect this system recorded, by the event id it issued for it:
        answered from the trace it recorded it under, audited and traced there, and named by its
        event id. An unknown or ambiguous event is refused after the caller is checked."""
        trace: str | None = None
        refusal: ValueError | None = None
        try:
            trace = event_ids.trace_of(store, event, lisbon_today())
        except ValueError as error:
            refusal = error

        def answered() -> Any:
            if refusal is not None:
                raise refusal
            assert trace is not None
            return work(trace)

        result = guarded(
            operation, headers, answered, mcp=True, request={"event_id": event}, event_trace=trace
        )
        return _named(result, event)

    async def facts_tool(
        operation: str,
        ctx: Context,
        disruption: Mapping[str, Any],
        work: Callable[[dict[str, Any]], Any],
        option: tuple[str, int] | None = None,
    ) -> dict[str, Any]:
        """An agent's tool call about one disruption, by its facts, checked and audited as a
        REST read is."""
        headers = dict(ctx.headers or {})
        try:
            result: dict[str, Any] = await anyio.to_thread.run_sync(
                lambda: by_facts(operation, headers, disruption, work, mcp=True, option=option)
            )
        except (CallerRefused, ValueError) as error:
            raise ToolError(str(error)) from error
        return result

    async def get_defect(event_id: str, ctx: Context) -> dict[str, Any]:
        """Read a defect Maintenance recorded. event_id: the operations event id Maintenance
        issued when it recorded the defect (EV- and six hex digits)."""
        headers = dict(ctx.headers or {})
        try:
            result: dict[str, Any] = await anyio.to_thread.run_sync(
                lambda: for_event("get_defect", headers, event_id, defects.read)
            )
        except (CallerRefused, ValueError) as error:
            raise ToolError(str(error)) from error
        return result

    async def get_recovery_options(disruption: DisruptionArgument, ctx: Context) -> dict[str, Any]:
        return await facts_tool(
            "get_recovery_options",
            ctx,
            disruption,
            lambda found: grounded_options(store, found, inventory=inventory_of(store, found)),
        )

    async def get_disruption(disruption: DisruptionArgument, ctx: Context) -> dict[str, Any]:
        return await facts_tool(
            "get_disruption", ctx, disruption, lambda d: disruption_with_knock_on(store, d)
        )

    async def get_reserve_options(
        disruption: DisruptionArgument, option_id: str, additional_delay_minutes: int, ctx: Context
    ) -> dict[str, Any]:
        caller = trace_id(dict(ctx.headers or {}))
        return await facts_tool(
            "get_reserve_options",
            ctx,
            disruption,
            lambda d: reserves.options(d, option_id, additional_delay_minutes, caller=caller),
            (option_id, additional_delay_minutes),
        )

    async def get_rebooking_plan(
        disruption: DisruptionArgument, option_id: str, additional_delay_minutes: int, ctx: Context
    ) -> dict[str, Any]:
        result = await facts_tool(
            "get_rebooking_plan",
            ctx,
            disruption,
            lambda d: rebooking.plan(d, option_id, additional_delay_minutes),
            (option_id, additional_delay_minutes),
        )
        # Agents get counts only; ARROW reads journey facts over REST.
        return {
            key: value for key, value in result.items() if key not in {"assignments", "journeys"}
        }

    async def assess_domain_recovery(
        disruption: DisruptionArgument,
        option_id: str,
        additional_delay_minutes: int,
        ctx: Context,
    ) -> dict[str, Any]:
        return await facts_tool(
            f"assess_{system}",
            ctx,
            disruption,
            lambda d: assessments.assess(system, d, option_id, additional_delay_minutes),
            (option_id, additional_delay_minutes),
        )

    if system == "schedule":
        mcp.tool(name="get_aircraft_rotation", description=_INSTRUCTIONS[system])(
            get_aircraft_rotation
        )
        mcp.tool(description=DESCRIPTIONS["get_recovery_options"])(get_recovery_options)
        mcp.tool(description=DESCRIPTIONS["get_disruption"])(get_disruption)
    elif system == "maintenance":
        mcp.tool()(get_defect)
    elif system in _DOMAIN_TOOLS:
        mcp.tool(
            name=_DOMAIN_TOOLS[system],
            description=f"{_INSTRUCTIONS[system]} {DISRUPTION_ARGUMENT} option_id and"
            " additional_delay_minutes as Schedule's recovery options give them.",
        )(assess_domain_recovery)
        if system == "crew":
            mcp.tool(description=DESCRIPTIONS["get_reserve_options"])(get_reserve_options)
        elif system == "booking":
            mcp.tool(description=DESCRIPTIONS["get_rebooking_plan"])(get_rebooking_plan)

    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True, host="0.0.0.0"
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            yield

    api = FastAPI(title=f"airrec-{system}", lifespan=lifespan, docs_url=None, redoc_url=None)

    @api.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/api/network")
    async def network(request: Request) -> dict[str, Any]:
        if system != "schedule":
            raise HTTPException(404, "Not a schedule system")
        try:
            result: dict[str, Any] = await anyio.to_thread.run_sync(
                lambda: guarded(
                    "get_network", dict(request.headers), lambda: service.network(lisbon_today())
                )
            )
            return result
        except CallerRefused as error:
            raise HTTPException(403, str(error)) from error

    @api.get("/api/flights/{flight_no}/summary")
    async def booking_flight_summary(
        flight_no: Annotated[str, Path(pattern=r"^[A-Za-z0-9]{2,8}$")],
        request: Request,
    ) -> dict[str, Any]:
        if system != "booking":
            raise HTTPException(404, "Not a booking system")
        normalized = flight_no.strip().upper()
        try:
            result = cast(
                dict[str, Any] | None,
                await anyio.to_thread.run_sync(
                    lambda: guarded(
                        "get_flight_summary",
                        dict(request.headers),
                        lambda: cast(BookingSummaryStore, store).booking_flight_summary(
                            lisbon_today(), normalized
                        ),
                        request={"flight_no": normalized},
                    ),
                ),
            )
        except CallerRefused as error:
            raise HTTPException(403, str(error)) from error
        if result is None:
            raise HTTPException(404, "Flight not found")
        return result

    @api.post("/api/recovery/holds")
    async def create_recovery_hold(
        hold_request: RecoveryHoldRequest,
        request: Request,
    ) -> JSONResponse:
        if system != "booking":
            raise HTTPException(404, "Not a booking system")
        headers = dict(request.headers)
        held = {
            "case": hold_request.trace_id,
            "option_id": hold_request.option_id,
            "source_flight_no": hold_request.source_flight_no.strip().upper(),
            "assessment_version": hold_request.assessment_version,
        }
        if not passenger_holds_enabled:
            disabled = "Seat holds are disabled in this increment"
            refused("create_recovery_hold", headers, disabled, held)
            raise HTTPException(403, disabled)
        try:
            caller_with_role(headers, writer_oids, WRITE_ROLE)
        except CallerRefused as error:
            refused("create_recovery_hold", headers, error, held)
            raise HTTPException(403, str(error)) from error
        try:
            receipt, created = await anyio.to_thread.run_sync(
                lambda: guarded(
                    "create_recovery_hold",
                    headers,
                    lambda: passenger_holds.create(
                        trace_id=hold_request.trace_id,
                        option_id=hold_request.option_id,
                        source_flight_no=hold_request.source_flight_no.strip().upper(),
                        assessment_version=hold_request.assessment_version,
                        idempotency_key=hold_request.idempotency_key,
                    ),
                    request=held,
                    role=WRITE_ROLE,
                )
            )
        except CallerRefused as error:
            raise HTTPException(403, str(error)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return JSONResponse(status_code=201 if created else 200, content=receipt)

    async def facts_read(
        operation: str,
        owners: frozenset[str],
        request: Request,
        work: Callable[[dict[str, Any]], Any],
        *,
        option: tuple[str, int] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """ARROW's read about one disruption at this system's own route, by its facts: the
        query parameters (airline_systems.facts). Facts that name no disruption are refused with
        422, facts that do not fit today's operation with 409."""
        if system not in owners:
            raise HTTPException(404, f"Not a {' or '.join(sorted(owners))} system")
        headers = dict(request.headers)
        values = dict(request.query_params)
        try:
            result: dict[str, Any] = await anyio.to_thread.run_sync(
                lambda: by_facts(operation, headers, values, work, option=option, extra=extra)
            )
        except CallerRefused as error:
            raise HTTPException(403, str(error)) from error
        except FactsRefused as error:
            raise HTTPException(422, str(error)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return result

    schedule_only = frozenset({"schedule"})

    @api.get("/api/disruption")
    async def disruption(request: Request) -> dict[str, Any]:
        """The disruption as operations control sees it, and its knock-on if nothing changes."""
        return await facts_read(
            "get_disruption",
            schedule_only,
            request,
            lambda found: disruption_with_knock_on(store, found),
        )

    @api.get("/api/recovery-options")
    async def recovery_options(
        request: Request, additional_delay_minutes: Delay = 0
    ) -> dict[str, Any]:
        return await facts_read(
            "get_recovery_options",
            schedule_only,
            request,
            lambda found: grounded_options(
                store, found, additional_delay_minutes, inventory=inventory_of(store, found)
            ),
            extra={"additional_delay_minutes": additional_delay_minutes},
        )

    @api.get("/api/dispatch-advisories")
    async def dispatch_advice(request: Request) -> dict[str, Any]:
        """Advice for dispatch in a weather forecast (FUEL-FLAG, ALT-FLAG); read-only, it changes
        nothing."""
        return await facts_read(
            "get_dispatch_advisories",
            schedule_only,
            request,
            lambda found: dispatch_advisories(store, found),
        )

    @api.get("/api/recovery-options/{option_id}/impact")
    async def domain_assessment(
        option_id: OptionId, request: Request, additional_delay_minutes: Delay = 0
    ) -> dict[str, Any]:
        return await facts_read(
            f"assess_{system}",
            frozenset(_DOMAIN_TOOLS),
            request,
            lambda found: assessments.assess(system, found, option_id, additional_delay_minutes),
            option=(option_id, additional_delay_minutes),
        )

    @api.get("/api/recovery-options/{option_id}/rebooking-plan")
    async def rebooking_plan(
        option_id: OptionId, request: Request, additional_delay_minutes: Delay = 0
    ) -> dict[str, Any]:
        result = await facts_read(
            "get_rebooking_plan",
            frozenset({"booking"}),
            request,
            lambda found: rebooking.plan(found, option_id, additional_delay_minutes),
            option=(option_id, additional_delay_minutes),
        )
        # Journey facts (no booking reference) are for ARROW's cost estimate: the writers' and
        # the listed journey readers' (ARROW's tools app prices plans and never writes); agents
        # get counts.
        hidden = {"assignments"}
        headers = dict(request.headers)
        try:
            caller_with_role(headers, writer_oids, WRITE_ROLE)
        except CallerRefused:
            try:
                caller_with_role(headers, journey_reader_oids, reader)
            except CallerRefused:
                hidden.add("journeys")
        return {key: value for key, value in result.items() if key not in hidden}

    @api.get("/api/recovery-options/{option_id}/standby-crews")
    async def reserve_options(
        option_id: OptionId, request: Request, additional_delay_minutes: Delay = 0
    ) -> dict[str, Any]:
        # The case asking keeps the standby crews it has called itself.
        caller = trace_id(dict(request.headers))
        return await facts_read(
            "get_reserve_options",
            frozenset({"crew"}),
            request,
            lambda found: reserves.options(
                found, option_id, additional_delay_minutes, caller=caller
            ),
            option=(option_id, additional_delay_minutes),
        )

    @api.get("/api/recovery-options/{option_id}/duty-plan")
    async def duty_plan(
        option_id: OptionId,
        request: Request,
        additional_delay_minutes: Delay = 0,
        reserve_option_id: str | None = Query(default=None, pattern=r"^R[0-9]{1,2}$"),
        reserve_option_ids: list[str] = Query(default=[], max_length=20),
    ) -> dict[str, Any]:
        """The crew duty changes of an option, with every standby crew in
        ``reserve_option_ids`` (repeated), or the single ``reserve_option_id``."""
        if system == "crew" and not all(
            RESERVE_OPTION_ID.fullmatch(item) for item in reserve_option_ids
        ):
            raise HTTPException(422, "reserve_option_ids are standby crew options R1 to R99")
        caller = trace_id(dict(request.headers))
        result = await facts_read(
            "get_duty_plan",
            frozenset({"crew"}),
            request,
            lambda found: DutyPlanService(store).plan(
                found,
                option_id,
                additional_delay_minutes,
                reserve_option_id,
                reserve_option_ids=reserve_option_ids,
                caller=caller,
            ),
            option=(option_id, additional_delay_minutes),
            extra={
                "reserve_option_id": reserve_option_id,
                # Only when sent: one standby crew may be named alone.
                **({"reserve_option_ids": reserve_option_ids} if reserve_option_ids else {}),
            },
        )
        return {key: value for key, value in result.items() if key != "changes"}

    @api.get("/api/capabilities")
    async def capabilities(request: Request) -> dict[str, Any]:
        try:
            caller_with_role(dict(request.headers), allowed_oids, reader)
        except CallerRefused as error:
            raise HTTPException(403, str(error)) from error
        return actions.capabilities()

    @api.get("/api/airports")
    async def airport_rules(request: Request) -> dict[str, Any]:
        if system != "slots":
            raise HTTPException(404, "Not a slots system")
        try:
            rules = await anyio.to_thread.run_sync(
                lambda: guarded(
                    "get_airport_rules",
                    dict(request.headers),
                    lambda: store.airport_rules(),  # type: ignore[attr-defined]
                )
            )
        except CallerRefused as error:
            raise HTTPException(403, str(error)) from error
        return {"airports": rules}

    async def native_call(
        operation: str,
        owner: str,
        request: Request,
        named: Mapping[str, Any] | None = None,
    ) -> JSONResponse:
        """A call at a recipient's own interface (native dispatch spec §3), audited with the
        call's X-Request-Id; a write needs ARROW's identity and its Actions.Write role."""
        if system != owner:
            raise HTTPException(404, f"Not a {owner} system")
        headers = dict(request.headers)
        body: dict[str, Any] | None = None
        if request.method != "GET":
            try:
                parsed = await request.json()
            except ValueError:
                parsed = None
            if not isinstance(parsed, dict):
                raise HTTPException(400, "The body must be a JSON object")
            body = parsed
        traced = trace_id(headers)
        sent = {
            # The write's operations event, as this system issued it for the case's record.
            "event_id": event_ids.event_id(traced) if traced else None,
            "request_id": headers.get("x-request-id"),
            **(named or {}),
        }
        unread: str | None = None
        try:
            validated = playbook_body(request.url.path, body or {}) if body is not None else None
        except BodyRefused as error:
            validated, unread = None, str(error)
        if validated is not None:
            # A playbook write records only what its interface validated.
            sent.update(audit.call_request(validated[1]))
        elif unread is None:
            sent.update(audit.call_request(body or {}))
        write = request.method != "GET"
        if write:
            try:
                caller_with_role(headers, writer_oids, WRITE_ROLE)
            except CallerRefused as error:
                refused(operation, headers, error, sent)
                raise HTTPException(403, str(error)) from error
        if unread is not None:
            refused(operation, headers, unread, sent)
            return JSONResponse(status_code=400, content={"detail": unread})

        def reply() -> tuple[int, dict[str, Any]]:
            status, answer = native.handle(request.method, request.url.path, headers, body)
            if status >= 400:
                raise _Declined(status, answer)
            return status, answer

        try:
            status, answer = await anyio.to_thread.run_sync(
                lambda: guarded(
                    operation,
                    headers,
                    reply,
                    request=sent,
                    role=WRITE_ROLE if write else reader,
                )
            )
        except CallerRefused as error:
            raise HTTPException(403, str(error)) from error
        except _Declined as declined:
            return JSONResponse(status_code=declined.status, content=declined.answer)
        return JSONResponse(status_code=status, content=answer)

    @api.patch("/api/flights/{flight_no}")
    async def update_flight(
        flight_no: Annotated[str, Path(pattern=r"^[A-Za-z0-9]{2,8}$")], request: Request
    ) -> JSONResponse:
        return await native_call(
            "update_flight", "schedule", request, {"flight_no": flight_no.upper()}
        )

    # Only ARROW's executor records a reported disruption, as the first action of a plan it
    # carries out (a delay in operations control, a defect in Maintenance); the system issues its
    # event id then.
    @api.post("/api/delays")
    async def record_delay(request: Request) -> JSONResponse:
        return await native_call("record_delay", "schedule", request)

    @api.post("/api/defects")
    async def record_defect(request: Request) -> JSONResponse:
        return await native_call("record_defect", "maintenance", request)

    @api.post("/api/duty-changes")
    async def submit_duty_changes(request: Request) -> JSONResponse:
        return await native_call("submit_duty_changes", "crew", request)

    @api.post("/api/rebookings")
    async def submit_rebooking(request: Request) -> JSONResponse:
        return await native_call("submit_rebooking", "booking", request)

    @api.post("/api/passenger-notices")
    async def send_passenger_notice(request: Request) -> JSONResponse:
        return await native_call("send_passenger_notice", "booking", request)

    # A storm case's standby crews (case summary and held storm plan): on alert, called at their
    # call-by time, released when the storm is stood down.
    @api.post("/api/standby-alerts")
    async def alert_standby_crews(request: Request) -> JSONResponse:
        return await native_call("alert_standby_crews", "crew", request)

    @api.post("/api/standby-alerts/call")
    async def call_standby_crews(request: Request) -> JSONResponse:
        return await native_call("call_standby_crews", "crew", request)

    @api.post("/api/standby-alerts/release")
    async def release_standby_crews(request: Request) -> JSONResponse:
        return await native_call("release_standby_crews", "crew", request)

    @api.post("/ground-handling/transfer-orders")
    async def order_transfer(request: Request) -> JSONResponse:
        return await native_call("order_transfer", "booking", request)

    @api.post("/nm/messages")
    async def send_nm_message(request: Request) -> JSONResponse:
        return await native_call("send_nm_message", "slots", request)

    @api.post("/acdm/flights/{flight_no}/tobt")
    async def update_tobt(
        flight_no: Annotated[str, Path(pattern=r"^[A-Za-z0-9]{2,8}$")], request: Request
    ) -> JSONResponse:
        return await native_call("update_tobt", "slots", request, {"flight_no": flight_no.upper()})

    @api.post("/slot-coordinator/requests")
    async def request_slot(request: Request) -> JSONResponse:
        return await native_call("request_slot", "slots", request)

    @api.get("/slot-coordinator/requests/{request_id}")
    async def get_slot_request(
        request_id: Annotated[str, Path(pattern=r"^[a-z0-9-]{8,80}$")], request: Request
    ) -> JSONResponse:
        # The coordinator's answer joins the request it answers on the trail.
        return await native_call("get_slot_request", "slots", request, {"request_id": request_id})

    @api.get("/api/aircraft/{aircraft_reg}/rotation")
    async def rotation(aircraft_reg: str, request: Request) -> dict[str, Any]:
        if system != "schedule":
            raise HTTPException(404, "Not a schedule system")
        headers = dict(request.headers)
        try:
            result: dict[str, Any] = await anyio.to_thread.run_sync(
                lambda: guarded(
                    "get_aircraft_rotation",
                    headers,
                    lambda: service.aircraft_rotation(aircraft_reg, lisbon_today()),
                    request={"aircraft_reg": aircraft_reg},
                )
            )
        except CallerRefused as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        return result

    api.mount("/", mcp_app)
    return api


def main() -> None:
    import uvicorn
    from azure.identity import ManagedIdentityCredential

    from airline_systems.sql import SqlScheduleStore

    system = os.environ["SYSTEM"]
    credential = ManagedIdentityCredential(client_id=os.environ["AZURE_CLIENT_ID"])
    telemetry.configure(system, credential, logger_name="airline_systems")
    store = SqlScheduleStore(
        server=os.environ["SQL_SERVER"],
        database=os.environ["SQL_DATABASE"],
        client_id=os.environ["AZURE_CLIENT_ID"],
        system=system,
        credential=credential,
    )
    app = create_app(
        store,
        parse_allowed(os.environ["ALLOWED_CALLER_OIDS"]),
        system=system,
        writer_oids=parse_allowed(os.environ["WRITER_OIDS"])
        if os.getenv("WRITER_OIDS")
        else frozenset(),
        journey_reader_oids=parse_allowed(os.environ["JOURNEY_READER_OIDS"])
        if os.getenv("JOURNEY_READER_OIDS")
        else frozenset(),
        mock_confirm_seconds=float(os.getenv("MOCK_CONFIRM_SECONDS", "5")),
        mock_refuse_kinds=frozenset(
            kind.strip() for kind in os.getenv("MOCK_REFUSE_KINDS", "").split(",") if kind.strip()
        ),
    )
    telemetry.instrument(app)
    uvicorn.run(app, host="0.0.0.0", port=8080, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
