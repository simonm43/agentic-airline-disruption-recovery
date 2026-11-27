"""Azure Service Bus adapter for ARROW's assessment queue (Service Bus intake spec, ADR 0025).

Entra only (the namespace has local keys disabled). Each worker opens its own client and
receiver: the SDK's clients are not safe to share across threads. Received messages keep their
lock while a case is assessed (AutoLockRenewer). Exercised in Dev; the flows around it run
offline with an in-memory bus.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from azure.core.credentials import TokenCredential
from azure.servicebus import (
    AutoLockRenewer,
    ServiceBusClient,
    ServiceBusMessage,
    ServiceBusReceivedMessage,
    ServiceBusSubQueue,
)

from ops_app.events import Delivery
from ops_app.intake import wake_body

logger = logging.getLogger("ops_app.bus")
LOCK_RENEWAL_SECONDS = 20 * 60  # longer than the 15-minute assessment window
RECEIVE_WAIT_SECONDS = 20


def _body(message: ServiceBusReceivedMessage) -> bytes:
    body = message.body
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode()
    return b"".join(part if isinstance(part, bytes) else bytes(part) for part in body)


def _kind(message: ServiceBusReceivedMessage) -> str | None:
    properties = message.application_properties or {}
    value = properties.get(b"kind", properties.get("kind"))
    if isinstance(value, bytes):
        value = value.decode()
    return str(value) if value else None


class _Receiver:
    """One receiver on the queue (no ``subscription``) or on the topic subscription."""

    def __init__(
        self,
        namespace: str,
        credential: TokenCredential,
        entity: str,
        subscription: str | None = None,
        *,
        dead_letter: bool,
    ) -> None:
        self._client = ServiceBusClient(namespace, credential)
        self._renewer = AutoLockRenewer(
            max_lock_renewal_duration=LOCK_RENEWAL_SECONDS,
            on_lock_renew_failure=lambda renewable, error: logger.warning(
                "Lock renewal failed: %s", error
            ),
        )
        sub_queue = ServiceBusSubQueue.DEAD_LETTER if dead_letter else None
        self._receiver = (
            self._client.get_subscription_receiver(
                entity,
                subscription,
                sub_queue=sub_queue,
                prefetch_count=0,
                auto_lock_renewer=self._renewer,
            )
            if subscription
            else self._client.get_queue_receiver(
                entity, sub_queue=sub_queue, prefetch_count=0, auto_lock_renewer=self._renewer
            )
        )
        self._held: dict[int, ServiceBusReceivedMessage] = {}

    def __enter__(self) -> _Receiver:
        self._receiver.__enter__()
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            self._renewer.close()
            self._receiver.__exit__(*exc)
        finally:
            self._client.close()

    def receive(self) -> list[Delivery]:
        deliveries = []
        for message in self._receiver.receive_messages(
            max_message_count=1, max_wait_time=RECEIVE_WAIT_SECONDS
        ):
            delivery = Delivery(
                body=_body(message),
                kind=_kind(message),
                message_id=message.message_id,
                sequence_number=message.sequence_number,
                enqueued_at=message.enqueued_time_utc,
                delivery_count=message.delivery_count or 1,
                dead_letter_reason=message.dead_letter_reason,
            )
            self._held[id(delivery)] = message
            deliveries.append(delivery)
        return deliveries

    def complete(self, delivery: Delivery) -> None:
        self._receiver.complete_message(self._held.pop(id(delivery)))

    def abandon(self, delivery: Delivery) -> None:
        self._receiver.abandon_message(self._held.pop(id(delivery)))


class ServiceBusQueue:
    """ARROW's side of the namespace: the events subscription it reads, the assessment queue it
    wakes cases on (send and receive) and the execution queue that hands authorised plans to the
    workers (three container apps spec §4)."""

    def __init__(
        self,
        namespace: str,
        queue: str,
        credential: TokenCredential,
        *,
        topic: str = "disruption-events",
        subscription: str = "assessment",
        executions: str = "executions",
    ) -> None:
        self.namespace, self.queue, self.credential = namespace, queue, credential
        self.topic, self.subscription = topic, subscription
        self.execution_queue = executions
        self._client = ServiceBusClient(namespace, credential)
        self._lock = threading.Lock()

    def events(self) -> _Receiver:
        return _Receiver(
            self.namespace, self.credential, self.topic, self.subscription, dead_letter=False
        )

    def receiver(self) -> _Receiver:
        return _Receiver(self.namespace, self.credential, self.queue, dead_letter=False)

    def execution_receiver(self) -> _Receiver:
        return _Receiver(self.namespace, self.credential, self.execution_queue, dead_letter=False)

    def dead_letters(self) -> _Receiver:
        return _Receiver(self.namespace, self.credential, self.queue, dead_letter=True)

    def dead_events(self) -> _Receiver:
        return _Receiver(
            self.namespace, self.credential, self.topic, self.subscription, dead_letter=True
        )

    def wake(self, trace: str, at: datetime | None = None) -> None:
        """Send "wake case X" now, or schedule it for ``at``."""
        message = ServiceBusMessage(
            wake_body(trace),
            message_id=f"wake-{trace}-{uuid.uuid4().hex[:12]}",
            content_type="application/json",
            application_properties={"kind": "wake"},
        )
        with self._lock, self._client.get_queue_sender(self.queue) as sender:
            if at is not None and at > datetime.now(UTC) + timedelta(seconds=1):
                sender.schedule_messages(message, at)
            else:
                sender.send_messages(message)

    def execute(self, trace: str) -> None:
        """Send "carry out case X" to the workers. Only a wake: the case decides what runs, under
        its lease (ADR 0028: Service Bus carries only wake-ups)."""
        message = ServiceBusMessage(
            wake_body(trace),
            message_id=f"execute-{trace}-{uuid.uuid4().hex[:12]}",
            content_type="application/json",
            application_properties={"kind": "execute"},
        )
        with self._lock, self._client.get_queue_sender(self.execution_queue) as sender:
            sender.send_messages(message)
