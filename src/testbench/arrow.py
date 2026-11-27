"""ARROW's test bench endpoints (spec §4.2), called with the test bench's managed identity."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import date
from typing import Any

from azure.core.credentials import TokenCredential


class ArrowUnavailable(RuntimeError):
    """ARROW could not be reached or refused the test bench."""


class ArrowClient:
    def __init__(self, ops_url: str, api_app_id: str, credential: TokenCredential) -> None:
        self._url = ops_url.rstrip("/")
        self._scope = f"api://{api_app_id}/.default"
        self._credential = credential

    def _call(self, method: str, path: str) -> tuple[int, Any]:
        token = self._credential.get_token(self._scope).token
        request = urllib.request.Request(
            f"{self._url}{path}",
            data=b"" if method == "POST" else None,
            method=method,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return 404, None
            raise ArrowUnavailable(f"ARROW answered {error.code} for {path}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise ArrowUnavailable(f"ARROW unreachable: {error}") from error

    def event(self, event_id: str) -> dict[str, Any] | None:
        status, body = self._call("GET", f"/api/events/{event_id}")
        return None if status == 404 else dict(body)

    def events(self, day: date) -> list[dict[str, Any]]:
        """Every event ARROW received on one Lisbon day, with its case or why it has none."""
        body = self._call("GET", f"/api/events?day={day.isoformat()}")[1]
        return [dict(item) for item in body.get("events", [])]

    def close_cases(self) -> dict[str, Any]:
        return dict(self._call("POST", "/api/test/close-cases")[1])
