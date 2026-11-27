"""Durable operation documents and an append-only Azure SQL decision ledger."""

from __future__ import annotations

import builtins
import json
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import pyodbc

from ops_app.actions import authorise_execution
from ops_app.policy import classify, limits_of, originals_of

RUNNABLE = frozenset({"approved", "executing", "pending_external"})
LEASE_SECONDS = 300  # > 4 action attempts at 30 s each, plus back-off
# How long a recording waits for another app recording the same disruption (D109); the
# assessment retries after that.
SUBJECT_LOCK_TIMEOUT_MS = 120_000
LISBON = ZoneInfo("Europe/Lisbon")


class AssessmentInactive(RuntimeError):
    """A terminal assessment cannot accept late progress or results."""


class DecisionConflict(RuntimeError):
    """A Duty Manager decision is stale, unsupported, or not ready."""


class ExecutionLeaseLost(RuntimeError):
    """Another executor holds this case; this one must stop without writing."""


class DuplicateEvent(RuntimeError):
    """A case for this disruption event already exists."""


class SubjectBusy(RuntimeError):
    """Another app has been recording the same disruption for too long; try again shortly."""


@dataclass(frozen=True)
class CaseChange:
    """What one atomic update of a case writes."""

    values: dict[str, Any]
    events: tuple[dict[str, Any], ...]
    execution: dict[str, Any] | None = None  # a new execution, created in the same transaction


class Unchanged(Exception):  # noqa: N818 - control flow, not an error
    """An update decided nothing needs writing; carries what to return instead."""

    def __init__(self, result: Any) -> None:
        super().__init__("unchanged")
        self.result = result


def apply_change(document: dict[str, Any], change: CaseChange, now: datetime) -> None:
    document.update(change.values)
    if change.execution is not None:
        document["execution"] = merge_execution(
            document.get("execution"),
            change.execution,
            owner=None,
            release=False,
            create=True,
            now=now,
        )
    document["version"] += 1


def reassessable(document: dict[str, Any]) -> bool:
    """A case that recorded its disruption, then stopped before any plan or execution: a new
    report of the same disruption assesses it again instead of recording it twice."""
    return (
        document.get("status") == "failed"
        and bool(document.get("recorded_at"))
        and not document.get("options")
        and not document.get("execution")
        and not document.get("repeat_of")
        and not document.get("conflicts_with")
    )


def _lease(owner: str, now: datetime) -> dict[str, str]:
    return {"owner": owner, "until": (now + timedelta(seconds=LEASE_SECONDS)).isoformat()}


def claim(execution: Any, owner: str, now: datetime) -> dict[str, Any] | None:
    """One executor per case: claim a runnable execution unless another live lease holds it.

    A lease still present when claimed means the previous run died without releasing it.
    """
    if not isinstance(execution, dict) or execution.get("status") not in RUNNABLE:
        return None
    lease = execution.get("lease")
    if isinstance(lease, dict) and lease.get("owner") != owner:
        if str(lease.get("until", "")) > now.isoformat():
            return None
        execution = {**execution, "interrupted_runs": execution.get("interrupted_runs", 0) + 1}
    return {**execution, "lease": _lease(owner, now)}


def merge_execution(
    stored: Any,
    execution: dict[str, Any],
    *,
    owner: str | None,
    release: bool,
    create: bool,
    now: datetime,
) -> dict[str, Any]:
    """The execution to save: the lease holder's view, keeping reviews recorded meanwhile."""
    if create and isinstance(stored, dict):
        raise DecisionConflict("A recovery plan for this case is already being carried out.")
    if owner is not None:
        lease = stored.get("lease") if isinstance(stored, dict) else None
        if not isinstance(lease, dict) or lease.get("owner") != owner:
            raise ExecutionLeaseLost("Another executor holds this case")
    merged = {key: value for key, value in execution.items() if key != "lease"}
    if isinstance(stored, dict) and stored.get("reviews"):
        merged["reviews"] = stored["reviews"]
    if owner is not None and not release:
        merged["lease"] = _lease(owner, now)
    return merged


def prepare_decision(
    document: dict[str, Any],
    *,
    option_id: str,
    decision: str,
    expected_version: int,
    idempotency_key: str,
    actor_oid: str,
    previous_event: dict[str, Any] | None = None,
    comment: str | None = None,
) -> tuple[dict[str, Any], bool]:
    request = {
        "option_id": option_id,
        "decision": decision,
        "expected_version": expected_version,
        "idempotency_key": idempotency_key,
        "actor": actor_oid,
    }
    if previous_event is not None:
        # The same decision sent again keeps the comment it was first recorded with.
        if all(previous_event.get(key) == value for key, value in request.items()):
            return previous_event, False
        raise DecisionConflict("The idempotency key was already used for another decision.")

    if document.get("status") != "assessment_ready":
        raise DecisionConflict("Only a ready assessment can receive a Duty Manager decision.")
    if isinstance(document.get("execution"), dict):
        raise DecisionConflict("A recovery plan for this case is already being carried out.")
    if type(document.get("version")) is not int or document["version"] != expected_version:
        raise DecisionConflict("The assessment changed; refresh it before recording a decision.")

    option = next(
        (
            item
            for item in document.get("options", [])
            if isinstance(item, dict) and item.get("option_id") == option_id
        ),
        None,
    )
    if option is None:
        raise DecisionConflict("This option is not a Duty Manager decision candidate.")
    # The plan's own recorded limits: a rule change never reroutes a plan already built.
    verdict = classify(
        option,
        originals=originals_of(document),
        validation=option.get("validation"),
        limits=limits_of(option),
    )
    if verdict.route == "blocked":
        raise DecisionConflict(f"Decision blocked: {'; '.join(verdict.reasons)}.")
    # OCC decides OCC plans, and automatic plans code handed to OCC because the orchestrator
    # submitted nothing (requires_duty_manager is set by code, never by an agent).
    if verdict.route != "occ" and not (
        verdict.route == "auto" and option.get("requires_duty_manager") is True
    ):
        raise DecisionConflict("This option is not a Duty Manager decision candidate.")

    next_version = expected_version + 1
    said = (comment or "").strip()
    event = {
        **request,
        "autonomy": "human",
        "event": "duty_manager_decision",
        "route": verdict.route,
        "case_version": next_version,
        "message": (
            f"Duty Manager approved option {option_id}; ARROW is carrying it out."
            if decision == "approve"
            else f"Duty Manager rejected option {option_id}; nothing changes."
        ),
        **({"comment": said} if said else {}),
    }
    return event, True


def apply_decision(document: dict[str, Any], event: dict[str, Any]) -> None:
    """An approval authorises execution of exactly the reviewed option and version."""
    if event["decision"] == "approve":
        document["execution"] = authorise_execution(
            document,
            event["option_id"],
            route=event.get("route", "occ"),
            approved_by=event["actor"],
        )
    document["version"] = event["case_version"]


def prepare_review(
    document: dict[str, Any], kind: str, outcome: str, actor_oid: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """An expert closes the review ARROW opened for items outside its agents' limits (D12)."""
    execution = document.get("execution")
    if not isinstance(execution, dict):
        raise DecisionConflict("No review of this kind is waiting for an expert.")
    reviews = execution.get("reviews", [])
    review = next(
        (r for r in reviews if r.get("kind") == kind and r.get("status") == "awaiting_expert"),
        None,
    )
    if review is None:
        raise DecisionConflict("No review of this kind is waiting for an expert.")
    review.update(status=outcome, decided_by=actor_oid)
    event: dict[str, Any] = {
        "actor": actor_oid,
        "autonomy": "human",
        "event": "passenger_review_recorded",
        "message": f"Passenger Services {outcome} the {review['passengers']} passengers "
        "outside rebooking policy.",
    }
    return execution, event


def decision_receipt(trace: str, event: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "recorded",
        "trace_id": trace,
        "version": event["case_version"],
        "option_id": event["option_id"],
        "decision": event["decision"],
    }


class CaseStore(Protocol):
    def create(self, document: dict[str, Any]) -> None: ...
    def get(self, trace: str) -> dict[str, Any]: ...
    def find_by_event(self, event_id: str) -> dict[str, Any] | None: ...
    def case_traces(self, day: str, prefix: str) -> builtins.list[str]: ...
    def event_traces(self, day: str, digits: str) -> builtins.list[str]: ...
    def list(self, day: str) -> list[dict[str, Any]]: ...
    def assessing(self) -> builtins.list[dict[str, Any]]: ...
    def received_events(self, day: str) -> builtins.list[dict[str, Any]]: ...
    def runnable_traces(self) -> tuple[str, ...]: ...
    def change(self, trace: str, values: dict[str, Any], event: dict[str, Any]) -> None: ...
    def append_event(self, trace: str, event: dict[str, Any]) -> None: ...
    def save_trail(self, trace: str, trail: dict[str, Any]) -> None: ...
    def system_calls(self, trace: str) -> builtins.list[dict[str, Any]]: ...
    def open_traces(self) -> builtins.list[str]: ...
    def close(self, trace: str, event: dict[str, Any]) -> bool: ...
    def reopen(self, trace: str, values: dict[str, Any], event: dict[str, Any]) -> bool: ...
    def mutate(
        self,
        trace: str,
        update: Callable[[dict[str, Any]], CaseChange],
        *,
        statuses: frozenset[str] = frozenset({"assessing"}),
    ) -> None: ...
    def update_chat(
        self,
        trace: str,
        update: Callable[[builtins.list[dict[str, Any]]], builtins.list[dict[str, Any]]],
    ) -> None: ...
    def claim_execution(self, trace: str, owner: str) -> dict[str, Any] | None: ...
    def update_execution(
        self,
        trace: str,
        execution: dict[str, Any],
        event: dict[str, Any] | None,
        *,
        owner: str | None = None,
        release: bool = False,
        create: bool = False,
    ) -> None: ...
    def record_review(self, trace: str, kind: str, outcome: str, actor_oid: str) -> None: ...
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
    ) -> dict[str, Any]: ...
    def subject_lock(self, key: str) -> AbstractContextManager[None]:
        """One recording at a time per disruption subject across every ARROW app and replica
        (D109): exclusive, re-entrant within a thread."""
        ...


class SqlCaseStore:
    def __init__(self, connect: Callable[[], AbstractContextManager[pyodbc.Connection]]) -> None:
        self.connect = connect
        self._held = threading.local()

    @contextmanager
    def subject_lock(self, key: str) -> Iterator[None]:
        """An exclusive SQL application lock on the subject, owned by a transaction on its own
        connection: every ARROW app and replica shares it, and a crashed holder's connection
        releases it. Re-entrant within a thread, as the in-memory lock it replaces."""
        held: dict[str, int] = self._held.__dict__.setdefault("keys", {})
        if held.get(key):
            held[key] += 1
            try:
                yield
            finally:
                held[key] -= 1
            return
        with self.connect() as conn:
            granted = conn.execute(
                "SET NOCOUNT ON; DECLARE @rc int; EXEC @rc = sp_getapplock @Resource = ?,"
                " @LockMode = 'Exclusive', @LockOwner = 'Transaction', @LockTimeout = ?;"
                " SELECT @rc;",
                f"airrec-subject:{key}"[:255],
                SUBJECT_LOCK_TIMEOUT_MS,
            ).fetchone()
            if granted is None or int(granted[0]) < 0:
                raise SubjectBusy(f"Another report of {key} is being recorded")
            held[key] = 1
            try:
                yield
            finally:
                del held[key]

    def create(self, document: dict[str, Any]) -> None:
        scope = (document.get("disruption") or {}).get("scope") or {}
        try:
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO ops.recovery_case"
                    " (trace_id, operating_date, aircraft_reg, event_id, document)"
                    " VALUES (?, ?, ?, ?, ?)",
                    document["trace_id"],
                    document["operating_date"],
                    scope.get("aircraft_reg"),
                    document.get("event_id"),
                    json.dumps(document),
                )
        except pyodbc.IntegrityError as error:
            if "ux_recovery_case_event" in str(error):
                raise DuplicateEvent(str(document.get("event_id"))) from error
            raise

    def find_by_event(self, event_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT trace_id FROM ops.recovery_case WHERE event_id=?", event_id
            ).fetchone()
        return self.get(str(row[0])) if row else None

    def case_traces(self, day: str, prefix: str) -> builtins.list[str]:
        """The day's cases whose trace starts with ``prefix`` (a case id names six digits)."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT trace_id FROM ops.recovery_case WHERE operating_date=? AND trace_id LIKE ?",
                day,
                f"{prefix}%",
            ).fetchall()
        return [str(row[0]) for row in rows]

    def event_traces(self, day: str, digits: str) -> builtins.list[str]:
        """The day's cases whose trace holds ``digits`` as characters 7-12: the airline's
        systems name a recorded disruption by them (its operations event id)."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT trace_id FROM ops.recovery_case"
                " WHERE operating_date=? AND SUBSTRING(trace_id, 7, 6)=?",
                day,
                digits,
            ).fetchall()
        return [str(row[0]) for row in rows]

    def get(self, trace: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT document FROM ops.recovery_case WHERE trace_id=?", trace
            ).fetchone()
            if row is None:
                raise KeyError(trace)
            result: dict[str, Any] = json.loads(row[0])
            events = conn.execute(
                "SELECT sequence,at,document FROM ops.decision_log"
                " WHERE trace_id=? ORDER BY sequence",
                trace,
            ).fetchall()
        result["events"] = [
            {**json.loads(e[2]), "sequence": e[0], "at": e[1].isoformat() + "Z", "trace_id": trace}
            for e in events
        ]
        return result

    def list(self, day: str) -> list[dict[str, Any]]:
        """Open cases of one operating day, newest first; evaluation (QA-) cases are left out."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT TOP 60 document FROM ops.recovery_case"
                " WHERE operating_date = ?"
                " AND ISNULL(JSON_VALUE(document, '$.display_id'), '') NOT LIKE 'QA-%'"
                " AND JSON_VALUE(document, '$.status') <> 'closed'"
                " ORDER BY created_at DESC",
                day,
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def assessing(self) -> builtins.list[dict[str, Any]]:
        """Every case still being assessed, with its queue state, for the supervisor's sweep."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT trace_id, JSON_QUERY(document, '$.assessment'),"
                " JSON_VALUE(document, '$.created_at'), JSON_VALUE(document, '$._deadline_at')"
                " FROM ops.recovery_case WHERE JSON_VALUE(document, '$.status') = 'assessing'"
            ).fetchall()
        return [
            {
                "trace_id": str(row[0]),
                "assessment": json.loads(row[1]) if row[1] else None,
                "created_at": row[2],
                **({"_deadline_at": row[3]} if row[3] else {}),
            }
            for row in rows
        ]

    def received_events(self, day: str) -> builtins.list[dict[str, Any]]:
        """The newest 100 cases opened by an event on one operating day, whatever their status,
        each with only its delivery records (for the Test Bench's events list)."""
        query = (
            "SELECT TOP 100 trace_id, document FROM ops.recovery_case"
            " WHERE operating_date = ? AND event_id IS NOT NULL ORDER BY created_at DESC"
        )
        with self.connect() as conn:
            rows = conn.execute(query, day).fetchall()
            deliveries = conn.execute(
                "SELECT trace_id, sequence, at, document FROM ops.decision_log"
                f" WHERE trace_id IN (SELECT trace_id FROM ({query}) AS recent)"
                " AND JSON_VALUE(document, '$.event') = 'event_received' ORDER BY sequence",
                day,
            ).fetchall()
        logged: dict[str, builtins.list[dict[str, Any]]] = {}
        for trace, sequence, at, document in deliveries:
            logged.setdefault(str(trace), []).append(
                {**json.loads(document), "sequence": sequence, "at": at.isoformat() + "Z"}
            )
        return [{**json.loads(row[1]), "events": logged.get(str(row[0]), [])} for row in rows]

    def runnable_traces(self) -> tuple[str, ...]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT trace_id FROM ops.recovery_case"
                " WHERE JSON_VALUE(document, '$.execution.status')"
                " IN ('approved', 'executing', 'pending_external')"
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def claim_execution(self, trace: str, owner: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT document FROM ops.recovery_case WITH (UPDLOCK, ROWLOCK) WHERE trace_id=?",
                trace,
            ).fetchone()
            if row is None:
                raise KeyError(trace)
            doc = json.loads(row[0])
            claimed = claim(doc.get("execution"), owner, datetime.now(UTC))
            if claimed is None:
                return None
            doc["execution"] = claimed
            conn.execute(
                "UPDATE ops.recovery_case SET document=? WHERE trace_id=?", json.dumps(doc), trace
            )
        return self.get(trace)

    def change(self, trace: str, values: dict[str, Any], event: dict[str, Any]) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT document FROM ops.recovery_case WITH (UPDLOCK, ROWLOCK) WHERE trace_id=?",
                trace,
            ).fetchone()
            if row is None:
                raise KeyError(trace)
            doc = json.loads(row[0])
            if doc["status"] != "assessing":
                raise AssessmentInactive("This assessment is no longer active")
            doc.update(values)
            doc["version"] += 1
            conn.execute(
                "UPDATE ops.recovery_case SET document=? WHERE trace_id=?", json.dumps(doc), trace
            )
            conn.execute(
                "INSERT INTO ops.decision_log (trace_id,document) VALUES (?,?)",
                trace,
                json.dumps(event),
            )

    def append_event(self, trace: str, event: dict[str, Any]) -> None:
        """Add a log record whatever the case's status; document and version stay unchanged."""
        with self.connect() as conn:
            if (
                conn.execute("SELECT 1 FROM ops.recovery_case WHERE trace_id=?", trace).fetchone()
                is None
            ):
                raise KeyError(trace)
            conn.execute(
                "INSERT INTO ops.decision_log (trace_id,document) VALUES (?,?)",
                trace,
                json.dumps(event),
            )

    def save_trail(self, trace: str, trail: dict[str, Any]) -> None:
        """Keep the orchestrator's recorded steps on the case whatever its status; the trail is
        private (never sent with the case) and changes neither the version nor the decision."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT document FROM ops.recovery_case WITH (UPDLOCK, ROWLOCK) WHERE trace_id=?",
                trace,
            ).fetchone()
            if row is None:
                raise KeyError(trace)
            doc = json.loads(row[0])
            doc["_trail"] = trail
            conn.execute(
                "UPDATE ops.recovery_case SET document=? WHERE trace_id=?", json.dumps(doc), trace
            )

    def system_calls(self, trace: str) -> builtins.list[dict[str, Any]]:
        """Every airline-system call logged for this case, oldest first. Endpoint, request and
        response (sql/014_audit_payloads.sql) are NULL on rows written before them."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT TOP 300 called_at, system_name, operation, caller_oid, outcome, detail,"
                " caller_name, endpoint, request_json, response_json"
                " FROM audit.system_call WHERE trace_id=? ORDER BY call_id",
                trace,
            ).fetchall()
        return [
            {
                "at": row[0].isoformat() + "Z",
                "system": row[1],
                "operation": row[2],
                "caller_oid": str(row[3]).lower() if row[3] else None,
                "outcome": row[4],
                "detail": row[5],
                "caller_name": row[6],
                "endpoint": row[7],
                "request": row[8],
                "response": row[9],
            }
            for row in rows
        ]

    def open_traces(self) -> builtins.list[str]:
        """Every open case of any day and source; evaluation (QA-) cases stay as evidence."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT trace_id FROM ops.recovery_case"
                " WHERE ISNULL(JSON_VALUE(document, '$.display_id'), '') NOT LIKE 'QA-%'"
                " AND JSON_VALUE(document, '$.status') <> 'closed'"
                " ORDER BY created_at"
            ).fetchall()
        return [str(row[0]) for row in rows]

    def close(self, trace: str, event: dict[str, Any]) -> bool:
        """Close a case under its row lock; a case whose execution may still write is left."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT document FROM ops.recovery_case WITH (UPDLOCK, ROWLOCK) WHERE trace_id=?",
                trace,
            ).fetchone()
            if row is None:
                raise KeyError(trace)
            doc = json.loads(row[0])
            if (doc.get("execution") or {}).get("status") in RUNNABLE:
                return False
            doc["status"] = "closed"
            doc["version"] += 1
            conn.execute(
                "UPDATE ops.recovery_case SET document=? WHERE trace_id=?", json.dumps(doc), trace
            )
            conn.execute(
                "INSERT INTO ops.decision_log (trace_id,document) VALUES (?,?)",
                trace,
                json.dumps(event),
            )
        return True

    def reopen(self, trace: str, values: dict[str, Any], event: dict[str, Any]) -> bool:
        """Assess a ``reassessable`` case again, under its row lock; False if it is not one."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT document FROM ops.recovery_case WITH (UPDLOCK, ROWLOCK) WHERE trace_id=?",
                trace,
            ).fetchone()
            if row is None:
                raise KeyError(trace)
            doc = json.loads(row[0])
            if not reassessable(doc):
                return False
            doc.update(values)
            doc["version"] += 1
            conn.execute(
                "UPDATE ops.recovery_case SET document=? WHERE trace_id=?", json.dumps(doc), trace
            )
            conn.execute(
                "INSERT INTO ops.decision_log (trace_id,document) VALUES (?,?)",
                trace,
                json.dumps(event),
            )
        return True

    def mutate(
        self,
        trace: str,
        update: Callable[[dict[str, Any]], CaseChange],
        *,
        statuses: frozenset[str] = frozenset({"assessing"}),
    ) -> None:
        """Read, decide and write a case in one of ``statuses`` under its row lock, in one
        transaction.

        ``update`` must not call other systems: it runs while the row is locked.
        """
        with self.connect() as conn:
            row = conn.execute(
                "SELECT document FROM ops.recovery_case WITH (UPDLOCK, ROWLOCK) WHERE trace_id=?",
                trace,
            ).fetchone()
            if row is None:
                raise KeyError(trace)
            doc = json.loads(row[0])
            if doc["status"] not in statuses:
                raise AssessmentInactive("This assessment is no longer active")
            change = update(doc)
            apply_change(doc, change, datetime.now(UTC))
            conn.execute(
                "UPDATE ops.recovery_case SET document=? WHERE trace_id=?", json.dumps(doc), trace
            )
            for event in change.events:
                conn.execute(
                    "INSERT INTO ops.decision_log (trace_id,document) VALUES (?,?)",
                    trace,
                    json.dumps(event),
                )

    def update_chat(
        self,
        trace: str,
        update: Callable[[builtins.list[dict[str, Any]]], builtins.list[dict[str, Any]]],
    ) -> None:
        """Change the case's conversation under its row lock, whatever its status. The version
        stays: a question never makes an open approval stale. ``update`` must not call other
        systems; if it raises, nothing is written."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT document FROM ops.recovery_case WITH (UPDLOCK, ROWLOCK) WHERE trace_id=?",
                trace,
            ).fetchone()
            if row is None:
                raise KeyError(trace)
            doc = json.loads(row[0])
            doc["chat"] = update(builtins.list(doc.get("chat") or []))
            conn.execute(
                "UPDATE ops.recovery_case SET document=? WHERE trace_id=?", json.dumps(doc), trace
            )

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
        with self.connect() as conn:
            row = conn.execute(
                "SELECT document FROM ops.recovery_case WITH (UPDLOCK, ROWLOCK) WHERE trace_id=?",
                trace,
            ).fetchone()
            if row is None:
                raise KeyError(trace)
            document: dict[str, Any] = json.loads(row[0])
            previous = conn.execute(
                "SELECT TOP (1) document FROM ops.decision_log"
                " WHERE trace_id=? AND JSON_VALUE(document, '$.idempotency_key')=?"
                " ORDER BY sequence",
                trace,
                idempotency_key,
            ).fetchone()
            previous_event = json.loads(previous[0]) if previous is not None else None
            event, is_new = prepare_decision(
                document,
                option_id=option_id,
                decision=decision,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                actor_oid=actor_oid,
                previous_event=previous_event,
                comment=comment,
            )
            if is_new:
                apply_decision(document, event)
                conn.execute(
                    "UPDATE ops.recovery_case SET document=? WHERE trace_id=?",
                    json.dumps(document),
                    trace,
                )
                conn.execute(
                    "INSERT INTO ops.decision_log (trace_id,document) VALUES (?,?)",
                    trace,
                    json.dumps(event),
                )
            return decision_receipt(trace, event)

    def update_execution(
        self,
        trace: str,
        execution: dict[str, Any],
        event: dict[str, Any] | None,
        *,
        owner: str | None = None,
        release: bool = False,
        create: bool = False,
    ) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT document FROM ops.recovery_case WITH (UPDLOCK, ROWLOCK) WHERE trace_id=?",
                trace,
            ).fetchone()
            if row is None:
                raise KeyError(trace)
            doc = json.loads(row[0])
            doc["execution"] = merge_execution(
                doc.get("execution"),
                execution,
                owner=owner,
                release=release,
                create=create,
                now=datetime.now(UTC),
            )
            doc["version"] += 1
            conn.execute(
                "UPDATE ops.recovery_case SET document=? WHERE trace_id=?", json.dumps(doc), trace
            )
            if event is not None:
                conn.execute(
                    "INSERT INTO ops.decision_log (trace_id,document) VALUES (?,?)",
                    trace,
                    json.dumps(event),
                )

    def record_review(self, trace: str, kind: str, outcome: str, actor_oid: str) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT document FROM ops.recovery_case WITH (UPDLOCK, ROWLOCK) WHERE trace_id=?",
                trace,
            ).fetchone()
            if row is None:
                raise KeyError(trace)
            doc = json.loads(row[0])
            execution, event = prepare_review(doc, kind, outcome, actor_oid)
            doc["execution"] = execution
            doc["version"] += 1
            conn.execute(
                "UPDATE ops.recovery_case SET document=? WHERE trace_id=?", json.dumps(doc), trace
            )
            conn.execute(
                "INSERT INTO ops.decision_log (trace_id,document) VALUES (?,?)",
                trace,
                json.dumps(event),
            )


class SqlIntakeLog:
    """The disruption events that opened no case, and the last Test Bench Reset, in Azure SQL
    (three container apps spec §5): the workers' intake writes them and the operator app's
    Test Bench routes read them. The in-memory ``events.Rejections`` serves offline runs."""

    def __init__(self, connect: Callable[[], AbstractContextManager[pyodbc.Connection]]) -> None:
        self.connect = connect

    def add(
        self,
        event_id: str | None,
        reason: str,
        received_at: datetime,
        body: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO ops.rejected_event (event_id, received_at, reason, body)"
                " VALUES (?, ?, ?, ?)",
                event_id[:100] if event_id else None,
                _utc_naive(received_at),
                reason[:1000],
                json.dumps(body) if body is not None else None,
            )

    def get(self, event_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT TOP 1 id, event_id, received_at, reason, body FROM ops.rejected_event"
                " WHERE event_id = ? ORDER BY id DESC",
                event_id,
            ).fetchone()
        if row is None:
            return None
        return {key: value for key, value in _rejected(row).items() if key != "event_id"}

    def on(self, day: date) -> builtins.list[dict[str, Any]]:
        """The rejections received on one Lisbon day, newest first, one per event (its latest)."""
        start = datetime.combine(day, time(0), LISBON).astimezone(UTC)
        end = datetime.combine(day + timedelta(days=1), time(0), LISBON).astimezone(UTC)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT TOP 100 id, event_id, received_at, reason, body FROM ops.rejected_event"
                " WHERE received_at >= ? AND received_at < ? ORDER BY id DESC",
                _utc_naive(start),
                _utc_naive(end),
            ).fetchall()
        listed: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = _rejected(row)
            listed.setdefault(str(item["event_id"]), item)
        return builtins.list(listed.values())

    def reset_at(self) -> datetime | None:
        with self.connect() as conn:
            row = conn.execute("SELECT reset_at FROM ops.day_reset WHERE id = 1").fetchone()
        return row[0].replace(tzinfo=UTC) if row else None

    def mark_reset(self, at: datetime) -> None:
        with self.connect() as conn:
            conn.execute(
                "MERGE ops.day_reset WITH (HOLDLOCK) AS target USING (SELECT 1 AS id) AS source"
                " ON target.id = source.id WHEN MATCHED THEN UPDATE SET reset_at = ?"
                " WHEN NOT MATCHED THEN INSERT (id, reset_at) VALUES (1, ?);",
                _utc_naive(at),
                _utc_naive(at),
            )


def _utc_naive(at: datetime) -> datetime:
    """A moment as SQL's datetime2 holds it: UTC without an offset."""
    return at.astimezone(UTC).replace(tzinfo=None) if at.tzinfo else at


def _rejected(row: Any) -> dict[str, Any]:
    return {
        "event_id": row[1] or f"no-id-{row[0]}",
        "status": "rejected",
        "reason": row[3],
        "received_at": row[2].replace(tzinfo=UTC).isoformat(),
        "body": json.loads(row[4]) if row[4] else None,
    }
