"""Azure control for the test bench: the day-load job, through ARM with the test bench's managed
identity (custom role "Airrec test bench operator (Dev)", spec §5.2). Fabric capacity control
left with the Eventstream (Service Bus intake spec)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

from azure.core.credentials import TokenCredential

ARM = "https://management.azure.com"
JOB_API = "2025-01-01"


class ControlError(RuntimeError):
    """An ARM call failed."""


def _arm(
    credential: TokenCredential, method: str, path: str, api: str, body: Any = None
) -> dict[str, Any]:
    token = credential.get_token(f"{ARM}/.default").token
    request = urllib.request.Request(
        f"{ARM}{path}?api-version={api}",
        data=json.dumps(body or {}).encode() if method == "POST" else None,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
    except (urllib.error.URLError, TimeoutError) as error:
        raise ControlError(f"{method} {path.rsplit('/', 1)[-1]} failed: {error}") from error
    return json.loads(body) if body else {}


class DayLoadJob:
    """The day-reset job (its own command reloads today with ``--force``) and, for the status,
    the nightly day-load job. Azure refuses a start that overrides a job's command unless the
    caller may rewrite the job, so the test bench only ever starts the reset job as it is."""

    def __init__(self, job_id: str, credential: TokenCredential, nightly_id: str = "") -> None:
        self._id, self._credential, self._nightly = job_id, credential, nightly_id

    def start(self) -> str:
        return str(_arm(self._credential, "POST", f"{self._id}/start", JOB_API)["name"])

    def execution(self, name: str) -> str:
        status = _arm(self._credential, "GET", f"{self._id}/executions/{name}", JOB_API)
        value = str(status.get("properties", {}).get("status", ""))
        if value == "Succeeded":
            return "Succeeded"
        return "Failed" if value in {"Failed", "Stopped", "Degraded"} else "Running"

    def last_success(self) -> datetime | None:
        """When today's data was last loaded: the later of the reset and nightly runs."""
        ends = [
            datetime.fromisoformat(item["properties"]["endTime"])
            for job in (self._id, self._nightly)
            if job
            for item in _arm(self._credential, "GET", f"{job}/executions", JOB_API).get("value", [])
            if item.get("properties", {}).get("status") == "Succeeded"
            and item["properties"].get("endTime")
        ]
        return max(ends) if ends else None
