"""ARROW Test Bench API: owner-only (Easy Auth plus an oid check), spec §5."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from testbench.arrow import ArrowUnavailable
from testbench.bus import QueueUnavailable
from testbench.control import ControlError
from testbench.service import ResetRunning, TestBench

STATIC = Path(__file__).parent / "static"
ASSETS = {"app.js", "favicon.svg", "styles.css"}
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_OID_CLAIMS = ("http://schemas.microsoft.com/identity/claims/objectidentifier", "oid")


def _caller_oid(request: Request) -> str | None:
    header = request.headers.get("x-ms-client-principal")
    if not header:
        return None
    try:
        claims = json.loads(base64.b64decode(header))["claims"]
    except (ValueError, KeyError, TypeError):
        return ""
    values = {str(c.get("typ")): str(c.get("val")) for c in claims if isinstance(c, dict)}
    oid = next((values[name] for name in _OID_CLAIMS if name in values), "").lower()
    return oid if _GUID.match(oid) else ""


def owner_oids(value: str) -> frozenset[str]:
    """OWNER_OIDS as object ids; empty entries (a trailing comma) are dropped, never admitted."""
    oids = frozenset(part.strip().lower() for part in value.split(",") if part.strip())
    if not oids or not all(_GUID.match(oid) for oid in oids):
        raise ValueError("OWNER_OIDS must be a comma-separated list of object ids")
    return oids


def create_app(
    bench: TestBench, owner_oids: frozenset[str], public_url: str, ops_url: str = ""
) -> FastAPI:
    app = FastAPI(title="ARROW Test Bench", docs_url=None, redoc_url=None, openapi_url=None)

    def owner(request: Request, *, write: bool = False) -> None:
        oid = _caller_oid(request)
        if oid is None:
            raise HTTPException(401, "Sign in required")
        if not oid or oid not in owner_oids:
            raise HTTPException(403, "The test bench is for its owner only")
        if write and (
            request.headers.get("origin") != public_url
            or request.headers.get("x-testbench-request") != "1"
        ):
            raise HTTPException(403, "Same-origin test bench request required")

    @app.exception_handler(QueueUnavailable)
    async def queue_unavailable(request: Request, error: QueueUnavailable) -> JSONResponse:
        logging.getLogger("testbench").warning("Service Bus unavailable: %s", error)
        return JSONResponse({"detail": "Service Bus unavailable"}, status_code=503)

    @app.exception_handler(ArrowUnavailable)
    async def arrow_unavailable(request: Request, error: ArrowUnavailable) -> JSONResponse:
        logging.getLogger("testbench").warning("ARROW unavailable: %s", error)
        return JSONResponse({"detail": "ARROW unavailable"}, status_code=503)

    @app.exception_handler(ControlError)
    async def control_failed(request: Request, error: ControlError) -> JSONResponse:
        logging.getLogger("testbench").warning("Azure control failed: %s", error)
        return JSONResponse({"detail": "Azure control call failed"}, status_code=503)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/status")
    def status(request: Request) -> dict[str, Any]:
        owner(request)
        return {**bench.status(), "today": bench.today().isoformat(), "ops_url": ops_url}

    @app.get("/api/presets")
    def presets(request: Request) -> dict[str, Any]:
        owner(request)
        return {"presets": bench.presets()}

    @app.post("/api/events")
    def send(request: Request, body: dict[str, Any] = Body()) -> dict[str, Any]:  # noqa: B008
        owner(request, write=True)
        data = body.get("data")
        try:
            return bench.send(
                body.get("preset"),
                body.get("kind"),
                data if isinstance(data, dict) else None,
            )
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/api/events")
    def events(request: Request, day: str | None = None) -> dict[str, Any]:
        owner(request)
        try:
            chosen = date.fromisoformat(day) if day else bench.today()
        except ValueError as error:
            raise HTTPException(422, "day: YYYY-MM-DD") from error
        return {"day": chosen.isoformat(), "events": bench.events(chosen)}

    @app.get("/api/events/{event_id}")
    def event(event_id: str, request: Request) -> dict[str, Any]:
        owner(request)
        found = bench.event(event_id)
        if found is None:
            raise HTTPException(404, "Event not found")
        return found

    @app.post("/api/reset", status_code=202)
    def reset(request: Request) -> dict[str, Any]:
        owner(request, write=True)
        try:
            return bench.reset()
        except ResetRunning as error:
            raise HTTPException(409, str(error)) from error

    @app.get("/api/reset")
    def reset_state(request: Request) -> dict[str, Any]:
        owner(request)
        return bench.reset_state()

    # The browser checks the page files again on each load, so a release never mixes old and new.
    fresh = {"Cache-Control": "no-cache"}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers=fresh)

    @app.get("/{asset}")
    def asset(asset: str) -> FileResponse:
        if asset not in ASSETS:
            raise HTTPException(404, "Not found")
        return FileResponse(STATIC / asset, headers=fresh)

    return app


def build_bench() -> TestBench:
    """The test bench with its real adapters, from the app's environment."""
    from azure.identity import ManagedIdentityCredential

    from testbench.arrow import ArrowClient
    from testbench.bus import EventSender
    from testbench.control import DayLoadJob

    credential = ManagedIdentityCredential(client_id=os.environ["AZURE_CLIENT_ID"])
    return TestBench(
        sender=EventSender(
            os.environ["SERVICEBUS_NAMESPACE"],
            os.getenv("DISRUPTION_TOPIC", "disruption-events"),
            credential,
        ),
        job=DayLoadJob(os.environ["JOB_ID"], credential, os.getenv("NIGHTLY_JOB_ID", "")),
        arrow=ArrowClient(os.environ["OPS_URL"], os.environ["OPS_API_APP_ID"], credential),
    )


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    logging.getLogger("azure").setLevel(logging.WARNING)
    owners = owner_oids(os.environ["OWNER_OIDS"])
    app = create_app(build_bench(), owners, os.environ["PUBLIC_URL"], os.environ["OPS_URL"])
    uvicorn.run(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
