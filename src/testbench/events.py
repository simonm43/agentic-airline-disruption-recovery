"""The disruption CloudEvent the test bench publishes to the Service Bus topic (event trigger
and test bench spec §3), and the weather update that settles a held storm case (case summary
and held storm spec §2)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

TESTBENCH_ORIGIN = "arrow-testbench"
EVENT_TYPE_PREFIX = "com.arrow.disruption."
# Never opens a case: ARROW matches it to the open storm case at that airport and window.
WEATHER_UPDATE_TYPE = "com.arrow.weather.update"


def cloud_event(
    kind: Literal["message", "flight_delay", "aircraft_defect", "weather_update"],
    data: dict[str, Any],
    *,
    event_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One CloudEvent 1.0; every send gets a new id unless one is given (a deliberate repeat)."""
    sent = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    typed = (
        WEATHER_UPDATE_TYPE
        if kind == "weather_update"
        else f"{EVENT_TYPE_PREFIX}{'reported' if kind == 'message' else kind}"
    )
    return {
        "specversion": "1.0",
        "id": event_id or str(uuid.uuid4()),
        "source": TESTBENCH_ORIGIN,
        "type": typed,
        "time": sent.isoformat(),
        "datacontenttype": "application/json",
        "data": dict(data),
    }
