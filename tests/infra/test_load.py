"""The load test (tools/load): its day of reports is valid input that the Test Bench itself would
send, about distinct aircraft after the demo clock, and its figures are computed as stated."""

from __future__ import annotations

from datetime import date, time

import pytest

from airline_systems.operating_day import build_operating_day
from tools.env.rest import RestError
from tools.load import scenario, send, summarize

DAY = date(2026, 11, 25)


class _Topic:
    """Service Bus answering each post with the next status."""

    def __init__(self, *statuses: int) -> None:
        self.statuses = list(statuses)
        self.posts = 0

    def request(self, method: str, url: str, body: object = None, **_: object) -> None:
        self.posts += 1
        status = self.statuses.pop(0)
        if status != 201:
            raise RestError(method, url, status, None)


def test_the_first_report_waits_only_for_the_new_role_to_take_effect() -> None:
    naps: list[float] = []
    event = {"id": "1", "type": "t"}
    topic = _Topic(403, 401, 201)
    send.post_once_allowed(topic, "sb", event, sleep=naps.append)  # type: ignore[arg-type]
    assert topic.posts == 3 and naps == [send.ACCESS_RETRY_SECONDS] * 2
    with pytest.raises(RestError):  # any other failure fails at once
        send.post_once_allowed(_Topic(500), "sb", event, sleep=naps.append)  # type: ignore[arg-type]
    late = iter([0.0, send.ACCESS_WAIT_SECONDS]).__next__
    with pytest.raises(RestError):  # and a refusal after the wait
        send.post_once_allowed(_Topic(403), "sb", event, sleep=naps.append, clock=late)  # type: ignore[arg-type]
    assert len(naps) == 2


def test_a_day_of_reports_is_valid_and_names_each_aircraft_once() -> None:
    reports, unused = scenario.day_reports(DAY, speed=8)
    flights = {f.flight_no: f for f in build_operating_day(DAY).flights}
    assert len(reports) == scenario.DELAYS + scenario.DEFECTS + 1
    for report in reports:
        report.checked()  # raises if the Test Bench would refuse it
    delayed = [flights[r.subject] for r in reports if r.subject in flights]
    assert all(f.std_local.time() >= time(10, 0) for f in delayed)
    aircraft = [f.aircraft_reg for f in delayed] + [
        r.subject for r in reports if r.subject.startswith("CS-")
    ]
    assert len(aircraft) == len(set(aircraft)) == scenario.DELAYS + scenario.DEFECTS
    assert not set(aircraft) & set(unused)
    assert max(r.offset_seconds for r in reports) <= 14.25 * 3600 / 8
    assert len(scenario.burst_reports(DAY, unused)) == len(unused) > 0


def test_the_figures_measure_waits_backlog_and_ended_windows() -> None:
    def case(opened: str, started: str | None, decided: str | None, **extra: object) -> dict:
        return {
            "id": "x",
            "event_id": "e",
            "kind": "flight_delay",
            "event_time": opened,
            "enqueued": opened,
            "opened": opened,
            "started": started,
            "decided": decided,
            "attempts": 1,
            "retries": 0,
            "status": "assessment_ready",
            "stage": "",
            **extra,
        }

    cases = [
        case("2026-11-25T10:00:00+00:00", "2026-11-25 10:00:10", "2026-11-25 10:01:30"),
        case("2026-11-25T10:00:05+00:00", "2026-11-25 10:00:20", "2026-11-25 10:02:00", retries=1),
        case(
            "2026-11-25T10:00:06+00:00",
            None,
            "2026-11-25 10:15:06",
            status="failed",
            stage="Assessment stopped: its 15-minute window ended before ARROW could start it.",
        ),
    ]
    figures = summarize.phase_figures(cases, [{"sent": True}] * 3)
    assert figures["queue_wait_seconds"] == {"p50": 10.0, "p90": 15.0, "p95": 15.0, "max": 15.0}
    assert figures["largest_backlog"] == 3
    assert figures["outcomes"] == {"plans_ready": 2, "window_ended": 1}
    assert figures["retries"] == 1 and figures["failures_by_stage"] == {}
