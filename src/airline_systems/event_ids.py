"""The operations event id an airline system issues when ARROW's executor records a disruption in
it, as the first action of a plan it carries out (owner: the airline's systems never see ARROW's
case id).

An event id reads EV-XXXXXX: six upper-case hex digits. In these sample systems they are the
seventh to twelfth digits of the trace the record was written under, so an event id never repeats
its case id (DSR-YYYYMMDD- and the first six digits). Maintenance finds the defect behind an event
id among the day's recorded delays and defects, and refuses an unknown or ambiguous one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date
from typing import Protocol

EVENT_ID = re.compile(r"EV-([0-9A-F]{6})")
TRACE = re.compile(r"[0-9a-f]{32}")


class UnknownEvent(ValueError):
    """No record, or more than one, stands behind the event id."""


class EventRecords(Protocol):
    def event_traces(self, day: date, digits: str) -> list[str]: ...


def event_id(trace: str) -> str:
    return f"EV-{trace[6:12].upper()}"


def parse(value: str) -> str:
    """The six hex digits (lower case) an event id names."""
    match = EVENT_ID.fullmatch(str(value).strip().upper())
    if match is None:
        raise ValueError(
            f"Not an operations event id: {str(value)[:40]!r} (expected EV- and six hex digits)"
        )
    return match.group(1).lower()


def single(value: str, traces: Iterable[str]) -> str:
    """The one trace behind an event id; refuses an unknown or ambiguous event."""
    found = sorted({trace for trace in traces if TRACE.fullmatch(trace)})
    if not found:
        raise UnknownEvent(f"No event {value} is recorded today")
    if len(found) > 1:
        raise UnknownEvent(f"Event {value} is ambiguous; ask OCC")
    return found[0]


def trace_of(records: EventRecords, value: str, day: date) -> str:
    """The trace this system recorded the event under on operating day ``day``."""
    return single(value, records.event_traces(day, parse(value)))
