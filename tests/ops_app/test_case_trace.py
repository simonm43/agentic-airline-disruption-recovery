"""A case is one trace in Application Insights (OpenTelemetry, spec: the coordinator's telemetry
brief). ARROW opens, assesses, decides and executes a case under the case's own trace
id, and each call to Foundry and the airline systems carries that trace on (W3C traceparent), so
their handling joins it. Without a connection string telemetry stays off and ARROW works as before.

Real intake, engine, CloudGateway request code, tools, policy and executor; HTTP is answered by
the in-process systems and a scripted orchestrator. Spans go to memory instead of Azure Monitor.
"""

from __future__ import annotations

import base64
import io
import json
import os
import time
import unittest
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import Request

from arrow_scenario import CaseMemory, ScenarioStore, SystemView
from azure.core.credentials import AccessToken
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from test_event_intake import ARROW, cloud_event
from test_orchestrated_flow import DEFECT, HOST, OPERATOR, TRACE, ScriptedGateway, insist, submitted

from airline_systems import telemetry
from airline_systems.app import create_app as system_app
from ops_app.app import create_app
from ops_app.engine import CloudGateway, Operations
from ops_app.events import Delivery, Rejections
from ops_app.intake import Intake, wake_body
from ops_app.tools import ArrowTools

EXPORTER = InMemorySpanExporter()
PASSENGER_TEXT = "Hydraulic leak"  # the reported free text: never a span attribute


def recorded() -> InMemorySpanExporter:
    """Spans to memory, with ARROW's case trace ids (the provider is set once per process)."""
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        provider = TracerProvider(id_generator=telemetry.CaseIds())
        provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
        trace.set_tracer_provider(provider)
    EXPORTER.clear()
    return EXPORTER


class Credential:
    def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        return AccessToken("token", int(time.time()) + 3600)


class Wire:
    """urllib's opener for CloudGateway: keeps each request and answers it in-process."""

    def __init__(self, gateway: WiredGateway) -> None:
        self.gateway = gateway
        self.sent: list[Request] = []

    def open(self, request: Request, timeout: float) -> io.BytesIO:
        self.sent.append(request)
        return io.BytesIO(json.dumps(self.gateway.answer(request)).encode())


class WiredGateway(ScriptedGateway):
    """The scripted systems, but rotation reads, the orchestrator run and every executor action
    go through CloudGateway's HTTP request, as in Dev."""

    def __init__(self, store: ScenarioStore, script: Any) -> None:
        super().__init__(store, script)
        self.cloud = CloudGateway(
            Credential(),
            "systems-app",
            "https://schedule.test",
            "https://maintenance.test",
            "https://foundry.test/api/projects/arrow",
            booking_url="https://booking.test",
            crew_url="https://crew.test",
            slots_url="https://slots.test",
        )
        self.wire = Wire(self)

    def _http(self) -> Any:
        return patch("ops_app.engine.build_opener", lambda *handlers: self.wire)

    def rotation(self, trace: str, aircraft_reg: str) -> dict[str, Any]:
        with self._http():
            return self.cloud.rotation(trace, aircraft_reg)

    def orchestrate(
        self, trace: str, brief: dict[str, Any], *, timeout: float = 600
    ) -> dict[str, Any]:
        with self._http():
            return self.cloud.orchestrate(trace, brief, timeout=timeout)

    def send(
        self,
        system: str,
        method: str,
        path: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
        trace: str,
    ) -> dict[str, Any]:
        with self._http():
            return self.cloud.send(system, method, path, headers, body, trace)

    def answer(self, request: Request) -> dict[str, Any]:
        url = urlsplit(request.full_url)
        body = json.loads(request.data) if isinstance(request.data, bytes) else None
        case = str(request.get_header("Traceparent")).split("-")[1]
        if url.path.startswith("/api/aircraft/"):
            return ScriptedGateway.rotation(self, case, url.path.split("/")[3])
        if url.path.endswith("/responses"):
            assert body is not None
            result = ScriptedGateway.orchestrate(
                self, case, json.loads(body["input"][0]["content"])
            )
            text = {"type": "output_text", "text": json.dumps(result)}
            message = {"type": "message", "content": [text]}
            return {"id": "resp_1", "status": "completed", "output": [message]}
        # Each action's call, at its recipient's own route on the system that plays it.
        system = str(url.hostname).split(".")[0]
        headers = dict(request.header_items())
        return ScriptedGateway.send(
            self, system, request.get_method(), url.path, headers, body, case
        )


def delivery(body: bytes, kind: str | None) -> Delivery:
    return Delivery(body=body, kind=kind, message_id="m1", sequence_number=1, enqueued_at=None)


# ARROW's identity at the airline systems, with the roles Easy Auth hands them (app_roles.py).
ARROW_OID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ARROW_ROLES = ("Schedule.Read", "Crew.Read", "Booking.Read", "Slots.Read", "Actions.Write")


class Granted(SystemView):
    """One system's handle on the shared store as its SQL grants allow: only operations control
    and Maintenance may read the operating day itself (infra/systems/sql 005 and 006); the
    crew, passenger and capacity systems are refused, as SQL Server refuses them (error 229)."""

    def has_day(self, day: Any) -> bool:
        if self._system not in {"schedule", "maintenance"}:
            raise PermissionError(
                f"The SELECT permission was denied on schedule.operating_day ({self._system})"
            )
        return bool(self._scenario.has_day(day))


class SystemsOverHttp:
    """urllib's opener for CloudGateway: each request reaches the airline system its host names,
    that system's own app in-process, with ARROW's identity as Easy Auth passes it on, and each
    system reads the store only as its grants allow."""

    def __init__(self, store: ScenarioStore) -> None:
        self.apps = {
            name: TestClient(
                system_app(
                    Granted(store, name),  # type: ignore[arg-type]
                    frozenset({ARROW_OID}),
                    system=name,
                    writer_oids=frozenset({ARROW_OID}),
                    mock_confirm_seconds=0,
                )
            )
            for name in ("schedule", "maintenance", "crew", "booking", "slots")
        }
        self.sent: list[Request] = []

    def open(self, request: Request, timeout: float) -> io.BytesIO:
        self.sent.append(request)
        url = urlsplit(request.full_url)
        claims = [
            {"typ": "oid", "val": ARROW_OID},
            *({"typ": "roles", "val": r} for r in ARROW_ROLES),
        ]
        principal = base64.b64encode(json.dumps({"claims": claims}).encode()).decode()
        answer = self.apps[str(url.hostname).split(".")[0]].request(
            request.get_method(),
            f"{url.path}?{url.query}" if url.query else url.path,
            headers={**dict(request.header_items()), "X-MS-CLIENT-PRINCIPAL": principal},
            content=request.data if isinstance(request.data, bytes) else None,
        )
        if answer.status_code >= 400:
            raise HTTPError(
                request.full_url,
                answer.status_code,
                answer.reason_phrase,
                answer.headers,  # type: ignore[arg-type]
                io.BytesIO(answer.content),
            )
        return io.BytesIO(answer.content)


class ArrowOverHttp(CloudGateway):
    """ARROW's own gateway: every airline-system call goes over (in-process) HTTP to the
    systems' apps; the orchestrator follows a script."""

    def __init__(self, store: ScenarioStore, script: Any) -> None:
        super().__init__(
            Credential(),
            "systems-app",
            "https://schedule.test",
            "https://maintenance.test",
            "https://foundry.test/api/projects/arrow",
            booking_url="https://booking.test",
            crew_url="https://crew.test",
            slots_url="https://slots.test",
        )
        self.wire = SystemsOverHttp(store)
        self.script = script
        self.tools: ArrowTools | None = None

    def request(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        with patch("ops_app.engine.build_opener", lambda *handlers: self.wire):
            return super().request(*args, **kwargs)

    def orchestrate(
        self, trace: str, brief: dict[str, Any], *, timeout: float = 600
    ) -> dict[str, Any]:
        assert self.tools is not None
        result: dict[str, Any] = self.script(self.tools, trace, brief)
        return result


def descends(span: ReadableSpan, ancestor: ReadableSpan, spans: list[ReadableSpan]) -> bool:
    by_id = {item.context.span_id: item for item in spans}
    parent = span.parent
    while parent is not None:
        if parent.span_id == ancestor.context.span_id:
            return True
        found = by_id.get(parent.span_id)
        parent = found.parent if found is not None else None
    return False


def check_a_case_is_one_trace_from_event_to_execution() -> None:
    exporter = recorded()

    def script(tools: ArrowTools, trace_id: str, brief: dict[str, Any]) -> dict[str, Any]:
        tools.build_plan(trace_id, {"aircraft_option_id": "cancel"})
        delay = tools.build_plan(trace_id, {"aircraft_option_id": "delay"})
        insist(tools, trace_id, delay["plan_id"], "Delay keeps every passenger travelling")
        return submitted(delay["plan_id"])

    cases = CaseMemory()
    gateway = WiredGateway(ScenarioStore(TRACE), script)
    operations = Operations(cases, gateway)
    gateway.tools = ArrowTools(
        cases,
        gateway,
        execute=operations.execute,
        rules=operations.rules,
        record=operations.record_reading,
    )
    woken: list[str] = []
    intake = Intake(operations, cases, Rejections(), ARROW, lambda case, at: woken.append(case))

    # The Test Bench's event opens the case; the queue's wake assesses it.
    event = cloud_event("aircraft_defect", {k: v for k, v in DEFECT.items() if k != "type"})
    assert intake.handle(delivery(json.dumps(event).encode(), None)) == "new_case"
    [case_trace] = woken
    intake.handle(delivery(wake_body(case_trace), "wake"))
    case = cases.get(case_trace)
    assert case["status"] == "assessment_ready", case.get("error")

    # OCC approves from the browser: its request has a trace of its own.
    [plan] = [plan for plan in case["options"] if plan.get("recommended")]
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("POST /api/cases/{trace}/approval") as operator_request:
        operations.record_decision(
            case_trace,
            option_id=plan["option_id"],
            decision="approve",
            expected_version=case["version"],
            idempotency_key="decision-approve-trace-01",
            actor_oid=OPERATOR,
        )
    operations.execute(case_trace)
    assert cases.get(case_trace)["execution"]["status"] == "completed"

    spans = list(exporter.get_finished_spans())
    mine = [span for span in spans if f"{span.context.trace_id:032x}" == case_trace]
    named = {span.name: span for span in mine}
    # The case's own roots: opening, queueing, assessing, OCC's decision and the execution.
    roots = {span.name: span.kind for span in mine if span.parent is None}
    assert roots == {
        "Open case": SpanKind.CONSUMER,
        "Queue assessment": SpanKind.PRODUCER,
        "Assess case": SpanKind.CONSUMER,
        "Decide plan": SpanKind.SERVER,
        "Execute plan": SpanKind.CONSUMER,
    }, roots
    # OCC's decision joins the case's trace and links back to the operator's request.
    [link] = named["Decide plan"].links
    assert link.context.span_id == operator_request.get_span_context().span_id

    # Every call left in the case's trace, from the assessment or the execution, and carried
    # its own span as the parent the service continues from.
    calls = [span for span in mine if span.kind is SpanKind.CLIENT]
    paths = sorted(urlsplit(str((span.attributes or {})["url.full"])).path for span in calls)
    assert {"/api/flights/AR101", "/nm/messages"} <= set(paths), paths
    assert any(path.endswith("/openai/responses") for path in paths), paths
    assert "/api/aircraft/CS-TQA/rotation" in paths, paths
    assess, execute = named["Assess case"], named["Execute plan"]
    for span in calls:
        assert descends(span, assess, mine) or descends(span, execute, mine), span.name
        if span.name.endswith("/openai/responses"):
            assert descends(span, assess, mine), span.name
        if span.name.startswith("PATCH ") or span.name.endswith("/nm/messages"):
            assert descends(span, execute, mine), span.name
        assert (span.attributes or {})["http.response.status_code"] == 200, span.name
    sent = {str(request.get_header("Traceparent")) for request in gateway.wire.sent}
    assert sent == {f"00-{case_trace}-{span.context.span_id:016x}-01" for span in calls}, sent

    # Ids, routes and outcomes only: the reported text never reaches a span.
    for span in mine:
        values = [str(value) for value in (span.attributes or {}).values()]
        assert not any(PASSENGER_TEXT in value for value in values), (span.name, values)


def check_without_a_connection_string_arrow_works_and_offers_no_trace_link() -> None:
    with patch.dict(os.environ, {telemetry.CONNECTION_STRING: ""}):
        assert telemetry.configure("arrow", Credential(), logger_name="ops_app") is False
    assert not telemetry.enabled()

    cases = CaseMemory()
    gateway = ScriptedGateway(ScenarioStore(TRACE), lambda tools, trace_id, brief: submitted(""))
    operations = Operations(cases, gateway)
    app = create_app(cases, operations, frozenset({OPERATOR}), frozenset({HOST}), "https://a.test")
    claims = {"claims": [{"typ": "oid", "val": OPERATOR}]}
    header = {"X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(claims).encode()).decode()}
    with TestClient(app) as client:
        session = client.get("/api/session", headers=header)
    assert session.status_code == 200, session.text
    assert session.json()["trace_url_template"] is None

    template = telemetry.trace_url_template(
        "/subscriptions/s/resourceGroups/g/providers/microsoft.insights/components/appi"
    )
    assert "{trace}" in template and "PLACEHOLDER" not in template
    assert template.startswith("https://portal.azure.com/")
    with TestClient(
        create_app(
            cases,
            operations,
            frozenset({OPERATOR}),
            frozenset({HOST}),
            "https://a.test",
            trace_url_template=template,
        )
    ) as client:
        assert client.get("/api/session", headers=header).json()["trace_url_template"] == template


RESOURCE = "/subscriptions/s/resourceGroups/g/providers/microsoft.insights/components/appi"
FIRST_ITEM = "b81104f6-e4f2-11f1-84d9-7ced8d25a036"


class TelemetryApi:
    """Application Insights' query API (api.loganalytics.io, the component's own data): a case's
    first item once its telemetry has arrived, or a lookup that cannot be answered."""

    def __init__(self) -> None:
        self.arrived: dict[str, tuple[str, str]] = {}
        self.down = False
        self.sent: list[Request] = []

    def open(self, request: Request, timeout: float) -> io.BytesIO:
        self.sent.append(request)
        if self.down:
            raise URLError("query API unreachable")
        query = json.loads(request.data)["query"] if isinstance(request.data, bytes) else ""
        rows = [
            [item, at, "dependency", "invoke_agent recovery-orchestrator-agent:5"]
            for trace, (item, at) in self.arrived.items()
            if f'"{trace}"' in query
        ]
        columns = [{"name": n} for n in ("itemId", "timestamp", "itemType", "name")]
        return io.BytesIO(json.dumps({"tables": [{"columns": columns, "rows": rows}]}).encode())


def check_the_trace_button_opens_the_case_end_to_end_transaction() -> None:
    """Owner: the trace button should open the case's end-to-end transaction view, not a
    search list. That view's link needs the item id App Insights assigns at ingestion, so ARROW
    looks up the case's first item when OCC clicks; until it has arrived, the search opens."""
    cases = CaseMemory()
    gateway = ScriptedGateway(ScenarioStore(TRACE), lambda tools, trace_id, brief: submitted(""))
    cloud = CloudGateway(
        Credential(), "systems-app", "https://s.test", "https://m.test", "", booking_url=""
    )
    api = TelemetryApi()

    def first_trace_item(resource_id: str, trace_id: str, opened: Any) -> Any:
        with patch("ops_app.engine.build_opener", lambda *handlers: api):
            return cloud.first_trace_item(resource_id, trace_id, opened)

    gateway.first_trace_item = first_trace_item  # type: ignore[attr-defined]
    operations = Operations(cases, gateway)
    case = operations.open(dict(DEFECT), OPERATOR)["trace_id"]
    template = telemetry.trace_url_template(RESOURCE)
    claims = {"claims": [{"typ": "oid", "val": OPERATOR}]}
    header = {"X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(claims).encode()).decode()}
    app = create_app(
        cases,
        operations,
        frozenset({OPERATOR}),
        frozenset({HOST}),
        "https://a.test",
        trace_url_template=template,
        trace_resource_id=RESOURCE,
    )
    search = case.join(template.split("{trace}"))
    with TestClient(app, follow_redirects=False) as client:

        def opened() -> str:
            answer = client.get(f"/api/cases/{case}/trace", headers=header)
            assert answer.status_code == 302, answer.text
            return str(answer.headers["location"])

        # Seconds after the report nothing has arrived yet, or the query API is down: the
        # search for the case opens, as before.
        assert opened() == search
        api.down = True
        assert opened() == search

        # Once the telemetry is in, the button opens the transaction view at the case's first item.
        api.down = False
        api.arrived[case] = (FIRST_ITEM, "2026-11-20T14:56:54.9694198Z")
        link = opened()
        prefix = "https://portal.azure.com/#view/AppInsightsExtension/DetailsV2Blade/ComponentId~/"
        assert link.startswith(prefix), link
        component, model = (
            json.loads(unquote(part)) for part in link[len(prefix) :].split("/DataModel~/")
        )
        assert component["Name"] == "appi" and component["ResourceGroup"] == "g", component
        assert unquote(component["ResourceId"]) == RESOURCE, component
        assert model["eventId"] == FIRST_ITEM and model["operationId"] == case, model
        assert model["eventTable"] == "dependencies", model
        assert model["timestamp"].startswith("2026-11-20T14:56:54.969"), model
        end = datetime.fromisoformat(model["timeContext"]["endTime"])
        start = end - timedelta(milliseconds=model["timeContext"]["durationMs"])
        assert start <= datetime.fromisoformat(model["timestamp"]) <= end, model

        # The lookup read only the component's own data, for this case's trace, and stayed out
        # of the case's trace (an operator's click is not part of the recovery).
        last = api.sent[-1]
        assert last.full_url == f"https://api.loganalytics.io/v1{RESOURCE}/query", last.full_url
        assert last.get_header("Traceparent") is None
        assert f'operation_Id == "{case}"' in json.loads(last.data)["query"]  # type: ignore[arg-type]

        # The item never changes: a later click opens it again without asking.
        asked = len(api.sent)
        api.down = True
        assert opened() == link and len(api.sent) == asked

        unknown = client.get(f"/api/cases/{'0' * 32}/trace", headers=header)
        assert unknown.status_code == 404, unknown.text

    # Without tracing there is no trace to open.
    quiet = create_app(
        cases, operations, frozenset({OPERATOR}), frozenset({HOST}), "https://a.test"
    )
    with TestClient(quiet, follow_redirects=False) as client:
        assert client.get(f"/api/cases/{case}/trace", headers=header).status_code == 404


def check_the_systems_are_read_by_facts_and_written_only_by_the_plan() -> None:
    """Owner: while ARROW assesses a case it only reads each airline system, by the disruption's
    facts, and writes nothing; the defect reaches Maintenance as the first action of the plan
    OCC approved, and Maintenance issues its event id then. ARROW's case id and trace never reach
    a system as a path or an argument. The trace travels only as telemetry, and each system still
    files its calls under it. The crew, passenger and capacity systems read the defect without
    the operating day they hold no grant on. Real gateway, systems' apps and executor; only the
    network is in-process."""

    def script(tools: ArrowTools, trace_id: str, brief: dict[str, Any]) -> dict[str, Any]:
        tools.build_plan(trace_id, {"aircraft_option_id": "cancel"})
        delay = tools.build_plan(trace_id, {"aircraft_option_id": "delay"})
        insist(tools, trace_id, delay["plan_id"], "Delay keeps every passenger travelling")
        return submitted(delay["plan_id"])

    store, cases = ScenarioStore(TRACE), CaseMemory()
    gateway = ArrowOverHttp(store, script)
    operations = Operations(cases, gateway)
    gateway.tools = ArrowTools(
        cases,
        gateway,
        execute=operations.execute,
        rules=operations.rules,
        record=operations.record_reading,
    )
    trace_id = operations.open(dict(DEFECT), OPERATOR)["trace_id"]
    operations.run(trace_id)
    case = cases.get(trace_id)
    assert case["status"] == "assessment_ready", case.get("error")
    # Assessed and waiting for OCC: every call so far read a system, and none wrote.
    assessed = list(gateway.wire.sent)
    assert assessed and {request.get_method() for request in assessed} == {"GET"}, [
        (request.get_method(), request.full_url) for request in assessed
    ]
    assert "event_id" not in case["disruption"], case["disruption"]
    [plan] = [plan for plan in case["options"] if plan.get("recommended")]
    operations.record_decision(
        trace_id,
        option_id=plan["option_id"],
        decision="approve",
        expected_version=case["version"],
        idempotency_key="decision-approve-event-01",
        actor_oid=OPERATOR,
    )
    operations.execute(trace_id)
    execution = cases.get(trace_id)["execution"]
    assert execution["status"] == "completed", execution

    # The plan's first write records the defect in Maintenance, which issues its event id now.
    writes = [request for request in gateway.wire.sent if request.get_method() != "GET"]
    first = urlsplit(writes[0].full_url)
    assert (writes[0].get_method(), first.hostname, first.path) == (
        "POST",
        "maintenance.test",
        "/api/defects",
    ), writes[0].full_url
    [recorded] = execution["actions"][0]["calls"]
    event = recorded["event_id"]
    assert event == f"EV-{trace_id[6:12].upper()}" and event[3:] not in case["display_id"], event
    sent = [urlsplit(request.full_url) for request in gateway.wire.sent]
    for url, request in zip(sent, gateway.wire.sent, strict=True):
        address = f"{url.path}?{url.query}"
        assert trace_id not in address and "DSR-" not in address, address
        body = request.data.decode() if isinstance(request.data, bytes) else ""
        assert case["display_id"] not in body, (address, body)
    # Every read about the disruption names its facts, never a case, trace or record.
    facts = {
        "type": ["aircraft_defect"],
        "aircraft_reg": ["CS-TQA"],
        "unavailable_minutes": ["240"],
    }
    reads = [url for url in sent if url.path.startswith("/api/recovery-options")]
    assert all({key: parse_qs(url.query).get(key) for key in facts} == facts for url in reads), [
        url.query for url in reads
    ]
    assert {url.path.rsplit("/", 1)[1] for url in reads} == {
        "recovery-options",
        "impact",
        "rebooking-plan",
        "duty-plan",
    }, reads
    # Each system still files the calls under the case's trace, naming the facts it was asked.
    audited = [
        json.loads(row["request"])
        for row in store.system_calls(trace_id)
        if row["endpoint"].startswith("GET /api/recovery-options")
    ]
    assert audited and all(
        request["type"] == "aircraft_defect" and "event_id" not in request for request in audited
    ), audited


class CaseTraceTests(unittest.TestCase):
    def test_a_case_is_one_trace_from_event_to_execution(self) -> None:
        check_a_case_is_one_trace_from_event_to_execution()

    def test_the_systems_are_read_by_facts_and_written_only_by_the_plan(self) -> None:
        check_the_systems_are_read_by_facts_and_written_only_by_the_plan()

    def test_without_a_connection_string_arrow_works_and_offers_no_trace_link(self) -> None:
        check_without_a_connection_string_arrow_works_and_offers_no_trace_link()

    def test_the_trace_button_opens_the_case_end_to_end_transaction(self) -> None:
        check_the_trace_button_opens_the_case_end_to_end_transaction()


if __name__ == "__main__":
    unittest.main()
