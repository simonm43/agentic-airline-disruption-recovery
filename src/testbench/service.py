"""The test bench's behaviour: send events, list them with ARROW's outcome, reset the day
(event trigger and test bench spec §5.1, §7; Service Bus since the Service Bus intake spec).
Adapters are passed in, so the flows run offline with fakes."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from testbench.events import cloud_event
from testbench.presets import PRESETS, Kind, validate

LISBON = ZoneInfo("Europe/Lisbon")
# A sent event ARROW has not read yet: the queue holds it while ARROW works through a burst.
WAITING_SECONDS = 15 * 60
SENT_KEPT = 200
RUNNING_RESET = {"closing", "loading_day"}
EXECUTING = {"approved", "executing", "pending_external"}


class ResetRunning(RuntimeError):
    """A reset is already in progress."""


class Sender(Protocol):
    def send(self, event: dict[str, Any]) -> None: ...


class Job(Protocol):
    def start(self) -> str: ...
    def execution(self, name: str) -> str: ...
    def last_success(self) -> datetime | None: ...


class Arrow(Protocol):
    def event(self, event_id: str) -> dict[str, Any] | None: ...
    def events(self, day: date) -> list[dict[str, Any]]: ...
    def close_cases(self) -> dict[str, Any]: ...


def _thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="testbench-reset", daemon=True).start()


def _moment(value: Any) -> datetime | None:
    if not value:
        return None
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


def outcome(view: dict[str, Any] | None, sent_at: datetime, now: datetime) -> str:
    """The plain outcome a person reads in the events list."""
    if view is None:
        waited = (now - sent_at).total_seconds()
        return "Waiting for ARROW" if waited <= WAITING_SECONDS else "Not received by ARROW"
    if view.get("status") == "rejected":
        return "Rejected"
    status = view.get("status")
    execution = view.get("execution") or {}
    if status == "closed" and view.get("repeat_of"):
        return "Added to open case"
    if status == "closed":
        return "Closed by reset"
    if status == "failed":
        return "Stopped"
    if status == "no_disruption":
        return "No disruption"
    if view.get("manual_intervention"):
        return "Manual intervention"
    # A storm case's plan (case summary and held storm spec): its "now" part done and the rest
    # held until the storm is confirmed, or stood down when the forecast is dropped.
    if execution.get("status") == "held":
        return "Held until confirmed"
    if execution.get("status") == "stood_down":
        return "Stood down"
    if execution.get("status") == "completed":
        by_policy = execution.get("approved_by") == "policy"
        return "Executed automatically" if by_policy else "Executed after approval"
    if execution.get("status") == "needs_attention":
        return "Needs attention"
    if execution.get("status") in EXECUTING:
        return "Executing"
    return "Assessing" if status == "assessing" else "Waiting for OCC"


def _data(envelope: Any) -> dict[str, Any]:
    data = envelope.get("data") if isinstance(envelope, dict) else None
    return data if isinstance(data, dict) else {}


def expected(envelope: Any) -> dict[str, Any] | None:
    """The demo case an event was sent from (same message text, or the same weather update, as
    written for the day it was sent) and its expected outcome."""
    data = _data(envelope)
    text = data.get("text")
    sent = _moment(envelope.get("time")) if isinstance(envelope, dict) else None
    day = (sent or datetime.now(UTC)).astimezone(LISBON).date()
    case = next(
        (
            p
            for p in PRESETS
            if (text and p.data_on(day).get("text") == text)
            or (p.kind == "weather_update" and p.data_on(day) == data)
        ),
        None,
    )
    if case is None:
        return None
    return {"case": case.case, "title": case.title_on(day), "outcome": case.expected}


def label(envelope: Any) -> str:
    data = _data(envelope)
    # A weather report reads as the report it quotes.
    quoted = data.get("text") or (data.get("description") if data.get("hazard") else "")
    text = " ".join(str(quoted or "").split())
    if text:
        return text if len(text) <= 70 else text[:69] + "…"
    if data.get("aircraft_reg"):
        return f"{data['aircraft_reg']} AOG {data.get('unavailable_minutes')} min"
    cause = str(data.get("cause") or "").replace("_", " ")
    return f"{data.get('flight_no')} +{data.get('delay_minutes')} · {cause}"


class TestBench:
    __test__ = False  # not a pytest test class

    def __init__(
        self,
        *,
        sender: Sender,
        job: Job,
        arrow: Arrow,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        spawn: Callable[[Callable[[], None]], None] = _thread,
        sleep: Callable[[float], None] = time.sleep,
        poll_seconds: float = 5.0,
        reset_timeout_seconds: float = 600.0,
    ) -> None:
        self._sender, self._job, self._arrow, self._now = sender, job, arrow, now
        self._spawn, self._sleep = spawn, sleep
        self._poll, self._timeout = poll_seconds, reset_timeout_seconds
        self._lock = threading.Lock()
        self._reset: dict[str, Any] = {"state": "idle", "closed": [], "skipped": [], "message": ""}
        # What this process sent, so an event ARROW has not read yet is still listed.
        self._sent: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def status(self) -> dict[str, Any]:
        try:
            loaded = self._job.last_success()
        except Exception:
            loaded = None
        return {
            "can_send": True,
            "day_loaded_at": loaded.isoformat() if loaded else None,
            "reset": self.reset_state(),
        }

    def presets(self) -> list[dict[str, Any]]:
        today = self.today()
        return [
            {
                "key": p.key,
                "label": p.label,
                "kind": p.kind,
                "data": p.data_on(today),
                "case": p.case,
                "title": p.title_on(today),
                "expected": p.expected,
            }
            for p in PRESETS
        ]

    def send(
        self, preset: str | None, kind: str | None, data: dict[str, Any] | None
    ) -> dict[str, Any]:
        if preset is not None:
            chosen = next((p for p in PRESETS if p.key == preset), None)
            if chosen is None:
                raise ValueError(f"preset: unknown {preset}")
            kind, clean = chosen.kind, chosen.data_on(self.today())
        else:
            clean = validate(str(kind), data or {})
        typed: Kind = (
            "message"
            if kind == "message"
            else "flight_delay"
            if kind == "flight_delay"
            else "weather_update"
            if kind == "weather_update"
            else "aircraft_defect"
        )
        event = cloud_event(typed, clean, now=self._now())
        self._sender.send(event)
        with self._lock:
            self._sent[event["id"]] = event
            while len(self._sent) > SENT_KEPT:
                self._sent.popitem(last=False)
        return {"event_id": event["id"], "sent_at": event["time"], "event": event}

    def _row(self, item: dict[str, Any]) -> dict[str, Any]:
        """One event as the list shows it: sent, queued, its ARROW case and the outcome."""
        envelope = item.get("envelope") or {}
        sent = _moment(envelope.get("time")) or self._now()
        view = item.get("case") or item.get("rejected")
        queued = _moment(item.get("queued_at"))
        case = None
        case_after = None
        if view is not None and view.get("trace_id"):
            case = {"display_id": view["display_id"], "trace_id": view["trace_id"]}
            # A repeat was added to the case that already holds the disruption: open that one.
            repeat_of = view.get("repeat_of") if view.get("status") == "closed" else None
            if isinstance(repeat_of, dict) and repeat_of.get("trace_id"):
                case = {"display_id": repeat_of["display_id"], "trace_id": repeat_of["trace_id"]}
            created = _moment(view.get("created_at"))
            case_after = round((created - sent).total_seconds()) if created else None
        return {
            "event_id": item["event_id"],
            "sent_at": sent.isoformat(),
            "label": label(envelope),
            "queued_after_s": round((queued - sent).total_seconds()) if queued else None,
            "case": case,
            "case_after_s": case_after,
            "outcome": outcome(view, sent, self._now()),
            "reason": view.get("reason") if view else None,
            "expected": expected(envelope),
        }

    def events(self, day: date) -> list[dict[str, Any]]:
        """Test bench events of one Lisbon operating day, newest first: what ARROW received
        (with its case or why it has none) and what was sent that ARROW has not read yet. An event
        is "earlier" when a Reset closed its case, or it was sent before the day was last
        reloaded and never became a case; the page folds those away."""
        received = [
            item for item in self._arrow.events(day) if isinstance(item.get("envelope"), dict)
        ]
        known = {item["event_id"] for item in received}
        with self._lock:
            pending = [
                {"event_id": event_id, "envelope": event}
                for event_id, event in self._sent.items()
                if event_id not in known
                and (_moment(event["time"]) or self._now()).astimezone(LISBON).date() == day
            ]
        try:
            loaded = self._job.last_success()
        except Exception:
            loaded = None
        listed = []
        for item in received + pending:
            row = self._row(item)
            sent = datetime.fromisoformat(row["sent_at"])
            before_reload = loaded is not None and sent < loaded
            # A repeat sent before the last Reset joined a case that Reset closed: fold it too.
            row["earlier"] = row["outcome"] == "Closed by reset" or (
                before_reload and (row["case"] is None or row["outcome"] == "Added to open case")
            )
            listed.append(row)
        return sorted(listed, key=lambda row: row["sent_at"], reverse=True)

    def today(self) -> date:
        return self._now().astimezone(LISBON).date()

    def event(self, event_id: str) -> dict[str, Any] | None:
        """The message at each step: sent, queued, received by ARROW, outcome."""
        view = self._arrow.event(event_id)
        deliveries = (view or {}).get("deliveries") or []
        with self._lock:
            envelope = self._sent.get(event_id)
        if envelope is None:
            envelope = (deliveries[0].get("body") if deliveries else None) or (view or {}).get(
                "body"
            )
        if envelope is None:
            return None
        rejected = view if view and view.get("status") == "rejected" else None
        first = deliveries[0] if deliveries else {}
        item = {
            "event_id": event_id,
            "envelope": envelope,
            "queued_at": first.get("enqueued_at"),
            **({"rejected": rejected} if rejected else {"case": view}),
        }
        return {
            "row": self._row(item),
            "sent": envelope,
            "queued": {
                key: first.get(key)
                for key in ("message_id", "sequence_number", "enqueued_at", "delivery_count")
            }
            if first
            else None,
            "delivered": deliveries,
            "rejection": {k: v for k, v in rejected.items() if k != "body"} if rejected else None,
            "outcome": view if view and not rejected else None,
        }

    def reset_state(self) -> dict[str, Any]:
        with self._lock:
            state = {**self._reset, "closed": list(self._reset["closed"])}
        # Seconds since Reset was pressed while it runs, so the page can show progress.
        started = state.pop("started_at", None)
        if started is not None and state["state"] in RUNNING_RESET:
            state["elapsed_s"] = int((self._now() - started).total_seconds())
        return state

    def _finish(self, state: str, message: str) -> None:
        with self._lock:
            started = self._reset.get("started_at")
            took = int((self._now() - started).total_seconds()) if started else None
            self._reset.update(state=state, message=message, took_s=took)

    def _set(self, **values: Any) -> None:
        with self._lock:
            self._reset.update(values)

    def reset(self) -> dict[str, Any]:
        with self._lock:
            if self._reset["state"] in RUNNING_RESET:
                raise ResetRunning("A reset is already running")
            self._reset = {
                "state": "closing",
                "closed": [],
                "skipped": [],
                "message": "Closing cases",
                "started_at": self._now(),
            }
        self._spawn(self._run_reset)
        return self.reset_state()

    def _run_reset(self) -> None:
        try:
            result = self._arrow.close_cases()
            skipped = list(result.get("skipped", []))
            self._set(closed=list(result.get("closed", [])), skipped=skipped)
            if skipped:
                names = ", ".join(item["display_id"] for item in skipped)
                self._finish("stopped", f"{names} still executing; retry in a minute")
                return
            self._set(state="loading_day", message="Reloading the day")
            name = self._job.start()
            waited = 0.0
            status = self._job.execution(name)
            while status == "Running" and waited < self._timeout:
                self._sleep(self._poll)
                waited += self._poll
                status = self._job.execution(name)
            if status != "Succeeded" or self._job.last_success() is None:
                self._finish("failed", f"Reset failed: day not reloaded (job {status})")
                return
            self._finish("done", "Done · day reloaded")
        except Exception as error:
            self._finish("failed", f"Reset failed: {type(error).__name__}")
