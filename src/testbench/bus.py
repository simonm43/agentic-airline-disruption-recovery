"""The test bench publishes disruption CloudEvents to the Service Bus topic, standing in for the
airline systems (Service Bus intake spec §3.5). Entra only: the namespace has local keys disabled.

Any failure is QueueUnavailable, so the console says so instead of pretending it was sent.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from azure.core.credentials import TokenCredential

ORIGIN = "arrow-testbench"


class QueueUnavailable(RuntimeError):
    """Service Bus could not be reached or refused the message."""


class EventSender:
    def __init__(self, namespace: str, topic: str, credential: TokenCredential) -> None:
        self._namespace, self._topic, self._credential = namespace, topic, credential
        self._lock = threading.Lock()

    def send(self, event: dict[str, Any]) -> None:
        from azure.servicebus import ServiceBusClient, ServiceBusMessage

        message = ServiceBusMessage(
            json.dumps(event),
            message_id=str(event["id"]),  # the topic drops a resend of the same event (10 min)
            subject=str(event["type"]),
            content_type="application/json",
        )
        try:
            with (
                self._lock,
                ServiceBusClient(self._namespace, self._credential) as client,
                client.get_topic_sender(self._topic) as sender,
            ):
                sender.send_messages(message)
        except Exception as error:
            raise QueueUnavailable(f"Service Bus send failed: {type(error).__name__}") from error
