"""ARROW's workers app (three container apps spec): one process, no ingress.

It runs the Service Bus intake of disruption events, the assessment receivers (each runs the
orchestrator in Foundry), the execution intake, the dead-letter drains and the execution
supervisor (resumes, the sweeper, storm call-by times). Only this app carries out plans, so only
its identity, the one holder of the airline systems' Actions.Write, writes to them. A periodic
wake lets the supervisor see work the operator and tools apps recorded without a wake reaching
it (an approval whose send failed, a case whose assessment wake was lost); ``/healthz`` answers
the liveness probe, and fails once a thread has stopped so the platform restarts the replica.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ops_app.intake import Intake, serve

if TYPE_CHECKING:
    from ops_app.engine import Operations
    from ops_app.events import IntakeLog
    from ops_app.store import CaseStore

logger = logging.getLogger("ops_app.workers")
SUPERVISOR_INTERVAL_SECONDS = 30.0
# How often the idle supervisor looks for work another app recorded (three container apps spec
# §5); Azure SQL S2 is always on (ADR 0031), so the reads cost nothing extra.
BACKSTOP_SECONDS = 60.0


class Queues(Protocol):
    """The Service Bus entities the workers receive from (``ops_app.bus.ServiceBusQueue``)."""

    def events(self) -> AbstractContextManager[Any]: ...
    def receiver(self) -> AbstractContextManager[Any]: ...
    def execution_receiver(self) -> AbstractContextManager[Any]: ...
    def dead_letters(self) -> AbstractContextManager[Any]: ...
    def dead_events(self) -> AbstractContextManager[Any]: ...
    def wake(self, trace: str, at: datetime | None = None) -> None: ...


@dataclass
class Runtime:
    """The workers' threads, and the event that stops their receive loops and the backstop."""

    threads: list[threading.Thread] = field(default_factory=list)
    stopping: threading.Event = field(default_factory=threading.Event)

    def stopped(self) -> list[str]:
        return [thread.name for thread in self.threads if not thread.is_alive()]

    def stop(self) -> None:
        self.stopping.set()


def _backstop(operations: Operations, every: float, stopping: threading.Event) -> None:
    while not stopping.wait(every):
        operations.wake()


def start(
    operations: Operations,
    store: CaseStore,
    intake_log: IntakeLog,
    queues: Queues,
    *,
    workers: int,
    reporter_oid: str,
    backstop_seconds: float = BACKSTOP_SECONDS,
) -> Runtime:
    """Start every thread of the workers app: one receiver opens the cases of disruption events
    from ARROW's topic subscription at once; ``workers`` receivers assess cases from the
    assessment queue, one each; one receiver carries out plans from the execution queue; two
    drain the dead-letter queues; the supervisor resumes executions."""
    intake = Intake(operations, store, intake_log, reporter_oid, queues.wake)
    runtime = Runtime()
    receivers: list[tuple[str, Callable[[], AbstractContextManager[Any]], Callable[[Any], object]]]
    receivers = [
        ("disruption-intake", queues.events, intake.handle),
        *((f"assessment-worker-{n}", queues.receiver, intake.handle) for n in range(workers)),
        ("execution-intake", queues.execution_receiver, intake.handle),
        ("assessment-dead-letters", queues.dead_letters, intake.dead),
        ("event-dead-letters", queues.dead_events, intake.dead),
    ]
    for name, open_receiver, handle in receivers:
        runtime.threads.append(
            threading.Thread(
                target=serve,
                args=(open_receiver, handle),
                kwargs={"stop": runtime.stopping},
                name=name,
                daemon=True,
            )
        )
    runtime.threads.append(
        threading.Thread(
            target=operations.supervise,
            args=(SUPERVISOR_INTERVAL_SECONDS,),
            name="execution-supervisor",
            daemon=True,
        )
    )
    runtime.threads.append(
        threading.Thread(
            target=_backstop,
            args=(operations, backstop_seconds, runtime.stopping),
            name="supervisor-backstop",
            daemon=True,
        )
    )
    for thread in runtime.threads:
        thread.start()
    logger.info("Workers started: %s", ", ".join(thread.name for thread in runtime.threads))
    return runtime


def health_app(runtime: Runtime) -> FastAPI:
    """The liveness probe's only route; the workers app has no ingress."""
    app = FastAPI(title="ARROW workers", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        stopped = runtime.stopped()
        if stopped:
            return JSONResponse({"status": "stopped", "threads": stopped}, status_code=503)
        return JSONResponse({"status": "ok"})

    return app


def serve_forever(
    operations: Operations,
    store: CaseStore,
    intake_log: IntakeLog,
    queues: Queues,
    *,
    workers: int,
    reporter_oid: str,
    port: int = 8080,
) -> None:
    import uvicorn

    runtime = start(
        operations, store, intake_log, queues, workers=workers, reporter_oid=reporter_oid
    )
    uvicorn.run(health_app(runtime), host="0.0.0.0", port=port)
