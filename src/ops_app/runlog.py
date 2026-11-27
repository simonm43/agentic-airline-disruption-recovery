"""What ARROW keeps of an orchestrator run (business UI and free-text spec §4.1): its steps as
Foundry returned them, shortened so a case stays small, plus tokens and model."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

MAX_TEXT = 4000
MAX_ITEMS = 60


def clip(value: Any, depth: int = 0) -> Any:
    """A JSON-safe copy with long strings shortened."""
    if isinstance(value, str):
        return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + " …"
    if isinstance(value, Mapping):
        if depth >= 6:
            return "…"
        return {str(k): clip(v, depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        return [clip(v, depth + 1) for v in value[:40]] if depth < 6 else "…"
    if value is None or isinstance(value, bool | int | float):
        return value
    return str(value)[:MAX_TEXT]


def record_run(response: Mapping[str, Any]) -> dict[str, Any]:
    items = [item for item in response.get("output") or [] if isinstance(item, Mapping)]
    usage = response.get("usage")
    return {
        "response_id": response.get("id"),
        "model": response.get("model"),
        "usage": clip(dict(usage)) if isinstance(usage, Mapping) else {},
        "items": [clip(dict(item)) for item in items[:MAX_ITEMS]],
    }
