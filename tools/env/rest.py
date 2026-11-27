"""Small REST clients for Azure Resource Manager, Microsoft Graph and Foundry, signed in as the
Azure CLI user. Tokens and secrets are never printed or written; an error carries the service's
status, code and message only."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

ARM = "https://management.azure.com"
GRAPH = "https://graph.microsoft.com/v1.0"
ARM_SCOPE = "https://management.azure.com/.default"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
FOUNDRY_SCOPE = "https://ai.azure.com/.default"
TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: Any = field(default=None)


Send = Callable[[str, str, Mapping[str, str], bytes | None], tuple[int, Mapping[str, str], bytes]]


class RestError(RuntimeError):
    def __init__(self, method: str, url: str, status: int, body: Any) -> None:
        error = body.get("error") if isinstance(body, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        text = message or (json.dumps(body)[:400] if body is not None else "")
        super().__init__(f"{method} {url.split('?')[0]} -> HTTP {status} {code or ''}: {text}")
        self.status = status
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect must not carry the bearer token anywhere: it fails the call instead."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _urllib_send(
    method: str, url: str, headers: Mapping[str, str], data: bytes | None
) -> tuple[int, Mapping[str, str], bytes]:
    request = urllib.request.Request(url, data=data, headers=dict(headers), method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers or {}), error.read()


class Rest:
    """Calls signed in as the Azure CLI user of ``tenant_id``."""

    def __init__(
        self,
        tenant_id: str,
        *,
        send: Send | None = None,
        token: Callable[[str], str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._send = send or _urllib_send
        self._sleep = sleep
        if token is None:
            from azure.identity import AzureCliCredential

            credential = AzureCliCredential(tenant_id=tenant_id, process_timeout=60)
            self._token: Callable[[str], str] = lambda scope: credential.get_token(scope).token
        else:
            self._token = token

    def request(
        self,
        method: str,
        url: str,
        body: Any = None,
        *,
        scope: str,
        headers: Mapping[str, str] | None = None,
        ok: tuple[int, ...] = (200, 201, 202, 204),
    ) -> Response:
        data = None if body is None else json.dumps(body).encode("utf-8")
        sent = {
            "Authorization": f"Bearer {self._token(scope)}",
            "Content-Type": "application/json",
            **(headers or {}),
        }
        status, received, raw = self._send(method, url, sent, data)
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            parsed = {"error": {"message": raw[:300].decode("utf-8", "replace")}}
        if status not in ok:
            raise RestError(method, url, status, parsed)
        return Response(status, {key.casefold(): value for key, value in received.items()}, parsed)

    def arm(self, method: str, path: str, body: Any = None, *, api_version: str) -> Response:
        separator = "&" if "?" in path else "?"
        url = f"{ARM}{path}{separator}api-version={api_version}"
        return self.request(method, url, body, scope=ARM_SCOPE)

    def graph(
        self, method: str, path: str, body: Any = None, *, headers: Mapping[str, str] | None = None
    ) -> Response:
        return self.request(method, f"{GRAPH}{path}", body, scope=GRAPH_SCOPE, headers=headers)

    def graph_items(self, path: str) -> list[dict[str, Any]]:
        page = self.graph("GET", path).body
        found = list(page.get("value", []))
        while page.get("@odata.nextLink"):
            page = self.request("GET", page["@odata.nextLink"], scope=GRAPH_SCOPE).body
            found.extend(page.get("value", []))
        return found

    def foundry(self, method: str, url: str, body: Any = None) -> Response:
        return self.request(method, url, body, scope=FOUNDRY_SCOPE)

    def wait(self, response: Response, *, timeout_seconds: float = 1800.0) -> Response:
        """Follow an ARM long-running operation (202 + Location) to its final response."""
        location = response.headers.get("location") or response.headers.get("azure-asyncoperation")
        if response.status != 202 or not location:
            return response
        deadline = time.monotonic() + timeout_seconds
        while True:
            retry = response.headers.get("retry-after", "10")
            self._sleep(float(retry) if retry.replace(".", "", 1).isdigit() else 10.0)
            response = self.request("GET", location, scope=ARM_SCOPE)
            if response.status != 202:
                return response
            if time.monotonic() > deadline:
                raise TimeoutError(f"ARM operation still running after {timeout_seconds:.0f} s")
