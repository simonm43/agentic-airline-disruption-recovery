"""ARROW's disruption intake and assessment workers (Service Bus intake spec, ADR 0025).

Disruption CloudEvents arrive on ARROW's topic subscription: the intake opens each case at once
(OCC sees it queued) and sends a wake to the assessment queue. Workers take wakes from the queue,
two at a time. The case row decides what is due; a wake only wakes it, so duplicates and stale
wakes do nothing. Execution wakes from the operator and tools apps arrive on the execution queue
(three container apps spec). ``serve`` is the receive loop each thread runs; the Service Bus
adapter is in ``ops_app.bus`` and the workers app in ``ops_app.workers``.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from opentelemetry.trace import SpanKind

from airline_systems import telemetry
from ops_app.events import Delivery, IntakeLog, decode, receive

if TYPE_CHECKING:
    from ops_app.engine import Operations
    from ops_app.store import CaseStore

__all__ = ["Delivery", "Intake", "Receiver", "serve"]
logger = logging.getLogger("ops_app.intake")
FIRST_WAIT_SECONDS = 5.0
MAX_WAIT_SECONDS = 60.0
TRACE = re.compile(r"[0-9a-f]{32}")
Wake = Callable[[str, datetime | None], None]


class Receiver(Protocol):
    def receive(self) -> list[Delivery]: ...
    def complete(self, delivery: Delivery) -> None: ...
    def abandon(self, delivery: Delivery) -> None: ...


class Intake:
    def __init__(
        self,
        operations: Operations,
        store: CaseStore,
        rejections: IntakeLog,
        reporter_oid: str,
        wake: Wake,
    ) -> None:
        self.operations, self.store, self.rejections = operations, store, rejections
        self.reporter_oid, self.wake = reporter_oid, wake

    def handle(self, delivery: Delivery) -> str:
        """One message. An event (ARROW's topic subscription) opens its case at once, so OCC
        sees it queued, and wakes it on the assessment queue; a wake (the queue) runs whatever
        the case has due; an execution wake (the execution queue, three container apps spec §4)
        carries out the case's authorised plan. Returns what the message was: "wake",
        "execute", or the event's intake outcome."""
        if delivery.kind in ("wake", "execute"):
            body = decode(delivery.body)
            trace = body.get("trace_id") if isinstance(body, dict) else None
            if not isinstance(trace, str) or not TRACE.fullmatch(trace):
                logger.warning(
                    "Ignored a %s without a case: %s", delivery.kind, delivery.message_id
                )
                return "ignored"
            if delivery.kind == "execute":
                # The case decides what runs, under its lease: a duplicate does nothing.
                self.operations.execute(trace)
                return "execute"
            # The supervisor's sweeper watches the case while it is assessed, as it does for a
            # case this app opened.
            self.operations.wake()
            self.assess(trace)
            return "wake"
        outcome, found = receive(
            decode(delivery.body),
            operations=self.operations,
            store=self.store,
            rejections=self.rejections,
            reporter_oid=self.reporter_oid,
            delivery=delivery,
        )
        if found is not None:
            # Also for a repeat or a redelivery: a case that is not due ignores the wake.
            with telemetry.case_span("Queue assessment", found, root_kind=SpanKind.PRODUCER):
                self.wake(found, None)
        return outcome

    def assess(self, trace: str) -> None:
        """One wake: the case's assessment attempt (if due), as a root of the case's trace."""
        with telemetry.case_span("Assess case", trace, kind=SpanKind.CONSUMER):
            retry_at = self.operations.run(trace)
            if retry_at is not None:
                self.wake(trace, retry_at)

    def dead(self, delivery: Delivery) -> None:
        """A dead-lettered message: an event that never became a case is listed with the reason;
        a dead wake is only logged (the sweeper and the case deadline cover its case)."""
        reason = delivery.dead_letter_reason or "dead-lettered"
        if delivery.kind == "wake":
            logger.warning("Dead-lettered wake %s: %s", delivery.message_id, reason)
            return
        body = decode(delivery.body)
        event_id = body.get("id") if isinstance(body, dict) else None
        if event_id and self.store.find_by_event(str(event_id)) is not None:
            return
        self.rejections.add(
            str(event_id) if event_id else None,
            f"Not processed: {reason}",
            self.operations.now(),
            body if isinstance(body, dict) else None,
        )
        logger.warning("Dead-lettered event %s: %s", event_id, reason)


def _settle(action: Callable[[Delivery], None], delivery: Delivery) -> None:
    try:
        action(delivery)
    except Exception as error:
        # Usually a lost lock: the message comes back, and the case decides it is not due.
        logger.warning("Message %s could not be settled: %s", delivery.message_id, error)


def serve(
    open_receiver: Callable[[], AbstractContextManager[Any]],
    handle: Callable[[Delivery], object],
    *,
    stop: threading.Event | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Receive and handle one message at a time until stopped. A handled message is completed;
    one that raised is abandoned, so it returns (and is dead-lettered after five deliveries).
    Connection failures back off 5 s → 60 s, with one log line per change of state."""
    failing, wait = False, FIRST_WAIT_SECONDS
    # A handler that fails (the database waking up, a token not yet issued) waits before its
    # message returns, so five deliveries are not spent in a moment and the event dead-lettered.
    retry_wait = FIRST_WAIT_SECONDS

    def stopped() -> bool:
        return stop is not None and stop.is_set()

    while not stopped():
        try:
            with open_receiver() as receiver:
                if failing:
                    logger.info("Assessment queue reachable again")
                failing, wait = False, FIRST_WAIT_SECONDS
                while not stopped():
                    for delivery in receiver.receive():
                        try:
                            handle(delivery)
                        except Exception:
                            logger.exception(
                                "Message %s could not be handled; it returns in %.0f s",
                                delivery.message_id,
                                retry_wait,
                            )
                            sleep(retry_wait)  # the lock renewer keeps the message meanwhile
                            retry_wait = min(retry_wait * 2, MAX_WAIT_SECONDS)
                            _settle(receiver.abandon, delivery)
                            continue
                        retry_wait = FIRST_WAIT_SECONDS
                        _settle(receiver.complete, delivery)
        except Exception as error:
            if not failing:
                logger.warning("Assessment queue unavailable; retrying: %s", error)
            failing = True
            if stop is not None:
                stop.wait(wait)
            else:
                sleep(wait)
            wait = min(wait * 2, MAX_WAIT_SECONDS)


def wake_body(trace: str) -> bytes:
    return json.dumps({"trace_id": trace}).encode()
