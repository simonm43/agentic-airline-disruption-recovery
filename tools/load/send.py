"""Send the load test's disruption reports to the environment's Service Bus topic, as the airline's
systems would (tools/load/scenario.py builds them from the generated day).

    python -m tools.load.send <env> day [--speed 8]              # read-only: the schedule
    python -m tools.load.send <env> day [--speed 8] --apply      # send at the scheduled times
    python -m tools.load.send <env> burst --run <run> --apply    # the leftover aircraft at once

Each report is a CloudEvent 1.0 exactly as the Test Bench sends one (same validation), with the
source ``arrow-load/<run>`` so tools/load/report.py finds its cases. It is posted to the topic
over Service Bus's REST API with the signed-in user's Entra token, which needs Azure Service Bus
Data Sender on the topic; no key exists (local authentication is disabled). A role assigned just
before can take minutes to take effect, so the first report is retried while Service Bus refuses
it (401 or 403), for up to ten minutes, and the schedule's clock starts once it is accepted. A
send log without secrets goes to .local/load/<run>.jsonl.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from airline_systems.schedule import lisbon_today
from testbench.events import cloud_event
from tools.env.environment import load
from tools.env.rest import Rest, RestError
from tools.load import scenario

ROOT = Path(__file__).resolve().parents[2]
LOGS = ROOT / ".local" / "load"
TOPIC = "disruption-events"
SERVICE_BUS_SCOPE = "https://servicebus.azure.net/.default"
ACCESS_WAIT_SECONDS = 600.0
ACCESS_RETRY_SECONDS = 15.0


def origin(run: str) -> str:
    return f"arrow-load/{run}"


def post(rest: Rest, namespace: str, event: dict[str, object]) -> None:
    """One message on the topic; the topic drops a resend of the same id for 10 minutes."""
    rest.request(
        "POST",
        f"https://{namespace}/{TOPIC}/messages",
        event,
        scope=SERVICE_BUS_SCOPE,
        headers={
            "BrokerProperties": json.dumps({"MessageId": event["id"], "Label": event["type"]})
        },
        ok=(201,),
    )


def post_once_allowed(
    rest: Rest,
    namespace: str,
    event: dict[str, object],
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """The first report: wait while Service Bus refuses the new role, then fail as usual."""
    deadline = clock() + ACCESS_WAIT_SECONDS
    while True:
        try:
            post(rest, namespace, event)
            return
        except RestError as error:
            if error.status not in (401, 403) or clock() >= deadline:
                raise
            print(
                f"HTTP {error.status}: waiting for the Data Sender role to take effect", flush=True
            )
            sleep(ACCESS_RETRY_SECONDS)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.load.send", description=__doc__)
    parser.add_argument("environment")
    parser.add_argument("phase", choices=("day", "burst"))
    parser.add_argument("--speed", type=float, default=8.0, help="times real time (day phase)")
    parser.add_argument("--run", help="the day phase's run id (burst phase)")
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args(argv)
    env = load(arguments.environment)
    day = lisbon_today()
    reports, unused = scenario.day_reports(day, speed=arguments.speed)
    if arguments.phase == "burst":
        if not arguments.run:
            parser.error("burst needs --run <the day phase's run id>")
        run = arguments.run
        reports = scenario.burst_reports(day, unused)
    else:
        run = f"lt{datetime.now(UTC):%Y%m%d%H%M}"
    for report in reports:
        report.checked()
    kinds = Counter(report.kind for report in reports)
    span = max(report.offset_seconds for report in reports) / 60
    print(
        json.dumps(
            {
                "run": run,
                "phase": arguments.phase,
                "operating_day": day.isoformat(),
                "reports": len(reports),
                "kinds": dict(kinds),
                "minutes": round(span, 1),
                "topic": f"{env.names()['serviceBusFqdn']}/{TOPIC}",
            },
            indent=2,
        )
    )
    if not arguments.apply:
        print("Read-only: nothing sent. Re-run with --apply.")
        return 0

    rest = Rest(env.tenant_id)
    namespace = env.names()["serviceBusFqdn"]
    LOGS.mkdir(parents=True, exist_ok=True)
    started: float | None = None
    failures = 0
    with (LOGS / f"{run}.jsonl").open("a", encoding="utf-8") as log:
        for report in reports:
            if started is not None:
                wait = report.offset_seconds - (time.monotonic() - started)
                if wait > 0:
                    time.sleep(wait)
            event = cloud_event(report.kind, report.checked())  # type: ignore[arg-type]
            event["source"] = origin(run)
            record = {
                "phase": arguments.phase,
                "id": event["id"],
                "kind": report.kind,
                "subject": report.subject,
                "sent_at": datetime.now(UTC).isoformat(),
            }
            try:
                if started is None:
                    post_once_allowed(rest, namespace, event)
                else:
                    post(rest, namespace, event)
                record["sent"] = True
            except Exception as error:  # noqa: BLE001 - logged and counted, the run goes on
                failures += 1
                record.update(sent=False, error=str(error)[:300])
            if started is None:
                started = time.monotonic() - report.offset_seconds
            log.write(json.dumps(record) + "\n")
            log.flush()
    print(f"sent {len(reports) - failures} of {len(reports)}; log .local/load/{run}.jsonl")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
