"""Test bench business flows: sending events, merging their outcomes, resetting the day."""

from __future__ import annotations

import base64
import json
import unittest
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest import mock

from fastapi.testclient import TestClient

from testbench import control
from testbench.app import create_app, owner_oids
from testbench.arrow import ArrowUnavailable
from testbench.bus import QueueUnavailable
from testbench.events import cloud_event
from testbench.service import TestBench

OWNER = "00000000-0000-4000-8000-000000000010"
PUBLIC_URL = "https://testbench.test"
NOW = datetime(2026, 11, 10, 9, 20, tzinfo=UTC)


def check_cloud_event_has_one_new_id_per_send() -> None:
    data = {
        "flight_no": "AR259",
        "delay_minutes": 20,
        "cause": "staff_shortage",
        "description": "Ground staff shortage at the gate",
    }
    first = cloud_event("flight_delay", data)
    second = cloud_event("flight_delay", data)

    assert first["id"] != second["id"]
    assert str(uuid.UUID(first["id"])) == first["id"]
    assert first["specversion"] == "1.0" and first["datacontenttype"] == "application/json"
    assert first["source"] == "arrow-testbench"
    assert first["type"] == "com.arrow.disruption.flight_delay"
    assert first["time"].endswith("+00:00"), first["time"]
    assert first["data"] == data
    fixed = str(uuid.uuid4())
    now = datetime(2026, 11, 10, 8, 12, 3, tzinfo=UTC)
    pinned = cloud_event("aircraft_defect", {"aircraft_reg": "CS-TQA"}, event_id=fixed, now=now)
    assert pinned["id"] == fixed and pinned["time"] == "2026-11-10T08:12:03+00:00"
    assert pinned["type"] == "com.arrow.disruption.aircraft_defect"


class Sender:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail = fail

    def send(self, event: dict[str, Any]) -> None:
        if self.fail:
            raise QueueUnavailable("Service Bus send failed: ServiceBusConnectionError")
        self.sent.append(event)


class Job:
    def __init__(self, outcome: str = "Succeeded") -> None:
        self.outcome = outcome
        self.calls: list[str] = []

    def start(self) -> str:
        self.calls.append("job.start")
        return "caj-airrec-dayload-dev-exec1"

    def execution(self, name: str) -> str:
        return self.outcome

    def last_success(self) -> datetime | None:
        return NOW if self.outcome == "Succeeded" else None


class Arrow:
    """ARROW's test bench endpoints: the day's received events and one event's view."""

    def __init__(self, received: list[dict[str, Any]] | None = None, *, fail: bool = False) -> None:
        self.received = received or []
        self.fail = fail
        self.days: list[date] = []
        self.skipped: list[dict[str, str]] = []
        self.calls: list[str] = []

    def events(self, day: date) -> list[dict[str, Any]]:
        self.days.append(day)
        if self.fail:
            raise ArrowUnavailable("ARROW answered 502 for /api/events")
        return self.received

    def event(self, event_id: str) -> dict[str, Any] | None:
        item = next((i for i in self.received if i["event_id"] == event_id), None)
        if item is None:
            return None
        if "rejected" in item:
            return {**item["rejected"], "body": item["envelope"]}
        delivery = {
            "at": item["queued_at"],
            "enqueued_at": item["queued_at"],
            "body": item["envelope"],
        }
        return {**item["case"], "deliveries": [{**delivery, "delivery": "new_case"}]}

    def close_cases(self) -> dict[str, Any]:
        self.calls.append("close_cases")
        return {"closed": ["DSR-20261110-AAAAAA"], "skipped": self.skipped}


class Spawn:
    """Background work runs when the test says so, so a reset can be caught mid-flight."""

    def __init__(self) -> None:
        self.pending: list[Callable[[], None]] = []

    def __call__(self, work: Callable[[], None]) -> None:
        self.pending.append(work)

    def run(self) -> None:
        while self.pending:
            self.pending.pop(0)()


def principal(oid: str) -> dict[str, str]:
    claims = {"claims": [{"typ": "oid", "val": oid}]}
    return {
        "X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(claims).encode()).decode(),
        "Origin": PUBLIC_URL,
        "X-Testbench-Request": "1",
    }


def bench(
    clock: list[datetime] | None = None, **parts: Any
) -> tuple[TestBench, TestClient, dict[str, Any]]:
    deps = {"sender": Sender(), "job": Job(), "arrow": Arrow(), "spawn": Spawn(), **parts}
    now = clock or [NOW]
    test_bench = TestBench(**deps, now=lambda: now[0], sleep=lambda _seconds: None)
    client = TestClient(create_app(test_bench, frozenset({OWNER}), PUBLIC_URL))
    return test_bench, client, deps


def received(
    event_id: str,
    sent: datetime,
    queued_after: float,
    *,
    case: dict[str, Any] | None = None,
    rejected: dict[str, Any] | None = None,
    envelope: dict[str, Any] | None = None,
    **data: Any,
) -> dict[str, Any]:
    """One event as ARROW lists it: the envelope as received, when it was queued, its case."""
    kind = "aircraft_defect" if "aircraft_reg" in data else "flight_delay"
    item: dict[str, Any] = {
        "event_id": event_id,
        "envelope": envelope or cloud_event(kind, data, event_id=event_id, now=sent),
        "queued_at": (sent + timedelta(seconds=queued_after)).isoformat(),
    }
    if rejected is not None:
        return {**item, "rejected": rejected}
    return {**item, "case": case}


def view(trace: str, created: datetime, status: str, **extra: Any) -> dict[str, Any]:
    return {
        "trace_id": trace,
        "display_id": f"DSR-{trace.upper()}",
        "status": status,
        "created_at": created.isoformat(),
        "manual_intervention": False,
        "deliveries": [],
        "plans": [],
        "execution": None,
        **extra,
    }


def check_preset_send_publishes_a_cloud_event_and_reports_an_unavailable_queue() -> None:
    _, client, deps = bench()
    sent = client.post("/api/events", json={"preset": "ar259-20"}, headers=principal(OWNER))

    assert sent.status_code == 200, sent.text
    [event] = deps["sender"].sent
    # The presets are free text, as airline systems write it (D94).
    assert event["type"] == "com.arrow.disruption.reported"
    # Owner: Case 1 is a border-control queue (Lisbon's Entry/Exit System checks) on
    # AR259, a departure outside the Schengen area.
    assert event["data"] == {
        "text": "Border-control queues (Entry/Exit System checks) are holding passengers for"
        " AR259: it will leave about 20 minutes late.",
        "system": "Airport operations",
    }
    assert sent.json()["event_id"] == event["id"]
    status = client.get("/api/status", headers=principal(OWNER)).json()
    assert status["can_send"] is True and "fabric" not in status, status

    form = client.post(
        "/api/events", json={"kind": "message", "data": {"text": "  "}}, headers=principal(OWNER)
    )
    assert form.status_code == 422 and "3–2,000 characters" in form.text, form.text

    other = client.post(
        "/api/events",
        json={"preset": "ar259-20"},
        headers=principal("11111111-2222-3333-4444-555555555555"),
    )
    assert other.status_code == 403
    assert len(deps["sender"].sent) == 1

    _, client, _ = bench(sender=Sender(fail=True))
    down = client.post("/api/events", json={"preset": "ar259-20"}, headers=principal(OWNER))
    assert down.status_code == 503 and down.json() == {"detail": "Service Bus unavailable"}


def check_only_a_listed_owner_is_admitted() -> None:
    # Review: a trailing comma in OWNER_OIDS must not admit a principal without an oid.
    assert owner_oids(f" {OWNER.upper()} ,") == frozenset({OWNER})
    for bad in (",", "not-an-oid"):
        try:
            owner_oids(bad)
        except ValueError:
            continue
        raise AssertionError(f"OWNER_OIDS {bad!r} was accepted")

    test_bench, _, _ = bench()
    client = TestClient(create_app(test_bench, frozenset({OWNER, ""}), PUBLIC_URL))
    claims = {"claims": [{"typ": "name", "val": "someone"}]}
    no_oid = {"X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(claims).encode()).decode()}
    assert client.get("/api/status", headers=no_oid).status_code == 403
    assert client.get("/api/status", headers=principal(OWNER)).status_code == 200


def check_events_merge_arrow_events_with_what_was_sent() -> None:
    delay = {"flight_no": "AR259", "delay_minutes": 20, "cause": "staff_shortage"}
    sent = NOW - timedelta(minutes=10)
    ids = [str(uuid.uuid4()) for _ in range(6)]
    case = sent + timedelta(seconds=6)
    policy_run = {"status": "completed", "approved_by": "policy", "actions": []}
    original = {"trace_id": "a1", "display_id": "DSR-A1"}
    listed_by_arrow = [
        received(
            ids[0],
            sent,
            2,
            case=view("a1", case, "assessment_ready", execution=policy_run),
            **delay,
        ),
        received(
            ids[1],
            sent + timedelta(seconds=1),
            2,
            case=view("b2", case, "assessment_ready"),
            **delay,
        ),
        received(
            ids[2],
            sent + timedelta(seconds=2),
            2,
            case=view("c3", case, "assessment_ready", manual_intervention=True),
            **delay,
        ),
        received(ids[3], sent + timedelta(seconds=3), 2, case=view("d4", case, "failed"), **delay),
        received(
            ids[4],
            sent + timedelta(seconds=4),
            2,
            rejected={"status": "rejected", "reason": "data.delay_minutes: multiple of 5"},
            **delay,
        ),
        # The same AR259 report again: ARROW added it to DSR-A1 instead of a second delay.
        received(
            ids[5],
            sent + timedelta(seconds=7),
            2,
            case=view("e5", case, "closed", repeat_of=original),
            **delay,
        ),
    ]
    clock = [NOW - timedelta(minutes=20)]
    test_bench, client, deps = bench(clock, arrow=Arrow(listed_by_arrow))
    # Two sends ARROW has not read yet: one 20 minutes ago, one just now.
    lost = test_bench.send("ar259-20", None, None)["event_id"]
    clock[0] = NOW
    waiting = test_bench.send("ar259-20", None, None)["event_id"]

    listed = client.get("/api/events", params={"day": "2026-11-10"}, headers=principal(OWNER))

    assert listed.status_code == 200, listed.text
    events = {item["event_id"]: item for item in listed.json()["events"]}
    assert [events[i]["outcome"] for i in [*ids, waiting, lost]] == [
        "Executed automatically",
        "Waiting for OCC",
        "Manual intervention",
        "Stopped",
        "Rejected",
        "Added to open case",
        "Waiting for ARROW",
        "Not received by ARROW",
    ]
    assert deps["arrow"].days == [date(2026, 11, 10)]
    assert events[ids[5]]["case"] == original, events[ids[5]]
    first = events[ids[0]]
    assert first["queued_after_s"] == 2 and first["case_after_s"] == 6, first
    assert first["case"] == {"display_id": "DSR-A1", "trace_id": "a1"}
    assert first["label"] == "AR259 +20 · staff shortage"
    assert events[ids[4]]["reason"] == "data.delay_minutes: multiple of 5"
    assert events[waiting]["case"] is None and events[waiting]["queued_after_s"] is None
    newest_first = [item["sent_at"] for item in listed.json()["events"]]
    assert newest_first == sorted(newest_first, reverse=True)

    detail = client.get(f"/api/events/{ids[0]}", headers=principal(OWNER)).json()
    assert detail["sent"] == listed_by_arrow[0]["envelope"]
    assert detail["queued"]["enqueued_at"] == listed_by_arrow[0]["queued_at"], detail["queued"]
    assert detail["outcome"]["status"] == "assessment_ready"
    rejected = client.get(f"/api/events/{ids[4]}", headers=principal(OWNER)).json()
    assert rejected["rejection"]["reason"] == "data.delay_minutes: multiple of 5", rejected
    pending = client.get(f"/api/events/{waiting}", headers=principal(OWNER)).json()
    assert pending["sent"]["id"] == waiting and pending["outcome"] is None, pending
    unknown = client.get(f"/api/events/{uuid.uuid4()}", headers=principal(OWNER))
    assert unknown.status_code == 404

    # "Today" is the Lisbon operating day: in summer 00:30 Lisbon is 23:30 UTC the day before.
    clock = [datetime(2026, 7, 9, 23, 30, tzinfo=UTC)]
    test_bench, _, deps = bench(clock)
    summer = test_bench.send("ar259-20", None, None)["event_id"]
    clock[0] = datetime(2026, 7, 10, 23, 30, tzinfo=UTC)
    test_bench.send("ar259-20", None, None)
    assert [item["event_id"] for item in test_bench.events(date(2026, 7, 10))] == [summer]


def check_events_report_arrow_unavailable() -> None:
    _, client, _ = bench(arrow=Arrow(fail=True))

    listed = client.get("/api/events", headers=principal(OWNER))

    assert listed.status_code == 503
    assert listed.json() == {"detail": "ARROW unavailable"}


def check_events_before_the_last_reset_are_marked_earlier() -> None:
    # Owner: after Reset "these cases should also all go away" from Events today.
    delay = {"flight_no": "AR259", "delay_minutes": 20, "cause": "staff_shortage"}
    loaded = NOW - timedelta(minutes=30)
    before, after = loaded - timedelta(minutes=60), loaded + timedelta(minutes=5)
    ids = [str(uuid.uuid4()) for _ in range(5)]
    listed_by_arrow = [
        # its case was closed by the reset
        received(
            ids[0], before, 2, case=view("a1", before + timedelta(seconds=3), "closed"), **delay
        ),
        # rejected as sent before the reset: it never became a case
        received(
            ids[1],
            before + timedelta(seconds=5),
            2,
            rejected={"status": "rejected", "reason": "Sent before the last Reset"},
            **delay,
        ),
        # still open: reset skipped it
        received(
            ids[2],
            before + timedelta(seconds=9),
            2,
            case=view("c3", before + timedelta(seconds=12), "assessment_ready"),
            **delay,
        ),
        # sent after the reset
        received(
            ids[3],
            after,
            2,
            case=view("d4", after + timedelta(seconds=3), "assessment_ready"),
            **delay,
        ),
        # a repeat of a1, before it
        received(
            ids[4],
            before + timedelta(seconds=20),
            2,
            case=view(
                "e5",
                before + timedelta(seconds=22),
                "closed",
                repeat_of={"trace_id": "a1", "display_id": "DSR-A1"},
            ),
            **delay,
        ),
    ]
    job = Job()
    job.last_success = lambda: loaded  # type: ignore[method-assign]
    _, client, _ = bench(arrow=Arrow(listed_by_arrow), job=job)

    listed = client.get("/api/events", params={"day": "2026-11-10"}, headers=principal(OWNER))

    assert listed.status_code == 200, listed.text
    events = {item["event_id"]: item for item in listed.json()["events"]}
    assert [events[i]["earlier"] for i in ids] == [True, True, False, False, True], events
    assert events[ids[0]]["outcome"] == "Closed by reset"


def check_reset_closes_cases_then_reloads_the_day() -> None:
    calls: list[str] = []
    arrow, job = Arrow(), Job()
    arrow.calls = job.calls = calls
    _, client, deps = bench(arrow=arrow, job=job)

    first = client.post("/api/reset", headers=principal(OWNER))
    again = client.post("/api/reset", headers=principal(OWNER))
    assert first.status_code == 202 and again.status_code == 409
    deps["spawn"].run()
    state = client.get("/api/reset", headers=principal(OWNER)).json()
    assert calls == ["close_cases", "job.start"], calls
    assert state["state"] == "done" and state["closed"] == ["DSR-20261110-AAAAAA"], state

    skipping = Arrow()
    skipping.skipped = [{"display_id": "DSR-20261110-BBBBBB", "reason": "executing"}]
    _, client, deps = bench(arrow=skipping)
    client.post("/api/reset", headers=principal(OWNER))
    deps["spawn"].run()
    state = client.get("/api/reset", headers=principal(OWNER)).json()
    assert state["state"] == "stopped" and deps["job"].calls == [], state
    assert "DSR-20261110-BBBBBB" in state["message"] and "retry in a minute" in state["message"]

    _, client, deps = bench(job=Job("Failed"))
    client.post("/api/reset", headers=principal(OWNER))
    deps["spawn"].run()
    assert client.get("/api/reset", headers=principal(OWNER)).json()["state"] == "failed"


def check_a_reset_shows_how_long_it_has_run_and_took() -> None:
    # Owner: "I also clicked to reload the day, but nothing happened since 30seconds".
    clock = [NOW]
    _, client, deps = bench(clock=clock)
    assert client.post("/api/reset", headers=principal(OWNER)).json()["elapsed_s"] == 0
    clock[0] = NOW + timedelta(seconds=42)
    running = client.get("/api/reset", headers=principal(OWNER)).json()
    assert running["state"] == "closing" and running["elapsed_s"] == 42, running
    clock[0] = NOW + timedelta(seconds=83)
    deps["spawn"].run()
    clock[0] = NOW + timedelta(seconds=300)
    done = client.get("/api/reset", headers=principal(OWNER)).json()
    assert done["state"] == "done" and done["took_s"] == 83 and "elapsed_s" not in done, done


RESET_JOB = "/subscriptions/s/jobs/caj-airrec-dayreset-dev"
NIGHTLY_JOB = "/subscriptions/s/jobs/caj-airrec-dayload-dev"


def check_reset_starts_the_reset_job_as_it_is_and_reads_the_latest_load() -> None:
    # Azure refuses a start that overrides the job's command without jobs/write, so Reset starts
    # a job whose own command reloads the day; "loaded" is the later of the nightly or reset run.
    calls: list[tuple[str, str, Any]] = []
    ends = {RESET_JOB: "2026-11-11T13:24:50+00:00", NIGHTLY_JOB: "2026-11-11T00:06:18+00:00"}

    def arm(credential: Any, method: str, path: str, api: str, body: Any = None) -> Any:
        calls.append((method, path, body))
        job = path.removesuffix("/executions")
        if method == "GET" and job in ends:
            return {"value": [{"properties": {"status": "Succeeded", "endTime": ends[job]}}]}
        return {"name": "caj-airrec-dayreset-dev-exec1"}

    with mock.patch.object(control, "_arm", arm):
        job = control.DayLoadJob(RESET_JOB, object(), NIGHTLY_JOB)
        name = job.start()
        loaded = job.last_success()

    assert name == "caj-airrec-dayreset-dev-exec1"
    assert calls[0] == ("POST", f"{RESET_JOB}/start", None), calls[0]
    assert loaded == datetime(2026, 11, 11, 13, 24, 50, tzinfo=UTC), loaded


def check_cases_show_their_expected_outcome_beside_the_actual_one() -> None:
    _, client, _ = bench()
    presets = client.get("/api/presets", headers=principal(OWNER)).json()["presets"]
    assert [(p["case"], p["expected"]) for p in presets] == [
        (1, "Executed automatically"),
        (2, "Executed automatically"),
        (3, "Waiting for OCC"),
        # Owner: the delay breaks the crew limit, so ARROW offers OCC the cancellation.
        (4, "Waiting for OCC"),
        # Owner: a strong crosswind read from a TAF; its plans wait for OCC.
        (5, "Waiting for OCC"),
        # Owner: once OCC approved, the forecast confirmed or dropped.
        (5, "Executed after approval"),
        (5, "Stood down"),
    ], presets
    assert [p["label"].split(" · ")[0] for p in presets] == [
        "Case 1",
        "Case 2",
        "Case 3",
        "Case 4",
        "Case 5",
        "Case 5",
        "Case 5",
    ]
    defect = presets[2]

    sent = NOW - timedelta(minutes=2)
    event_id = str(uuid.uuid4())
    message = received(
        event_id,
        sent,
        2,
        case=view("c3", sent + timedelta(seconds=5), "assessment_ready"),
        envelope=cloud_event("message", defect["data"], event_id=event_id, now=sent),
    )
    typed = received(
        str(uuid.uuid4()), sent, 2, case=None, flight_no="AR259", delay_minutes=20, cause="other"
    )
    _, client, _ = bench(arrow=Arrow([message, typed]))
    listed = client.get("/api/events", params={"day": "2026-11-10"}, headers=principal(OWNER))

    events = {item["event_id"]: item for item in listed.json()["events"]}
    assert events[event_id]["expected"] == {
        "case": 3,
        "title": defect["title"],
        "outcome": "Waiting for OCC",
    }, events[event_id]
    assert events[event_id]["outcome"] == "Waiting for OCC"
    assert events[typed["event_id"]]["expected"] is None


def check_the_taf_preset_is_written_for_the_day_it_is_sent() -> None:
    # A TAF names only days of the month (TAF weather case research §1.1): Case 5 is written when it
    # is sent, for today's Lisbon date and tomorrow's, so the demo works on any day, month end too.
    month_end = datetime(2026, 11, 30, 9, 20, tzinfo=UTC)
    _, client, deps = bench([month_end])
    sent = client.post("/api/events", json={"preset": "lppt-taf"}, headers=principal(OWNER))

    assert sent.status_code == 200, sent.text
    [event] = deps["sender"].sent
    assert event["type"] == "com.arrow.disruption.reported"
    assert event["data"] == {
        "text": "TAF AMD LPPT 300920Z 3010/0112 19012KT 9999 SCT020 BKN040 BECMG 3013/3015"
        " 21018G28KT 6000 RA BKN012 TEMPO 3016/3018 27022G35KT 6000 SHRA BKN015"
        " BECMG 3018/3020 30015KT 9999 NSW SCT020",
        "system": "IPMA (weather)",
    }, event["data"]
    listed = received(
        event["id"], month_end, 2, case=view("c5", month_end, "assessment_ready"), envelope=event
    )
    _, client, _ = bench([month_end], arrow=Arrow([listed]))
    [shown] = [
        p
        for p in client.get("/api/presets", headers=principal(OWNER)).json()["presets"]
        if p["key"] == "lppt-taf"
    ]
    assert shown["data"] == event["data"], shown
    # Owner: "write out what TAF is somewher".
    assert (shown["label"], shown["title"]) == (
        "Case 5 · Crosswind forecast",
        "Terminal Aerodrome Forecast (TAF) · Lisbon · strong crosswind 16:00–18:00"
        " · gusts 33 kt across runway 02/20 · 24 movements an hour · plans for OCC",
    ), shown
    rows = client.get("/api/events", params={"day": "2026-11-30"}, headers=principal(OWNER))
    [row] = rows.json()["events"]
    assert row["expected"] == {"case": 5, "title": shown["title"], "outcome": "Waiting for OCC"}


def check_the_storm_updates_are_written_for_the_day_they_are_sent() -> None:
    # Held until confirmed (case summary and held storm spec §2): after OCC approves the weather
    # plan, the Test Bench sends what settles it, a weather update for today's window: an
    # observation (METAR) with the gusts across the runway confirms it; a TAF amended without
    # them drops it.
    month_end = datetime(2026, 11, 30, 9, 20, tzinfo=UTC)
    _, client, deps = bench([month_end])
    for key in ("storm-confirmed", "storm-dropped"):
        sent = client.post("/api/events", json={"preset": key}, headers=principal(OWNER))
        assert sent.status_code == 200, sent.text

    confirmed, dropped = deps["sender"].sent
    for event in (confirmed, dropped):
        assert event["type"] == "com.arrow.weather.update", event
        assert event["specversion"] == "1.0" and event["source"] == "arrow-testbench", event
        assert str(uuid.UUID(event["id"])) == event["id"], event
    assert confirmed["data"] == {
        "airport": "LPPT",
        "taf_window": "3016/3018",
        "hazard": "crosswind",
        "status": "confirmed",
        "rate_per_hour": 24,
        "description": "METAR LPPT 301420Z 26021G34KT 9999 -SHRA FEW012 BKN025 15/11 Q1002"
        " TEMPO 27022G35KT 6000 SHRA",
    }, confirmed["data"]
    assert dropped["data"] == {
        "airport": "LPPT",
        "taf_window": "3016/3018",
        "hazard": "crosswind",
        "status": "dropped",
        "description": "TAF AMD LPPT 301310Z 3013/0112 21015KT 9999 SCT025 BKN040 TEMPO"
        " 3015/3019 22015G25KT 8000 -RA BKN015 BECMG 3019/3021 30012KT NSW SCT030",
    }, dropped["data"]

    shown = {
        p["key"]: p
        for p in client.get("/api/presets", headers=principal(OWNER)).json()["presets"]
        if p["key"].startswith("storm-")
    }
    assert {key: (p["label"], p["kind"], p["case"]) for key, p in shown.items()} == {
        "storm-confirmed": ("Case 5 · Crosswind confirmed (METAR)", "weather_update", 5),
        "storm-dropped": ("Case 5 · Forecast dropped (TAF amended)", "weather_update", 5),
    }, shown
    assert shown["storm-confirmed"]["data"] == confirmed["data"]
    # Listed with what it says and the outcome each should bring to the weather case.
    rows = [
        received(event["id"], month_end, 2, case=None, envelope=event)
        for event in (confirmed, dropped)
    ]
    _, client, _ = bench([month_end], arrow=Arrow(rows))
    listed = client.get("/api/events", params={"day": "2026-11-30"}, headers=principal(OWNER))
    by_id = {row["event_id"]: row for row in listed.json()["events"]}
    assert by_id[confirmed["id"]]["label"] == confirmed["data"]["description"][:69] + "…"
    assert [by_id[event["id"]]["expected"] for event in (confirmed, dropped)] == [
        {
            "case": 5,
            "title": shown["storm-confirmed"]["title"],
            "outcome": "Executed after approval",
        },
        {"case": 5, "title": shown["storm-dropped"]["title"], "outcome": "Stood down"},
    ]


def check_a_held_or_stood_down_storm_reads_as_such() -> None:
    sent = NOW - timedelta(minutes=5)
    held = view("c5", sent, "handled", execution={"status": "held", "approved_by": "occ"})
    down = view("c6", sent, "handled", execution={"status": "stood_down", "approved_by": "occ"})
    rows = [
        received(str(uuid.uuid4()), sent, 2, case=case, flight_no="AR259", delay_minutes=20)
        for case in (held, down)
    ]
    _, client, _ = bench(arrow=Arrow(rows))
    listed = client.get("/api/events", params={"day": "2026-11-10"}, headers=principal(OWNER))
    assert sorted(row["outcome"] for row in listed.json()["events"]) == [
        "Held until confirmed",
        "Stood down",
    ], listed.json()


def check_page_files_are_checked_again_after_a_release() -> None:
    # A guessed browser cache lifetime kept stale scripts beside new ones in ARROW.
    _, client, _ = bench()
    for path in ("/", "/app.js", "/styles.css"):
        served = client.get(path)
        assert served.status_code == 200, path
        assert served.headers.get("cache-control") == "no-cache", path


class TestBenchFlows(unittest.TestCase):
    def test_page_files_are_checked_again_after_a_release(self) -> None:
        check_page_files_are_checked_again_after_a_release()

    def test_cloud_event_has_one_new_id_per_send(self) -> None:
        check_cloud_event_has_one_new_id_per_send()

    def test_preset_send_publishes_a_cloud_event_and_reports_an_unavailable_queue(self) -> None:
        check_preset_send_publishes_a_cloud_event_and_reports_an_unavailable_queue()

    def test_events_merge_arrow_events_with_what_was_sent(self) -> None:
        check_events_merge_arrow_events_with_what_was_sent()

    def test_events_report_arrow_unavailable(self) -> None:
        check_events_report_arrow_unavailable()

    def test_events_before_the_last_reset_are_marked_earlier(self) -> None:
        check_events_before_the_last_reset_are_marked_earlier()

    def test_reset_closes_cases_then_reloads_the_day(self) -> None:
        check_reset_closes_cases_then_reloads_the_day()

    def test_a_reset_shows_how_long_it_has_run_and_took(self) -> None:
        check_a_reset_shows_how_long_it_has_run_and_took()

    def test_reset_starts_the_reset_job_as_it_is_and_reads_the_latest_load(self) -> None:
        check_reset_starts_the_reset_job_as_it_is_and_reads_the_latest_load()

    def test_cases_show_their_expected_outcome_beside_the_actual_one(self) -> None:
        check_cases_show_their_expected_outcome_beside_the_actual_one()

    def test_the_taf_preset_is_written_for_the_day_it_is_sent(self) -> None:
        check_the_taf_preset_is_written_for_the_day_it_is_sent()

    def test_the_storm_updates_are_written_for_the_day_they_are_sent(self) -> None:
        check_the_storm_updates_are_written_for_the_day_they_are_sent()

    def test_a_held_or_stood_down_storm_reads_as_such(self) -> None:
        check_a_held_or_stood_down_storm_reads_as_such()

    def test_only_a_listed_owner_is_admitted(self) -> None:
        check_only_a_listed_owner_is_admitted()
