"""Tracing into Azure Application Insights (Azure Monitor OpenTelemetry Distro), shared by ARROW
and the airline systems.

Off unless APPLICATIONINSIGHTS_CONNECTION_STRING is set. Then the app's managed identity signs
ingestion (the component accepts Entra only), and telemetry fails open: a setup error leaves it
off, and the exporter sends from its own thread, so an unreachable Application Insights never
stops a request. A case's work runs in the case's own trace: its 32-hex trace id is the W3C trace
id, so Application Insights shows one operation (operation_Id) per case. Spans carry ids, names,
routes, SQL text with ``?`` placeholders and outcomes; never passenger data, bodies or tokens.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import secrets
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from azure.core.credentials import TokenCredential
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.id_generator import IdGenerator, RandomIdGenerator
from opentelemetry.trace import Link, Span, SpanKind
from opentelemetry.util.types import AttributeValue

CONNECTION_STRING = "APPLICATIONINSIGHTS_CONNECTION_STRING"
RESOURCE_ID = "APPLICATIONINSIGHTS_RESOURCE_ID"
# Only the instrumentations the apps need: the Azure SDK (Service Bus) here, and FastAPI per app
# (instrument). Outgoing HTTP is traced by the callers themselves (case_span), so a call always
# carries its case's trace even when made outside it.
_UNUSED = (
    "django",
    "fastapi",
    "flask",
    "psycopg2",
    "httpx",
    "httpx2",
    "requests",
    "urllib",
    "urllib3",
)
_TRACE = re.compile(r"[0-9a-f]{32}")
_TABLE = re.compile(r"\b(?:FROM|INTO|UPDATE|JOIN)\s+([A-Za-z_][\w.]*)", re.IGNORECASE)
_case: contextvars.ContextVar[int | None] = contextvars.ContextVar("case_trace_id", default=None)
_enabled = False
logger = logging.getLogger(__name__)


class CaseIds(IdGenerator):
    """Random ids, except that a root span started by case_span takes its case's trace id."""

    def __init__(self, inner: IdGenerator | None = None) -> None:
        self._inner = inner or RandomIdGenerator()

    def generate_span_id(self) -> int:
        return self._inner.generate_span_id()

    def generate_trace_id(self) -> int:
        case = _case.get()
        return case if case is not None else self._inner.generate_trace_id()

    def is_trace_id_random(self) -> bool:
        return self._inner.is_trace_id_random()  # case ids are secrets.token_hex(16)


def configure(role: str, credential: TokenCredential, *, logger_name: str) -> bool:
    """Send this app's traces, its logs under ``logger_name`` and its metrics to Application
    Insights as cloud role ``role``. False (telemetry off) without a connection string or when
    the setup fails."""
    global _enabled
    # The Azure SDK logs every token and every telemetry upload at INFO, burying the apps' own
    # lines (ops 55); its warnings and errors still show.
    logging.getLogger("azure").setLevel(logging.WARNING)
    connection = os.getenv(CONNECTION_STRING, "").strip()
    if not connection:
        return False
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource

        configure_azure_monitor(
            connection_string=connection,
            credential=credential,
            resource=Resource.create({SERVICE_NAME: role}),
            logger_name=logger_name,
            sampling_ratio=1.0,  # every span of every case (the default rate limit drops some)
            enable_live_metrics=False,
            instrumentation_options={name: {"enabled": False} for name in _UNUSED},
        )
        provider = trace.get_tracer_provider()
        if not isinstance(provider, TracerProvider):
            raise RuntimeError("Azure Monitor set no SDK tracer provider")
        # The SDK types the attribute after its default generator.
        provider.id_generator = CaseIds(provider.id_generator)  # type: ignore[assignment]
    except Exception:
        logger.warning("Telemetry is off: Azure Monitor could not be set up", exc_info=True)
        return False
    _enabled = True
    logger.info("Telemetry on as cloud role %s", role)
    return True


def instrument(app: Any) -> None:
    """Trace the app's incoming requests, joining the caller's trace (its traceparent)."""
    if not _enabled:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(
        app, excluded_urls="healthz", exclude_spans=["receive", "send"]
    )


def enabled() -> bool:
    return _enabled


def current_trace() -> str | None:
    """The current span's trace id (32 hex), if any: a call made for no case stays in it."""
    current = trace.get_current_span().get_span_context()
    return f"{current.trace_id:032x}" if current.is_valid else None


@contextmanager
def case_span(
    name: str,
    case: str | None,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    root_kind: SpanKind | None = None,
    attributes: Mapping[str, AttributeValue] | None = None,
) -> Iterator[Span]:
    """A span in the case's trace (``case``: its 32-hex trace id). Inside that trace it is a child
    of the current span. Called from another trace (a Foundry run's tool call, an operator's
    request) or from none, it starts a new root in the case's trace (kind ``root_kind``), linked
    to the span it was called from. Without a valid case id it is an ordinary span."""
    tracer = trace.get_tracer(__name__)
    current = trace.get_current_span().get_span_context()
    case_id = int(case, 16) if case and _TRACE.fullmatch(case) else None
    if case_id is None or (current.is_valid and current.trace_id == case_id):
        with tracer.start_as_current_span(name, kind=kind, attributes=attributes) as span:
            yield span
        return
    token = _case.set(case_id)
    try:
        span = tracer.start_span(
            name,
            context=otel_context.Context(),
            kind=root_kind or kind,
            attributes=attributes,
            links=[Link(current)] if current.is_valid else None,
        )
    finally:
        _case.reset(token)
    with trace.use_span(span, end_on_exit=True):
        yield span


def traceparent(case: str) -> str:
    """The W3C traceparent for a call made for this case: the current span when it is in the
    case's trace, else a fresh parent id (telemetry off)."""
    current = trace.get_current_span().get_span_context()
    if current.is_valid and f"{current.trace_id:032x}" == case:
        flags = "01" if current.trace_flags.sampled else "00"
        return f"00-{case}-{current.span_id:016x}-{flags}"
    return f"00-{case}-{secrets.token_hex(8)}-01"


def sql_connection(connection: Any, server: str, database: str) -> Any:
    """The connection, its statements traced as SQL dependencies while telemetry is on."""
    if not _enabled:
        return connection
    attributes = {"db.system": "mssql", "db.name": database, "server.address": server}
    return _TracedSql(connection, attributes)


@contextmanager
def sql_span(statement: str, attributes: Mapping[str, AttributeValue]) -> Iterator[Span]:
    words = statement.split(None, 1)
    table = _TABLE.search(statement)
    name = " ".join(
        part for part in (words[0].upper() if words else "SQL", table and table.group(1)) if part
    )
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span(
        name, kind=SpanKind.CLIENT, attributes={**attributes, "db.statement": statement[:4000]}
    ) as span:
        yield span


class _TracedSql:
    """A pyodbc connection or cursor whose statements become SQL dependency spans; everything
    else passes through to it."""

    __slots__ = ("_inner", "_attributes")

    def __init__(self, inner: Any, attributes: Mapping[str, AttributeValue]) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_attributes", attributes)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._inner, name, value)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._inner)

    def __enter__(self) -> _TracedSql:
        self._inner.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        return self._inner.__exit__(*exc)

    def cursor(self) -> _TracedSql:
        return _TracedSql(self._inner.cursor(), self._attributes)

    def execute(self, statement: str, *params: Any) -> Any:
        with sql_span(statement, self._attributes):
            return self._inner.execute(statement, *params)

    def executemany(self, statement: str, params: Any) -> Any:
        with sql_span(statement, self._attributes):
            return self._inner.executemany(statement, params)


def trace_url_template(resource_id: str, *, days: int = 2) -> str:
    """Application Insights' transaction search over the last ``days`` for one operation, with
    the literal placeholder ``{trace}`` for the case's trace id. Selecting a result opens its
    end-to-end transaction view (that view's own link needs an item id, not an operation id)."""
    marker = "TRACEPLACEHOLDER"
    found = f' * has "{marker}"'
    tables = {
        "request": ("requests", "Request"),
        "dependency": ("dependencies", "Dependency"),
        "trace": ("traces", "Trace"),
        "exception": ("exceptions", "Exception"),
    }
    inputs = {
        "tables": [table for table, _ in tables.values()],
        "timeContextWhereClause": f"| where timestamp > ago({days}d)",
        "filterWhereClause": f"| where{found}| order by timestamp asc",
        "originalParams": {
            "eventTypes": [
                {"value": value, "tableName": table, "label": label}
                for value, (table, label) in tables.items()
            ],
            "timeContext": {"durationMs": days * 86_400_000},
            "filter": [],
            "searchPhrase": {
                "originalPhrase": marker,
                "_tokens": [{"conjunction": "and", "value": marker, "isNot": False, "kql": found}],
            },
            "sort": "asc",
        },
    }
    url = (
        "https://portal.azure.com/#blade/AppInsightsExtension/BladeRedirect/BladeName/searchV1"
        f"/ResourceId/{quote(resource_id, safe='')}"
        f"/BladeInputs/{quote(json.dumps(inputs, separators=(',', ':')), safe='')}"
    )
    return url.replace(marker, "{trace}")


def _portal_time(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def transaction_url(
    resource_id: str,
    operation_id: str,
    item: Mapping[str, str],
    *,
    now: datetime | None = None,
) -> str:
    """Application Insights' end-to-end transaction view of one operation (a case's trace),
    opened at one of its items. The view's link names an item by the id App Insights gives it at
    ingestion (``itemId``, ``_ItemId`` in the workspace), so the caller looks it up first."""
    parts = resource_id.strip("/").split("/")
    component = {
        "SubscriptionId": parts[1],
        "ResourceGroup": parts[3],
        "Name": parts[-1],
        "LinkedApplicationType": 0,
        "ResourceId": quote(resource_id, safe=""),
        "ResourceType": "microsoft.insights%2Fcomponents",
        "IsAzureFirst": False,
    }
    at = datetime.fromisoformat(item["timestamp"])
    # Two days around the item, never ending in the future.
    end = min(now or datetime.now(UTC), at + timedelta(days=1))
    model = {
        "eventId": item["item_id"],
        "operationId": operation_id,
        "timestamp": _portal_time(at),
        "cacheId": str(uuid.uuid4()),
        "eventTable": item["table"],
        "timeContext": {"durationMs": 2 * 86_400_000, "endTime": _portal_time(end)},
    }

    def encoded(value: Mapping[str, Any]) -> str:
        return quote(json.dumps(value, separators=(",", ":")), safe="")

    return (
        "https://portal.azure.com/#view/AppInsightsExtension/DetailsV2Blade"
        f"/ComponentId~/{encoded(component)}/DataModel~/{encoded(model)}"
    )
