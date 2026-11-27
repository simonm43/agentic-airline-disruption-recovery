"""Nightly job: load the new Lisbon operating day into the sample airline systems.

Run as ``python -m airline_systems.day_loader`` by the scheduled Container Apps job, as the
Schedule system's identity. Idempotent: an already loaded day is left alone, and the database
lock in ``load_operating_day`` serialises it with the load-on-first-use fallback. The Test Bench
Reset starts the same job with ``--force`` to reload today from scratch.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import date
from typing import Any, Protocol

from opentelemetry.trace import SpanKind

from airline_systems.operating_day import build_operating_day
from airline_systems.schedule import lisbon_today

logger = logging.getLogger(__name__)


class DayStore(Protocol):
    def has_day(self, day: date) -> bool: ...
    def load_operating_day(self, day: date, op_day: Any, *, force: bool = False) -> bool: ...


def prepare_day(store: DayStore, day: date, *, force: bool = False) -> dict[str, Any]:
    started = time.perf_counter()
    if not force and store.has_day(day):
        return {"day": day.isoformat(), "loaded": False, "reason": "already loaded"}
    op_day = build_operating_day(day)
    loaded = store.load_operating_day(day, op_day, force=force)
    return {
        "day": day.isoformat(),
        "loaded": loaded,
        "flights": len(op_day.flights),
        "seconds": round(time.perf_counter() - started, 1),
    }


def main() -> None:
    from azure.identity import ManagedIdentityCredential

    from airline_systems import telemetry
    from airline_systems.sql import SqlScheduleStore

    logging.basicConfig(level=logging.INFO)
    credential = ManagedIdentityCredential(client_id=os.environ["AZURE_CLIENT_ID"])
    telemetry.configure("day-loader", credential, logger_name="airline_systems")
    store = SqlScheduleStore(
        os.environ["SQL_SERVER"],
        os.environ["SQL_DATABASE"],
        os.environ["AZURE_CLIENT_ID"],
        "schedule",
        credential,
    )
    force = "--force" in sys.argv[1:]
    day = lisbon_today()
    with telemetry.case_span(
        "Load operating day",
        None,
        kind=SpanKind.CONSUMER,
        attributes={"airrec.day": day.isoformat(), "airrec.force": force},
    ):
        result = prepare_day(store, day, force=force)
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
