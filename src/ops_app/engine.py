"""The operator-side event handler. Hosted Foundry owns specialist orchestration."""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import random
import re
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

import pyodbc
from azure.core.credentials import TokenCredential
from azure.core.exceptions import AzureError
from opentelemetry.trace import SpanKind

from airline_systems import case_ids, telemetry
from airline_systems.knock_on import knock_on
from airline_systems.operating_day import CITY, weather_rate
from airline_systems.schedule import DEMO_CLOCK, lisbon_today
from ops_app.planner import CaseRef, disruption_facts
from ops_app.policy import (
    CREW_AFTER_RESERVE_REASON,
    CREW_AFTER_STANDBY_CREWS_REASON,
    CREW_AFTER_STANDBY_REASON,
    CREW_BREACH_REASONS,
    CREW_LIMIT_REASON,
    MAX_PLANS,
    Limits,
)
from ops_app.reports import (
    HAZARD_WORDS,
    Reading,
    crosswind_details,
    storm_period,
    storm_title,
    storm_window,
)
from ops_app.rules import MemoryRules, RuleStore
from ops_app.runlog import record_run
from ops_app.store import (
    AssessmentInactive,
    CaseChange,
    CaseStore,
    DuplicateEvent,
    SubjectBusy,
    Unchanged,
    reassessable,
)

logger = logging.getLogger("ops_app")
ORCHESTRATOR_AGENT = "recovery-orchestrator-agent"
CASE_ASSISTANT_AGENT = "case-assistant-agent"
LISBON = ZoneInfo("Europe/Lisbon")
# The domain agents that call ARROW's own tools, by the actor their case events name, and their
# autonomy levels (scenario: detection L4, passenger re-accommodation L3).
AUTONOMY = {"detection": "L4", "passenger": "L3"}

# Queued assessments (Service Bus intake spec §3.4): deterministic retry rules, never the model's.
MAX_ATTEMPTS = 4
FIRST_WAIT_SECONDS = 15.0
MAX_WAIT_SECONDS = 120.0
MAX_RETRY_AFTER_SECONDS = 300.0
JITTER = 0.2
ASSESSMENT_WINDOW = timedelta(minutes=15)
MIN_START = timedelta(seconds=90)
MIN_RETRY = timedelta(minutes=4)
ORCHESTRATOR_TIMEOUT_SECONDS = 600.0
SETTLE_MARGIN_SECONDS = 45.0
OVERDUE = timedelta(seconds=60)
# A storm held until confirmed: how often its standby crews' call-by times are checked.
HELD_TICK_SECONDS = 60.0
# A storm case a weather update may settle: being assessed, or ready (approved or not).
STORM_OPEN = frozenset({"assessing", "assessment_ready"})


def brief_limits(limits: Limits) -> dict[str, Any]:
    """The limits the orchestrator's brief carries: the rule version in force and fixed rules."""
    return {
        "rules_version": limits.version,
        "cost_cap_eur": limits.cost_cap_eur,
        "rebook_limit": limits.rebook_limit,
        "max_plans": MAX_PLANS,
        "eu261_care_threshold_hours": {
            "up_to_1500_km": 2,
            "1500_to_3500_km_or_intra_eu_over_1500_km": 3,
            "longer": 4,
        },
    }


class Gateway(Protocol):
    def network(self, trace: str) -> dict[str, Any]: ...
    def flight_details(self, flight_no: str, trace: str) -> dict[str, Any]: ...
    def prepare(
        self, document: dict[str, Any], progress: Callable[[str, str], None]
    ) -> dict[str, Any]: ...
    def orchestrate(
        self, trace: str, brief: dict[str, Any], *, timeout: float = 600
    ) -> dict[str, Any]: ...
    def ask_case_assistant(
        self, trace: str, brief: dict[str, Any], *, timeout: float
    ) -> dict[str, Any]: ...
    def create_passenger_hold(
        self,
        trace: str,
        option_id: str,
        source_flight_no: str,
        assessment_version: int,
        idempotency_key: str,
    ) -> dict[str, Any]: ...
    def send(
        self,
        system: str,
        method: str,
        path: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
        trace: str,
    ) -> dict[str, Any]: ...
    def rotation(self, trace: str, aircraft_reg: str) -> dict[str, Any]: ...
    def rebooking(
        self, case: CaseRef, option_id: str, additional_delay_minutes: int
    ) -> dict[str, Any]: ...
    def duty_plan(
        self,
        case: CaseRef,
        option_id: str,
        additional_delay_minutes: int,
        reserve_option_ids: Sequence[str] = (),
    ) -> dict[str, Any]: ...
    def airports(self, trace: str) -> dict[str, Any]: ...
    def options(self, case: CaseRef, additional_delay_minutes: int) -> dict[str, Any]: ...
    def assessment(
        self, domain: str, case: CaseRef, option_id: str, additional_delay_minutes: int
    ) -> dict[str, Any]: ...
    def reserve_options(
        self, case: CaseRef, option_id: str, additional_delay_minutes: int
    ) -> dict[str, Any]: ...
    def dispatch_advisories(self, case: CaseRef) -> dict[str, Any]: ...
    def clock(self) -> str: ...


class CloudDependencyError(RuntimeError):
    """A system request failed; no replacement data may be fabricated.

    ``retryable``: a temporary condition (HTTP 408, 429, 5xx, or a lost connection).
    ``may_continue``: the request may have reached the service and still be running there (a
    timeout or a dropped connection after sending). ``retry_after``: the service's own wait.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        detail: str | None = None,
        code: str | None = None,
        retryable: bool = False,
        may_continue: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail
        self.code = code
        self.retryable = retryable
        self.may_continue = may_continue
        self.retry_after = retry_after


RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
# Foundry run error codes that mean "try again later". An unknown code is never retried.
RETRYABLE_RUN_CODES = frozenset(
    {"rate_limit_exceeded", "server_error", "internal_error", "service_unavailable"}
)


def retry_after(headers: Any) -> float | None:
    """The service's requested wait in seconds (Retry-After or retry-after-ms), if any."""
    if headers is None:
        return None
    try:
        milliseconds = headers.get("retry-after-ms")
        if milliseconds:
            return max(0.0, float(milliseconds) / 1000)
        value = headers.get("retry-after")
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            moment = parsedate_to_datetime(str(value))
            return max(0.0, (moment - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError):
        return None


class FlightNotFoundError(LookupError):
    """The selected flight is not present in the current Schedule response."""


def _completed_text(response: Mapping[str, Any], agent: str, *, last: bool = False) -> str:
    """A finished prompt-agent run's output text (with ``last``, only its final message: a run
    may write a note before a tool call, then its answer); a run that did not complete is an
    error, temporary only for Foundry's own "try again later" codes."""
    if response.get("status") != "completed":
        failure = response.get("error") or {}
        incomplete = response.get("incomplete_details") or {}
        code = str(
            (failure.get("code") if isinstance(failure, dict) else None)
            or (incomplete.get("reason") if isinstance(incomplete, dict) else None)
            or ""
        )
        raise CloudDependencyError(
            f"{agent} did not complete: {response.get('status')}" + (f" ({code})" if code else ""),
            retryable=code in RETRYABLE_RUN_CODES,
        )
    messages = [
        "".join(
            str(content.get("text", ""))
            for content in item.get("content") or []
            if isinstance(content, dict) and content.get("type") == "output_text"
        )
        for item in response.get("output") or []
        if isinstance(item, dict) and item.get("type") == "message"
    ]
    if last:
        return messages[-1] if messages else ""
    return "".join(messages)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


class CloudGateway:
    def __init__(
        self,
        credential: TokenCredential,
        systems_app_id: str,
        schedule_url: str,
        maintenance_url: str,
        project_endpoint: str,
        *,
        booking_url: str,
        crew_url: str = "",
        slots_url: str = "",
        writes: bool = True,
    ) -> None:
        self.credential = credential
        # Only the workers app's gateway writes; the operator and tools apps refuse a write before
        # any token is requested (three container apps spec §5), and the systems refuse them too.
        self.writes = writes
        # The plain app ID, not api://: an equivalent audience the systems accept. Managed-identity
        # tokens are cached per resource for up to 24 h, so the api:// token fetched just before
        # the app roles were assigned would have lacked the roles claim until it expired.
        self.systems_scope = f"{systems_app_id}/.default"
        self.schedule_url = schedule_url
        self.maintenance_url = maintenance_url
        self.project_endpoint = project_endpoint
        self.booking_url = booking_url
        self.system_urls = {
            "schedule": schedule_url,
            "maintenance": maintenance_url,
            "booking": booking_url,
            "crew": crew_url,
            "slots": slots_url,
        }

    def _system_url(self, system: str) -> str:
        url = self.system_urls.get(system)
        if not url:
            raise CloudDependencyError(f"The {system} system is not configured for ARROW.")
        return url

    def _writing(self) -> None:
        if not self.writes:
            raise CloudDependencyError("This ARROW app does not write to the airline systems.")

    def send(
        self,
        system: str,
        method: str,
        path: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
        trace: str,
    ) -> dict[str, Any]:
        """One call to a recipient's own interface, at the system that plays it in Dev."""
        self._writing()
        return self.request(
            f"{self._system_url(system)}{path}",
            self.systems_scope,
            trace,
            body,
            timeout=30,
            method=method,
            headers=headers,
        )

    def rotation(self, trace: str, aircraft_reg: str) -> dict[str, Any]:
        return self.request(
            f"{self.schedule_url}/api/aircraft/{quote(aircraft_reg)}/rotation",
            self.systems_scope,
            trace,
        )

    def _facts_url(
        self, system: str, case: CaseRef, path: str, query: Mapping[str, Any] | None = None
    ) -> str:
        """A read about the case's disruption at the system's own route, by its facts (owner:
        never ARROW's case or trace, and nothing recorded before a plan runs)."""
        params = {**case.disruption, **(query or {})}
        return f"{self._system_url(system)}{path}?{urlencode(params, doseq=True)}"

    def _option_url(
        self, system: str, case: CaseRef, option_id: str, what: str, query: dict[str, Any]
    ) -> str:
        return self._facts_url(
            system, case, f"/api/recovery-options/{quote(option_id)}/{what}", query
        )

    def rebooking(
        self, case: CaseRef, option_id: str, additional_delay_minutes: int
    ) -> dict[str, Any]:
        return self.request(
            self._option_url(
                "booking",
                case,
                option_id,
                "rebooking-plan",
                {"additional_delay_minutes": additional_delay_minutes},
            ),
            self.systems_scope,
            case.trace,
        )

    def duty_plan(
        self,
        case: CaseRef,
        option_id: str,
        additional_delay_minutes: int,
        reserve_option_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"additional_delay_minutes": additional_delay_minutes}
        if reserve_option_ids:
            # Every standby crew the plan calls, the parameter repeated (Crew: at most 20).
            params["reserve_option_ids"] = list(reserve_option_ids)
        return self.request(
            self._option_url("crew", case, option_id, "duty-plan", params),
            self.systems_scope,
            case.trace,
        )

    def options(self, case: CaseRef, additional_delay_minutes: int) -> dict[str, Any]:
        return self.request(
            self._facts_url(
                "schedule",
                case,
                "/api/recovery-options",
                {"additional_delay_minutes": additional_delay_minutes},
            ),
            self.systems_scope,
            case.trace,
        )

    def assessment(
        self, domain: str, case: CaseRef, option_id: str, additional_delay_minutes: int
    ) -> dict[str, Any]:
        if domain not in {"crew", "booking", "slots"}:
            raise ValueError(f"Unknown recovery domain: {domain}")
        return self.request(
            self._option_url(
                domain,
                case,
                option_id,
                "impact",
                {"additional_delay_minutes": additional_delay_minutes},
            ),
            self.systems_scope,
            case.trace,
        )

    def reserve_options(
        self, case: CaseRef, option_id: str, additional_delay_minutes: int
    ) -> dict[str, Any]:
        return self.request(
            self._option_url(
                "crew",
                case,
                option_id,
                "standby-crews",
                {"additional_delay_minutes": additional_delay_minutes},
            ),
            self.systems_scope,
            case.trace,
        )

    # The OCC playbook's read (dispatch's fuel and alternates advice for a weather forecast).
    def dispatch_advisories(self, case: CaseRef) -> dict[str, Any]:
        return self.request(
            self._facts_url("schedule", case, "/api/dispatch-advisories"),
            self.systems_scope,
            case.trace,
        )

    def airports(self, trace: str) -> dict[str, Any]:
        return self.request(f"{self._system_url('slots')}/api/airports", self.systems_scope, trace)

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
        """One call for a case; ``in_case=False`` for a call that is not part of its recovery (an
        operator's look-up): it neither joins nor carries the case's trace. ``method`` defaults
        to GET without a body and POST with one; ``headers`` are the call's own (idempotency,
        conditions, correlation)."""
        try:
            token = self.credential.get_token(scope)
        except AzureError as error:
            # No token yet (the managed identity endpoint starting, a throttled token service):
            # nothing was sent, so the call may be repeated.
            raise CloudDependencyError(
                "ARROW could not get a token for a cloud dependency; retry shortly.",
                retryable=True,
            ) from error
        method = method or ("GET" if body is None else "POST")
        target = urlsplit(url)
        # A dependency in the case's trace; its span id is the parent the service continues from.
        with telemetry.case_span(
            f"{method} {target.path}",
            trace if in_case else None,
            kind=SpanKind.CLIENT,
            attributes={
                "http.request.method": method,
                "url.full": url,
                "server.address": target.hostname or "",
            },
        ) as span:
            sent = {
                **(headers or {}),
                "Authorization": f"Bearer {token.token}",
                "Content-Type": "application/json",
            }
            if in_case:
                sent["traceparent"] = telemetry.traceparent(trace)
            request = Request(
                url,
                data=json.dumps(body).encode() if body is not None else None,
                headers=sent,
                method=method,
            )
            # The whole call ends on time: a per-read timeout never fires while bytes keep
            # trickling in (a Foundry answer that never finished once held a worker for good).
            deadline = time.monotonic() + timeout
            try:
                with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
                    span.set_attribute(
                        "http.response.status_code", getattr(response, "status", 200)
                    )
                    body_bytes = bytearray()
                    while chunk := response.read1(65536):
                        body_bytes += chunk
                        if time.monotonic() > deadline:
                            raise TimeoutError("The answer did not finish in time")
                    result: dict[str, Any] = json.loads(bytes(body_bytes))
                    return result
            except HTTPError as error:
                span.set_attribute("http.response.status_code", error.code)
                body_text = error.read(2048).decode("utf-8", errors="replace")
                try:
                    body_json = json.loads(body_text)
                except json.JSONDecodeError:
                    body_json = {}
                detail = body_json.get("detail") if isinstance(body_json, dict) else None
                # Foundry answers {"error": {"code", "message"}}; keep both for the stage and logs.
                foundry = body_json.get("error") if isinstance(body_json, dict) else None
                code = foundry.get("code") if isinstance(foundry, dict) else None
                if isinstance(code, str) and not isinstance(detail, str):
                    message = foundry.get("message") if isinstance(foundry, dict) else None
                    detail = f"{code}: {message}"[:300] if isinstance(message, str) else code
                raise CloudDependencyError(
                    f"Cloud dependency returned HTTP {error.code}; no result accepted",
                    status_code=error.code,
                    detail=detail if isinstance(detail, str) else None,
                    code=code if isinstance(code, str) else None,
                    retryable=error.code in RETRYABLE_STATUS,
                    retry_after=retry_after(error.headers),
                ) from error
            except URLError as error:
                # Never connected (refused, name not resolved, connect timeout): nothing was sent.
                raise CloudDependencyError(
                    "Cloud dependency could not be reached; retry shortly.", retryable=True
                ) from error
            except (TimeoutError, ConnectionError, http.client.HTTPException) as error:
                # Sent, but no answer arrived: the service may still be working on it.
                raise CloudDependencyError(
                    "Cloud dependency did not answer in time.", retryable=True, may_continue=True
                ) from error

    def network(self, trace: str) -> dict[str, Any]:
        return self.request(f"{self.schedule_url}/api/network", self.systems_scope, trace)

    def clock(self) -> str:
        """Schedule's operating clock (``as_of_local``), read outside any case: a held storm's
        call-by check is no part of a case's recovery, so no system audits it against one."""
        network = self.request(
            f"{self.schedule_url}/api/network", self.systems_scope, "", in_case=False
        )
        return str(network["as_of_local"])

    def flight_details(self, flight_no: str, trace: str) -> dict[str, Any]:
        normalized = flight_no.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{2,8}", normalized):
            raise ValueError("Flight number is invalid")

        network = self.network(trace)
        if not isinstance(network, dict):
            raise CloudDependencyError("Schedule returned an invalid network.")
        flights = network.get("flights")
        aircraft_items = network.get("aircraft")
        if not isinstance(flights, list) or not isinstance(aircraft_items, list):
            raise CloudDependencyError("Schedule returned an incomplete network.")
        flight = next(
            (
                item
                for item in flights
                if isinstance(item, dict) and item.get("flight_no") == normalized
            ),
            None,
        )
        if flight is None:
            raise FlightNotFoundError(normalized)

        flight_fields = (
            "flight_no",
            "origin",
            "destination",
            "std_local",
            "sta_local",
            "aircraft_reg",
            "status",
        )
        flight_summary = {field: flight.get(field) for field in flight_fields}
        registration = flight.get("aircraft_reg")
        aircraft = next(
            (
                item
                for item in aircraft_items
                if isinstance(item, dict) and item.get("registration") == registration
            ),
            None,
        )
        aircraft_summary = (
            {
                field: aircraft.get(field)
                for field in ("registration", "fleet_type", "seats", "status")
            }
            if aircraft is not None
            else None
        )

        booking = self.request(
            f"{self.booking_url}/api/flights/{normalized}/summary",
            self.systems_scope,
            trace,
        )
        if not isinstance(booking, dict):
            raise CloudDependencyError("Booking returned an invalid flight summary.")
        self._validate_booking_summary(booking, normalized)
        return {"flight": flight_summary, "aircraft": aircraft_summary, "booking": booking}

    @staticmethod
    def _validate_booking_summary(summary: dict[str, Any], flight_no: str) -> None:
        aggregate_fields = {
            "passenger_count",
            "seat_capacity",
            "held_seat_count",
            "available_seats",
            "connecting_passenger_count",
            "connections",
        }
        status = summary.get("status")
        if summary.get("flight_no") != flight_no:
            raise CloudDependencyError("Booking returned a summary for a different flight.")
        if status == "unavailable":
            if set(summary) != aggregate_fields | {"flight_no", "status", "reason"}:
                raise CloudDependencyError("Booking returned an invalid unavailable summary.")
            if not isinstance(summary["reason"], str) or not summary["reason"].strip():
                raise CloudDependencyError("Booking did not explain unavailable summary data.")
            if any(summary[field] is not None for field in aggregate_fields):
                raise CloudDependencyError("Booking returned partial unavailable summary data.")
            return
        if status != "available" or set(summary) != aggregate_fields | {"flight_no", "status"}:
            raise CloudDependencyError("Booking returned an unsupported flight summary.")

        passenger_count = summary["passenger_count"]
        seat_capacity = summary["seat_capacity"]
        held_seat_count = summary["held_seat_count"]
        available_seats = summary["available_seats"]
        connecting_count = summary["connecting_passenger_count"]
        connections = summary["connections"]
        if (
            type(passenger_count) is not int
            or passenger_count < 0
            or type(seat_capacity) is not int
            or seat_capacity <= 0
            or type(held_seat_count) is not int
            or held_seat_count < 0
            or held_seat_count > seat_capacity - passenger_count
            or type(available_seats) is not int
            or available_seats != seat_capacity - passenger_count - held_seat_count
            or type(connecting_count) is not int
            or connecting_count < 0
            or connecting_count > passenger_count
            or not isinstance(connections, list)
        ):
            raise CloudDependencyError("Booking returned inconsistent flight aggregates.")
        if any(
            not isinstance(connection, dict)
            or set(connection) != {"flight_no", "passenger_count"}
            or not isinstance(connection["flight_no"], str)
            or not re.fullmatch(r"[A-Z0-9]{2,8}", connection["flight_no"])
            or type(connection["passenger_count"]) is not int
            or connection["passenger_count"] < 0
            for connection in connections
        ):
            raise CloudDependencyError("Booking returned invalid connection aggregates.")
        if sum(connection["passenger_count"] for connection in connections) != connecting_count:
            raise CloudDependencyError("Booking returned inconsistent connection totals.")

    def prepare(
        self, document: dict[str, Any], progress: Callable[[str, str], None]
    ) -> dict[str, Any]:
        """Read what the disruption affects, by its facts, and record it nowhere (owner: the
        airline's systems are written only when a plan for it is carried out): the rotation of
        the aircraft it concerns and Schedule's options, or, for weather (no single rotation),
        the options and the flights they move. ARROW keeps the disruption on its own case."""
        trace = document["trace_id"]
        disruption = planned(document["disruption"])
        case = CaseRef(trace, disruption_facts(disruption, document.get("defect")))
        if disruption["type"] == "weather":
            progress(
                "schedule-system",
                "Loading the recovery options and the flights they move from the operations"
                " control system.",
            )
            candidates = self.options(case, 0)
            network = self.network(trace)
            if network.get("operating_date") != document["operating_date"]:
                raise ValueError("Operating day changed during the request; submit a new report")
            return storm_prepared(disruption, candidates, network.get("flights") or [])
        progress(
            "schedule-system",
            "Loading the rotation and recovery options from the operations control system.",
        )
        candidates = self.options(case, 0)
        registration = str(
            case.disruption.get("aircraft_reg")
            or (candidates.get("aircraft") or {}).get("registration")
            or ""
        )
        if not registration:
            raise CloudDependencyError("Schedule's options name no aircraft for the delay.")
        rotation = self.rotation(trace, registration)
        if rotation.get("date") != document["operating_date"]:
            raise ValueError("Operating day changed during the request; submit a new report")
        return rotation_prepared(disruption, registration, rotation, candidates)

    def orchestrate(
        self, trace: str, brief: dict[str, Any], *, timeout: float = 600
    ) -> dict[str, Any]:
        """Run the orchestrator prompt agent once; it plans through the ARROW tools."""
        try:
            response = self.request(
                f"{self.project_endpoint}/agents/{ORCHESTRATOR_AGENT}/endpoint/protocols/openai"
                "/responses?api-version=v1",
                "https://ai.azure.com/.default",
                trace,
                {"input": [{"role": "user", "content": json.dumps(brief)}], "store": True},
                timeout=timeout,
            )
        except CloudDependencyError as error:
            if error.status_code == 424 or (
                error.status_code == 400 and error.code == "tool_user_error"
            ):
                # A tool or agent the run called failed (for example a rate-limited domain
                # agent; Foundry once answered 400 tool_user_error for it); Foundry answered,
                # so the run is over and may be tried again.
                error.retryable = True
            if error.status_code in (502, 504):
                # A gateway gave up, not the run: it may still be working behind it.
                error.may_continue = True
            raise
        text = _completed_text(response, "Orchestrator")
        try:
            result = json.loads(text)
        except json.JSONDecodeError as error:
            raise ValueError("The orchestrator returned no structured result") from error
        plan_id = result.get("submitted_plan_id") if isinstance(result, dict) else None
        if (
            not isinstance(result, dict)
            or result.get("outcome") not in {"submitted", "no_plan", "no_disruption", "unreadable"}
            or not isinstance(result.get("summary"), str)
            or (plan_id is not None and not isinstance(plan_id, str))
        ):
            raise ValueError("The orchestrator returned an unsupported result")
        return {
            "outcome": result["outcome"],
            "summary": result["summary"][:600],
            "submitted_plan_id": plan_id,
            "trail": record_run(response),
        }

    def ask_case_assistant(
        self, trace: str, brief: dict[str, Any], *, timeout: float
    ) -> dict[str, Any]:
        """Ask the Case Assistant prompt agent one question in the case's trace; it reads the
        case through ARROW's tools and answers {"answer": "<text>", "refused": <bool>}, refused
        optional."""
        response = self.request(
            f"{self.project_endpoint}/agents/{CASE_ASSISTANT_AGENT}/endpoint/protocols/openai"
            "/responses?api-version=v1",
            "https://ai.azure.com/.default",
            trace,
            {"input": [{"role": "user", "content": json.dumps(brief)}], "store": True},
            timeout=timeout,
        )
        text = _completed_text(response, "The Case Assistant", last=True)
        try:
            result = json.loads(text)
        except json.JSONDecodeError as error:
            raise ValueError("The Case Assistant returned no answer") from error
        answer = result.get("answer") if isinstance(result, dict) else None
        refused = result.get("refused", False) if isinstance(result, dict) else None
        if not isinstance(answer, str) or not answer.strip() or not isinstance(refused, bool):
            raise ValueError("The Case Assistant returned no answer")
        # refused: it declined a request outside its authority (approve, carry out, rules…).
        return {"answer": answer, "refused": refused, "trail": record_run(response)}

    def first_trace_item(
        self, resource_id: str, trace: str, opened: datetime
    ) -> dict[str, str] | None:
        """The case's first request or dependency in Application Insights (owner: open
        the end-to-end transaction view, whose link names an item by its ingestion id); None
        until the case's telemetry has arrived. Reads only the component's own data."""
        if not re.fullmatch(r"[0-9a-f]{32}", trace) or not resource_id.startswith(
            "/subscriptions/"
        ):
            return None
        start = opened - timedelta(minutes=10)
        end = min(datetime.now(UTC), opened + timedelta(days=1))
        answer = self.request(
            f"https://api.loganalytics.io/v1{resource_id}/query",
            "https://api.loganalytics.io/.default",
            trace,
            {
                "query": "union requests, dependencies"
                f' | where operation_Id == "{trace}"'
                " | top 1 by timestamp asc | project itemId, timestamp, itemType",
                "timespan": f"{start.isoformat()}/{end.isoformat()}",
            },
            timeout=20,
            in_case=False,
        )
        tables = answer.get("tables") or [{}]
        rows = tables[0].get("rows") or []
        if not rows:
            return None
        item_id, timestamp, item_type = (str(value) for value in rows[0][:3])
        table = {"request": "requests", "dependency": "dependencies"}.get(item_type)
        if table is None or not re.fullmatch(r"[0-9a-fA-F-]{36}", item_id):
            return None
        return {"item_id": item_id, "timestamp": timestamp, "table": table}

    def agent_definition(self, name: str) -> dict[str, Any]:
        """An agent's current prompt, version, model and own identity as Foundry holds them
        (read only, D95)."""
        agent = self.request(
            f"{self.project_endpoint}/agents/{quote(name)}?api-version=v1",
            "https://ai.azure.com/.default",
            telemetry.current_trace() or secrets.token_hex(16),
            timeout=20,
        )
        latest = (agent.get("versions") or {}).get("latest") or agent
        definition = latest.get("definition") or {}
        identity = agent.get("instance_identity") or latest.get("instance_identity") or {}
        return {
            "version": latest.get("version"),
            "instructions": definition.get("instructions"),
            "model": definition.get("model"),
            "identity": {
                "client_id": identity.get("client_id"),
                # The object id the airline systems audit for the agent's calls.
                "principal_id": identity.get("principal_id"),
                "name": agent.get("name") or name,
            },
        }

    def create_passenger_hold(
        self,
        trace: str,
        option_id: str,
        source_flight_no: str,
        assessment_version: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        request = {
            "trace_id": trace,
            "option_id": option_id,
            "source_flight_no": source_flight_no,
            "assessment_version": assessment_version,
            "idempotency_key": idempotency_key,
        }
        self._writing()
        for attempt in range(2):
            try:
                receipt = self.request(
                    f"{self.booking_url}/api/recovery/holds",
                    self.systems_scope,
                    trace,
                    request,
                )
            except CloudDependencyError as error:
                if error.status_code == 409:
                    return {
                        "status": "blocked",
                        "reason": error.detail or "Booking deterministically blocked the hold.",
                    }
                if error.status_code is not None and 400 <= error.status_code < 500:
                    return {
                        "status": "failed",
                        "reason": f"Booking refused the hold request (HTTP {error.status_code}).",
                    }
                if attempt == 0:
                    continue
                return {
                    "status": "unknown",
                    "reason": (
                        "Booking action outcome could not be verified; do not retry with a new key."
                    ),
                }
            try:
                self._validate_passenger_hold_receipt(receipt, trace)
            except (KeyError, TypeError, ValueError):
                if attempt == 0:
                    continue
                return {
                    "status": "unknown",
                    "reason": "Booking returned no verifiable hold receipt.",
                }
            return {"status": receipt["status"], "receipt": receipt}
        raise RuntimeError("Passenger hold retry loop ended unexpectedly")

    @staticmethod
    def _validate_passenger_hold_receipt(receipt: dict[str, Any], trace: str) -> None:
        expected = {
            "action_id",
            "trace_id",
            "flight_no",
            "party_count",
            "seat_count",
            "created_at",
            "expires_at",
            "status",
        }
        if not isinstance(receipt, dict) or set(receipt) != expected:
            raise ValueError("Booking returned an invalid hold receipt")
        if (
            receipt["trace_id"] != trace
            or not isinstance(receipt["action_id"], str)
            or str(uuid.UUID(receipt["action_id"])) != receipt["action_id"]
            or not isinstance(receipt["flight_no"], str)
            or not re.fullmatch(r"[A-Z0-9]{2,8}", receipt["flight_no"])
            or type(receipt["party_count"]) is not int
            or receipt["party_count"] != 1
            or type(receipt["seat_count"]) is not int
            or receipt["seat_count"] < 1
            or receipt["status"] not in {"held", "expired"}
        ):
            raise ValueError("Booking returned inconsistent hold receipt data")
        created_at = datetime.fromisoformat(receipt["created_at"])
        expires_at = datetime.fromisoformat(receipt["expires_at"])
        if (
            created_at.tzinfo is None
            or expires_at.tzinfo is None
            or expires_at - created_at != timedelta(minutes=15)
        ):
            raise ValueError("Booking returned an invalid hold expiry")


def planned(disruption: Mapping[str, Any]) -> dict[str, Any]:
    """The disruption as ARROW plans it: a weather forecast at the reduced hourly rate the OCC
    playbook plans while its hazard lasts (operating_day.weather_rate), which the systems are
    asked with; any other as reported."""
    if disruption.get("type") != "weather" or disruption.get("hourly_rate"):
        return dict(disruption)
    rate = weather_rate(str(disruption["airport"]), str(disruption["hazard"]))
    return {**disruption, "hourly_rate": rate}


def rotation_prepared(
    disruption: Mapping[str, Any],
    registration: str,
    rotation: dict[str, Any],
    candidates: dict[str, Any],
) -> dict[str, Any]:
    """A delay or defect as ARROW records it on its case: its scope (the flights Schedule's
    options touch, on its aircraft) and its knock-on if nothing changes, with the aircraft's
    rotation and the options as read. An event its source had recorded keeps that system's
    event id."""
    return {
        "disruption": {
            **disruption,
            "scope": {
                "flights": candidates.get("affected_flight_ids") or [],
                "aircraft_reg": registration,
            },
            "knock_on": knock_on(candidates, rotation.get("flights") or []),
        },
        "rotation": rotation,
        "candidates": candidates,
    }


def storm_prepared(
    disruption: Mapping[str, Any],
    candidates: dict[str, Any],
    flights: list[dict[str, Any]],
) -> dict[str, Any]:
    """A weather forecast as ARROW records it on its case (no airline system records it: the
    met service owns it). It names no aircraft and reads no rotation: its scope is every flight
    a plan moves or cancels, on whichever aircraft, and ARROW keeps the version of each it read
    (``affected_flights``) to check the plans against. Its knock-on is what the reduced rate
    does if nothing changes."""
    affected = [str(number) for number in candidates.get("affected_flight_ids") or []]
    listed = set(affected)
    weather = candidates.get("weather") or {}
    rate = {
        key: weather[key]
        for key in ("hourly_rate", "normal_rate", "window_start_local", "window_end_local")
        if key in weather
    }
    return {
        "disruption": {
            **disruption,
            **rate,
            **({"rate_basis": weather["basis"]} if "basis" in weather else {}),
            # Now with the rate the operations control system plans the weather at.
            "title": storm_title({**disruption, **rate}),
            "scope": {"flights": affected, "aircraft_reg": None},
            "knock_on": knock_on(candidates, flights),
        },
        "affected_flights": [dict(f) for f in flights if f.get("flight_no") in listed],
        "candidates": candidates,
    }


def structured_disruption(
    report: dict[str, Any], reported: datetime
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, str]]:
    """The case's disruption (and defect) for a flight delay, aircraft defect or weather
    report."""
    if report.get("type") == "weather":
        # Code, not the reader, turns the TAF's ICAO code and UTC period into Lisbon times, and
        # computes a crosswind on Lisbon's runway from the group's wind.
        storm = storm_window(report["airport"], report["taf_window"], reported.date())
        disruption = {
            "type": "weather",
            "cause": "weather",
            "scope": {"flights": [], "aircraft_reg": None},
            **storm,
            "icao": report["airport"],
            "taf_window": report["taf_window"],
            "hazard": report["hazard"],
            **(
                crosswind_details(report["description"], report["taf_window"])
                if report["hazard"] == "crosswind"
                else {}
            ),
            "description": report["description"],
            "reported_at": reported.isoformat(),
        }
        disruption["title"] = storm_title(disruption)
        event = {
            "event": "weather_submitted",
            "message": f"Reported {disruption['title']} (TAF {report['airport']}"
            f" {report['taf_window']} UTC).",
        }
        return disruption, None, event
    if report.get("type") == "flight_delay":
        disruption = {
            "type": "flight_delay",
            "cause": report["cause"],
            "scope": {"flights": [report["flight_no"]], "aircraft_reg": None},
            "flight_no": report["flight_no"],
            "delay_minutes": report["delay_minutes"],
            "description": report["description"],
            "reported_at": reported.isoformat(),
            **_recorded_at_source(report),
        }
        event = {
            "event": "delay_submitted",
            "message": f"Reported {report['flight_no']} {report['delay_minutes']} min late"
            f" ({report['cause'].replace('_', ' ')}): {report['description']}."
            + _at_source(report),
        }
        return disruption, None, event
    defect = {
        "aircraft_reg": report["aircraft_reg"],
        "description": report["description"],
        "unavailable_minutes": report["unavailable_minutes"],
        "reported_at": reported.isoformat(),
        "unavailable_until": (
            reported + timedelta(minutes=report["unavailable_minutes"])
        ).isoformat(),
    }
    disruption = {
        "type": "aircraft_defect",
        "cause": "aircraft_defect",
        "scope": {"flights": [], "aircraft_reg": report["aircraft_reg"]},
        "description": report["description"],
        "reported_at": reported.isoformat(),
        **_recorded_at_source(report),
    }
    event = {
        "event": "defect_submitted",
        "message": f"Reported {report['description']} on {report['aircraft_reg']};"
        f" maintenance estimate {report['unavailable_minutes']} minutes." + _at_source(report),
    }
    return disruption, defect, event


def _recorded_at_source(report: Mapping[str, Any]) -> dict[str, Any]:
    """An event its source system had recorded already keeps that system's event id: ARROW
    never records it again (owner)."""
    source = report.get("source_event_id")
    return {"event_id": str(source), "recorded_at_source": True} if source else {}


def _at_source(report: Mapping[str, Any]) -> str:
    source = report.get("source_event_id")
    return f" Already recorded at its source as event {source}." if source else ""


def _subject(
    disruption: dict[str, Any], defect: dict[str, Any] | None
) -> tuple[str | None, str | None]:
    """What a recorded disruption is about, and its extent: a flight's delay, an aircraft's
    time out of service or a storm's window."""

    def minutes(value: Any) -> str | None:
        return None if value is None else f"{value} min"

    if disruption.get("type") == "flight_delay":
        return disruption.get("flight_no"), minutes(disruption.get("delay_minutes"))
    if disruption.get("type") == "aircraft_defect":
        return (disruption.get("scope") or {}).get("aircraft_reg"), minutes(
            (defect or {}).get("unavailable_minutes")
        )
    if disruption.get("type") == "weather":
        # One storm case per airport and day: the hazard and window are its figures, so another
        # reading of the same airport's weather is an update for OCC (final review).
        airport, hazard = str(disruption.get("airport")), str(disruption.get("hazard"))
        return (
            f"Weather at {CITY.get(airport, airport)}",
            f"{storm_period(disruption)} {HAZARD_WORDS.get(hazard, hazard).lower()}",
        )
    return None, None


EVENT_SOURCES = frozenset({"event", "fabric"})  # "fabric": cases opened before Service Bus intake


def _deadline(document: Mapping[str, Any]) -> datetime:
    if "_deadline_at" in document:
        return datetime.fromisoformat(document["_deadline_at"])
    return datetime.fromisoformat(document["created_at"]) + ASSESSMENT_WINDOW


def _claim_hash(held: Any) -> str | None:
    """The reading a case is recording: {"hash", "values"}, or a bare hash from earlier versions."""
    if isinstance(held, dict):
        return str(held.get("hash")) if held.get("hash") else None
    return str(held) if held else None


def _retryable(error: Exception, phase: str) -> bool:
    """Temporary and safe to repeat. ARROW's own preparation is idempotent per case; an
    orchestrator run is repeated only when Foundry answered or was never reached, never while a
    run may still be working (its late tool calls would race the retry)."""
    if isinstance(error, pyodbc.OperationalError | SubjectBusy):
        # ARROW's database dropped the connection or timed out while the case was prepared, or
        # another app was still recording the same disruption.
        return phase == "prepare"
    if not isinstance(error, CloudDependencyError) or not error.retryable:
        return False
    return phase == "prepare" or not error.may_continue


def _reason(error: Exception) -> str:
    detail = getattr(error, "detail", None)
    return (f"{error}: {detail}" if detail else str(error))[:200]


def _final(error: Exception, attempt: int, phase: str) -> Exception:
    """The error a case stops with: a temporary failure says how often it was tried."""
    if not _retryable(error, phase):
        return error
    tries = f"{attempt} attempt{'s' if attempt != 1 else ''}"
    return CloudDependencyError(f"Assessment stopped after {tries}: {_reason(error)}")


def reading_label(disruption: dict[str, Any]) -> str:
    if disruption.get("type") == "flight_delay":
        return (
            f"flight delay · {disruption['flight_no']} · {disruption['delay_minutes']} min · "
            f"{str(disruption['cause']).replace('_', ' ')}"
        )
    if disruption.get("type") == "weather":
        return f"weather · {storm_title(disruption)}"
    scope = disruption.get("scope") or {}
    return f"aircraft defect · {scope.get('aircraft_reg')}"


def _reading_key(disruption: Mapping[str, Any], defect: Mapping[str, Any] | None) -> list[Any]:
    """The values a reading names, as code normalised them: two readings of one report are the
    same reading when these match, whatever ARROW learned on recording it (the event id, the
    knock-on, a forecast's reduced rate)."""
    kind = disruption.get("type")
    if kind == "flight_delay":
        named = [disruption.get(key) for key in ("flight_no", "delay_minutes", "cause")]
    elif kind == "weather":
        named = [disruption.get(key) for key in ("icao", "taf_window", "hazard")]
    else:
        scope = disruption.get("scope") or {}
        named = [scope.get("aircraft_reg"), (defect or {}).get("unavailable_minutes")]
    return [kind, *named]


# The fields each disruption type is recorded with (owner: never every type's fields).
_TYPED_WEATHER = (
    "airport",
    "icao",
    "taf_window",
    "hazard",
    "window_start",
    "window_end",
    "hourly_rate",
    "normal_rate",
    "wind",
    "crosswind_kt",
    "runway",
)


def typed_disruption(document: Mapping[str, Any]) -> dict[str, Any]:
    """The recorded disruption in its own type's fields only: a flight delay's flight, minutes
    and cause; an aircraft defect's aircraft and time out of service; a weather forecast's
    airport, TAF period, hazard, Lisbon window and reduced rate."""
    disruption = document.get("disruption") or {}
    kind = disruption.get("type")
    scope = disruption.get("scope") or {}
    if kind == "flight_delay":
        return {
            "type": kind,
            **{key: disruption.get(key) for key in ("flight_no", "delay_minutes", "cause")},
            "aircraft_reg": scope.get("aircraft_reg"),
        }
    if kind == "aircraft_defect":
        defect = document.get("defect") or {}
        return {
            "type": kind,
            "aircraft_reg": scope.get("aircraft_reg") or defect.get("aircraft_reg"),
            "unavailable_minutes": defect.get("unavailable_minutes"),
            "unavailable_until": defect.get("unavailable_until"),
        }
    if kind == "weather":
        return {
            "type": kind,
            **{key: disruption[key] for key in _TYPED_WEATHER if key in disruption},
        }
    return {"type": kind}


def reporter(source: Mapping[str, Any]) -> str:
    """Who reported the disruption, as a brief names them."""
    return str(source.get("system") or source.get("origin") or "OCC")


def _logged(disruption: Mapping[str, Any]) -> dict[str, str]:
    """ARROW's log line once it recorded a disruption on its own case (owner): no airline system
    changes until a plan for it is carried out, which records a delay or a defect in its system
    first; a weather forecast is recorded in none. An event its source had recorded keeps that
    system's event id."""
    kind = disruption.get("type")
    what = {"flight_delay": "the delay", "aircraft_defect": "the defect"}.get(
        str(kind), "the weather forecast"
    )
    event = disruption.get("event_id")
    source = f", as its source recorded it (event {event})," if event else ""
    read = "the flights it moves" if kind == "weather" else "its rotation and recovery options"
    message = (
        f"ARROW recorded {what}{source} in the case and read {read} from the operations control"
        " system; no airline system changed."
    )
    return {
        "actor": "arrow-intake",
        "autonomy": "system",
        "event": "disruption_recorded",
        "message": message,
    }


class Operations:
    def __init__(self, store: CaseStore, gateway: Gateway, rules: RuleStore | None = None) -> None:
        self.store = store
        self.gateway = gateway
        self.rules = rules or MemoryRules()
        self._wake = threading.Event()
        self.now: Callable[[], datetime] = lambda: datetime.now(UTC)
        self.rand: Callable[[], float] = random.random
        # Sends "wake case X" to the assessment queue (scheduled when given a time).
        self.waker: Callable[[str, datetime | None], None] | None = None

    def open(
        self, report: dict[str, Any], user_oid: str, *, source: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Open a case for a reported disruption; a repeated event_id returns the same case."""
        event_id = report.get("event_id")
        if event_id:
            existing = self.store.find_by_event(str(event_id))
            if existing is not None:
                return {
                    "trace_id": existing["trace_id"],
                    "display_id": existing["display_id"],
                    "created": False,
                }
        now = self.now()
        day = lisbon_today()
        # The case id names the trace's first six hex digits and the airline's systems name the
        # recorded disruption by the next six (its operations event id): keep both unique for
        # the day, so a case id or an event id always finds this one case.
        trace = secrets.token_hex(16)
        while self.store.case_traces(day.isoformat(), trace[:6]) or self.store.event_traces(
            day.isoformat(), trace[6:12]
        ):
            trace = secrets.token_hex(16)
        reported = datetime.combine(day, DEMO_CLOCK)
        display_id = case_ids.case_id(trace, day)
        document: dict[str, Any] = {
            "trace_id": trace,
            "display_id": display_id,
            "event_id": str(event_id) if event_id else None,
            "_run_token": secrets.token_hex(32),
            "_user_oid": user_oid,
            "version": 0,
            "created_at": now.isoformat(),
            "_deadline_at": (now + ASSESSMENT_WINDOW).isoformat(),
            "assessment": {"attempt": 1, "state": "queued", "queued_at": now.isoformat()},
            "operating_date": day.isoformat(),
            "status": "assessing",
            "stage": "Connecting to airline systems.",
            "affected_flights": [],
            "options": [],
            "plans_submitted": 0,
            "events": [],
            "metrics": {},
            "source": source or {"kind": "ui"},
        }
        if report.get("type") == "message":
            text = str(report["text"]).strip()
            document["disruption"] = {
                "type": "message",
                "text": text,
                "scope": {"flights": [], "aircraft_reg": None},
                "reported_at": reported.isoformat(),
            }
            document["stage"] = "The Detection agent is reading the report."
            event = {"event": "message_received", "message": f"Reported: {text[:300]}"}
        else:
            disruption, defect, event = structured_disruption(report, reported)
            document["disruption"] = disruption
            if defect is not None:
                document["defect"] = defect
        with telemetry.case_span(
            "Open case",
            trace,
            kind=SpanKind.CONSUMER,
            attributes={
                "arrow.case": display_id,
                "arrow.disruption": str(document["disruption"]["type"]),
                "arrow.source": str(document["source"].get("kind", "")),
            },
        ):
            try:
                self.store.create(document)
            except DuplicateEvent:
                existing = self.store.find_by_event(str(event_id))
                if existing is None:
                    raise
                return {
                    "trace_id": existing["trace_id"],
                    "display_id": existing["display_id"],
                    "created": False,
                }
            reporter = (
                {"actor": "system-event", "autonomy": "system"}
                if document["source"]["kind"] in EVENT_SOURCES
                else {"actor": user_oid, "autonomy": "human"}
            )
            self.store.change(trace, {}, {**reporter, **event})
        self.wake()  # the supervisor watches queued cases while any exist
        return {"trace_id": trace, "display_id": display_id, "created": True}

    def reconcile(self, document: dict[str, Any]) -> dict[str, Any]:
        if document["status"] == "assessing" and (
            self.now() >= _deadline(document)
            or document["operating_date"] != lisbon_today().isoformat()
        ):
            options = document.get("options", [])
            pending_hold = any(
                isinstance(option, dict)
                and isinstance(option.get("passenger_action"), dict)
                and option["passenger_action"].get("status") == "pending"
                for option in options
            )
            if pending_hold:
                options = [
                    {
                        **option,
                        "passenger_action": {
                            "status": "unknown",
                            "reason": (
                                "The assessment ended while Booking was processing the hold; "
                                "verify its receipt before retrying."
                            ),
                        },
                    }
                    if isinstance(option, dict)
                    and isinstance(option.get("passenger_action"), dict)
                    and option["passenger_action"].get("status") == "pending"
                    else option
                    for option in options
                ]
                message = (
                    "Assessment stopped. The Booking action outcome is unknown; "
                    "verify it before retrying."
                )
                event_name = "passenger_hold_outcome_unknown"
            else:
                options = []
                message = (
                    "Assessment was interrupted or exceeded its deadline. "
                    "No recovery actions were executed; submit a new report."
                )
                event_name = "assessment_interrupted"
            try:
                self.store.change(
                    document["trace_id"],
                    {
                        "status": "failed",
                        "stage": message,
                        "error": message,
                        "options": options,
                    },
                    {
                        "actor": "recovery-orchestrator",
                        "autonomy": "orchestrator",
                        "event": event_name,
                        "message": message,
                    },
                )
            except AssessmentInactive:
                logger.info("Assessment %s already reconciled", document["trace_id"])
            return self.store.get(document["trace_id"])
        return document

    def load(self, trace: str) -> dict[str, Any]:
        return self.reconcile(self.store.get(trace))

    def trace_of(self, value: str) -> str:
        """The trace behind a case id an agent names (DSR-YYYYMMDD-XXXXXX)."""
        day, prefix = case_ids.parse(value)
        return case_ids.single(value, self.store.case_traces(day.isoformat(), prefix))

    def record_decision(
        self,
        trace: str,
        *,
        option_id: str,
        decision: str,
        expected_version: int,
        idempotency_key: str,
        actor_oid: str,
        comment: str | None = None,
    ) -> dict[str, Any]:
        with telemetry.case_span(
            "Decide plan",
            trace,
            root_kind=SpanKind.SERVER,
            attributes={"arrow.decision": decision, "arrow.option": option_id},
        ):
            return self.store.record_decision(
                trace,
                option_id=option_id,
                decision=decision,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                actor_oid=actor_oid,
                comment=comment,
            )

    def _airports(self, trace: str) -> dict[str, Any]:
        """The airport rules to keep on the case; none if Slots could not be read, so the
        planner reads them again rather than taking an empty set for the rules."""
        try:
            rules = self.gateway.airports(trace).get("airports", [])
        except CloudDependencyError:
            logger.warning("Airport rules unavailable for %s; the planner reads them again", trace)
            return {}
        found = {rule["iata"]: rule for rule in rules if isinstance(rule, dict)}
        return {"airports": found} if found else {}

    def execute(self, trace: str) -> None:
        from ops_app.executor import Executor

        try:
            Executor(self.store, self.gateway).run(trace)
        except Exception:
            logger.exception("Execution interrupted for %s; the supervisor resumes it", trace)
        finally:
            self.wake()

    def storm_case(self, update: Mapping[str, Any]) -> dict[str, Any] | None:
        """The open storm case a weather update settles: today's, at its airport, whose window
        overlaps the update's (code, not the model, matches them); None if there is none."""
        for case in self.store.list(lisbon_today().isoformat()):
            storm = case.get("disruption") or {}
            if (
                storm.get("type") == "weather"
                and storm.get("airport") == update["airport"]
                and case.get("status") in STORM_OPEN
                and case.get("recorded_at")
                and not case.get("repeat_of")
                and not case.get("conflicts_with")
                and str(storm.get("window_start")) < str(update["window_end"])
                and str(update["window_start"]) < str(storm.get("window_end"))
            ):
                return case
        return None

    def storm_update(self, trace: str, update: dict[str, Any]) -> bool:
        """Record a weather update on its storm case; False if the case already has it. A held
        plan it settles is carried on by the supervisor, woken here."""
        from ops_app.executor import Executor

        recorded = Executor(self.store, self.gateway).record(trace, update)
        if recorded == "duplicate":
            return False
        self.wake()
        return True

    def tick_held(self) -> int:
        """Today's storm cases held until confirmed with a standby crew on alert not yet called:
        Schedule's clock is read once, outside any case, and each crew whose call-by time has
        come is called (crew only). Returns how many such cases wait for a call-by time."""
        from ops_app.executor import Executor, uncalled

        waiting = [
            case["trace_id"]
            for case in self.store.list(lisbon_today().isoformat())
            if (case.get("execution") or {}).get("status") == "held"
            and ((case.get("execution") or {}).get("storm") or {}).get("state") == "awaiting"
            and uncalled(case["execution"].get("actions") or [])
        ]
        if not waiting:
            return 0
        try:
            clock = datetime.fromisoformat(self.gateway.clock())
        except Exception:
            logger.warning("Schedule's clock unreadable; standby call-by checked next round")
            return len(waiting)
        for trace in waiting:
            try:
                Executor(self.store, self.gateway).tick(trace, clock)
            except Exception:
                logger.exception("Standby call-by check of %s failed; it runs again", trace)
        return len(waiting)

    def wake(self) -> None:
        """Ask the supervisor to scan for executions that are still runnable."""
        self._wake.set()

    def resume(self) -> int:
        """Continue every approved, executing or waiting case; the lease keeps one executor."""
        traces = self.store.runnable_traces()
        for trace in traces:
            self._run_once(trace)
        return len(traces)

    def _run_once(self, trace: str) -> None:
        from ops_app.executor import Executor

        try:
            Executor(self.store, self.gateway).run(trace)
        except Exception:
            logger.exception("Execution interrupted for %s; retrying later", trace)

    def supervise(
        self,
        interval_seconds: float,
        *,
        rounds: int | None = None,
        idle_timeout: float | None = None,
    ) -> None:
        """Resume executions while any is runnable; when idle, wait to be woken.

        Idle means no SQL reads (it scans at startup and after each execution run, then only while
        work remains). The one exception: a storm
        held until confirmed with a standby crew on alert not yet called is work. Every
        HELD_TICK_SECONDS this database is read and Schedule's clock once (outside any case,
        so nothing is audited against the case) until each such crew is called, the storm
        confirmed or dropped, or the day Reset.
        """
        done, busy, held = 0, True, False
        while rounds is None or done < rounds:
            done += 1
            if not busy:
                woken = self._wake.wait(HELD_TICK_SECONDS if held else idle_timeout)
                if not woken and not held:
                    continue
            self._wake.clear()
            try:
                busy = self.resume() > 0
                busy = self.sweep() > 0 or busy
                held = self.tick_held() > 0
            except Exception:
                logger.exception("Execution supervisor round failed; retrying")
                busy = True
            if busy:
                time.sleep(interval_seconds)

    def sweep(self) -> int:
        """Wake every assessing case whose wake is overdue (a lost send, a crash between saving a
        case and queueing it, a dead-lettered wake, or a case past its deadline); returns how
        many cases are still being assessed."""
        cases = self.store.assessing()
        if self.waker is None:
            return len(cases)
        now = self.now()
        for case in cases:
            state = case.get("assessment") or {}
            if state.get("state") == "queued":
                due = datetime.fromisoformat(str(state.get("queued_at") or case["created_at"]))
            elif state.get("state") == "waiting":
                due = datetime.fromisoformat(str(state["not_before"]))
            else:
                due = _deadline(case)  # running: only its deadline needs a wake
            if now - due >= OVERDUE:
                try:
                    self.waker(case["trace_id"], None)
                except Exception:
                    logger.warning("Could not wake case %s; next round", case["trace_id"])
        return len(cases)

    def active(self, trace: str) -> dict[str, Any]:
        document = self.load(trace)
        if document["status"] != "assessing":
            raise AssessmentInactive("This assessment is no longer active")
        return document

    def stop(self, trace: str, message: str, event: str = "assessment_stopped") -> None:
        """Stop an assessment visibly; nothing was executed."""
        try:
            self.store.change(
                trace,
                {"status": "failed", "stage": message, "error": message, "options": []},
                {"actor": "arrow-intake", "autonomy": "system", "event": event, "message": message},
            )
        except AssessmentInactive:
            logger.info("Assessment %s had already ended", trace)

    def _subject_lock(
        self, day: str, disruption: dict[str, Any], defect: dict[str, Any] | None
    ) -> AbstractContextManager[None]:
        """One recording at a time per flight or aircraft and day, so two reports of the same
        disruption handled at once cannot both record it (D109), whichever ARROW app or replica
        handles them: the lock is the case store's (three container apps spec §5). Re-entrant:
        a tool call made while the same thread records continues, and the reading claim
        decides."""
        return self.store.subject_lock(
            f"{day}:{disruption.get('type')}:{_subject(disruption, defect)[0]}"
        )

    def run(self, trace: str) -> datetime | None:
        """One assessment attempt, if the case's queue state says one is due (Service Bus intake
        spec).

        Returns when the case wants to be woken again (a retry, or a wake that came early), or
        None. The case row decides; a queue message only wakes it, so a duplicate or stale wake
        does nothing.
        """
        try:
            document = self.load(trace)  # a case past its deadline stops here
        except KeyError:
            logger.warning("Wake for unknown case %s ignored", trace)
            return None
        if document["status"] != "assessing":
            return None
        now = self.now()
        claimed: dict[str, int] = {}

        def claim(case: dict[str, Any]) -> CaseChange:
            state = case.get("assessment") or {"attempt": 1, "state": "queued"}
            if case.get("options") or case.get("plans_submitted"):
                # A late tool call of an earlier run built a plan meanwhile: OCC decides it, and
                # no second orchestrator run starts on the case.
                raise Unchanged("settle")
            if state.get("state") == "running":
                # Another worker has it. If that worker died, the deadline stops the case.
                raise Unchanged(None)
            if state.get("state") == "waiting":
                due = datetime.fromisoformat(state["not_before"])
                if due > now:
                    raise Unchanged(due)
            if _deadline(case) - now < MIN_START:
                raise Unchanged("expired")
            claimed["attempt"] = int(state.get("attempt") or 1)
            running = {**state, "state": "running", "started_at": now.isoformat()}
            return CaseChange({"assessment": running}, ())

        try:
            self.store.mutate(trace, claim)
        except Unchanged as unchanged:
            if unchanged.result == "settle":
                self._settle(trace, None, None)
                return None
            if unchanged.result == "expired":
                self.stop(
                    trace,
                    "Assessment stopped: its 15-minute window ended before ARROW could start it."
                    " No recovery actions were executed.",
                )
                return None
            later: datetime | None = unchanged.result
            return later
        except AssessmentInactive:
            return None
        return self._attempt(trace, claimed["attempt"])

    def _attempt(self, trace: str, attempt: int) -> datetime | None:
        """Record the disruption, let the orchestrator plan through the ARROW tools, settle."""

        def progress(actor: str, message: str) -> None:
            self.active(trace)
            self.store.change(
                trace,
                {"stage": message},
                {
                    "actor": actor,
                    "autonomy": AUTONOMY.get(actor)
                    or ("system" if actor.endswith("-system") else "orchestrator"),
                    "event": "assessment_progress",
                    "message": message,
                },
            )

        phase = "prepare"
        brief: dict[str, Any] = {}
        try:
            document = self.active(trace)
            claim = document.get("_reading_claim")
            if (
                document["disruption"]["type"] == "message"
                and isinstance(claim, dict)
                and isinstance(claim.get("values"), dict)
            ):
                # An earlier attempt may have recorded this reading without hearing back: finish
                # exactly that reading before the Detection agent reads the report again.
                self.record_reading(trace, claim["values"])
                document = self.active(trace)
            if document["disruption"]["type"] == "message":
                # The Detection agent reads the report and records it with a typed tool.
                brief = self._message_brief(document)
            else:
                if not document.get("recorded_at"):
                    disruption, defect = document["disruption"], document.get("defect")
                    with self._subject_lock(document["operating_date"], disruption, defect):
                        if self._attach_repeat(trace, document, disruption, defect):
                            return None
                        self._record(trace, document, progress)
                brief = self._brief(self.active(trace))
            if document["disruption"]["type"] == "message":
                progress("detection", "The Detection agent is reading the report.")
            else:
                progress(
                    "recovery-orchestrator",
                    "The orchestrator is assessing the disruption with the domain agents.",
                )
            phase = "orchestrate"
            left = (_deadline(document) - self.now()).total_seconds() - SETTLE_MARGIN_SECONDS
            timeout = min(ORCHESTRATOR_TIMEOUT_SECONDS, left)
            if timeout < 30:
                raise CloudDependencyError("No time was left in the assessment window.")
            result = self.gateway.orchestrate(trace, brief, timeout=timeout)
        except AssessmentInactive:
            logger.warning("Ignored late work for terminal assessment %s", trace)
            return None
        except Exception as error:
            retry_at = self._retry(trace, attempt, error, phase)
            if retry_at is not None:
                logger.warning("Assessment %s retries at %s: %s", trace, retry_at, error)
                return retry_at
            logger.exception("Assessment failed for trace %s", trace)
            self._settle(trace, None, _final(error, attempt, phase))
            return None
        trail = result.pop("trail", None) if isinstance(result, dict) else None
        if isinstance(trail, dict):
            try:
                # When the run ended: the decision trail keeps an agent's calls up to then with
                # the assessment, even when a chat turn asked that agent meanwhile.
                ended = self.now().isoformat()
                self.store.save_trail(trace, {**trail, "brief": brief, "ended_at": ended})
            except Exception:
                # The trail explains a decision; it never changes or blocks one.
                logger.exception("Decision trail not saved for %s", trace)
        self._settle(trace, result, None)
        return None

    def _retry(self, trace: str, attempt: int, error: Exception, phase: str) -> datetime | None:
        """Schedule the next attempt if the failure is temporary, no plan exists yet, attempts
        and time remain; decided under the case's row lock. None means settle now."""
        if not _retryable(error, phase):
            return None
        base = min(FIRST_WAIT_SECONDS * 2 ** (attempt - 1), MAX_WAIT_SECONDS)
        wait = base * (1 + JITTER * (2 * self.rand() - 1))
        requested = error.retry_after if isinstance(error, CloudDependencyError) else None
        if requested is not None:
            wait = max(wait, min(requested, MAX_RETRY_AFTER_SECONDS))
        not_before = self.now() + timedelta(seconds=wait)
        reason = _reason(error)

        def decide(case: dict[str, Any]) -> CaseChange:
            if case.get("options") or case.get("plans_submitted"):
                raise Unchanged(None)
            if attempt >= MAX_ATTEMPTS or _deadline(case) - not_before < MIN_RETRY:
                raise Unchanged(None)
            following = attempt + 1
            message = (
                f"Waiting to retry: {reason} (attempt {following} of {MAX_ATTEMPTS} at"
                f" {not_before.astimezone(LISBON):%H:%M:%S})"
            )
            return CaseChange(
                {
                    "assessment": {
                        "attempt": following,
                        "state": "waiting",
                        "not_before": not_before.isoformat(),
                        "reason": reason,
                    },
                    "stage": message,
                },
                (
                    {
                        "actor": "arrow-intake",
                        "autonomy": "system",
                        "event": "assessment_retry_scheduled",
                        "attempt": following,
                        "not_before": not_before.isoformat(),
                        "message": message,
                    },
                ),
            )

        try:
            self.store.mutate(trace, decide)
        except (Unchanged, AssessmentInactive):
            return None
        return not_before

    def _record(
        self, trace: str, document: dict[str, Any], progress: Callable[[str, str], None]
    ) -> None:
        """Record a structured disruption on ARROW's own case, with what it affects as the
        systems answer by its facts; nothing is written to an airline system."""
        prepared = self.gateway.prepare(document, progress)
        self.active(trace)
        recorded = prepared.get("disruption") or document["disruption"]
        self.store.change(
            trace,
            {
                **prepared,
                **self._airports(trace),
                "recorded_at": self.now().isoformat(),
            },
            _logged(recorded),
        )

    def record_reading(self, trace: str, values: dict[str, Any]) -> dict[str, Any]:
        """The Detection agent's reading of a report: checked and recorded on ARROW's case with
        the original text, never in an airline system (business UI and free-text spec §3.2;
        owner: the systems are written only when a plan is carried out)."""
        document = self.active(trace)
        disruption = document["disruption"]
        text = str(disruption.get("message") or disruption.get("text") or "")
        if not text:
            raise ValueError("This case was not reported as a message; it is already recorded")
        reading = Reading(**values)
        report = reading.report(text, date.fromisoformat(document["operating_date"]))
        reported = datetime.fromisoformat(disruption["reported_at"])
        structured, defect, _ = structured_disruption(report, reported)
        wanted = _reading_key(structured, defect)

        def already(case: Mapping[str, Any]) -> bool:
            """True if the case holds this reading; ValueError if it holds a different one."""
            current = case["disruption"]
            if current.get("type") == "message":
                return False
            if _reading_key(current, case.get("defect")) != wanted:
                raise ValueError(
                    f"The message was already read as {reading_label(current)};"
                    " a case has one reading"
                )
            return True

        if already(document):
            return self._reading_result(document)
        with self._subject_lock(document["operating_date"], structured, defect):
            repeated = self._attach_repeat(trace, document, structured, defect)
            if repeated is not None:
                return repeated
            candidate = {
                **document,
                "disruption": structured,
                **({"defect": defect} if defect else {}),
            }
            # The claim is the reading itself: the same values may be sent again (a retry) and a
            # different reading is refused, so a case records one reading.
            claim = hashlib.sha256(json.dumps(wanted).encode()).hexdigest()[:16]

            def take(case: dict[str, Any]) -> CaseChange:
                if already(case):
                    raise Unchanged(None)
                held = _claim_hash(case.get("_reading_claim"))
                if held and held != claim:
                    raise ValueError(
                        "A different reading is already being recorded; a case has one"
                    )
                return CaseChange({"_reading_claim": {"hash": claim, "values": dict(values)}}, ())

            try:
                self.store.mutate(trace, take)
            except Unchanged:
                return self._reading_result(self.active(trace))

            def progress(actor: str, message: str) -> None:
                self.store.change(
                    trace,
                    {"stage": message},
                    {
                        "actor": actor,
                        "autonomy": "system",
                        "event": "assessment_progress",
                        "message": message,
                    },
                )

            try:
                prepared = self.gateway.prepare(candidate, progress)
            except Exception as error:
                # A temporary failure keeps the claim, so the next attempt finishes exactly this
                # reading. Anything else frees the case; a system's refusal of the facts (the
                # flight has left, say) is the reading's reason.
                if not (isinstance(error, CloudDependencyError) and error.retryable):
                    try:
                        self.store.mutate(
                            trace, lambda case: CaseChange({"_reading_claim": None}, ())
                        )
                    except AssessmentInactive:
                        pass
                if (
                    isinstance(error, CloudDependencyError)
                    and not error.retryable
                    and error.status_code is not None
                    and 400 <= error.status_code < 500
                ):
                    raise ValueError(
                        error.detail or f"The system refused it (HTTP {error.status_code})"
                    ) from error
                raise
            recorded = {
                **(prepared.get("disruption") or structured),
                "message": text,
                "reading": reading.reading,
            }
            changes = {
                **prepared,
                **({"defect": defect} if defect else {}),
                "disruption": recorded,
                **self._airports(trace),
                "_reading_claim": None,
                "recorded_at": self.now().isoformat(),
            }

            def store_reading(case: dict[str, Any]) -> CaseChange:
                if already(case):
                    raise Unchanged(None)
                if _claim_hash(case.get("_reading_claim")) not in (claim, None):
                    raise ValueError("A different reading was recorded meanwhile; a case has one")
                return CaseChange(
                    changes,
                    (
                        {
                            "actor": "detection",
                            "autonomy": "L4",
                            "event": "disruption_read",
                            "message": f"Read the report as {reading_label(recorded)}:"
                            f" {reading.reading}",
                        },
                        _logged(recorded),
                    ),
                )

            try:
                self.store.mutate(trace, store_reading)
            except Unchanged:
                pass
            return self._reading_result(self.active(trace))

    def _attach_repeat(
        self,
        trace: str,
        document: dict[str, Any],
        disruption: dict[str, Any],
        defect: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """A flight or aircraft that already has an open case today: attach, never record twice.

        Recording a second report would add its delay on top of the first (an owner test:
        AR259 +20 sent four times). The same figures close this case as a repeat; other figures
        are an update, so this case stops with both for OCC and nothing is recorded. If the case
        that recorded it stopped before any plan, the same figures assess that case again.
        """
        subject, minutes = _subject(disruption, defect)
        # Only a case that recorded its disruption holds it; a form or system event carries its
        # figures before that, and a repeat or stopped update never does.
        original = next(
            (
                case
                for case in self.store.list(document["operating_date"])
                if case["trace_id"] != trace
                and case.get("recorded_at")
                and not case.get("repeat_of")
                and not case.get("conflicts_with")
                and (case.get("disruption") or {}).get("type") == disruption["type"]
                and _subject(case["disruption"], case.get("defect"))[0] == subject
            ),
            None,
        )
        if original is None:
            return None
        held = _subject(original["disruption"], original.get("defect"))[1]
        source = document.get("source") or {}
        reporter = source.get("system") or (
            "a system event" if source.get("kind") in EVENT_SOURCES else "OCC"
        )
        ref = {"trace_id": original["trace_id"], "display_id": original["display_id"]}
        actor = {"actor": "arrow-intake", "autonomy": "system"}
        note: str | None
        if minutes == held and self._reassess(original, document, reporter):
            message = (
                f"Same disruption as {original['display_id']}, which stopped before a plan ·"
                " added to that case; ARROW assesses it again."
            )
            values: dict[str, Any] = {"status": "closed", "stage": message, "repeat_of": ref}
            kind = "repeat_report"
            note = None  # the reopened case logs it
        elif minutes == held:
            message = f"Same disruption as {original['display_id']} · added to that case."
            values = {"status": "closed", "stage": message, "repeat_of": ref}
            kind = "repeat_report"
            note = f"Also reported by {reporter} ({document['display_id']}) · same disruption"
        else:
            message = (
                f"{subject} is already case {original['display_id']} at {held}; this"
                f" report says {minutes} · stopped for OCC to update the case."
            )
            values = {
                "status": "failed",
                "stage": message,
                "error": message,
                "options": [],
                "conflicts_with": ref,
            }
            kind = "conflicting_report"
            note = (
                f"Also reported by {reporter} ({document['display_id']}) with {minutes}"
                f" instead of {held} · stopped for OCC"
            )
        self.store.mutate(
            trace, lambda case: CaseChange(values, ({**actor, "event": kind, "message": message},))
        )
        if note is not None:
            self.store.append_event(
                original["trace_id"],
                {**actor, "event": kind, "message": note, "repeat": document["display_id"]},
            )
        # No agent gets another live case's id: it may act only on its own case (review).
        # OCC sees the link on both cases.
        return {
            "recorded": False,
            "repeat": True,
            "message": "Another open case already holds this disruption: not recorded again;"
            " OCC sees both cases.",
        }

    def _reassess(self, original: dict[str, Any], document: dict[str, Any], reporter: str) -> bool:
        """Queue a fresh assessment of a case that recorded its disruption and then stopped
        before any plan (a Foundry timeout, say). Its recorded disruption and rotation stay; the
        orchestrator gets the recorded brief. False if it is not such a case (anymore): the
        report is then an ordinary repeat."""
        if not reassessable(original):
            return False
        now = self.now()
        message = (
            f"Also reported by {reporter} ({document['display_id']}) · the case had stopped"
            " before a plan, so ARROW assesses it again"
        )
        values: dict[str, Any] = {
            "status": "assessing",
            "stage": "Reported again · ARROW is assessing the disruption again.",
            "error": None,
            "options": [],
            "plans_submitted": 0,
            "options_reconsidered": None,
            "options_refused": [],
            "unassessed_options": None,
            "summary": None,
            "findings": [],
            "assessment": {"attempt": 1, "state": "queued", "queued_at": now.isoformat()},
            "reassessed_at": now.isoformat(),
            "_deadline_at": (now + ASSESSMENT_WINDOW).isoformat(),
            "_reading_claim": None,
            "_trail": None,
        }
        event = {
            "actor": "arrow-intake",
            "autonomy": "system",
            "event": "assessment_reopened",
            "message": message,
            "repeat": document["display_id"],
        }
        if not self.store.reopen(original["trace_id"], values, event):
            return False
        self.wake()
        if self.waker is not None:
            try:
                self.waker(original["trace_id"], None)
            except Exception:
                logger.warning(
                    "Could not queue case %s; the sweeper wakes it", original["trace_id"]
                )
        return True

    def _reading_result(self, document: dict[str, Any]) -> dict[str, Any]:
        """What the Detection agent learns once its reading is recorded on the case: the
        disruption in its type's own fields and its knock-on. No airline system has it yet (only
        a plan carried out records it), so there is no event id."""
        disruption = document["disruption"]
        return {
            "case_id": document["display_id"],
            "recorded": reading_label(disruption),
            "disruption": typed_disruption(document),
            "knock_on": disruption.get("knock_on"),
        }

    def _message_brief(self, document: dict[str, Any]) -> dict[str, Any]:
        """A report in someone's own words: the Detection agent reads and records it."""
        return {
            "case_id": document["display_id"],
            "message": document["disruption"]["text"],
            "reported_by": reporter(document.get("source") or {}),
            "reported_at": document["disruption"]["reported_at"],
            "operating_date": document["operating_date"],
            "limits": brief_limits(self.rules.in_force()),
        }

    def _brief(self, document: dict[str, Any]) -> dict[str, Any]:
        """A report already typed when it arrived, recorded on the case by ARROW before the run:
        the brief carries the disruption in its type's own fields, which the domain agents ask
        their systems by, and the event id only when its source had recorded it already."""
        disruption = document["disruption"]
        return {
            "case_id": document["display_id"],
            **({"event_id": disruption["event_id"]} if disruption.get("event_id") else {}),
            "disruption": typed_disruption(document),
            "description": disruption.get("description"),
            "reported_by": reporter(document.get("source") or {}),
            "reported_at": disruption.get("reported_at"),
            "limits": brief_limits(self.rules.in_force()),
        }

    def _settle(self, trace: str, result: dict[str, Any] | None, error: Exception | None) -> None:
        """After the orchestrator: a submission already decided; otherwise OCC or a stop.

        Decided under the case lock, so a plan the agent builds late is never overwritten.
        """
        outcome = (result or {}).get("outcome")
        summary = str((result or {}).get("summary") or "")[:600]
        actor = {"actor": "recovery-orchestrator", "autonomy": "orchestrator"}

        def update(document: dict[str, Any]) -> CaseChange:
            elapsed = round(
                (self.now() - datetime.fromisoformat(document["created_at"])).total_seconds(),
                3,
            )
            metrics = {**(document.get("metrics") or {}), "total_elapsed_seconds": elapsed}
            plans = document.get("options") or []
            decidable = [
                p for p in plans if (p.get("policy") or {}).get("route") in {"occ", "auto"}
            ]
            if decidable:
                # OCC decides every plan that is not blocked, including automatic ones.
                message = "Orchestrator made no submission; OCC decides"
                values: dict[str, Any] = {
                    "status": "assessment_ready",
                    "stage": message,
                    "metrics": metrics,
                    "options": [{**p, "requires_duty_manager": p in decidable} for p in plans],
                }
                event = {**actor, "event": "orchestrator_no_submission", "message": message}
            elif plans and (
                crews := {
                    reason
                    for p in plans
                    for reason in (p.get("policy") or {}).get("reasons") or []
                    if reason in CREW_BREACH_REASONS
                }
            ):
                # No legal crew: the case stays open with OCC for manual intervention. Where
                # standby crews were tried, say so (a plan built by an earlier version says
                # "reserve crew").
                crew_reason = next(
                    (
                        reason
                        for reason in (
                            CREW_AFTER_STANDBY_CREWS_REASON,
                            CREW_AFTER_STANDBY_REASON,
                            CREW_AFTER_RESERVE_REASON,
                        )
                        if reason in crews
                    ),
                    CREW_LIMIT_REASON,
                )
                disruption = document.get("disruption") or {}
                subject = disruption.get("flight_no") or (
                    "The flights in the weather window"
                    if disruption.get("type") == "weather"
                    else "The affected rotation"
                )
                limit = (
                    "flight-time limit exceeded and no standby crew"
                    if crew_reason == CREW_LIMIT_REASON
                    else crew_reason
                )
                message = (
                    f"Manual intervention: {limit}."
                    f" {subject} cannot operate as planned; crew control finds crew,"
                    " or OCC cancels outside ARROW."
                )
                values = {
                    "status": "assessment_ready",
                    "stage": message,
                    "metrics": metrics,
                    "manual_intervention": {"reason": crew_reason, "message": message},
                }
                event = {**actor, "event": "manual_intervention_required", "message": message}
            elif plans:
                message = (
                    "Assessment stopped. Every plan was blocked; no recovery actions were executed."
                )
                values = {
                    "status": "failed",
                    "stage": message,
                    "error": "Every plan the orchestrator built was blocked by policy",
                    "metrics": metrics,
                }
                event = {**actor, "event": "assessment_failed", "message": message}
            elif error is None and outcome == "no_disruption":
                disruption = document.get("disruption") or {}
                unread = disruption.get("type") == "message"
                affected = list((disruption.get("scope") or {}).get("flights") or [])
                if unread or affected:
                    # "No disruption" is accepted only when Schedule agrees: a recorded disruption
                    # with no affected departures. Otherwise OCC sees the case as stopped.
                    reason = (
                        "the message was not recorded"
                        if unread
                        else f"the operations control system shows {', '.join(affected)} affected"
                    )
                    message = (
                        f"ARROW could not confirm the disruption ({reason}):"
                        f" {summary or 'no reason given'}"
                    )
                    values = {
                        "status": "failed",
                        "stage": message,
                        "error": message,
                        "options": [],
                        "metrics": metrics,
                    }
                    event = {**actor, "event": "assessment_failed", "message": message}
                else:
                    message = summary or "No affected departures"
                    values = {"status": "no_disruption", "stage": message, "metrics": metrics}
                    event = {**actor, "event": "assessment_completed", "message": message}
            elif error is None and outcome == "unreadable":
                message = f"ARROW could not read this message: {summary or 'no reason given'}"
                values = {
                    "status": "failed",
                    "stage": message,
                    "error": message,
                    "options": [],
                    "metrics": metrics,
                }
                event = {**actor, "event": "message_unreadable", "message": message}
            else:
                detail = getattr(error, "detail", None) or (str(error) if error else summary)
                message = "Assessment stopped. No recovery actions were executed."
                values = {
                    "status": "failed",
                    "stage": message,
                    "error": str(detail or "The orchestrator built no plan")[:600],
                    "options": [],
                    "metrics": metrics,
                }
                event = {**actor, "event": "assessment_failed", "message": values["error"]}
            return CaseChange(values, (event,))

        try:
            self.store.mutate(trace, update)
        except (AssessmentInactive, KeyError):
            logger.info("Assessment %s was already decided; nothing to settle", trace)
