"""The case id OCC and the agents use (owner: "it should not be a technical id, trace_id
is really only for OTEL").

A case id reads DSR-YYYYMMDD-XXXXXX: the operating day and the first six hex digits of the case's
internal trace id, upper case. ARROW keeps the 32-hex trace id as the case's storage key and
OpenTelemetry trace; the agents and their tools see only the case id, and each side finds the
trace behind it in its own records.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime

CASE_ID = re.compile(r"DSR-(\d{8})-([0-9A-F]{6})")
TRACE = re.compile(r"[0-9a-f]{32}")


def case_id(trace: str, day: date) -> str:
    return f"DSR-{day:%Y%m%d}-{trace[:6].upper()}"


def parse(value: str) -> tuple[date, str]:
    """The operating day and the trace id's first six hex digits (lower case)."""
    match = CASE_ID.fullmatch(str(value).strip())
    if match is None:
        raise ValueError(f"Not a case id: {str(value)[:40]!r} (expected DSR-YYYYMMDD-XXXXXX)")
    return datetime.strptime(match.group(1), "%Y%m%d").date(), match.group(2).lower()


def single(value: str, traces: Iterable[str]) -> str:
    """The one trace behind a case id; refuses an unknown or ambiguous case."""
    found = sorted({trace for trace in traces if TRACE.fullmatch(trace)})
    if not found:
        raise ValueError(f"No case {value} is recorded here")
    if len(found) > 1:
        raise ValueError(f"Case {value} is ambiguous; ask OCC")
    return found[0]
