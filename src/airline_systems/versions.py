"""A flight's version: a short token of what a schedule change compares.

Operations control serves it with each flight; ARROW sends the version of the flight it validated
as If-Match, and a change to an outdated flight is refused (native dispatch spec §3).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

VERSIONED = ("std_local", "sta_local", "status", "aircraft_reg")


def flight_version(flight: Mapping[str, Any]) -> str:
    """First 16 hex digits of SHA-256 over departure, arrival, status and aircraft."""
    text = "|".join(str(flight[key]) for key in VERSIONED)
    return hashlib.sha256(text.encode()).hexdigest()[:16]
