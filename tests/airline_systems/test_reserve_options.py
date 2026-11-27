"""A crew duty a recovery option pushes past its FDP limit gets a legal reserve option from Crew."""

from __future__ import annotations

import base64
import json
import unittest
from collections import Counter
from datetime import datetime, time
from typing import Any

from arrow_scenario import ScenarioStore
from fastapi.testclient import TestClient
from system_callers import ARROW, READS, role_claims

from airline_systems import event_ids
from airline_systems.app import create_app
from airline_systems.defects import recovery_options
from airline_systems.facts import facts_of, reported
from airline_systems.operating_day import max_daily_fdp_minutes

TRACE = "0123456789abcdef0123456789abcdef"
EVENT = event_ids.event_id(TRACE)
OPERATOR = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
AGENT = "11111111-2222-3333-4444-555555555555"


def principal(oid: str = AGENT) -> dict[str, str]:
    roles = ARROW if oid == OPERATOR else READS
    claims = {"claims": [{"typ": "oid", "val": oid}, *role_claims(roles)]}
    return {
        "X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(claims).encode()).decode(),
        "traceparent": f"00-{TRACE}-00f067aa0ba902b7-01",
    }


def delay_facts(
    minutes: int = 60, flight_no: str = "AR131", cause: str = "staff_shortage"
) -> dict[str, Any]:
    """The delay as ARROW and the agents name it to the systems: its facts."""
    return {
        "type": "flight_delay",
        "flight_no": flight_no,
        "delay_minutes": minutes,
        "cause": cause,
    }


def delayed_store(*, reserves: bool = True, limit: int = 450) -> ScenarioStore:
    # D-LIS-001 reports 09:30; AR131 +60 -> 15:10-15:55, AR132 -> 16:30-17:15, release 17:45:
    # FDP (report to on-blocks) 465 against a 450-minute limit.
    store = ScenarioStore(TRACE)
    store.duties["D-LIS-001"]["max_fdp_minutes"] = limit
    if not reserves:
        store.reserves = [r for r in store.reserves if r["base"] != "LIS"]
    return store


def crew_client(store: ScenarioStore) -> TestClient:
    return TestClient(
        create_app(
            store, frozenset({AGENT, OPERATOR}), system="crew", writer_oids=frozenset({OPERATOR})
        )
    )


def at(store: ScenarioStore, hour: int, minute: int = 0) -> str:
    return datetime.combine(store.day, time(hour, minute)).isoformat()


def on_standby(
    store: ScenarioStore, start: tuple[int, int], end: tuple[int, int], kind: str = "home"
) -> None:
    """Put the scenario's standby crew on one window, at home or at the airport."""
    for reserve in store.reserves:
        reserve.update(start_local=at(store, *start), end_local=at(store, *end), standby_type=kind)


def offered(store: ScenarioStore, minutes: int = 60) -> dict[str, Any]:
    with crew_client(store) as client:
        response = client.get(
            "/api/recovery-options/delay/standby-crews",
            params=delay_facts(minutes),
            headers=principal(),
        )
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


def clock(value: str) -> str:
    return value[11:16]


def check_breached_duty_gets_a_legal_reserve_option() -> None:
    store = delayed_store()
    with crew_client(store) as client:
        reserve = client.get(
            "/api/recovery-options/delay/standby-crews",
            params=delay_facts(),
            headers=principal(),
        )
        without = client.get(
            "/api/recovery-options/delay/duty-plan", params=delay_facts(), headers=principal()
        )
        with_reserve = client.get(
            "/api/recovery-options/delay/duty-plan",
            params={**delay_facts(), "reserve_option_id": "R1"},
            headers=principal(),
        )

    assert reserve.status_code == 200, reserve.text
    body = reserve.json()
    assert body["breaches"] == [
        {"duty_id": "D-LIS-001", "fdp_minutes": 465, "max_fdp_minutes": 450}
    ], body
    [option] = body["options"]
    assert option["reserve_option_id"] == "R1" and option["duty_id"] == "D-LIS-001"
    assert option["split_after_flight"] == "AR102"
    assert option["flights_for_reserve"] == ["AR131", "AR132"]
    assert Counter(member["rank"] for member in option["crew"]) == Counter(
        {"CPT": 1, "FO": 1, "SC": 1, "CC": 3}
    )
    assert option["report_local"].endswith("T14:10:00")
    assert option["release_local"].endswith("T17:45:00")
    assert option["fdp_minutes"] <= option["max_fdp_minutes"]
    assert without.status_code == 200 and with_reserve.status_code == 200, with_reserve.text
    assert without.json()["breaches_unresolved"] == 1
    assert with_reserve.json()["breaches_unresolved"] == 0
    assert with_reserve.json()["plan_digest"] != without.json()["plan_digest"]
    assert "SYN0" not in reserve.text + without.text + with_reserve.text


def check_no_reserve_means_breach_stays_unresolved() -> None:
    store = delayed_store(reserves=False)
    with crew_client(store) as client:
        reserve = client.get(
            "/api/recovery-options/delay/standby-crews",
            params=delay_facts(),
            headers=principal(),
        )
        plan = client.get(
            "/api/recovery-options/delay/duty-plan", params=delay_facts(), headers=principal()
        )

    assert reserve.status_code == 200, reserve.text
    assert reserve.json()["options"] == []
    assert len(reserve.json()["breaches"]) == 1
    assert plan.json()["breaches_unresolved"] == 1
    assert "SYN0" not in reserve.text + plan.text


def check_a_home_standby_called_at_the_clock_reports_an_hour_later() -> None:
    # D-LIS-001's crew has not reported yet and, its limit cut to 170 min, may fly none of its
    # legs: a standby crew takes the whole duty from AR101 (10:30). On home standby from 06:00
    # it is called at the 09:45 clock, answers in 60 min and reports at 10:45, so AR101 cannot
    # leave with it before 11:45. A plan calling it leaves the breach and Crew refuses the write.
    store = delayed_store()
    store.duties["D-LIS-001"].update(report_local=at(store, 10), max_fdp_minutes=170)
    on_standby(store, (6, 0), (14, 0))
    delay = recovery_options(store, reported(store, facts_of(delay_facts())))["options"][0]
    originals = [dict(store.flights[f["flight_no"]]) for f in delay["flights"]]
    action = "0123456789ab-0001-late"

    [option] = offered(store)["options"]
    with crew_client(store) as client:
        plan = client.get(
            "/api/recovery-options/delay/duty-plan",
            params={**delay_facts(), "reserve_option_ids": "R1"},
            headers=principal(),
        )
        written = client.post(
            "/api/duty-changes",
            json={
                "flights": delay["flights"],
                "originals": originals,
                "reserve_option_ids": ["R1"],
            },
            headers={
                **principal(OPERATOR),
                "Idempotency-Key": action,
                "X-Request-Id": action,
                "If-Match": plan.json()["plan_digest"],
            },
        )

    assert option["split_after_flight"] is None, option
    assert option["flights_for_reserve"] == ["AR101", "AR102", "AR131", "AR132"], option
    assert option["standby_type"] == "home", option
    assert [
        clock(option[key])
        for key in ("called_local", "report_local", "departure_local", "earliest_departure_local")
    ] == ["09:45", "10:45", "10:30", "11:45"], option
    assert plan.status_code == 200, plan.text
    assert plan.json()["breaches_unresolved"] == 1, plan.json()
    assert "R1 reports 10:45, too late for AR101 at 10:30" in plan.json()["message"], plan.json()
    assert written.status_code == 409, written.text
    assert "too late for AR101" in written.json()["detail"], written.json()
    assert "D-LIS-001-R1" not in store.duties


def check_an_airport_standby_reports_at_the_call() -> None:
    # AR131 +60 leaves at 15:10, so its crew reports at 14:10. On airport standby from 14:00 the
    # crew is called at 14:10 and reports at once; on home standby in the same window it is
    # called at 14:00, reports at 15:00 and AR131 cannot leave with it before 16:00.
    found: dict[str, list[str]] = {}
    for kind in ("airport", "home"):
        store = delayed_store()
        on_standby(store, (14, 0), (18, 0), kind)
        [option] = offered(store)["options"]
        assert option["standby_type"] == kind, option
        found[kind] = [
            clock(option[key])
            for key in ("called_local", "report_local", "earliest_departure_local")
        ]

    assert found == {"airport": ["14:10", "14:10", "15:10"], "home": ["14:00", "15:00", "16:00"]}


def check_a_duty_ending_after_the_standby_window_is_covered() -> None:
    # Standby 08:00-16:00 (an island base's window): the crew reports at 14:10, inside it, and
    # flies AR131 and AR132 until 17:15. The window limits the report, not the end of the duty.
    store = delayed_store()
    on_standby(store, (8, 0), (16, 0))
    with crew_client(store) as client:
        reserve = client.get(
            "/api/recovery-options/delay/standby-crews",
            params=delay_facts(),
            headers=principal(),
        )
        plan = client.get(
            "/api/recovery-options/delay/duty-plan",
            params={**delay_facts(), "reserve_option_ids": "R1"},
            headers=principal(),
        )

    [option] = reserve.json()["options"]
    assert (clock(option["report_local"]), clock(option["release_local"])) == ("14:10", "17:45")
    assert plan.status_code == 200, plan.text
    assert plan.json()["breaches_unresolved"] == 0, plan.json()
    assert plan.json()["reserve_option_ids"] == ["R1"], plan.json()

    # A home standby may report up to an hour after its window: called at 13:10, inside
    # 06:00-14:00, it reports at 14:10. At the airport the report must fall inside the window.
    home = delayed_store()
    on_standby(home, (6, 0), (14, 0))
    [called] = offered(home)["options"]
    assert [clock(called[key]) for key in ("called_local", "report_local")] == ["13:10", "14:10"]
    airport = delayed_store()
    on_standby(airport, (10, 0), (14, 0), "airport")
    assert offered(airport)["options"] == []


def check_the_limit_falls_by_standby_time_beyond_six_hours() -> None:
    # AR131 +30 leaves at 14:40. A home standby since 06:00 reports at 13:40, 7 h 40 min into
    # its standby; 06:00-07:00 does not count before the call (CS FTL.1.225(b)), so its limit is
    # Table 2's at 13:40 for two sectors less the 40 min beyond six hours.
    store = delayed_store(limit=430)
    on_standby(store, (6, 0), (14, 0))

    [option] = offered(store, minutes=30)["options"]

    assert clock(option["report_local"]) == "13:40", option
    assert option["max_fdp_minutes"] == max_daily_fdp_minutes(time(13, 40), 2) - 40 == 725
    assert option["fdp_minutes"] <= option["max_fdp_minutes"], option


def standby(
    client: TestClient, path: str, body: dict[str, Any], key: str, trace: str = TRACE
) -> Any:
    return client.post(
        f"/api/standby-alerts{path}",
        json={"trace_id": trace, **body},
        headers={**principal(OPERATOR), "Idempotency-Key": key, "X-Request-Id": key},
    )


def standby_left_at_lisbon(client: TestClient) -> int:
    plan = client.get(
        "/api/recovery-options/delay/duty-plan", params=delay_facts(), headers=principal()
    )
    assert plan.status_code == 200, plan.text
    left = {(e["base"], e["qualification"]): e["crews"] for e in plan.json()["standby_left"]}
    return left[("LIS", "A320")]


def check_a_standby_crew_on_alert_stays_free_until_it_is_called() -> None:
    # Storm held until confirmed (case summary and held storm spec §2): approving a storm plan puts
    # its standby crews on alert with a call-by time (report minus 60 min). An alert is not a call:
    # the crew is still free for other cases until ARROW calls it; a stand-down releases it.
    store = delayed_store()
    [option] = offered(store)["options"]
    alert = {"reserve_option_id": "R1", "call_by_local": at(store, 13, 10), "crew": option["crew"]}
    other = "fedcba9876543210fedcba9876543210"
    with crew_client(store) as client:
        before = standby_left_at_lisbon(client)
        alerted = standby(client, "", {"crews": [alert]}, "0123456789ab-0001-alert")
        on_alert = standby_left_at_lisbon(client)
        twice = standby(client, "", {"crews": [alert]}, "0123456789ab-0002-alert")
        stranger = standby(
            client, "/call", {"reserve_option_ids": ["R2"]}, "0123456789ab-0003-call"
        )
        elsewhere = standby(
            client, "/call", {"reserve_option_ids": ["R1"]}, "0123456789ab-0004-call", other
        )
        called = standby(client, "/call", {"reserve_option_ids": ["R1"]}, "0123456789ab-0005-call")
        retry = standby(client, "/call", {"reserve_option_ids": ["R1"]}, "0123456789ab-0005-call")
        on_call = standby_left_at_lisbon(client)
        again = standby(client, "/call", {"reserve_option_ids": ["R1"]}, "0123456789ab-0006-call")
        released = standby(client, "/release", {}, "0123456789ab-0007-release")
        after = standby_left_at_lisbon(client)
        reader = client.post(
            "/api/standby-alerts/release",
            json={"trace_id": TRACE},
            headers={**principal(), "Idempotency-Key": "0123456789ab-0008-release"},
        )

    assert (before, on_alert, on_call, after) == (1, 1, 0, 1)
    assert alerted.status_code == 200, alerted.text
    assert alerted.json() == {
        "status": "alerted",
        "alerted": ["R1"],
        "message": "Standby crew R1 on alert: call by 13:10",
    }, alerted.json()
    assert (twice.status_code, twice.json()) == (
        409,
        {"detail": "R1 is already on alert for this case"},
    )
    assert (stranger.status_code, stranger.json()) == (
        409,
        {"detail": "R2 is not on alert for this case"},
    )
    assert (elsewhere.status_code, elsewhere.json()) == (
        409,
        {"detail": "R1 is not on alert for this case"},
    )
    assert called.status_code == 200, called.text
    assert called.json() == {
        "status": "called",
        "called": ["R1"],
        "message": "Called standby crew R1",
    }, called.json()
    assert retry.json() == called.json()
    assert (again.status_code, again.json()) == (409, {"detail": "R1 was already called"})
    assert released.status_code == 200, released.text
    assert released.json() == {
        "status": "released",
        "released": ["R1"],
        "message": "Released standby crew R1",
    }, released.json()
    assert reader.status_code == 403, reader.text
    # Nothing of the crew's roster changed: an alert, a call and a release are records.
    assert [r["crew_id"] for r in store.reserves] == [f"R00{n}" for n in range(1, 7)]
    assert "D-LIS-001-R1" not in store.duties
    # The audit keeps the crews' options and call-by times, not who is in them, under the
    # operations event the write belongs to.
    audited = [json.loads(call["request"]) for call in store.calls if call["request"]]
    assert {
        "event_id": EVENT,
        "request_id": "0123456789ab-0001-alert",
        "crews": [{"reserve_option_id": "R1", "call_by_local": at(store, 13, 10)}],
    } in audited, audited


OTHER_CASE = "fedcba9876543210fedcba9876543210"
# Each case's delay, as it names it to the systems: case A's AR131 +60, case B's AR132 +60.
DELAYS = {TRACE: delay_facts(), OTHER_CASE: delay_facts(flight_no="AR132", cause="late_inbound")}


def in_case(trace: str, oid: str = OPERATOR) -> dict[str, str]:
    """A caller's headers in one case: its identity (ARROW's by default) and the case's trace."""
    return {**principal(oid), "traceparent": f"00-{trace}-00f067aa0ba902b7-01"}


def crew_write(client: TestClient, store: ScenarioStore, trace: str, digest: str, key: str) -> Any:
    """A case's crew write calling standby crew R1 for its delay, bound to its duty plan."""
    found = reported(store, facts_of(DELAYS[trace]))
    delay = next(o for o in recovery_options(store, found)["options"] if o["option_id"] == "delay")
    originals = [dict(store.flights[f["flight_no"]]) for f in delay["flights"]]
    return client.post(
        "/api/duty-changes",
        json={"flights": delay["flights"], "originals": originals, "reserve_option_ids": ["R1"]},
        headers={**in_case(trace), "Idempotency-Key": key, "X-Request-Id": key, "If-Match": digest},
    )


def check_a_called_standby_crew_stays_with_its_case() -> None:
    # Review: a standby crew one case has called is not another case's to take. Case A
    # (AR131 +60) and case B (AR132 +60) both push D-LIS-001 past its limit and are both offered
    # Lisbon's one standby crew. A alerts and calls it; B is no longer offered it and its crew
    # write naming it is refused; A's duty change still assigns it. Each case reads the systems
    # in its own trace.
    store = delayed_store()
    [mine] = offered(store)["options"]

    def offers(client: TestClient, trace: str) -> list[dict[str, Any]]:
        found = client.get(
            "/api/recovery-options/delay/standby-crews",
            params=DELAYS[trace],
            headers=in_case(trace, AGENT),
        )
        assert found.status_code == 200, found.text
        options: list[dict[str, Any]] = found.json()["options"]
        return options

    def duty_plan(client: TestClient, trace: str) -> Any:
        return client.get(
            "/api/recovery-options/delay/duty-plan",
            params={**DELAYS[trace], "reserve_option_ids": "R1"},
            headers=in_case(trace, AGENT),
        )

    alert = {"reserve_option_id": "R1", "call_by_local": at(store, 13, 10), "crew": mine["crew"]}
    with crew_client(store) as client:
        before = offers(client, OTHER_CASE)
        theirs = duty_plan(client, OTHER_CASE).json()["plan_digest"]
        standby(client, "", {"crews": [alert]}, "0123456789ab-0021-alert")
        on_alert = offers(client, OTHER_CASE)
        standby(client, "/call", {"reserve_option_ids": ["R1"]}, "0123456789ab-0022-call")
        after = offers(client, OTHER_CASE)
        their_plan = duty_plan(client, OTHER_CASE)
        taken = crew_write(client, store, OTHER_CASE, theirs, "0123456789ab-0023-crew")
        ours = duty_plan(client, TRACE)
        kept = crew_write(
            client, store, TRACE, ours.json()["plan_digest"], "0123456789ab-0024-crew"
        )

    called = {member["crew_id"] for member in mine["crew"]}
    assert [{m["crew_id"] for m in option["crew"]} for option in before] == [called], before
    # On alert only, the crew is still free for another case.
    assert [{m["crew_id"] for m in option["crew"]} for option in on_alert] == [called], on_alert
    assert after == [], after
    assert (their_plan.status_code, their_plan.json()) == (
        409,
        {"detail": "Reserve option 'R1' is not available"},
    )
    assert (taken.status_code, taken.json()) == (
        412,
        {"detail": "Source changed: reserve option R1 is no longer offered"},
    )
    assert ours.status_code == 200 and ours.json()["breaches_unresolved"] == 0, ours.text
    assert kept.status_code == 200, kept.text
    assigned = {member["crew_id"] for member in store.duties["D-LIS-001-R1"]["members"]}
    assert assigned == called, store.duties["D-LIS-001-R1"]


def check_an_alert_names_crew_on_standby_today() -> None:
    store = delayed_store()
    [option] = offered(store)["options"]
    alert = {"reserve_option_id": "R1", "call_by_local": at(store, 13, 10), "crew": option["crew"]}
    stranger = {**alert, "crew": [*option["crew"][:-1], {"crew_id": "C9999", "rank": "CC"}]}
    with crew_client(store) as client:
        unknown = standby(client, "", {"crews": [stranger]}, "0123456789ab-0011-alert")
        first = standby(client, "", {"crews": [alert]}, "0123456789ab-0012-alert")
        # The same crew for another case while it is on alert for this one.
        taken = standby(
            client,
            "",
            {"crews": [alert]},
            "0123456789ab-0013-alert",
            "fedcba9876543210fedcba9876543210",
        )

    assert (unknown.status_code, unknown.json()) == (
        409,
        {"detail": "R1: C9999 is not on standby today"},
    )
    assert first.status_code == 200, first.text
    member = option["crew"][0]["crew_id"]
    assert (taken.status_code, taken.json()) == (
        409,
        {"detail": f"R1: {member} is already on alert for another case"},
    )


def check_a_crew_another_case_assigned_cannot_be_called() -> None:
    # Final review: case A puts Lisbon's standby crew on alert (an alert keeps it free);
    # case B's duty change then assigns it. A's call at its call-by time is refused in plain
    # words, as an alert naming crew not on standby is, and nothing is recorded as called.
    store = delayed_store()
    [mine] = offered(store)["options"]
    alert = {"reserve_option_id": "R1", "call_by_local": at(store, 13, 10), "crew": mine["crew"]}
    with crew_client(store) as client:
        alerted = standby(client, "", {"crews": [alert]}, "0123456789ab-0031-alert")
        theirs = client.get(
            "/api/recovery-options/delay/duty-plan",
            params={**DELAYS[OTHER_CASE], "reserve_option_ids": "R1"},
            headers=in_case(OTHER_CASE, AGENT),
        ).json()["plan_digest"]
        assigned = crew_write(client, store, OTHER_CASE, theirs, "0123456789ab-0032-crew")
        call = standby(client, "/call", {"reserve_option_ids": ["R1"]}, "0123456789ab-0033-call")

    assert alerted.status_code == 200 and assigned.status_code == 200, assigned.text
    member = mine["crew"][0]["crew_id"]
    assert (call.status_code, call.json()) == (
        409,
        {"detail": f"R1: {member} is no longer on standby today"},
    )
    assert [a["status"] for a in store.standby_alerts(store.day)] == ["alerted"]


class ReserveOptionTests(unittest.TestCase):
    def test_a_crew_another_case_assigned_cannot_be_called(self) -> None:
        check_a_crew_another_case_assigned_cannot_be_called()

    def test_breached_duty_gets_a_legal_reserve_option(self) -> None:
        check_breached_duty_gets_a_legal_reserve_option()

    def test_no_reserve_means_breach_stays_unresolved(self) -> None:
        check_no_reserve_means_breach_stays_unresolved()

    def test_a_home_standby_called_at_the_clock_reports_an_hour_later(self) -> None:
        check_a_home_standby_called_at_the_clock_reports_an_hour_later()

    def test_an_airport_standby_reports_at_the_call(self) -> None:
        check_an_airport_standby_reports_at_the_call()

    def test_a_duty_ending_after_the_standby_window_is_covered(self) -> None:
        check_a_duty_ending_after_the_standby_window_is_covered()

    def test_the_limit_falls_by_standby_time_beyond_six_hours(self) -> None:
        check_the_limit_falls_by_standby_time_beyond_six_hours()

    def test_a_standby_crew_on_alert_stays_free_until_it_is_called(self) -> None:
        check_a_standby_crew_on_alert_stays_free_until_it_is_called()

    def test_a_called_standby_crew_stays_with_its_case(self) -> None:
        check_a_called_standby_crew_stays_with_its_case()

    def test_an_alert_names_crew_on_standby_today(self) -> None:
        check_an_alert_names_crew_on_standby_today()
