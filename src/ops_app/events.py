"""Disruption CloudEvents from the Service Bus assessment queue (Service Bus intake spec; formerly
the Fabric Eventstream, event trigger and test bench spec §3–4), and the weather updates that
settle a held storm case (case summary and held storm spec §2)."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from airline_systems.schedule import lisbon_today
from ops_app.reports import DefectReport, DelayReport, Hazard, WeatherReport, storm_window

if TYPE_CHECKING:
    from ops_app.engine import Operations
    from ops_app.store import CaseStore

EVENT_TYPE_PREFIX = "com.arrow.disruption."
MESSAGE_TYPE = "com.arrow.disruption.reported"
# A METAR or amended TAF that confirms or drops a weather forecast: never a new case.
WEATHER_UPDATE_TYPE = "com.arrow.weather.update"
Outcome = Literal["new_case", "repeat", "skipped", "rejected", "ignored", "update"]
LISBON = ZoneInfo("Europe/Lisbon")
NO_STORM_CASE = "No weather case waits for this update"


@dataclass(frozen=True)
class Delivery:
    """One message as a queue receiver hands it over: the body and where it came from."""

    body: bytes
    kind: str | None  # "wake" or "execute" for a wake-up; None for a disruption CloudEvent
    message_id: str | None
    sequence_number: int | None
    enqueued_at: datetime | None
    delivery_count: int = 1
    dead_letter_reason: str | None = None


def decode(raw: bytes | str) -> Any:
    """The message's JSON, or None for anything that is not JSON (including JSON nested too
    deeply to decode)."""
    try:
        return json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return None


class DisruptionEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    specversion: Literal["1.0"]
    id: uuid.UUID
    source: str = Field(min_length=1, max_length=200)
    type: Literal[
        "com.arrow.disruption.reported",
        "com.arrow.disruption.flight_delay",
        "com.arrow.disruption.aircraft_defect",
        "com.arrow.disruption.weather",
    ]
    time: AwareDatetime
    datacontenttype: Literal["application/json"] = "application/json"
    data: dict[str, Any]


class MessageData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=3, max_length=2000)
    system: str | None = Field(default=None, min_length=1, max_length=100)


class WeatherUpdateEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    specversion: Literal["1.0"]
    id: uuid.UUID
    source: str = Field(min_length=1, max_length=200)
    type: Literal["com.arrow.weather.update"]
    time: AwareDatetime
    datacontenttype: Literal["application/json"] = "application/json"
    data: dict[str, Any]


class WeatherUpdate(BaseModel):
    """A weather forecast confirmed (a METAR, an amended TAF or a EUROCONTROL regulation, with the
    movements an hour the airport takes) or dropped (an amended TAF without it), for the airport
    and TAF period the forecast named."""

    model_config = ConfigDict(extra="forbid")
    airport: str = Field(pattern=r"^[A-Z]{4}$")
    taf_window: str = Field(pattern=r"^[0-9]{4}/[0-9]{4}$")
    hazard: Hazard
    status: Literal["confirmed", "dropped"]
    rate_per_hour: int | None = Field(default=None, strict=True, ge=1, le=120)
    description: str = Field(min_length=3, max_length=1000)


def storm_update(
    data: Any, *, day: date, at: datetime, event_id: str | None = None
) -> dict[str, Any]:
    """A weather update as its storm case records it, the window in Lisbon wall-clock time on
    the operating day ``day``; ValueError says in plain words what is wrong."""
    try:
        update = WeatherUpdate.model_validate(data)
    except ValidationError as error:
        raise ValueError(f"data: {_reason(error)}") from error
    if update.status == "confirmed" and update.rate_per_hour is None:
        raise ValueError(
            "data.rate_per_hour: a confirmed forecast gives its rate (movements an hour)"
        )
    window = storm_window(update.airport, update.taf_window, day)
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "at": at.isoformat(),
        "status": update.status,
        "airport": window["airport"],
        "taf_window": update.taf_window,
        "window": f"{window['window_start']}–{window['window_end']}",
        "window_start": window["window_start"],
        "window_end": window["window_end"],
        "hazard": update.hazard,
        "rate_per_hour": update.rate_per_hour,
        "description": update.description.strip(),
    }


def _reason(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in issue['loc']) or 'event'}: {issue['msg']}"
        for issue in error.errors()
    )


def to_report(event: DisruptionEvent) -> dict[str, Any]:
    """The OCC report an event stands for; ValueError carries the validation reason."""
    if event.type == MESSAGE_TYPE:
        try:
            data = MessageData.model_validate(
                {**event.data, "text": str(event.data.get("text") or "").strip()}
            )
        except ValidationError as error:
            raise ValueError(f"data: {_reason(error)}") from error
        return {"type": "message", "text": data.text, "event_id": str(event.id)}
    kind = event.type.removeprefix(EVENT_TYPE_PREFIX)
    model: type[DelayReport | DefectReport | WeatherReport] = (
        DelayReport
        if kind == "flight_delay"
        else WeatherReport
        if kind == "weather"
        else DefectReport
    )
    try:
        report = model.model_validate({**event.data, "type": kind})
    except ValidationError as error:
        raise ValueError(f"data: {_reason(error)}") from error
    if not report.description.strip():
        raise ValueError("data.description: a description is required")
    if isinstance(report, WeatherReport):
        # The storm opens a case only if code can plan it today (Lisbon, today's window).
        storm_window(report.airport, report.taf_window, lisbon_today())
    return {**report.model_dump(mode="json"), "event_id": str(event.id)}


def read_event(body: dict[str, Any]) -> tuple[DisruptionEvent, dict[str, Any]]:
    """Validate a received CloudEvent and its data; ValueError says what is wrong."""
    try:
        event = DisruptionEvent.model_validate(body)
    except ValidationError as error:
        raise ValueError(_reason(error)) from error
    return event, to_report(event)


def event_source(event: DisruptionEvent) -> dict[str, Any]:
    system = event.data.get("system") if event.type == MESSAGE_TYPE else None
    return {
        "kind": "event",
        "event_id": str(event.id),
        "event_time": event.time.isoformat(),
        "origin": event.source,
        **({"system": str(system).strip()[:100]} if system else {}),
    }


def delivery_event(body: dict[str, Any], delivery: Delivery, *, created: bool) -> dict[str, Any]:
    """The decision-log record of one message read, with the event exactly as received."""
    return {
        "actor": "system-event",
        "autonomy": "system",
        "event": "event_received",
        "event_id": str(body.get("id")),
        "message_id": delivery.message_id,
        # The decision log owns "sequence" (its own row number), so the queue's is renamed.
        "sequence_number": delivery.sequence_number,
        "enqueued_at": delivery.enqueued_at.isoformat() if delivery.enqueued_at else None,
        "delivery_count": delivery.delivery_count,
        "delivery": "new_case" if created else "repeat",
        "body": body,
        "message": f"Event received from the queue ({'new case' if created else 'repeat'})",
    }


class IntakeLog(Protocol):
    """What ARROW's intake keeps about events beyond their cases: the newest events that opened
    no case (invalid, sent before the last Reset, or dead-lettered) with the reason and the event
    as received, and when the Test Bench last reset the day. ``store.SqlIntakeLog`` keeps them in
    Azure SQL for the three apps; ``Rejections`` in memory for offline runs."""

    def add(
        self,
        event_id: str | None,
        reason: str,
        received_at: datetime,
        body: dict[str, Any] | None = None,
    ) -> None: ...
    def get(self, event_id: str) -> dict[str, Any] | None: ...
    def on(self, day: date) -> list[dict[str, Any]]: ...
    def reset_at(self) -> datetime | None: ...
    def mark_reset(self, at: datetime) -> None: ...


class Rejections:
    """The newest disruption events that opened no case (invalid, sent before the last Reset, or
    dead-lettered) and why, with the event as received, for the test bench, and the last Reset
    (memory only: offline runs; the three apps use ``store.SqlIntakeLog``)."""

    def __init__(self, limit: int = 100) -> None:
        self._limit = limit
        self._items: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._reset_at: datetime | None = None

    def reset_at(self) -> datetime | None:
        """When the Test Bench last reset the day: an event sent before it never opens a case."""
        return self._reset_at

    def mark_reset(self, at: datetime) -> None:
        self._reset_at = at

    def add(
        self,
        event_id: str | None,
        reason: str,
        received_at: datetime,
        body: dict[str, Any] | None = None,
    ) -> None:
        key = event_id or f"no-id-{uuid.uuid4()}"
        with self._lock:
            self._items.pop(key, None)
            self._items[key] = {
                "status": "rejected",
                "reason": reason,
                "received_at": received_at.isoformat(),
                "body": body,
            }
            while len(self._items) > self._limit:
                self._items.popitem(last=False)

    def get(self, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._items.get(event_id)
            return dict(item) if item else None

    def on(self, day: date) -> list[dict[str, Any]]:
        """Rejections received on one Lisbon day, newest first, with the event as received."""
        with self._lock:
            items = list(self._items.items())
        return [
            {"event_id": key, **item}
            for key, item in reversed(items)
            if datetime.fromisoformat(item["received_at"]).astimezone(LISBON).date() == day
        ]


def _already_read(document: dict[str, Any], sequence: int | None) -> bool:
    """A redelivery of a message already logged (same queue sequence number)."""
    return sequence is not None and any(
        event.get("event") == "event_received" and event.get("sequence_number") == sequence
        for event in document.get("events", [])
    )


def _weather_update(
    body: dict[str, Any],
    *,
    operations: Operations,
    store: CaseStore,
    rejections: IntakeLog,
    delivery: Delivery,
) -> tuple[Outcome, str | None]:
    """A weather update settles the storm case code matches it to (today's open storm case at
    that airport whose window overlaps); it never opens a case. Each update is recorded once."""
    event_id = body.get("id")

    def rejected(reason: str) -> tuple[Outcome, str | None]:
        rejections.add(str(event_id) if event_id else None, reason, operations.now(), body)
        logging.getLogger("ops_app").warning("Rejected weather update %s: %s", event_id, reason)
        return "rejected", None

    try:
        event = WeatherUpdateEvent.model_validate(body)
    except ValidationError as error:
        return rejected(_reason(error))
    try:
        update = storm_update(
            event.data, day=lisbon_today(), at=operations.now(), event_id=str(event.id)
        )
    except ValueError as error:
        return rejected(str(error))
    reset_at = rejections.reset_at()
    if reset_at is not None and event.time < reset_at:
        return rejected("Sent before the last Reset; the day was reloaded, so no case was updated")
    case = operations.storm_case(update)
    if case is None:
        return rejected(NO_STORM_CASE)
    recorded = (case.get("storm") or {}).get("updates") or []
    if any(item.get("event_id") == update["event_id"] for item in recorded):
        return "skipped", None  # this update is already on its case
    store.append_event(
        case["trace_id"],
        {
            **delivery_event(body, delivery, created=False),
            "delivery": "update",
            "message": "Weather update received from the queue",
        },
    )
    if not operations.storm_update(case["trace_id"], update):
        return "skipped", None
    return "update", None


def receive(
    body: Any,
    *,
    operations: Operations,
    store: CaseStore,
    rejections: IntakeLog,
    reporter_oid: str,
    delivery: Delivery,
) -> tuple[Outcome, str | None]:
    """Handle one event from the queue: at most one case per event id, every message logged
    once. Returns the outcome and the case to wake (a redelivery wakes its case again)."""
    if isinstance(body, dict) and body.get("type") == WEATHER_UPDATE_TYPE:
        return _weather_update(
            body, operations=operations, store=store, rejections=rejections, delivery=delivery
        )
    if not isinstance(body, dict) or not str(body.get("type", "")).startswith(EVENT_TYPE_PREFIX):
        # Listed for the Test Bench with the reason, as any other event that opened no case.
        if isinstance(body, dict):
            event_id = body.get("id")
            reason = f"Not a disruption event: type {str(body.get('type'))[:100]!r}"
        else:
            event_id, reason = None, "Not a JSON CloudEvent"
        rejections.add(
            str(event_id) if event_id else None,
            reason,
            operations.now(),
            body if isinstance(body, dict) else None,
        )
        logging.getLogger("ops_app").warning("Rejected message %s: %s", delivery.message_id, reason)
        return "rejected", None
    try:
        event, report = read_event(body)
    except ValueError as error:
        event_id = body.get("id")
        rejections.add(str(event_id) if event_id else None, str(error), operations.now(), body)
        logging.getLogger("ops_app").warning("Rejected disruption event %s: %s", event_id, error)
        return "rejected", None
    existing = store.find_by_event(str(event.id))
    if existing is not None and _already_read(existing, delivery.sequence_number):
        return "skipped", existing["trace_id"]
    reset_at = rejections.reset_at() if existing is None else None
    if reset_at is not None and event.time < reset_at:
        # Queued before the Test Bench reloaded the day: it must not replay into the new day.
        reason = "Sent before the last Reset; the day was reloaded, so no case was opened"
        rejections.add(str(event.id), reason, operations.now(), body)
        return "rejected", None
    opened = operations.open(report, reporter_oid, source=event_source(event))
    store.append_event(
        opened["trace_id"], delivery_event(body, delivery, created=opened["created"])
    )
    return ("new_case" if opened["created"] else "repeat"), opened["trace_id"]


def _delivered(event: dict[str, Any], event_id: str | None) -> bool:
    """A delivery of the event asked for: the case's own event (its updates are listed apart),
    or one weather update by its id."""
    if event.get("event") != "event_received":
        return False
    if event_id is None:
        return event.get("delivery") != "update"
    body = event.get("body")
    return isinstance(body, dict) and str(body.get("id")) == event_id


def event_view(document: dict[str, Any], event_id: str | None = None) -> dict[str, Any]:
    """What happened to an event's case, for the test bench: every delivery, plan and action.
    ``event_id`` names a weather update the case received; its deliveries alone are listed."""
    execution = document.get("execution")
    return {
        **{
            key: document.get(key)
            for key in (
                "trace_id",
                "display_id",
                "status",
                "stage",
                "created_at",
                "source",
                "repeat_of",
            )
        },
        "summary": document.get("summary"),
        "manual_intervention": bool(document.get("manual_intervention")),
        # A storm's weather updates recorded before OCC approved a plan.
        "storm": document.get("storm"),
        "deliveries": [
            {
                key: event.get(key)
                for key in (
                    "at",
                    "message_id",
                    "sequence_number",
                    "enqueued_at",
                    "delivery_count",
                    "delivery",
                    "body",
                )
            }
            for event in document.get("events", [])
            if _delivered(event, event_id)
        ],
        "plans": [
            {
                "plan_id": plan.get("plan_id") or plan.get("option_id"),
                "kind": plan.get("kind"),
                "route": (plan.get("policy") or {}).get("route"),
                "reasons": (plan.get("policy") or {}).get("reasons", []),
                "cost_eur": (plan.get("cost") or {}).get("total_eur"),
                "recommended": bool(plan.get("recommended")),
                "recommendation_reason": plan.get("recommendation_reason"),
            }
            for plan in document.get("options", [])
        ],
        "execution": {
            "status": execution.get("status"),
            "approved_by": execution.get("approved_by"),
            "storm": execution.get("storm"),
            "actions": [
                {key: action.get(key) for key in ("label", "kind", "status", "message")}
                | {"stage": action.get("stage") or "now"}
                for action in execution.get("actions", [])
            ],
        }
        if isinstance(execution, dict)
        else None,
    }
